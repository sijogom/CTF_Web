"""일반 첨부 원장, 저장소 확인, 부모 transaction 연결.

업로드 reservation만 먼저 commit한다. 부모 연결 helper는 commit하지 않으며,
MinIO 응답 유실을 재PUT 권한으로 바꾸지 않는다.
"""

import hashlib
import re
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.challenge import Challenge
from app.models.challenge_attachment import ChallengeAttachment, ChallengeAttachmentDraft
from app.schemas.challenge_attachment import (
    AttachmentDraftResponse, AttachmentUploadResponse, ChallengeAttachmentResponse,
)
from app.utils.minio_client import (
    CHALLENGE_PENDING_BUCKET, CHALLENGE_PRIVATE_BUCKET, EXTENSION_CONTENT_TYPE,
    MAX_FILE_SIZE, close_object_stream, copy_pending_challenge_object, ensure_challenge_private_bucket, get_file_extension,
    get_object_stream, iter_object_stream, put_private_challenge_object, stat_challenge_object,
    validate_filename, verify_challenge_object,
)


def storage_unavailable():
    return HTTPException(503, detail="첨부 저장소 상태를 확인할 수 없습니다. 상태 조회 후 다시 확인하세요.")


def attachment_conflict(code, message):
    return HTTPException(409, detail={"code": code, "message": message})


def require_edit_draft(body):
    if "attachment_ids" in body.model_fields_set and body.attachment_draft_id is None:
        raise HTTPException(422, detail={"code": "attachment_draft_required",
            "message": "첨부 목록을 변경하려면 현재 문제의 첨부 초안이 필요합니다."})


def private_path(owner_id, draft_id, upload_id):
    return f"{CHALLENGE_PRIVATE_BUCKET}/attachments/{owner_id}/{draft_id}/{upload_id}"


def validate_upload_identity(asset, *, owner_id, draft_id, upload_id, original_name, byte_size, sha256):
    if asset.owner_id != owner_id or asset.draft_id != draft_id or asset.id != upload_id:
        raise HTTPException(404, detail="첨부를 찾을 수 없습니다.")
    if (asset.original_name, asset.byte_size, asset.sha256) != (original_name, byte_size, sha256):
        raise attachment_conflict("attachment_upload_conflict", "같은 업로드 ID에 다른 파일을 사용할 수 없습니다.")


def binding_revision(current_ids, requested_ids, *, base_revision, current_revision,
                     current_files=(), requested_files=()):
    if list(current_ids) == list(requested_ids) and list(current_files) == list(requested_files):
        return current_revision
    if base_revision is not None and base_revision != current_revision:
        raise attachment_conflict("attachment_revision_conflict", "첨부가 다른 편집에서 변경되었습니다. 새 초안으로 다시 확인하세요.")
    return current_revision + 1


def project_upload_status(asset, *, active, observed_state):
    verified = not active and observed_state == "verified"
    missing = not active and observed_state == "missing"
    ready = verified and asset.state == "ready"
    return AttachmentUploadResponse(
        id=asset.id, draft_id=asset.draft_id, original_name=asset.original_name,
        byte_size=asset.byte_size, sha256=asset.sha256, content_type=asset.content_type,
        state="ready" if verified else "missing" if missing else "pending",
        ready_for_attach=ready, reconcile_required=not ready and not missing,
        retry_allowed=missing and asset.bound_challenge_id is None,
    )


async def try_upload_lock(db, upload_id):
    # transaction-level lock은 같은 AsyncSession의 물리 연결에서 객체 I/O까지 유지된다.
    key = int.from_bytes(hashlib.sha256(UUID(str(upload_id)).bytes).digest()[:8], "big", signed=True)
    return bool(await db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}))


async def owned_draft(db, draft_id, owner_id, *, lock=False):
    query = select(ChallengeAttachmentDraft).where(
        ChallengeAttachmentDraft.id == draft_id, ChallengeAttachmentDraft.owner_id == owner_id,
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="첨부 초안을 찾을 수 없습니다.")
    return row


async def owned_upload(db, draft_id, upload_id, owner_id):
    await owned_draft(db, draft_id, owner_id)
    row = (await db.execute(select(ChallengeAttachment).where(
        ChallengeAttachment.id == upload_id, ChallengeAttachment.draft_id == draft_id,
        ChallengeAttachment.owner_id == owner_id,
    ))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, detail="첨부를 찾을 수 없습니다.")
    if row.object_path != private_path(owner_id, draft_id, upload_id):
        raise HTTPException(409, detail="편집 업로드가 아닌 첨부입니다.")
    return row


async def observe_upload(asset):
    try:
        return await run_in_threadpool(verify_challenge_object, asset.object_path, asset.byte_size, asset.sha256)
    except Exception:
        return "unknown"


async def get_upload_status(db, draft_id, upload_id, owner_id):
    asset = await owned_upload(db, draft_id, upload_id, owner_id)
    acquired = await try_upload_lock(db, upload_id)
    observed = await observe_upload(asset) if acquired else "unknown"
    return project_upload_status(asset, active=not acquired, observed_state=observed)


async def reconcile_upload(db, draft_id, upload_id, owner_id):
    asset = await owned_upload(db, draft_id, upload_id, owner_id)
    acquired = await try_upload_lock(db, upload_id)
    if not acquired:
        return project_upload_status(asset, active=True, observed_state="unknown")
    observed = await observe_upload(asset)
    if observed == "verified":
        asset.state = "ready"
        await db.commit()
    return project_upload_status(asset, active=False, observed_state=observed)


async def upload_attachment(db, draft_id, upload_id, owner_id, file: UploadFile):
    name = file.filename or ""
    error = validate_filename(name)
    if error:
        raise HTTPException(422, detail=error)
    digest, size = hashlib.sha256(), 0
    while chunk := await file.read(64 * 1024):
        size += len(chunk)
        if size > MAX_FILE_SIZE:
            raise HTTPException(413, detail="파일은 100MiB를 초과할 수 없습니다.")
        digest.update(chunk)
    await file.seek(0)
    fingerprint = digest.hexdigest()
    draft = await owned_draft(db, draft_id, owner_id)
    existing = (await db.execute(select(ChallengeAttachment).where(ChallengeAttachment.id == upload_id))).scalar_one_or_none()
    if existing is None:
        if draft.state != "open":
            raise attachment_conflict("attachment_draft_closed", "저장된 초안에는 새 파일을 추가할 수 없습니다.")
        asset = ChallengeAttachment(
            id=upload_id, owner_id=owner_id, draft_id=draft_id, original_name=name,
            byte_size=size, sha256=fingerprint,
            content_type=EXTENSION_CONTENT_TYPE.get(get_file_extension(name), "application/octet-stream"),
            object_path=private_path(owner_id, draft_id, upload_id), state="pending",
        )
        db.add(asset)
        try:
            # 이 commit이 성공한 뒤에만 object PUT을 허용한다.
            await db.commit()
        except IntegrityError:
            await db.rollback()
            asset = (await db.execute(select(ChallengeAttachment).where(ChallengeAttachment.id == upload_id))).scalar_one()
        except Exception as error:
            await db.rollback()
            raise storage_unavailable() from error
    else:
        asset = existing
    validate_upload_identity(asset, owner_id=owner_id, draft_id=draft_id, upload_id=upload_id,
                             original_name=name, byte_size=size, sha256=fingerprint)
    acquired = await try_upload_lock(db, upload_id)
    if not acquired:
        return project_upload_status(asset, active=True, observed_state="unknown")
    draft = await owned_draft(db, draft_id, owner_id, lock=True)
    if draft.state != "open" and asset.bound_challenge_id is None:
        raise attachment_conflict("attachment_draft_closed", "저장된 초안에는 새 파일을 추가할 수 없습니다.")
    try:
        await run_in_threadpool(ensure_challenge_private_bucket)
    except Exception:
        return project_upload_status(asset, active=False, observed_state="unknown")
    observed = await observe_upload(asset)
    if observed != "missing":
        return project_upload_status(asset, active=False, observed_state=observed)
    if draft.state != "open" or asset.bound_challenge_id is not None:
        raise attachment_conflict("attachment_draft_closed", "이미 연결된 첨부를 재업로드할 수 없습니다.")
    try:
        await run_in_threadpool(put_private_challenge_object, asset.object_path, file.file, size, asset.content_type)
    except Exception:
        # 응답 유실도 pending으로 남긴다. 같은 요청에서 무조건 재PUT하지 않는다.
        return project_upload_status(asset, active=False, observed_state="unknown")
    observed = await observe_upload(asset)
    if observed == "verified":
        asset.state = "ready"
        await db.commit()
    return project_upload_status(asset, active=False, observed_state=observed)


async def create_draft(db, request, owner_id):
    challenge = None
    if request.challenge_id is not None:
        challenge = (await db.execute(select(Challenge).where(Challenge.id == request.challenge_id).with_for_update())).scalar_one_or_none()
        if challenge is None:
            raise HTTPException(404, detail="문제를 찾을 수 없습니다.")
    existing = (await db.execute(select(ChallengeAttachmentDraft).where(ChallengeAttachmentDraft.id == request.draft_id))).scalar_one_or_none()
    if existing is not None:
        if existing.owner_id != owner_id:
            raise HTTPException(404, detail="첨부 초안을 찾을 수 없습니다.")
        if existing.original_challenge_id != request.challenge_id:
            raise HTTPException(409, detail="초안의 대상 문제를 변경할 수 없습니다.")
        return await get_draft(db, request.draft_id, owner_id)
    db.add(ChallengeAttachmentDraft(id=request.draft_id, owner_id=owner_id,
        challenge_id=request.challenge_id, original_challenge_id=request.challenge_id,
        base_attachment_revision=challenge.attachment_revision if challenge else 0))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await owned_draft(db, request.draft_id, owner_id)
        if existing.original_challenge_id != request.challenge_id:
            raise HTTPException(409, detail="초안의 대상 문제를 변경할 수 없습니다.")
    return await get_draft(db, request.draft_id, owner_id)


async def get_draft(db, draft_id, owner_id):
    draft = await owned_draft(db, draft_id, owner_id)
    challenge = await db.get(Challenge, draft.challenge_id) if draft.challenge_id else None
    if draft.challenge_id and challenge is None:
        raise HTTPException(409, detail="대상 문제가 삭제되었습니다.")
    staged = (await db.execute(select(ChallengeAttachment).where(
        ChallengeAttachment.draft_id == draft_id, ChallengeAttachment.bound_challenge_id.is_(None),
    ).order_by(ChallengeAttachment.created_at, ChallengeAttachment.id))).scalars().all()
    uploads = [await get_upload_status(db, draft_id, row.id, owner_id) for row in staged]
    return AttachmentDraftResponse(draft_id=draft.id, challenge_id=draft.challenge_id, state=draft.state,
        base_attachment_revision=draft.base_attachment_revision,
        current_attachment_revision=challenge.attachment_revision if challenge else 0,
        attachments=await attachment_descriptors(db, challenge) if challenge else [], uploads=uploads)


async def bind_attachments(db, challenge, *, actor_id, attachment_ids=None, attachment_draft_id=None,
                           previous_files=None, new_challenge=False):
    """부모 FOR UPDATE 이후 호출. 연결·files revision은 부모 transaction 하나다."""
    draft = None
    if attachment_draft_id is not None:
        draft = await owned_draft(db, attachment_draft_id, actor_id, lock=True)
        if ((draft.challenge_id is None and not new_challenge)
                or draft.challenge_id not in (None, challenge.id)
                or (draft.state == "bound" and draft.challenge_id != challenge.id)):
            raise attachment_conflict("attachment_draft_target_conflict", "초안의 대상 문제와 일치하지 않습니다.")
    current = (await db.execute(select(ChallengeAttachment).where(
        ChallengeAttachment.challenge_id == challenge.id,
    ).order_by(ChallengeAttachment.position, ChallengeAttachment.id).with_for_update())).scalars().all()
    current_ids = [row.id for row in current]
    requested = current_ids if attachment_ids is None else list(attachment_ids)
    all_ids = set(current_ids) | set(requested)
    rows = (await db.execute(select(ChallengeAttachment).where(ChallengeAttachment.id.in_(all_ids))
                            .order_by(ChallengeAttachment.id).with_for_update())).scalars().all() if all_ids else []
    by_id = {row.id: row for row in rows}
    for identifier in requested:
        row = by_id.get(identifier)
        if row is None:
            raise attachment_conflict("attachment_reference_conflict", "첨부 참조를 찾을 수 없습니다.")
        if row.bound_challenge_id is not None:
            if row.bound_challenge_id != challenge.id:
                raise attachment_conflict("attachment_reference_conflict", "다른 문제의 첨부를 연결할 수 없습니다.")
        elif draft is None or row.owner_id != actor_id or row.draft_id != draft.id:
            raise attachment_conflict("attachment_reference_conflict", "현재 초안의 업로드만 연결할 수 있습니다.")
        if row.state != "ready" or row.object_path != private_path(row.owner_id, row.draft_id, row.id):
            raise attachment_conflict("attachment_not_ready", "아직 저장 가능한 첨부가 아닙니다.")
        if row.challenge_id != challenge.id and await observe_upload(row) != "verified":
            raise storage_unavailable()
    old_files = challenge.files or [] if previous_files is None else previous_files
    revision = binding_revision(current_ids, requested,
        base_revision=draft.base_attachment_revision if draft else None,
        current_revision=challenge.attachment_revision or 0,
        current_files=old_files, requested_files=challenge.files or [])
    for row in current:
        if row.id not in requested:
            row.challenge_id = None
    for position, identifier in enumerate(requested):
        row = by_id[identifier]
        row.challenge_id, row.bound_challenge_id, row.position = challenge.id, challenge.id, position
    challenge.attachment_revision = revision
    if draft:
        draft.challenge_id, draft.state = challenge.id, "bound"


def legacy_object_path(value, challenge_id):
    if not isinstance(value, str):
        return None
    for prefix in ("/ctf/storage/", "/storage/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    parts = value.split("/")
    if len(parts) != 5 or parts[0] != "challenges" or parts[3] != str(challenge_id):
        return None
    if any(not part or part in {".", ".."} or "\\" in part or "%" in part or re.search(r"[\x00-\x1f\x7f?#]", part) for part in parts):
        return None
    return value


def pending_object_path(value, submission_id):
    if not isinstance(value, str):
        return None
    parts = value.split("/")
    if len(parts) != 5 or parts[0] != CHALLENGE_PENDING_BUCKET or parts[3] != str(submission_id):
        return None
    if any(not p or p in {".", ".."} or "\\" in p or re.search(r"[\x00-\x1f\x7f?#%]", p) for p in parts):
        return None
    return value


def legacy_pending_metadata(source):
    stat = stat_challenge_object(source)
    if stat is None or stat.size > MAX_FILE_SIZE:
        raise storage_unavailable()
    stream, content_type, size = get_object_stream(source)
    try:
        digest, actual_size = hashlib.sha256(), 0
        for chunk in iter_object_stream(stream):
            actual_size += len(chunk)
            if actual_size > MAX_FILE_SIZE:
                raise storage_unavailable()
            digest.update(chunk)
        if actual_size != size:
            raise storage_unavailable()
    finally:
        close_object_stream(stream)
    return size, content_type, digest.hexdigest()


def download_path(challenge_id, *, asset_id=None, index=None, competition_id=None):
    prefix = f"/competitions/{competition_id}" if competition_id else ""
    suffix = f"attachments/{asset_id}" if asset_id is not None else f"files/{index}"
    return f"/api/learn{prefix}/challenges/{challenge_id}/{suffix}"


async def attachment_descriptors(db, challenge, *, competition_id=None):
    result = []
    for index, value in enumerate(challenge.files or []):
        path = legacy_object_path(value, challenge.id)
        try:
            stat = await run_in_threadpool(stat_challenge_object, path) if path else None
        except Exception:
            raise storage_unavailable()
        result.append(ChallengeAttachmentResponse(legacy_index=index,
            name=value.rsplit("/", 1)[-1], byte_size=stat.size if stat else None,
            state="ready" if stat else "unavailable",
            download_path=download_path(challenge.id, index=index, competition_id=competition_id) if stat else None))
    assets = (await db.execute(select(ChallengeAttachment).where(ChallengeAttachment.challenge_id == challenge.id)
                               .order_by(ChallengeAttachment.position, ChallengeAttachment.id))).scalars().all()
    for row in assets:
        valid = row.state == "ready" and row.bound_challenge_id == challenge.id and row.object_path == private_path(row.owner_id, row.draft_id, row.id)
        try:
            stat = await run_in_threadpool(stat_challenge_object, row.object_path) if valid else None
        except Exception:
            raise storage_unavailable()
        ready = stat is not None and stat.size == row.byte_size
        result.append(ChallengeAttachmentResponse(id=row.id, name=row.original_name, byte_size=row.byte_size,
            sha256=row.sha256, state="ready" if ready else "unavailable",
            download_path=download_path(challenge.id, asset_id=row.id, competition_id=competition_id) if ready else None))
    return result


async def resolve_attachment(db, challenge, *, asset_id=None, file_index=None):
    if asset_id is not None:
        row = (await db.execute(select(ChallengeAttachment).where(
            ChallengeAttachment.id == asset_id, ChallengeAttachment.challenge_id == challenge.id,
            ChallengeAttachment.bound_challenge_id == challenge.id, ChallengeAttachment.state == "ready",
        ))).scalar_one_or_none()
        if row is None or row.object_path != private_path(row.owner_id, row.draft_id, row.id):
            raise HTTPException(404, detail="첨부를 찾을 수 없습니다.")
        try:
            if await run_in_threadpool(verify_challenge_object, row.object_path, row.byte_size, row.sha256) != "verified":
                raise HTTPException(404, detail="첨부 실물을 찾을 수 없습니다.")
        except HTTPException:
            raise
        except Exception:
            raise storage_unavailable()
        path, name = row.object_path, row.original_name
    else:
        files = challenge.files or []
        if file_index is None or file_index < 0 or file_index >= len(files):
            raise HTTPException(404, detail="첨부를 찾을 수 없습니다.")
        path = legacy_object_path(files[file_index], challenge.id)
        if path is None:
            raise HTTPException(404, detail="실물이 확인되지 않은 기존 첨부입니다.")
        name = path.rsplit("/", 1)[-1]
    try:
        stat = await run_in_threadpool(stat_challenge_object, path)
        if stat is None:
            raise HTTPException(404, detail="첨부 실물을 찾을 수 없습니다.")
        stream, content_type, size = await run_in_threadpool(get_object_stream, path)
    except HTTPException:
        raise
    except Exception:
        raise storage_unavailable()
    from starlette.background import BackgroundTask
    from starlette.responses import StreamingResponse
    return StreamingResponse(iter_object_stream(stream), media_type=content_type,
        headers={"Content-Length": str(size), "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
                 "X-Content-Type-Options": "nosniff"}, background=BackgroundTask(close_object_stream, stream))


async def prepare_submission_attachments(db, submission, challenge):
    """모든 pending 소유·복사를 검증한 뒤에만 부모와 승인 상태를 commit한다."""
    assets = (await db.execute(select(ChallengeAttachment).where(
        ChallengeAttachment.draft_id == submission.id,
    ).order_by(ChallengeAttachment.position, ChallengeAttachment.id).with_for_update())).scalars().all()
    by_path = {row.object_path: row for row in assets}
    for position, source in enumerate(submission.file_paths or []):
        parts = source.split("/")
        if pending_object_path(source, submission.id) is None:
            raise storage_unavailable()
        row = by_path.get(source)
        if row is None:
            # 과거에 원본명이 소실된 경우 저장된 안전한 이름만 사용한다.
            try:
                size, content_type, fingerprint = await run_in_threadpool(legacy_pending_metadata, source)
            except Exception:
                raise storage_unavailable()
            draft = await db.get(ChallengeAttachmentDraft, submission.id)
            if draft is None:
                draft = ChallengeAttachmentDraft(id=submission.id, owner_id=submission.submitter_id)
                db.add(draft)
                await db.flush()
            row = ChallengeAttachment(id=uuid4(), owner_id=submission.submitter_id, draft_id=draft.id,
                original_name=parts[-1], byte_size=size, sha256=fingerprint, content_type=content_type,
                object_path=source, state="pending")
            db.add(row)
        if row.owner_id != submission.submitter_id or row.byte_size > MAX_FILE_SIZE:
            raise storage_unavailable()
        destination = private_path(row.owner_id, row.draft_id, row.id)
        try:
            await run_in_threadpool(copy_pending_challenge_object, source, destination)
            verified = await run_in_threadpool(verify_challenge_object, destination, row.byte_size, row.sha256)
            if verified != "verified":
                raise storage_unavailable()
        except Exception:
            raise storage_unavailable()
        row.object_path, row.state = destination, "ready"
        row.challenge_id, row.bound_challenge_id, row.position = challenge.id, challenge.id, position
    challenge.files = []
    if submission.file_paths:
        challenge.attachment_revision = 1
        draft = await db.get(ChallengeAttachmentDraft, submission.id)
        draft.challenge_id, draft.state = challenge.id, "bound"


async def record_submission_upload(db, submission, path, name, data, content_type):
    draft = await db.get(ChallengeAttachmentDraft, submission.id)
    if draft is None:
        draft = ChallengeAttachmentDraft(id=submission.id, owner_id=submission.submitter_id)
        db.add(draft)
        await db.flush()
    db.add(ChallengeAttachment(id=uuid4(), owner_id=submission.submitter_id, draft_id=draft.id,
        original_name=name, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        content_type=EXTENSION_CONTENT_TYPE.get(get_file_extension(name), "application/octet-stream"),
        object_path=path, state="pending"))
