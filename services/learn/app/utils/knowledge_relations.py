"""지식 관계 원장의 콘텐츠 해석과 공개 조회 공통 로직.

관계 원장은 서로 다른 원본 테이블의 ID 형식을 직접 연결하지 않는다. 이 모듈은
원본 콘텐츠를 서버 측 allowlist로 해석하고, 공개 화면에서는 원본의 현재 공개 상태를
다시 확인한다. 따라서 오래된 노드 투영이나 삭제된 콘텐츠 때문에 비공개 정보가
관계 그래프·힌트에 노출되지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import exists, or_, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge, Hint
from app.models.course import ContentStatus, Course, Lesson
from app.models.education import EducationAssignment, EducationRoleGrant, TeachingClass, TeachingClassEnrollment
from app.models.event import PlatformEvent
from app.models.knowledge import (
    ChallengeKnowledgeTag,
    CourseKnowledgeTag,
    KnowledgeContentNode,
    KnowledgeRelation,
    KnowledgeTag,
    LessonKnowledgeTag,
)
from app.utils.event_visibility import is_publicly_visible
from app.utils.knowledge_mapping_v2 import public_evidence_summary


PUBLIC_CONTENT_TYPES = {
    "challenge",
    "course",
    "lesson",
    "knowledge_tag",
    "hint",
    "event",
    "assignment",
}
# ``requires``는 잠금/선행조건 의미를 가지므로 힌트의 "도움 콘텐츠"로 역방향
# 노출하지 않는다. 공개 도움 영역은 안내 목적의 관계만 사용한다.
HELPFUL_RELATION_TYPES = {"recommends", "helps"}


@dataclass(frozen=True)
class ResolvedKnowledgeContent:
    entity_type: str
    entity_id: str
    display_name: str
    is_active: bool
    href: str | None
    course_id: str | None = None
    source_updated_at: datetime | None = None


def normalize_content_identifier(entity_type: str, entity_id: str) -> str:
    """허용된 콘텐츠 유형별 ID를 canonical 문자열로 정규화한다."""
    if entity_type not in PUBLIC_CONTENT_TYPES:
        raise ValueError("지원하지 않는 콘텐츠 유형입니다.")

    value = str(entity_id).strip()
    if not value:
        raise ValueError("콘텐츠 ID는 비어 있을 수 없습니다.")

    if entity_type in {"challenge", "knowledge_tag", "hint"}:
        try:
            numeric_id = int(value)
        except ValueError as exc:
            raise ValueError("콘텐츠 ID 형식이 올바르지 않습니다.") from exc
        if numeric_id <= 0:
            raise ValueError("콘텐츠 ID는 양수여야 합니다.")
        return str(numeric_id)

    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError("콘텐츠 ID 형식이 올바르지 않습니다.") from exc


def _course_is_public(course: Course) -> bool:
    return (
        not course.is_deleted
        and course.is_published
        and course.status == ContentStatus.APPROVED.value
    )


def _challenge_is_public(challenge: Challenge) -> bool:
    return challenge.is_published and challenge.challenge_type == "wargame"


def public_tag_mapping_clause(mapping_model):
    """Return the public approval boundary for a content↔tag junction.

    Historical manual/import/rule rows may not have an approval timestamp, so
    those origins remain compatible.  A model-generated row is public only
    after an explicit approval timestamp; candidate provenance must never turn
    into a visible graph edge merely because it was inserted into a junction.
    """
    return or_(
        mapping_model.origin.is_(None),
        mapping_model.origin != "model_candidate",
        mapping_model.approved_at.is_not(None),
    )


def public_knowledge_tag_ids_query():
    """공개 콘텐츠에 연결되었거나 그 공개 태그의 조상인 태그 ID를 반환한다.

    태그 자체에는 별도 ``is_published`` 상태가 없으므로 공개 그래프·검색에서
    태그를 노출할 수 있는 근거는 공개 워게임, 승인·공개 코스 또는 그 코스의
    레슨에 연결된 매핑이다. 여기에 그 매핑의 부모 taxonomy를 재귀적으로 포함한다.
    부모 태그가 직접 콘텐츠에 매핑되지 않았더라도 공개 자식의 분류 경로를 설명해야
    하므로, 부모를 숨기면 ``tag_parent`` 관계가 중간에서 끊긴다. 반대로 공개
    콘텐츠와 연결되지 않은 독립 태그 가지는 여전히 결과에 들어오지 않는다.

    이 CTE를 공용으로 유지해 overview/search/focus/legacy graph가 서로 다른 공개
    범위를 쓰지 않게 한다. 부모 순환은 태그 CRUD에서 차단되며, 레거시 DB에 남은
    순환도 재귀 ``UNION``의 중복 제거로 무한 확장을 일으키지 않는다.
    """
    directly_public = union(
        select(ChallengeKnowledgeTag.tag_id.label("tag_id"))
        .join(Challenge, Challenge.id == ChallengeKnowledgeTag.challenge_id)
        .where(
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
            public_tag_mapping_clause(ChallengeKnowledgeTag),
        ),
        select(CourseKnowledgeTag.tag_id.label("tag_id"))
        .join(Course, Course.id == CourseKnowledgeTag.course_id)
        .where(
            Course.is_deleted.is_(False),
            Course.is_published.is_(True),
            Course.status == ContentStatus.APPROVED.value,
            public_tag_mapping_clause(CourseKnowledgeTag),
        ),
        select(LessonKnowledgeTag.tag_id.label("tag_id"))
        .join(Lesson, Lesson.id == LessonKnowledgeTag.lesson_id)
        .join(Course, Course.id == Lesson.course_id)
        .where(
            Course.is_deleted.is_(False),
            Course.is_published.is_(True),
            Course.status == ContentStatus.APPROVED.value,
            public_tag_mapping_clause(LessonKnowledgeTag),
        ),
    )
    # A compound UNION cannot itself be extended with CTE.union() in the
    # SQLAlchemy version used by Learn. Wrap the direct set in a SELECT first;
    # the recursive CTE element is then a regular selectable and can safely
    # add taxonomy ancestors.
    directly_public_subquery = directly_public.subquery("directly_public_knowledge_tag_ids")
    public_tags = select(directly_public_subquery.c.tag_id).cte(
        "public_knowledge_tag_ids", recursive=True
    )
    ancestors = (
        select(KnowledgeTag.parent_id.label("tag_id"))
        .join(public_tags, KnowledgeTag.id == public_tags.c.tag_id)
        .where(KnowledgeTag.parent_id.is_not(None))
    )
    public_tags = public_tags.union(ancestors)
    return select(public_tags.c.tag_id)


async def resolve_content(
    db: AsyncSession,
    entity_type: str,
    entity_id: str,
    *,
    public_only: bool,
    viewer_id: UUID | None = None,
) -> ResolvedKnowledgeContent | None:
    """원본 콘텐츠를 해석한다.

    ``public_only``이면 일반 사용자에게 현재 표시 가능한 콘텐츠만 반환한다.
    관리자 조회에서는 비공개·삭제된 원본도 확인하되 ``is_active``에 실제 공개 여부를
    반영한다.
    """
    try:
        normalized_id = normalize_content_identifier(entity_type, entity_id)
    except ValueError:
        return None

    if entity_type == "challenge":
        challenge = await db.get(Challenge, int(normalized_id))
        if not challenge:
            return None
        is_active = _challenge_is_public(challenge)
        if public_only and not is_active:
            return None
        return ResolvedKnowledgeContent(
            entity_type=entity_type,
            entity_id=normalized_id,
            display_name=challenge.name,
            is_active=is_active,
            href=f"/wargame/{challenge.id}" if is_active else None,
            source_updated_at=challenge.updated_at,
        )

    if entity_type == "course":
        course = await db.get(Course, UUID(normalized_id))
        if not course:
            return None
        is_active = _course_is_public(course)
        if public_only and not is_active:
            return None
        return ResolvedKnowledgeContent(
            entity_type=entity_type,
            entity_id=normalized_id,
            display_name=course.title,
            is_active=is_active,
            href=f"/learn/{course.id}" if is_active else None,
            source_updated_at=course.updated_at,
        )

    if entity_type == "lesson":
        result = await db.execute(
            select(Lesson, Course)
            .join(Course, Course.id == Lesson.course_id)
            .where(Lesson.id == UUID(normalized_id))
        )
        row = result.first()
        if not row:
            return None
        lesson, course = row
        is_active = _course_is_public(course)
        if public_only and not is_active:
            return None
        return ResolvedKnowledgeContent(
            entity_type=entity_type,
            entity_id=normalized_id,
            display_name=lesson.title,
            is_active=is_active,
            href=f"/learn/{course.id}/{lesson.id}" if is_active else None,
            course_id=str(course.id),
            source_updated_at=lesson.updated_at,
        )

    if entity_type == "knowledge_tag":
        result = await db.execute(
            select(
                KnowledgeTag,
                KnowledgeTag.id.in_(public_knowledge_tag_ids_query()).label("is_public"),
            ).where(KnowledgeTag.id == int(normalized_id))
        )
        row = result.first()
        if not row:
            return None
        tag, is_active = row
        if public_only and not is_active:
            return None
        # 태그는 공개 원본과 연결된 경우에만 일반 사용자에게 제목·slug를 노출한다.
        return ResolvedKnowledgeContent(
            entity_type=entity_type,
            entity_id=normalized_id,
            display_name=tag.name,
            is_active=bool(is_active),
            # A tag has no standalone public detail route; opening it in the
            # map keeps the node actionable and preserves its original identity.
            href=f"/learn?view=map&focus_type=knowledge_tag&focus_id={tag.id}" if is_active else None,
            # The tag node must move when its name/description/slug changes;
            # using created_at here made the graph revision and node metadata
            # disagree after an edit.
            source_updated_at=tag.updated_at or tag.created_at,
        )

    if entity_type == "event":
        event = await db.get(PlatformEvent, UUID(normalized_id))
        if not event:
            return None
        now = datetime.now(timezone.utc)
        is_active = is_publicly_visible(
            status=event.status,
            visible_from_at=event.visible_from_at,
            visible_until_at=event.visible_until_at,
            now=now,
        )
        if public_only and not is_active:
            return None
        return ResolvedKnowledgeContent(
            entity_type=entity_type,
            entity_id=normalized_id,
            display_name=event.title,
            is_active=is_active,
            href=f"/events/{event.id}" if is_active else None,
            source_updated_at=event.updated_at,
        )

    if entity_type == "assignment":
        result = await db.execute(
            select(EducationAssignment, TeachingClass)
            .join(TeachingClass, TeachingClass.id == EducationAssignment.class_id)
            .where(EducationAssignment.id == UUID(normalized_id))
        )
        row = result.first()
        if not row:
            return None
        assignment, teaching_class = row
        is_active = (
            teaching_class.status == "published"
            and assignment.status == "published"
        )
        if public_only:
            if not is_active or viewer_id is None:
                return None
            has_enrollment = await db.scalar(
                select(
                    exists().where(
                        TeachingClassEnrollment.class_id == teaching_class.id,
                        TeachingClassEnrollment.student_id == viewer_id,
                        TeachingClassEnrollment.status == "active",
                    )
                )
            )
            has_professor_grant = await db.scalar(
                select(
                    exists().where(
                        EducationRoleGrant.subject_id == viewer_id,
                        EducationRoleGrant.role == "professor_admin",
                        EducationRoleGrant.revoked_at.is_(None),
                    )
                )
            )
            if teaching_class.instructor_id != viewer_id and not has_enrollment and not has_professor_grant:
                return None
        return ResolvedKnowledgeContent(
            entity_type=entity_type,
            entity_id=normalized_id,
            display_name=assignment.title,
            is_active=is_active,
            href=f"/learn/classes/{teaching_class.id}?assignment={assignment.id}" if is_active else None,
            source_updated_at=assignment.updated_at,
        )

    # hint: 힌트 본문은 관계 맵 노드 이름에 복제하지 않는다.
    result = await db.execute(
        select(Hint, Challenge)
        .join(Challenge, Challenge.id == Hint.challenge_id)
        .where(Hint.id == int(normalized_id))
    )
    row = result.first()
    if not row:
        return None
    hint, challenge = row
    is_active = _challenge_is_public(challenge)
    if public_only and not is_active:
        return None
    return ResolvedKnowledgeContent(
        entity_type=entity_type,
        entity_id=normalized_id,
        display_name=f"{challenge.name} · 힌트 {hint.order_index + 1}",
        is_active=is_active,
        href=f"/wargame/{challenge.id}" if is_active else None,
        source_updated_at=challenge.updated_at,
    )


async def ensure_content_node(
    db: AsyncSession,
    content: ResolvedKnowledgeContent,
) -> KnowledgeContentNode:
    """관계 작성 트랜잭션 안에서 원본 콘텐츠의 노드 투영을 원자적으로 최신화한다.

    여러 관리자가 같은 원본 콘텐츠를 동시에 관계에 연결해도 ``SELECT → INSERT``
    경쟁으로 unique constraint 오류가 나지 않도록 PostgreSQL upsert를 쓴다.
    관계 서비스의 기준 DB는 PostgreSQL이므로 이 경로는 SQLite 호환보다 운영 정합성을
    우선한다.
    """
    if content.entity_type == "knowledge_tag":
        # 태그 삭제와 경합한 오래된 resolver 결과가 노드를 다시 활성화하지 않는다.
        tag_id = await db.scalar(select(KnowledgeTag.id).where(KnowledgeTag.id == int(content.entity_id))
            .with_for_update(read=True, key_share=True))
        if tag_id is None:
            raise HTTPException(409, "선택한 지식 태그가 삭제되었습니다. 콘텐츠를 다시 선택하세요.")
        content = await resolve_content(db, "knowledge_tag", str(tag_id), public_only=False)
        if content is None:
            raise HTTPException(409, "선택한 지식 태그가 삭제되었습니다. 콘텐츠를 다시 선택하세요.")
    now = datetime.now(timezone.utc)
    statement = (
        pg_insert(KnowledgeContentNode)
        .values(
            id=uuid4(),
            entity_type=content.entity_type,
            entity_id=content.entity_id,
            display_name=content.display_name,
            is_active=content.is_active,
            source_updated_at=content.source_updated_at,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_knowledge_content_node_entity",
            set_={
                "display_name": content.display_name,
                "is_active": content.is_active,
                "source_updated_at": content.source_updated_at,
                "updated_at": now,
            },
        )
        .returning(KnowledgeContentNode)
    )
    result = await db.execute(statement)
    return result.scalar_one()


async def resolve_node(
    db: AsyncSession,
    node: KnowledgeContentNode,
    *,
    public_only: bool,
    viewer_id: UUID | None = None,
) -> ResolvedKnowledgeContent | None:
    """노드 레지스트리 행을 원본 콘텐츠 기준으로 다시 검증한다."""
    return await resolve_content(
        db,
        node.entity_type,
        node.entity_id,
        public_only=public_only,
        viewer_id=viewer_id,
    )


async def get_approved_helpful_content(
    challenge_id: int,
    db: AsyncSession,
) -> list[dict]:
    """문제 상세에 표시할 승인된 도움 콘텐츠만 반환한다.

    방향 관계는 도움 콘텐츠 → 문제를 기본으로 하며, 명시적으로 무방향인 관계만
    역방향도 허용한다. 비공개·삭제된 원본은 항상 제외한다.
    """
    challenge_node_result = await db.execute(
        select(KnowledgeContentNode).where(
            KnowledgeContentNode.entity_type == "challenge",
            KnowledgeContentNode.entity_id == str(challenge_id),
        )
    )
    challenge_node = challenge_node_result.scalar_one_or_none()
    if not challenge_node:
        return []

    relation_result = await db.execute(
        select(KnowledgeRelation).where(
            KnowledgeRelation.status == "approved",
            KnowledgeRelation.relation_type.in_(HELPFUL_RELATION_TYPES),
            or_(
                KnowledgeRelation.target_node_id == challenge_node.id,
                (KnowledgeRelation.is_directed.is_(False))
                & (KnowledgeRelation.source_node_id == challenge_node.id),
            ),
        )
    )
    relations = relation_result.scalars().all()
    if not relations:
        return []

    node_ids = {
        relation.source_node_id
        if relation.target_node_id == challenge_node.id
        else relation.target_node_id
        for relation in relations
    }
    node_result = await db.execute(
        select(KnowledgeContentNode).where(KnowledgeContentNode.id.in_(node_ids))
    )
    nodes_by_id = {node.id: node for node in node_result.scalars().all()}

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for relation in relations:
        content_node_id = (
            relation.source_node_id
            if relation.target_node_id == challenge_node.id
            else relation.target_node_id
        )
        content_node = nodes_by_id.get(content_node_id)
        if not content_node:
            continue
        content = await resolve_node(db, content_node, public_only=True)
        if not content:
            continue
        key = (content.entity_type, content.entity_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "type": content.entity_type,
                "id": content.entity_id,
                "name": content.display_name,
                "href": content.href,
                "course_id": content.course_id,
                "relation_type": relation.relation_type,
                "strength": float(relation.strength),
                "confidence": float(relation.confidence),
                "evidence_summary": public_evidence_summary(relation.evidence_summary),
                "origin": relation.origin,
            }
        )

    return sorted(items, key=lambda item: (-item["strength"], item["name"].casefold()))
