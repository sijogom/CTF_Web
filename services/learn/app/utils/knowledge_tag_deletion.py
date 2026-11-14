"""태그 삭제의 영향 확인과 감사 보존. commit/rollback은 호출 API가 소유한다."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select, update

from app.models.knowledge import (
    ChallengeKnowledgeTag, CourseKnowledgeTag, KnowledgeContentNode,
    KnowledgeProjectionOutbox, KnowledgeRelation, KnowledgeRelationProposal,
    KnowledgeRelationProposalReview, KnowledgeRelationReview, KnowledgeTag,
    KnowledgeTagDeletionArchive, KnowledgeTagMappingAuditEvent, LessonKnowledgeTag,
)
from app.schemas.knowledge import KnowledgeTagDeletionPreview, KnowledgeTagResponse
from app.utils.knowledge_mapping_v2 import digest_json
from app.utils.knowledge_tag_audit import add_mapping_audit, mapping_snapshot


_MAPPINGS = (
    ("challenge", ChallengeKnowledgeTag, ChallengeKnowledgeTag.challenge_id),
    ("course", CourseKnowledgeTag, CourseKnowledgeTag.course_id),
    ("lesson", LessonKnowledgeTag, LessonKnowledgeTag.lesson_id),
)
_ACTIVE_STATES = {"draft", "pending_review", "approved"}


def _json_value(value):
    if isinstance(value, (UUID, datetime)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _snapshot(row) -> dict:
    return {column.name: _json_value(getattr(row, column.name)) for column in row.__table__.columns}


@dataclass
class TagDeletionImpact:
    tag: KnowledgeTag
    mappings: dict
    children: list
    nodes: list
    relations: list
    proposals: list
    audit_events: int

    @property
    def counts(self) -> dict[str, int]:
        return {
            **{kind: len(rows) for kind, rows in self.mappings.items()},
            "children": len(self.children), "relations": len(self.relations),
            "proposals": len(self.proposals), "audit_events": self.audit_events,
        }

    @property
    def confirmation(self) -> str:
        # 수뿐 아니라 같은 수의 연결 교체와 근거/품질 수정도 검출한다.
        return digest_json({
            "tag": _snapshot(self.tag), "counts": self.counts,
            "mappings": {kind: [_snapshot(row) for row in rows] for kind, rows in self.mappings.items()},
            **{name: [_snapshot(row) for row in getattr(self, name)]
               for name in ("children", "nodes", "relations", "proposals")},
        })

    def preview(self) -> KnowledgeTagDeletionPreview:
        return KnowledgeTagDeletionPreview(tag=KnowledgeTagResponse.model_validate(self.tag),
            counts=self.counts, confirmation=self.confirmation)


async def _rows(db, query, *, lock: bool):
    if lock:
        # 기존 writer의 콘텐츠→junction→태그 FK 잠금과 서로 기다리지 않는다.
        query = query.with_for_update(nowait=True)
    return list((await db.scalars(query.execution_options(populate_existing=True))).all())


async def load_tag_deletion_impact(db, tag_id: int, *, lock: bool = False) -> TagDeletionImpact:
    tags = await _rows(db, select(KnowledgeTag).where(KnowledgeTag.id == tag_id), lock=lock)
    if not tags:
        raise HTTPException(404, "태그를 찾을 수 없습니다.")
    mappings = {}
    for kind, model, column in _MAPPINGS:
        mappings[kind] = await _rows(db, select(model).where(model.tag_id == tag_id).order_by(column), lock=lock)
    children = await _rows(db, select(KnowledgeTag).where(KnowledgeTag.parent_id == tag_id)
        .order_by(KnowledgeTag.id), lock=lock)
    nodes = await _rows(db, select(KnowledgeContentNode).where(
        KnowledgeContentNode.entity_type == "knowledge_tag", KnowledgeContentNode.entity_id == str(tag_id))
        .order_by(KnowledgeContentNode.id), lock=lock)
    node_ids = [node.id for node in nodes]
    relations = await _rows(db, select(KnowledgeRelation).where(or_(
        KnowledgeRelation.source_node_id.in_(node_ids), KnowledgeRelation.target_node_id.in_(node_ids)))
        .order_by(KnowledgeRelation.id), lock=lock) if node_ids else []
    relation_ids = [relation.id for relation in relations]
    proposals = await _rows(db, select(KnowledgeRelationProposal).where(or_(
        KnowledgeRelationProposal.source_node_id.in_(node_ids), KnowledgeRelationProposal.target_node_id.in_(node_ids),
        KnowledgeRelationProposal.base_relation_id.in_(relation_ids),
        KnowledgeRelationProposal.published_relation_id.in_(relation_ids)))
        .order_by(KnowledgeRelationProposal.id), lock=lock) if node_ids else []
    audit_events = await db.scalar(select(func.count()).select_from(KnowledgeTagMappingAuditEvent)
        .where(KnowledgeTagMappingAuditEvent.tag_id == tag_id))
    return TagDeletionImpact(tags[0], mappings, children, nodes, relations, proposals, int(audit_events or 0))


def _outbox(db, *, aggregate_type, aggregate_id, event_type, graph_revision, payload):
    db.add(KnowledgeProjectionOutbox(aggregate_type=aggregate_type, aggregate_id=aggregate_id,
        event_type=event_type, graph_revision=graph_revision, payload=payload, payload_digest=digest_json(payload)))


async def delete_tag_preserving_history(db, tag_id: int, *, actor_id: UUID,
                                      confirmation: str | None = None) -> None:
    impact = await load_tag_deletion_impact(db, tag_id, lock=True)
    if confirmation is not None and confirmation != impact.confirmation:
        raise HTTPException(409, "삭제 확인 후 태그 또는 연결이 변경되었습니다. 최신 삭제 영향을 다시 확인하세요.")
    now = datetime.now(timezone.utc)
    revision = uuid4()
    archive_id = uuid4()
    db.add(KnowledgeTagDeletionArchive(id=archive_id, tag_id=tag_id, actor_id=actor_id,
        tag_snapshot=_snapshot(impact.tag), graph_revision=revision, deleted_at=now,
        impact_snapshot={"counts": impact.counts,
            "children": [child.id for child in impact.children],
            "nodes": [str(node.id) for node in impact.nodes],
            "relations": [str(row.id) for row in impact.relations],
            "proposals": [str(row.id) for row in impact.proposals]}))
    reason = f"지식 태그 삭제: {impact.tag.name} (#{tag_id})"
    for relation in impact.relations:
        if relation.status not in _ACTIVE_STATES:
            continue
        before = _snapshot(relation)
        action = "retired" if relation.status == "approved" else "rejected"
        relation.status = action
        relation.reviewed_by = actor_id
        relation.updated_at = now
        if action == "retired":
            relation.retired_at = now
        db.add(KnowledgeRelationReview(relation_id=relation.id, reviewer_id=actor_id,
            action=action, reason=reason, before_snapshot=before, after_snapshot=_snapshot(relation), created_at=now))
        _outbox(db, aggregate_type="knowledge_relation", aggregate_id=relation.id,
            event_type="relation_retired" if action == "retired" else "proposal_rejected",
            graph_revision=revision, payload={"relation_id": str(relation.id), "deleted_tag_id": str(tag_id)})
    for proposal in impact.proposals:
        if proposal.state not in _ACTIVE_STATES:
            continue
        before = _snapshot(proposal)
        action = "retired" if proposal.state == "approved" else "rejected"
        proposal.state = action
        proposal.reviewed_by = actor_id
        proposal.updated_at = now
        if action == "retired":
            proposal.retired_at = now
        db.add(KnowledgeRelationProposalReview(proposal_id=proposal.id, reviewer_id=actor_id,
            action=action, reason=reason, before_snapshot=before, after_snapshot=_snapshot(proposal), created_at=now))
        _outbox(db, aggregate_type="knowledge_relation_proposal", aggregate_id=proposal.id,
            event_type="proposal_retired" if action == "retired" else "proposal_rejected",
            graph_revision=revision, payload={"proposal_id": str(proposal.id), "deleted_tag_id": str(tag_id)})
    for node in impact.nodes:
        node.is_active = False
        node.updated_at = now
    for kind, model, column in _MAPPINGS:
        for mapping in impact.mappings[kind]:
            add_mapping_audit(db, entity_type=kind, entity_id=str(getattr(mapping, column.key)),
                tag_id=tag_id, action="detach", actor_id=actor_id, snapshot=mapping_snapshot(mapping))
        # bulk DELETE는 ORM 관계 cascade를 불러 기존 감사/노드 원장을 지우지 않는다.
        await db.execute(delete(model).where(model.tag_id == tag_id))
    if impact.children:
        await db.execute(update(KnowledgeTag).where(KnowledgeTag.parent_id == tag_id)
            .values(parent_id=None, updated_at=now))
    _outbox(db, aggregate_type="knowledge_tag", aggregate_id=archive_id, event_type="tag_deleted",
        graph_revision=revision, payload={"tag_id": str(tag_id), "deletion_archive_id": str(archive_id)})
    await db.flush()
    await db.execute(delete(KnowledgeTag).where(KnowledgeTag.id == tag_id))
    await db.flush()
