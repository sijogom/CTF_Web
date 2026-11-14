"""일반 첨부 편집과 기존 문제 접근 판정에 결속된 다운로드."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import UUID4
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, get_optional_user_id, get_optional_user_role, require_role
from app.database import get_db
from app.models.challenge import Challenge
from app.schemas.challenge_attachment import (
    AttachmentDraftRequest, AttachmentDraftResponse, AttachmentReconcileRequest, AttachmentUploadResponse,
)
from app.services.challenge_attachments import (
    create_draft, get_draft, get_upload_status, reconcile_upload, resolve_attachment, upload_attachment,
)

router = APIRouter(tags=["문제 첨부"])
editor = Depends(require_role("admin", "moderator"))


@router.post("/challenge-attachment-drafts", response_model=AttachmentDraftResponse)
async def post_attachment_draft(body: AttachmentDraftRequest, db: AsyncSession = Depends(get_db),
                                owner_id: UUID = Depends(get_current_user_id), _role: str = editor):
    return await create_draft(db, body, owner_id)


@router.get("/challenge-attachment-drafts/{draft_id}", response_model=AttachmentDraftResponse)
async def read_attachment_draft(draft_id: UUID, db: AsyncSession = Depends(get_db),
                                owner_id: UUID = Depends(get_current_user_id), _role: str = editor):
    return await get_draft(db, draft_id, owner_id)


@router.put("/challenge-attachment-drafts/{draft_id}/uploads/{upload_id}", response_model=AttachmentUploadResponse)
async def put_attachment_upload(draft_id: UUID4, upload_id: UUID4, file: UploadFile = File(...),
                                 db: AsyncSession = Depends(get_db), owner_id: UUID = Depends(get_current_user_id),
                                 _role: str = editor):
    return await upload_attachment(db, draft_id, upload_id, owner_id, file)


@router.get("/challenge-attachment-drafts/{draft_id}/uploads/{upload_id}", response_model=AttachmentUploadResponse)
async def read_attachment_upload(draft_id: UUID, upload_id: UUID, db: AsyncSession = Depends(get_db),
                                 owner_id: UUID = Depends(get_current_user_id), _role: str = editor):
    return await get_upload_status(db, draft_id, upload_id, owner_id)


@router.post("/challenge-attachment-drafts/{draft_id}/uploads/{upload_id}/reconcile", response_model=AttachmentUploadResponse)
async def reconcile_attachment_upload(draft_id: UUID, upload_id: UUID, body: AttachmentReconcileRequest,
                                      db: AsyncSession = Depends(get_db), owner_id: UUID = Depends(get_current_user_id),
                                      _role: str = editor):
    return await reconcile_upload(db, draft_id, upload_id, owner_id)


async def readable_challenge(db, challenge_id, role, user_id, *, competition_id=None):
    # 함수를 직접 호출하더라도 모든 dependency 값을 명시하여 기존 공개/잠금 정책을 재사용한다.
    from app.api.challenges import _require_competition_context, get_challenge
    if competition_id is None:
        detail = await get_challenge(challenge_id=challenge_id, lang="ko", db=db, role=role, user_id=user_id)
        if detail.get("locked"):
            raise HTTPException(403, detail="선행조건을 먼저 완료하세요.")
    challenge = await db.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(404, detail="문제를 찾을 수 없습니다.")
    if competition_id is not None:
        if user_id is None:
            raise HTTPException(401, detail="로그인이 필요합니다.")
        await _require_competition_context(challenge=challenge, competition_id=competition_id,
                                           user_id=user_id, purpose="view")
    return challenge


@router.get("/challenges/{challenge_id}/attachments/{asset_id}")
async def download_challenge_attachment(challenge_id: int, asset_id: UUID, db: AsyncSession = Depends(get_db),
                                        role: str | None = Depends(get_optional_user_role),
                                        user_id: UUID | None = Depends(get_optional_user_id)):
    challenge = await readable_challenge(db, challenge_id, role, user_id)
    return await resolve_attachment(db, challenge, asset_id=asset_id)


@router.get("/challenges/{challenge_id}/files/{file_index}")
async def download_legacy_challenge_attachment(challenge_id: int, file_index: int, db: AsyncSession = Depends(get_db),
                                               role: str | None = Depends(get_optional_user_role),
                                               user_id: UUID | None = Depends(get_optional_user_id)):
    challenge = await readable_challenge(db, challenge_id, role, user_id)
    return await resolve_attachment(db, challenge, file_index=file_index)


@router.get("/competitions/{competition_id}/challenges/{challenge_id}/attachments/{asset_id}")
async def download_competition_attachment(competition_id: str, challenge_id: int, asset_id: UUID,
                                          db: AsyncSession = Depends(get_db),
                                          user_id: UUID = Depends(get_current_user_id)):
    challenge = await readable_challenge(db, challenge_id, None, user_id, competition_id=competition_id)
    return await resolve_attachment(db, challenge, asset_id=asset_id)
