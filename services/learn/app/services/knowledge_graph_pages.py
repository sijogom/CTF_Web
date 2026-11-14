"""공개 SQL projection과 edge keyset pagination. 원문·개인 이력은 읽지 않는다."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import String, and_, case, cast, exists, func, literal, or_, select, union_all
from sqlalchemy.orm import aliased

from app.models.challenge import Challenge, ChallengePrerequisite, Hint
from app.models.course import ContentStatus, Course, CoursePrerequisite, Lesson
from app.models.education import EducationAssignment, EducationRoleGrant, TeachingClass, TeachingClassEnrollment
from app.models.event import PlatformEvent
from app.models.knowledge import (ChallengeKnowledgeTag, ChallengeLearningPrerequisite, CourseKnowledgeTag,
                                 KnowledgeContentNode, KnowledgeRelation, KnowledgeTag, LessonKnowledgeTag)
from app.schemas.knowledge import KnowledgeMapV2Edge, KnowledgeMapV2Node
from app.utils.knowledge_mapping_v2 import public_evidence_summary
from app.utils.knowledge_relations import public_knowledge_tag_ids_query, public_tag_mapping_clause

PAGE_NODE_LIMIT = 120
PAGE_EDGE_LIMIT = 240


def _key(kind, identifier):
    return literal(kind + ":") + cast(identifier, String)


def public_nodes_query(viewer_id=None):
    now = datetime.now(timezone.utc)
    course_public = and_(Course.is_deleted.is_(False), Course.is_published.is_(True), Course.status == ContentStatus.APPROVED.value)
    challenge_public = and_(Challenge.is_published.is_(True), Challenge.challenge_type == "wargame")
    def node(kind, identifier, name, href):
        return select(_key(kind, identifier).label("key"), literal(kind).label("type"),
                      cast(identifier, String).label("entity_id"), name.label("name"), href.label("href"))
    queries = [
        node("challenge", Challenge.id, Challenge.name, literal("/wargame/") + cast(Challenge.id, String)).where(challenge_public),
        node("course", Course.id, Course.title, literal("/learn/") + cast(Course.id, String)).where(course_public),
        node("lesson", Lesson.id, Lesson.title, literal("/learn/") + cast(Course.id, String) + literal("/") + cast(Lesson.id, String))
            .select_from(Lesson).join(Course, Course.id == Lesson.course_id).where(course_public),
        node("knowledge_tag", KnowledgeTag.id, KnowledgeTag.name,
             literal("/learn?view=map&focus_type=knowledge_tag&focus_id=") + cast(KnowledgeTag.id, String))
            .where(KnowledgeTag.id.in_(public_knowledge_tag_ids_query())),
        node("hint", Hint.id, Challenge.name + literal(" · 힌트 ") + cast(Hint.order_index + 1, String),
             literal("/wargame/") + cast(Challenge.id, String)).select_from(Hint).join(Challenge, Challenge.id == Hint.challenge_id).where(challenge_public),
        node("event", PlatformEvent.id, PlatformEvent.title, literal("/events/") + cast(PlatformEvent.id, String)).where(
            PlatformEvent.status == "published", PlatformEvent.visible_from_at <= now,
            or_(PlatformEvent.visible_until_at.is_(None), PlatformEvent.visible_until_at > now)),
    ]
    if viewer_id is not None:
        viewer_permission = or_(TeachingClass.instructor_id == viewer_id,
            exists(select(TeachingClassEnrollment.id).where(TeachingClassEnrollment.class_id == TeachingClass.id,
                TeachingClassEnrollment.student_id == viewer_id, TeachingClassEnrollment.status == "active")),
            exists(select(EducationRoleGrant.id).where(EducationRoleGrant.subject_id == viewer_id,
                EducationRoleGrant.role == "professor_admin", EducationRoleGrant.revoked_at.is_(None))))
        queries.append(node("assignment", EducationAssignment.id, EducationAssignment.title,
            literal("/learn/classes/") + cast(TeachingClass.id, String) + literal("?assignment=") + cast(EducationAssignment.id, String))
            .select_from(EducationAssignment).join(TeachingClass, TeachingClass.id == EducationAssignment.class_id)
            .where(EducationAssignment.status == "published", TeachingClass.status == "published", viewer_permission))
    return union_all(*queries).cte("page_public_nodes")


def public_edges_query(nodes, content_types, relation_types):
    raw = []
    def edge(key, source, target, kind, directed=True, strength=1.0, confidence=1.0,
             evidence=None, origin="legacy_projection", created=None, approved=None):
        def expr(value):
            return value if hasattr(value, "label") else literal(value)
        return select(*[expr(value).label(label) for label, value in (
            ("edge_key", key), ("source", source), ("target", target), ("relation_type", kind),
            ("is_directed", directed), ("strength", strength), ("confidence", confidence),
            ("evidence_summary", evidence), ("origin", origin), ("created_at", created), ("approved_at", approved))])
    source_registry, target_registry = aliased(KnowledgeContentNode), aliased(KnowledgeContentNode)
    raw.append(edge(literal("relation|") + cast(KnowledgeRelation.id, String),
        source_registry.entity_type + literal(":") + source_registry.entity_id,
        target_registry.entity_type + literal(":") + target_registry.entity_id,
        KnowledgeRelation.relation_type, KnowledgeRelation.is_directed, KnowledgeRelation.strength,
        KnowledgeRelation.confidence, KnowledgeRelation.evidence_summary, KnowledgeRelation.origin,
        KnowledgeRelation.created_at, KnowledgeRelation.approved_at)
        .select_from(KnowledgeRelation).join(source_registry, source_registry.id == KnowledgeRelation.source_node_id)
        .join(target_registry, target_registry.id == KnowledgeRelation.target_node_id)
        .where(KnowledgeRelation.status == "approved", KnowledgeRelation.relation_type != "tag_affinity"))
    for kind, model, column in (("challenge", ChallengeKnowledgeTag, ChallengeKnowledgeTag.challenge_id),
                                ("course", CourseKnowledgeTag, CourseKnowledgeTag.course_id),
                                ("lesson", LessonKnowledgeTag, LessonKnowledgeTag.lesson_id)):
        source, target = _key(kind, column), _key("knowledge_tag", model.tag_id)
        raw.append(edge(literal("mapping|") + source + literal("|") + target, source, target, "tag_affinity", True,
            model.strength, model.confidence, model.evidence_summary, model.origin + literal("_tag_mapping"),
            model.created_at, model.approved_at).where(public_tag_mapping_clause(model)))
    raw.extend([
        edge(literal("contains|") + cast(Lesson.id, String), _key("course", Lesson.course_id), _key("lesson", Lesson.id), "contains", evidence="코스의 레슨 구성"),
        edge(literal("hint|") + cast(Hint.id, String), _key("challenge", Hint.challenge_id), _key("hint", Hint.id), "contains", evidence="문제의 힌트 구성"),
        edge(literal("tag-parent|") + cast(KnowledgeTag.id, String), _key("knowledge_tag", KnowledgeTag.id), _key("knowledge_tag", KnowledgeTag.parent_id), "tag_parent", evidence="공개 지식 태그 분류 체계").where(KnowledgeTag.parent_id.is_not(None)),
        edge(literal("challenge-prereq|") + cast(ChallengePrerequisite.prerequisite_id, String) + literal("|") + cast(ChallengePrerequisite.challenge_id, String),
            _key("challenge", ChallengePrerequisite.prerequisite_id), _key("challenge", ChallengePrerequisite.challenge_id), "requires", evidence="문제의 필수 선행조건", origin="system_prerequisite"),
        edge(literal("course-prereq|") + cast(CoursePrerequisite.prerequisite_id, String) + literal("|") + cast(CoursePrerequisite.course_id, String),
            _key("course", CoursePrerequisite.prerequisite_id), _key("course", CoursePrerequisite.course_id), "requires", evidence="코스의 필수 선행조건", origin="system_prerequisite"),
        edge(literal("learning-prereq|") + cast(ChallengeLearningPrerequisite.challenge_id, String) + literal("|") + ChallengeLearningPrerequisite.prerequisite_type + literal("|") + cast(ChallengeLearningPrerequisite.prerequisite_target_id, String),
            ChallengeLearningPrerequisite.prerequisite_type + literal(":") + cast(ChallengeLearningPrerequisite.prerequisite_target_id, String),
            _key("challenge", ChallengeLearningPrerequisite.challenge_id), "requires", evidence="문제의 필수 학습 선행조건", origin="system_prerequisite"),
    ])
    candidates = union_all(*raw).subquery("page_edge_candidates")
    source_node, target_node = nodes.alias("page_source"), nodes.alias("page_target")
    return select(candidates).join(source_node, source_node.c.key == candidates.c.source).join(target_node, target_node.c.key == candidates.c.target).where(
        source_node.c.type.in_(content_types), target_node.c.type.in_(content_types), candidates.c.relation_type.in_(relation_types)).cte("page_public_edges")


def encode_graph_cursor(context, edge_key):
    payload = {"v": 1, **context, "after": edge_key}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()


def decode_graph_cursor(value):
    if len(value) > 8192:
        raise HTTPException(422, "그래프 cursor가 너무 깁니다.")
    try:
        payload = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError()
        if set(payload) != {"v", "mode", "revision", "viewer", "focus_type", "focus_id", "depth", "types", "relation_types", "after"}:
            raise ValueError()
        if not isinstance(payload["after"], str) or len(payload["after"]) > 256:
            raise ValueError()
        if payload["mode"] not in {"canvas", "focus"} or type(payload["depth"]) is not int or not 0 <= payload["depth"] <= 3:
            raise ValueError()
        for key in ("types", "relation_types"):
            if not isinstance(payload[key], list) or len(payload[key]) > 20 or any(not isinstance(item, str) for item in payload[key]):
                raise ValueError()
        for key in ("revision", "viewer", "focus_type", "focus_id"):
            if payload[key] is not None and not isinstance(payload[key], str):
                raise ValueError()
        return payload
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise HTTPException(422, "그래프 cursor 형식이 올바르지 않습니다.") from None


async def graph_page(db, *, context, after=""):
    nodes = public_nodes_query(UUID(context["viewer"]) if context["viewer"] else None)
    edges = public_edges_query(nodes, context["types"], context["relation_types"])
    query = select(edges)
    focus_key = f"{context['focus_type']}:{context['focus_id']}" if context["focus_type"] else None
    distances = {}
    if focus_key:
        reachable = select(literal(focus_key).label("node_key"), literal(0).label("distance")).cte("page_reachable", recursive=True)
        next_key = case((edges.c.source == reachable.c.node_key, edges.c.target), else_=edges.c.source)
        reachable = reachable.union(select(next_key, reachable.c.distance + 1).select_from(reachable)
            .join(edges, or_(edges.c.source == reachable.c.node_key, edges.c.target == reachable.c.node_key))
            .where(reachable.c.distance < context["depth"]))
        frontier = select(reachable.c.node_key).where(reachable.c.distance < context["depth"])
        query = query.where(or_(edges.c.source.in_(frontier), edges.c.target.in_(frontier)))
    rows = (await db.execute(query.where(edges.c.edge_key > after).order_by(edges.c.edge_key).limit(PAGE_EDGE_LIMIT + 1))).mappings().all()
    kept, keys = [], {focus_key} if focus_key else set()
    last_key = after
    has_more = False
    for row in rows:
        candidate_keys = keys | {row["source"], row["target"]}
        if len(candidate_keys) > PAGE_NODE_LIMIT or len(kept) >= PAGE_EDGE_LIMIT:
            has_more = True
            break
        keys = candidate_keys
        kept.append(row)
        last_key = row["edge_key"]
    public_rows = (await db.execute(select(nodes).where(nodes.c.key.in_(keys)))).mappings().all() if keys else []
    if focus_key:
        depths = (await db.execute(select(reachable.c.node_key, func.min(reachable.c.distance)).where(reachable.c.node_key.in_(keys)).group_by(reachable.c.node_key))).all()
        distances = dict(depths)
    result_nodes = [KnowledgeMapV2Node(id=row["key"], type=row["type"], entity_id=row["entity_id"],
                    name=row["name"], href=row["href"], is_focus=row["key"] == focus_key,
                    distance=min(distances.get(row["key"], 3), 3)) for row in public_rows]
    result_edges = [KnowledgeMapV2Edge(id=UUID(row["edge_key"].split("|", 1)[1]) if row["edge_key"].startswith("relation|") else uuid5(NAMESPACE_URL, "knowledge-public-page:" + row["edge_key"]),
                    source=row["source"], target=row["target"], relation_type=row["relation_type"],
                    is_directed=row["is_directed"], strength=float(row["strength"]), confidence=float(row["confidence"]),
                    evidence_summary=public_evidence_summary(row["evidence_summary"]), origin=row["origin"],
                    created_at=row["created_at"], approved_at=row["approved_at"]) for row in kept]
    return result_nodes, result_edges, encode_graph_cursor(context, last_key) if has_more else None
