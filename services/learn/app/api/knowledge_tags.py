"""지식 태그 CRUD API."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, select, delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database import get_db
from app.api.deps import get_current_user_id, require_role
from app.models.knowledge import (
    KnowledgeTag,
    ChallengeKnowledgeTag,
    CourseKnowledgeTag,
    LessonKnowledgeTag,
    KnowledgeTagMappingAuditEvent,
)
from app.models.challenge import Challenge
from app.models.course import ContentStatus, Course, Lesson
from app.schemas.knowledge import (
    ContentTagMappingItem,
    ContentTagMappingEntry,
    ContentTagMappingAuditEventResponse,
    ContentTagMappingResponse,
    ContentTagMappingUpdate,
    KnowledgeTagCreate,
    KnowledgeTagUpdate,
    KnowledgeTagResponse,
    KnowledgeTagDeletionPreview,
    KnowledgeTagReconcileResponse,
    KnowledgeTagUnmappedContent,
)
from app.utils.knowledge_tag_audit import (
    add_mapping_audit as _add_mapping_audit,
    mapping_snapshot as _mapping_snapshot,
)
from app.utils.knowledge_tag_projection import (
    challenge_metadata_tag_slugs,
    project_rule_tag_mappings,
)
from app.utils.knowledge_relations import public_tag_mapping_clause
from app.utils.content_tag_mappings import mapping_revision, replace_content_tag_mappings
from app.utils.knowledge_tag_deletion import delete_tag_preserving_history, load_tag_deletion_impact

router = APIRouter(prefix="/knowledge-tags", tags=["지식태그"])


def _mapping_entry(mapping) -> ContentTagMappingEntry:
    """태그 junction을 관리자용 provenance 계약으로 정규화한다."""
    return ContentTagMappingEntry(
        tag_id=mapping.tag_id,
        direction=mapping.direction or "content_to_knowledge_tag",
        strength=float(mapping.strength if mapping.strength is not None else 1),
        confidence=float(mapping.confidence if mapping.confidence is not None else 1),
        evidence_summary=mapping.evidence_summary or "수동 지식 태그 매핑",
        origin=mapping.origin or "manual",
        created_by=mapping.created_by,
        created_at=mapping.created_at,
        approved_at=mapping.approved_at,
        source_relation_id=getattr(mapping, "source_relation_id", None),
    )


def _mapping_response(entity_type: str, entity_id: str, mappings) -> ContentTagMappingResponse:
    entries = sorted((_mapping_entry(mapping) for mapping in mappings), key=lambda item: item.tag_id)
    return ContentTagMappingResponse(
        entity_type=entity_type,
        entity_id=entity_id,
        tag_ids=[entry.tag_id for entry in entries],
        mappings=entries,
        mapping_revision=mapping_revision(mappings),
    )


def _mapping_provenance(item: ContentTagMappingItem, previous, actor_id: UUID, now: datetime) -> dict:
    """Keep an existing mapping's provenance when an editor changes its quality fields.

    A replace request is a set replacement, not permission to rewrite the
    origin or first-approval timestamp of a mapping that remains attached.
    Newly attached tags receive the normal manual provenance.
    """
    origin = getattr(previous, "origin", None) or "manual"
    previous_approved_at = getattr(previous, "approved_at", None)
    approved_at = (
        previous_approved_at
        if origin == "model_candidate"
        else previous_approved_at or now
    )
    return {
        "direction": item.direction,
        "strength": item.strength,
        "confidence": item.confidence,
        "evidence_summary": item.evidence_summary,
        "origin": origin,
        "created_at": getattr(previous, "created_at", None) or now,
        "created_by": getattr(previous, "created_by", None) or actor_id,
        # Editing provenance fields is not an approval action. In particular,
        # a pending model candidate must remain hidden until an explicit review.
        "approved_at": approved_at,
    }


def _mapping_items_for_replace(
    body: ContentTagMappingUpdate,
    existing_by_tag: dict[int, object],
) -> list[ContentTagMappingItem]:
    """Resolve a replace request without degrading legacy ID-only mappings.

    ``mappings`` is the explicit quality-editing contract, so its values stay
    authoritative. ``tag_ids`` is a compatibility set-replacement contract;
    callers using it never supplied quality fields and therefore must not reset
    the strength, confidence, or evidence of a tag that remains attached.
    """

    items = body.resolved_mappings()
    if body.mappings is not None:
        return items

    resolved: list[ContentTagMappingItem] = []
    for item in items:
        previous = existing_by_tag.get(item.tag_id)
        if previous is None:
            resolved.append(item)
            continue
        previous_strength = getattr(previous, "strength", None)
        previous_confidence = getattr(previous, "confidence", None)
        resolved.append(
            ContentTagMappingItem(
                tag_id=item.tag_id,
                direction=getattr(previous, "direction", None) or item.direction,
                strength=float(
                    previous_strength
                    if previous_strength is not None
                    else item.strength
                ),
                confidence=float(
                    previous_confidence
                    if previous_confidence is not None
                    else item.confidence
                ),
                evidence_summary=(
                    getattr(previous, "evidence_summary", None)
                    or item.evidence_summary
                ),
            )
        )
    return resolved


def _mapping_snapshot_for_item(item: ContentTagMappingItem, previous, actor_id: UUID, now: datetime) -> dict:
    values = _mapping_provenance(item, previous, actor_id, now)
    created_by = values["created_by"]
    created_at = values["created_at"]
    approved_at = values["approved_at"]
    return {
        "direction": values["direction"],
        "strength": float(values["strength"]),
        "confidence": float(values["confidence"]),
        "evidence_summary": values["evidence_summary"],
        "origin": values["origin"],
        "created_by": str(created_by) if created_by else None,
        "created_at": created_at.isoformat() if created_at else None,
        "approved_at": approved_at.isoformat() if approved_at else None,
    }


async def _validate_tag_ids(db: AsyncSession, tag_ids: list[int]) -> None:
    if not tag_ids:
        return
    if len(set(tag_ids)) != len(tag_ids):
        raise HTTPException(422, "지식 태그 ID 중복은 허용되지 않습니다.")
    result = await db.execute(select(KnowledgeTag.id).where(KnowledgeTag.id.in_(tag_ids)))
    found_ids = {tag_id for tag_id in result.scalars().all()}
    if found_ids != set(tag_ids):
        raise HTTPException(404, "존재하지 않는 지식 태그가 포함되어 있습니다.")


async def _validate_parent_tag(
    db: AsyncSession,
    *,
    tag_id: int | None,
    parent_id: int | None,
) -> None:
    """Reject missing, self-referencing, and cyclic tag taxonomy links."""
    if parent_id is None:
        return
    if tag_id is not None and tag_id == parent_id:
        raise HTTPException(422, "지식 태그는 자기 자신을 부모로 지정할 수 없습니다.")

    visited: set[int] = set()
    current_id: int | None = parent_id
    while current_id is not None:
        if current_id in visited or (tag_id is not None and current_id == tag_id):
            raise HTTPException(422, "지식 태그 부모 관계에 순환을 만들 수 없습니다.")
        visited.add(current_id)
        current = await db.get(KnowledgeTag, current_id)
        if not current:
            raise HTTPException(404, "부모 지식 태그를 찾을 수 없습니다.")
        current_id = current.parent_id
        if len(visited) > 100:
            raise HTTPException(422, "지식 태그 계층이 허용된 깊이를 초과했습니다.")


async def _course_or_404(db: AsyncSession, course_id: UUID) -> Course:
    course = await db.get(Course, course_id)
    if not course:
        raise HTTPException(404, "코스를 찾을 수 없습니다.")
    if course.is_deleted:
        raise HTTPException(409, "보관 삭제된 코스의 태그는 변경할 수 없습니다.")
    return course


async def _lesson_or_404(db: AsyncSession, lesson_id: UUID) -> Lesson:
    result = await db.execute(
        select(Lesson, Course)
        .join(Course, Course.id == Lesson.course_id)
        .where(Lesson.id == lesson_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "레슨을 찾을 수 없습니다.")
    lesson, course = row
    if course.is_deleted:
        raise HTTPException(409, "보관 삭제된 코스의 레슨 태그는 변경할 수 없습니다.")
    return lesson


async def _challenge_or_404(db: AsyncSession, challenge_id: int) -> Challenge:
    challenge = await db.get(Challenge, challenge_id)
    if not challenge:
        raise HTTPException(404, "臾몄젣瑜?李얠쓣 ???놁뒿?덈떎.")
    return challenge


@router.get("", response_model=list[KnowledgeTagResponse])
async def list_tags(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """전체 지식 태그 목록."""
    result = await db.execute(
        select(KnowledgeTag).order_by(KnowledgeTag.name)
    )
    return result.scalars().all()


@router.get("/unmapped", response_model=list[KnowledgeTagUnmappedContent])
async def list_unmapped_content(
    entity_type: Literal["all", "challenge", "course", "lesson"] = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """공개 콘텐츠 중 아직 지식 태그가 하나도 없는 항목을 반환한다.

    이 목록은 그래프에 임의의 가짜 노드를 채우지 않는다. 실제 공개 원본과
    실제 junction ``NOT EXISTS``만 사용하므로 관리자가 항목을 열어 매핑을
    저장한 뒤 그래프의 다음 revision에서 바로 확인할 수 있다.
    """
    items: list[KnowledgeTagUnmappedContent] = []
    if entity_type in {"all", "challenge"}:
        result = await db.execute(
            select(Challenge.id, Challenge.name)
            .where(
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
                ~exists().where(
                    ChallengeKnowledgeTag.challenge_id == Challenge.id,
                    public_tag_mapping_clause(ChallengeKnowledgeTag),
                ),
            )
            .order_by(Challenge.name, Challenge.id)
            .limit(limit)
        )
        items.extend(
            KnowledgeTagUnmappedContent(
                entity_type="challenge",
                entity_id=str(challenge_id),
                name=name,
                href=f"/wargame/{challenge_id}",
            )
            for challenge_id, name in result.all()
        )

    if entity_type in {"all", "course"}:
        result = await db.execute(
            select(Course.id, Course.title)
            .where(
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
                ~exists().where(
                    CourseKnowledgeTag.course_id == Course.id,
                    public_tag_mapping_clause(CourseKnowledgeTag),
                ),
            )
            .order_by(Course.title, Course.id)
            .limit(limit)
        )
        items.extend(
            KnowledgeTagUnmappedContent(
                entity_type="course",
                entity_id=str(course_id),
                name=title,
                href=f"/learn/{course_id}",
            )
            for course_id, title in result.all()
        )

    if entity_type in {"all", "lesson"}:
        result = await db.execute(
            select(Lesson.id, Lesson.title, Lesson.course_id)
            .join(Course, Course.id == Lesson.course_id)
            .where(
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
                ~exists().where(
                    LessonKnowledgeTag.lesson_id == Lesson.id,
                    public_tag_mapping_clause(LessonKnowledgeTag),
                ),
            )
            .order_by(Lesson.title, Lesson.id)
            .limit(limit)
        )
        items.extend(
            KnowledgeTagUnmappedContent(
                entity_type="lesson",
                entity_id=str(lesson_id),
                name=title,
                href=f"/learn/{course_id}/{lesson_id}",
                course_id=str(course_id),
            )
            for lesson_id, title, course_id in result.all()
        )

    return sorted(items, key=lambda item: (item.entity_type, item.name.casefold(), item.entity_id))[:limit]


@router.post("/reconcile", response_model=KnowledgeTagReconcileResponse)
async def reconcile_rule_mappings(
    dry_run: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """공개 콘텐츠의 명시 메타데이터를 기존 지식 태그 junction으로 투영한다.

    이 작업은 후보 관계를 승인하는 경로가 아니다. 이미 콘텐츠가 가진
    명시적인 ``Challenge.tags``·코스 category·코스-레슨 소속만 근거로 삼고,
    매핑이 없는 행만 ``origin=rule``로 추가한다. 수동/import 매핑은 절대
    덮어쓰거나 삭제하지 않으며, 모든 추가 행은 append-only 매핑 감사 원장을
    남긴다.
    """
    tag_query = select(KnowledgeTag.id, KnowledgeTag.slug).order_by(KnowledgeTag.id)
    if not dry_run:
        tag_query = tag_query.with_for_update(read=True, key_share=True)
    tag_rows = (await db.execute(tag_query)).all()
    tags_by_slug = {slug.strip().lower(): tag_id for tag_id, slug in tag_rows if slug and slug.strip()}

    challenge_rows = (
        await db.execute(
            select(Challenge.id, Challenge.tags, Challenge.category).where(
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            )
        )
    ).all()
    course_rows = (
        await db.execute(
            select(Course.id, Course.category).where(
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            )
        )
    ).all()
    lesson_rows = (
        await db.execute(
            select(Lesson.id, Lesson.course_id)
            .join(Course, Course.id == Lesson.course_id)
            .where(
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            )
        )
    ).all()

    existing_challenges = {
        (challenge_id, tag_id)
        for challenge_id, tag_id in (
            await db.execute(select(ChallengeKnowledgeTag.challenge_id, ChallengeKnowledgeTag.tag_id))
        ).all()
    }
    existing_courses = {
        (course_id, tag_id)
        for course_id, tag_id in (
            await db.execute(select(CourseKnowledgeTag.course_id, CourseKnowledgeTag.tag_id))
        ).all()
    }
    authoritative_courses = {
        (course_id, tag_id)
        for course_id, tag_id in (
            await db.execute(
                select(CourseKnowledgeTag.course_id, CourseKnowledgeTag.tag_id).where(
                    public_tag_mapping_clause(CourseKnowledgeTag)
                )
            )
        ).all()
    }
    existing_lessons = {
        (lesson_id, tag_id)
        for lesson_id, tag_id in (
            await db.execute(select(LessonKnowledgeTag.lesson_id, LessonKnowledgeTag.tag_id))
        ).all()
    }

    # Course mappings are also the authoritative taxonomy inherited by a
    # lesson. Include this run's rule candidates so a single reconcile pass is
    # complete even when the course row was previously unmapped.
    course_tags_by_id: dict[UUID, set[int]] = {}
    for course_id, category in course_rows:
        tag_id = tags_by_slug.get(str(category or "").strip().lower())
        if tag_id is not None:
            course_tags_by_id.setdefault(course_id, set()).add(tag_id)
    for course_id, tag_id in authoritative_courses:
        course_tags_by_id.setdefault(course_id, set()).add(tag_id)

    candidates: list[tuple[str, object, int, float, float, str]] = []
    for challenge_id, tags, category in challenge_rows:
        for slug in sorted(challenge_metadata_tag_slugs(tags=tags, category=category)):
            tag_id = tags_by_slug.get(slug)
            if tag_id is not None:
                candidates.append(("challenge", challenge_id, tag_id, 0.9, 0.95, f"문제의 공개 category/tags 메타데이터와 지식 태그 slug 일치: {slug}"))
    for course_id, category in course_rows:
        tag_id = tags_by_slug.get(str(category or "").strip().lower())
        if tag_id is not None:
            candidates.append(("course", course_id, tag_id, 0.7, 0.8, f"공개 코스 category와 지식 태그 slug 일치: {category}"))
    for lesson_id, course_id in lesson_rows:
        for tag_id in sorted(course_tags_by_id.get(course_id, set())):
            candidates.append(("lesson", lesson_id, tag_id, 0.65, 0.8, "공개 코스의 지식 태그를 레슨에 상속"))

    # A content/tag pair can be discovered by more than one deterministic rule;
    # preserve the strongest evidence once rather than creating duplicate work.
    unique_candidates: dict[tuple[str, object, int], tuple[str, object, int, float, float, str]] = {}
    for candidate in candidates:
        key = (candidate[0], candidate[1], candidate[2])
        previous = unique_candidates.get(key)
        if previous is None or candidate[3] > previous[3]:
            unique_candidates[key] = candidate

    existing_by_entity = {
        "challenge": existing_challenges,
        "course": existing_courses,
        "lesson": existing_lessons,
    }
    pending = [
        candidate
        for candidate in unique_candidates.values()
        if (candidate[1], candidate[2]) not in existing_by_entity[candidate[0]]
    ]
    existing_count = len(unique_candidates) - len(pending)
    by_entity: dict[str, int] = {"challenge": 0, "course": 0, "lesson": 0}
    for entity_type, *_ in pending:
        by_entity[entity_type] += 1

    created = 0
    if not dry_run:
        now = datetime.now(timezone.utc)
        mapping_models = {
            "challenge": ChallengeKnowledgeTag,
            "course": CourseKnowledgeTag,
            "lesson": LessonKnowledgeTag,
        }
        for entity_type, entity_id, tag_id, strength, confidence, evidence in pending:
            model = mapping_models[entity_type]
            # Do not build a dict of model attributes here: Python evaluates
            # every value before selecting a key, so the challenge model would
            # incorrectly try to access course_id/lesson_id.  Resolve only
            # the column belonging to the selected entity type.
            entity_column = getattr(model, f"{entity_type}_id")
            values = {
                entity_column.key: entity_id,
                "tag_id": tag_id,
                "direction": "content_to_knowledge_tag",
                "strength": strength,
                "confidence": confidence,
                "evidence_summary": evidence,
                "origin": "rule",
                "created_by": actor_id,
                "created_at": now,
                "approved_at": now,
            }
            result = await db.execute(
                pg_insert(model)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[entity_column.key, "tag_id"])
            )
            if int(result.rowcount or 0) != 1:
                continue
            created += 1
            _add_mapping_audit(
                db,
                entity_type=entity_type,
                entity_id=str(entity_id),
                tag_id=tag_id,
                action="attach",
                actor_id=actor_id,
                snapshot={
                    "direction": "content_to_knowledge_tag",
                    "strength": strength,
                    "confidence": confidence,
                    "evidence_summary": evidence,
                    "origin": "rule",
                    "created_by": str(actor_id),
                    "created_at": now.isoformat(),
                    "approved_at": now.isoformat(),
                },
            )
        await db.commit()

    scanned = len(challenge_rows) + len(course_rows) + len(lesson_rows)
    return KnowledgeTagReconcileResponse(
        dry_run=dry_run,
        scanned=scanned,
        candidates=len(unique_candidates),
        created=created,
        existing=existing_count,
        skipped=max(scanned - len(unique_candidates), 0),
        by_entity=by_entity,
    )


@router.post("", response_model=KnowledgeTagResponse, status_code=201)
async def create_tag(
    body: KnowledgeTagCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """지식 태그 생성 (관리자)."""
    existing = await db.execute(
        select(KnowledgeTag).where(
            (KnowledgeTag.name == body.name) | (KnowledgeTag.slug == body.slug)
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, "이미 존재하는 태그 이름 또는 slug입니다.")

    await db.execute(select(KnowledgeTag.id).with_for_update())
    await _validate_parent_tag(db, tag_id=None, parent_id=body.parent_id)
    tag = KnowledgeTag(**body.model_dump())
    db.add(tag)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "이미 사용 중인 지식 태그 이름 또는 slug입니다.") from exc
    await db.refresh(tag)
    return tag


@router.get("/challenges/{challenge_id}", response_model=ContentTagMappingResponse)
async def get_challenge_tag_mapping(
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """臾몄젣??吏???쒓렇 留ㅽ븨???ш린??媛?ν븳 愿由ъ옄 API."""
    challenge = await _challenge_or_404(db, challenge_id)
    result = await db.execute(
        select(ChallengeKnowledgeTag).where(ChallengeKnowledgeTag.challenge_id == challenge_id)
    )
    return _mapping_response("challenge", str(challenge_id), result.scalars().all())


@router.put("/challenges/{challenge_id}", response_model=ContentTagMappingResponse)
async def replace_challenge_tag_mapping(
    challenge_id: int,
    body: ContentTagMappingUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """수동 태그를 교체하고 자동 규칙 태그는 원본 메타데이터와 동기화한다."""
    entity = await _challenge_or_404(db, challenge_id)
    response = await replace_content_tag_mappings(
        db, entity_type="challenge", entity=entity, body=body, actor_id=actor_id,
    )
    await db.commit()
    return response


@router.get("/courses/{course_id}", response_model=ContentTagMappingResponse)
async def get_course_tag_mapping(
    course_id: UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """코스의 지식 태그 매핑을 관리자에게 반환한다."""
    course = await _course_or_404(db, course_id)
    result = await db.execute(
        select(CourseKnowledgeTag).where(CourseKnowledgeTag.course_id == course_id)
    )
    return _mapping_response("course", str(course_id), result.scalars().all())


@router.put("/courses/{course_id}", response_model=ContentTagMappingResponse)
async def replace_course_tag_mapping(
    course_id: UUID,
    body: ContentTagMappingUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """수동 태그를 교체하고 자동 규칙 태그는 원본 메타데이터와 동기화한다."""
    entity = await _course_or_404(db, course_id)
    response = await replace_content_tag_mappings(
        db, entity_type="course", entity=entity, body=body, actor_id=actor_id,
    )
    await db.commit()
    return response


@router.get("/lessons/{lesson_id}", response_model=ContentTagMappingResponse)
async def get_lesson_tag_mapping(
    lesson_id: UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """레슨의 지식 태그 매핑을 관리자에게 반환한다."""
    lesson = await _lesson_or_404(db, lesson_id)
    result = await db.execute(
        select(LessonKnowledgeTag).where(LessonKnowledgeTag.lesson_id == lesson_id)
    )
    return _mapping_response("lesson", str(lesson_id), result.scalars().all())


@router.put("/lessons/{lesson_id}", response_model=ContentTagMappingResponse)
async def replace_lesson_tag_mapping(
    lesson_id: UUID,
    body: ContentTagMappingUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """수동 태그를 교체하고 자동 규칙 태그는 원본 메타데이터와 동기화한다."""
    entity = await _lesson_or_404(db, lesson_id)
    response = await replace_content_tag_mappings(
        db, entity_type="lesson", entity=entity, body=body, actor_id=actor_id,
    )
    await db.commit()
    return response


@router.get("/mapping-events/{entity_type}/{entity_id}", response_model=list[ContentTagMappingAuditEventResponse])
async def list_mapping_events(
    entity_type: str,
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """콘텐츠-태그 매핑의 attach/detach 이력을 반환한다."""
    if entity_type not in {"challenge", "course", "lesson"} or not entity_id.strip():
        raise HTTPException(422, "매핑 이력 대상 형식이 올바르지 않습니다.")
    result = await db.execute(
        select(KnowledgeTagMappingAuditEvent)
        .where(
            KnowledgeTagMappingAuditEvent.entity_type == entity_type,
            KnowledgeTagMappingAuditEvent.entity_id == entity_id.strip(),
        )
        .order_by(
            KnowledgeTagMappingAuditEvent.created_at.desc(),
            KnowledgeTagMappingAuditEvent.id.desc(),
        )
        .limit(200)
    )
    return [
        ContentTagMappingAuditEventResponse.model_validate(event)
        for event in result.scalars().all()
    ]


@router.put("/{tag_id}", response_model=KnowledgeTagResponse)
async def update_tag(
    tag_id: int,
    body: KnowledgeTagUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """지식 태그 수정 (관리자)."""
    tag = await db.get(KnowledgeTag, tag_id)
    if not tag:
        raise HTTPException(404, "태그를 찾을 수 없습니다.")

    update_data = body.model_dump(exclude_unset=True)
    if "name" in update_data or "slug" in update_data:
        duplicate = await db.execute(
            select(KnowledgeTag.id).where(
                KnowledgeTag.id != tag_id,
                (KnowledgeTag.name == update_data.get("name", tag.name))
                | (KnowledgeTag.slug == update_data.get("slug", tag.slug)),
            )
        )
        if duplicate.scalar_one_or_none() is not None:
            raise HTTPException(409, "이미 사용 중인 지식 태그 이름 또는 slug입니다.")
    if "parent_id" in update_data:
        await db.execute(select(KnowledgeTag.id).with_for_update())
        await _validate_parent_tag(db, tag_id=tag.id, parent_id=update_data["parent_id"])
    for key, val in update_data.items():
        setattr(tag, key, val)
    if update_data:
        tag.updated_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "이미 사용 중인 지식 태그 이름 또는 slug입니다.") from exc
    await db.refresh(tag)
    return tag


@router.get("/{tag_id}/deletion-preview", response_model=KnowledgeTagDeletionPreview)
async def preview_tag_deletion(
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
):
    """삭제 전에 현재 이름과 연결 영향을 확인한다. 데이터는 변경하지 않는다."""
    return (await load_tag_deletion_impact(db, tag_id)).preview()


@router.delete("/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role("admin", "moderator")),
    actor_id: UUID = Depends(get_current_user_id),
    confirmation: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
):
    """태그와 해당 연결만 삭제하고 원본 콘텐츠·기존 감사 기록은 보존한다."""
    try:
        await delete_tag_preserving_history(db, tag_id, actor_id=actor_id, confirmation=confirmation)
        await db.commit()
    except DBAPIError as exc:
        await db.rollback()
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if code in {"55P03", "40P01", "40001", "23503"}:
            raise HTTPException(409, "다른 작업에서 태그 또는 연결을 변경 중입니다. 최신 삭제 영향을 다시 확인하세요.") from exc
        raise
    except Exception:
        await db.rollback()
        raise
