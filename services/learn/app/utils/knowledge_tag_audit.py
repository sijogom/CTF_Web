"""수동 지식 태그 매핑의 provenance와 append-only 감사 기록."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.models.knowledge import KnowledgeTagMappingAuditEvent


_DEFAULT_DIRECTION = "content_to_knowledge_tag"
_DEFAULT_EVIDENCE = "수동 지식 태그 매핑"
_DEFAULT_ORIGIN = "manual"


def mapping_snapshot(mapping, *, fallback_now: datetime | None = None) -> dict:
    """현재 junction 행을 삭제 전 복원 가능한 최소 provenance로 고정한다."""

    created_at = getattr(mapping, "created_at", None) or fallback_now
    approved_at = getattr(mapping, "approved_at", None)
    created_by = getattr(mapping, "created_by", None)
    strength = getattr(mapping, "strength", None)
    confidence = getattr(mapping, "confidence", None)
    return {
        "direction": getattr(mapping, "direction", None) or _DEFAULT_DIRECTION,
        "strength": float(strength if strength is not None else 1),
        "confidence": float(confidence if confidence is not None else 1),
        "evidence_summary": getattr(mapping, "evidence_summary", None) or _DEFAULT_EVIDENCE,
        "origin": getattr(mapping, "origin", None) or _DEFAULT_ORIGIN,
        "created_by": str(created_by) if created_by else None,
        "created_at": created_at.isoformat() if created_at else None,
        "approved_at": approved_at.isoformat() if approved_at else None,
        "source_relation_id": str(getattr(mapping, "source_relation_id", None)) if getattr(mapping, "source_relation_id", None) else None,
    }


def manual_mapping_snapshot(
    actor_id: UUID,
    now: datetime,
    *,
    direction: str = _DEFAULT_DIRECTION,
    strength: float = 1,
    confidence: float = 1,
    evidence_summary: str = _DEFAULT_EVIDENCE,
) -> dict:
    """새 수동 매핑이 생성되는 순간 사용할 명시적 provenance."""

    return {
        "direction": direction,
        "strength": float(strength),
        "confidence": float(confidence),
        "evidence_summary": evidence_summary,
        "origin": _DEFAULT_ORIGIN,
        "created_by": str(actor_id),
        "created_at": now.isoformat(),
        "approved_at": now.isoformat(),
    }


def add_mapping_audit(
    db,
    *,
    entity_type: str,
    entity_id: str,
    tag_id: int,
    action: str,
    actor_id: UUID,
    snapshot: dict,
) -> None:
    db.add(
        KnowledgeTagMappingAuditEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            tag_id=tag_id,
            action=action,
            actor_id=actor_id,
            mapping_snapshot=snapshot,
        )
    )
