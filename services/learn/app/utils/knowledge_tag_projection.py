"""명시 메타데이터를 공개 지식 태그 매핑으로 투영하는 공통 서비스.

이 모듈은 제목·본문 유사도나 모델 추측을 사용하지 않는다. 공개 상태와
저장된 category/tags/코스 소속만 읽어 결정적 매핑을 만들며, 수동·import
매핑은 절대로 덮어쓰지 않는다. 콘텐츠 승인/발행 트랜잭션 안에서 호출해
발행 직후 그래프가 고립된 장식 노드로 남지 않게 한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import ContentStatus, Course, Lesson
from app.models.knowledge import (
    ChallengeKnowledgeTag,
    CourseKnowledgeTag,
    KnowledgeTag,
    LessonKnowledgeTag,
)
from app.utils.knowledge_tag_audit import add_mapping_audit, mapping_snapshot


EntityType = Literal["challenge", "course", "lesson"]


def normalize_legacy_tag_slugs(value: object) -> set[str]:
    """Challenge.tags의 레거시 JSON에서 비어 있지 않은 slug만 추출한다."""

    if not isinstance(value, list):
        return set()
    return {
        item.strip().lower()
        for item in value
        if isinstance(item, str) and item.strip()
    }


def challenge_metadata_tag_slugs(*, tags: object, category: object) -> set[str]:
    """문제의 명시 메타데이터에서 결정적으로 매핑할 slug를 반환한다.

    ``Challenge.tags``는 기존 입력을 그대로 보존하고, ``category``는
    지식 태그와 slug가 정확히 일치할 때만 추가한다. 제목·본문 유사도나
    후보 점수는 이 공개 매핑 경로에 들어오지 않는다.
    """

    category_slug = category.strip().lower() if isinstance(category, str) else ""
    return normalize_legacy_tag_slugs(tags) | ({category_slug} if category_slug else set())


async def _tag_ids_by_slug(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(select(KnowledgeTag.id, KnowledgeTag.slug))).all()
    return {
        slug.strip().lower(): tag_id
        for tag_id, slug in rows
        if isinstance(slug, str) and slug.strip()
    }


async def lock_content_tag_mapping_entity(db: AsyncSession, *, entity_type: EntityType, entity) -> None:
    """항상 부모 코스 -> 콘텐츠 -> junction 순서로 잠근다."""
    from app.models.challenge import Challenge

    # A dirty lesson must not autoflush (and lock its row) before its parent.
    if entity_type == "lesson":
        await db.execute(select(Course.id).where(Course.id == entity.course_id)
                         .with_for_update(read=True).execution_options(autoflush=False))
    model = {"challenge": Challenge, "course": Course, "lesson": Lesson}[entity_type]
    await db.execute(select(model.id).where(model.id == entity.id)
                     .with_for_update().execution_options(autoflush=False))


def _mapping_contract(entity_type: EntityType):
    if entity_type == "challenge":
        return ChallengeKnowledgeTag, ChallengeKnowledgeTag.challenge_id
    if entity_type == "course":
        return CourseKnowledgeTag, CourseKnowledgeTag.course_id
    return LessonKnowledgeTag, LessonKnowledgeTag.lesson_id


async def _expected_tag_ids(
    db: AsyncSession,
    *,
    entity_type: EntityType,
    entity,
    tag_ids_by_slug: dict[str, int],
) -> tuple[set[int], str]:
    """현재 공개 상태에서만 허용되는 deterministic tag set과 근거를 반환한다."""

    if entity_type == "challenge":
        from app.models.challenge import Challenge
        entity = await db.scalar(select(Challenge).where(Challenge.id == entity.id)
                                 .execution_options(populate_existing=True))
        if entity is None:
            return set(), "문제를 찾을 수 없음"
        if not entity.is_published or entity.challenge_type != "wargame":
            return set(), "공개 워게임이 아님"
        expected = {
            tag_ids_by_slug[slug]
            for slug in challenge_metadata_tag_slugs(
                tags=entity.tags,
                category=entity.category,
            )
            if slug in tag_ids_by_slug
        }
        return expected, "문제의 공개 category/tags 메타데이터와 지식 태그 slug 일치"

    if entity_type == "course":
        entity = await db.scalar(select(Course).where(Course.id == entity.id)
                                 .execution_options(populate_existing=True))
        if entity is None:
            return set(), "코스를 찾을 수 없음"
        if (
            entity.is_deleted
            or not entity.is_published
            or entity.status != ContentStatus.APPROVED.value
        ):
            return set(), "공개 승인 코스가 아님"
        category = str(entity.category or "").strip().lower()
        tag_id = tag_ids_by_slug.get(category)
        return ({tag_id} if tag_id is not None else set()), "공개 코스 category와 지식 태그 slug 일치"

    course = await db.scalar(select(Course).where(Course.id == entity.course_id)
                             .with_for_update(read=True).execution_options(populate_existing=True))
    if not course or course.is_deleted or not course.is_published or course.status != ContentStatus.APPROVED.value:
        return set(), "공개 승인 코스에 속한 레슨이 아님"
    course_tag_ids = await db.execute(
        select(CourseKnowledgeTag.tag_id).where(
            CourseKnowledgeTag.course_id == course.id,
            or_(
                CourseKnowledgeTag.origin != "model_candidate",
                CourseKnowledgeTag.approved_at.is_not(None),
            ),
        )
    )
    # 수동/import 매핑과 이미 생성된 rule 매핑 모두 코스의 authoritative taxonomy다.
    return set(course_tag_ids.scalars().all()), "공개 코스의 지식 태그를 레슨에 상속"


async def project_rule_tag_mappings(
    db: AsyncSession,
    *,
    entity_type: EntityType,
    entity,
    actor_id: UUID,
    include_course_lessons: bool = True,
) -> int:
    """한 콘텐츠의 rule 매핑을 현재 명시 메타데이터와 동기화한다.

    반환값은 추가된 행 수다. 기존 ``manual``/``import``/``model_candidate``
    행은 유지하며, 이전에 생성된 ``rule`` 행만 현재 기대 집합과 다르면
    삭제하고 append-only 감사 이벤트를 남긴다.
    """

    await lock_content_tag_mapping_entity(db, entity_type=entity_type, entity=entity)
    tag_ids_by_slug = await _tag_ids_by_slug(db)
    model, entity_column = _mapping_contract(entity_type)
    expected, evidence_prefix = await _expected_tag_ids(
        db,
        entity_type=entity_type,
        entity=entity,
        tag_ids_by_slug=tag_ids_by_slug,
    )
    if expected:
        # 삭제와 경합한 오래된 slug 조회 결과로 FK 오류를 내거나 태그를 재생성하지 않는다.
        expected = set((await db.scalars(select(KnowledgeTag.id).where(KnowledgeTag.id.in_(expected))
            .order_by(KnowledgeTag.id).with_for_update(read=True, key_share=True))).all())
    existing_result = await db.execute(
        select(model).where(entity_column == entity.id).with_for_update()
    )
    existing = list(existing_result.scalars().all())
    now = datetime.now(timezone.utc)
    existing_by_tag = {mapping.tag_id: mapping for mapping in existing}
    created = 0

    # 규칙 매핑만 stale 정리한다. 수동/import/후보는 사용자 의도를 보존한다.
    for mapping in existing:
        if mapping.origin != "rule" or getattr(mapping, "source_relation_id", None) is not None or mapping.tag_id in expected:
            continue
        await db.delete(mapping)
        add_mapping_audit(
            db,
            entity_type=entity_type,
            entity_id=str(entity.id),
            tag_id=mapping.tag_id,
            action="detach",
            actor_id=actor_id,
            snapshot=mapping_snapshot(mapping, fallback_now=now),
        )

    for tag_id in sorted(expected):
        if tag_id in existing_by_tag:
            continue
        evidence = f"{evidence_prefix}: {tag_id}"
        values = {
            entity_column.key: entity.id,
            "tag_id": tag_id,
            "direction": "content_to_knowledge_tag",
            "strength": 0.9 if entity_type == "challenge" else 0.7 if entity_type == "course" else 0.65,
            "confidence": 0.95 if entity_type == "challenge" else 0.8,
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
        add_mapping_audit(
            db,
            entity_type=entity_type,
            entity_id=str(entity.id),
            tag_id=tag_id,
            action="attach",
            actor_id=actor_id,
            snapshot={
                "direction": "content_to_knowledge_tag",
                "strength": values["strength"],
                "confidence": values["confidence"],
                "evidence_summary": evidence,
                "origin": "rule",
                "created_by": str(actor_id),
                "created_at": now.isoformat(),
                "approved_at": now.isoformat(),
            },
        )

    # 코스의 category가 승인되는 순간 기존 레슨도 같은 트랜잭션에서 투영한다.
    if entity_type == "course" and include_course_lessons:
        lesson_rows = await db.execute(select(Lesson).where(Lesson.course_id == entity.id).order_by(Lesson.id))
        for lesson in lesson_rows.scalars().all():
            created += await project_rule_tag_mappings(
                db,
                entity_type="lesson",
                entity=lesson,
                actor_id=actor_id,
                include_course_lessons=False,
            )
    return created
