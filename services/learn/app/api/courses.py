"""학습 코스 CRUD + 콘텐츠 승인 API."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.course import (
    ContentStatus,
    Course,
    CourseAuditEvent,
    CourseDeletionConfirmation,
    CoursePrerequisite,
    Lesson,
    UserProgress,
)
from app.models.knowledge import (
    ChallengeLearningPrerequisite,
    CourseKnowledgeTag,
    KnowledgeTag,
    LessonKnowledgeTag,
)
from app.schemas.course import (
    ApprovalActionResponse,
    CourseCreate,
    DeletionActionResponse,
    DeletionConfirmationRequest,
    DeletionConfirmationResponse,
    DeletionExecuteRequest,
    DeletionImpactResponse,
    CourseDetail,
    CourseListResponse,
    CourseMetadataResponse,
    CourseMetadataUpdate,
    CoursePrerequisiteReplace,
    CoursePrerequisiteResponse,
    CourseReject,
    CourseSummary,
    LessonSummary,
    PendingCourseResponse,
)
from app.api.deps import (
    get_current_user_id,
    get_optional_user_id,
    get_optional_user_role,
    require_role,
)
from app.utils.auth_client import verify_user_exists
from app.utils.course_prerequisite_rules import (
    build_course_prerequisite_access,
    is_public_course,
    would_create_course_prerequisite_cycle,
)
from app.utils.course_prerequisites import get_course_prerequisite_status
from app.utils.course_prerequisite_mutations import lock_course_prerequisite_mutation
from app.utils.course_metadata import (
    course_metadata_revision,
    normalize_course_description_for_write,
)
from app.utils.knowledge_tag_audit import add_mapping_audit as _add_mapping_audit, manual_mapping_snapshot
from app.utils.knowledge_tag_projection import project_rule_tag_mappings
from app.utils.content_tag_mappings import replace_content_tag_mappings
from app.utils.rich_content import RichContentError

router = APIRouter(tags=["코스"])

# ──── 승인 시스템에서 사용하는 역할 상수 ────
_MODERATOR_ROLES = ("admin", "moderator")
_DELETION_CONFIRMATION_TTL = timedelta(minutes=10)


def _escape_like(value: str) -> str:
    """LIKE 패턴에서 특수 와일드카드 문자를 이스케이프한다."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _course_is_available(course: Course) -> bool:
    return not course.is_deleted


async def _validate_course_prerequisite_replace(
    db: AsyncSession,
    *,
    course_id: UUID,
    prerequisite_ids: list[UUID],
) -> None:
    """그래프·원본·대상 잠금을 얻은 트랜잭션 안에서 상태·자기참조·순환을 검증한다."""
    if course_id in prerequisite_ids:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "자기 자신을 선행 코스로 지정할 수 없습니다.")
    if not prerequisite_ids:
        return

    target_result = await db.execute(select(Course).where(Course.id.in_(prerequisite_ids)))
    targets = {course.id: course for course in target_result.scalars().all()}
    missing_ids = [str(course_id) for course_id in prerequisite_ids if course_id not in targets]
    if missing_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "존재하지 않는 선행 코스가 포함되어 있습니다.")
    unavailable_ids = [
        str(target_id)
        for target_id, target in targets.items()
        if not is_public_course(target)
    ]
    if unavailable_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "보관 삭제·비공개·미승인 코스는 강제 선행조건으로 지정할 수 없습니다.",
        )

    # CoursePrerequisite의 방향은 `현재 코스 -> 먼저 완료해야 할 코스`다. 현재
    # 코스의 기존 간선은 교체될 것이므로 제외하고, 새 간선과 다른 코스 간선을
    # 합친 뒤 선행 대상에서 출발해 현재 코스로 되돌아오는 경로가 있는지 확인한다.
    edge_result = await db.execute(
        select(CoursePrerequisite.course_id, CoursePrerequisite.prerequisite_id).where(
            CoursePrerequisite.course_id != course_id
        )
    )
    if would_create_course_prerequisite_cycle(
        course_id=course_id,
        prerequisite_ids=prerequisite_ids,
        existing_edges=edge_result.all(),
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "코스 선행조건에 순환 참조가 생깁니다.",
        )


async def _course_deletion_impact(db: AsyncSession, course: Course) -> dict[str, int]:
    lesson_ids_result = await db.execute(select(Lesson.id, Lesson.attachments).where(Lesson.course_id == course.id))
    lesson_rows = lesson_ids_result.all()
    lesson_ids = [row.id for row in lesson_rows]
    progress_count = 0
    if lesson_ids:
        progress_result = await db.execute(
            select(func.count()).select_from(UserProgress).where(UserProgress.lesson_id.in_(lesson_ids))
        )
        progress_count = int(progress_result.scalar() or 0)

    prerequisite_result = await db.execute(
        select(func.count()).select_from(CoursePrerequisite).where(
            (CoursePrerequisite.course_id == course.id)
            | (CoursePrerequisite.prerequisite_id == course.id)
        )
    )
    learning_prereq_condition = (
        (ChallengeLearningPrerequisite.prerequisite_type == "course")
        & (ChallengeLearningPrerequisite.prerequisite_target_id == course.id)
    )
    if lesson_ids:
        learning_prereq_condition = learning_prereq_condition | (
            (ChallengeLearningPrerequisite.prerequisite_type == "lesson")
            & (ChallengeLearningPrerequisite.prerequisite_target_id.in_(lesson_ids))
        )
    learning_prereq_result = await db.execute(
        select(func.count()).select_from(ChallengeLearningPrerequisite).where(
            learning_prereq_condition
        )
    )
    course_tag_count = int(
        (await db.scalar(
            select(func.count()).select_from(CourseKnowledgeTag).where(
                CourseKnowledgeTag.course_id == course.id
            )
        )) or 0
    )
    lesson_tag_count = 0
    if lesson_ids:
        lesson_tag_count = int(
            (await db.scalar(
                select(func.count()).select_from(LessonKnowledgeTag).where(
                    LessonKnowledgeTag.lesson_id.in_(lesson_ids)
                )
            )) or 0
        )
    attachment_count = sum(len(row.attachments or []) for row in lesson_rows)
    return {
        "lessons": len(lesson_rows),
        "lesson_progress": progress_count,
        "course_prerequisites": int(prerequisite_result.scalar() or 0),
        "challenge_learning_prerequisites": int(learning_prereq_result.scalar() or 0),
        "knowledge_tag_mappings": course_tag_count + lesson_tag_count,
        "lesson_attachments": attachment_count,
    }


def _add_course_audit(
    db: AsyncSession,
    *,
    course_id: UUID,
    actor_id: UUID,
    action: str,
    details: dict,
) -> None:
    db.add(
        CourseAuditEvent(
            course_id=course_id,
            actor_id=actor_id,
            action=action,
            details=details,
        )
    )


def _build_visibility_filter(
    user_id: UUID | None,
    user_role: str | None,
):
    """사용자 역할에 따른 코스 가시성 필터 조건 목록을 반환한다.

    - admin/moderator: 모든 상태 열람 가능
    - 일반 사용자(로그인): approved + 본인 작성 전체
    - 비로그인: approved만
    """
    # admin/moderator는 필터 없음 (전체 열람)
    if user_role in _MODERATOR_ROLES:
        return []

    from sqlalchemy import or_

    conditions = []
    if user_id:
        # 승인된 코스 + 내가 작성한 모든 코스
        conditions.append(
            or_(
                Course.status == ContentStatus.APPROVED.value,
                Course.author_id == user_id,
            )
        )
    else:
        # 비로그인: 승인된 코스만
        conditions.append(Course.status == ContentStatus.APPROVED.value)

    return conditions


@router.get("/courses", response_model=list[CourseSummary] | CourseListResponse)
async def list_courses(
    category: str | None = Query(None, description="카테고리 필터"),
    difficulty: str | None = Query(None, description="난이도 필터"),
    search: str | None = Query(None, description="제목 검색"),
    status_filter: str | None = Query(None, alias="status", description="상태 필터 (admin/moderator 전용)"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    include_pagination: bool = Query(
        False,
        description="기존 배열 응답 대신 items/total/page/size 메타데이터 포함",
    ),
    include_deleted: bool = Query(False, description="보관 삭제 코스 포함 (admin 전용)"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID | None = Depends(get_optional_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """코스 목록 조회 (카테고리/난이도 필터, 검색, 페이지네이션).

    가시성 규칙:
    - 일반 사용자: approved 상태만 (+ 본인 작성 전체)
    - admin/moderator: 모든 상태 (status 쿼리 파라미터로 필터 가능)
    """
    conditions = []
    if not (include_deleted and user_role == "admin"):
        conditions.append(Course.is_deleted.is_(False))

    # admin/moderator는 미공개 코스도 조회 가능, 일반 사용자는 공개 코스만
    if user_role not in _MODERATOR_ROLES:
        conditions.append(Course.is_published.is_(True))

    # 역할별 가시성 필터 적용
    for condition in _build_visibility_filter(user_id, user_role):
        conditions.append(condition)

    # admin/moderator의 상태 필터
    if status_filter and user_role in _MODERATOR_ROLES:
        conditions.append(Course.status == status_filter)

    if category:
        conditions.append(Course.category == category.lower())
    if difficulty:
        conditions.append(Course.difficulty == difficulty.lower())
    if search:
        escaped = _escape_like(search)
        conditions.append(Course.title.ilike(f"%{escaped}%", escape="\\"))

    query = select(Course).where(*conditions)
    total_courses = 0
    if include_pagination:
        count_query = select(func.count()).select_from(Course).where(*conditions)
        total_courses = int((await db.execute(count_query)).scalar() or 0)

    # admin/moderator는 pending 우선 정렬
    if user_role in _MODERATOR_ROLES:
        status_order = case(
            (Course.status == ContentStatus.PENDING.value, 0),
            (Course.status == ContentStatus.DRAFT.value, 1),
            (Course.status == ContentStatus.APPROVED.value, 2),
            (Course.status == ContentStatus.REJECTED.value, 3),
            else_=4,
        )
        query = query.order_by(
            status_order,
            Course.order_index,
            Course.created_at.desc(),
            Course.id.desc(),
        )
    else:
        query = query.order_by(Course.order_index, Course.created_at.desc(), Course.id.desc())

    query = query.offset((page - 1) * size).limit(size)

    result = await db.execute(query.options(selectinload(Course.lessons)))
    courses = result.scalars().all()

    # 사용자 진행도 일괄 계산 (user_id가 있을 때만)
    progress_map: dict[UUID, float] = {}
    if user_id and courses:
        course_ids = [c.id for c in courses]
        total_q = await db.execute(
            select(Lesson.course_id, func.count(Lesson.id))
            .where(Lesson.course_id.in_(course_ids))
            .group_by(Lesson.course_id)
        )
        total_map = dict(total_q.all())

        completed_q = await db.execute(
            select(Lesson.course_id, func.count(UserProgress.id))
            .join(UserProgress, UserProgress.lesson_id == Lesson.id)
            .where(
                Lesson.course_id.in_(course_ids),
                UserProgress.user_id == user_id,
                UserProgress.completed.is_(True),
            )
            .group_by(Lesson.course_id)
        )
        completed_map = dict(completed_q.all())

        for cid in course_ids:
            total_lessons = total_map.get(cid, 0)
            completed = completed_map.get(cid, 0)
            progress_map[cid] = (
                round(completed / total_lessons * 100, 1)
                if total_lessons > 0
                else 0.0
            )

    summaries = [
        CourseSummary(
            id=c.id,
            title=c.title,
            category=c.category,
            difficulty=c.difficulty,
            estimated_hours=c.estimated_hours,
            thumbnail_url=c.thumbnail_url,
            is_published=c.is_published,
            is_deleted=c.is_deleted,
            status=c.status,
            lesson_count=len(c.lessons),
            progress_percent=progress_map.get(c.id, 0.0),
            created_at=c.created_at,
        )
        for c in courses
    ]
    if include_pagination:
        return CourseListResponse(items=summaries, total=total_courses, page=page, size=size)
    return summaries


@router.get("/courses/pending", response_model=PendingCourseResponse)
async def list_pending_courses(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """승인 대기(pending) 코스 목록 조회 (admin/moderator 전용)."""
    base_filter = (
        (Course.status == ContentStatus.PENDING.value)
        & (Course.is_deleted.is_(False))
    )

    # 총 개수
    count_result = await db.execute(
        select(func.count()).select_from(Course).where(base_filter)
    )
    total = count_result.scalar() or 0

    # 데이터 (오래된 것부터 — 먼저 제출된 것을 먼저 검토)
    query = (
        select(Course)
        .where(base_filter)
        .options(selectinload(Course.lessons))
        .order_by(Course.created_at.asc(), Course.id.asc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(query)
    courses = result.scalars().all()

    items = [
        CourseSummary(
            id=c.id,
            title=c.title,
            category=c.category,
            difficulty=c.difficulty,
            estimated_hours=c.estimated_hours,
            thumbnail_url=c.thumbnail_url,
            is_published=c.is_published,
            is_deleted=c.is_deleted,
            status=c.status,
            lesson_count=len(c.lessons),
            created_at=c.created_at,
        )
        for c in courses
    ]

    return PendingCourseResponse(items=items, total=total, page=page, size=size)


@router.get("/courses/{course_id}", response_model=CourseDetail)
async def get_course(
    course_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID | None = Depends(get_optional_user_id),
    user_role: str | None = Depends(get_optional_user_role),
):
    """코스 상세 조회 (레슨 목록 + 진행도 포함)."""
    result = await db.execute(
        select(Course)
        .where(Course.id == course_id)
        .options(selectinload(Course.lessons), selectinload(Course.prerequisites))
    )
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    # 접근 제어: 상태 + 역할에 따른 가시성 판단
    if course.is_deleted and user_role != "admin":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="삭제된 코스입니다.")

    is_admin_or_mod = user_role in _MODERATOR_ROLES
    is_author = user_id is not None and user_id == course.author_id

    # approved가 아닌 코스는 admin/moderator 또는 작성자만 접근 가능
    if course.status != ContentStatus.APPROVED.value:
        if not (is_admin_or_mod or is_author):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    # 미공개 코스 접근 제어 (기존 로직 유지)
    if not course.is_published:
        if not (is_admin_or_mod or is_author):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    # 강제 선행 코스는 상세·레슨 직접 URL·완료 처리에서 같은 공통 규칙으로 검사한다.
    # 관리자/모더레이터는 콘텐츠 제작·복구를 위해 우회할 수 있다.
    locked = False
    prerequisite_details: list[dict] = []
    if not is_admin_or_mod:
        locked, prerequisite_details = await get_course_prerequisite_status(
            db,
            course_id=course.id,
            user_id=user_id,
        )
    prerequisite_access = build_course_prerequisite_access(
        course_id=course.id,
        is_authenticated=user_id is not None,
        locked=locked,
        prerequisite_details=prerequisite_details,
    )

    # 사용자 진행도 조회
    completed_set: set[UUID] = set()
    if user_id:
        progress_result = await db.execute(
            select(UserProgress.lesson_id)
            .where(UserProgress.user_id == user_id, UserProgress.completed.is_(True))
            .where(UserProgress.lesson_id.in_([lesson.id for lesson in course.lessons]))
        )
        completed_set = {row[0] for row in progress_result.all()}

    total = len(course.lessons)
    completed_count = len(completed_set)
    progress_pct = (completed_count / total * 100) if total > 0 else 0.0

    lessons = [] if locked else [
        LessonSummary(
            id=lesson.id,
            title=lesson.title,
            order_index=lesson.order_index,
            estimated_minutes=lesson.estimated_minutes,
            challenge_id=lesson.challenge_id,
            completed=lesson.id in completed_set,
        )
        for lesson in course.lessons
    ]

    prereq_ids = [p.prerequisite_id for p in course.prerequisites]

    return CourseDetail(
        id=course.id,
        title=course.title,
        description=course.description,
        metadata_revision=course_metadata_revision(course.title, course.description),
        category=course.category,
        difficulty=course.difficulty,
        estimated_hours=course.estimated_hours,
        thumbnail_url=course.thumbnail_url,
        is_published=course.is_published,
        is_deleted=course.is_deleted,
        status=course.status,
        rejection_reason=course.rejection_reason,
        author_id=course.author_id,
        lessons=lessons,
        prerequisites=prereq_ids,
        prerequisite_details=prerequisite_details,
        prerequisite_access=prerequisite_access,
        locked=locked,
        progress_percent=progress_pct,
        created_at=course.created_at,
    )


@router.patch("/courses/{course_id}", response_model=CourseMetadataResponse)
async def update_course_metadata(
    course_id: UUID,
    body: CourseMetadataUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """Update only a course title and rich description with optimistic locking."""

    result = await db.execute(
        select(Course).where(Course.id == course_id).with_for_update()
    )
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="코스를 찾을 수 없습니다.",
        )
    if course.is_deleted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "course_metadata_archived",
                "message": "보관 삭제된 코스의 제목과 설명은 변경할 수 없습니다.",
            },
        )

    before_revision = course_metadata_revision(course.title, course.description)
    if body.expected_metadata_revision != before_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "course_metadata_conflict",
                "message": "다른 변경사항이 먼저 저장되었습니다. 초안을 유지한 채 최신 내용을 확인해 주세요.",
            },
        )

    candidate_title = body.title if "title" in body.model_fields_set else course.title
    if candidate_title is None:  # The schema rejects explicit null; keep the invariant local.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="코스 제목은 null일 수 없습니다.",
        )
    description_source = (
        body.description
        if "description" in body.model_fields_set
        else course.description
    )
    try:
        candidate_description = normalize_course_description_for_write(description_source)
    except RichContentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.as_detail(),
        ) from exc

    changed_fields: list[str] = []
    if candidate_title != course.title:
        changed_fields.append("title")
    if candidate_description != course.description:
        changed_fields.append("description")
    after_revision = course_metadata_revision(candidate_title, candidate_description)

    if changed_fields:
        course.title = candidate_title
        course.description = candidate_description
        _add_course_audit(
            db,
            course_id=course.id,
            actor_id=actor_id,
            action="course.metadata.update",
            details={
                "changed_fields": changed_fields,
                "before_metadata_revision": before_revision,
                "after_metadata_revision": after_revision,
            },
        )

    if body.knowledge_tag_mapping is not None:
        await replace_content_tag_mappings(
            db, entity_type="course", entity=course, body=body.knowledge_tag_mapping, actor_id=actor_id,
        )
    await db.commit()
    return CourseMetadataResponse(
        id=course.id,
        title=candidate_title,
        description=candidate_description,
        metadata_revision=after_revision,
    )


@router.put(
    "/courses/{course_id}/prerequisites",
    response_model=CoursePrerequisiteResponse,
)
async def replace_course_prerequisites(
    course_id: UUID,
    body: CoursePrerequisiteReplace,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """코스의 강제 선행 코스를 원자적으로 교체한다.

    공개되지 않은 콘텐츠·자기참조·순환 참조는 서버에서 거절한다. 이 API는
    기존 강제 선행 데이터만 변경하며, 지식 관계 원장의 추천 관계와 섞지 않는다.
    """
    course = await lock_course_prerequisite_mutation(
        db, course_id=course_id, prerequisite_ids=body.prerequisite_ids,
    )
    if not course:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "코스를 찾을 수 없습니다.")
    if course.is_deleted:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관 삭제된 코스의 선행조건은 변경할 수 없습니다.")

    await _validate_course_prerequisite_replace(
        db,
        course_id=course_id,
        prerequisite_ids=body.prerequisite_ids,
    )
    before_result = await db.execute(
        select(CoursePrerequisite.prerequisite_id).where(CoursePrerequisite.course_id == course_id)
    )
    before_ids = sorted(str(value) for value in before_result.scalars().all())
    await db.execute(delete(CoursePrerequisite).where(CoursePrerequisite.course_id == course_id))
    db.add_all(
        [
            CoursePrerequisite(course_id=course_id, prerequisite_id=prerequisite_id)
            for prerequisite_id in body.prerequisite_ids
        ]
    )
    _add_course_audit(
        db,
        course_id=course_id,
        actor_id=actor_id,
        action="course.prerequisites.replace",
        details={
            "before_prerequisite_ids": before_ids,
            "after_prerequisite_ids": [str(value) for value in body.prerequisite_ids],
        },
    )
    await db.commit()
    return CoursePrerequisiteResponse(
        course_id=course_id,
        prerequisite_ids=body.prerequisite_ids,
    )


@router.post("/courses", response_model=CourseSummary, status_code=status.HTTP_201_CREATED)
async def create_course(
    body: CourseCreate,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """코스 생성 (admin/moderator 전용). 초기 상태는 draft."""
    if not await verify_user_exists(str(user_id)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="사용자를 찾을 수 없습니다.",
        )

    course = Course(
        title=body.title,
        description=body.description,
        category=body.category,
        difficulty=body.difficulty,
        estimated_hours=body.estimated_hours,
        thumbnail_url=body.thumbnail_url,
        author_id=user_id,
        status=ContentStatus.DRAFT.value,
    )
    db.add(course)
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
        mapping = CourseKnowledgeTag(
            course_id=course.id,
            tag_id=tag_id,
            direction="content_to_knowledge_tag",
            strength=1,
            confidence=1,
            evidence_summary="관리자가 생성 시 연결한 지식 태그",
            origin="manual",
            created_by=user_id,
            approved_at=tag_mapping_now,
            created_at=tag_mapping_now,
        )
        db.add(mapping)
        _add_mapping_audit(
            db,
            entity_type="course",
            entity_id=str(course.id),
            tag_id=tag_id,
            action="attach",
            actor_id=user_id,
            snapshot=manual_mapping_snapshot(
                user_id,
                tag_mapping_now,
                evidence_summary="관리자가 생성 시 연결한 지식 태그",
            ),
        )
    await db.commit()
    await db.refresh(course)

    return CourseSummary(
        id=course.id,
        title=course.title,
        category=course.category,
        difficulty=course.difficulty,
        estimated_hours=course.estimated_hours,
        thumbnail_url=course.thumbnail_url,
        is_published=course.is_published,
        is_deleted=course.is_deleted,
        status=course.status,
        lesson_count=0,
        created_at=course.created_at,
    )


@router.patch("/courses/{course_id}/submit", response_model=ApprovalActionResponse)
async def submit_course(
    course_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """코스를 승인 요청 상태로 제출한다 (draft/rejected → pending).

    작성자 본인만 제출 가능하다.
    """
    result = await db.execute(select(Course).where(Course.id == course_id))
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    # 작성자 본인만 제출 가능
    if course.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="삭제된 코스는 제출할 수 없습니다.")
    if course.author_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="본인의 코스만 제출할 수 있습니다.")

    # draft 또는 rejected 상태에서만 제출 가능
    if course.status not in (ContentStatus.DRAFT.value, ContentStatus.REJECTED.value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"현재 상태({course.status})에서는 제출할 수 없습니다. draft 또는 rejected 상태에서만 제출 가능합니다.",
        )

    course.status = ContentStatus.PENDING.value
    course.rejection_reason = None  # 이전 거부 사유 초기화
    course.reviewed_by = None
    course.reviewed_at = None
    await db.commit()
    await db.refresh(course)

    return ApprovalActionResponse(
        id=course.id,
        status=course.status,
        message="코스가 승인 대기 상태로 제출되었습니다.",
    )


@router.patch("/courses/{course_id}/approve", response_model=ApprovalActionResponse)
async def approve_course(
    course_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """코스를 승인한다 (admin/moderator 전용)."""
    result = await db.execute(
        select(Course).where(Course.id == course_id).with_for_update()
    )
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    if course.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="삭제된 코스는 승인할 수 없습니다.")
    if course.status != ContentStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"현재 상태({course.status})에서는 승인할 수 없습니다. pending 상태에서만 승인 가능합니다.",
        )

    now = datetime.now(timezone.utc)
    course.status = ContentStatus.APPROVED.value
    course.is_published = True  # 승인 시 자동 공개
    course.rejection_reason = None
    course.reviewed_by = admin_id
    course.reviewed_at = now
    # 공개 승인과 같은 트랜잭션에서 명시 category 기반 rule 매핑과
    # 소속 레슨의 상속 매핑을 투영한다. 수동/import 매핑은 보존된다.
    await project_rule_tag_mappings(
        db,
        entity_type="course",
        entity=course,
        actor_id=admin_id,
    )
    await db.commit()
    await db.refresh(course)

    return ApprovalActionResponse(
        id=course.id,
        status=course.status,
        message="코스가 승인되었습니다.",
        reviewed_by=course.reviewed_by,
        reviewed_at=course.reviewed_at,
    )


@router.patch("/courses/{course_id}/reject", response_model=ApprovalActionResponse)
async def reject_course(
    course_id: UUID,
    body: CourseReject,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """코스를 거부한다 (admin/moderator 전용). 거부 사유를 반드시 포함해야 한다."""
    result = await db.execute(select(Course).where(Course.id == course_id))
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    if course.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="삭제된 코스는 거부할 수 없습니다.")

    if course.status != ContentStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"현재 상태({course.status})에서는 거부할 수 없습니다. pending 상태에서만 거부 가능합니다.",
        )

    now = datetime.now(timezone.utc)
    course.status = ContentStatus.REJECTED.value
    course.is_published = False  # 거부 시 비공개
    course.rejection_reason = body.rejection_reason
    course.reviewed_by = admin_id
    course.reviewed_at = now
    await db.commit()
    await db.refresh(course)

    return ApprovalActionResponse(
        id=course.id,
        status=course.status,
        message="코스가 거부되었습니다.",
        reviewed_by=course.reviewed_by,
        reviewed_at=course.reviewed_at,
    )


@router.get("/courses/{course_id}/deletion-impact", response_model=DeletionImpactResponse)
async def get_course_deletion_impact(
    course_id: UUID,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin")),
):
    result = await db.execute(select(Course).where(Course.id == course_id))
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    return DeletionImpactResponse(
        resource_type="course",
        target_id=course.id,
        target_name=course.title,
        is_deleted=course.is_deleted,
        related_counts=await _course_deletion_impact(db, course),
        recovery_available=course.is_deleted,
        deleted_at=course.deleted_at,
    )


@router.post(
    "/courses/{course_id}/deletion-confirmations",
    response_model=DeletionConfirmationResponse,
)
async def create_course_deletion_confirmation(
    course_id: UUID,
    body: DeletionConfirmationRequest,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    # Confirmation issuance, deletion, and restoration all lock the parent
    # first so a token cannot commit across a deletion-generation boundary.
    result = await db.execute(
        select(Course).where(Course.id == course_id).with_for_update()
    )
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")
    if course.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 삭제된 코스입니다.")
    if body.target_name != course.title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="최종 확인 대상명이 일치하지 않습니다.")
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="삭제 사유를 입력하세요.")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + _DELETION_CONFIRMATION_TTL
    db.add(
        CourseDeletionConfirmation(
            course_id=course.id,
            actor_id=admin_id,
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            reason=reason,
            expires_at=expires_at,
        )
    )
    await db.commit()
    return DeletionConfirmationResponse(confirmation_token=token, expires_at=expires_at)


@router.post("/courses/{course_id}/restore", response_model=DeletionActionResponse)
async def restore_course(
    course_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    result = await db.execute(select(Course).where(Course.id == course_id).with_for_update())
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")
    if not course.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="삭제되지 않은 코스입니다.")

    prior_reason = course.deletion_reason
    # 삭제 후 복구된 대상을 과거에 발급했지만 사용하지 않은 확인 토큰으로 다시
    # 삭제할 수 없게 한다. 확인 토큰은 대상의 현재 삭제 세대에만 유효하다.
    invalidated_tokens = await db.execute(
        update(CourseDeletionConfirmation)
        .where(
            CourseDeletionConfirmation.course_id == course_id,
            CourseDeletionConfirmation.used_at.is_(None),
        )
        .values(used_at=datetime.now(timezone.utc))
    )
    course.is_deleted = False
    course.is_published = course.published_before_deletion
    course.published_before_deletion = False
    course.deleted_at = None
    course.deleted_by = None
    course.deletion_reason = None
    _add_course_audit(
        db,
        course_id=course.id,
        actor_id=admin_id,
        action="course.restore",
        details={
            "previous_deletion_reason": prior_reason,
            "invalidated_confirmation_tokens": max(invalidated_tokens.rowcount or 0, 0),
        },
    )
    await db.commit()
    return DeletionActionResponse(
        message="코스를 복구했습니다.",
        target_id=course.id,
        is_deleted=False,
        recovery_available=False,
    )


@router.delete("/courses/{course_id}", response_model=DeletionActionResponse)
async def delete_course(
    course_id: UUID,
    body: DeletionExecuteRequest,
    db: AsyncSession = Depends(get_db),
    admin_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    now = datetime.now(timezone.utc)
    token_hash = hashlib.sha256(body.confirmation_token.encode("utf-8")).hexdigest()
    # Keep the existing invalid/cross-target response without taking a row
    # lock, then acquire mutating locks in the shared parent-first order.
    confirmation_probe_result = await db.execute(
        select(
            CourseDeletionConfirmation.id,
            CourseDeletionConfirmation.course_id,
            CourseDeletionConfirmation.actor_id,
        )
        .where(CourseDeletionConfirmation.token_hash == token_hash)
    )
    confirmation_probe = confirmation_probe_result.one_or_none()
    if not confirmation_probe:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="유효한 삭제 확인 토큰이 아닙니다.")
    if confirmation_probe.course_id != course_id or confirmation_probe.actor_id != admin_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="삭제 확인 토큰의 대상 또는 행위자가 일치하지 않습니다.")

    result = await db.execute(select(Course).where(Course.id == course_id).with_for_update())
    course = result.scalar_one_or_none()
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="코스를 찾을 수 없습니다.")

    confirmation_result = await db.execute(
        select(CourseDeletionConfirmation)
        .where(CourseDeletionConfirmation.id == confirmation_probe.id)
        .with_for_update()
    )
    confirmation = confirmation_result.scalar_one_or_none()
    if not confirmation:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="유효한 삭제 확인 토큰이 아닙니다.")
    if confirmation.course_id != course_id or confirmation.actor_id != admin_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="삭제 확인 토큰의 대상 또는 행위자가 일치하지 않습니다.")
    if confirmation.used_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용한 삭제 확인 토큰입니다.")
    if confirmation.expires_at <= now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="삭제 확인 토큰이 만료되었습니다.")
    if course.is_deleted:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 삭제된 코스입니다.")

    impact = await _course_deletion_impact(db, course)
    confirmation.used_at = now
    course.published_before_deletion = course.is_published
    course.is_published = False
    course.is_deleted = True
    course.deleted_at = now
    course.deleted_by = admin_id
    course.deletion_reason = confirmation.reason
    _add_course_audit(
        db,
        course_id=course.id,
        actor_id=admin_id,
        action="course.soft_delete",
        details={"reason": confirmation.reason, "impact": impact},
    )
    await db.commit()
    return DeletionActionResponse(
        message="코스를 보관 삭제했습니다.",
        target_id=course.id,
        is_deleted=True,
        recovery_available=True,
    )
