"""강의·과제 교육 운영 V1 API.

이 라우터는 기존 공개 Course/Lesson, 지식 그래프, 워게임/CTF 점수와 분리된다.
모든 변경 경로는 Auth 서비스의 현재 활성·밴·전역 역할을 재검증한 education
principal을 사용하며, ``professor_admin`` grant는 이 라우터 안에서만 유효하다.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.database import get_db
from app.utils.safe_html import (
    RichContentError,
    normalize_rich_content_for_write,
    rich_content_visible_char_count_for_read,
)
from app.models.challenge import Challenge
from app.models.course import ContentStatus, Course, Lesson
from app.models.education import (
    AssignmentGrade,
    AssignmentStatus,
    AssignmentSubmission,
    EducationAsset,
    EducationAssetCleanupJob,
    EducationAssetScope,
    EducationAuditEvent,
    EducationResourceLink,
    EducationResourceType,
    EducationRole,
    EducationRoleGrant,
    EnrollmentStatus,
    GradeStatus,
    TeachingClass,
    TeachingClassEnrollment,
    TeachingClassStatus,
    EducationAssignment,
)
from app.schemas.education import (
    ArchiveRequest,
    AssignmentGradeResponse,
    AssignmentGradeUpsert,
    AssignmentRestoreRequest,
    AssignmentSubmissionDetail,
    AssignmentSubmissionListResponse,
    AssignmentSubmissionMetadata,
    AssignmentSubmissionUpsert,
    EducationAssetDeleteRequest,
    EducationAssetListResponse,
    EducationAssetResponse,
    EducationAssetUsageResponse,
    EducationAssignmentCreate,
    EducationPermissionsResponse,
    EducationAssignmentDetail,
    EducationAssignmentListResponse,
    EducationAssignmentSummary,
    EducationAssignmentUpdate,
    EducationAuditEventResponse,
    EducationAuditListResponse,
    EducationResourceLinkCreate,
    EducationResourceLinkResponse,
    EducationRoleGrantCreate,
    EducationRoleGrantResponse,
    EducationRoleGrantRevoke,
    EnrollmentCreate,
    EnrollmentDropRequest,
    EnrollmentResponse,
    TeachingClassCreate,
    TeachingClassDetail,
    TeachingClassListResponse,
    TeachingClassRestoreRequest,
    TeachingClassSummary,
    TeachingClassUpdate,
    validate_time_window,
)
from app.utils.auth_client import get_current_authorization
from app.utils.education_authorization import (
    EducationPrincipal,
    get_education_principal,
    require_education_manager,
    require_global_admin,
)
from app.utils.education_visibility import (
    can_student_view_grade,
    is_student_assignment_visible,
    is_student_class_visible,
    is_submission_window_open,
    utc_now,
)
from app.utils.minio_client import (
    EducationAssetMalwareDetectedError,
    EducationAssetScannerUnavailableError,
    MAX_EDUCATION_ASSET_CLASS_TOTAL_SIZE,
    MAX_EDUCATION_ASSET_SIZE,
    MAX_EDUCATION_ASSETS_PER_CLASS,
    close_object_stream,
    delete_education_asset,
    get_object_stream,
    is_education_asset_object_path,
    iter_object_stream,
    scan_education_asset_before_storage,
    upload_education_asset,
    validate_education_asset_filename,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/education", tags=["education"])

_PAGE_SIZE_MAX = 100
_UPLOAD_CHUNK_SIZE = 1024 * 1024


def _add_audit(
    db: AsyncSession,
    *,
    actor_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    class_id: UUID | None,
    details: dict,
) -> None:
    """본문·피드백·object path를 넣지 않는 최소 append-only 감사 이벤트."""
    db.add(
        EducationAuditEvent(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            class_id=class_id,
            details=details,
        )
    )


def _class_summary(klass: TeachingClass) -> TeachingClassSummary:
    return TeachingClassSummary(
        id=klass.id,
        title=klass.title,
        summary=klass.summary,
        instructor_id=klass.instructor_id,
        status=klass.status,
        starts_at=klass.starts_at,
        ends_at=klass.ends_at,
        created_at=klass.created_at,
    )


async def _resolve_public_resource_href(
    db: AsyncSession,
    link: EducationResourceLink,
) -> str | None:
    """Return one safe local route only while the target remains public.

    EducationResourceLink is intentionally a read-only cross-domain
    reference.  Do not let the client infer a lesson parent or construct an
    arbitrary URL from manager-entered data.  A missing or stale target has no
    href; its record can still be audited by an education manager.
    """
    if link.resource_type == EducationResourceType.COURSE.value and link.course_id:
        course_id = (
            await db.execute(
                select(Course.id).where(
                    Course.id == link.course_id,
                    Course.is_published.is_(True),
                    Course.is_deleted.is_(False),
                    Course.status == ContentStatus.APPROVED.value,
                )
            )
        ).scalar_one_or_none()
        return f"/learn/{course_id}" if course_id is not None else None

    if link.resource_type == EducationResourceType.LESSON.value and link.lesson_id:
        course_id = (
            await db.execute(
                select(Lesson.course_id)
                .join(Course, Lesson.course_id == Course.id)
                .where(
                    Lesson.id == link.lesson_id,
                    Course.is_published.is_(True),
                    Course.is_deleted.is_(False),
                    Course.status == ContentStatus.APPROVED.value,
                )
            )
        ).scalar_one_or_none()
        return f"/learn/{course_id}/{link.lesson_id}" if course_id is not None else None

    if link.resource_type == EducationResourceType.WARGAME.value and link.challenge_id:
        challenge_id = (
            await db.execute(
                select(Challenge.id).where(
                    Challenge.id == link.challenge_id,
                    Challenge.challenge_type == "wargame",
                    Challenge.is_published.is_(True),
                )
            )
        ).scalar_one_or_none()
        return f"/wargame/{challenge_id}" if challenge_id is not None else None

    return None


async def _resource_response(
    db: AsyncSession,
    link: EducationResourceLink,
    *,
    href: str | None = None,
) -> EducationResourceLinkResponse:
    return EducationResourceLinkResponse(
        id=link.id,
        resource_type=link.resource_type,
        course_id=link.course_id,
        lesson_id=link.lesson_id,
        challenge_id=link.challenge_id,
        label=link.label,
        href=href if href is not None else await _resolve_public_resource_href(db, link),
        created_at=link.created_at,
    )


def _assignment_summary(assignment: EducationAssignment) -> EducationAssignmentSummary:
    return EducationAssignmentSummary(
        id=assignment.id,
        class_id=assignment.class_id,
        title=assignment.title,
        status=assignment.status,
        max_score=assignment.max_score,
        available_from_at=assignment.available_from_at,
        due_at=assignment.due_at,
        created_at=assignment.created_at,
    )


def _assignment_detail(assignment: EducationAssignment) -> EducationAssignmentDetail:
    return EducationAssignmentDetail(
        **_assignment_summary(assignment).model_dump(),
        instructions_markdown=assignment.instructions_markdown,
        published_at=assignment.published_at,
        archived_at=assignment.archived_at,
        archive_reason=assignment.archive_reason,
    )


def _asset_response(asset: EducationAsset) -> EducationAssetResponse:
    return EducationAssetResponse(
        id=asset.id,
        class_id=asset.class_id,
        assignment_id=asset.assignment_id,
        scope=asset.scope,
        stored_name=asset.stored_name,
        original_name=asset.original_name,
        content_type=asset.content_type,
        byte_size=asset.byte_size,
        uploaded_by=asset.uploaded_by,
        created_at=asset.created_at,
        deleted_at=asset.deleted_at,
    )


def _submission_metadata(submission: AssignmentSubmission, grade: AssignmentGrade | None = None) -> AssignmentSubmissionMetadata:
    return AssignmentSubmissionMetadata(
        id=submission.id,
        assignment_id=submission.assignment_id,
        student_id=submission.student_id,
        submitted_at=submission.submitted_at,
        updated_at=submission.updated_at,
        content_length=rich_content_visible_char_count_for_read(submission.content_markdown),
        grade_status=grade.status if grade else None,
    )


def _submission_detail(submission: AssignmentSubmission, grade: AssignmentGrade | None = None) -> AssignmentSubmissionDetail:
    return AssignmentSubmissionDetail(
        **_submission_metadata(submission, grade).model_dump(),
        content_markdown=submission.content_markdown,
    )


def _grade_response(grade: AssignmentGrade, *, include_feedback: bool) -> AssignmentGradeResponse:
    return AssignmentGradeResponse(
        id=grade.id,
        submission_id=grade.submission_id,
        score=grade.score,
        status=grade.status,
        graded_by=grade.graded_by,
        graded_at=grade.graded_at,
        published_at=grade.published_at,
        feedback_markdown=grade.feedback_markdown if include_feedback else None,
    )


async def _class_or_404(
    db: AsyncSession,
    class_id: UUID,
    *,
    lock: bool = False,
) -> TeachingClass:
    statement = select(TeachingClass).where(TeachingClass.id == class_id)
    if lock:
        statement = statement.with_for_update()
    klass = (await db.execute(statement)).scalar_one_or_none()
    if klass is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="강의를 찾을 수 없습니다.")
    return klass


async def _assignment_or_404(
    db: AsyncSession,
    assignment_id: UUID,
    *,
    lock: bool = False,
) -> EducationAssignment:
    statement = select(EducationAssignment).where(EducationAssignment.id == assignment_id)
    if lock:
        statement = statement.with_for_update()
    assignment = (await db.execute(statement)).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")
    return assignment


async def _active_enrollment(
    db: AsyncSession,
    *,
    class_id: UUID,
    student_id: UUID,
    lock: bool = False,
) -> TeachingClassEnrollment | None:
    statement = select(TeachingClassEnrollment).where(
        TeachingClassEnrollment.class_id == class_id,
        TeachingClassEnrollment.student_id == student_id,
        TeachingClassEnrollment.status == EnrollmentStatus.ACTIVE.value,
    )
    if lock:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def _enrollment_status(
    db: AsyncSession,
    *,
    class_id: UUID,
    student_id: UUID,
) -> str | None:
    enrollment = await _active_enrollment(db, class_id=class_id, student_id=student_id)
    return enrollment.status if enrollment else None


async def _require_student_class_visibility(
    db: AsyncSession,
    *,
    klass: TeachingClass,
    principal: EducationPrincipal,
) -> str | None:
    """교육 관리자는 전부, 수강생은 활성 수강·공개 상태만 보게 한다."""
    if principal.is_education_manager:
        return None
    enrollment_status = await _enrollment_status(
        db,
        class_id=klass.id,
        student_id=principal.user_id,
    )
    if not is_student_class_visible(
        class_status=klass.status,
        enrollment_status=enrollment_status,
    ):
        # 수강하지 않은 초안 강의의 존재·제목을 노출하지 않는다.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="강의를 찾을 수 없습니다.")
    return enrollment_status


async def _resource_is_public(db: AsyncSession, link: EducationResourceLink) -> bool:
    """자료 링크 대상이 현재도 공개 콘텐츠인지 서버에서 다시 확인한다."""
    return await _resolve_public_resource_href(db, link) is not None


async def _class_resources(
    db: AsyncSession,
    *,
    class_id: UUID,
    include_stale: bool,
) -> list[EducationResourceLinkResponse]:
    links = (
        await db.execute(
            select(EducationResourceLink)
            .where(EducationResourceLink.class_id == class_id)
            .order_by(EducationResourceLink.created_at.asc())
        )
    ).scalars().all()
    visible: list[EducationResourceLinkResponse] = []
    for link in links:
        href = await _resolve_public_resource_href(db, link)
        if include_stale or href is not None:
            visible.append(await _resource_response(db, link, href=href))
    return visible


async def _validate_resource_target(db: AsyncSession, body: EducationResourceLinkCreate) -> None:
    """대회/비공개 콘텐츠는 링크를 생성할 수 없다."""
    if body.resource_type == EducationResourceType.COURSE.value:
        probe = EducationResourceLink(resource_type=body.resource_type, course_id=body.course_id)
    elif body.resource_type == EducationResourceType.LESSON.value:
        probe = EducationResourceLink(resource_type=body.resource_type, lesson_id=body.lesson_id)
    else:
        probe = EducationResourceLink(resource_type=body.resource_type, challenge_id=body.challenge_id)
    if not await _resource_is_public(db, probe):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="공개된 코스·레슨·워게임 문제만 강의 자료로 연결할 수 있습니다.",
        )


def _validate_class_update_schedule(klass: TeachingClass, updates: dict) -> None:
    validate_time_window(
        starts_at=updates.get("starts_at", klass.starts_at),
        ends_at=updates.get("ends_at", klass.ends_at),
        starts_field="starts_at",
        ends_field="ends_at",
    )


def _validate_assignment_update_window(assignment: EducationAssignment, updates: dict) -> None:
    validate_time_window(
        starts_at=updates.get("available_from_at", assignment.available_from_at),
        ends_at=updates.get("due_at", assignment.due_at),
        starts_field="available_from_at",
        ends_field="due_at",
    )


async def _validate_education_upload(file: UploadFile) -> int:
    # Multipart parsing has already spooled this part.  Validate and rewind the
    # existing stream instead of copying a second 250 MiB temporary file.
    """Validate the education asset and close it on every rejected path."""
    try:
        if not file.filename:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="파일명이 필요합니다.")
        filename_error = validate_education_asset_filename(file.filename)
        if filename_error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=filename_error)

        total = 0
        while chunk := await file.read(_UPLOAD_CHUNK_SIZE):
            total += len(chunk)
            if total > MAX_EDUCATION_ASSET_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="교육 자료가 플랫폼 파일 안전 상한을 초과했습니다.",
                )
        if total <= 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="빈 파일은 업로드할 수 없습니다.")
        await file.seek(0)
        return total
    except Exception:
        await file.close()
        raise


async def _assert_class_asset_quota(db: AsyncSession, *, class_id: UUID, new_size: int) -> None:
    """class row lock을 보유한 상태에서 active asset quota를 판정한다."""
    asset_count, asset_total = (
        await db.execute(
            select(
                func.count(EducationAsset.id),
                func.coalesce(func.sum(EducationAsset.byte_size), 0),
            ).where(
                EducationAsset.class_id == class_id,
                EducationAsset.deleted_at.is_(None),
            )
        )
    ).one()
    if int(asset_count) >= MAX_EDUCATION_ASSETS_PER_CLASS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="강의 자료 개수가 플랫폼 안전 상한에 도달했습니다.",
        )
    if int(asset_total) + new_size > MAX_EDUCATION_ASSET_CLASS_TOTAL_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="강의 자료 총용량이 플랫폼 안전 상한을 초과합니다.",
        )


async def _record_education_asset_scan_rejection(
    *,
    db: AsyncSession,
    actor_id: UUID,
    asset_id: UUID,
    class_id: UUID,
    assignment_id: UUID | None,
    scope: str,
    byte_size: int,
    scan_state: str,
) -> bool:
    """Persist a minimal audit event without retaining scanner or file content."""
    _add_audit(
        db,
        actor_id=actor_id,
        action="education.asset.scan_rejected",
        entity_type="asset",
        entity_id=asset_id,
        class_id=class_id,
        details={
            "scope": scope,
            "assignment_id": str(assignment_id) if assignment_id else None,
            "byte_size": byte_size,
            "scan_state": scan_state,
        },
    )
    try:
        await db.commit()
        return True
    except SQLAlchemyError:
        await db.rollback()
        logger.error("Education asset scan rejection audit write failed", exc_info=True)
        return False


async def _create_education_asset(
    *,
    db: AsyncSession,
    klass: TeachingClass,
    assignment: EducationAssignment | None,
    file: UploadFile,
    actor_id: UUID,
) -> EducationAsset:
    """DB 메타데이터와 private object를 보상 처리로 함께 만든다."""
    byte_size = await _validate_education_upload(file)
    asset_id = uuid4()
    scope = EducationAssetScope.ASSIGNMENT.value if assignment else EducationAssetScope.CLASS.value
    class_id = klass.id
    assignment_id = assignment.id if assignment else None
    object_path: str | None = None
    object_uploaded = False
    committed = False
    scan_state: str | None = None
    try:
        scan_state = await asyncio.to_thread(
            scan_education_asset_before_storage,
            data_stream=file.file,
            byte_size=byte_size,
            original_filename=file.filename or "",
        )
        await _assert_class_asset_quota(db, class_id=klass.id, new_size=byte_size)
        metadata = upload_education_asset(
            data_stream=file.file,
            byte_size=byte_size,
            original_filename=file.filename or "",
            class_id=str(klass.id),
            asset_id=str(asset_id),
            scope=scope,
            assignment_id=str(assignment.id) if assignment else None,
        )
        object_path = str(metadata["object_path"])
        object_uploaded = True
        asset = EducationAsset(
            id=asset_id,
            class_id=klass.id,
            assignment_id=assignment.id if assignment else None,
            scope=scope,
            object_path=object_path,
            stored_name=str(metadata["stored_name"]),
            original_name=str(metadata["original_name"]),
            content_type=str(metadata["content_type"]),
            byte_size=int(metadata["byte_size"]),
            uploaded_by=actor_id,
        )
        db.add(asset)
        _add_audit(
            db,
            actor_id=actor_id,
            action="education.asset.create",
            entity_type="asset",
            entity_id=asset.id,
            class_id=klass.id,
            details={
                "scope": scope,
                "assignment_id": str(assignment.id) if assignment else None,
                "byte_size": byte_size,
                "content_type": asset.content_type,
                "scan_state": scan_state,
            },
        )
        await db.commit()
        committed = True
        await db.refresh(asset)
        return asset
    except EducationAssetMalwareDetectedError:
        await db.rollback()
        audit_recorded = await _record_education_asset_scan_rejection(
            db=db,
            actor_id=actor_id,
            asset_id=asset_id,
            class_id=class_id,
            assignment_id=assignment_id,
            scope=scope,
            byte_size=byte_size,
            scan_state="malware_detected",
        )
        logger.warning("Education asset upload rejected by malware scanner")
        if not audit_recorded:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="교육 자료 검증 기록을 저장할 수 없어 업로드를 완료하지 않았습니다.",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="악성 파일 또는 검사 제한 초과가 감지되어 자료를 저장하지 않았습니다.",
        ) from None
    except EducationAssetScannerUnavailableError:
        await db.rollback()
        await _record_education_asset_scan_rejection(
            db=db,
            actor_id=actor_id,
            asset_id=asset_id,
            class_id=class_id,
            assignment_id=assignment_id,
            scope=scope,
            byte_size=byte_size,
            scan_state="scanner_unavailable",
        )
        logger.warning("Education asset upload blocked because scanner readiness is unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="교육 자료 악성 파일 검사 준비 상태를 확인할 수 없습니다.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="교육 자료를 저장할 수 없습니다.",
        ) from exc
    finally:
        await file.close()
        # DB commit이 실패하면 private object만 남지 않도록 보상 삭제한다. 성공 뒤에는
        # object path가 asset 행에 있어 이 finally에서 삭제하지 않는다.
        if object_uploaded and not committed and object_path is not None:
            try:
                delete_education_asset(object_path)
            except Exception:
                logger.error("교육 자료 업로드 보상 삭제 실패: asset_id=%s", asset_id, exc_info=True)


# ── 교육 역할 grant (global admin만) ──


@router.get("/me/permissions", response_model=EducationPermissionsResponse)
async def get_my_education_permissions(
    principal: EducationPrincipal = Depends(get_education_principal),
):
    """현재 로그인한 사용자의 교육 영역 권한만 반환한다."""
    return EducationPermissionsResponse(
        user_id=principal.user_id,
        is_education_manager=principal.is_education_manager,
        is_platform_admin=principal.is_global_admin,
        education_role=(EducationRole.PROFESSOR_ADMIN.value if principal.has_professor_admin_grant else None),
    )


@router.get("/role-grants", response_model=list[EducationRoleGrantResponse])
async def list_role_grants(
    include_revoked: bool = False,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_global_admin(principal)
    statement = select(EducationRoleGrant).order_by(EducationRoleGrant.granted_at.desc())
    if not include_revoked:
        statement = statement.where(EducationRoleGrant.revoked_at.is_(None))
    grants = (await db.execute(statement)).scalars().all()
    return [
        EducationRoleGrantResponse(
            id=grant.id,
            subject_id=grant.subject_id,
            role=grant.role,
            granted_by=grant.granted_by,
            grant_reason=grant.grant_reason,
            granted_at=grant.granted_at,
            revoked_by=grant.revoked_by,
            revoke_reason=grant.revoke_reason,
            revoked_at=grant.revoked_at,
            active=grant.revoked_at is None,
        )
        for grant in grants
    ]


@router.get("/role-grants/audit-events", response_model=EducationAuditListResponse)
async def list_role_grant_audit_events(
    subject_id: UUID | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    """Return minimal grant/revoke audit metadata to a global admin only.

    Reasons stay in the separately protected role-grant record; audit details
    intentionally contain only identifiers, the role, and a boolean that a
    reason was recorded.  This endpoint never resolves account profiles,
    email addresses, or names.
    """
    require_global_admin(principal)
    conditions = [EducationAuditEvent.entity_type == "role_grant"]
    statement = select(EducationAuditEvent)
    count_statement = select(func.count()).select_from(EducationAuditEvent)
    if subject_id is not None:
        statement = statement.join(
            EducationRoleGrant,
            EducationAuditEvent.entity_id == EducationRoleGrant.id,
        )
        count_statement = count_statement.join(
            EducationRoleGrant,
            EducationAuditEvent.entity_id == EducationRoleGrant.id,
        )
        conditions.append(EducationRoleGrant.subject_id == subject_id)

    total = int((await db.execute(count_statement.where(*conditions))).scalar_one())
    events = (
        await db.execute(
            statement.where(*conditions)
            .order_by(EducationAuditEvent.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return EducationAuditListResponse(
        items=[
            EducationAuditEventResponse(
                id=event.id,
                actor_id=event.actor_id,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                class_id=None,
                details=event.details,
                created_at=event.created_at,
            )
            for event in events
        ],
        total=total,
        page=page,
        size=size,
    )


@router.post("/role-grants", response_model=EducationRoleGrantResponse, status_code=status.HTTP_201_CREATED)
async def grant_professor_admin(
    body: EducationRoleGrantCreate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_global_admin(principal)
    is_active, _target_role = await get_current_authorization(str(body.subject_id))
    if not is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="활성 사용자에게만 교육 권한을 부여할 수 있습니다.")

    existing = (
        await db.execute(
            select(EducationRoleGrant)
            .where(
                EducationRoleGrant.subject_id == body.subject_id,
                EducationRoleGrant.role == EducationRole.PROFESSOR_ADMIN.value,
                EducationRoleGrant.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 활성화된 교육 관리자 권한입니다.")

    grant = EducationRoleGrant(
        subject_id=body.subject_id,
        role=EducationRole.PROFESSOR_ADMIN.value,
        granted_by=principal.user_id,
        grant_reason=body.reason,
    )
    db.add(grant)
    await db.flush()
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.role_grant.create",
        entity_type="role_grant",
        entity_id=grant.id,
        class_id=None,
        details={"subject_id": str(body.subject_id), "role": grant.role, "reason_recorded": True},
    )
    await db.commit()
    await db.refresh(grant)
    return EducationRoleGrantResponse(
        id=grant.id,
        subject_id=grant.subject_id,
        role=grant.role,
        granted_by=grant.granted_by,
        grant_reason=grant.grant_reason,
        granted_at=grant.granted_at,
        revoked_by=None,
        revoke_reason=None,
        revoked_at=None,
        active=True,
    )


@router.post("/role-grants/{subject_id}/revoke", response_model=EducationRoleGrantResponse)
async def revoke_professor_admin(
    subject_id: UUID,
    body: EducationRoleGrantRevoke,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_global_admin(principal)
    grant = (
        await db.execute(
            select(EducationRoleGrant)
            .where(
                EducationRoleGrant.subject_id == subject_id,
                EducationRoleGrant.role == EducationRole.PROFESSOR_ADMIN.value,
                EducationRoleGrant.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not grant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="활성 교육 관리자 권한을 찾을 수 없습니다.")

    now = utc_now()
    grant.revoked_by = principal.user_id
    grant.revoke_reason = body.reason
    grant.revoked_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.role_grant.revoke",
        entity_type="role_grant",
        entity_id=grant.id,
        class_id=None,
        details={"subject_id": str(subject_id), "role": grant.role, "reason_recorded": True},
    )
    await db.commit()
    return EducationRoleGrantResponse(
        id=grant.id,
        subject_id=grant.subject_id,
        role=grant.role,
        granted_by=grant.granted_by,
        grant_reason=grant.grant_reason,
        granted_at=grant.granted_at,
        revoked_by=grant.revoked_by,
        revoke_reason=grant.revoke_reason,
        revoked_at=grant.revoked_at,
        active=False,
    )


# ── 강의와 수강 등록 ──


@router.get("/classes", response_model=TeachingClassListResponse)
async def list_classes(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    """교육 관리자는 전체, 수강생은 본인의 활성 수강 강의만 조회한다."""
    if principal.is_education_manager:
        conditions: list = []
        statement = select(TeachingClass)
        count_statement = select(func.count()).select_from(TeachingClass)
    else:
        conditions = [
            TeachingClassEnrollment.student_id == principal.user_id,
            TeachingClassEnrollment.status == EnrollmentStatus.ACTIVE.value,
            TeachingClass.status.in_([
                TeachingClassStatus.PUBLISHED.value,
                TeachingClassStatus.ARCHIVED.value,
            ]),
        ]
        statement = select(TeachingClass).join(
            TeachingClassEnrollment,
            TeachingClassEnrollment.class_id == TeachingClass.id,
        )
        count_statement = select(func.count()).select_from(TeachingClass).join(
            TeachingClassEnrollment,
            TeachingClassEnrollment.class_id == TeachingClass.id,
        )

    total = int((await db.execute(count_statement.where(*conditions))).scalar_one())
    classes = (
        await db.execute(
            statement.where(*conditions)
            .order_by(TeachingClass.starts_at.desc().nullslast(), TeachingClass.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return TeachingClassListResponse(
        items=[_class_summary(klass) for klass in classes],
        total=total,
        page=page,
        size=size,
    )


@router.post("/classes", response_model=TeachingClassDetail, status_code=status.HTTP_201_CREATED)
async def create_class(
    body: TeachingClassCreate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    now = utc_now()
    klass = TeachingClass(
        **body.model_dump(),
        instructor_id=principal.user_id,
        created_by=principal.user_id,
        updated_by=principal.user_id,
        published_at=now if body.status == TeachingClassStatus.PUBLISHED.value else None,
    )
    db.add(klass)
    await db.flush()
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.class.create",
        entity_type="class",
        entity_id=klass.id,
        class_id=klass.id,
        details={"status": klass.status, "has_schedule": bool(klass.starts_at or klass.ends_at)},
    )
    await db.commit()
    await db.refresh(klass)
    return TeachingClassDetail(
        **_class_summary(klass).model_dump(),
        description_markdown=klass.description_markdown,
        published_at=klass.published_at,
        archived_at=None,
        archive_reason=None,
        resources=[],
        is_enrolled=False,
        is_education_manager=True,
    )


@router.get("/classes/{class_id}", response_model=TeachingClassDetail)
async def get_class(
    class_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    klass = await _class_or_404(db, class_id)
    enrollment_status = await _require_student_class_visibility(db, klass=klass, principal=principal)
    is_manager = principal.is_education_manager
    return TeachingClassDetail(
        **_class_summary(klass).model_dump(),
        description_markdown=klass.description_markdown,
        published_at=klass.published_at,
        archived_at=klass.archived_at,
        archive_reason=klass.archive_reason if is_manager else None,
        resources=await _class_resources(db, class_id=klass.id, include_stale=is_manager),
        is_enrolled=enrollment_status == EnrollmentStatus.ACTIVE.value,
        is_education_manager=is_manager,
    )


@router.patch("/classes/{class_id}", response_model=TeachingClassDetail)
async def update_class(
    class_id: UUID,
    body: TeachingClassUpdate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의는 복구한 뒤 수정할 수 있습니다.")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="수정할 값이 없습니다.")
    if "description_markdown" not in updates:
        try:
            updates["description_markdown"] = normalize_rich_content_for_write(
                klass.description_markdown,
                field_name="강의 설명",
                max_visible_chars=20000,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
    try:
        _validate_class_update_schedule(klass, updates)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    prior_status = klass.status
    for field_name, value in updates.items():
        setattr(klass, field_name, value)
    now = utc_now()
    if klass.status == TeachingClassStatus.PUBLISHED.value and prior_status != TeachingClassStatus.PUBLISHED.value:
        klass.published_at = klass.published_at or now
    klass.updated_by = principal.user_id
    klass.updated_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.class.update",
        entity_type="class",
        entity_id=klass.id,
        class_id=klass.id,
        details={"changed_fields": sorted(updates), "status": klass.status},
    )
    await db.commit()
    await db.refresh(klass)
    return TeachingClassDetail(
        **_class_summary(klass).model_dump(),
        description_markdown=klass.description_markdown,
        published_at=klass.published_at,
        archived_at=klass.archived_at,
        archive_reason=klass.archive_reason,
        resources=await _class_resources(db, class_id=klass.id, include_stale=True),
        is_enrolled=False,
        is_education_manager=True,
    )


@router.post("/classes/{class_id}/archive", response_model=TeachingClassDetail)
async def archive_class(
    class_id: UUID,
    body: ArchiveRequest,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 보관된 강의입니다.")
    now = utc_now()
    previous_status = klass.status
    klass.status = TeachingClassStatus.ARCHIVED.value
    klass.archived_at = now
    klass.archived_by = principal.user_id
    klass.archive_reason = body.reason
    klass.updated_by = principal.user_id
    klass.updated_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.class.archive",
        entity_type="class",
        entity_id=klass.id,
        class_id=klass.id,
        details={"previous_status": previous_status, "reason_recorded": True},
    )
    await db.commit()
    return TeachingClassDetail(
        **_class_summary(klass).model_dump(),
        description_markdown=klass.description_markdown,
        published_at=klass.published_at,
        archived_at=klass.archived_at,
        archive_reason=klass.archive_reason,
        resources=await _class_resources(db, class_id=klass.id, include_stale=True),
        is_enrolled=False,
        is_education_manager=True,
    )


@router.post("/classes/{class_id}/restore", response_model=TeachingClassDetail)
async def restore_class(
    class_id: UUID,
    body: TeachingClassRestoreRequest,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status != TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의만 복구할 수 있습니다.")
    now = utc_now()
    klass.status = body.status
    if body.status == TeachingClassStatus.PUBLISHED.value:
        klass.published_at = klass.published_at or now
    klass.archived_at = None
    klass.archived_by = None
    klass.archive_reason = None
    klass.updated_by = principal.user_id
    klass.updated_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.class.restore",
        entity_type="class",
        entity_id=klass.id,
        class_id=klass.id,
        details={"restored_status": body.status},
    )
    await db.commit()
    return TeachingClassDetail(
        **_class_summary(klass).model_dump(),
        description_markdown=klass.description_markdown,
        published_at=klass.published_at,
        archived_at=None,
        archive_reason=None,
        resources=await _class_resources(db, class_id=klass.id, include_stale=True),
        is_enrolled=False,
        is_education_manager=True,
    )


@router.get("/classes/{class_id}/enrollments", response_model=list[EnrollmentResponse])
async def list_enrollments(
    class_id: UUID,
    include_dropped: bool = False,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    await _class_or_404(db, class_id)
    statement = select(TeachingClassEnrollment).where(TeachingClassEnrollment.class_id == class_id)
    if not include_dropped:
        statement = statement.where(TeachingClassEnrollment.status == EnrollmentStatus.ACTIVE.value)
    enrollments = (await db.execute(statement.order_by(TeachingClassEnrollment.enrolled_at.asc()))).scalars().all()
    return [
        EnrollmentResponse(
            id=row.id,
            class_id=row.class_id,
            student_id=row.student_id,
            status=row.status,
            enrolled_at=row.enrolled_at,
            dropped_at=row.dropped_at,
        )
        for row in enrollments
    ]


@router.post("/classes/{class_id}/enrollments", response_model=EnrollmentResponse, status_code=status.HTTP_201_CREATED)
async def enroll_student(
    class_id: UUID,
    body: EnrollmentCreate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의에는 수강생을 등록할 수 없습니다.")
    is_active, _role = await get_current_authorization(str(body.student_id))
    if not is_active:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="활성 사용자만 수강생으로 등록할 수 있습니다.")
    enrollment = (
        await db.execute(
            select(TeachingClassEnrollment)
            .where(
                TeachingClassEnrollment.class_id == class_id,
                TeachingClassEnrollment.student_id == body.student_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = utc_now()
    action = "education.enrollment.create"
    if enrollment is None:
        enrollment = TeachingClassEnrollment(
            class_id=class_id,
            student_id=body.student_id,
            status=EnrollmentStatus.ACTIVE.value,
            enrolled_by=principal.user_id,
            enrolled_at=now,
        )
        db.add(enrollment)
    elif enrollment.status == EnrollmentStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 등록된 수강생입니다.")
    else:
        enrollment.status = EnrollmentStatus.ACTIVE.value
        enrollment.enrolled_by = principal.user_id
        enrollment.enrolled_at = now
        enrollment.dropped_by = None
        enrollment.dropped_at = None
        action = "education.enrollment.restore"
    await db.flush()
    _add_audit(
        db,
        actor_id=principal.user_id,
        action=action,
        entity_type="enrollment",
        entity_id=enrollment.id,
        class_id=class_id,
        details={"student_id": str(body.student_id), "status": enrollment.status},
    )
    await db.commit()
    return EnrollmentResponse(
        id=enrollment.id,
        class_id=enrollment.class_id,
        student_id=enrollment.student_id,
        status=enrollment.status,
        enrolled_at=enrollment.enrolled_at,
        dropped_at=enrollment.dropped_at,
    )


@router.post("/classes/{class_id}/enrollments/{student_id}/drop", response_model=EnrollmentResponse)
async def drop_student(
    class_id: UUID,
    student_id: UUID,
    body: EnrollmentDropRequest,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    await _class_or_404(db, class_id)
    enrollment = (
        await db.execute(
            select(TeachingClassEnrollment)
            .where(
                TeachingClassEnrollment.class_id == class_id,
                TeachingClassEnrollment.student_id == student_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if enrollment is None or enrollment.status != EnrollmentStatus.ACTIVE.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="활성 수강 등록을 찾을 수 없습니다.")
    now = utc_now()
    enrollment.status = EnrollmentStatus.DROPPED.value
    enrollment.dropped_by = principal.user_id
    enrollment.dropped_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.enrollment.drop",
        entity_type="enrollment",
        entity_id=enrollment.id,
        class_id=class_id,
        details={"student_id": str(student_id), "reason_recorded": True},
    )
    await db.commit()
    return EnrollmentResponse(
        id=enrollment.id,
        class_id=enrollment.class_id,
        student_id=enrollment.student_id,
        status=enrollment.status,
        enrolled_at=enrollment.enrolled_at,
        dropped_at=enrollment.dropped_at,
    )


# ── 공개 학습 콘텐츠 링크 ──


@router.post(
    "/classes/{class_id}/resources",
    response_model=EducationResourceLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_class_resource(
    class_id: UUID,
    body: EducationResourceLinkCreate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의에는 자료 링크를 추가할 수 없습니다.")
    await _validate_resource_target(db, body)
    link = EducationResourceLink(
        class_id=class_id,
        resource_type=body.resource_type,
        course_id=body.course_id,
        lesson_id=body.lesson_id,
        challenge_id=body.challenge_id,
        label=body.label,
        created_by=principal.user_id,
    )
    db.add(link)
    try:
        await db.flush()
    except SQLAlchemyError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="같은 자료가 이미 연결되어 있습니다.") from exc
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.resource_link.create",
        entity_type="resource_link",
        entity_id=link.id,
        class_id=class_id,
        details={"resource_type": link.resource_type, "target_recorded": True},
    )
    await db.commit()
    await db.refresh(link)
    return await _resource_response(db, link)


@router.delete("/classes/{class_id}/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_class_resource(
    class_id: UUID,
    resource_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    await _class_or_404(db, class_id)
    link = (
        await db.execute(
            select(EducationResourceLink)
            .where(EducationResourceLink.id == resource_id, EducationResourceLink.class_id == class_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="자료 링크를 찾을 수 없습니다.")
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.resource_link.delete",
        entity_type="resource_link",
        entity_id=link.id,
        class_id=class_id,
        details={"resource_type": link.resource_type, "target_recorded": True},
    )
    await db.delete(link)
    await db.commit()


# ── 과제·제출·성적 ──


@router.get("/classes/{class_id}/assignments", response_model=EducationAssignmentListResponse)
async def list_assignments(
    class_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    klass = await _class_or_404(db, class_id)
    enrollment_status = await _require_student_class_visibility(db, klass=klass, principal=principal)
    conditions: list = [EducationAssignment.class_id == class_id]
    if not principal.is_education_manager:
        if not is_student_class_visible(class_status=klass.status, enrollment_status=enrollment_status):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="강의를 찾을 수 없습니다.")
        conditions.append(
            EducationAssignment.status.in_([
                AssignmentStatus.PUBLISHED.value,
                AssignmentStatus.ARCHIVED.value,
            ])
        )
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(EducationAssignment).where(*conditions)
            )
        ).scalar_one()
    )
    assignments = (
        await db.execute(
            select(EducationAssignment)
            .where(*conditions)
            .order_by(EducationAssignment.due_at.asc().nullslast(), EducationAssignment.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return EducationAssignmentListResponse(
        items=[_assignment_summary(item) for item in assignments],
        total=total,
        page=page,
        size=size,
    )


@router.post(
    "/classes/{class_id}/assignments",
    response_model=EducationAssignmentDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_assignment(
    class_id: UUID,
    body: EducationAssignmentCreate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의에는 과제를 만들 수 없습니다.")
    now = utc_now()
    assignment = EducationAssignment(
        **body.model_dump(),
        class_id=class_id,
        created_by=principal.user_id,
        updated_by=principal.user_id,
        published_at=now if body.status == AssignmentStatus.PUBLISHED.value else None,
    )
    db.add(assignment)
    await db.flush()
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.assignment.create",
        entity_type="assignment",
        entity_id=assignment.id,
        class_id=class_id,
        details={
            "status": assignment.status,
            "max_score": assignment.max_score,
            "has_window": bool(assignment.available_from_at or assignment.due_at),
        },
    )
    await db.commit()
    await db.refresh(assignment)
    return _assignment_detail(assignment)


@router.get("/assignments/{assignment_id}", response_model=EducationAssignmentDetail)
async def get_assignment(
    assignment_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    assignment = await _assignment_or_404(db, assignment_id)
    klass = await _class_or_404(db, assignment.class_id)
    enrollment_status = await _require_student_class_visibility(db, klass=klass, principal=principal)
    if not principal.is_education_manager and not is_student_assignment_visible(
        class_status=klass.status,
        enrollment_status=enrollment_status,
        assignment_status=assignment.status,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")
    detail = _assignment_detail(assignment)
    if not principal.is_education_manager:
        # 보관 사유는 수강생 공개 계약이 아니다.
        detail.archive_reason = None
    return detail


@router.patch("/assignments/{assignment_id}", response_model=EducationAssignmentDetail)
async def update_assignment(
    assignment_id: UUID,
    body: EducationAssignmentUpdate,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    assignment = await _assignment_or_404(db, assignment_id, lock=True)
    if assignment.status == AssignmentStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 과제는 복구한 뒤 수정할 수 있습니다.")
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="수정할 값이 없습니다.")
    if "instructions_markdown" not in updates:
        try:
            updates["instructions_markdown"] = normalize_rich_content_for_write(
                assignment.instructions_markdown,
                field_name="과제 안내",
                max_visible_chars=20000,
            )
        except RichContentError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=error.as_detail(),
            ) from error
    try:
        _validate_assignment_update_window(assignment, updates)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    prior_status = assignment.status
    for field_name, value in updates.items():
        setattr(assignment, field_name, value)
    now = utc_now()
    if assignment.status == AssignmentStatus.PUBLISHED.value and prior_status != AssignmentStatus.PUBLISHED.value:
        assignment.published_at = assignment.published_at or now
    assignment.updated_by = principal.user_id
    assignment.updated_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.assignment.update",
        entity_type="assignment",
        entity_id=assignment.id,
        class_id=assignment.class_id,
        details={"changed_fields": sorted(updates), "status": assignment.status},
    )
    await db.commit()
    await db.refresh(assignment)
    return _assignment_detail(assignment)


@router.post("/assignments/{assignment_id}/archive", response_model=EducationAssignmentDetail)
async def archive_assignment(
    assignment_id: UUID,
    body: ArchiveRequest,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    assignment = await _assignment_or_404(db, assignment_id, lock=True)
    if assignment.status == AssignmentStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 보관된 과제입니다.")
    now = utc_now()
    previous_status = assignment.status
    assignment.status = AssignmentStatus.ARCHIVED.value
    assignment.archived_at = now
    assignment.archived_by = principal.user_id
    assignment.archive_reason = body.reason
    assignment.updated_by = principal.user_id
    assignment.updated_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.assignment.archive",
        entity_type="assignment",
        entity_id=assignment.id,
        class_id=assignment.class_id,
        details={"previous_status": previous_status, "reason_recorded": True},
    )
    await db.commit()
    return _assignment_detail(assignment)


@router.post("/assignments/{assignment_id}/restore", response_model=EducationAssignmentDetail)
async def restore_assignment(
    assignment_id: UUID,
    body: AssignmentRestoreRequest,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    assignment = await _assignment_or_404(db, assignment_id, lock=True)
    if assignment.status != AssignmentStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 과제만 복구할 수 있습니다.")
    now = utc_now()
    assignment.status = body.status
    if body.status == AssignmentStatus.PUBLISHED.value:
        assignment.published_at = assignment.published_at or now
    assignment.archived_at = None
    assignment.archived_by = None
    assignment.archive_reason = None
    assignment.updated_by = principal.user_id
    assignment.updated_at = now
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.assignment.restore",
        entity_type="assignment",
        entity_id=assignment.id,
        class_id=assignment.class_id,
        details={"restored_status": body.status},
    )
    await db.commit()
    return _assignment_detail(assignment)


@router.put("/assignments/{assignment_id}/submission", response_model=AssignmentSubmissionDetail)
async def upsert_own_submission(
    assignment_id: UUID,
    body: AssignmentSubmissionUpsert,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    """수강생은 공개 기간 안에서 자신의 text Markdown 제출 하나만 생성·수정한다."""
    assignment = await _assignment_or_404(db, assignment_id)
    klass = await _class_or_404(db, assignment.class_id)
    if principal.is_education_manager:
        # 교육 관리자가 수강생을 가장해 제출하는 우회 경로를 만들지 않는다.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="교육 관리자는 수강생 제출 API를 사용할 수 없습니다.")
    enrollment_status = await _enrollment_status(
        db,
        class_id=klass.id,
        student_id=principal.user_id,
    )
    now = utc_now()
    if not is_submission_window_open(
        class_status=klass.status,
        enrollment_status=enrollment_status,
        assignment_status=assignment.status,
        available_from_at=assignment.available_from_at,
        due_at=assignment.due_at,
        now=now,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="현재 수강 상태 또는 제출 기간에서는 과제를 제출할 수 없습니다.",
        )
    submission = (
        await db.execute(
            select(AssignmentSubmission)
            .where(
                AssignmentSubmission.assignment_id == assignment_id,
                AssignmentSubmission.student_id == principal.user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    action = "education.submission.create"
    if submission is None:
        submission = AssignmentSubmission(
            assignment_id=assignment_id,
            student_id=principal.user_id,
            content_markdown=body.content_markdown,
            submitted_at=now,
            updated_at=now,
        )
        db.add(submission)
        await db.flush()
    else:
        submission.content_markdown = body.content_markdown
        submission.updated_at = now
        action = "education.submission.update"
    _add_audit(
        db,
        actor_id=principal.user_id,
        action=action,
        entity_type="submission",
        entity_id=submission.id,
        class_id=klass.id,
        details={
            "assignment_id": str(assignment_id),
            "content_length": rich_content_visible_char_count_for_read(body.content_markdown),
        },
    )
    await db.commit()
    await db.refresh(submission)
    return _submission_detail(submission)


@router.get("/assignments/{assignment_id}/submission")
async def get_own_submission(
    assignment_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    assignment = await _assignment_or_404(db, assignment_id)
    klass = await _class_or_404(db, assignment.class_id)
    if principal.is_education_manager:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="교육 관리자는 수강생 제출 API를 사용할 수 없습니다.")
    enrollment_status = await _enrollment_status(
        db,
        class_id=klass.id,
        student_id=principal.user_id,
    )
    if not is_student_assignment_visible(
        class_status=klass.status,
        enrollment_status=enrollment_status,
        assignment_status=assignment.status,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")
    submission = (
        await db.execute(
            select(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == assignment_id,
                AssignmentSubmission.student_id == principal.user_id,
            )
        )
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="제출본을 찾을 수 없습니다.")
    grade = (
        await db.execute(select(AssignmentGrade).where(AssignmentGrade.submission_id == submission.id))
    ).scalar_one_or_none()
    visible_grade = (
        grade
        if grade
        and can_student_view_grade(
            grade_status=grade.status,
            enrollment_status=enrollment_status,
        )
        else None
    )
    return {
        "submission": _submission_detail(submission, visible_grade),
        "grade": _grade_response(visible_grade, include_feedback=True)
        if visible_grade
        else None,
    }


@router.get("/assignments/{assignment_id}/submissions", response_model=AssignmentSubmissionListResponse)
async def list_assignment_submissions(
    assignment_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    await _assignment_or_404(db, assignment_id)
    total = int(
        (
            await db.execute(
                select(func.count()).select_from(AssignmentSubmission).where(
                    AssignmentSubmission.assignment_id == assignment_id
                )
            )
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(AssignmentSubmission, AssignmentGrade)
            .outerjoin(AssignmentGrade, AssignmentGrade.submission_id == AssignmentSubmission.id)
            .where(AssignmentSubmission.assignment_id == assignment_id)
            .order_by(AssignmentSubmission.updated_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return AssignmentSubmissionListResponse(
        items=[_submission_metadata(submission, grade) for submission, grade in rows],
        total=total,
        page=page,
        size=size,
    )


@router.get("/assignments/{assignment_id}/submissions/{student_id}")
async def get_assignment_submission_for_grading(
    assignment_id: UUID,
    student_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    await _assignment_or_404(db, assignment_id)
    submission = (
        await db.execute(
            select(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == assignment_id,
                AssignmentSubmission.student_id == student_id,
            )
        )
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="제출본을 찾을 수 없습니다.")
    grade = (
        await db.execute(select(AssignmentGrade).where(AssignmentGrade.submission_id == submission.id))
    ).scalar_one_or_none()
    return {
        "submission": _submission_detail(submission, grade),
        "grade": _grade_response(grade, include_feedback=True) if grade else None,
    }


@router.put("/assignments/{assignment_id}/submissions/{student_id}/grade", response_model=AssignmentGradeResponse)
async def upsert_assignment_grade(
    assignment_id: UUID,
    student_id: UUID,
    body: AssignmentGradeUpsert,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    assignment = await _assignment_or_404(db, assignment_id)
    if body.score > assignment.max_score:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="점수는 과제의 최대 점수를 초과할 수 없습니다.",
        )
    submission = (
        await db.execute(
            select(AssignmentSubmission)
            .where(
                AssignmentSubmission.assignment_id == assignment_id,
                AssignmentSubmission.student_id == student_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="제출본을 찾을 수 없습니다.")
    grade = (
        await db.execute(
            select(AssignmentGrade).where(AssignmentGrade.submission_id == submission.id).with_for_update()
        )
    ).scalar_one_or_none()
    now = utc_now()
    action = "education.grade.create"
    if grade is None:
        grade = AssignmentGrade(
            submission_id=submission.id,
            score=body.score,
            feedback_markdown=body.feedback_markdown,
            status=body.status,
            graded_by=principal.user_id,
            graded_at=now,
            published_at=now if body.status == GradeStatus.PUBLISHED.value else None,
            updated_at=now,
        )
        db.add(grade)
        await db.flush()
    else:
        grade.score = body.score
        grade.feedback_markdown = body.feedback_markdown
        grade.status = body.status
        grade.graded_by = principal.user_id
        grade.graded_at = now
        grade.published_at = now if body.status == GradeStatus.PUBLISHED.value else None
        grade.updated_at = now
        action = "education.grade.update"
    _add_audit(
        db,
        actor_id=principal.user_id,
        action=action,
        entity_type="grade",
        entity_id=grade.id,
        class_id=assignment.class_id,
        details={
            "assignment_id": str(assignment_id),
            "student_id": str(student_id),
            "score": grade.score,
            "status": grade.status,
            "feedback_recorded": bool(grade.feedback_markdown),
        },
    )
    await db.commit()
    await db.refresh(grade)
    return _grade_response(grade, include_feedback=True)


# ── 교수 private 자료 ──


async def _list_assets(
    db: AsyncSession,
    *,
    class_id: UUID,
    assignment_id: UUID | None,
    page: int,
    size: int,
) -> EducationAssetListResponse:
    conditions: list = [EducationAsset.class_id == class_id, EducationAsset.deleted_at.is_(None)]
    if assignment_id is None:
        conditions.append(EducationAsset.scope == EducationAssetScope.CLASS.value)
    else:
        conditions.extend(
            [
                EducationAsset.scope == EducationAssetScope.ASSIGNMENT.value,
                EducationAsset.assignment_id == assignment_id,
            ]
        )
    total = int(
        (
            await db.execute(select(func.count()).select_from(EducationAsset).where(*conditions))
        ).scalar_one()
    )
    assets = (
        await db.execute(
            select(EducationAsset)
            .where(*conditions)
            .order_by(EducationAsset.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return EducationAssetListResponse(
        items=[_asset_response(asset) for asset in assets],
        total=total,
        page=page,
        size=size,
    )


@router.get("/classes/{class_id}/asset-usage", response_model=EducationAssetUsageResponse)
async def get_class_asset_usage(
    class_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    """교육 관리자만 수업 전체 private 자료 사용량·안전 상한을 본다."""
    require_education_manager(principal)
    await _class_or_404(db, class_id)
    active_asset_count, active_byte_size = (
        await db.execute(
            select(
                func.count(EducationAsset.id),
                func.coalesce(func.sum(EducationAsset.byte_size), 0),
            ).where(
                EducationAsset.class_id == class_id,
                EducationAsset.deleted_at.is_(None),
            )
        )
    ).one()
    return EducationAssetUsageResponse(
        class_id=class_id,
        active_asset_count=int(active_asset_count),
        active_byte_size=int(active_byte_size),
        max_assets=MAX_EDUCATION_ASSETS_PER_CLASS,
        max_byte_size=MAX_EDUCATION_ASSET_CLASS_TOTAL_SIZE,
        max_file_size=MAX_EDUCATION_ASSET_SIZE,
    )


@router.get("/classes/{class_id}/assets", response_model=EducationAssetListResponse)
async def list_class_assets(
    class_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    klass = await _class_or_404(db, class_id)
    await _require_student_class_visibility(db, klass=klass, principal=principal)
    return await _list_assets(db, class_id=class_id, assignment_id=None, page=page, size=size)


@router.post(
    "/classes/{class_id}/assets",
    response_model=EducationAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_class_asset(
    class_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    klass = await _class_or_404(db, class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의에는 자료를 업로드할 수 없습니다.")
    asset = await _create_education_asset(
        db=db,
        klass=klass,
        assignment=None,
        file=file,
        actor_id=principal.user_id,
    )
    return _asset_response(asset)


@router.get("/assignments/{assignment_id}/assets", response_model=EducationAssetListResponse)
async def list_assignment_assets(
    assignment_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    assignment = await _assignment_or_404(db, assignment_id)
    klass = await _class_or_404(db, assignment.class_id)
    enrollment_status = await _require_student_class_visibility(db, klass=klass, principal=principal)
    if not principal.is_education_manager and not is_student_assignment_visible(
        class_status=klass.status,
        enrollment_status=enrollment_status,
        assignment_status=assignment.status,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="과제를 찾을 수 없습니다.")
    return await _list_assets(
        db,
        class_id=klass.id,
        assignment_id=assignment_id,
        page=page,
        size=size,
    )


@router.post(
    "/assignments/{assignment_id}/assets",
    response_model=EducationAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_assignment_asset(
    assignment_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    assignment = await _assignment_or_404(db, assignment_id, lock=True)
    if assignment.status == AssignmentStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 과제에는 자료를 업로드할 수 없습니다.")
    klass = await _class_or_404(db, assignment.class_id, lock=True)
    if klass.status == TeachingClassStatus.ARCHIVED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="보관된 강의에는 자료를 업로드할 수 없습니다.")
    asset = await _create_education_asset(
        db=db,
        klass=klass,
        assignment=assignment,
        file=file,
        actor_id=principal.user_id,
    )
    return _asset_response(asset)


@router.delete("/assets/{asset_id}")
async def delete_education_asset_endpoint(
    asset_id: UUID,
    body: EducationAssetDeleteRequest,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    """DB에서 먼저 soft-delete해 권한 우회 다운로드를 막고, object는 후속 정리한다."""
    require_education_manager(principal)
    asset = (
        await db.execute(
            select(EducationAsset)
            .where(EducationAsset.id == asset_id, EducationAsset.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="교육 자료를 찾을 수 없습니다.")
    now = utc_now()
    asset.deleted_by = principal.user_id
    asset.deleted_at = now
    asset.delete_reason = body.reason
    db.add(
        EducationAssetCleanupJob(
            asset_id=asset.id,
            object_path=asset.object_path,
            status="pending",
            available_at=now,
        )
    )
    _add_audit(
        db,
        actor_id=principal.user_id,
        action="education.asset.soft_delete",
        entity_type="asset",
        entity_id=asset.id,
        class_id=asset.class_id,
        details={
            "scope": asset.scope,
            "assignment_id": str(asset.assignment_id) if asset.assignment_id else None,
            "byte_size": asset.byte_size,
            "reason_recorded": True,
            "storage_cleanup_job_queued": True,
        },
    )
    await db.commit()
    return {"message": "교육 자료를 삭제했습니다.", "storage_cleanup_pending": True}


@router.get("/assets/{asset_id}/download")
async def download_education_asset(
    asset_id: UUID,
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    asset = (
        await db.execute(
            select(EducationAsset).where(
                EducationAsset.id == asset_id,
                EducationAsset.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="교육 자료를 찾을 수 없습니다.")
    if not principal.is_education_manager:
        klass = await _class_or_404(db, asset.class_id)
        enrollment_status = await _require_student_class_visibility(db, klass=klass, principal=principal)
        if asset.assignment_id is not None:
            assignment = await _assignment_or_404(db, asset.assignment_id)
            if assignment.class_id != klass.id or not is_student_assignment_visible(
                class_status=klass.status,
                enrollment_status=enrollment_status,
                assignment_status=assignment.status,
            ):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="교육 자료를 찾을 수 없습니다.")
    if not is_education_asset_object_path(asset.object_path):
        logger.error("교육 자료 저장 경로 무결성 오류: asset_id=%s", asset.id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="교육 자료를 제공할 수 없습니다.")
    try:
        stream, _stored_content_type, size = get_object_stream(asset.object_path)
    except Exception:
        logger.error("교육 자료 스트리밍 실패: asset_id=%s", asset.id, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="교육 자료를 제공할 수 없습니다.") from None

    return StreamingResponse(
        iter_object_stream(stream),
        media_type=asset.content_type,
        # The iterator covers normal/early iteration exits.  A background
        # close also covers a response that is constructed but never starts
        # iterating because the client disconnects before body streaming.
        background=BackgroundTask(close_object_stream, stream),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(asset.original_name)}",
            "Content-Length": str(size),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


# ── 교육 감사 조회 ──


@router.get("/classes/{class_id}/audit-events", response_model=EducationAuditListResponse)
async def list_class_audit_events(
    class_id: UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=_PAGE_SIZE_MAX),
    db: AsyncSession = Depends(get_db),
    principal: EducationPrincipal = Depends(get_education_principal),
):
    require_education_manager(principal)
    await _class_or_404(db, class_id)
    conditions = [EducationAuditEvent.class_id == class_id]
    total = int(
        (
            await db.execute(select(func.count()).select_from(EducationAuditEvent).where(*conditions))
        ).scalar_one()
    )
    events = (
        await db.execute(
            select(EducationAuditEvent)
            .where(*conditions)
            .order_by(EducationAuditEvent.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return EducationAuditListResponse(
        items=[
            EducationAuditEventResponse(
                id=event.id,
                actor_id=event.actor_id,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                class_id=event.class_id,
                details=event.details,
                created_at=event.created_at,
            )
            for event in events
        ],
        total=total,
        page=page,
        size=size,
    )
