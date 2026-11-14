"""콘텐츠 편집과 독립 매핑 편집이 공유하는 트랜잭션 계약."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models.challenge import Challenge
from app.models.course import Course, Lesson
from app.models.knowledge import ChallengeKnowledgeTag, CourseKnowledgeTag, KnowledgeTag, LessonKnowledgeTag
from app.schemas.knowledge import ContentTagMappingEntry, ContentTagMappingResponse, ContentTagMappingUpdate
from app.utils.knowledge_tag_audit import add_mapping_audit, mapping_snapshot
from app.utils.knowledge_tag_projection import lock_content_tag_mapping_entity, project_rule_tag_mappings

EntityType = Literal["challenge", "course", "lesson"]


def mapping_contract(entity_type: EntityType):
    return {
        "challenge": (Challenge, ChallengeKnowledgeTag, ChallengeKnowledgeTag.challenge_id),
        "course": (Course, CourseKnowledgeTag, CourseKnowledgeTag.course_id),
        "lesson": (Lesson, LessonKnowledgeTag, LessonKnowledgeTag.lesson_id),
    }[entity_type]


def mapping_revision(mappings) -> str:
    snapshots = []
    for mapping in sorted(mappings, key=lambda item: item.tag_id):
        snapshot = mapping_snapshot(mapping)
        owner = getattr(mapping, "source_relation_id", None)
        snapshots.append({"tag_id": mapping.tag_id, **snapshot, "source_relation_id": str(owner) if owner else None})
    return sha256(json.dumps(snapshots, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mapping_response(entity_type: EntityType, entity_id: str, mappings) -> ContentTagMappingResponse:
    rows = sorted(mappings, key=lambda item: item.tag_id)
    entries = [ContentTagMappingEntry(tag_id=row.tag_id, **mapping_snapshot(row)) for row in rows]
    return ContentTagMappingResponse(entity_type=entity_type, entity_id=entity_id,
                                    tag_ids=[row.tag_id for row in rows], mappings=entries,
                                    mapping_revision=mapping_revision(rows))


async def get_content_tag_mapping(db, *, entity_type: EntityType, entity_id) -> ContentTagMappingResponse:
    _entity_model, model, column = mapping_contract(entity_type)
    rows = (await db.scalars(select(model).where(column == entity_id).execution_options(populate_existing=True))).all()
    return mapping_response(entity_type, str(entity_id), rows)


async def replace_content_tag_mappings(db, *, entity_type: EntityType, entity,
                                       body: ContentTagMappingUpdate, actor_id: UUID,
                                       synchronize_rules: bool = True) -> ContentTagMappingResponse:
    """호출자의 트랜잭션에서만 변경한다. commit/rollback은 호출자 책임이다."""
    _entity_model, model, column = mapping_contract(entity_type)
    await lock_content_tag_mapping_entity(db, entity_type=entity_type, entity=entity)
    rows = list((await db.scalars(select(model).where(column == entity.id)
                                .with_for_update().execution_options(populate_existing=True))).all())
    if body.expected_revision is not None and body.expected_revision != mapping_revision(rows):
        raise HTTPException(409, "다른 작업에서 태그 매핑이 변경되었습니다. 초안을 보존하고 최신 매핑을 확인하세요.")
    previous_by_id = {row.tag_id: row for row in rows}
    items = body.resolved_mappings()
    requested_ids = {item.tag_id for item in items}
    if requested_ids:
        found = set((await db.scalars(select(KnowledgeTag.id).where(KnowledgeTag.id.in_(requested_ids))
            .order_by(KnowledgeTag.id).with_for_update(read=True, key_share=True))).all())
        if found != requested_ids:
            raise HTTPException(404, "존재하지 않는 지식 태그가 포함되어 있습니다.")
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.origin == "rule" or row.tag_id in requested_ids:
            continue
        add_mapping_audit(db, entity_type=entity_type, entity_id=str(entity.id), tag_id=row.tag_id,
                          action="detach", actor_id=actor_id, snapshot=mapping_snapshot(row))
        await db.delete(row)
    for item in items:
        previous = previous_by_id.get(item.tag_id)
        if previous is not None and previous.origin == "rule":
            if body.mappings is not None and (
                float(previous.strength) != item.strength or float(previous.confidence) != item.confidence
                or previous.direction != item.direction or previous.evidence_summary != item.evidence_summary
            ):
                raise HTTPException(422, "자동 규칙 태그는 읽기 전용입니다. 원본 category/tags를 수정하세요.")
            continue
        if previous is not None and body.mappings is None:
            continue
        if previous is None:
            previous = model(**{column.key: entity.id, "tag_id": item.tag_id, "origin": "manual",
                                "created_by": actor_id, "created_at": now, "approved_at": now})
            db.add(previous)
        elif all(getattr(previous, field) == getattr(item, field) for field in
                 ("direction", "strength", "confidence", "evidence_summary")):
            continue
        for field in ("direction", "strength", "confidence", "evidence_summary"):
            setattr(previous, field, getattr(item, field))
        add_mapping_audit(db, entity_type=entity_type, entity_id=str(entity.id), tag_id=item.tag_id,
                          action="attach", actor_id=actor_id, snapshot=mapping_snapshot(previous))
    await db.flush()
    if synchronize_rules:
        await project_rule_tag_mappings(db, entity_type=entity_type, entity=entity, actor_id=actor_id)
    await db.flush()
    return await get_content_tag_mapping(db, entity_type=entity_type, entity_id=entity.id)
