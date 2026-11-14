import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.email.sender import send_email
from app.email.templates import email_verification_template
from app.models.user import User
from app.schemas.auth import RegisterRequest, RegisterResponse
from app.security import pwd_context
from app.utils.admin_settings import (
    ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
    AdminSettingsStorageError,
    load_admin_settings,
)
from app.utils.audit import record_audit
from app.utils.email_verification import issue_verification_code

logger = logging.getLogger(__name__)
router = APIRouter(tags=["인증"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """신규 계정을 만들고, 이메일 인증 코드를 발급한다.

    인증 메일 발송은 DB 커밋 뒤 수행한다. 발송 실패가 인증 성공처럼 보이지 않도록
    응답에 delivery_status를 남기고, 계정은 미인증 상태로 재발송만 허용한다.
    """
    try:
        admin_settings, _ = await load_admin_settings(db)
    except AdminSettingsStorageError as exc:
        logger.warning("가입 허용 설정 조회 실패: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ADMIN_SETTINGS_UNAVAILABLE_DETAIL,
        ) from exc
    if not admin_settings.get("registration_open", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="현재 회원가입이 일시적으로 닫혀 있습니다.")

    # 이메일·사용자명 중복 확인
    existing_email = await db.execute(select(User).where(User.email == req.email))
    if existing_email.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 이메일입니다.")

    existing_username = await db.execute(select(User).where(User.username == req.username))
    if existing_username.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 사용자명입니다.")

    user = User(
        email=req.email,
        username=req.username,
        password_hash=pwd_context.hash(req.password),
        email_verified=False,
        email_verify_code_required=True,
    )
    db.add(user)
    await db.flush()  # 사용자 ID를 코드 HMAC 범위에 포함하기 위해 먼저 확보한다.
    verification_code = issue_verification_code(user)

    try:
        await record_audit(
            db,
            actor_id=str(user.id),
            action="auth.register_pending_verification",
            target_type="user",
            target_id=str(user.id),
            details={"username": user.username},
            request=request,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("신규 가입 저장 또는 감사 기록 실패")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="회원가입을 저장하지 못했습니다.")

    html = email_verification_template(
        user.username,
        verification_code=verification_code,
    )
    delivery_status = "sent"
    message = "인증 코드를 이메일로 발송했습니다. 코드를 입력해야 가입이 완료됩니다."
    try:
        await send_email(user.email, "[CTF Platform] 이메일 인증 코드", html)
    except Exception as exc:
        # 메일 주소·코드 원문은 로그·감사 정보에 남기지 않는다.
        delivery_status = "failed"
        message = "계정은 생성되었지만 인증 코드 발송에 실패했습니다. 잠시 후 인증 메일을 다시 요청해주세요."
        logger.warning(
            "신규 가입 인증 메일 발송 실패 (user_id=%s, error_type=%s)",
            user.id,
            type(exc).__name__,
        )
        try:
            await record_audit(
                db,
                actor_id=str(user.id),
                action="auth.verification_delivery_failed",
                target_type="user",
                target_id=str(user.id),
                details={"channel": "email"},
                request=request,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("가입 인증 메일 실패 감사 기록 저장 실패")

    return RegisterResponse(
        id=str(user.id),
        email=user.email,
        username=user.username,
        message=message,
        auto_verified=False,
        verification_required=True,
        delivery_status=delivery_status,
    )
