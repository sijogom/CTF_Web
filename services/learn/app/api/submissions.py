"""사용자 챌린지 등록 요청 API — 제출/목록/승인/거부."""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from uuid import UUID

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.api.deps import get_current_user_id, get_current_user_name, require_role
from app.models.challenge import Challenge, Hint, Submission, SubmissionFlagRevealAudit
from app.models.challenge_submission import ChallengeSubmission
from app.schemas.submission import SubmissionAuditDateTime, SubmissionFlagRevealRequest
from app.utils.flag_audit import (
    CHALLENGE_SUBMISSION_FLAG_ENCRYPTION_VERSION,
    FlagStorageUnavailable,
    build_challenge_submission_flag_metadata,
    build_submission_list_metadata,
    decrypt_challenge_submission_flag,
    decrypt_submitted_flag,
    encrypt_challenge_submission_flag,
    merge_stored_submission_audit_metadata,
)
from app.utils.challenge_submission_policy import user_submission_flag_type_is_allowed
from app.utils.safe_html import RichContentError, normalize_rich_content_for_write
from app.utils.minio_client import (
    MAX_FILE_SIZE,
    delete_pending_files,
    get_object_stream,
    close_object_stream,
    iter_object_stream,
    upload_challenge_file,
    validate_filename,
)
from app.services.challenge_attachments import pending_object_path, prepare_submission_attachments, record_submission_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/challenge-submissions", tags=["문제 제출"])

VALID_CATEGORIES = {"web", "crypto", "pwn", "rev", "forensic", "misc"}
VALID_DIFFICULTIES = {
    "beginner", "easy", "medium_low", "medium",
    "medium_high", "hard", "expert", "impossible",
}


# ──── 사용자: 문제 등록 요청 ────

@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_challenge(
    name: str = Form(..., min_length=1, max_length=200),
    category: str = Form(...),
    difficulty: str = Form(...),
    description: str = Form(""),
    value: int = Form(100),
    flag_type: str = Form("static"),
    flag_value: str = Form(""),
    tags: str = Form(""),  # 쉼표 구분
    hints_json: str = Form("[]"),  # JSON 문자열
    files: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    username: str = Depends(get_current_user_name),
):
    """일반 사용자가 문제 등록을 요청합니다. 파일은 MinIO에 안전하게 저장됩니다."""
    # 입력 검증
    if category not in VALID_CATEGORIES:
        raise HTTPException(400, f"유효하지 않은 카테고리: {category}")
    if difficulty not in VALID_DIFFICULTIES:
        raise HTTPException(400, f"유효하지 않은 난이도: {difficulty}")
    if not user_submission_flag_type_is_allowed(flag_type):
        raise HTTPException(400, "일반 문제 등록 요청은 static 또는 regex 플래그만 지원합니다.")
    if flag_type == "static" and not flag_value.strip():
        raise HTTPException(400, "static 플래그는 비어있을 수 없습니다.")

    try:
        encrypted_flag = encrypt_challenge_submission_flag(
            flag_value,
            encryption_key=settings.flag_storage_encryption_key,
        )
    except FlagStorageUnavailable as exc:
        # 새 문제 등록 요청이 원문 저장 fallback으로 되돌아가지 않도록 한다.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "플래그 저장 암호화 키가 준비되지 않아 문제 등록을 처리할 수 없습니다.",
        ) from exc

    # 본문과 힌트는 파일/DB 쓰기 전에 canonical HTML로 확정한다.
    try:
        normalized_description = normalize_rich_content_for_write(
            description,
            field_name="문제 설명",
            allow_empty=True,
            max_visible_chars=10000,
        )
    except RichContentError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.as_detail(),
        ) from error

    # 힌트 파싱
    import json
    try:
        hints_list = json.loads(hints_json) if hints_json else []
    except json.JSONDecodeError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "rich_content_review_required", "message": "힌트 JSON을 해석할 수 없습니다.", "field": "hints"},
        ) from error
    if not isinstance(hints_list, list):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "rich_content_review_required", "message": "힌트 목록은 배열이어야 합니다.", "field": "hints"},
        )
    normalized_hints: list[dict] = []
    for hint_index, hint_data in enumerate(hints_list):
        if not isinstance(hint_data, dict):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "rich_content_review_required", "message": "힌트 항목은 객체여야 합니다.", "field": f"hints[{hint_index}]"},
            )
        try:
            normalized_hint = dict(hint_data)
            normalized_hint["content"] = normalize_rich_content_for_write(
                hint_data.get("content", ""),
                field_name=f"힌트({hint_index})",
                max_visible_chars=2000,
            )
        except RichContentError as error:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
        normalized_hints.append(normalized_hint)

    # 태그 파싱
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # DB 제출 기록 생성 (파일 업로드 전 — ID 확보)
    submission = ChallengeSubmission(
        submitter_id=user_id,
        submitter_name=username,
        name=name,
        category=category,
        difficulty=difficulty,
        description=normalized_description,
        value=value,
        flag_type=flag_type,
        flag_value="[static-regex flag redacted]",
        flag_ciphertext=encrypted_flag,
        flag_key_version=settings.flag_storage_encryption_key_version,
        flag_storage_version=CHALLENGE_SUBMISSION_FLAG_ENCRYPTION_VERSION,
        flag_value_length=len(flag_value),
        tags=tag_list,
        hints=normalized_hints,
    )
    db.add(submission)
    await db.flush()  # ID 확보

    submission_id = str(submission.id)

    # 파일 업로드 (MinIO)
    uploaded_paths: list[str] = []
    for f in files:
        # 파일명 보안 검증
        error = validate_filename(f.filename or "")
        if error:
            raise HTTPException(400, error)

        data = await f.read()
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(400, f"파일 크기 제한 초과 (최대 {MAX_FILE_SIZE // 1024 // 1024}MB)")

        path = upload_challenge_file(
            data=data,
            original_filename=f.filename or "unknown",
            category=category,
            difficulty=difficulty,
            submission_id=submission_id,
            content_type=f.content_type or "application/octet-stream",
            pending=True,
        )
        uploaded_paths.append(path)
        await record_submission_upload(db, submission, path, f.filename or "unknown", data,
                                       f.content_type or "application/octet-stream")

    submission.file_paths = uploaded_paths
    await db.commit()
    await db.refresh(submission)

    return {
        "id": submission_id,
        "name": name,
        "status": "pending",
        "message": "문제 등록 요청이 접수되었습니다. 관리자 검토 후 등록됩니다.",
    }


# ──── 사용자: 내 제출 목록 ────

@router.get("/my")
async def my_submissions(
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """내가 제출한 문제 등록 요청 목록."""
    query = (
        select(ChallengeSubmission)
        .where(ChallengeSubmission.submitter_id == user_id)
        .order_by(ChallengeSubmission.created_at.desc())
    )
    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar() or 0

    result = await db.execute(query.offset((page - 1) * size).limit(size))
    items = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": str(s.id),
                "name": s.name,
                "category": s.category,
                "difficulty": s.difficulty,
                "status": s.status,
                "reject_reason": s.reject_reason,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in items
        ],
    }


# ──── 관리자: 전체 제출 목록 ────

@router.get("")
async def list_submissions(
    status_filter: str = Query("pending", description="pending / approved / rejected / all"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자: 문제 등록 요청 목록 조회."""
    query = select(ChallengeSubmission).order_by(ChallengeSubmission.created_at.desc())

    if status_filter != "all":
        query = query.where(ChallengeSubmission.status == status_filter)

    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar() or 0

    result = await db.execute(query.offset((page - 1) * size).limit(size))
    items = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": str(s.id),
                "name": s.name,
                "category": s.category,
                "difficulty": s.difficulty,
                "value": s.value,
                "submitter_name": s.submitter_name,
                "status": s.status,
                "file_count": len(s.file_paths or []),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in items
        ],
    }


# ──── 관리자: 제출 상세 조회 ────

@router.get("/{submission_id}")
async def get_submission(
    submission_id: str,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자: 제출 상세 조회 (파일 다운로드 URL 포함)."""
    result = await db.execute(
        select(ChallengeSubmission).where(ChallengeSubmission.id == submission_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "제출을 찾을 수 없습니다.")

    # 파일 다운로드 URL (백엔드 프록시 — presigned URL 노출 방지)
    file_urls = []
    for idx, path in enumerate(sub.file_paths or []):
        filename = path.rsplit("/", 1)[-1]
        download_url = f"/api/learn/challenge-submissions/{submission_id}/files/{idx}/download"
        # MinIO object paths are storage metadata, not an API response field.
        file_urls.append({"filename": filename, "url": download_url})

    storage_version = getattr(sub, "flag_storage_version", None) or "legacy-raw"
    encrypted = bool(getattr(sub, "flag_ciphertext", None))
    legacy_value = sub.flag_value if not encrypted and storage_version == "legacy-raw" else ""
    stored_length = getattr(sub, "flag_value_length", None)
    flag_metadata = build_challenge_submission_flag_metadata(
        legacy_value,
        fingerprint_secret=settings.internal_api_key,
        flag_length=stored_length if encrypted else None,
        storage_version=storage_version,
        flag_recorded=(stored_length or 0) > 0 if encrypted else bool(legacy_value),
    )

    return {
        "id": str(sub.id),
        "name": sub.name,
        "category": sub.category,
        "difficulty": sub.difficulty,
        "description": sub.description,
        "value": sub.value,
        "flag_type": sub.flag_type,
        # 승인 처리기는 서버 내부에서 원문을 사용하지만, 상세 HTTP 응답에는
        # 원문을 절대 포함하지 않는다.
        **flag_metadata,
        "tags": sub.tags or [],
        "hints": sub.hints or [],
        "submitter_name": sub.submitter_name,
        "submitter_id": str(sub.submitter_id),
        "status": sub.status,
        "reject_reason": sub.reject_reason,
        "files": file_urls,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
    }


# ──── 관리자: 승인 ────

@router.post("/{submission_id}/approve")
async def approve_submission(
    submission_id: str,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자: 문제 등록 요청을 승인하고 실제 챌린지로 등록."""
    from datetime import datetime, timezone

    result = await db.execute(
        select(ChallengeSubmission).where(ChallengeSubmission.id == submission_id).with_for_update()
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "제출을 찾을 수 없습니다.")
    if sub.status != "pending":
        raise HTTPException(400, f"이미 처리된 요청입니다 (현재 상태: {sub.status})")
    # 과거 데이터나 직접 DB 수정으로 유효하지 않은 값이 들어왔더라도, 승인 과정이
    # 환경 발급 정책 없는 dynamic_instance 문제를 만들지 않도록 다시 검사한다.
    if not user_submission_flag_type_is_allowed(sub.flag_type):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "이 제출의 플래그 유형은 관리자 동적 플래그 정책 없이 승인할 수 없습니다.",
        )

    try:
        normalized_description = normalize_rich_content_for_write(
            sub.description or "",
            field_name="문제 설명",
            allow_empty=True,
            max_visible_chars=10000,
        )
        raw_stored_hints = sub.hints or []
        if not isinstance(raw_stored_hints, list):
            raise RichContentError("힌트 목록은 배열이어야 합니다.", field_name="hints")
        normalized_hints: list[dict] = []
        for hint_index, hint_data in enumerate(raw_stored_hints):
            if not isinstance(hint_data, dict):
                raise RichContentError(
                    "힌트 항목은 객체여야 합니다.",
                    field_name=f"hints[{hint_index}]",
                )
            normalized_hint = dict(hint_data)
            normalized_hint["content"] = normalize_rich_content_for_write(
                hint_data.get("content", ""),
                field_name=f"힌트({hint_index})",
                max_visible_chars=2000,
            )
            normalized_hints.append(normalized_hint)
    except RichContentError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.as_detail(),
        ) from error

    storage_version = getattr(sub, "flag_storage_version", None) or "legacy-raw"
    try:
        if sub.flag_ciphertext:
            stored_flag_value = decrypt_challenge_submission_flag(
                sub.flag_ciphertext,
                encryption_key=settings.flag_storage_encryption_key,
            )
        elif storage_version == "legacy-raw":
            # 기존 pending 행은 승인 전까지 내부 호환을 유지한다. HTTP 응답에는
            # 원문을 반환하지 않으며, 신규 행은 위 암호화 경로만 만든다.
            stored_flag_value = sub.flag_value
        else:
            raise FlagStorageUnavailable("플래그 암호문이 없습니다.")
    except FlagStorageUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "플래그 저장을 검증할 수 없어 승인을 처리할 수 없습니다.",
        ) from exc

    # 챌린지 생성
    challenge = Challenge(
        name=sub.name,
        category=sub.category,
        difficulty=sub.difficulty,
        description=normalized_description,
        value=sub.value,
        flag_type=sub.flag_type,
        flag_value=stored_flag_value,
        tags=sub.tags or [],
        author_id=sub.submitter_id,
        author_name=sub.submitter_name,
        is_published=True,
    )
    db.add(challenge)
    await db.flush()  # challenge.id 확보

    # 힌트 생성
    for i, hint_data in enumerate(normalized_hints):
        hint = Hint(
            challenge_id=challenge.id,
            content=hint_data.get("content", ""),
            cost=hint_data.get("cost", 0),
            order_index=i,
        )
        db.add(hint)

    try:
        await prepare_submission_attachments(db, sub, challenge)
    except Exception as error:
        await db.rollback()
        raise HTTPException(503, "첨부 복사를 확인할 수 없어 승인을 처리하지 않았습니다.") from error

    # 제출 상태 업데이트
    sub.status = "approved"
    sub.reviewed_by = admin_id
    sub.reviewed_at = datetime.now(timezone.utc)

    await db.commit()

    return {
        "message": f"문제 '{sub.name}'이(가) 승인되어 등록되었습니다.",
        "challenge_id": challenge.id,
    }


# ──── 관리자: 거부 ────

@router.post("/{submission_id}/reject")
async def reject_submission(
    submission_id: str,
    reason: str = Form(""),
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자: 문제 등록 요청을 거부."""
    from datetime import datetime, timezone

    result = await db.execute(
        select(ChallengeSubmission).where(ChallengeSubmission.id == submission_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "제출을 찾을 수 없습니다.")
    if sub.status != "pending":
        raise HTTPException(400, f"이미 처리된 요청입니다 (현재 상태: {sub.status})")

    # pending 파일 삭제
    delete_pending_files(str(sub.id), sub.category, sub.difficulty)

    # 상태 업데이트
    sub.status = "rejected"
    sub.reject_reason = reason or "사유 없음"
    sub.reviewed_by = admin_id
    sub.reviewed_at = datetime.now(timezone.utc)
    # 거부된 신규 요청은 더 이상 승인에 사용할 필요가 없으므로 암호문을 즉시
    # 제거한다. 메타데이터는 상태·길이·감사 목적으로 유지한다.
    if getattr(sub, "flag_ciphertext", None):
        sub.flag_ciphertext = None
        sub.flag_key_version = None
        sub.flag_storage_version = "rejected-redacted"
        sub.flag_value = "[static-regex flag redacted]"
    await db.commit()

    return {"message": f"문제 '{sub.name}' 등록 요청이 거부되었습니다."}


# ──── 관리자: 제출 파일 다운로드 ────

@router.get("/{submission_id}/files/{file_index}/download")
async def download_submission_file(
    submission_id: str,
    file_index: int,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자: 제출 파일을 스트리밍으로 다운로드 (presigned URL 노출 방지)."""
    result = await db.execute(
        select(ChallengeSubmission).where(ChallengeSubmission.id == submission_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(404, "제출을 찾을 수 없습니다.")

    paths = sub.file_paths or []
    if file_index < 0 or file_index >= len(paths):
        raise HTTPException(404, "파일 인덱스가 유효하지 않습니다.")

    object_path = paths[file_index]
    if pending_object_path(object_path, sub.id) is None:
        raise HTTPException(404, "제출 소유 첨부를 찾을 수 없습니다.")
    try:
        stream, content_type, size = get_object_stream(object_path)
    except Exception:
        logger.error("MinIO 파일 다운로드 실패: %s", object_path, exc_info=True)
        raise HTTPException(500, "파일 다운로드에 실패했습니다.")

    filename = object_path.rsplit("/", 1)[-1]
    encoded_filename = quote(filename)

    return StreamingResponse(
        iter_object_stream(stream),
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
            "Content-Length": str(size),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(close_object_stream, stream),
    )


# ──── 관리자: 플래그 제출 감사 로그 ────

flag_logs_router = APIRouter(prefix="/submissions", tags=["플래그 제출 로그"])


# 감사 필터는 고정된 허용값만 받는다. 아래에서 요청값을 SQLAlchemy 식으로
# 매핑하므로, 요청값이 SQL 조각으로 사용되지 않는다.
_AUDIT_CONTEXTS = {"all", "wargame", "ctf"}
_AUDIT_CHALLENGE_TYPES = {"all", "wargame", "competition"}
_AUDIT_SORTS = {
    "submitted_at_desc",
    "submitted_at_asc",
    "challenge_name_asc",
    "result_desc",
}
_USERNAME_SEARCH_PAGE_SIZE = 100
_MAX_USERNAME_SEARCH_RESULTS = 500


def _escape_like(value: str) -> str:
    """호출자 값을 ILIKE에 넣기 전에 SQL LIKE 와일드카드를 이스케이프한다."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_audit_search(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} 검색어는 공백만 사용할 수 없습니다.",
        )
    return normalized


def _validate_audit_choice(value: str, allowed: set[str], field_name: str) -> str:
    if value not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} 값이 올바르지 않습니다.",
        )
    return value


def _normalize_audit_time(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name}에는 시간대가 포함된 ISO 8601 시간이 필요합니다.",
        )
    return value.astimezone(timezone.utc)


def _submission_log_order(sort: str) -> tuple[object, ...]:
    _validate_audit_choice(sort, _AUDIT_SORTS, "sort")
    orders: dict[str, tuple[object, ...]] = {
        "submitted_at_desc": (Submission.submitted_at.desc(), Submission.id.desc()),
        "submitted_at_asc": (Submission.submitted_at.asc(), Submission.id.asc()),
        "challenge_name_asc": (
            Challenge.name.asc(),
            Submission.submitted_at.desc(),
            Submission.id.desc(),
        ),
        "result_desc": (
            Submission.is_correct.desc(),
            Submission.submitted_at.desc(),
            Submission.id.desc(),
        ),
    }
    return orders[sort]


def build_admin_submission_audit_metadata(
    submitted_flag: str | None,
    *,
    fingerprint_secret: str | None = None,
) -> dict[str, object]:
    """저장된 플래그 제출값의 안전한 화면용 메타데이터만 반환한다.

    이는 암호화가 아니며 기존 저장·보존 경계를 변경하지 않는다. 기존 서비스 간
    비밀키가 설정된 경우에는 키드 다이제스트로 관리자에게 반복 시도를 연관시킬 수
    있게 한다. 비밀키가 없으면 안전하지 않은 raw hash로 대체하지 않고 지문을 생략한다.
    """
    value = submitted_flag or ""
    secret = settings.internal_api_key if fingerprint_secret is None else fingerprint_secret
    fingerprint: str | None = None
    if value and secret:
        digest = hmac.new(
            secret.encode("utf-8"),
            b"learn/submission-audit/v1:" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        fingerprint = f"hmac-sha256:{digest}"

    return {
        **build_submission_list_metadata(value),
        "submitted_flag_masked": "[redacted]" if value else None,
        "submitted_flag_fingerprint": fingerprint,
        "submitted_flag_fingerprint_algorithm": "hmac-sha256" if fingerprint else None,
    }


def _auth_proxy_headers(request: Request) -> dict[str, str]:
    """Auth 사용자 검색에 필요한 관리자 인증 정보만 전달한다."""
    authorization = request.headers.get("authorization")
    if authorization:
        return {"Authorization": authorization}

    access_token = request.cookies.get("access_token")
    if access_token:
        return {"Cookie": f"access_token={access_token}"}
    return {}


async def _search_user_ids_by_username(username: str, request: Request) -> list[UUID]:
    """Learn 기록을 조회하기 전에 Auth 소유 사용자명 필터를 ID로 해석한다.

    사용자명은 Learn DB에 복제하지 않는다. 제한된 proxy 호출로 Auth가 신원 검색의
    소유자임을 유지하며, 호출자 인증을 고정된 서비스 URL로 전달할 수 없으면
    fail-closed 한다.
    """
    import httpx

    headers = _auth_proxy_headers(request)
    if not headers:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="사용자명 검색을 위해 인증 서비스를 호출할 수 없습니다.",
        )

    user_ids: list[UUID] = []
    expected_total: int | None = None
    page = 1
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                response = await client.get(
                    f"{settings.auth_service_url}/admin/users",
                    params={
                        "search": username,
                        "page": page,
                        "size": _USERNAME_SEARCH_PAGE_SIZE,
                    },
                    headers=headers,
                )
                if response.status_code != 200:
                    logger.warning(
                        "Auth 사용자명 검색 실패: status=%s",
                        response.status_code,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="사용자명 검색 서비스를 사용할 수 없습니다.",
                    )

                payload = response.json()
                items = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(items, list):
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="사용자명 검색 서비스 응답이 올바르지 않습니다.",
                    )

                if expected_total is None:
                    total = payload.get("total", len(items))
                    if not isinstance(total, int) or total < 0:
                        raise HTTPException(
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="사용자명 검색 서비스 응답이 올바르지 않습니다.",
                        )
                    if total > _MAX_USERNAME_SEARCH_RESULTS:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="사용자명 검색 결과가 너무 많습니다. 더 구체적으로 검색하세요.",
                        )
                    expected_total = total

                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                        continue
                    try:
                        user_ids.append(UUID(item["id"]))
                    except ValueError:
                        logger.warning("Auth 사용자명 검색이 잘못된 사용자 ID를 반환했습니다")

                if expected_total is None or len(user_ids) >= expected_total or not items:
                    break
                page += 1
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Auth 사용자명 검색 요청 실패: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="사용자명 검색 서비스를 사용할 수 없습니다.",
        ) from exc

    return list(dict.fromkeys(user_ids))


async def _resolve_usernames(user_ids: set[str]) -> dict[str, str]:
    """감사 행의 신원 정보를 가능한 범위에서 보강하며, 실패 시 추가 정보를 노출하지 않는다."""
    if not user_ids:
        return {}

    import httpx

    headers = {"X-Internal-Key": settings.internal_api_key} if settings.internal_api_key else {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{settings.auth_service_url}/internal/users/batch",
                json=sorted(user_ids),
                headers=headers,
            )
        if response.status_code != 200:
            logger.warning("Auth 사용자 일괄 조회 실패: status=%s", response.status_code)
            return {}

        payload = response.json()
        if not isinstance(payload, list):
            logger.warning("Auth 사용자 일괄 조회가 올바르지 않은 응답을 반환했습니다")
            return {}
        return {
            item["id"]: item["username"]
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("username"), str)
        }
    except Exception as exc:
        logger.warning("Auth 사용자 일괄 조회 실패: %s", exc)
        return {}


@flag_logs_router.get("/logs")
async def get_submission_logs(
    request: Request,
    username: str | None = Query(None, max_length=50, description="사용자명 부분 검색"),
    challenge_search: str | None = Query(None, max_length=200, description="문제명 부분 검색"),
    competition_search: str | None = Query(None, max_length=36, description="대회 ID 부분 검색"),
    submission_context: str = Query("all", description="all / wargame / ctf"),
    challenge_type: str = Query("all", description="all / wargame / competition"),
    submitted_from: SubmissionAuditDateTime | None = Query(None, description="시간대 포함 ISO 8601 시작 시각"),
    submitted_to: SubmissionAuditDateTime | None = Query(None, description="시간대 포함 ISO 8601 종료 시각"),
    sort: str = Query("submitted_at_desc", description="submitted_at_desc / submitted_at_asc / challenge_name_asc / result_desc"),
    user_id: str | None = Query(None, max_length=36, description="유저 ID 필터"),
    challenge_id: int | None = Query(None, ge=1, description="챌린지 ID 필터"),
    competition_id: str | None = Query(None, max_length=36, description="대회 ID 필터"),
    is_correct: bool | None = Query(None, description="정답 여부 필터"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자용 플래그 제출 로그 조회 (필터 + 페이지네이션)."""
    query = select(Submission).join(Challenge, Submission.challenge_id == Challenge.id)

    submission_context = _validate_audit_choice(
        submission_context,
        _AUDIT_CONTEXTS,
        "submission_context",
    )
    challenge_type = _validate_audit_choice(
        challenge_type,
        _AUDIT_CHALLENGE_TYPES,
        "challenge_type",
    )
    submitted_from = _normalize_audit_time(submitted_from, "submitted_from")
    submitted_to = _normalize_audit_time(submitted_to, "submitted_to")
    if submitted_from and submitted_to and submitted_from > submitted_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="submitted_from은 submitted_to보다 늦을 수 없습니다.",
        )

    if username is not None:
        matched_user_ids = await _search_user_ids_by_username(
            _normalize_audit_search(username, "username"),
            request,
        )
        query = query.where(Submission.user_id.in_(matched_user_ids))

    if submission_context == "wargame":
        query = query.where(Submission.competition_id.is_(None))
    elif submission_context == "ctf":
        query = query.where(Submission.competition_id.is_not(None))
    if challenge_type != "all":
        query = query.where(Challenge.challenge_type == challenge_type)

    if user_id:
        try:
            query = query.where(Submission.user_id == UUID(_normalize_audit_search(user_id, "user_id")))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="user_id는 UUID 형식이어야 합니다.",
            ) from exc
    if challenge_id is not None:
        query = query.where(Submission.challenge_id == challenge_id)
    if competition_id:
        query = query.where(
            Submission.competition_id == _normalize_audit_search(competition_id, "competition_id")
        )
    if challenge_search is not None:
        normalized_challenge_search = _normalize_audit_search(
            challenge_search,
            "challenge_search",
        )
        query = query.where(
            Challenge.name.ilike(
                f"%{_escape_like(normalized_challenge_search)}%",
                escape="\\",
            )
        )
    if competition_search is not None:
        normalized_competition_search = _normalize_audit_search(
            competition_search,
            "competition_search",
        )
        query = query.where(
            Submission.competition_id.ilike(
                f"%{_escape_like(normalized_competition_search)}%",
                escape="\\",
            )
        )
    if is_correct is not None:
        query = query.where(Submission.is_correct == is_correct)
    if submitted_from:
        query = query.where(Submission.submitted_at >= submitted_from)
    if submitted_to:
        query = query.where(Submission.submitted_at <= submitted_to)

    # 전체 수
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # 페이지네이션 + 정렬
    query = query.order_by(*_submission_log_order(sort))
    query = query.offset((page - 1) * size).limit(size)

    result = await db.execute(
        query.add_columns(
            Challenge.name.label("challenge_name"),
            Challenge.challenge_type.label("challenge_type"),
        )
    )
    rows = result.all()

    # 신원 정보는 Auth가 소유한다. 보강 실패가 원문 제출값을 노출하거나 검색 범위를
    # 넓히면 안 되므로, 해당 행은 불투명 ID 축약값으로만 대체한다.
    username_map = await _resolve_usernames({str(row[0].user_id) for row in rows})

    items = []
    for sub, challenge_name, stored_challenge_type in rows:
        uid = str(sub.user_id)
        submission_audit_metadata = merge_stored_submission_audit_metadata(
            build_admin_submission_audit_metadata(sub.submitted_flag),
            submitted_flag_length=getattr(sub, "submitted_flag_length", None),
            submitted_flag_fingerprint=getattr(sub, "submitted_flag_fingerprint", None),
            submitted_flag_storage_version=getattr(sub, "submitted_flag_storage_version", None),
        )
        # 목록에는 원문·HMAC 지문을 표시하지 않는다. 원문은 명시적 admin-only
        # reveal endpoint에서만 열람하며, 존재 여부만 목록에 알려준다.
        submission_audit_metadata.pop("submitted_flag_fingerprint", None)
        submission_audit_metadata.pop("submitted_flag_fingerprint_algorithm", None)
        submission_audit_metadata["submitted_flag_revealable"] = bool(
            getattr(sub, "submitted_flag_ciphertext", None)
            or (
                getattr(sub, "submitted_flag_storage_version", None) == "legacy-raw"
                and sub.submitted_flag
                and not sub.submitted_flag.startswith("[")
            )
        )
        items.append({
            "id": str(sub.id),
            "user_id": uid,
            "username": username_map.get(uid, uid[:8] + "..."),
            "challenge_id": sub.challenge_id,
            "challenge_name": challenge_name,
            "challenge_type": stored_challenge_type,
            **submission_audit_metadata,
            "is_correct": sub.is_correct,
            "competition_id": sub.competition_id,
            "submission_context": "ctf" if sub.competition_id else "wargame",
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
        })

    return {"items": items, "total": total, "page": page, "size": size}


@flag_logs_router.get("/logs/{submission_id}/flag")
async def reject_legacy_submission_flag_reveal(
    submission_id: UUID,
    _role: str = Depends(require_role("admin")),
):
    """사유 없는 구형 GET으로는 제출 원문을 반환하지 않는다."""
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        detail="제출 원문 열람은 사유를 포함한 POST 요청이 필요합니다.",
        headers={"Allow": "POST"},
    )


@flag_logs_router.post("/logs/{submission_id}/flag")
async def reveal_submission_flag(
    submission_id: UUID,
    body: SubmissionFlagRevealRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """관리자만 제출 원문을 명시적으로 열람한다. 목록 응답에는 포함하지 않는다."""
    result = await db.execute(select(Submission).where(Submission.id == submission_id))
    submission = result.scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="제출을 찾을 수 없습니다.")

    storage_version = getattr(submission, "submitted_flag_storage_version", None) or "legacy-raw"
    try:
        if getattr(submission, "submitted_flag_ciphertext", None):
            value = decrypt_submitted_flag(
                submission.submitted_flag_ciphertext,
                encryption_key=settings.flag_storage_encryption_key,
                fallback_secret=settings.internal_api_key,
            )
        elif storage_version == "legacy-raw" and submission.submitted_flag and not submission.submitted_flag.startswith("["):
            value = submission.submitted_flag
        else:
            raise FlagStorageUnavailable("이 제출의 원문은 보존되지 않았습니다.")
    except FlagStorageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이 제출의 원문은 보존되지 않았거나 복호화할 수 없습니다.",
        ) from exc

    try:
        db.add(SubmissionFlagRevealAudit(submission_id=submission.id, actor_id=actor_id, reason=body.reason))
        await db.commit()
    except Exception as exc:
        await db.rollback()
        # 원문 열람은 감사 원장 기록 없이는 성공으로 처리하지 않는다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="제출 원문 열람 감사 기록에 실패했습니다.",
        ) from exc
    return {
        "id": str(submission.id),
        "submitted_flag": value,
        "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
    }
