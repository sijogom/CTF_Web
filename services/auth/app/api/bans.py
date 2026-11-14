"""수동 검토 기반 계정 밴·이의제기 API."""

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, security
from app.api.refresh import _revoke_all_user_tokens
from app.database import get_db
from app.jwt.token import decode_token
from app.models.ban import AccountBan, BanAppeal
from app.models.user import User
from app.schemas.ban import BanAppealCreate, BanAppealReview, BanLiftRequest, ManualBanCreate
from app.utils.audit import record_audit
from app.utils.bans import get_active_ban, is_ban_active, public_ban_details, utc_now

router = APIRouter(tags=["계정 밴"])


def _serialize_ban(
    ban: AccountBan,
    *,
    username: str | None = None,
    email: str | None = None,
    include_evidence: bool = False,
) -> dict:
    now = utc_now()
    if is_ban_active(ban, now=now):
        state = "active"
    elif ban.lifted_at is not None:
        state = "lifted"
    else:
        state = "expired"

    result = {
        "id": str(ban.id),
        "user_id": str(ban.user_id),
        "username": username,
        "email": email,
        "public_reason": ban.public_reason,
        "policy_version": ban.policy_version,
        "source": ban.source,
        "detection_rule_version": ban.detection_rule_version,
        "started_at": ban.started_at.isoformat(),
        "expires_at": ban.expires_at.isoformat() if ban.expires_at else None,
        "created_by": str(ban.created_by),
        "created_at": ban.created_at.isoformat(),
        "lifted_at": ban.lifted_at.isoformat() if ban.lifted_at else None,
        "lifted_by": str(ban.lifted_by) if ban.lifted_by else None,
        "lift_reason": ban.lift_reason,
        "appeal_allowed": bool(ban.appeal_allowed),
        "status": state,
    }
    if include_evidence:
        result["evidence_summary"] = ban.evidence_summary
    return result


def _serialize_appeal(appeal: BanAppeal) -> dict:
    return {
        "id": str(appeal.id),
        "ban_id": str(appeal.ban_id),
        "user_id": str(appeal.user_id),
        "message": appeal.message,
        "status": appeal.status,
        "reviewer_id": str(appeal.reviewer_id) if appeal.reviewer_id else None,
        "reviewer_note": appeal.reviewer_note,
        "reviewed_at": appeal.reviewed_at.isoformat() if appeal.reviewed_at else None,
        "created_at": appeal.created_at.isoformat(),
    }


@router.get("/admin/bans")
async def list_bans(
    search: str = Query("", max_length=100),
    state: str | None = Query(None, pattern="^(active|lifted|expired)$"),
    source: str | None = Query(None, pattern="^(manual|automated)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    now = utc_now()
    query = select(AccountBan, User.username, User.email).outerjoin(User, User.id == AccountBan.user_id)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(
            User.username.ilike(f"%{escaped}%", escape="\\")
            | User.email.ilike(f"%{escaped}%", escape="\\")
        )
    if source:
        query = query.where(AccountBan.source == source)
    if state == "active":
        query = query.where(
            AccountBan.started_at <= now,
            AccountBan.lifted_at.is_(None),
            or_(AccountBan.expires_at.is_(None), AccountBan.expires_at > now),
        )
    elif state == "lifted":
        query = query.where(AccountBan.lifted_at.is_not(None))
    elif state == "expired":
        query = query.where(
            AccountBan.lifted_at.is_(None),
            AccountBan.expires_at.is_not(None),
            AccountBan.expires_at <= now,
        )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0
    rows = (
        await db.execute(
            query.order_by(AccountBan.started_at.desc()).offset((page - 1) * size).limit(size)
        )
    ).all()
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [
            _serialize_ban(ban, username=username, email=email)
            for ban, username, email in rows
        ],
    }


@router.get("/admin/bans/{ban_id}")
async def get_ban(
    ban_id: UUID,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    ban = await db.get(AccountBan, ban_id)
    if not ban:
        raise HTTPException(status_code=404, detail="밴 기록을 찾을 수 없습니다.")
    target = await db.get(User, ban.user_id)
    appeals = (
        await db.execute(
            select(BanAppeal).where(BanAppeal.ban_id == ban.id).order_by(BanAppeal.created_at.desc())
        )
    ).scalars().all()
    return {
        **_serialize_ban(
            ban,
            username=target.username if target else None,
            email=target.email if target else None,
            include_evidence=True,
        ),
        "appeals": [_serialize_appeal(appeal) for appeal in appeals],
    }


@router.post("/admin/bans", status_code=status.HTTP_201_CREATED)
async def create_manual_ban(
    body: ManualBanCreate,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    target = (
        await db.execute(select(User).where(User.id == body.target_user_id).with_for_update())
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="대상 사용자를 찾을 수 없습니다.")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="자기 자신은 밴할 수 없습니다.")
    if target.role == "admin":
        raise HTTPException(status_code=400, detail="다른 최고 관리자 계정은 밴할 수 없습니다.")
    if not target.is_active:
        raise HTTPException(status_code=409, detail="비활성화된 계정에는 새 밴을 만들 수 없습니다.")

    now = utc_now()
    if await get_active_ban(db, target.id, now=now, for_update=True):
        raise HTTPException(status_code=409, detail="대상 계정에 이미 활성 밴이 있습니다.")

    ban = AccountBan(
        user_id=target.id,
        public_reason=body.public_reason,
        evidence_summary=body.evidence_summary,
        policy_version=body.policy_version,
        source="manual",
        started_at=now,
        expires_at=None if body.permanent else now + timedelta(hours=body.duration_hours or 0),
        created_by=admin.id,
        appeal_allowed=body.appeal_allowed,
    )
    db.add(ban)
    await db.flush()
    await record_audit(
        db,
        actor_id=str(admin.id),
        action="ban.create",
        target_type="user",
        target_id=str(target.id),
        details={
            "ban_id": str(ban.id),
            "source": ban.source,
            "policy_version": ban.policy_version,
            "expires_at": ban.expires_at.isoformat() if ban.expires_at else None,
        },
        request=request,
    )
    await db.commit()

    refresh_revocation_pending = False
    try:
        await _revoke_all_user_tokens(str(target.id))
    except Exception:
        refresh_revocation_pending = True

    return {
        "ban": _serialize_ban(ban, username=target.username, email=target.email, include_evidence=True),
        "refresh_revocation_pending": refresh_revocation_pending,
    }


@router.post("/admin/bans/{ban_id}/lift")
async def lift_ban(
    ban_id: UUID,
    body: BanLiftRequest,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    ban = (
        await db.execute(select(AccountBan).where(AccountBan.id == ban_id).with_for_update())
    ).scalar_one_or_none()
    if not ban:
        raise HTTPException(status_code=404, detail="밴 기록을 찾을 수 없습니다.")
    if not is_ban_active(ban):
        raise HTTPException(status_code=409, detail="활성 상태의 밴만 해제할 수 있습니다.")

    ban.lifted_at = utc_now()
    ban.lifted_by = admin.id
    ban.lift_reason = body.reason
    await record_audit(
        db,
        actor_id=str(admin.id),
        action="ban.lift",
        target_type="ban",
        target_id=str(ban.id),
        details={"user_id": str(ban.user_id), "reason": body.reason},
        request=request,
    )
    await db.commit()
    return {"ban": _serialize_ban(ban, include_evidence=True)}


@router.post("/bans/appeals", status_code=status.HTTP_201_CREATED)
async def submit_ban_appeal(
    body: BanAppealCreate,
    request: Request,
    credentials=Depends(security),
    db: AsyncSession = Depends(get_db),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="밴 이의제기 토큰이 필요합니다.")
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "ban_appeal":
        raise HTTPException(status_code=401, detail="유효하지 않은 밴 이의제기 토큰입니다.")
    try:
        user_id = UUID(str(payload["sub"]))
        ban_id = UUID(str(payload["ban_id"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="유효하지 않은 밴 이의제기 토큰입니다.")

    ban = (
        await db.execute(select(AccountBan).where(AccountBan.id == ban_id).with_for_update())
    ).scalar_one_or_none()
    if not ban or ban.user_id != user_id:
        raise HTTPException(status_code=401, detail="유효하지 않은 밴 이의제기 토큰입니다.")
    if not ban.appeal_allowed:
        raise HTTPException(status_code=403, detail="이 밴에는 이의제기가 허용되지 않았습니다.")
    if not is_ban_active(ban):
        raise HTTPException(status_code=409, detail="활성 상태의 밴에만 이의제기를 제출할 수 있습니다.")

    existing = (
        await db.execute(
            select(BanAppeal.id).where(BanAppeal.ban_id == ban.id, BanAppeal.status == "pending")
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="이미 검토 대기 중인 이의제기가 있습니다.")

    appeal = BanAppeal(ban_id=ban.id, user_id=user_id, message=body.message)
    db.add(appeal)
    await db.flush()
    await record_audit(
        db,
        actor_id=str(user_id),
        action="ban.appeal_submit",
        target_type="ban",
        target_id=str(ban.id),
        details={"appeal_id": str(appeal.id)},
        request=request,
    )
    await db.commit()
    return {"appeal": _serialize_appeal(appeal)}


@router.post("/admin/bans/{ban_id}/appeals/{appeal_id}/review")
async def review_ban_appeal(
    ban_id: UUID,
    appeal_id: UUID,
    body: BanAppealReview,
    request: Request,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    appeal = (
        await db.execute(
            select(BanAppeal)
            .where(BanAppeal.id == appeal_id, BanAppeal.ban_id == ban_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not appeal:
        raise HTTPException(status_code=404, detail="이의제기 기록을 찾을 수 없습니다.")
    if appeal.status != "pending":
        raise HTTPException(status_code=409, detail="이미 검토된 이의제기입니다.")

    appeal.status = body.decision
    appeal.reviewer_id = admin.id
    appeal.reviewer_note = body.reviewer_note
    appeal.reviewed_at = utc_now()

    ban_lifted = False
    if body.lift_ban:
        ban = (
            await db.execute(select(AccountBan).where(AccountBan.id == ban_id).with_for_update())
        ).scalar_one_or_none()
        if not ban or not is_ban_active(ban):
            raise HTTPException(status_code=409, detail="활성 상태의 밴만 이의제기로 해제할 수 있습니다.")
        ban.lifted_at = appeal.reviewed_at
        ban.lifted_by = admin.id
        ban.lift_reason = body.lift_reason
        ban_lifted = True

    await record_audit(
        db,
        actor_id=str(admin.id),
        action="ban.appeal_review",
        target_type="ban",
        target_id=str(ban_id),
        details={
            "appeal_id": str(appeal.id),
            "decision": appeal.status,
            "ban_lifted": ban_lifted,
        },
        request=request,
    )
    await db.commit()
    return {"appeal": _serialize_appeal(appeal), "ban_lifted": ban_lifted}
