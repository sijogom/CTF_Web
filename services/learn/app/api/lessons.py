"""레슨 CRUD + 이미지/첨부파일 API."""

import logging
from datetime import datetime, timezone
from uuid import UUID

import re

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models.course import Course, Lesson, UserProgress
from app.models.lesson_image import LessonImageAsset
from app.models.knowledge import KnowledgeTag, LessonKnowledgeTag
from app.utils.knowledge_tag_audit import (
    add_mapping_audit as _add_mapping_audit,
    manual_mapping_snapshot,
)
from app.utils.knowledge_tag_projection import project_rule_tag_mappings
from app.utils.content_tag_mappings import replace_content_tag_mappings
from app.schemas.knowledge import ContentTagMappingUpdate
from app.schemas.course import (
    AttachmentInfo,
    AttachmentPublic,
    ImageUploadResponse,
    LessonCreate,
    LessonDetail,
    LessonPublic,
    LessonResponse,
    LessonSibling,
    LessonUpdate,
)
from app.api.deps import get_current_user_id, get_optional_user_id, get_optional_user_role, require_role
from app.utils.course_prerequisites import get_course_prerequisite_status
from app.utils.lesson_image_validation import validate_lesson_image_bytes
from app.utils.safe_html import RichContentError, normalize_rich_content_for_write
from app.utils.minio_client import (
    ATTACHMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    LESSON_ASSETS_BUCKET,
    MAX_ATTACHMENT_SIZE,
    MAX_ATTACHMENTS_PER_LESSON,
    MAX_IMAGE_SIZE,
    get_file_extension,
    get_object_stream,
    close_object_stream,
    iter_object_stream,
    delete_lesson_attachment,
    delete_lesson_image,
    delete_lesson_files,
    upload_lesson_attachment,
    upload_lesson_image,
    validate_filename,
)

logger = logging.getLogger(__name__)


async def _require_active_course(db: AsyncSession, course_id: UUID, *, lock_for_write: bool = False) -> Course:
    statement = select(Course).where(Course.id == course_id, Course.is_deleted.is_(False))
    if lock_for_write:
        statement = statement.with_for_update(read=True).execution_options(populate_existing=True, autoflush=False)
    result = await db.execute(statement)
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")
    return course


async def _require_course_learning_access(
    db: AsyncSession,
    *,
    course_id: UUID,
    user_id: UUID,
    user_role: str | None,
) -> Course:
    """레슨·첨부 직접 URL에서도 코스 공개·선행조건을 강제한다."""
    course = await _require_active_course(db, course_id)
    is_admin_or_mod = user_role in ("admin", "moderator")
    if not is_admin_or_mod and (
        not course.is_published or course.status != "approved"
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")
    if not is_admin_or_mod:
        locked, _details = await get_course_prerequisite_status(
            db,
            course_id=course_id,
            user_id=user_id,
        )
        if locked:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="선행 코스를 모두 완료한 뒤 이 레슨에 접근할 수 있습니다.",
            )
    return course


router = APIRouter(tags=["레슨"])


def _build_lesson_detail(lesson: Lesson, completed: bool = False) -> LessonDetail:
    """Lesson ORM → LessonDetail 응답 변환 헬퍼."""
    raw_attachments = lesson.attachments or []
    attachments = [AttachmentInfo(**a) for a in raw_attachments]
    return LessonDetail(
        id=lesson.id,
        title=lesson.title,
        content=lesson.content,
        order_index=lesson.order_index,
        estimated_minutes=lesson.estimated_minutes,
        challenge_id=lesson.challenge_id,
        attachments=attachments,
        completed=completed,
        created_at=lesson.created_at,
    )


# ── 레슨 CRUD ──


@router.get("/courses/{course_id}/lessons/{lesson_id}", response_model=LessonResponse)
async def get_lesson(
    course_id: UUID,
    lesson_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """레슨 상세 조회 — 로그인 필수."""
    await _require_course_learning_access(
        db,
        course_id=course_id,
        user_id=user_id,
        user_role=user_role,
    )
    result = await db.execute(
        select(Lesson)
        .where(Lesson.id == lesson_id, Lesson.course_id == course_id)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="레슨을 찾을 수 없습니다.")

    completed = False
    if user_id:
        prog_result = await db.execute(
            select(UserProgress)
            .where(
                UserProgress.user_id == user_id,
                UserProgress.lesson_id == lesson_id,
                UserProgress.completed.is_(True),
            )
        )
        completed = prog_result.scalar_one_or_none() is not None

    # siblings (이전/다음 레슨) 조회
    all_lessons_result = await db.execute(
        select(Lesson.id, Lesson.title, Lesson.order_index)
        .where(Lesson.course_id == course_id)
        .order_by(Lesson.order_index)
    )
    all_lessons = all_lessons_result.all()

    prev_sibling = None
    next_sibling = None
    for i, row in enumerate(all_lessons):
        if row.id == lesson_id:
            if i > 0:
                prev_sibling = LessonSibling(id=all_lessons[i - 1].id, title=all_lessons[i - 1].title)
            if i < len(all_lessons) - 1:
                next_sibling = LessonSibling(id=all_lessons[i + 1].id, title=all_lessons[i + 1].title)
            break

    # 학습자용 응답 (object_path 제외)
    raw_attachments = lesson.attachments or []
    public_attachments = [AttachmentPublic(**{k: v for k, v in a.items() if k != "object_path"}) for a in raw_attachments]

    lesson_public = LessonPublic(
        id=lesson.id,
        title=lesson.title,
        content=lesson.content,
        order_index=lesson.order_index,
        estimated_minutes=lesson.estimated_minutes,
        challenge_id=lesson.challenge_id,
        attachments=public_attachments,
        completed=completed,
        created_at=lesson.created_at,
    )

    return LessonResponse(
        lesson=lesson_public,
        siblings={"prev": prev_sibling, "next": next_sibling},
    )


@router.post(
    "/courses/{course_id}/lessons",
    response_model=LessonDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_lesson(
    course_id: UUID,
    body: LessonCreate,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """레슨 생성 (admin/moderator 전용)."""
    await _require_active_course(db, course_id, lock_for_write=True)

    lesson = Lesson(
        course_id=course_id,
        title=body.title,
        content=body.content,
        order_index=body.order_index,
        estimated_minutes=body.estimated_minutes,
        challenge_id=body.challenge_id,
    )
    db.add(lesson)
    if body.knowledge_tag_ids:
        tag_result = await db.execute(
            select(KnowledgeTag.id).where(KnowledgeTag.id.in_(body.knowledge_tag_ids))
        )
        existing_tag_ids = set(tag_result.scalars().all())
        if existing_tag_ids != set(body.knowledge_tag_ids):
            missing = sorted(set(body.knowledge_tag_ids) - existing_tag_ids)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"존재하지 않는 지식 태그가 포함되어 있습니다: {missing}",
            )
    await db.flush()
    tag_mapping_now = datetime.now(timezone.utc)
    for tag_id in body.knowledge_tag_ids:
        mapping = LessonKnowledgeTag(
            lesson_id=lesson.id,
            tag_id=tag_id,
            direction="content_to_knowledge_tag",
            strength=1,
            confidence=1,
            evidence_summary="관리자가 생성 시 연결한 지식 태그",
            origin="manual",
            created_by=_user_id,
            approved_at=tag_mapping_now,
            created_at=tag_mapping_now,
        )
        db.add(mapping)
        _add_mapping_audit(
            db,
            entity_type="lesson",
            entity_id=str(lesson.id),
            tag_id=tag_id,
            action="attach",
            actor_id=_user_id,
            snapshot=manual_mapping_snapshot(
                _user_id,
                tag_mapping_now,
                evidence_summary="관리자가 생성 시 연결한 지식 태그",
            ),
        )
    await project_rule_tag_mappings(
        db,
        entity_type="lesson",
        entity=lesson,
        actor_id=_user_id,
    )
    await db.commit()
    await db.refresh(lesson)

    return _build_lesson_detail(lesson)


@router.put("/courses/{course_id}/lessons/{lesson_id}", response_model=LessonDetail)
async def update_lesson(
    course_id: UUID,
    lesson_id: UUID,
    body: LessonUpdate,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """레슨 수정 (admin/moderator 전용)."""
    await _require_active_course(db, course_id, lock_for_write=True)
    result = await db.execute(
        select(Lesson).where(Lesson.id == lesson_id, Lesson.course_id == course_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="레슨을 찾을 수 없습니다.")

    update_data = body.model_dump(exclude_unset=True)
    if "content" not in update_data and lesson.content is not None:
        try:
            update_data["content"] = normalize_rich_content_for_write(
                lesson.content,
                field_name="레슨 본문",
                allow_empty=True,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
    knowledge_tag_ids = update_data.pop("knowledge_tag_ids", None)
    update_data.pop("knowledge_tag_mapping", None)
    mapping_input = body.knowledge_tag_mapping
    if mapping_input is None and knowledge_tag_ids is not None:
        mapping_input = ContentTagMappingUpdate(tag_ids=knowledge_tag_ids)
    for field, value in update_data.items():
        setattr(lesson, field, value)

    if mapping_input is not None:
        await replace_content_tag_mappings(
            db, entity_type="lesson", entity=lesson, body=mapping_input, actor_id=_user_id,
        )
    else:
        await project_rule_tag_mappings(
            db, entity_type="lesson", entity=lesson, actor_id=_user_id,
        )
    await db.commit()
    await db.refresh(lesson)

    return _build_lesson_detail(lesson)


@router.delete(
    "/courses/{course_id}/lessons/{lesson_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_lesson(
    course_id: UUID,
    lesson_id: UUID,
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """레슨 삭제 (admin/moderator 전용) — MinIO 파일도 정리."""
    await _require_active_course(db, course_id, lock_for_write=True)
    result = await db.execute(
        select(Lesson).where(Lesson.id == lesson_id, Lesson.course_id == course_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="레슨을 찾을 수 없습니다.")

    # MinIO 첨부파일 정리
    delete_lesson_files(str(lesson_id))

    await db.delete(lesson)
    await db.commit()


# ── 이미지 업로드 ──


@router.post("/lessons/upload-image", response_model=ImageUploadResponse)
async def upload_image(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """에디터용 이미지 업로드 → presigned URL + 마크다운 텍스트 반환."""
    try:
        if not file.filename:
            raise HTTPException(status_code=400, detail="파일명이 필요합니다.")

        # 파일명 보안 검증
        error = validate_filename(file.filename)
        if error:
            raise HTTPException(status_code=400, detail=error)

        # 확장자 검증
        ext = get_file_extension(file.filename)
        if ext not in IMAGE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"허용되지 않는 이미지 형식입니다. 허용: {', '.join(sorted(IMAGE_EXTENSIONS))}",
            )

        # I-3: 청크 단위 읽기로 메모리 DoS 방지
        chunks: list[bytes] = []
        total = 0
        while chunk := await file.read(8192):
            total += len(chunk)
            if total > MAX_IMAGE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"이미지 크기가 {MAX_IMAGE_SIZE // (1024*1024)}MB를 초과합니다.",
                )
            chunks.append(chunk)
        data = b"".join(chunks)

        content_error = validate_lesson_image_bytes(data=data, extension=ext)
        if content_error:
            raise HTTPException(status_code=400, detail=content_error)

        result = upload_lesson_image(
            data=data,
            filename=file.filename,
        )

        # 업로드 객체와 DB 원장을 함께 기록한다. 본문에 삽입되지 않거나
        # 업로드가 취소된 객체는 lifecycle runner가 grace period 뒤 정리한다.
        object_name = result["object_path"].split("/", 1)[1]
        _images, folder_id, stored_name = object_name.split("/", 2)
        asset = LessonImageAsset(
            object_path=result["object_path"],
            folder_id=folder_id,
            filename=stored_name,
            original_name=file.filename,
            uploaded_by=user_id,
        )
        db.add(asset)
        try:
            await db.flush()
            asset_id = asset.id
            await db.commit()
        except Exception:
            await db.rollback()
            try:
                delete_lesson_image(result["object_path"])
            except Exception:
                logger.error("레슨 이미지 원장 저장 실패 후 객체 정리 실패", exc_info=True)
            raise HTTPException(status_code=503, detail="이미지 업로드를 기록할 수 없습니다.") from None

        return ImageUploadResponse(
            asset_id=asset_id,
            url=result["url"],
            markdown=result["markdown"],
        )
    finally:
        await file.close()


# ── 첨부파일 CRUD ──


@router.post(
    "/courses/{course_id}/lessons/{lesson_id}/attachments",
    response_model=AttachmentInfo,
    status_code=status.HTTP_201_CREATED,
)
async def add_attachment(
    course_id: UUID,
    lesson_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """레슨에 첨부파일 추가."""
    await _require_active_course(db, course_id, lock_for_write=True)
    result = await db.execute(
        select(Lesson).where(Lesson.id == lesson_id, Lesson.course_id == course_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=404, detail="레슨을 찾을 수 없습니다.")

    if not file.filename:
        raise HTTPException(status_code=400, detail="파일명이 필요합니다.")

    # 파일명 보안 검증
    error = validate_filename(file.filename)
    if error:
        raise HTTPException(status_code=400, detail=error)

    # 확장자 검증
    ext = get_file_extension(file.filename)
    if ext not in ATTACHMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"허용되지 않는 파일 형식입니다: {ext}")

    # 첨부파일 개수 제한
    current_attachments = lesson.attachments or []
    if len(current_attachments) >= MAX_ATTACHMENTS_PER_LESSON:
        raise HTTPException(
            status_code=400,
            detail=f"레슨당 첨부파일은 최대 {MAX_ATTACHMENTS_PER_LESSON}개입니다.",
        )

    # I-3: 청크 단위 읽기로 메모리 DoS 방지
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(8192):
        total += len(chunk)
        if total > MAX_ATTACHMENT_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"파일 크기가 {MAX_ATTACHMENT_SIZE // (1024*1024)}MB를 초과합니다.",
            )
        chunks.append(chunk)
    data = b"".join(chunks)

    # MinIO 업로드
    meta = upload_lesson_attachment(
        data=data,
        filename=file.filename,
        lesson_id=str(lesson_id),
    )

    # JSONB 배열에 메타데이터 추가
    updated = list(current_attachments)
    updated.append(meta)
    lesson.attachments = updated

    # SQLAlchemy JSONB 변경 감지를 위해 명시적 flag
    flag_modified(lesson, "attachments")

    await db.commit()
    await db.refresh(lesson)

    return AttachmentInfo(**meta)


@router.delete(
    "/courses/{course_id}/lessons/{lesson_id}/attachments/{filename}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_attachment(
    course_id: UUID,
    lesson_id: UUID,
    filename: str = Path(..., pattern=r"^[a-f0-9]{8}\.[a-z0-9]+$"),
    db: AsyncSession = Depends(get_db),
    _user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """레슨에서 첨부파일 삭제."""
    await _require_active_course(db, course_id, lock_for_write=True)
    result = await db.execute(
        select(Lesson).where(Lesson.id == lesson_id, Lesson.course_id == course_id)
        .with_for_update().execution_options(populate_existing=True, autoflush=False)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=404, detail="레슨을 찾을 수 없습니다.")

    current_attachments = lesson.attachments or []
    target = None
    remaining = []
    for att in current_attachments:
        if att.get("filename") == filename:
            target = att
        else:
            remaining.append(att)

    if not target:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")

    # MinIO에서 파일 삭제
    delete_lesson_attachment(target["object_path"])

    # JSONB 업데이트
    lesson.attachments = remaining
    flag_modified(lesson, "attachments")

    await db.commit()


@router.get("/courses/{course_id}/lessons/{lesson_id}/attachments/{filename}/download")
async def download_attachment(
    course_id: UUID,
    lesson_id: UUID,
    filename: str = Path(..., pattern=r"^[a-f0-9]{8}\.[a-z0-9]+$"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """첨부파일 다운로드 (presigned URL 리다이렉트).

    접근 제어: 코스가 공개(published) + 승인(approved) 상태인지 확인한다.
    비공개/미승인 코스의 첨부파일은 다운로드할 수 없다.
    """
    # 코스 공개·선행조건은 레슨 상세와 동일하게 강제한다.
    await _require_course_learning_access(
        db,
        course_id=course_id,
        user_id=user_id,
        user_role=user_role,
    )

    result = await db.execute(
        select(Lesson).where(Lesson.id == lesson_id, Lesson.course_id == course_id)
    )
    lesson = result.scalar_one_or_none()
    if not lesson:
        raise HTTPException(status_code=404, detail="레슨을 찾을 수 없습니다.")

    current_attachments = lesson.attachments or []
    target = next((a for a in current_attachments if a.get("filename") == filename), None)
    if not target:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")

    try:
        stream, content_type, size = get_object_stream(target["object_path"])
    except Exception:
        logger.error("MinIO 파일 다운로드 실패: %s", target["object_path"], exc_info=True)
        raise HTTPException(status_code=500, detail="파일 다운로드에 실패했습니다.")

    original_name = target.get("original_name", filename)
    encoded_name = quote(original_name)

    return StreamingResponse(
        iter_object_stream(stream),
        media_type=content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Content-Length": str(size),
        },
        background=BackgroundTask(close_object_stream, stream),
    )


# ── I-1: 이미지 프록시 (presigned URL 만료 방지) ──

SAFE_IMAGE_PATH = re.compile(r"^[a-f0-9]{12}/[a-f0-9]{8}\.(png|jpg|jpeg|gif|webp)$")


@router.get("/lesson-images/{folder_id}/{filename}")
async def proxy_lesson_image(
    folder_id: str = Path(..., pattern=r"^[a-f0-9]{12}$"),
    filename: str = Path(..., pattern=r"^[a-f0-9]{8}\.(png|jpg|jpeg|gif|webp)$"),
):
    """마크다운 내 이미지를 백엔드 스트리밍으로 제공 (127.0.0.1 presigned URL 문제 방지)."""
    object_path = f"{LESSON_ASSETS_BUCKET}/images/{folder_id}/{filename}"
    try:
        stream, content_type, size = get_object_stream(object_path)
    except Exception:
        raise HTTPException(status_code=404, detail="이미지를 찾을 수 없습니다.")

    return StreamingResponse(
        iter_object_stream(stream),
        media_type=content_type,
        headers={"Content-Length": str(size), "Cache-Control": "public, max-age=86400"},
        background=BackgroundTask(close_object_stream, stream),
    )
