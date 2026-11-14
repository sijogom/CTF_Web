"""공개 학습 경로·지식 관계 그래프 API.

기존 선행조건과 태그 기반 연결은 호환성을 위해 그대로 투영한다. 새 관계 원장은
관리자 승인(``approved``)된 행만 추가로 투영하며, 어떤 경우에도 관계 그래프가
문제 잠금·대회 점수·공개 권한을 직접 변경하지 않는다.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import get_optional_user_id
from app.database import get_db
from app.models.challenge import Challenge, ChallengePrerequisite, Hint, Submission
from app.models.course import ContentStatus, Course, CoursePrerequisite, Lesson, UserProgress
from app.models.education import EducationAssignment, EducationRoleGrant, TeachingClass, TeachingClassEnrollment
from app.models.event import PlatformEvent
from app.models.knowledge import (
    ChallengeKnowledgeTag,
    ChallengeLearningPrerequisite,
    CourseKnowledgeTag,
    KnowledgeContentNode,
    KnowledgeProjectionOutbox,
    KnowledgeRelation,
    KnowledgeTag,
    KnowledgeTagMappingAuditEvent,
    KnowledgeGraphRevision,
    LessonKnowledgeTag,
)
from app.schemas.knowledge import (
    GraphEdge,
    GraphNode,
    KnowledgeGraphResponse,
    KnowledgeMapV2Edge,
    KnowledgeMapV2CanvasResponse,
    KnowledgeMapV2FocusResponse,
    KnowledgeMapV2MappingCoverage,
    KnowledgeMapV2Node,
    KnowledgeMapV2OverviewResponse,
    KnowledgeMapV2SearchResponse,
    KnowledgeMapV2SearchResult,
)
from app.utils.knowledge_relations import (
    PUBLIC_CONTENT_TYPES,
    ResolvedKnowledgeContent,
    normalize_content_identifier,
    public_tag_mapping_clause,
    public_knowledge_tag_ids_query,
    resolve_content,
    resolve_node,
)
from app.utils.knowledge_mapping_v2 import (
    KnowledgeMappingValidationError,
    decode_focus_continuation,
    encode_focus_continuation,
    public_evidence_summary,
    system_structure_edge_id,
    system_tag_edge_id,
    system_tag_parent_edge_id,
)
from app.utils.course_prerequisites import get_course_prerequisite_status
from app.utils.learning_path import system_prerequisite_edge_id
from app.services.knowledge_graph_pages import decode_graph_cursor, graph_page, public_nodes_query, public_edges_query


router = APIRouter(tags=["학습경로"])


def _graph_utc_datetime(value: datetime | None) -> datetime:
    """Normalize database timestamps before comparing projection metadata.

    PostgreSQL ``TIMESTAMP WITH TIME ZONE`` normally returns aware values, but
    legacy rows/drivers may still yield a naive datetime.  Comparing that value
    with the aware UTC sentinel used for missing timestamps raises ``TypeError``
    and turns a tag focus expansion into HTTP 500.  Treat a naive persisted
    value as UTC, matching the platform's storage contract, and normalize an
    aware value without changing its instant.
    """
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _graph_node_id(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}-{entity_id}"


def _course_is_public(course: Course) -> bool:
    return (
        not course.is_deleted
        and course.is_published
        and course.status == ContentStatus.APPROVED.value
    )


def _append_edge_once(
    edges: list[GraphEdge],
    seen: set[tuple[str, str, str, bool]],
    edge: GraphEdge,
) -> None:
    key = (edge.source, edge.target, edge.type, edge.is_directed)
    if key not in seen:
        edges.append(edge)
        seen.add(key)


async def _load_user_progress(
    db: AsyncSession,
    user_id,
) -> tuple[set[int], set]:
    if not user_id:
        return set(), set()

    solved_result = await db.execute(
        select(Submission.challenge_id).where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            # CTF 정답은 워게임 선행조건·학습 그래프 완료 상태에 섞이지 않는다.
            Submission.competition_id.is_(None),
        )
    )
    progress_result = await db.execute(
        select(UserProgress.lesson_id).where(
            UserProgress.user_id == user_id,
            UserProgress.completed.is_(True),
        )
    )
    return {row[0] for row in solved_result.all()}, {row[0] for row in progress_result.all()}


async def _build_graph(
    db: AsyncSession,
    user_id,
) -> KnowledgeGraphResponse:
    solved_ids, completed_lesson_ids = await _load_user_progress(db, user_id)
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    seen_edges: set[tuple[str, str, str, bool]] = set()

    # 공개 워게임만 포함한다. CTF 배정·비공개 문제는 대회 전용 API가 담당한다.
    challenges = (
        await db.execute(
            select(Challenge).where(
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            )
        )
    ).scalars().all()
    challenge_ids = {challenge.id for challenge in challenges}

    prereq_result = await db.execute(select(ChallengePrerequisite))
    prerequisite_map: dict[int, list[int]] = defaultdict(list)
    prerequisite_rows = []
    for prerequisite in prereq_result.scalars().all():
        if prerequisite.challenge_id in challenge_ids and prerequisite.prerequisite_id in challenge_ids:
            prerequisite_rows.append(prerequisite)
            prerequisite_map[prerequisite.challenge_id].append(prerequisite.prerequisite_id)

    challenge_tag_result = await db.execute(
        select(ChallengeKnowledgeTag.challenge_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == ChallengeKnowledgeTag.tag_id)
        .where(
            ChallengeKnowledgeTag.challenge_id.in_(challenge_ids),
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
            public_tag_mapping_clause(ChallengeKnowledgeTag),
        )
        if challenge_ids
        else select(ChallengeKnowledgeTag.challenge_id, KnowledgeTag.slug).join(KnowledgeTag, KnowledgeTag.id == ChallengeKnowledgeTag.tag_id).where(False)
    )
    challenge_tags: dict[int, list[str]] = defaultdict(list)
    for challenge_id, tag_slug in challenge_tag_result.all():
        challenge_tags[challenge_id].append(tag_slug)

    tag_result = await db.execute(
        select(KnowledgeTag.id, KnowledgeTag.name, KnowledgeTag.slug).where(
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query())
        )
    )
    tags_by_slug = {
        tag_slug: (tag_id, tag_name)
        for tag_id, tag_name, tag_slug in tag_result.all()
    }

    def ensure_tag_node(tag_slug: str) -> str | None:
        """공개 콘텐츠에 실제로 연결된 태그만 별도 노드로 투영한다."""
        tag = tags_by_slug.get(tag_slug)
        if not tag:
            return None
        tag_id, tag_name = tag
        node_id = _graph_node_id("knowledge_tag", str(tag_id))
        nodes.setdefault(
            node_id,
            GraphNode(
                id=node_id,
                type="knowledge_tag",
                name=tag_name,
                tags=[],
                status="available",
                entity_id=str(tag_id),
                href=f"/learn?view=map&focus_type=knowledge_tag&focus_id={tag_id}",
            ),
        )
        return node_id

    def append_tag_membership(content_node_id: str, tag_slug: str) -> None:
        tag_node_id = ensure_tag_node(tag_slug)
        if not tag_node_id:
            return
        _append_edge_once(
            edges,
            seen_edges,
            GraphEdge(
                source=content_node_id,
                target=tag_node_id,
                type="content_tag",
                is_directed=False,
                strength=0.45,
                confidence=1.0,
                evidence_summary=f"관리자가 연결한 지식 태그: {tag_slug}",
                origin="legacy",
            ),
        )

    for challenge in challenges:
        if not user_id:
            node_status = "unknown"
        elif challenge.id in solved_ids:
            node_status = "solved"
        elif all(required_id in solved_ids for required_id in prerequisite_map.get(challenge.id, [])):
            node_status = "available"
        else:
            node_status = "locked"
        node_id = _graph_node_id("challenge", str(challenge.id))
        nodes[node_id] = GraphNode(
            id=node_id,
            type="challenge",
            name=challenge.name,
            tags=challenge_tags.get(challenge.id, []),
            status=node_status,
            entity_id=str(challenge.id),
            href=f"/wargame/{challenge.id}",
        )
        for tag_slug in challenge_tags.get(challenge.id, []):
            append_tag_membership(node_id, tag_slug)

    # 힌트 본문은 그래프에 복제하지 않는다. 공개 워게임의 힌트 메타 노드와 원본
    # 문제 연결만 노출해 선택한 문제에서 도움 경로를 탐색할 수 있게 한다.
    hints = (
        await db.execute(select(Hint).where(Hint.challenge_id.in_(challenge_ids)))
    ).scalars().all() if challenge_ids else []
    challenge_names = {challenge.id: challenge.name for challenge in challenges}
    for hint in hints:
        hint_node_id = _graph_node_id("hint", str(hint.id))
        nodes[hint_node_id] = GraphNode(
            id=hint_node_id,
            type="hint",
            name=f"{challenge_names[hint.challenge_id]} · 힌트 {hint.order_index + 1}",
            tags=[],
            status="unknown",
            entity_id=str(hint.id),
            href=f"/wargame/{hint.challenge_id}",
        )
        _append_edge_once(
            edges,
            seen_edges,
            GraphEdge(
                source=_graph_node_id("challenge", str(hint.challenge_id)),
                target=hint_node_id,
                type="hint_context",
                strength=0.6,
                confidence=1.0,
                evidence_summary="문제에 연결된 힌트",
                origin="legacy",
            ),
        )

    courses = (
        await db.execute(
            select(Course).where(
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            )
        )
    ).scalars().all()
    course_ids = {course.id for course in courses}
    lessons = (
        await db.execute(select(Lesson).where(Lesson.course_id.in_(course_ids)))
    ).scalars().all() if course_ids else []
    lesson_ids = {lesson.id for lesson in lessons}
    lessons_by_course: dict = defaultdict(list)
    for lesson in lessons:
        lessons_by_course[lesson.course_id].append(lesson)

    course_tag_result = await db.execute(
        select(CourseKnowledgeTag.course_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == CourseKnowledgeTag.tag_id)
        .where(
            CourseKnowledgeTag.course_id.in_(course_ids),
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
            public_tag_mapping_clause(CourseKnowledgeTag),
        )
        if course_ids
        else select(CourseKnowledgeTag.course_id, KnowledgeTag.slug).join(KnowledgeTag, KnowledgeTag.id == CourseKnowledgeTag.tag_id).where(False)
    )
    course_tags: dict = defaultdict(list)
    for course_id, tag_slug in course_tag_result.all():
        course_tags[course_id].append(tag_slug)

    lesson_tag_result = await db.execute(
        select(LessonKnowledgeTag.lesson_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == LessonKnowledgeTag.tag_id)
        .where(
            LessonKnowledgeTag.lesson_id.in_(lesson_ids),
            KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
            public_tag_mapping_clause(LessonKnowledgeTag),
        )
        if lesson_ids
        else select(LessonKnowledgeTag.lesson_id, KnowledgeTag.slug).join(KnowledgeTag, KnowledgeTag.id == LessonKnowledgeTag.tag_id).where(False)
    )
    lesson_tags: dict = defaultdict(list)
    for lesson_id, tag_slug in lesson_tag_result.all():
        lesson_tags[lesson_id].append(tag_slug)

    course_statuses: dict = {}
    for course in courses:
        course_lessons = lessons_by_course.get(course.id, [])
        if not user_id:
            node_status = "unknown"
        else:
            locked, _details = await get_course_prerequisite_status(
                db,
                course_id=course.id,
                user_id=user_id,
            )
            if locked:
                node_status = "locked"
            elif course_lessons and all(lesson.id in completed_lesson_ids for lesson in course_lessons):
                node_status = "completed"
            else:
                node_status = "available"
        course_statuses[course.id] = node_status
        node_id = _graph_node_id("course", str(course.id))
        nodes[node_id] = GraphNode(
            id=node_id,
            type="course",
            name=course.title,
            tags=course_tags.get(course.id, []),
            status=node_status,
            entity_id=str(course.id),
            href=f"/learn/{course.id}",
        )
        for tag_slug in course_tags.get(course.id, []):
            append_tag_membership(node_id, tag_slug)

    # 레슨 노드를 실제로 생성해야 학습 선행 엣지가 dangling edge가 되지 않는다.
    for lesson in lessons:
        if not user_id:
            node_status = "unknown"
        elif lesson.id in completed_lesson_ids:
            node_status = "completed"
        elif course_statuses.get(lesson.course_id) == "locked":
            node_status = "locked"
        else:
            node_status = "available"
        node_id = _graph_node_id("lesson", str(lesson.id))
        nodes[node_id] = GraphNode(
            id=node_id,
            type="lesson",
            name=lesson.title,
            tags=lesson_tags.get(lesson.id, []),
            status=node_status,
            entity_id=str(lesson.id),
            href=f"/learn/{lesson.course_id}/{lesson.id}",
            course_id=str(lesson.course_id),
        )
        _append_edge_once(
            edges,
            seen_edges,
            GraphEdge(
                source=_graph_node_id("course", str(lesson.course_id)),
                target=node_id,
                type="course_lesson",
                strength=0.7,
                confidence=1.0,
                evidence_summary="코스에 포함된 레슨",
                origin="legacy",
            ),
        )
        for tag_slug in lesson_tags.get(lesson.id, []):
            append_tag_membership(node_id, tag_slug)

    for prerequisite in prerequisite_rows:
        _append_edge_once(
            edges,
            seen_edges,
            GraphEdge(
                source=_graph_node_id("challenge", str(prerequisite.prerequisite_id)),
                target=_graph_node_id("challenge", str(prerequisite.challenge_id)),
                type="challenge_prerequisite",
                strength=1.0,
                confidence=1.0,
                evidence_summary="관리자가 설정한 문제 선행조건",
                origin="legacy",
            ),
        )

    learning_prereq_result = await db.execute(select(ChallengeLearningPrerequisite))
    for prerequisite in learning_prereq_result.scalars().all():
        if prerequisite.challenge_id not in challenge_ids:
            continue
        if prerequisite.prerequisite_type == "course":
            if prerequisite.prerequisite_target_id not in course_ids:
                continue
        elif prerequisite.prerequisite_type == "lesson":
            if prerequisite.prerequisite_target_id not in lesson_ids:
                continue
        else:
            continue
        _append_edge_once(
            edges,
            seen_edges,
            GraphEdge(
                source=_graph_node_id(prerequisite.prerequisite_type, str(prerequisite.prerequisite_target_id)),
                target=_graph_node_id("challenge", str(prerequisite.challenge_id)),
                type="learning_prerequisite",
                strength=1.0,
                confidence=1.0,
                evidence_summary="관리자가 설정한 학습 선행조건",
                origin="legacy",
            ),
        )

    course_prereq_result = await db.execute(select(CoursePrerequisite))
    for prerequisite in course_prereq_result.scalars().all():
        if prerequisite.course_id not in course_ids or prerequisite.prerequisite_id not in course_ids:
            continue
        _append_edge_once(
            edges,
            seen_edges,
            GraphEdge(
                source=_graph_node_id("course", str(prerequisite.prerequisite_id)),
                target=_graph_node_id("course", str(prerequisite.course_id)),
                type="course_prerequisite",
                strength=1.0,
                confidence=1.0,
                evidence_summary="관리자가 설정한 코스 선행조건",
                origin="legacy",
            ),
        )

    # 동일 태그는 보조적 관련성일 뿐 강제 잠금으로 취급하지 않는다.
    tag_sources: dict[str, list[str]] = defaultdict(list)
    for course_id, tag_slugs in course_tags.items():
        for tag_slug in tag_slugs:
            tag_sources[tag_slug].append(_graph_node_id("course", str(course_id)))
    for lesson_id, tag_slugs in lesson_tags.items():
        for tag_slug in tag_slugs:
            tag_sources[tag_slug].append(_graph_node_id("lesson", str(lesson_id)))
    tag_challenges: dict[str, list[str]] = defaultdict(list)
    for challenge_id, tag_slugs in challenge_tags.items():
        for tag_slug in tag_slugs:
            tag_challenges[tag_slug].append(_graph_node_id("challenge", str(challenge_id)))
    for tag_slug in set(tag_sources) & set(tag_challenges):
        for source_id in tag_sources[tag_slug]:
            for target_id in tag_challenges[tag_slug]:
                _append_edge_once(
                    edges,
                    seen_edges,
                    GraphEdge(
                        source=source_id,
                        target=target_id,
                        type="tag_related",
                        is_directed=False,
                        strength=0.35,
                        confidence=1.0,
                        evidence_summary=f"공유 지식 태그: {tag_slug}",
                        origin="legacy",
                    ),
                )

    # 새 관계 원장은 approved 상태 + 현재 공개 가능한 원본만 공개 그래프에 투영한다.
    relation_result = await db.execute(
        select(KnowledgeRelation).where(KnowledgeRelation.status == "approved")
    )
    relations = relation_result.scalars().all()
    if relations:
        relation_node_ids = {
            node_id
            for relation in relations
            for node_id in (relation.source_node_id, relation.target_node_id)
        }
        relation_node_result = await db.execute(
            select(KnowledgeContentNode).where(KnowledgeContentNode.id.in_(relation_node_ids))
        )
        relation_nodes = {node.id: node for node in relation_node_result.scalars().all()}

        for relation in relations:
            source_registry_node = relation_nodes.get(relation.source_node_id)
            target_registry_node = relation_nodes.get(relation.target_node_id)
            if not source_registry_node or not target_registry_node:
                continue
            source_content = await resolve_node(db, source_registry_node, public_only=True)
            target_content = await resolve_node(db, target_registry_node, public_only=True)
            if not source_content or not target_content:
                continue
            source_id = _graph_node_id(source_content.entity_type, source_content.entity_id)
            target_id = _graph_node_id(target_content.entity_type, target_content.entity_id)
            if source_id not in nodes:
                nodes[source_id] = GraphNode(
                    id=source_id,
                    type=source_content.entity_type,
                    name=source_content.display_name,
                    status="unknown",
                    entity_id=source_content.entity_id,
                    href=source_content.href,
                    course_id=source_content.course_id,
                )
            if target_id not in nodes:
                nodes[target_id] = GraphNode(
                    id=target_id,
                    type=target_content.entity_type,
                    name=target_content.display_name,
                    status="unknown",
                    entity_id=target_content.entity_id,
                    href=target_content.href,
                    course_id=target_content.course_id,
                )
            _append_edge_once(
                edges,
                seen_edges,
                GraphEdge(
                    source=source_id,
                    target=target_id,
                    type=relation.relation_type,
                    is_directed=relation.is_directed,
                    strength=float(relation.strength),
                    confidence=float(relation.confidence),
                    evidence_summary=public_evidence_summary(relation.evidence_summary),
                    origin=relation.origin,
                ),
            )

    return KnowledgeGraphResponse(nodes=list(nodes.values()), edges=edges)


async def _filter_graph_by_focus(
    graph: KnowledgeGraphResponse,
    *,
    focus_type: str,
    focus_id: str,
    hops: int,
    db: AsyncSession,
) -> KnowledgeGraphResponse:
    try:
        normalized_id = normalize_content_identifier(focus_type, focus_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "그래프 중심 콘텐츠 유형 또는 ID가 올바르지 않습니다.") from exc

    focus_node_id = _graph_node_id(focus_type, normalized_id)
    nodes_by_id = {node.id: node for node in graph.nodes}
    if focus_node_id not in nodes_by_id:
        # 관계가 아직 없는 공개 콘텐츠도 선택하면 빈 그래프 대신 그 노드를 확인할 수 있다.
        content = await resolve_content(db, focus_type, normalized_id, public_only=True)
        if not content:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "공개 가능한 중심 콘텐츠를 찾을 수 없습니다.")
        nodes_by_id[focus_node_id] = GraphNode(
            id=focus_node_id,
            type=content.entity_type,
            name=content.display_name,
            status="unknown",
            entity_id=content.entity_id,
            href=content.href,
            course_id=content.course_id,
        )

    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        adjacency[edge.source].add(edge.target)
        adjacency[edge.target].add(edge.source)

    included = {focus_node_id}
    queue = deque([(focus_node_id, 0)])
    while queue:
        current, distance = queue.popleft()
        if distance >= hops:
            continue
        for neighbor in adjacency.get(current, set()):
            if neighbor not in included:
                included.add(neighbor)
                queue.append((neighbor, distance + 1))

    return KnowledgeGraphResponse(
        nodes=[node for node_id, node in nodes_by_id.items() if node_id in included],
        edges=[edge for edge in graph.edges if edge.source in included and edge.target in included],
    )


@router.get("/knowledge-graph", response_model=KnowledgeGraphResponse)
async def get_knowledge_graph(
    db: AsyncSession = Depends(get_db),
    user_id=Depends(get_optional_user_id),
):
    """기존 학습 맵 호환 API. 공개 가능한 기존·승인 관계를 함께 반환한다."""
    return await _build_graph(db, user_id)


@router.get("/knowledge-map", response_model=KnowledgeGraphResponse)
async def get_knowledge_map(
    focus_type: str | None = Query(None),
    focus_id: str | None = Query(None),
    hops: int = Query(1, ge=0, le=3),
    db: AsyncSession = Depends(get_db),
    user_id=Depends(get_optional_user_id),
):
    """전체 또는 선택한 콘텐츠 중심의 설명 가능한 부분 그래프를 반환한다."""
    if bool(focus_type) != bool(focus_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "focus_type과 focus_id는 함께 지정해야 합니다.")
    graph = await _build_graph(db, user_id)
    if not focus_type:
        return graph
    return await _filter_graph_by_focus(
        graph,
        focus_type=focus_type,
        focus_id=focus_id or "",
        hops=hops,
        db=db,
    )


# ─── V2: 서버 측 포커스 그래프와 안전한 검색 ─────────────────────────

# V2 기본 API는 기존 /knowledge-graph, /knowledge-map의 응답 구조를 바꾸지
# 않는다. 전체 그래프를 먼저 전송한 뒤 브라우저에서 자르는 방식도 사용하지 않는다.
_V2_MAX_DEPTH = 3
_V2_MAX_NODES = 120
_V2_MAX_EDGES = 240
_V2_MAX_EDGE_CANDIDATES_PER_HOP = 400
_V2_RELATION_TYPES = {
    "requires",
    "prepares_for",
    "recommends",
    "helps",
    "related",
    "similar",
    "tag_affinity",
    # Authoritative course -> lesson hierarchy. This is a read-only projection
    # and is not an approval/candidate relation in the knowledge ledger.
    "contains",
    # KnowledgeTag.parent_id is a taxonomy edge, not a reviewable relation.
    "tag_parent",
}

# A public map should start from learning content and actual knowledge
# structure.  Operational records (events/assignments), hint nodes, and weak
# similarity edges remain available through explicit filters, but are not part
# of the default canvas/search surface.
_V2_DEFAULT_CONTENT_TYPES = {
    "challenge",
    "course",
    "lesson",
    "knowledge_tag",
}
_V2_DEFAULT_RELATION_TYPES = {
    "requires",
    "prepares_for",
    "helps",
    "recommends",
    "related",
    "tag_affinity",
    "contains",
}


def _v2_content_key(content: ResolvedKnowledgeContent) -> str:
    """내부 registry UUID가 아닌 원본 콘텐츠 identity를 UI key로 쓴다."""
    return f"{content.entity_type}:{content.entity_id}"


def _v2_node(
    content: ResolvedKnowledgeContent,
    *,
    distance: int,
    is_focus: bool = False,
) -> KnowledgeMapV2Node:
    return KnowledgeMapV2Node(
        id=_v2_content_key(content),
        type=content.entity_type,
        entity_id=content.entity_id,
        name=content.display_name,
        href=content.href,
        is_focus=is_focus,
        distance=distance,
    )


def _parse_v2_filter_values(
    values: list[str] | None,
    *,
    allowed: set[str],
    label: str,
    default: set[str] | None = None,
) -> set[str]:
    if values is None:
        return set(default if default is not None else allowed)
    normalized = {value.strip() for value in values if value and value.strip()}
    if not normalized or not normalized.issubset(allowed):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{label} 필터 값이 올바르지 않습니다.")
    return normalized


def _escape_like_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _v2_graph_revision(db: AsyncSession) -> str:
    """원본 쓰기 epoch와 시간에 따라 바뀌는 공개 이벤트 경계를 결속한다."""
    count, latest = (await db.execute(select(func.count(KnowledgeGraphRevision.id), func.max(KnowledgeGraphRevision.id)))).one()
    now = datetime.now(timezone.utc)
    boundaries = (await db.execute(select(
        func.max(PlatformEvent.visible_from_at).filter(PlatformEvent.visible_from_at <= now),
        func.max(PlatformEvent.visible_until_at).filter(PlatformEvent.visible_until_at <= now),
    ))).one()
    return f"epoch:{count}:{latest or 0}|event-window:" + ":".join(value.isoformat() if value else "-" for value in boundaries)


async def _v2_registry_node(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
) -> KnowledgeContentNode | None:
    return (
        await db.execute(
            select(KnowledgeContentNode).where(
                KnowledgeContentNode.entity_type == entity_type,
                KnowledgeContentNode.entity_id == entity_id,
            )
        )
    ).scalar_one_or_none()


async def _v2_resolve_registry_content(
    db: AsyncSession,
    node: KnowledgeContentNode,
    cache: dict,
    viewer_id=None,
) -> ResolvedKnowledgeContent | None:
    """모든 edge 양 끝점의 현재 공개 권한을 다시 확인한다."""
    cache_key = (node.id, str(viewer_id) if viewer_id else None)
    if cache_key not in cache:
        cache[cache_key] = await resolve_node(
            db,
            node,
            public_only=True,
            viewer_id=viewer_id,
        )
    return cache[cache_key]


async def _v2_project_approved_registry_edges(
    db: AsyncSession,
    *,
    nodes_by_key: dict[str, KnowledgeMapV2Node],
    content_types: set[str],
    relation_types: set[str],
    seen_edges: set[UUID],
    viewer_id=None,
) -> list[KnowledgeMapV2Edge]:
    """Project approved registry relations into the full public canvas.

    The compatibility builder owns legacy prerequisite/tag/structure edges, but
    event and education-assignment content lives only in the registry.  Keeping
    this projection separate prevents those nodes from becoming disconnected
    decoration while preserving the same public endpoint authorization used by
    the bounded focus graph.
    """
    if not relation_types:
        return []

    relation_result = await db.execute(
        select(KnowledgeRelation)
        .where(
            KnowledgeRelation.status == "approved",
            KnowledgeRelation.relation_type.in_(relation_types),
        )
        .order_by(KnowledgeRelation.updated_at.desc(), KnowledgeRelation.id)
        .limit(_V2_MAX_EDGE_CANDIDATES_PER_HOP)
    )
    relations = relation_result.scalars().all()
    if not relations:
        return []

    registry_ids = {
        registry_id
        for relation in relations
        for registry_id in (relation.source_node_id, relation.target_node_id)
    }
    registry_result = await db.execute(
        select(KnowledgeContentNode).where(KnowledgeContentNode.id.in_(registry_ids))
    )
    registry_nodes = {node.id: node for node in registry_result.scalars().all()}
    content_cache: dict = {}
    projected: list[KnowledgeMapV2Edge] = []

    for relation in relations:
        source_registry_node = registry_nodes.get(relation.source_node_id)
        target_registry_node = registry_nodes.get(relation.target_node_id)
        if not source_registry_node or not target_registry_node:
            continue
        source_content = await _v2_resolve_registry_content(
            db, source_registry_node, content_cache, viewer_id
        )
        target_content = await _v2_resolve_registry_content(
            db, target_registry_node, content_cache, viewer_id
        )
        # Never expose a relation count or endpoint for private, unpublished,
        # deleted, or competition-only content.
        if not source_content or not target_content:
            continue
        if source_content.entity_type not in content_types or target_content.entity_type not in content_types:
            continue

        source_key = _v2_content_key(source_content)
        target_key = _v2_content_key(target_content)
        nodes_by_key.setdefault(source_key, _v2_node(source_content, distance=3))
        nodes_by_key.setdefault(target_key, _v2_node(target_content, distance=3))
        if relation.id in seen_edges:
            continue
        seen_edges.add(relation.id)
        projected.append(
            KnowledgeMapV2Edge(
                id=relation.id,
                source=source_key,
                target=target_key,
                relation_type=relation.relation_type,
                is_directed=relation.is_directed,
                strength=float(relation.strength),
                confidence=float(relation.confidence),
                evidence_summary=public_evidence_summary(relation.evidence_summary),
                origin=relation.origin or "approved_relation",
                created_at=relation.created_at,
                approved_at=relation.approved_at,
            )
        )
    return projected


async def _v2_public_relation_edges(
    db: AsyncSession,
    *,
    focus_content: ResolvedKnowledgeContent,
    focus_registry_node: KnowledgeContentNode | None,
    depth: int,
    content_types: set[str],
    relation_types: set[str],
    graph_revision: str,
    viewer_id=None,
) -> KnowledgeMapV2FocusResponse:
    """현재 공개 가능한 원본만 사용하는 서버 측 bounded BFS를 만든다."""
    focus_key = _v2_content_key(focus_content)
    nodes: dict[str, KnowledgeMapV2Node] = {
        focus_key: _v2_node(focus_content, distance=0, is_focus=True)
    }
    distances: dict[str, int] = {focus_key: 0}
    edges: list[KnowledgeMapV2Edge] = []
    seen_edge_ids = set()
    content_cache: dict = {}
    node_key_by_registry_id: dict = {}
    registry_node_by_id: dict = {}
    frontier = set()
    if focus_registry_node:
        frontier.add(focus_registry_node.id)
        node_key_by_registry_id[focus_registry_node.id] = focus_key
        registry_node_by_id[focus_registry_node.id] = focus_registry_node

    truncated = False
    omitted_counts: dict[str, int] = {}
    for current_depth in range(1, depth + 1):
        if not frontier or len(edges) >= _V2_MAX_EDGES:
            break
        candidate_query = (
            select(KnowledgeRelation)
            .where(
                KnowledgeRelation.status == "approved",
                KnowledgeRelation.relation_type.in_(relation_types),
                or_(
                    KnowledgeRelation.source_node_id.in_(frontier),
                    KnowledgeRelation.target_node_id.in_(frontier),
                ),
            )
            .order_by(KnowledgeRelation.updated_at.desc(), KnowledgeRelation.id)
            # Limit is a server-work guard only. It must not become a public
            # ``truncated`` signal because candidates with hidden endpoints would
            # otherwise reveal that private/CTF-only content has relationships.
            .limit(_V2_MAX_EDGE_CANDIDATES_PER_HOP)
        )
        candidates = (await db.execute(candidate_query)).scalars().all()

        candidate_node_ids = {
            node_id
            for relation in candidates
            for node_id in (relation.source_node_id, relation.target_node_id)
        }
        if not candidate_node_ids:
            break
        registry_result = await db.execute(
            select(KnowledgeContentNode).where(KnowledgeContentNode.id.in_(candidate_node_ids))
        )
        registry_node_by_id.update({node.id: node for node in registry_result.scalars().all()})

        next_frontier = set()
        for relation in candidates:
            if relation.id in seen_edge_ids:
                continue
            source_registry_node = registry_node_by_id.get(relation.source_node_id)
            target_registry_node = registry_node_by_id.get(relation.target_node_id)
            if not source_registry_node or not target_registry_node:
                continue
            source_content = await _v2_resolve_registry_content(
                db, source_registry_node, content_cache, viewer_id
            )
            target_content = await _v2_resolve_registry_content(
                db, target_registry_node, content_cache, viewer_id
            )
            # A relation that refers to an unpublished/deleted/CTF-only source is not
            # an "omitted" edge in this public response; revealing even a count would
            # leak that the hidden content exists.
            if not source_content or not target_content:
                continue
            source_key = _v2_content_key(source_content)
            target_key = _v2_content_key(target_content)
            if source_content.entity_type not in content_types or target_content.entity_type not in content_types:
                continue

            unseen = []
            for registry_node, content, key in (
                (source_registry_node, source_content, source_key),
                (target_registry_node, target_content, target_key),
            ):
                node_key_by_registry_id[registry_node.id] = key
                if key not in nodes:
                    unseen.append((registry_node, content, key))
            if len(nodes) + len(unseen) > _V2_MAX_NODES:
                truncated = True
                omitted_counts["node_limit"] = omitted_counts.get("node_limit", 0) + len(unseen)
                continue
            for registry_node, content, key in unseen:
                nodes[key] = _v2_node(content, distance=current_depth)
                distances[key] = current_depth
                next_frontier.add(registry_node.id)

            if len(edges) >= _V2_MAX_EDGES:
                truncated = True
                omitted_counts["edge_limit"] = omitted_counts.get("edge_limit", 0) + 1
                continue
            edges.append(
                KnowledgeMapV2Edge(
                    id=relation.id,
                    source=source_key,
                    target=target_key,
                    relation_type=relation.relation_type,
                    is_directed=relation.is_directed,
                    strength=float(relation.strength),
                    confidence=float(relation.confidence),
                    evidence_summary=public_evidence_summary(relation.evidence_summary),
                    origin=relation.origin,
                    created_at=relation.created_at,
                    approved_at=relation.approved_at,
                )
            )
            seen_edge_ids.add(relation.id)
        frontier = next_frontier

    # Sorting makes force-layout input and non-visual fallback order deterministic.
    ordered_nodes = sorted(nodes.values(), key=lambda node: (node.distance, node.name.casefold(), node.id))
    ordered_edges = sorted(edges, key=lambda edge: str(edge.id))
    continuation = None
    # Continuation is an expansion control, not only an overflow recovery. It is
    # intentionally available after a complete 1-hop graph so the UI can grow
    # the local graph to 2–3 hops without reconstructing query parameters.
    if depth < _V2_MAX_DEPTH:
        continuation = encode_focus_continuation(
            focus_type=focus_content.entity_type,
            focus_id=focus_content.entity_id,
            depth=depth + 1,
            types=sorted(content_types),
            relation_types=sorted(relation_types),
        )
    return KnowledgeMapV2FocusResponse(
        graph_revision=graph_revision,
        focus=nodes[focus_key],
        nodes=ordered_nodes,
        edges=ordered_edges,
        layout_hints={
            "strategy": "seeded_force_v1",
            "seed": f"{graph_revision}:{focus_key}",
            "distance_by_node": distances,
            "strength_distance_rule": "stronger_relation_shorter",
            "direction_visible": True,
        },
        truncated=truncated,
        omitted_counts=omitted_counts,
        continuation=continuation,
        applied_filters={
            "types": sorted(content_types),
            "relation_types": sorted(relation_types),
            "depth": depth,
        },
    )


async def _v2_tag_neighbors(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
) -> list[tuple[str, str, int, object | None, str, object, object, str, str, object | None]]:
    """Return manual tag-junction neighbors for one public graph identity.

    The junction tables are the authoritative mapping edited by administrators.
    This function intentionally does not read candidate/proposal rows and never
    writes a relation, so a model candidate cannot become public by accident.
    """
    if entity_type == "challenge":
        result = await db.execute(
            select(
                ChallengeKnowledgeTag.tag_id,
                ChallengeKnowledgeTag.created_at,
                ChallengeKnowledgeTag.direction,
                ChallengeKnowledgeTag.strength,
                ChallengeKnowledgeTag.confidence,
                ChallengeKnowledgeTag.evidence_summary,
                ChallengeKnowledgeTag.origin,
                ChallengeKnowledgeTag.approved_at,
            )
            .join(KnowledgeTag, KnowledgeTag.id == ChallengeKnowledgeTag.tag_id)
            .where(
                ChallengeKnowledgeTag.challenge_id == int(entity_id),
                KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
                public_tag_mapping_clause(ChallengeKnowledgeTag),
            )
        )
        return [
            ("knowledge_tag", str(tag_id), tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at)
            for tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at in result.all()
        ]
    if entity_type == "course":
        result = await db.execute(
            select(
                CourseKnowledgeTag.tag_id,
                CourseKnowledgeTag.created_at,
                CourseKnowledgeTag.direction,
                CourseKnowledgeTag.strength,
                CourseKnowledgeTag.confidence,
                CourseKnowledgeTag.evidence_summary,
                CourseKnowledgeTag.origin,
                CourseKnowledgeTag.approved_at,
            )
            .join(KnowledgeTag, KnowledgeTag.id == CourseKnowledgeTag.tag_id)
            .where(
                CourseKnowledgeTag.course_id == UUID(entity_id),
                KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
                public_tag_mapping_clause(CourseKnowledgeTag),
            )
        )
        return [
            ("knowledge_tag", str(tag_id), tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at)
            for tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at in result.all()
        ]
    if entity_type == "lesson":
        result = await db.execute(
            select(
                LessonKnowledgeTag.tag_id,
                LessonKnowledgeTag.created_at,
                LessonKnowledgeTag.direction,
                LessonKnowledgeTag.strength,
                LessonKnowledgeTag.confidence,
                LessonKnowledgeTag.evidence_summary,
                LessonKnowledgeTag.origin,
                LessonKnowledgeTag.approved_at,
            )
            .join(KnowledgeTag, KnowledgeTag.id == LessonKnowledgeTag.tag_id)
            .where(
                LessonKnowledgeTag.lesson_id == UUID(entity_id),
                KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
                public_tag_mapping_clause(LessonKnowledgeTag),
            )
        )
        return [
            ("knowledge_tag", str(tag_id), tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at)
            for tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at in result.all()
        ]
    if entity_type == "knowledge_tag":
        tag_id = int(entity_id)
        neighbors: list[tuple[str, str, int, object | None, str, object, object, str, str, object | None]] = []
        challenge_rows = await db.execute(
            select(
                ChallengeKnowledgeTag.challenge_id,
                ChallengeKnowledgeTag.created_at,
                ChallengeKnowledgeTag.direction,
                ChallengeKnowledgeTag.strength,
                ChallengeKnowledgeTag.confidence,
                ChallengeKnowledgeTag.evidence_summary,
                ChallengeKnowledgeTag.origin,
                ChallengeKnowledgeTag.approved_at,
            )
            .where(
                ChallengeKnowledgeTag.tag_id == tag_id,
                public_tag_mapping_clause(ChallengeKnowledgeTag),
            )
        )
        neighbors.extend(
            ("challenge", str(challenge_id), tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at)
            for challenge_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at in challenge_rows.all()
        )
        course_rows = await db.execute(
            select(
                CourseKnowledgeTag.course_id,
                CourseKnowledgeTag.created_at,
                CourseKnowledgeTag.direction,
                CourseKnowledgeTag.strength,
                CourseKnowledgeTag.confidence,
                CourseKnowledgeTag.evidence_summary,
                CourseKnowledgeTag.origin,
                CourseKnowledgeTag.approved_at,
            )
            .where(
                CourseKnowledgeTag.tag_id == tag_id,
                public_tag_mapping_clause(CourseKnowledgeTag),
            )
        )
        neighbors.extend(
            ("course", str(course_id), tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at)
            for course_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at in course_rows.all()
        )
        lesson_rows = await db.execute(
            select(
                LessonKnowledgeTag.lesson_id,
                LessonKnowledgeTag.created_at,
                LessonKnowledgeTag.direction,
                LessonKnowledgeTag.strength,
                LessonKnowledgeTag.confidence,
                LessonKnowledgeTag.evidence_summary,
                LessonKnowledgeTag.origin,
                LessonKnowledgeTag.approved_at,
            )
            .where(
                LessonKnowledgeTag.tag_id == tag_id,
                public_tag_mapping_clause(LessonKnowledgeTag),
            )
        )
        neighbors.extend(
            ("lesson", str(lesson_id), tag_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at)
            for lesson_id, created_at, direction, strength, confidence, evidence_summary, origin, approved_at in lesson_rows.all()
        )
        return neighbors
    return []


async def _v2_tag_projection_edges(
    db: AsyncSession,
    *,
    graph: KnowledgeMapV2FocusResponse,
    focus_content: ResolvedKnowledgeContent,
    depth: int,
    content_types: set[str],
    relation_types: set[str],
    viewer_id=None,
) -> KnowledgeMapV2FocusResponse:
    """Project administrator-maintained tag mappings into a bounded graph.

    Tags were historically visible only in the legacy map. V2 now exposes the
    same mappings as stable, read-only ``tag_affinity`` edges. The graph can be
    expanded through a tag to related public content, while private, deleted,
    unpublished, and competition-only content is resolved and discarded.
    """
    if depth < 1 or "tag_affinity" not in relation_types:
        return graph

    focus_key = _v2_content_key(focus_content)
    nodes_by_key = {node.id: node for node in graph.nodes}
    distances = dict(graph.layout_hints.get("distance_by_node", {}))
    distances.setdefault(focus_key, 0)
    for node in graph.nodes:
        distances[node.id] = min(distances.get(node.id, node.distance), node.distance)

    content_cache: dict[tuple[str, str], ResolvedKnowledgeContent | None] = {}
    source_registry = aliased(KnowledgeContentNode)
    target_registry = aliased(KnowledgeContentNode)
    approved_relation_result = await db.execute(
        select(
            source_registry.entity_type,
            source_registry.entity_id,
            target_registry.entity_type,
            target_registry.entity_id,
        )
        .select_from(KnowledgeRelation)
        .join(source_registry, source_registry.id == KnowledgeRelation.source_node_id)
        .join(target_registry, target_registry.id == KnowledgeRelation.target_node_id)
        .where(
            KnowledgeRelation.status == "approved",
            KnowledgeRelation.relation_type == "tag_affinity",
            KnowledgeRelation.is_directed.is_(True),
        )
    )
    approved_relation_pairs = {
        (source_type, source_id, target_type, target_id)
        for source_type, source_id, target_type, target_id in approved_relation_result.all()
    }

    async def public_content(entity_type: str, entity_id: str) -> ResolvedKnowledgeContent | None:
        key = (entity_type, entity_id)
        if key not in content_cache:
            content_cache[key] = await resolve_content(
                db,
                entity_type,
                entity_id,
                public_only=True,
                viewer_id=viewer_id,
            )
        return content_cache[key]

    frontier: set[tuple[str, str]] = {(focus_content.entity_type, focus_content.entity_id)}
    visited: set[tuple[str, str]] = set(frontier)
    projected_edges: list[KnowledgeMapV2Edge] = []
    edge_ids = {edge.id for edge in graph.edges}
    truncated = graph.truncated
    omitted_counts = dict(graph.omitted_counts)

    for current_depth in range(1, depth + 1):
        if not frontier or len(graph.edges) + len(projected_edges) >= _V2_MAX_EDGES:
            break
        next_frontier: set[tuple[str, str]] = set()
        for current_type, current_id in sorted(frontier):
            current = await public_content(current_type, current_id)
            if not current:
                continue
            neighbors = await _v2_tag_neighbors(
                db,
                entity_type=current_type,
                entity_id=current_id,
            )
            for (
                neighbor_type,
                neighbor_id,
                tag_id,
                mapped_at,
                mapping_direction,
                mapping_strength,
                mapping_confidence,
                mapping_evidence,
                mapping_origin,
                mapping_approved_at,
            ) in neighbors:
                if neighbor_type not in content_types:
                    continue
                neighbor = await public_content(neighbor_type, neighbor_id)
                if not neighbor:
                    continue

                # Membership is always represented content -> tag. This keeps
                # direction consistent even when traversal starts at a tag.
                if current_type == "knowledge_tag":
                    content = neighbor
                    tag = current
                else:
                    content = current
                    tag = neighbor
                if "knowledge_tag" not in content_types:
                    continue
                source_key = _v2_content_key(content)
                target_key = _v2_content_key(tag)
                if (
                    content.entity_type,
                    content.entity_id,
                    tag.entity_type,
                    tag.entity_id,
                ) in approved_relation_pairs:
                    # The approved ledger edge is already returned by the
                    # relation BFS. Keep the junction as the mapping source
                    # without drawing a duplicate edge for the same pair.
                    neighbor_key = (neighbor_type, neighbor_id)
                    if neighbor_key not in visited:
                        visited.add(neighbor_key)
                        next_frontier.add(neighbor_key)
                    continue
                edge_id = system_tag_edge_id(
                    content_type=content.entity_type,
                    content_id=content.entity_id,
                    tag_id=tag.entity_id,
                )
                if edge_id in edge_ids:
                    if (neighbor_type, neighbor_id) not in visited:
                        visited.add((neighbor_type, neighbor_id))
                        next_frontier.add((neighbor_type, neighbor_id))
                    continue

                unseen_keys = {
                    key for key in (source_key, target_key) if key not in nodes_by_key
                }
                if len(nodes_by_key) + len(unseen_keys) > _V2_MAX_NODES:
                    truncated = True
                    omitted_counts["node_limit"] = omitted_counts.get("node_limit", 0) + len(unseen_keys)
                    continue
                if len(graph.edges) + len(projected_edges) >= _V2_MAX_EDGES:
                    truncated = True
                    omitted_counts["edge_limit"] = omitted_counts.get("edge_limit", 0) + 1
                    continue

                for endpoint in (content, tag):
                    endpoint_key = _v2_content_key(endpoint)
                    endpoint_distance = min(distances.get(endpoint_key, current_depth), current_depth)
                    distances[endpoint_key] = endpoint_distance
                    if endpoint_key not in nodes_by_key or endpoint_distance < nodes_by_key[endpoint_key].distance:
                        nodes_by_key[endpoint_key] = _v2_node(
                            endpoint,
                            distance=endpoint_distance,
                            is_focus=endpoint_key == focus_key,
                        )

                projected_edges.append(
                    KnowledgeMapV2Edge(
                        id=edge_id,
                        source=source_key,
                        target=target_key,
                        relation_type="tag_affinity",
                        is_directed=(mapping_direction or "content_to_knowledge_tag") == "content_to_knowledge_tag",
                        strength=float(mapping_strength if mapping_strength is not None else 1),
                        confidence=float(mapping_confidence if mapping_confidence is not None else 1),
                        evidence_summary=public_evidence_summary(
                            mapping_evidence or "수동 지식 태그 매핑"
                        ),
                        origin=f"{mapping_origin or 'manual'}_tag_projection",
                        created_at=mapped_at,
                        approved_at=mapping_approved_at or mapped_at,
                    )
                )
                edge_ids.add(edge_id)
                neighbor_key = (neighbor_type, neighbor_id)
                if neighbor_key not in visited:
                    visited.add(neighbor_key)
                    next_frontier.add(neighbor_key)
        frontier = next_frontier

    if not projected_edges:
        return graph
    layout_hints = dict(graph.layout_hints)
    layout_hints["distance_by_node"] = distances
    return KnowledgeMapV2FocusResponse(
        graph_revision=graph.graph_revision,
        focus=nodes_by_key[focus_key],
        nodes=sorted(nodes_by_key.values(), key=lambda node: (node.distance, node.name.casefold(), node.id)),
        edges=sorted([*graph.edges, *projected_edges], key=lambda edge: str(edge.id)),
        layout_hints=layout_hints,
        truncated=truncated,
        omitted_counts=omitted_counts,
        continuation=graph.continuation,
        applied_filters=graph.applied_filters,
    )


async def _v2_tag_hierarchy_projection_edges(
    db: AsyncSession,
    *,
    graph: KnowledgeMapV2FocusResponse,
    focus_content: ResolvedKnowledgeContent,
    depth: int,
    content_types: set[str],
    relation_types: set[str],
) -> KnowledgeMapV2FocusResponse:
    """Project the administrator-maintained tag taxonomy into the map.

    ``KnowledgeTag.parent_id`` is source data, not a candidate relation.  The
    projection is read-only and only exposes child/parent pairs when both tags
    are already connected to public content, preventing a private taxonomy
    branch from becoming discoverable through the graph.
    """
    if depth < 1 or "tag_parent" not in relation_types or "knowledge_tag" not in content_types:
        return graph

    nodes_by_key = {node.id: node for node in graph.nodes}
    distances = dict(graph.layout_hints.get("distance_by_node", {}))
    for node in graph.nodes:
        distances[node.id] = min(distances.get(node.id, node.distance), node.distance)
    focus_key = _v2_content_key(focus_content)
    distances.setdefault(focus_key, 0)
    edge_ids = {edge.id for edge in graph.edges}
    projected_edges: list[KnowledgeMapV2Edge] = []
    truncated = graph.truncated
    omitted_counts = dict(graph.omitted_counts)
    # Resolve the public boundary in its own statement before loading the
    # hierarchy.  Embedding the recursive public-tag CTE in both the child and
    # parent predicates makes SQLAlchemy clone the CTE under the same name in
    # a depth-2 focus query when tag_affinity and tag_parent are requested
    # together.  PostgreSQL then rejects the generated statement and the map
    # returns 500.  Materialising the small public id set here preserves the
    # exact visibility rule while keeping the hierarchy query a plain IN
    # predicate.  This is intentionally server-side; private-only tags never
    # reach the response or the client layout.
    public_tag_result = await db.execute(public_knowledge_tag_ids_query())
    public_tag_ids = {str(tag_id) for (tag_id,) in public_tag_result.all() if tag_id is not None}
    if not public_tag_ids:
        return graph
    frontier: set[str] = {
        node.entity_id
        for node in graph.nodes
        if node.type == "knowledge_tag" and node.distance < depth
    }
    if focus_content.entity_type == "knowledge_tag":
        frontier.add(focus_content.entity_id)

    for current_depth in range(1, depth + 1):
        if not frontier:
            break
        result = await db.execute(
            select(KnowledgeTag, KnowledgeTag.parent_id)
            .where(
                KnowledgeTag.id.in_([int(tag_id) for tag_id in frontier if tag_id.isdigit()]),
                KnowledgeTag.id.in_([int(tag_id) for tag_id in public_tag_ids if tag_id.isdigit()]),
                KnowledgeTag.parent_id.is_not(None),
                KnowledgeTag.parent_id.in_([int(tag_id) for tag_id in public_tag_ids if tag_id.isdigit()]),
            )
        )
        next_frontier: set[str] = set()
        for child, parent_id in result.all():
            parent = await resolve_content(
                db,
                "knowledge_tag",
                str(parent_id),
                public_only=True,
            )
            child_content = await resolve_content(
                db,
                "knowledge_tag",
                str(child.id),
                public_only=True,
            )
            if not parent or not child_content:
                continue
            child_key = _v2_content_key(child_content)
            parent_key = _v2_content_key(parent)
            edge_id = system_tag_parent_edge_id(
                child_tag_id=str(child.id),
                # `parent` is the public projection returned by
                # `resolve_content`, not the ORM KnowledgeTag row.  Using
                # `.id` here only failed when tag_parent was requested
                # (the default one-hop map never exercises this branch).
                parent_tag_id=parent.entity_id,
            )
            if edge_id in edge_ids:
                next_frontier.add(parent.entity_id)
                continue
            unseen_keys = {
                key for key in (child_key, parent_key) if key not in nodes_by_key
            }
            if len(nodes_by_key) + len(unseen_keys) > _V2_MAX_NODES:
                truncated = True
                omitted_counts["node_limit"] = omitted_counts.get("node_limit", 0) + len(unseen_keys)
                continue
            if len(graph.edges) + len(projected_edges) >= _V2_MAX_EDGES:
                truncated = True
                omitted_counts["edge_limit"] = omitted_counts.get("edge_limit", 0) + 1
                continue

            child_distance = min(distances.get(child_key, current_depth - 1), current_depth - 1)
            parent_distance = min(distances.get(parent_key, current_depth), current_depth)
            distances[child_key] = child_distance
            distances[parent_key] = parent_distance
            nodes_by_key[child_key] = _v2_node(
                child_content,
                distance=child_distance,
                is_focus=child_key == focus_key,
            )
            nodes_by_key[parent_key] = _v2_node(
                parent,
                distance=parent_distance,
                is_focus=parent_key == focus_key,
            )
            edge_time = max(
                _graph_utc_datetime(child_content.source_updated_at),
                _graph_utc_datetime(parent.source_updated_at),
            )
            projected_edges.append(
                KnowledgeMapV2Edge(
                    id=edge_id,
                    source=child_key,
                    target=parent_key,
                    relation_type="tag_parent",
                    is_directed=True,
                    strength=1.0,
                    confidence=1.0,
                    evidence_summary="지식 태그 분류 계층",
                    origin="system_tag_hierarchy",
                    created_at=edge_time,
                    approved_at=None,
                )
            )
            edge_ids.add(edge_id)
            next_frontier.add(parent.entity_id)
        frontier = next_frontier

    if not projected_edges:
        return graph
    layout_hints = dict(graph.layout_hints)
    layout_hints["distance_by_node"] = distances
    return KnowledgeMapV2FocusResponse(
        graph_revision=graph.graph_revision,
        focus=nodes_by_key[focus_key],
        nodes=sorted(nodes_by_key.values(), key=lambda node: (node.distance, node.name.casefold(), node.id)),
        edges=sorted([*graph.edges, *projected_edges], key=lambda edge: str(edge.id)),
        layout_hints=layout_hints,
        truncated=truncated,
        omitted_counts=omitted_counts,
        continuation=graph.continuation,
        applied_filters=graph.applied_filters,
    )


async def _v2_course_structure_projection_edges(
    db: AsyncSession,
    *,
    graph: KnowledgeMapV2FocusResponse,
    focus_content: ResolvedKnowledgeContent,
    depth: int,
    relation_types: set[str],
) -> KnowledgeMapV2FocusResponse:
    """Project the authoritative course -> lesson hierarchy into V2."""
    if depth < 1 or "contains" not in relation_types:
        return graph

    nodes_by_key = {node.id: node for node in graph.nodes}
    distances = dict(graph.layout_hints.get("distance_by_node", {}))
    for node in graph.nodes:
        distances[node.id] = min(distances.get(node.id, node.distance), node.distance)
    candidates: set[tuple[str, str]] = set()
    for node in graph.nodes:
        if node.type == "course" and node.distance < depth:
            candidates.add((node.entity_id, "course"))
        elif node.type == "lesson" and node.distance < depth:
            candidates.add((node.entity_id, "lesson"))
    if focus_content.entity_type == "course":
        candidates.add((focus_content.entity_id, "course"))
    elif focus_content.entity_type == "lesson":
        candidates.add((focus_content.entity_id, "lesson"))
    if not candidates:
        return graph

    pairs: set[tuple[str, str, object | None]] = set()
    for entity_id, entity_type in sorted(candidates):
        if entity_type == "course":
            rows = await db.execute(
                select(Lesson.id, Lesson.created_at).where(Lesson.course_id == UUID(entity_id))
            )
            pairs.update((entity_id, str(lesson_id), created_at) for lesson_id, created_at in rows.all())
        else:
            row = await db.execute(
                select(Lesson.course_id, Lesson.created_at).where(Lesson.id == UUID(entity_id))
            )
            lesson_row = row.first()
            if lesson_row:
                course_id, created_at = lesson_row
                pairs.add((str(course_id), entity_id, created_at))

    if not pairs:
        return graph

    content_cache: dict[tuple[str, str], ResolvedKnowledgeContent | None] = {}

    async def public_content(entity_type: str, entity_id: str) -> ResolvedKnowledgeContent | None:
        key = (entity_type, entity_id)
        if key not in content_cache:
            content_cache[key] = await resolve_content(db, entity_type, entity_id, public_only=True)
        return content_cache[key]

    projected_edges: list[KnowledgeMapV2Edge] = []
    edge_ids = {edge.id for edge in graph.edges}
    truncated = graph.truncated
    omitted_counts = dict(graph.omitted_counts)
    for course_id, lesson_id, structure_created_at in sorted(pairs):
        course = await public_content("course", course_id)
        lesson = await public_content("lesson", lesson_id)
        if not course or not lesson:
            continue
        source_key = _v2_content_key(course)
        target_key = _v2_content_key(lesson)
        edge_id = system_structure_edge_id(course_id=course.entity_id, lesson_id=lesson.entity_id)
        if edge_id in edge_ids:
            continue
        source_distance = distances.get(source_key)
        target_existing_distance = distances.get(target_key)
        if source_distance is None:
            source_distance = (
                target_existing_distance + 1
                if target_existing_distance is not None
                else depth
            )
        target_distance = min(
            target_existing_distance if target_existing_distance is not None else source_distance + 1,
            source_distance + 1,
        )
        if target_distance > depth:
            continue
        unseen_keys = {key for key in (source_key, target_key) if key not in nodes_by_key}
        if len(nodes_by_key) + len(unseen_keys) > _V2_MAX_NODES:
            truncated = True
            omitted_counts["node_limit"] = omitted_counts.get("node_limit", 0) + len(unseen_keys)
            continue
        if len(graph.edges) + len(projected_edges) >= _V2_MAX_EDGES:
            truncated = True
            omitted_counts["edge_limit"] = omitted_counts.get("edge_limit", 0) + 1
            continue
        for endpoint, distance in ((course, source_distance), (lesson, target_distance)):
            key = _v2_content_key(endpoint)
            distances[key] = min(distances.get(key, distance), distance)
            if key not in nodes_by_key or distances[key] < nodes_by_key[key].distance:
                nodes_by_key[key] = _v2_node(endpoint, distance=distances[key], is_focus=key == graph.focus.id)
        projected_edges.append(
            KnowledgeMapV2Edge(
                id=edge_id,
                source=source_key,
                target=target_key,
                relation_type="contains",
                is_directed=True,
                strength=0.95,
                confidence=1.0,
                evidence_summary="권위 있는 코스-레슨 구조",
                origin="system_structure_projection",
                created_at=structure_created_at,
                approved_at=None,
            )
        )
        edge_ids.add(edge_id)

    if not projected_edges:
        return graph
    layout_hints = dict(graph.layout_hints)
    layout_hints["distance_by_node"] = distances
    return KnowledgeMapV2FocusResponse(
        graph_revision=graph.graph_revision,
        focus=nodes_by_key[graph.focus.id],
        nodes=sorted(nodes_by_key.values(), key=lambda node: (node.distance, node.name.casefold(), node.id)),
        edges=sorted([*graph.edges, *projected_edges], key=lambda edge: str(edge.id)),
        layout_hints=layout_hints,
        truncated=truncated,
        omitted_counts=omitted_counts,
        continuation=graph.continuation,
        applied_filters=graph.applied_filters,
    )


async def _v2_system_prerequisite_edges(
    db: AsyncSession,
    *,
    graph: KnowledgeMapV2FocusResponse,
    focus_content: ResolvedKnowledgeContent,
    depth: int,
    content_types: set[str],
    relation_types: set[str],
    viewer_id=None,
) -> KnowledgeMapV2FocusResponse:
    """Append read-only, authoritative prerequisite edges around one focus.

    The access-control source remains the prerequisite tables.  These edges
    are derived for the response only and never use ``KnowledgeRelation`` or
    write ``knowledge_relations``.  Every endpoint is resolved again through
    the public-content rules before it can be returned.
    """
    if depth < 1 or "requires" not in relation_types:
        return graph

    # 시스템 edge를 만드는 모든 반환 경로에서 같은 배치 힌트 사본을 사용한다.
    # 분기 순서가 바뀌어도 정의되지 않은 layout_hints를 참조하지 않고, 기존
    # 공개 관계 그래프의 배치 계약을 그대로 보존한다.
    layout_hints = dict(graph.layout_hints)
    candidates: list[tuple[str, str, str, str, str, object | None]] = []
    focus_type = focus_content.entity_type
    focus_id = focus_content.entity_id

    if focus_type == "challenge":
        challenge_id = int(focus_id)
        challenge_rows = (
            await db.execute(
                select(
                    ChallengePrerequisite.challenge_id,
                    ChallengePrerequisite.prerequisite_id,
                ).where(
                    (ChallengePrerequisite.challenge_id == challenge_id)
                    | (ChallengePrerequisite.prerequisite_id == challenge_id)
                )
            )
        ).all()
        for target_id, source_id in challenge_rows:
            candidates.append(
                (
                    "challenge",
                    str(source_id),
                    "challenge",
                    str(target_id),
                    "문제 선행조건",
                    None,
                )
            )

        learning_rows = (
            await db.execute(
                select(ChallengeLearningPrerequisite).where(
                    ChallengeLearningPrerequisite.challenge_id == challenge_id
                )
            )
        ).scalars().all()
        for prerequisite in learning_rows:
            candidates.append(
                (
                    prerequisite.prerequisite_type,
                    str(prerequisite.prerequisite_target_id),
                    "challenge",
                    focus_id,
                    "학습 선행조건",
                    prerequisite.created_at,
                )
            )

    elif focus_type == "course":
        course_id = UUID(focus_id)
        course_rows = (
            await db.execute(
                select(
                    CoursePrerequisite.course_id,
                    CoursePrerequisite.prerequisite_id,
                ).where(
                    (CoursePrerequisite.course_id == course_id)
                    | (CoursePrerequisite.prerequisite_id == course_id)
                )
            )
        ).all()
        for target_id, source_id in course_rows:
            candidates.append(
                (
                    "course",
                    str(source_id),
                    "course",
                    str(target_id),
                    "코스 선행조건",
                    None,
                )
            )

        learning_rows = (
            await db.execute(
                select(ChallengeLearningPrerequisite).where(
                    ChallengeLearningPrerequisite.prerequisite_type == "course",
                    ChallengeLearningPrerequisite.prerequisite_target_id == course_id,
                )
            )
        ).scalars().all()
        for prerequisite in learning_rows:
            candidates.append(
                (
                    "course",
                    focus_id,
                    "challenge",
                    str(prerequisite.challenge_id),
                    "학습 선행조건",
                    prerequisite.created_at,
                )
            )

    elif focus_type == "lesson":
        lesson_id = UUID(focus_id)
        learning_rows = (
            await db.execute(
                select(ChallengeLearningPrerequisite).where(
                    ChallengeLearningPrerequisite.prerequisite_type == "lesson",
                    ChallengeLearningPrerequisite.prerequisite_target_id == lesson_id,
                )
            )
        ).scalars().all()
        for prerequisite in learning_rows:
            candidates.append(
                (
                    "lesson",
                    focus_id,
                    "challenge",
                    str(prerequisite.challenge_id),
                    "학습 선행조건",
                    prerequisite.created_at,
                )
            )

    if not candidates:
        return graph

    nodes_by_key = {node.id: node for node in graph.nodes}
    edge_ids = {edge.id for edge in graph.edges}
    system_edge_keys: set[tuple[str, str, str, str]] = set()
    system_edges: list[KnowledgeMapV2Edge] = []
    truncated = graph.truncated
    omitted_counts = dict(graph.omitted_counts)
    content_cache: dict[tuple[str, str], ResolvedKnowledgeContent | None] = {}

    async def public_content(entity_type: str, entity_id: str) -> ResolvedKnowledgeContent | None:
        key = (entity_type, entity_id)
        if key not in content_cache:
            content_cache[key] = await resolve_content(
                db,
                entity_type,
                entity_id,
                public_only=True,
                viewer_id=viewer_id,
            )
        return content_cache[key]

    for source_type, source_id, target_type, target_id, evidence_summary, created_at in candidates:
        if source_type not in content_types or target_type not in content_types:
            continue
        key = (source_type, source_id, target_type, target_id)
        if key in system_edge_keys:
            continue
        source = await public_content(source_type, source_id)
        target = await public_content(target_type, target_id)
        if not source or not target:
            continue
        source_key = _v2_content_key(source)
        target_key = _v2_content_key(target)
        edge_id = system_prerequisite_edge_id(
            source_type=source.entity_type,
            source_id=source.entity_id,
            target_type=target.entity_type,
            target_id=target.entity_id,
        )
        if edge_id in edge_ids:
            continue
        unseen_keys = {
            key for key in (source_key, target_key) if key not in nodes_by_key
        }
        if len(nodes_by_key) + len(unseen_keys) > _V2_MAX_NODES:
            truncated = True
            omitted_counts["node_limit"] = omitted_counts.get("node_limit", 0) + len(unseen_keys)
            continue
        if len(graph.edges) + len(system_edges) >= _V2_MAX_EDGES:
            truncated = True
            omitted_counts["edge_limit"] = omitted_counts.get("edge_limit", 0) + 1
            continue
        if source_key not in nodes_by_key:
            nodes_by_key[source_key] = _v2_node(source, distance=1)
        if target_key not in nodes_by_key:
            nodes_by_key[target_key] = _v2_node(target, distance=1)
        system_edges.append(
            KnowledgeMapV2Edge(
                id=edge_id,
                source=source_key,
                target=target_key,
                relation_type="requires",
                is_directed=True,
                strength=1.0,
                confidence=1.0,
                evidence_summary=evidence_summary,
                origin="system_prerequisite",
                created_at=created_at,
                approved_at=None,
            )
        )
        edge_ids.add(edge_id)
        system_edge_keys.add(key)

    if not system_edges:
        return graph
    distance_by_node = dict(layout_hints.get("distance_by_node", {}))
    for node_key, node in nodes_by_key.items():
        existing_distance = distance_by_node.get(node_key)
        if existing_distance is None or node.distance < existing_distance:
            distance_by_node[node_key] = node.distance
    layout_hints["distance_by_node"] = distance_by_node
    return KnowledgeMapV2FocusResponse(
        graph_revision=graph.graph_revision,
        focus=graph.focus,
        nodes=sorted(
            nodes_by_key.values(),
            key=lambda node: (node.distance, node.name.casefold(), node.id),
        ),
        edges=sorted(
            [*graph.edges, *system_edges],
            key=lambda edge: str(edge.id),
        ),
        layout_hints=layout_hints,
        truncated=truncated,
        omitted_counts=omitted_counts,
        continuation=graph.continuation,
        applied_filters=graph.applied_filters,
    )


async def _v2_visible_relation_counts(
    db: AsyncSession,
    viewer_id=None,
) -> tuple[dict[str, int], int]:
    """개요는 공개 가능한 양 끝점이 확인된 승인 관계만 집계한다."""
    relations = (
        await db.execute(select(KnowledgeRelation).where(KnowledgeRelation.status == "approved"))
    ).scalars().all()
    if not relations:
        return {}, 0
    node_ids = {
        node_id
        for relation in relations
        for node_id in (relation.source_node_id, relation.target_node_id)
    }
    node_result = await db.execute(select(KnowledgeContentNode).where(KnowledgeContentNode.id.in_(node_ids)))
    nodes_by_id = {node.id: node for node in node_result.scalars().all()}
    cache: dict = {}
    counts: dict[str, int] = defaultdict(int)
    visible_count = 0
    for relation in relations:
        source_node = nodes_by_id.get(relation.source_node_id)
        target_node = nodes_by_id.get(relation.target_node_id)
        if not source_node or not target_node:
            continue
        source = await _v2_resolve_registry_content(db, source_node, cache, viewer_id)
        target = await _v2_resolve_registry_content(db, target_node, cache, viewer_id)
        if not source or not target:
            continue
        counts[relation.relation_type] += 1
        visible_count += 1
    return dict(sorted(counts.items())), visible_count


async def _v2_public_projection_counts(db: AsyncSession) -> tuple[int, int, int]:
    """Count public, source-of-truth projection edges for the V2 overview."""
    public_tags = public_knowledge_tag_ids_query()
    challenge_tags = int(
        (
            await db.execute(
                select(func.count())
                .select_from(ChallengeKnowledgeTag)
                .join(Challenge, Challenge.id == ChallengeKnowledgeTag.challenge_id)
                .where(
                    Challenge.is_published.is_(True),
                    Challenge.challenge_type == "wargame",
                    ChallengeKnowledgeTag.tag_id.in_(public_tags),
                    public_tag_mapping_clause(ChallengeKnowledgeTag),
                )
            )
        ).scalar()
        or 0
    )
    course_tags = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CourseKnowledgeTag)
                .join(Course, Course.id == CourseKnowledgeTag.course_id)
                .where(
                    Course.is_deleted.is_(False),
                    Course.is_published.is_(True),
                    Course.status == ContentStatus.APPROVED.value,
                    CourseKnowledgeTag.tag_id.in_(public_tags),
                    public_tag_mapping_clause(CourseKnowledgeTag),
                )
            )
        ).scalar()
        or 0
    )
    lesson_tags = int(
        (
            await db.execute(
                select(func.count())
                .select_from(LessonKnowledgeTag)
                .join(Lesson, Lesson.id == LessonKnowledgeTag.lesson_id)
                .join(Course, Course.id == Lesson.course_id)
                .where(
                    Course.is_deleted.is_(False),
                    Course.is_published.is_(True),
                    Course.status == ContentStatus.APPROVED.value,
                    LessonKnowledgeTag.tag_id.in_(public_tags),
                    public_tag_mapping_clause(LessonKnowledgeTag),
                )
            )
        ).scalar()
        or 0
    )
    structure_edges = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Lesson)
                .join(Course, Course.id == Lesson.course_id)
                .where(
                    Course.is_deleted.is_(False),
                    Course.is_published.is_(True),
                    Course.status == ContentStatus.APPROVED.value,
                )
            )
        ).scalar()
        or 0
    )
    tag_hierarchy_edges = int(
        (
            await db.execute(
                select(func.count())
                .select_from(KnowledgeTag)
                .where(
                    KnowledgeTag.id.in_(public_tags),
                    KnowledgeTag.parent_id.in_(public_tags),
                )
            )
        ).scalar()
        or 0
    )
    return challenge_tags + course_tags + lesson_tags, structure_edges, tag_hierarchy_edges


async def _v2_default_focus(db: AsyncSession) -> KnowledgeMapV2Node | None:
    """Choose a deterministic public seed for the initial map view."""
    public_tag_ids = public_knowledge_tag_ids_query()
    connected_course = or_(
        select(Lesson.id)
        .where(Lesson.course_id == Course.id)
        .exists(),
        select(CourseKnowledgeTag.course_id)
            .where(
                CourseKnowledgeTag.course_id == Course.id,
                CourseKnowledgeTag.tag_id.in_(public_tag_ids),
                public_tag_mapping_clause(CourseKnowledgeTag),
            )
        .exists(),
    )
    course = (
        await db.execute(
            select(Course)
            .where(
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
                connected_course,
            )
            .order_by(Course.title, Course.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if course:
        content = await resolve_content(db, "course", str(course.id), public_only=True)
        if content:
            return _v2_node(content, distance=0, is_focus=True)

    if not course:
        course = (
            await db.execute(
                select(Course)
                .where(
                    Course.is_deleted.is_(False),
                    Course.is_published.is_(True),
                    Course.status == ContentStatus.APPROVED.value,
                )
                .order_by(Course.title, Course.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if course:
            content = await resolve_content(db, "course", str(course.id), public_only=True)
            if content:
                return _v2_node(content, distance=0, is_focus=True)

    challenge = (
        await db.execute(
            select(Challenge)
            .where(
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
                or_(
                    select(ChallengeKnowledgeTag.challenge_id)
                    .where(
                        ChallengeKnowledgeTag.challenge_id == Challenge.id,
                        ChallengeKnowledgeTag.tag_id.in_(public_tag_ids),
                        public_tag_mapping_clause(ChallengeKnowledgeTag),
                    )
                    .exists(),
                    select(Hint.id)
                    .where(Hint.challenge_id == Challenge.id)
                    .exists(),
                ),
            )
            .order_by(Challenge.name, Challenge.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if challenge:
        content = await resolve_content(db, "challenge", str(challenge.id), public_only=True)
        if content:
            return _v2_node(content, distance=0, is_focus=True)

    if not challenge:
        challenge = (
            await db.execute(
                select(Challenge)
                .where(
                    Challenge.is_published.is_(True),
                    Challenge.challenge_type == "wargame",
                )
                .order_by(Challenge.name, Challenge.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if challenge:
            content = await resolve_content(db, "challenge", str(challenge.id), public_only=True)
            if content:
                return _v2_node(content, distance=0, is_focus=True)

    tag = (
        await db.execute(
            select(KnowledgeTag)
            .where(KnowledgeTag.id.in_(public_knowledge_tag_ids_query()))
            .order_by(KnowledgeTag.name, KnowledgeTag.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if tag:
        content = await resolve_content(db, "knowledge_tag", str(tag.id), public_only=True)
        if content:
            return _v2_node(content, distance=0, is_focus=True)
    return None


async def _v2_mapping_coverage(db: AsyncSession) -> KnowledgeMapV2MappingCoverage:
    """공개 맵의 태그 연결 품질을 같은 공개 범위로 집계한다.

    이 지표는 그래프에 태그 노드가 몇 개 보이는지만 세지 않고, 공개 콘텐츠 중
    실제 junction 매핑이 있는 콘텐츠와 아직 분류되지 않은 콘텐츠를 함께 보여 준다.
    private-only 콘텐츠와 태그의 존재 여부는 공개 응답에서 역산할 수 없도록 각
    집계가 공개 콘텐츠 조건과 공개 태그 쿼리를 동시에 사용한다.
    """
    public_tag_ids = public_knowledge_tag_ids_query()
    definitions = (
        (
            "challenge",
            Challenge,
            (
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            ),
            ChallengeKnowledgeTag,
            ChallengeKnowledgeTag.challenge_id == Challenge.id,
        ),
        (
            "course",
            Course,
            (
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            ),
            CourseKnowledgeTag,
            CourseKnowledgeTag.course_id == Course.id,
        ),
        (
            "lesson",
            Lesson,
            (
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            ),
            LessonKnowledgeTag,
            LessonKnowledgeTag.lesson_id == Lesson.id,
        ),
    )
    public_content: dict[str, int] = {}
    tagged_content: dict[str, int] = {}
    unmapped_content: dict[str, int] = {}
    coverage_percent: dict[str, float] = {}

    for entity_type, model, conditions, junction, join_condition in definitions:
        public_query = select(func.count()).select_from(model)
        tagged_query = select(func.count()).select_from(model)
        if entity_type == "lesson":
            public_query = public_query.join(Course, Course.id == Lesson.course_id)
            tagged_query = tagged_query.join(Course, Course.id == Lesson.course_id)
        public_query = public_query.where(*conditions)
        tagged_exists = (
            select(junction.tag_id)
            .where(
                join_condition,
                junction.tag_id.in_(public_tag_ids),
                public_tag_mapping_clause(junction),
            )
            .exists()
        )
        tagged_query = tagged_query.where(*conditions, tagged_exists)
        total = int((await db.execute(public_query)).scalar() or 0)
        tagged = int((await db.execute(tagged_query)).scalar() or 0)
        public_content[entity_type] = total
        tagged_content[entity_type] = tagged
        unmapped_content[entity_type] = max(total - tagged, 0)
        coverage_percent[entity_type] = round((tagged / total) * 100, 1) if total else 0.0

    return KnowledgeMapV2MappingCoverage(
        public_content=public_content,
        tagged_content=tagged_content,
        unmapped_content=unmapped_content,
        coverage_percent=coverage_percent,
    )


_CANVAS_RELATION_TYPE_MAP = {
    "challenge_prerequisite": "requires",
    "learning_prerequisite": "requires",
    "course_prerequisite": "requires",
    "course_lesson": "contains",
    "content_tag": "tag_affinity",
    "hint_context": "helps",
    "tag_related": "related",
}


def _canvas_node_key(node: GraphNode) -> str | None:
    if not node.entity_id or node.type not in PUBLIC_CONTENT_TYPES:
        return None
    return f"{node.type}:{node.entity_id}"


def _canvas_edge_id(source: str, target: str, relation_type: str, origin: str | None) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"ctf-web:knowledge-map-canvas-v2:{source}:{target}:{relation_type}:{origin or 'legacy'}",
    )


async def _v2_build_canvas(
    db: AsyncSession, *, viewer_id=None, content_types: set[str], relation_types: set[str],
    cursor_context: dict | None = None,
) -> KnowledgeMapV2CanvasResponse:
    revision = await _v2_graph_revision(db)
    context = _graph_page_context("canvas", revision, viewer_id, content_types, relation_types)
    after = _validated_graph_cursor(cursor_context, context)
    nodes, edges, next_cursor = await graph_page(db, context=context, after=after)
    if await _v2_graph_revision(db) != revision:
        raise HTTPException(409, "지식 그래프가 변경되었습니다. 첫 구간을 다시 조회하세요.")
    return KnowledgeMapV2CanvasResponse(
        graph_revision=revision, nodes=nodes, edges=edges, next_cursor=next_cursor,
        truncated=next_cursor is not None,
        layout_hints={"strategy": "seeded_force_v1", "seed": f"{revision}:canvas",
                      "distance_by_node": {node.id: node.distance for node in nodes},
                      "strength_distance_rule": "stronger_relation_shorter", "direction_visible": True},
        applied_filters={"types": sorted(content_types), "relation_types": sorted(relation_types), "mode": "canvas"},
    )


def _graph_page_context(mode, revision, viewer_id, content_types, relation_types, focus_type=None, focus_id=None, depth=1):
    return {"mode": mode, "revision": revision, "viewer": str(viewer_id) if viewer_id else None,
            "focus_type": focus_type, "focus_id": focus_id, "depth": depth,
            "types": sorted(content_types), "relation_types": sorted(relation_types)}


def _validated_graph_cursor(payload, context):
    if payload is None:
        return ""
    if any(payload.get(key) != value for key, value in context.items()):
        raise HTTPException(409, "지식 그래프 또는 조회 권한이 변경되었습니다. 첫 구간을 다시 조회하세요.")
    return payload["after"]


@router.get("/knowledge-map/v2/canvas", response_model=KnowledgeMapV2CanvasResponse)
async def get_knowledge_map_v2_canvas(
    types: list[str] | None = Query(None),
    relation_types: list[str] | None = Query(None),
    cursor: str | None = Query(None, max_length=8192),
    db: AsyncSession = Depends(get_db),
    viewer_id=Depends(get_optional_user_id),
):
    """전체 공개 그래프를 Obsidian형 자유 캔버스에 제공한다."""
    cursor_context = None
    if cursor:
        if types is not None or relation_types is not None:
            raise HTTPException(422, "cursor와 필터는 함께 지정할 수 없습니다.")
        cursor_context = decode_graph_cursor(cursor)
        if cursor_context["mode"] != "canvas":
            raise HTTPException(422, "canvas cursor가 필요합니다.")
        types, relation_types = cursor_context["types"], cursor_context["relation_types"]
    content_types = _parse_v2_filter_values(
        types,
        allowed=set(PUBLIC_CONTENT_TYPES),
        label="content type",
        default=_V2_DEFAULT_CONTENT_TYPES,
    )
    selected_relations = _parse_v2_filter_values(
        relation_types,
        allowed=_V2_RELATION_TYPES,
        label="relation type",
        default=_V2_DEFAULT_RELATION_TYPES,
    )
    return await _v2_build_canvas(
        db,
        viewer_id=viewer_id,
        content_types=content_types,
        relation_types=selected_relations,
        cursor_context=cursor_context,
    )


@router.get("/knowledge-map/v2/overview", response_model=KnowledgeMapV2OverviewResponse)
async def get_knowledge_map_v2_overview(
    db: AsyncSession = Depends(get_db),
    viewer_id=Depends(get_optional_user_id),
):
    """공개 SQL projection을 DB에서 집계하여 원시 관계를 전부 적재하지 않는다."""
    revision = await _v2_graph_revision(db)
    nodes = public_nodes_query(viewer_id)
    edges = public_edges_query(nodes, PUBLIC_CONTENT_TYPES, _V2_RELATION_TYPES)
    counts = dict((await db.execute(select(nodes.c.type, func.count()).group_by(nodes.c.type))).all())
    edge_counts = dict((await db.execute(select(edges.c.relation_type, func.count()).group_by(edges.c.relation_type))).all())
    default_focus = await _v2_default_focus(db)
    coverage = await _v2_mapping_coverage(db)
    if await _v2_graph_revision(db) != revision:
        raise HTTPException(409, "지식 그래프가 변경되었습니다. 다시 조회하세요.")
    return KnowledgeMapV2OverviewResponse(
        graph_revision=revision, node_counts={kind: int(counts.get(kind, 0)) for kind in sorted(PUBLIC_CONTENT_TYPES)},
        edge_counts={kind: int(value) for kind, value in edge_counts.items()}, relation_count=sum(edge_counts.values()),
        default_focus=default_focus, mapping_coverage=coverage,
    )


@router.get("/knowledge-map/v2/focus", response_model=KnowledgeMapV2FocusResponse)
async def get_knowledge_map_v2_focus(
    focus_type: str | None = Query(None),
    focus_id: str | None = Query(None),
    depth: int = Query(1, ge=0, le=_V2_MAX_DEPTH),
    types: list[str] | None = Query(None),
    relation_types: list[str] | None = Query(None),
    continuation: str | None = Query(None, max_length=600),
    cursor: str | None = Query(None, max_length=8192),
    db: AsyncSession = Depends(get_db),
    viewer_id=Depends(get_optional_user_id),
):
    """선택 콘텐츠 중심의 권한 필터된 bounded 그래프를 서버에서 생성한다."""
    cursor_context = None
    if cursor:
        if continuation or focus_type or focus_id or types is not None or relation_types is not None:
            raise HTTPException(422, "cursor는 중심·필터·continuation과 함께 지정할 수 없습니다.")
        cursor_context = decode_graph_cursor(cursor)
        if cursor_context["mode"] != "focus":
            raise HTTPException(422, "focus cursor가 필요합니다.")
        focus_type, focus_id, depth = cursor_context["focus_type"], cursor_context["focus_id"], cursor_context["depth"]
        types, relation_types = cursor_context["types"], cursor_context["relation_types"]
    if continuation:
        if focus_type or focus_id or types or relation_types:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "continuation 사용 시 중심과 필터를 함께 지정할 수 없습니다.",
            )
        try:
            decoded = decode_focus_continuation(continuation)
        except KnowledgeMappingValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        focus_type = decoded["focus_type"]
        focus_id = decoded["focus_id"]
        depth = decoded["depth"]
        types = decoded["types"]
        relation_types = decoded["relation_types"]

    if bool(focus_type) != bool(focus_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "focus_type과 focus_id는 함께 지정해야 합니다.")
    if not focus_type or not focus_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "V2 포커스 맵에는 중심 콘텐츠가 필요합니다.")
    try:
        normalized_id = normalize_content_identifier(focus_type, focus_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "그래프 중심 콘텐츠 형식이 올바르지 않습니다.") from exc
    content_types = _parse_v2_filter_values(
        types,
        allowed=set(PUBLIC_CONTENT_TYPES),
        label="콘텐츠 유형",
        default=_V2_DEFAULT_CONTENT_TYPES,
    )
    selected_relation_types = _parse_v2_filter_values(
        relation_types,
        allowed=_V2_RELATION_TYPES,
        label="관계 유형",
        default=_V2_DEFAULT_RELATION_TYPES,
    )
    # 콘텐츠 중심 지도는 실제 매핑·학습 관계만 먼저 보여준다. 태그를
    # 중심으로 열었을 때만 parent_id taxonomy를 추가해, 공개 콘텐츠 맵에
    # 분류 계층이 무분별하게 들어와 캔버스를 채우지 않게 한다.
    if relation_types is None and focus_type == "knowledge_tag":
        selected_relation_types.add("tag_parent")
    # 중심 노드는 항상 맵에 남아야 한다. 호출자가 중심과 다른 유형만
    # 선택해도 중심을 제거해 422를 내지 않고, 탐색 대상에 중심 유형을
    # 안전하게 보완한다.
    content_types.add(focus_type)
    focus_content = await resolve_content(
        db,
        focus_type,
        normalized_id,
        public_only=True,
        viewer_id=viewer_id,
    )
    if not focus_content:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "공개 가능한 중심 콘텐츠를 찾을 수 없습니다.")
    focus_registry_node = await _v2_registry_node(
        db,
        entity_type=focus_content.entity_type,
        entity_id=focus_content.entity_id,
    )
    graph_revision = await _v2_graph_revision(db)
    context = _graph_page_context("focus", graph_revision, viewer_id, content_types, selected_relation_types,
                                  focus_type, normalized_id, depth)
    after = _validated_graph_cursor(cursor_context, context)
    nodes, edges, next_cursor = await graph_page(db, context=context, after=after)
    if await _v2_graph_revision(db) != graph_revision:
        raise HTTPException(409, "지식 그래프가 변경되었습니다. 첫 구간을 다시 조회하세요.")
    focus_node = next((node for node in nodes if node.is_focus), _v2_node(focus_content, distance=0, is_focus=True))
    continuation_token = encode_focus_continuation(
        focus_type=focus_type, focus_id=normalized_id, depth=depth + 1,
        types=sorted(content_types), relation_types=sorted(selected_relation_types),
    ) if depth < _V2_MAX_DEPTH else None
    return KnowledgeMapV2FocusResponse(
        graph_revision=graph_revision, focus=focus_node, nodes=nodes, edges=edges,
        next_cursor=next_cursor, continuation=continuation_token, truncated=next_cursor is not None,
        layout_hints={"strategy": "seeded_force_v1", "seed": f"{graph_revision}:{focus_node.id}",
                      "distance_by_node": {node.id: node.distance for node in nodes},
                      "strength_distance_rule": "stronger_relation_shorter", "direction_visible": True},
        applied_filters={"types": sorted(content_types), "relation_types": sorted(selected_relation_types), "depth": depth},
    )


async def _v2_search_contents(
    db: AsyncSession,
    *,
    query: str,
    content_types: set[str],
    limit: int,
    viewer_id=None,
) -> list[KnowledgeMapV2SearchResult]:
    """원본 테이블에서 공개 가능한 제목/태그만 검색한다.

    registry 동기화가 아직 되지 않은 공개 콘텐츠도 검색할 수 있도록 검색은
    ``knowledge_content_nodes``에만 의존하지 않는다. 힌트 본문은 검색하지 않는다.
    """
    pattern = f"%{_escape_like_query(query)}%"
    items: list[KnowledgeMapV2SearchResult] = []
    if "challenge" in content_types:
        challenges = (
            await db.execute(
                select(Challenge)
                .where(
                    Challenge.is_published.is_(True),
                    Challenge.challenge_type == "wargame",
                    Challenge.name.ilike(pattern, escape="\\"),
                )
                .limit(limit)
            )
        ).scalars().all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="challenge",
                entity_id=str(challenge.id),
                id=str(challenge.id),
                name=challenge.name,
                href=f"/wargame/{challenge.id}",
            )
            for challenge in challenges
        )
    if "course" in content_types:
        courses = (
            await db.execute(
                select(Course)
                .where(
                    Course.is_deleted.is_(False),
                    Course.is_published.is_(True),
                    Course.status == ContentStatus.APPROVED.value,
                    Course.title.ilike(pattern, escape="\\"),
                )
                .limit(limit)
            )
        ).scalars().all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="course",
                entity_id=str(course.id),
                id=str(course.id),
                name=course.title,
                href=f"/learn/{course.id}",
            )
            for course in courses
        )
    if "lesson" in content_types:
        lessons = (
            await db.execute(
                select(Lesson, Course)
                .join(Course, Course.id == Lesson.course_id)
                .where(
                    Course.is_deleted.is_(False),
                    Course.is_published.is_(True),
                    Course.status == ContentStatus.APPROVED.value,
                    Lesson.title.ilike(pattern, escape="\\"),
                )
                .limit(limit)
            )
        ).all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="lesson",
                entity_id=str(lesson.id),
                id=str(lesson.id),
                name=lesson.title,
                href=f"/learn/{course.id}/{lesson.id}",
            )
            for lesson, course in lessons
        )
    if "knowledge_tag" in content_types:
        tags = (
            await db.execute(
                select(KnowledgeTag)
                .where(
                    KnowledgeTag.id.in_(public_knowledge_tag_ids_query()),
                    or_(
                        KnowledgeTag.name.ilike(pattern, escape="\\"),
                        KnowledgeTag.slug.ilike(pattern, escape="\\"),
                    )
                )
                .limit(limit)
            )
        ).scalars().all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="knowledge_tag",
                entity_id=str(tag.id),
                id=str(tag.id),
                name=tag.name,
                href=f"/learn?view=map&focus_type=knowledge_tag&focus_id={tag.id}",
            )
            for tag in tags
        )
    if "hint" in content_types:
        hints = (
            await db.execute(
                select(Hint, Challenge)
                .join(Challenge, Challenge.id == Hint.challenge_id)
                .where(
                    Challenge.is_published.is_(True),
                    Challenge.challenge_type == "wargame",
                    Challenge.name.ilike(pattern, escape="\\"),
                )
                .limit(limit)
            )
        ).all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="hint",
                entity_id=str(hint.id),
                id=str(hint.id),
                name=f"{challenge.name} · 힌트 {hint.order_index + 1}",
                href=f"/wargame/{challenge.id}",
            )
            for hint, challenge in hints
        )
    if "event" in content_types:
        now = datetime.now(timezone.utc)
        events = (
            await db.execute(
                select(PlatformEvent)
                .where(
                    PlatformEvent.status == "published",
                    PlatformEvent.visible_from_at <= now,
                    or_(
                        PlatformEvent.visible_until_at.is_(None),
                        PlatformEvent.visible_until_at > now,
                    ),
                    PlatformEvent.title.ilike(pattern, escape="\\"),
                )
                .limit(limit)
            )
        ).scalars().all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="event",
                entity_id=str(event.id),
                id=str(event.id),
                name=event.title,
                href=f"/events/{event.id}",
            )
            for event in events
        )
    if "assignment" in content_types and viewer_id:
        assignment_access = or_(
            TeachingClass.instructor_id == viewer_id,
            select(TeachingClassEnrollment.id)
            .where(
                TeachingClassEnrollment.class_id == TeachingClass.id,
                TeachingClassEnrollment.student_id == viewer_id,
                TeachingClassEnrollment.status == "active",
            )
            .exists(),
            select(EducationRoleGrant.id)
            .where(
                EducationRoleGrant.subject_id == viewer_id,
                EducationRoleGrant.role == "professor_admin",
                EducationRoleGrant.revoked_at.is_(None),
            )
            .exists(),
        )
        assignments = (
            await db.execute(
                select(EducationAssignment, TeachingClass)
                .join(TeachingClass, TeachingClass.id == EducationAssignment.class_id)
                .where(
                    TeachingClass.status == "published",
                    EducationAssignment.status == "published",
                    assignment_access,
                    EducationAssignment.title.ilike(pattern, escape="\\"),
                )
                .limit(limit)
            )
        ).all()
        items.extend(
            KnowledgeMapV2SearchResult(
                type="assignment",
                entity_id=str(assignment.id),
                id=str(assignment.id),
                name=assignment.title,
                href=f"/learn/classes/{teaching_class.id}?assignment={assignment.id}",
            )
            for assignment, teaching_class in assignments
        )
    unique_items = {(item.type, item.id): item for item in items}
    return sorted(unique_items.values(), key=lambda item: (item.name.casefold(), item.type, item.id))[:limit]


@router.get("/knowledge-map/v2/search", response_model=KnowledgeMapV2SearchResponse)
async def search_knowledge_map_v2(
    q: str = Query(..., min_length=1, max_length=100),
    types: list[str] | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    viewer_id=Depends(get_optional_user_id),
):
    """힌트 본문·비공개 제목을 검색하지 않는 V2 그래프 진입점 검색."""
    query = q.strip()
    if not query:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "검색어는 공백만으로 구성할 수 없습니다.")
    content_types = _parse_v2_filter_values(
        types,
        allowed=set(PUBLIC_CONTENT_TYPES),
        label="콘텐츠 유형",
        default=_V2_DEFAULT_CONTENT_TYPES,
    )
    items = await _v2_search_contents(
        db,
        query=query,
        content_types=content_types,
        limit=limit,
        viewer_id=viewer_id,
    )
    return KnowledgeMapV2SearchResponse(query=query, items=items, total=len(items), limit=limit)
