"""관리자 지식 관계 원장과 검토 API.

관계는 곧바로 공개 추천·잠금 규칙이 되지 않는다. 모든 생성·수정은
``pending_review`` 상태로 들어가며, 관리자 승인 후에만 공개 그래프와 도움 콘텐츠에서
소비된다.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_id, require_role
from app.database import get_db
from app.models.challenge import Challenge, Hint
from app.models.course import Course, Lesson
from app.models.knowledge import (
    ChallengeKnowledgeTag,
    CourseKnowledgeTag,
    KnowledgeContentNode,
    KnowledgeContentProfile,
    KnowledgeCandidateRun,
    KnowledgeCandidateRunItem,
    KnowledgeProjectionOutbox,
    KnowledgeRelation,
    KnowledgeRelationProposal,
    KnowledgeRelationProposalEvidence,
    KnowledgeRelationProposalReview,
    KnowledgeRelationReview,
    KnowledgeTag,
    LessonKnowledgeTag,
)
from app.schemas.knowledge import (
    KnowledgeCandidateRunCreate,
    KnowledgeCandidateGenerationRequest,
    KnowledgeCandidateGenerationResponse,
    KnowledgeCandidatePreview,
    KnowledgeCandidateRunListResponse,
    KnowledgeCandidateRunResponse,
    KnowledgeCandidateRunnerStatusResponse,
    KnowledgeContentRef,
    KnowledgeContentSummary,
    KnowledgeNodeReconcileRequest,
    KnowledgeNodeReconcileResponse,
    KnowledgeRelationCreate,
    KnowledgeRelationListResponse,
    KnowledgeRelationResponse,
    KnowledgeRelationReviewRequest,
    KnowledgeRelationReviewResponse,
    KnowledgeRelationUpdate,
    KnowledgeProposalEvidenceResponse,
    KnowledgeProposalListResponse,
    KnowledgeProposalReviewRequest,
    KnowledgeProposalBulkReviewRequest,
    KnowledgeProposalReviewResponse,
    KnowledgeRelationProposalCreate,
    KnowledgeRelationProposalResponse,
)
from app.utils.knowledge_relations import (
    PUBLIC_CONTENT_TYPES,
    ResolvedKnowledgeContent,
    ensure_content_node,
    normalize_content_identifier,
    resolve_content,
    resolve_node,
)
from app.utils.knowledge_candidate_generation import (
    CandidateContentInput,
    candidate_input_snapshot,
    collect_public_tag_candidate_inputs,
    ensure_candidate_node_and_profile,
    existing_content_nodes,
)
from app.utils.knowledge_mapping_v2 import (
    DETERMINISTIC_TAG_PREPARES_FOR_V1,
    SAFE_PROFILE_SCHEMA_VERSION,
    build_deterministic_tag_prepares_for_candidates,
    combined_profile_revision,
    deterministic_tag_prepares_for_configuration,
    digest_json,
    next_proposal_state,
    profile_for_resolved_content,
)
from app.utils.knowledge_tag_audit import add_mapping_audit
from app.utils.knowledge_tag_projection import lock_content_tag_mapping_entity, project_rule_tag_mappings
from app.services.knowledge_candidate_generation import (
    CandidateGenerationInvocation,
    CandidateGenerationServiceError,
    CandidateRunnerConfigurationError,
    MANUAL_EXECUTION_PRINCIPAL,
    MANUAL_TRIGGER_KIND,
    generate_rule_based_candidate_run,
    scheduled_candidate_runner_settings_from_environment,
)


router = APIRouter(prefix="/admin", tags=["지식 관계 관리"])

_ACTIVE_RELATION_STATUSES = ("draft", "pending_review", "approved")
_EDITABLE_RELATION_STATUSES = {"draft", "pending_review", "approved"}
_REVIEWABLE_RELATION_STATUSES = {"draft", "pending_review"}
_CANDIDATE_BLOCKING_RELATION_TYPES = ("requires", "prepares_for", "tag_affinity")


async def _assert_independent_relation_review(db, relation, actor_id: UUID) -> None:
    last_author = await db.scalar(
        select(KnowledgeRelationReview.reviewer_id).where(
            KnowledgeRelationReview.relation_id == relation.id,
            KnowledgeRelationReview.action.in_(("submitted", "edited")),
        ).order_by(KnowledgeRelationReview.created_at.desc(), KnowledgeRelationReview.id.desc()).limit(1)
    )
    if actor_id in {relation.created_by, last_author}:
        raise HTTPException(403, "자신이 등록하거나 수정한 지식 관계는 승인할 수 없습니다.")


async def _assert_public_review_endpoints(db, source_id, target_id, actor_id, relation_type):
    for node_id in (source_id, target_id):
        node = await db.get(KnowledgeContentNode, node_id)
        public_only = not (relation_type == "tag_affinity" and node_id == target_id and node and node.entity_type == "knowledge_tag")
        content = await resolve_node(db, node, public_only=public_only, viewer_id=actor_id) if node else None
        if content is None:
            raise HTTPException(409, "관계 원본이 현재 공개 가능한 상태인지 확인하세요.")


def _is_expected_unique_constraint_violation(
    exc: IntegrityError,
    *constraint_names: str,
) -> bool:
    """의도한 부분 unique 충돌만 사용자에게 중복으로 표현한다.

    FK/CHECK/NOT NULL 등 다른 무결성 오류는 관계가 이미 존재한다는 409으로 숨기지
    않는다. 호출 경로는 rollback 뒤 원래 오류를 다시 올려 공통 오류 처리에서 fail-closed
    하게 처리한다.
    """
    original = getattr(exc, "orig", None)
    visited = set()
    expected = set(constraint_names)
    # asyncpg의 SQLAlchemy adapter는 제약명을 복사하지 않고 원래 DB 오류를
    # 명시적 cause로 연결한다. 문자열 추측이나 다른 무결성 오류의 변환은 하지 않는다.
    while original is not None and id(original) not in visited and len(visited) < 4:
        visited.add(id(original))
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if sqlstate is not None and sqlstate != "23505":
            return False
        diagnostic = getattr(original, "diag", None)
        constraint_name = getattr(diagnostic, "constraint_name", None) or getattr(original, "constraint_name", None)
        if constraint_name is not None:
            return constraint_name in expected
        original = getattr(original, "__cause__", None)
    return False


def _candidate_pair_advisory_lock_key(source_node_id: UUID, target_node_id: UUID) -> int:
    """동일 방향의 ``requires``/``prepares_for`` 경합을 직렬화하는 안정 키."""
    payload = f"knowledge-relation-pair-v1:{source_node_id}:{target_node_id}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big", signed=True)


async def _lock_candidate_relation_pair(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    relation_type: str,
    is_directed: bool,
) -> None:
    """서로 배타적인 선행 관계 쌍만 transaction 범위에서 잠근다."""
    if not is_directed or relation_type not in _CANDIDATE_BLOCKING_RELATION_TYPES:
        return
    await db.execute(
        select(func.pg_advisory_xact_lock(_candidate_pair_advisory_lock_key(source_node_id, target_node_id)))
    )


def _competing_candidate_relation_types(relation_type: str) -> tuple[str, ...]:
    if relation_type == "requires":
        return ("prepares_for",)
    if relation_type == "prepares_for":
        return ("requires",)
    return ()


async def _assert_no_competing_candidate_relation(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    relation_type: str,
    is_directed: bool,
    exclude_relation_id: UUID | None = None,
) -> None:
    """동일 쌍의 서로 다른 선행 관계는 동시에 대기·공개 상태일 수 없다."""
    competing_types = _competing_candidate_relation_types(relation_type)
    if not is_directed or not competing_types:
        return
    await _lock_candidate_relation_pair(
        db,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        relation_type=relation_type,
        is_directed=is_directed,
    )
    relation_conditions = [
            KnowledgeRelation.source_node_id == source_node_id,
            KnowledgeRelation.target_node_id == target_node_id,
            KnowledgeRelation.is_directed.is_(True),
            KnowledgeRelation.relation_type.in_(competing_types),
            KnowledgeRelation.status.in_(_ACTIVE_RELATION_STATUSES),
    ]
    if exclude_relation_id:
        relation_conditions.append(KnowledgeRelation.id != exclude_relation_id)
    existing_relation = await db.execute(select(KnowledgeRelation.id).where(*relation_conditions).limit(1))
    if existing_relation.scalar_one_or_none():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "같은 콘텐츠 쌍에 배타적인 선행 관계가 이미 대기 또는 공개 상태입니다.",
        )
    existing_proposal = await db.execute(
        select(KnowledgeRelationProposal.id).where(
            KnowledgeRelationProposal.source_node_id == source_node_id,
            KnowledgeRelationProposal.target_node_id == target_node_id,
            KnowledgeRelationProposal.is_directed.is_(True),
            KnowledgeRelationProposal.relation_type.in_(competing_types),
            KnowledgeRelationProposal.state.in_(("draft", "pending_review")),
        ).limit(1)
    )
    if existing_proposal.scalar_one_or_none():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "같은 콘텐츠 쌍에 배타적인 선행 관계 제안서가 이미 대기 상태입니다.",
        )


def _http_bad_content_identifier() -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "콘텐츠 유형 또는 ID 형식이 올바르지 않습니다.")


def _relation_snapshot(relation: KnowledgeRelation) -> dict:
    """감사 가능한 관계 상태를 JSON 안전한 최소 스냅샷으로 만든다."""
    return {
        "source_node_id": str(relation.source_node_id),
        "target_node_id": str(relation.target_node_id),
        "relation_type": relation.relation_type,
        "is_directed": relation.is_directed,
        "strength": float(relation.strength),
        "confidence": float(relation.confidence),
        "origin": relation.origin,
        "evidence_summary": relation.evidence_summary,
        "evidence": relation.evidence or {},
        "quality_version": relation.quality_version,
        "status": relation.status,
        "legacy_reference": relation.legacy_reference,
    }


def _assert_relation_evidence(
    evidence_summary: str | None,
    evidence: dict | None,
    *,
    origin: str,
    quality_version: str | None,
) -> None:
    """공개 후보 관계에 최소 검토 근거와 생성 출처를 강제한다."""
    if not (evidence_summary or "").strip() and not evidence:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "관계 후보에는 검토 가능한 근거 요약 또는 구조화된 근거가 필요합니다.",
        )
    if origin == "model_candidate" and not (quality_version or "").strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "모델 후보 관계에는 생성 규칙 또는 모델 버전이 필요합니다.",
        )


async def _resolve_content_ref(
    db: AsyncSession,
    ref: KnowledgeContentRef,
) -> ResolvedKnowledgeContent:
    try:
        normalized_id = normalize_content_identifier(ref.type, ref.id)
    except ValueError as exc:
        raise _http_bad_content_identifier() from exc

    content = await resolve_content(db, ref.type, normalized_id, public_only=False)
    if not content:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "관계에 지정한 원본 콘텐츠를 찾을 수 없습니다.")
    return content


def _canonicalize_nodes(
    source_node: KnowledgeContentNode,
    target_node: KnowledgeContentNode,
    *,
    is_directed: bool,
) -> tuple[KnowledgeContentNode, KnowledgeContentNode]:
    """무방향 관계는 한 방향으로 정규화해 A↔B 중복을 막는다."""
    if not is_directed and str(source_node.id) > str(target_node.id):
        return target_node, source_node
    return source_node, target_node


async def _assert_relation_not_duplicated(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    relation_type: str,
    is_directed: bool,
    exclude_relation_id: UUID | None = None,
) -> None:
    query = select(KnowledgeRelation.id).where(
        KnowledgeRelation.source_node_id == source_node_id,
        KnowledgeRelation.target_node_id == target_node_id,
        KnowledgeRelation.relation_type == relation_type,
        KnowledgeRelation.is_directed.is_(is_directed),
        KnowledgeRelation.status.in_(_ACTIVE_RELATION_STATUSES),
    )
    if exclude_relation_id:
        query = query.where(KnowledgeRelation.id != exclude_relation_id)
    if (await db.execute(query)).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "같은 공개 후보 관계가 이미 존재합니다.")


def _summary_from_registry(node: KnowledgeContentNode) -> KnowledgeContentSummary:
    return KnowledgeContentSummary(
        type=node.entity_type,
        id=node.entity_id,
        name=node.display_name,
        is_active=False,
        href=None,
    )


def _summary_from_content(content: ResolvedKnowledgeContent) -> KnowledgeContentSummary:
    return KnowledgeContentSummary(
        type=content.entity_type,
        id=content.entity_id,
        name=content.display_name,
        is_active=content.is_active,
        href=content.href,
        course_id=content.course_id,
    )


async def _serialize_relation(
    db: AsyncSession,
    relation: KnowledgeRelation,
) -> KnowledgeRelationResponse:
    node_result = await db.execute(
        select(KnowledgeContentNode).where(
            KnowledgeContentNode.id.in_([relation.source_node_id, relation.target_node_id])
        )
    )
    nodes = {node.id: node for node in node_result.scalars().all()}
    source_node = nodes.get(relation.source_node_id)
    target_node = nodes.get(relation.target_node_id)
    if not source_node or not target_node:
        # FK가 RESTRICT이므로 정상 DB에서는 발생하지 않아야 한다. 관리자 API도
        # 오류를 은폐하지 않고 관계를 표시할 수 있도록 최소 정보만 남긴다.
        raise HTTPException(status.HTTP_409_CONFLICT, "관계 노드 무결성이 손상되었습니다.")

    source_content = await resolve_node(db, source_node, public_only=False)
    target_content = await resolve_node(db, target_node, public_only=False)
    reviews_result = await db.execute(
        select(KnowledgeRelationReview)
        .where(KnowledgeRelationReview.relation_id == relation.id)
        .order_by(KnowledgeRelationReview.created_at)
    )
    reviews = reviews_result.scalars().all()

    return KnowledgeRelationResponse(
        id=relation.id,
        source=_summary_from_content(source_content) if source_content else _summary_from_registry(source_node),
        target=_summary_from_content(target_content) if target_content else _summary_from_registry(target_node),
        relation_type=relation.relation_type,
        is_directed=relation.is_directed,
        strength=float(relation.strength),
        confidence=float(relation.confidence),
        origin=relation.origin,
        evidence_summary=relation.evidence_summary,
        evidence=relation.evidence or {},
        quality_version=relation.quality_version,
        status=relation.status,
        created_by=relation.created_by,
        reviewed_by=relation.reviewed_by,
        approved_at=relation.approved_at,
        retired_at=relation.retired_at,
        created_at=relation.created_at,
        updated_at=relation.updated_at,
        reviews=[KnowledgeRelationReviewResponse.model_validate(review) for review in reviews],
    )


async def _load_relation_or_404(
    db: AsyncSession,
    relation_id: UUID,
    *,
    for_update: bool = False,
) -> KnowledgeRelation:
    """관계를 읽고, 상태 전이 시에는 행 잠금으로 경쟁을 직렬화한다."""
    if for_update:
        identity = (await db.execute(select(KnowledgeRelation.source_node_id, KnowledgeRelation.target_node_id,
            KnowledgeRelation.relation_type, KnowledgeRelation.is_directed).where(KnowledgeRelation.id == relation_id))).first()
        if identity is None:
            raise HTTPException(404, "지식 관계를 찾을 수 없습니다.")
        await _lock_candidate_relation_pair(db, source_node_id=identity[0], target_node_id=identity[1],
                                            relation_type=identity[2], is_directed=identity[3])
        result = await db.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.id == relation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        relation = result.scalar_one_or_none()
    else:
        relation = await db.get(KnowledgeRelation, relation_id)
    if not relation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "지식 관계를 찾을 수 없습니다.")
    return relation


@router.get("/knowledge-relations", response_model=KnowledgeRelationListResponse)
async def list_knowledge_relations(
    relation_status: str | None = Query(None, alias="status"),
    relation_type: str | None = Query(None),
    origin: str | None = Query(None),
    entity_type: str | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관계 원장 목록. 승인 전 후보도 관리자에게만 보인다."""
    if entity_type and entity_type not in PUBLIC_CONTENT_TYPES:
        raise _http_bad_content_identifier()

    query = select(KnowledgeRelation).order_by(
        KnowledgeRelation.updated_at.desc(),
        KnowledgeRelation.id.desc(),
    )
    count_query = select(func.count()).select_from(KnowledgeRelation)
    conditions = []
    if relation_status:
        conditions.append(KnowledgeRelation.status == relation_status)
    if relation_type:
        conditions.append(KnowledgeRelation.relation_type == relation_type)
    if origin:
        conditions.append(KnowledgeRelation.origin == origin)
    if entity_type:
        node_ids_query = select(KnowledgeContentNode.id).where(
            KnowledgeContentNode.entity_type == entity_type
        )
        conditions.append(
            (KnowledgeRelation.source_node_id.in_(node_ids_query))
            | (KnowledgeRelation.target_node_id.in_(node_ids_query))
        )
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)

    total = int((await db.execute(count_query)).scalar() or 0)
    relation_result = await db.execute(query.offset(offset).limit(limit))
    relations = relation_result.scalars().all()
    return KnowledgeRelationListResponse(
        items=[await _serialize_relation(db, relation) for relation in relations],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post("/knowledge-relations", response_model=KnowledgeRelationResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_relation(
    body: KnowledgeRelationCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관계를 후보로 등록한다. 공개에는 관리자 승인이 추가로 필요하다."""
    _assert_relation_evidence(
        body.evidence_summary,
        body.evidence,
        origin=body.origin,
        quality_version=body.quality_version,
    )
    source_content = await _resolve_content_ref(db, body.source)
    target_content = await _resolve_content_ref(db, body.target)
    source_node = await ensure_content_node(db, source_content)
    target_node = await ensure_content_node(db, target_content)
    source_node, target_node = _canonicalize_nodes(
        source_node,
        target_node,
        is_directed=body.is_directed,
    )
    if source_node.id == target_node.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "자기 자신을 가리키는 관계는 만들 수 없습니다.")
    await _assert_no_competing_candidate_relation(
        db,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=body.relation_type,
        is_directed=body.is_directed,
    )
    await _assert_relation_not_duplicated(
        db,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=body.relation_type,
        is_directed=body.is_directed,
    )

    relation = KnowledgeRelation(
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=body.relation_type,
        is_directed=body.is_directed,
        strength=body.strength,
        confidence=body.confidence,
        origin=body.origin,
        evidence_summary=body.evidence_summary,
        evidence=body.evidence,
        quality_version=body.quality_version,
        legacy_reference=body.legacy_reference,
        status="pending_review",
        created_by=actor_id,
    )
    db.add(relation)
    try:
        await db.flush()
        db.add(
            KnowledgeRelationReview(
                relation_id=relation.id,
                reviewer_id=actor_id,
                action="submitted",
                reason="관계 후보 등록",
                after_snapshot=_relation_snapshot(relation),
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_expected_unique_constraint_violation(exc, "uq_knowledge_relation_active_pair"):
            raise HTTPException(status.HTTP_409_CONFLICT, "같은 공개 후보 관계가 이미 존재합니다.") from exc
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "지식 관계 저장 중 무결성 오류가 발생했습니다.",
        ) from exc
    await db.refresh(relation)
    return await _serialize_relation(db, relation)


@router.put("/knowledge-relations/{relation_id}", response_model=KnowledgeRelationResponse)
async def update_knowledge_relation(
    relation_id: UUID,
    body: KnowledgeRelationUpdate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관계를 수정하고 재검토 상태로 되돌린다."""
    relation = await _load_relation_or_404(db, relation_id, for_update=True)
    for field in ("relation_type", "is_directed", "strength", "confidence", "origin"):
        if field in body.model_fields_set and getattr(body, field) is None:
            raise HTTPException(422, "관계 유형·방향·품질·출처는 null일 수 없습니다.")
    if relation.status not in _EDITABLE_RELATION_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "반려 또는 은퇴된 관계는 새 후보로 다시 등록해야 합니다.")

    if relation.status == "approved":
        source = await db.get(KnowledgeContentNode, relation.source_node_id)
        target = await db.get(KnowledgeContentNode, relation.target_node_id)
        values = body.model_dump(exclude_unset=True)
        values.pop("reason", None)
        values.pop("legacy_reference", None)
        values.pop("evidence", None)
        proposal_values = {
            "source": {"type": source.entity_type, "id": source.entity_id},
            "target": {"type": target.entity_type, "id": target.entity_id},
            "relation_type": relation.relation_type, "is_directed": relation.is_directed,
            "strength": float(relation.strength), "confidence": float(relation.confidence),
            "origin": relation.origin, "evidence_summary": relation.evidence_summary,
            "quality_version": relation.quality_version,
            **values, "base_relation_id": relation.id,
            "evidence": [{"evidence_kind": "manual", "summary": body.reason}],
        }
        from pydantic import ValidationError
        try:
            proposal_body = KnowledgeRelationProposalCreate.model_validate(proposal_values)
        except ValidationError as exc:
            raise HTTPException(422, "개정 제안서의 근거와 품질 입력을 확인하세요.") from exc
        proposal = await create_knowledge_proposal(proposal_body, db=db, actor_id=actor_id, _role=_role)
        response = await _serialize_relation(db, relation)
        response.pending_proposal_id = proposal.id
        return response

    before_snapshot = _relation_snapshot(relation)
    provided_fields = body.model_fields_set
    if "evidence" in provided_fields and body.evidence is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "관계 근거는 null일 수 없습니다. 근거를 지우려면 빈 객체 {}와 근거 요약을 함께 저장하세요.",
        )
    source_node_id = relation.source_node_id
    target_node_id = relation.target_node_id
    is_directed = body.is_directed if "is_directed" in provided_fields else relation.is_directed
    if "source" in provided_fields and body.source:
        source_node_id = (await ensure_content_node(db, await _resolve_content_ref(db, body.source))).id
    if "target" in provided_fields and body.target:
        target_node_id = (await ensure_content_node(db, await _resolve_content_ref(db, body.target))).id

    source_node = await db.get(KnowledgeContentNode, source_node_id)
    target_node = await db.get(KnowledgeContentNode, target_node_id)
    if not source_node or not target_node:
        raise HTTPException(status.HTTP_409_CONFLICT, "관계 노드 무결성이 손상되었습니다.")
    source_node, target_node = _canonicalize_nodes(source_node, target_node, is_directed=is_directed)
    if source_node.id == target_node.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "자기 자신을 가리키는 관계는 만들 수 없습니다.")

    relation_type = body.relation_type if "relation_type" in provided_fields else relation.relation_type
    await _assert_no_competing_candidate_relation(
        db,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=relation_type,
        is_directed=is_directed,
        exclude_relation_id=relation.id,
    )
    await _assert_relation_not_duplicated(
        db,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=relation_type,
        is_directed=is_directed,
        exclude_relation_id=relation.id,
    )

    relation.source_node_id = source_node.id
    relation.target_node_id = target_node.id
    relation.relation_type = relation_type
    relation.is_directed = is_directed
    for field in (
        "strength", "confidence", "origin", "evidence_summary", "evidence",
        "quality_version", "legacy_reference",
    ):
        if field in provided_fields:
            setattr(relation, field, getattr(body, field))
    _assert_relation_evidence(
        relation.evidence_summary,
        relation.evidence,
        origin=relation.origin,
        quality_version=relation.quality_version,
    )
    relation.status = "pending_review"
    relation.reviewed_by = None
    relation.approved_at = None
    relation.retired_at = None
    try:
        await db.flush()
        db.add(
            KnowledgeRelationReview(
                relation_id=relation.id,
                reviewer_id=actor_id,
                action="edited",
                reason=body.reason,
                before_snapshot=before_snapshot,
                after_snapshot=_relation_snapshot(relation),
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_expected_unique_constraint_violation(exc, "uq_knowledge_relation_active_pair"):
            raise HTTPException(status.HTTP_409_CONFLICT, "같은 공개 후보 관계가 이미 존재합니다.") from exc
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "지식 관계 저장 중 무결성 오류가 발생했습니다.",
        ) from exc
    await db.refresh(relation)
    return await _serialize_relation(db, relation)


@router.post("/knowledge-relations/{relation_id}/review", response_model=KnowledgeRelationResponse)
async def review_knowledge_relation(
    relation_id: UUID,
    body: KnowledgeRelationReviewRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """관리자가 후보를 승인·반려하거나 승인된 관계를 은퇴시킨다."""
    relation = await _load_relation_or_404(db, relation_id, for_update=True)
    before_snapshot = _relation_snapshot(relation)
    now = datetime.now(timezone.utc)

    if body.action == "approve":
        if relation.status not in _REVIEWABLE_RELATION_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, "승인 가능한 검토 대기 관계가 아닙니다.")
        await _assert_independent_relation_review(db, relation, actor_id)
        await _assert_public_review_endpoints(db, relation.source_node_id, relation.target_node_id, actor_id, relation.relation_type)
        await _assert_no_competing_candidate_relation(
            db,
            source_node_id=relation.source_node_id,
            target_node_id=relation.target_node_id,
            relation_type=relation.relation_type,
            is_directed=relation.is_directed,
        )
        relation.status = "approved"
        relation.approved_at = now
        relation.retired_at = None
        await _sync_tag_affinity_mapping(
            db,
            relation=relation,
            actor_id=actor_id,
            now=now,
            attach=True,
        )
        review_action = "approved"
    elif body.action == "reject":
        if relation.status not in _REVIEWABLE_RELATION_STATUSES:
            raise HTTPException(status.HTTP_409_CONFLICT, "반려 가능한 검토 대기 관계가 아닙니다.")
        relation.status = "rejected"
        relation.approved_at = None
        review_action = "rejected"
    else:
        if relation.status != "approved":
            raise HTTPException(status.HTTP_409_CONFLICT, "은퇴 가능한 승인 관계가 아닙니다.")
        relation.status = "retired"
        relation.retired_at = now
        await _sync_tag_affinity_mapping(
            db,
            relation=relation,
            actor_id=actor_id,
            now=now,
            attach=False,
        )
        review_action = "retired"

    relation.reviewed_by = actor_id
    await db.flush()
    db.add(
        KnowledgeRelationReview(
            relation_id=relation.id,
            reviewer_id=actor_id,
            action=review_action,
            reason=body.reason,
            before_snapshot=before_snapshot,
            after_snapshot=_relation_snapshot(relation),
        )
    )
    await _record_projection_outbox(
        db, aggregate_type="knowledge_relation", aggregate_id=relation.id,
        event_type="relation_published" if body.action == "approve" else "relation_retired" if body.action == "retire" else "proposal_rejected",
        payload={"relation_id": relation.id}, graph_revision=uuid4(),
    )
    await db.commit()
    await db.refresh(relation)
    return await _serialize_relation(db, relation)


async def _source_identifiers(entity_type: str, db: AsyncSession) -> list[str]:
    """노드 동기화에 필요한 원본 ID만 가져온다. 원본 본문은 읽지 않는다."""
    if entity_type == "challenge":
        result = await db.execute(select(Challenge.id))
    elif entity_type == "course":
        result = await db.execute(select(Course.id))
    elif entity_type == "lesson":
        result = await db.execute(select(Lesson.id))
    elif entity_type == "knowledge_tag":
        result = await db.execute(select(KnowledgeTag.id))
    else:
        result = await db.execute(select(Hint.id))
    return [str(value) for value in result.scalars().all()]


@router.post("/knowledge-nodes/reconcile", response_model=KnowledgeNodeReconcileResponse)
async def reconcile_knowledge_nodes(
    body: KnowledgeNodeReconcileRequest,
    db: AsyncSession = Depends(get_db),
    _actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """원본 콘텐츠와 노드 레지스트리의 차이를 dry-run 또는 실제 반영한다."""
    entity_types = body.entity_types or sorted(PUBLIC_CONTENT_TYPES)
    scanned = updated = deactivated = missing_source = 0

    existing_result = await db.execute(
        select(KnowledgeContentNode).where(KnowledgeContentNode.entity_type.in_(entity_types))
    )
    existing_nodes = {
        (node.entity_type, node.entity_id): node
        for node in existing_result.scalars().all()
    }
    source_keys: set[tuple[str, str]] = set()

    for entity_type in entity_types:
        identifiers = await _source_identifiers(entity_type, db)
        for identifier in identifiers:
            content = await resolve_content(db, entity_type, identifier, public_only=False)
            if not content:
                continue
            scanned += 1
            key = (content.entity_type, content.entity_id)
            source_keys.add(key)
            existing = existing_nodes.get(key)
            changed = (
                existing is None
                or existing.display_name != content.display_name
                or existing.is_active != content.is_active
                or existing.source_updated_at != content.source_updated_at
            )
            if changed:
                updated += 1
                if not body.dry_run:
                    await ensure_content_node(db, content)

    for key, node in existing_nodes.items():
        if key in source_keys:
            continue
        missing_source += 1
        if node.is_active:
            deactivated += 1
            if not body.dry_run:
                node.is_active = False

    if not body.dry_run:
        await db.commit()

    return KnowledgeNodeReconcileResponse(
        dry_run=body.dry_run,
        scanned=scanned,
        updated=updated,
        deactivated=deactivated,
        missing_source=missing_source,
    )


# ─── V2 후보·검토·공개본 전환 ────────────────────────────────────────

_V2_ACTIVE_PROPOSAL_STATES = ("draft", "pending_review")


def _proposal_snapshot(proposal: KnowledgeRelationProposal) -> dict:
    """제안서 상태 전이용 최소 감사 스냅샷.

    evidence 원문이나 콘텐츠 프로필 본문은 여기에 복제하지 않는다. 연결된 ID,
    버전, 상태만 남겨 검토 이력과 원본 콘텐츠 권한 경로를 분리한다.
    """
    return {
        "base_relation_id": str(proposal.base_relation_id) if proposal.base_relation_id else None,
        "published_relation_id": str(proposal.published_relation_id) if proposal.published_relation_id else None,
        "source_node_id": str(proposal.source_node_id),
        "target_node_id": str(proposal.target_node_id),
        "source_profile_id": str(proposal.source_profile_id) if proposal.source_profile_id else None,
        "target_profile_id": str(proposal.target_profile_id) if proposal.target_profile_id else None,
        "relation_type": proposal.relation_type,
        "is_directed": proposal.is_directed,
        "strength": float(proposal.strength),
        "confidence": float(proposal.confidence),
        "origin": proposal.origin,
        "evidence_summary": proposal.evidence_summary,
        "quality_version": proposal.quality_version,
        "generation_run_id": str(proposal.generation_run_id) if proposal.generation_run_id else None,
        "state": proposal.state,
        "content_revision": proposal.content_revision,
    }


async def _ensure_safe_content_profile(
    db: AsyncSession,
    *,
    node: KnowledgeContentNode,
    content: ResolvedKnowledgeContent,
) -> KnowledgeContentProfile:
    """현재 원본의 최소 allowlist 프로필을 immutable snapshot으로 만든다."""
    allowed_profile = profile_for_resolved_content(
        entity_type=content.entity_type,
        entity_id=content.entity_id,
        display_name=content.display_name,
    )
    profile_digest = digest_json(allowed_profile)
    # digest가 프로필 내용 자체를 정확히 나타내므로, 같은 node/digest는 같은
    # candidate input으로 재사용한다. 원본의 안전 메타데이터가 바뀌면 새 digest가
    # 생겨 과거 제안의 재현성을 해치지 않는다.
    statement = (
        pg_insert(KnowledgeContentProfile)
        .values(
            id=uuid4(),
            node_id=node.id,
            content_revision=profile_digest,
            profile_schema_version=SAFE_PROFILE_SCHEMA_VERSION,
            allowed_profile=allowed_profile,
            profile_digest=profile_digest,
            generated_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(constraint="uq_knowledge_content_profile_revision")
        .returning(KnowledgeContentProfile)
    )
    profile = (await db.execute(statement)).scalar_one_or_none()
    if profile:
        return profile
    profile_result = await db.execute(
        select(KnowledgeContentProfile).where(
            KnowledgeContentProfile.node_id == node.id,
            KnowledgeContentProfile.content_revision == profile_digest,
        )
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status.HTTP_409_CONFLICT, "콘텐츠 프로필 스냅샷을 확인할 수 없습니다.")
    return profile


async def _load_proposal_or_404(
    db: AsyncSession,
    proposal_id: UUID,
    *,
    for_update: bool = False,
) -> KnowledgeRelationProposal:
    statement = select(KnowledgeRelationProposal).where(KnowledgeRelationProposal.id == proposal_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    proposal = (await db.execute(statement)).scalar_one_or_none()
    if not proposal:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "지식 관계 제안서를 찾을 수 없습니다.")
    return proposal


async def _load_relation_for_update_or_404(
    db: AsyncSession,
    relation_id: UUID,
) -> KnowledgeRelation:
    relation = (
        await db.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.id == relation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not relation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "개정 기준 공개 관계를 찾을 수 없습니다.")
    return relation


async def _assert_v2_proposal_not_duplicated(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    relation_type: str,
    is_directed: bool,
    base_relation_id: UUID | None,
) -> None:
    active_proposal = await db.execute(
        select(KnowledgeRelationProposal.id).where(
            KnowledgeRelationProposal.source_node_id == source_node_id,
            KnowledgeRelationProposal.target_node_id == target_node_id,
            KnowledgeRelationProposal.relation_type == relation_type,
            KnowledgeRelationProposal.is_directed.is_(is_directed),
            KnowledgeRelationProposal.state.in_(_V2_ACTIVE_PROPOSAL_STATES),
        )
    )
    if active_proposal.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "같은 관계의 검토 대기 제안서가 이미 존재합니다.")

    current_public = await db.execute(
        select(KnowledgeRelation.id).where(
            KnowledgeRelation.source_node_id == source_node_id,
            KnowledgeRelation.target_node_id == target_node_id,
            KnowledgeRelation.relation_type == relation_type,
            KnowledgeRelation.is_directed.is_(is_directed),
            KnowledgeRelation.status == "approved",
        )
    )
    current_public_id = current_public.scalar_one_or_none()
    if current_public_id and current_public_id != base_relation_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "같은 공개 관계가 이미 존재합니다. 개정하려면 해당 공개 관계를 기준으로 지정하세요.",
        )


async def _proposal_summary_for_node(
    db: AsyncSession,
    node: KnowledgeContentNode,
) -> KnowledgeContentSummary:
    content = await resolve_node(db, node, public_only=False)
    return _summary_from_content(content) if content else _summary_from_registry(node)


async def _serialize_proposal(
    db: AsyncSession,
    proposal: KnowledgeRelationProposal,
) -> KnowledgeRelationProposalResponse:
    node_result = await db.execute(
        select(KnowledgeContentNode).where(
            KnowledgeContentNode.id.in_([proposal.source_node_id, proposal.target_node_id])
        )
    )
    nodes_by_id = {node.id: node for node in node_result.scalars().all()}
    proposal_source_node = nodes_by_id.get(proposal.source_node_id)
    proposal_target_node = nodes_by_id.get(proposal.target_node_id)
    if not proposal_source_node or not proposal_target_node:
        raise HTTPException(status.HTTP_409_CONFLICT, "제안서 노드 무결성이 손상되었습니다.")

    evidence_result = await db.execute(
        select(KnowledgeRelationProposalEvidence)
        .where(KnowledgeRelationProposalEvidence.proposal_id == proposal.id)
        .order_by(KnowledgeRelationProposalEvidence.created_at)
    )
    evidence_items = evidence_result.scalars().all()
    evidence_source_ids = [item.source_node_id for item in evidence_items if item.source_node_id]
    evidence_nodes: dict[UUID, KnowledgeContentNode] = {}
    if evidence_source_ids:
        evidence_node_result = await db.execute(
            select(KnowledgeContentNode).where(KnowledgeContentNode.id.in_(evidence_source_ids))
        )
        evidence_nodes = {node.id: node for node in evidence_node_result.scalars().all()}

    review_result = await db.execute(
        select(KnowledgeRelationProposalReview)
        .where(KnowledgeRelationProposalReview.proposal_id == proposal.id)
        .order_by(KnowledgeRelationProposalReview.created_at)
    )
    reviews = review_result.scalars().all()

    serialized_evidence = []
    for item in evidence_items:
        source = None
        evidence_source_node = evidence_nodes.get(item.source_node_id) if item.source_node_id else None
        if evidence_source_node:
            source = await _proposal_summary_for_node(db, evidence_source_node)
        serialized_evidence.append(
            KnowledgeProposalEvidenceResponse(
                id=item.id,
                evidence_kind=item.evidence_kind,
                source=source,
                source_digest=item.source_digest,
                locator=item.locator,
                summary=item.summary,
                score_component=item.score_component,
                generator_version=item.generator_version,
                created_at=item.created_at,
            )
        )

    return KnowledgeRelationProposalResponse(
        id=proposal.id,
        base_relation_id=proposal.base_relation_id,
        published_relation_id=proposal.published_relation_id,
        source=await _proposal_summary_for_node(db, proposal_source_node),
        target=await _proposal_summary_for_node(db, proposal_target_node),
        relation_type=proposal.relation_type,
        is_directed=proposal.is_directed,
        strength=float(proposal.strength),
        confidence=float(proposal.confidence),
        origin=proposal.origin,
        evidence_summary=proposal.evidence_summary,
        quality_version=proposal.quality_version,
        generation_run_id=proposal.generation_run_id,
        state=proposal.state,
        content_revision=proposal.content_revision,
        created_by=proposal.created_by,
        reviewed_by=proposal.reviewed_by,
        approved_at=proposal.approved_at,
        retired_at=proposal.retired_at,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        evidence=serialized_evidence,
        reviews=[
            KnowledgeProposalReviewResponse(
                id=review.id,
                reviewer_id=review.reviewer_id,
                action=review.action,
                reason=review.reason,
                created_at=review.created_at,
            )
            for review in reviews
        ],
    )


async def _record_projection_outbox(
    db: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: UUID,
    event_type: str,
    payload: dict,
    graph_revision: UUID,
) -> None:
    """공개 관계 변경과 outbox를 같은 DB 트랜잭션에 기록한다."""
    safe_payload = {key: str(value) for key, value in payload.items() if value is not None}
    db.add(
        KnowledgeProjectionOutbox(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            event_type=event_type,
            graph_revision=graph_revision,
            payload=safe_payload,
            payload_digest=digest_json(safe_payload),
        )
    )


async def _sync_tag_affinity_mapping(
    db: AsyncSession,
    *,
    relation: KnowledgeRelation,
    actor_id: UUID,
    now: datetime,
    attach: bool,
) -> None:
    """Materialize an approved ``tag_affinity`` relation into its junction.

    The relation ledger remains the review/audit source, while the junction is
    the source consumed by tag editors, coverage metrics, and tag projections.
    Both are changed in the same transaction.  Manual/import mappings are never
    silently overwritten by an automatic proposal.
    """
    if relation.relation_type != "tag_affinity":
        return
    source_node = await db.get(KnowledgeContentNode, relation.source_node_id)
    target_node = await db.get(KnowledgeContentNode, relation.target_node_id)
    if not source_node or not target_node:
        raise HTTPException(status.HTTP_409_CONFLICT, "태그 관계의 원본 노드를 찾을 수 없습니다.")
    if not relation.is_directed or target_node.entity_type != "knowledge_tag":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "tag_affinity 관계는 공개 콘텐츠에서 지식 태그로 향해야 합니다.",
        )
    source = await resolve_node(db, source_node, public_only=attach)
    target = await resolve_node(db, target_node, public_only=False)
    if not source or not target:
        if not attach:
            return
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "비공개 콘텐츠 또는 비공개 태그는 그래프 매핑으로 승인할 수 없습니다.",
        )
    tag_id = int(target.entity_id)
    mapping_model = {
        "challenge": ChallengeKnowledgeTag,
        "course": CourseKnowledgeTag,
        "lesson": LessonKnowledgeTag,
    }.get(source.entity_type)
    if mapping_model is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "tag_affinity 관계의 원본은 문제·코스·레슨이어야 합니다.",
        )
    key_column = {
        "challenge": ChallengeKnowledgeTag.challenge_id,
        "course": CourseKnowledgeTag.course_id,
        "lesson": LessonKnowledgeTag.lesson_id,
    }[source.entity_type]
    source_value = int(source.entity_id) if source.entity_type == "challenge" else UUID(source.entity_id)
    entity_model = {"challenge": Challenge, "course": Course, "lesson": Lesson}[source.entity_type]
    entity = await db.get(entity_model, source_value)
    if entity is None:
        if not attach:
            return
        raise HTTPException(status.HTTP_409_CONFLICT, "태그 관계의 원본 콘텐츠를 찾을 수 없습니다.")
    await lock_content_tag_mapping_entity(db, entity_type=source.entity_type, entity=entity)
    entity = await db.scalar(select(entity_model).where(entity_model.id == source_value)
                             .execution_options(populate_existing=True))
    if source.entity_type == "lesson" and entity is not None:
        await db.scalar(select(Course).where(Course.id == entity.course_id)
                        .execution_options(populate_existing=True))
    source = await resolve_node(db, source_node, public_only=attach)
    # Keep the first-taxonomy approval exception: the target must exist, not
    # already be publicly mapped. Its key lock prevents deletion during approval.
    target_tag = await db.scalar(select(KnowledgeTag).where(KnowledgeTag.id == tag_id)
                                 .with_for_update(read=True, key_share=True)
                                 .execution_options(populate_existing=True))
    if entity is None or source is None or target_tag is None:
        if not attach:
            return
        raise HTTPException(status.HTTP_409_CONFLICT, "잠금 대기 중 원본 콘텐츠 또는 태그의 공개·존재 상태가 변경되었습니다.")
    mapping = (
        await db.execute(
            select(mapping_model)
            .where(key_column == source_value, mapping_model.tag_id == tag_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not attach:
        if mapping and mapping.origin in {"rule", "model_candidate"} and mapping.source_relation_id == relation.id:
            snapshot = {
                "direction": mapping.direction,
                "strength": float(mapping.strength),
                "confidence": float(mapping.confidence),
                "evidence_summary": mapping.evidence_summary,
                "origin": mapping.origin,
                "created_by": str(mapping.created_by) if mapping.created_by else None,
                "created_at": mapping.created_at.isoformat() if mapping.created_at else None,
                "approved_at": mapping.approved_at.isoformat() if mapping.approved_at else None,
                "source_relation_id": str(mapping.source_relation_id),
            }
            await db.delete(mapping)
            add_mapping_audit(
                db,
                entity_type=source.entity_type,
                entity_id=source.entity_id,
                tag_id=tag_id,
                action="detach",
                actor_id=actor_id,
                snapshot=snapshot,
            )
        await db.flush()
        if source.entity_type == "course":
            course = await db.get(Course, source_value)
            await project_rule_tag_mappings(db, entity_type="course", entity=course, actor_id=actor_id)
        return
    if mapping and mapping.origin in {"manual", "import"}:
        if relation.origin in {"rule", "model_candidate"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "같은 콘텐츠에 이미 수동 지식 태그가 연결되어 있습니다.",
            )
        # A manual/import relation may confirm an already materialized
        # mapping. Keep its provenance and do not rewrite it.
        return
    if mapping and mapping.origin == "rule" and mapping.source_relation_id is None:
        return
    if mapping and mapping.source_relation_id not in {None, relation.id}:
        raise HTTPException(409, "다른 공개 관계가 이 자동 태그 매핑을 소유하고 있습니다.")
    mapping_values = {
        "direction": "content_to_knowledge_tag",
        "strength": relation.strength,
        "confidence": relation.confidence,
        "evidence_summary": relation.evidence_summary or "검토 승인된 자동 태그 후보",
        "origin": relation.origin if relation.origin in {"rule", "model_candidate"} else "manual",
        "created_by": relation.created_by or actor_id,
        "approved_at": now,
        "source_relation_id": relation.id,
    }
    if mapping is None:
        mapping_kwargs = {
            "tag_id": tag_id,
            **mapping_values,
            f"{source.entity_type}_id": source_value,
        }
        mapping = mapping_model(**mapping_kwargs)
        db.add(mapping)
    else:
        for key, value in mapping_values.items():
            setattr(mapping, key, value)
    add_mapping_audit(
        db,
        entity_type=source.entity_type,
        entity_id=source.entity_id,
        tag_id=tag_id,
        action="attach",
        actor_id=actor_id,
        snapshot={
            "direction": mapping_values["direction"],
            "strength": float(mapping_values["strength"]),
            "confidence": float(mapping_values["confidence"]),
            "evidence_summary": mapping_values["evidence_summary"],
            "origin": mapping_values["origin"],
            "created_by": str(mapping_values["created_by"]),
            "created_at": mapping.created_at.isoformat() if mapping.created_at else now.isoformat(),
            "approved_at": now.isoformat(),
            "source_relation_id": str(relation.id),
        },
    )
    await db.flush()
    if source.entity_type == "course":
        course = await db.get(Course, source_value)
        await project_rule_tag_mappings(db, entity_type="course", entity=course, actor_id=actor_id)


async def _publish_proposal(
    db: AsyncSession,
    *,
    proposal: KnowledgeRelationProposal,
    actor_id: UUID,
    now: datetime,
    graph_revision: UUID,
) -> KnowledgeRelation:
    """승인된 V2 제안서를 기존 공개 관계 원장의 새 현재 버전으로 만든다."""
    conflict_message = (
        "같은 관계가 이미 등록되어 있습니다. 관계 목록에서 기존 검토 대기 후보를 먼저 승인·반려하세요. "
        "이미 공개된 관계를 변경하려면 해당 공개 관계의 개정을 등록하세요."
    )
    try:
        await _assert_relation_not_duplicated(
            db,
            source_node_id=proposal.source_node_id,
            target_node_id=proposal.target_node_id,
            relation_type=proposal.relation_type,
            is_directed=proposal.is_directed,
            exclude_relation_id=proposal.base_relation_id,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_409_CONFLICT:
            raise HTTPException(status.HTTP_409_CONFLICT, conflict_message) from exc
        raise
    if proposal.base_relation_id:
        base_relation = await _load_relation_for_update_or_404(db, proposal.base_relation_id)
        if base_relation.status != "approved":
            raise HTTPException(status.HTTP_409_CONFLICT, "개정 기준 관계가 더 이상 공개 상태가 아닙니다.")
        before_base = _relation_snapshot(base_relation)
        await _sync_tag_affinity_mapping(db, relation=base_relation, actor_id=actor_id, now=now, attach=False)
        base_relation.status = "retired"
        base_relation.reviewed_by = actor_id
        base_relation.retired_at = now
        db.add(
            KnowledgeRelationReview(
                relation_id=base_relation.id,
                reviewer_id=actor_id,
                action="retired",
                reason="V2 개정 제안서 승인으로 현재 공개본 교체",
                before_snapshot=before_base,
                after_snapshot=_relation_snapshot(base_relation),
            )
        )
        # partial unique index가 같은 key의 새 공개본을 받을 수 있도록 기존
        # 공개본의 상태 변경을 먼저 flush한다. 이후 오류는 전체 트랜잭션 rollback된다.
        await db.flush()
        await _record_projection_outbox(
            db,
            aggregate_type="knowledge_relation",
            aggregate_id=base_relation.id,
            event_type="relation_retired",
            payload={"relation_id": base_relation.id, "proposal_id": proposal.id},
            graph_revision=graph_revision,
        )

    relation = KnowledgeRelation(
        source_node_id=proposal.source_node_id,
        target_node_id=proposal.target_node_id,
        relation_type=proposal.relation_type,
        is_directed=proposal.is_directed,
        strength=proposal.strength,
        confidence=proposal.confidence,
        origin=proposal.origin,
        evidence_summary=proposal.evidence_summary,
        evidence={},
        quality_version=proposal.quality_version,
        status="approved",
        created_by=proposal.created_by or actor_id,
        reviewed_by=actor_id,
        approved_at=now,
        legacy_reference={"v2_proposal_id": str(proposal.id)},
    )
    db.add(relation)
    try:
        await db.flush()
    except IntegrityError as exc:
        if _is_expected_unique_constraint_violation(exc, "uq_knowledge_relation_active_pair"):
            await db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, conflict_message) from exc
        raise
    await _sync_tag_affinity_mapping(
        db,
        relation=relation,
        actor_id=actor_id,
        now=now,
        attach=True,
    )
    db.add(
        KnowledgeRelationReview(
            relation_id=relation.id,
            reviewer_id=actor_id,
            action="approved",
            reason="V2 제안서 승인으로 공개 관계 생성",
            after_snapshot=_relation_snapshot(relation),
        )
    )
    await _record_projection_outbox(
        db,
        aggregate_type="knowledge_relation",
        aggregate_id=relation.id,
        event_type="relation_published",
        payload={"relation_id": relation.id, "proposal_id": proposal.id},
        graph_revision=graph_revision,
    )
    return relation


@router.get("/knowledge-proposals", response_model=KnowledgeProposalListResponse)
async def list_knowledge_proposals(
    proposal_state: str | None = Query(None, alias="state"),
    relation_type: str | None = Query(None),
    origin: str | None = Query(None),
    run_id: UUID | None = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """V2 후보/개정안 목록. 공개 그래프는 이 목록을 직접 소비하지 않는다."""
    conditions = []
    if proposal_state:
        conditions.append(KnowledgeRelationProposal.state == proposal_state)
    if relation_type:
        conditions.append(KnowledgeRelationProposal.relation_type == relation_type)
    if origin:
        conditions.append(KnowledgeRelationProposal.origin == origin)
    if run_id:
        conditions.append(KnowledgeRelationProposal.generation_run_id == run_id)
    query = select(KnowledgeRelationProposal).order_by(
        KnowledgeRelationProposal.updated_at.desc(),
        KnowledgeRelationProposal.id.desc(),
    )
    count_query = select(func.count()).select_from(KnowledgeRelationProposal)
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)
    total = int((await db.execute(count_query)).scalar() or 0)
    proposals = (await db.execute(query.offset(offset).limit(limit))).scalars().all()
    return KnowledgeProposalListResponse(
        items=[await _serialize_proposal(db, proposal) for proposal in proposals],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/knowledge-proposals",
    response_model=KnowledgeRelationProposalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_proposal(
    body: KnowledgeRelationProposalCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """수동 V2 관계 후보를 검토 대기 상태로만 등록한다."""
    source_content = await _resolve_content_ref(db, body.source)
    target_content = await _resolve_content_ref(db, body.target)
    source_node = await ensure_content_node(db, source_content)
    target_node = await ensure_content_node(db, target_content)
    source_node, target_node = _canonicalize_nodes(
        source_node,
        target_node,
        is_directed=body.is_directed,
    )
    if source_node.id == target_node.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "자기 자신을 가리키는 관계는 만들 수 없습니다.")

    base_relation = None
    if body.base_relation_id:
        base_relation = await db.get(KnowledgeRelation, body.base_relation_id)
        if not base_relation:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "개정 기준 공개 관계를 찾을 수 없습니다.")
        if base_relation.status != "approved":
            raise HTTPException(status.HTTP_409_CONFLICT, "공개 상태인 관계만 V2 개정 기준으로 지정할 수 있습니다.")
        if (
            base_relation.source_node_id != source_node.id
            or base_relation.target_node_id != target_node.id
            or base_relation.relation_type != body.relation_type
            or base_relation.is_directed != body.is_directed
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "개정 제안서는 기준 공개 관계와 같은 양 끝점·유형·방향을 유지해야 합니다.",
            )

    await _assert_no_competing_candidate_relation(
        db,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=body.relation_type,
        is_directed=body.is_directed,
    )
    await _assert_v2_proposal_not_duplicated(
        db,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        relation_type=body.relation_type,
        is_directed=body.is_directed,
        base_relation_id=body.base_relation_id,
    )
    # 무방향 관계는 node UUID 순서로 정규화되므로, 프로필도 정규화된 양 끝점과
    # 일치하는 원본에서 다시 읽는다. 그렇지 않으면 profile/node가 엇갈릴 수 있다.
    normalized_source_content = await resolve_node(db, source_node, public_only=False)
    normalized_target_content = await resolve_node(db, target_node, public_only=False)
    if not normalized_source_content or not normalized_target_content:
        raise HTTPException(status.HTTP_409_CONFLICT, "제안서 원본 콘텐츠를 다시 확인할 수 없습니다.")
    source_profile = await _ensure_safe_content_profile(
        db, node=source_node, content=normalized_source_content
    )
    target_profile = await _ensure_safe_content_profile(
        db, node=target_node, content=normalized_target_content
    )
    proposal = KnowledgeRelationProposal(
        base_relation_id=base_relation.id if base_relation else None,
        source_node_id=source_node.id,
        target_node_id=target_node.id,
        source_profile_id=source_profile.id,
        target_profile_id=target_profile.id,
        relation_type=body.relation_type,
        is_directed=body.is_directed,
        strength=body.strength,
        confidence=body.confidence,
        origin=body.origin,
        evidence_summary=body.evidence_summary,
        quality_version=body.quality_version,
        state="pending_review",
        content_revision=combined_profile_revision(source_profile.profile_digest, target_profile.profile_digest),
        created_by=actor_id,
    )
    db.add(proposal)
    try:
        await db.flush()
        for evidence in body.evidence:
            source_evidence_node_id = None
            if evidence.source:
                evidence_content = await _resolve_content_ref(db, evidence.source)
                source_evidence_node_id = (await ensure_content_node(db, evidence_content)).id
            db.add(
                KnowledgeRelationProposalEvidence(
                    proposal_id=proposal.id,
                    evidence_kind=evidence.evidence_kind,
                    source_node_id=source_evidence_node_id,
                    source_digest=evidence.source_digest,
                    locator=evidence.locator,
                    summary=evidence.summary,
                    score_component=evidence.score_component,
                    generator_version=evidence.generator_version,
                )
            )
        db.add(
            KnowledgeRelationProposalReview(
                proposal_id=proposal.id,
                reviewer_id=actor_id,
                action="submitted",
                reason="V2 지식 관계 제안 등록",
                after_snapshot=_proposal_snapshot(proposal),
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_expected_unique_constraint_violation(exc, "uq_knowledge_relation_proposal_active_pair"):
            raise HTTPException(status.HTTP_409_CONFLICT, "같은 관계의 검토 대기 제안서가 이미 존재합니다.") from exc
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "지식 관계 제안 저장 중 무결성 오류가 발생했습니다.",
        ) from exc
    await db.refresh(proposal)
    return await _serialize_proposal(db, proposal)


@router.post("/knowledge-proposals/{proposal_id}/review", response_model=KnowledgeRelationProposalResponse)
async def review_knowledge_proposal(
    proposal_id: UUID,
    body: KnowledgeProposalReviewRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """제안서와 공개본/Outbox를 한 트랜잭션에서 전이한다.

    승인 전에는 공개 관계를 전혀 바꾸지 않는다. 개정안 승인에서는 기존 공개본을
    retired 처리하고 새 공개본을 만든 뒤, 두 이벤트를 같은 commit에 기록한다.
    """
    proposal = await _load_proposal_or_404(db, proposal_id, for_update=True)
    if body.action == "approve" and proposal.created_by and proposal.created_by == actor_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "자신이 등록한 지식 관계 제안서는 승인할 수 없습니다.")
    before_snapshot = _proposal_snapshot(proposal)
    now = datetime.now(timezone.utc)
    # 하나의 승인/은퇴 transaction에서 발생한 모든 outbox 행은 같은 revision으로
    # 묶인다. UI·캐시·향후 읽기 투영 소비자가 중간 상태를 별도 graph 버전처럼
    # 오인하지 않게 한다.
    graph_revision = uuid4()
    try:
        next_state = next_proposal_state(proposal.state, body.action)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    relation = None
    if body.action == "approve":
        await _assert_public_review_endpoints(db, proposal.source_node_id, proposal.target_node_id, actor_id, proposal.relation_type)
        await _assert_no_competing_candidate_relation(
            db,
            source_node_id=proposal.source_node_id,
            target_node_id=proposal.target_node_id,
            relation_type=proposal.relation_type,
            is_directed=proposal.is_directed,
        )
        try:
            relation = await _publish_proposal(
                db,
                proposal=proposal,
                actor_id=actor_id,
                now=now,
                graph_revision=graph_revision,
            )
        except IntegrityError as exc:
            await db.rollback()
            if _is_expected_unique_constraint_violation(exc, "uq_knowledge_relation_active_pair"):
                raise HTTPException(status.HTTP_409_CONFLICT, "같은 공개 관계가 이미 존재합니다.") from exc
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "지식 관계 공개 전환 중 무결성 오류가 발생했습니다.",
            ) from exc
        proposal.published_relation_id = relation.id
        proposal.approved_at = now
        proposal.retired_at = None
        review_action = "approved"
        outbox_event = "proposal_approved"
    elif body.action == "retire":
        if proposal.published_relation_id:
            published = await _load_relation_for_update_or_404(db, proposal.published_relation_id)
            if published.status == "approved":
                before_published = _relation_snapshot(published)
                published.status = "retired"
                published.reviewed_by = actor_id
                published.retired_at = now
                await _sync_tag_affinity_mapping(
                    db,
                    relation=published,
                    actor_id=actor_id,
                    now=now,
                    attach=False,
                )
                db.add(
                    KnowledgeRelationReview(
                        relation_id=published.id,
                        reviewer_id=actor_id,
                        action="retired",
                        reason="V2 제안서 은퇴",
                        before_snapshot=before_published,
                        after_snapshot=_relation_snapshot(published),
                    )
                )
                await _record_projection_outbox(
                    db,
                    aggregate_type="knowledge_relation",
                    aggregate_id=published.id,
                    event_type="relation_retired",
                    payload={"relation_id": published.id, "proposal_id": proposal.id},
                    graph_revision=graph_revision,
                )
        proposal.retired_at = now
        review_action = "retired"
        outbox_event = "proposal_retired"
    else:
        proposal.approved_at = None
        review_action = "rejected"
        outbox_event = "proposal_rejected"

    proposal.state = next_state
    proposal.reviewed_by = actor_id
    db.add(
        KnowledgeRelationProposalReview(
            proposal_id=proposal.id,
            reviewer_id=actor_id,
            action=review_action,
            reason=body.reason,
            before_snapshot=before_snapshot,
            after_snapshot=_proposal_snapshot(proposal),
        )
    )
    await _record_projection_outbox(
        db,
        aggregate_type="knowledge_relation_proposal",
        aggregate_id=proposal.id,
        event_type=outbox_event,
        payload={"proposal_id": proposal.id, "relation_id": relation.id if relation else proposal.published_relation_id},
        graph_revision=graph_revision,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_expected_unique_constraint_violation(exc, "uq_knowledge_relation_active_pair"):
            raise HTTPException(status.HTTP_409_CONFLICT, "같은 공개 관계가 이미 존재합니다.") from exc
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "지식 관계 공개 전환 중 무결성 오류가 발생했습니다.",
        ) from exc
    await db.refresh(proposal)
    return await _serialize_proposal(db, proposal)


class _CandidateGenerationDuplicate(RuntimeError):
    """후보별 savepoint만 취소하고 실행 전체는 계속하기 위한 내부 신호."""


@router.post("/knowledge-proposals/bulk-review")
async def bulk_review_knowledge_proposals(
    body: KnowledgeProposalBulkReviewRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """각 항목을 독립 commit/rollback하고 검토 결과를 모두 반환한다."""
    items = []
    for proposal_id in body.proposal_ids:
        try:
            proposal = await review_knowledge_proposal(
                proposal_id, KnowledgeProposalReviewRequest(action=body.action, reason=body.reason),
                db=db, actor_id=actor_id, _role=_role,
            )
            items.append({"proposal_id": proposal_id, "status": "succeeded", "http_status": 200,
                          "proposal": proposal})
        except HTTPException as exc:
            await db.rollback()
            items.append({"proposal_id": proposal_id, "status": "failed", "http_status": exc.status_code,
                          "error": exc.detail})
        except Exception:
            await db.rollback()
            items.append({"proposal_id": proposal_id, "status": "failed", "http_status": 503,
                          "error": "제안서를 검토할 수 없습니다. 현재 상태를 다시 확인하세요."})
    return {"items": items}


def _is_expected_candidate_proposal_duplicate(exc: IntegrityError) -> bool:
    """동시 생성으로 인한 활성 proposal unique 충돌만 중복으로 분류한다."""
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = (
        getattr(diagnostic, "constraint_name", None)
        or getattr(original, "constraint_name", None)
    )
    return constraint_name == "uq_knowledge_relation_proposal_active_pair"


def _candidate_content_key(item: CandidateContentInput) -> tuple[str, str]:
    return item.content.entity_type, item.content.entity_id


async def _candidate_duplicate_index(
    db: AsyncSession,
    nodes_by_content_key: dict[tuple[str, str], KnowledgeContentNode],
) -> tuple[set[tuple[UUID, UUID]], set[tuple[UUID, UUID, str]]]:
    """기존 수동/승인 관계를 보존하기 위한 읽기 전용 중복 인덱스.

    첫 집합은 활성 수동 관계 또는 활성 V2 제안으로 막아야 하는 source/target 쌍이고,
    두 번째 집합은 같은 profile revision에서 이미 반려·은퇴된 규칙 제안이다.
    """
    node_ids = [node.id for node in nodes_by_content_key.values()]
    if not node_ids:
        return set(), set()
    relation_rows = await db.execute(
        select(KnowledgeRelation.source_node_id, KnowledgeRelation.target_node_id).where(
            KnowledgeRelation.source_node_id.in_(node_ids),
            KnowledgeRelation.target_node_id.in_(node_ids),
            KnowledgeRelation.is_directed.is_(True),
            KnowledgeRelation.relation_type.in_(_CANDIDATE_BLOCKING_RELATION_TYPES),
            KnowledgeRelation.status.in_(_ACTIVE_RELATION_STATUSES),
        )
    )
    blocking_pairs = set(relation_rows.all())
    proposal_rows = await db.execute(
        select(
            KnowledgeRelationProposal.source_node_id,
            KnowledgeRelationProposal.target_node_id,
            KnowledgeRelationProposal.relation_type,
            KnowledgeRelationProposal.state,
            KnowledgeRelationProposal.content_revision,
        ).where(
            KnowledgeRelationProposal.source_node_id.in_(node_ids),
            KnowledgeRelationProposal.target_node_id.in_(node_ids),
            KnowledgeRelationProposal.is_directed.is_(True),
            KnowledgeRelationProposal.relation_type.in_(_CANDIDATE_BLOCKING_RELATION_TYPES),
        )
    )
    same_revision_history: set[tuple[UUID, UUID, str]] = set()
    for source_node_id, target_node_id, relation_type, proposal_state, content_revision in proposal_rows.all():
        if proposal_state in _V2_ACTIVE_PROPOSAL_STATES:
            blocking_pairs.add((source_node_id, target_node_id))
        elif relation_type == "prepares_for" and proposal_state in {"rejected", "retired"}:
            same_revision_history.add((source_node_id, target_node_id, content_revision))
    return blocking_pairs, same_revision_history


async def _candidate_pair_is_currently_blocked(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    content_revision: str,
) -> bool:
    """저장 직전에도 중복을 다시 점검해 수동 변경과의 경합을 줄인다."""
    relation = await db.execute(
        select(KnowledgeRelation.id).where(
            KnowledgeRelation.source_node_id == source_node_id,
            KnowledgeRelation.target_node_id == target_node_id,
            KnowledgeRelation.is_directed.is_(True),
            KnowledgeRelation.relation_type.in_(_CANDIDATE_BLOCKING_RELATION_TYPES),
            KnowledgeRelation.status.in_(_ACTIVE_RELATION_STATUSES),
        )
    )
    if relation.scalar_one_or_none():
        return True
    proposal = await db.execute(
        select(
            KnowledgeRelationProposal.state,
            KnowledgeRelationProposal.relation_type,
            KnowledgeRelationProposal.content_revision,
        ).where(
            KnowledgeRelationProposal.source_node_id == source_node_id,
            KnowledgeRelationProposal.target_node_id == target_node_id,
            KnowledgeRelationProposal.is_directed.is_(True),
            KnowledgeRelationProposal.relation_type.in_(_CANDIDATE_BLOCKING_RELATION_TYPES),
        )
    )
    for proposal_state, relation_type, existing_revision in proposal.all():
        if proposal_state in _V2_ACTIVE_PROPOSAL_STATES:
            return True
        if (
            relation_type == "prepares_for"
            and proposal_state in {"rejected", "retired"}
            and existing_revision == content_revision
        ):
            return True
    return False


def _candidate_preview(
    candidate,
    inputs_by_content_key: dict[tuple[str, str], CandidateContentInput],
) -> KnowledgeCandidatePreview:
    source = inputs_by_content_key[(candidate.source_entity_type, candidate.source_entity_id)]
    target = inputs_by_content_key[(candidate.target_entity_type, candidate.target_entity_id)]
    return KnowledgeCandidatePreview(
        source=_summary_from_content(source.content),
        target=_summary_from_content(target.content),
        relation_type="prepares_for",
        strength=candidate.strength,
        confidence=candidate.confidence,
        evidence_summary=candidate.evidence_summary,
        score_component=candidate.score_component,
    )


def _candidate_run_item_snapshot(candidate) -> dict:
    """후보 결과를 재현할 최소 identity/digest만 실행 원장에 남긴다."""
    return {
        "source": {
            "entity_type": candidate.source_entity_type,
            "entity_id": candidate.source_entity_id,
            "profile_digest": candidate.source_profile_digest,
        },
        "target": {
            "entity_type": candidate.target_entity_type,
            "entity_id": candidate.target_entity_id,
            "profile_digest": candidate.target_profile_digest,
        },
        "relation_type": candidate.relation_type,
        "content_revision": candidate.content_revision,
    }


async def _record_candidate_run_failure(
    db: AsyncSession,
    *,
    actor_id: UUID,
    configuration_digest: str,
    input_snapshot_digest: str,
    input_count: int,
    error_count: int,
    is_dry_run: bool,
    execution_options: dict,
    input_snapshot: list[dict],
) -> KnowledgeCandidateRun:
    """후보 저장 트랜잭션이 실패해도 비밀값 없이 실패 원장을 남긴다."""
    now = datetime.now(timezone.utc)
    failed_run = KnowledgeCandidateRun(
        run_kind="rule",
        algorithm_version=DETERMINISTIC_TAG_PREPARES_FOR_V1,
        configuration_digest=configuration_digest,
        input_snapshot_digest=input_snapshot_digest,
        execution_options=execution_options,
        input_snapshot=input_snapshot,
        requested_by=actor_id,
        state="failed",
        is_dry_run=is_dry_run,
        started_at=now,
        finished_at=now,
        input_count=input_count,
        candidate_count_before_limit=0,
        preview_truncated=False,
        output_count=0,
        duplicate_count=0,
        error_count=max(1, error_count),
        error_code="candidate_generation_failed",
    )
    db.add(failed_run)
    await db.commit()
    await db.refresh(failed_run)
    return failed_run


async def _generate_knowledge_candidates_legacy(
    body: KnowledgeCandidateGenerationRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    # The public route delegates only to the ledger-aware service below.  Keep
    # this former private entrypoint inert so a future accidental call cannot
    # bypass the P1 snapshot, transaction, and commit-outcome safeguards.
    raise RuntimeError("legacy candidate generation entrypoint is retired")
    """공개 메타데이터만으로 pending_review 후보를 생성하거나 미리본다.

    이 엔드포인트는 `KnowledgeRelation`이나 outbox를 직접 만들지 않는다. 실제 실행도
    기존 수동 관계를 바꾸지 않고, 다른 최고 관리자의 승인 API를 통과해야만 공개 그래프에
    반영된다.
    """
    execution_options = {"max_candidates": body.max_candidates}
    configuration_digest = digest_json(
        {
            "algorithm": deterministic_tag_prepares_for_configuration(),
            "execution_options": execution_options,
        }
    )
    input_snapshot_digest = digest_json([])
    input_snapshot: list[dict] = []
    input_count = 0
    collection_error_count = 0
    try:
        collection = await collect_public_tag_candidate_inputs(db)
        all_inputs = [*collection.sources, *collection.targets]
        input_count = len(all_inputs)
        collection_error_count = collection.error_count
        input_snapshot = candidate_input_snapshot(all_inputs)
        input_snapshot_digest = digest_json(input_snapshot)
        candidates, preview_truncated, candidate_count_before_limit = build_deterministic_tag_prepares_for_candidates(
            [item.profile for item in collection.sources],
            [item.profile for item in collection.targets],
            max_candidates=body.max_candidates,
        )
        inputs_by_content_key = {_candidate_content_key(item): item for item in all_inputs}
        nodes_by_content_key = await existing_content_nodes(db, all_inputs)
        blocking_pairs, same_revision_history = await _candidate_duplicate_index(db, nodes_by_content_key)

        pending_candidates = []
        precheck_skipped = []
        duplicate_count = 0
        for rank, candidate in enumerate(candidates, start=1):
            source_node = nodes_by_content_key.get((candidate.source_entity_type, candidate.source_entity_id))
            target_node = nodes_by_content_key.get((candidate.target_entity_type, candidate.target_entity_id))
            if source_node and target_node:
                pair = (source_node.id, target_node.id)
                if pair in blocking_pairs or (*pair, candidate.content_revision) in same_revision_history:
                    duplicate_count += 1
                    precheck_skipped.append(
                        (
                            rank,
                            candidate,
                            "existing_skipped" if pair in blocking_pairs else "rejected_precheck",
                        )
                    )
                    continue
            pending_candidates.append((rank, candidate))

        now = datetime.now(timezone.utc)
        run = KnowledgeCandidateRun(
            run_kind="rule",
            algorithm_version=DETERMINISTIC_TAG_PREPARES_FOR_V1,
            configuration_digest=configuration_digest,
            input_snapshot_digest=input_snapshot_digest,
            execution_options=execution_options,
            input_snapshot=input_snapshot,
            requested_by=actor_id,
            state="running",
            is_dry_run=body.dry_run,
            started_at=now,
            input_count=input_count,
            candidate_count_before_limit=candidate_count_before_limit,
            preview_truncated=preview_truncated,
            output_count=0,
            duplicate_count=duplicate_count,
            error_count=collection_error_count,
        )
        db.add(run)
        await db.flush()
        for rank, candidate, outcome in precheck_skipped:
            db.add(
                KnowledgeCandidateRunItem(
                    run_id=run.id,
                    proposal_id=None,
                    rank=rank,
                    raw_score=candidate.strength,
                    dedupe_outcome=outcome,
                    candidate_snapshot=_candidate_run_item_snapshot(candidate),
                )
            )

        if not body.dry_run:
            created_count = 0
            created_candidates = []
            for rank, candidate in pending_candidates:
                source_input = inputs_by_content_key[(candidate.source_entity_type, candidate.source_entity_id)]
                target_input = inputs_by_content_key[(candidate.target_entity_type, candidate.target_entity_id)]
                try:
                    async with db.begin_nested():
                        source_node, source_profile = await ensure_candidate_node_and_profile(db, source_input)
                        target_node, target_profile = await ensure_candidate_node_and_profile(db, target_input)
                        await _lock_candidate_relation_pair(
                            db,
                            source_node_id=source_node.id,
                            target_node_id=target_node.id,
                            relation_type="prepares_for",
                            is_directed=True,
                        )
                        if (
                            source_profile.profile_digest != candidate.source_profile_digest
                            or target_profile.profile_digest != candidate.target_profile_digest
                        ):
                            raise RuntimeError("candidate_profile_digest_mismatch")
                        if await _candidate_pair_is_currently_blocked(
                            db,
                            source_node_id=source_node.id,
                            target_node_id=target_node.id,
                            content_revision=candidate.content_revision,
                        ):
                            raise _CandidateGenerationDuplicate()
                        proposal = KnowledgeRelationProposal(
                            source_node_id=source_node.id,
                            target_node_id=target_node.id,
                            source_profile_id=source_profile.id,
                            target_profile_id=target_profile.id,
                            relation_type="prepares_for",
                            is_directed=True,
                            strength=candidate.strength,
                            confidence=candidate.confidence,
                            origin="rule",
                            evidence_summary=candidate.evidence_summary,
                            quality_version=DETERMINISTIC_TAG_PREPARES_FOR_V1,
                            generation_run_id=run.id,
                            state="pending_review",
                            content_revision=candidate.content_revision,
                            created_by=actor_id,
                        )
                        db.add(proposal)
                        await db.flush()
                        db.add(
                            KnowledgeRelationProposalEvidence(
                                proposal_id=proposal.id,
                                evidence_kind="rule",
                                source_node_id=source_node.id,
                                source_digest=candidate.source_profile_digest,
                                locator=(
                                    f"knowledge-profile:{candidate.source_entity_type}:"
                                    f"{candidate.source_entity_id}"
                                ),
                                summary=candidate.evidence_summary,
                                score_component=candidate.score_component,
                                generator_version=DETERMINISTIC_TAG_PREPARES_FOR_V1,
                            )
                        )
                        db.add(
                            KnowledgeRelationProposalReview(
                                proposal_id=proposal.id,
                                reviewer_id=actor_id,
                                action="submitted",
                                reason="규칙 기반 지식 관계 후보 생성",
                                after_snapshot=_proposal_snapshot(proposal),
                            )
                        )
                        db.add(
                            KnowledgeCandidateRunItem(
                                run_id=run.id,
                                proposal_id=proposal.id,
                                rank=rank,
                                raw_score=candidate.strength,
                                dedupe_outcome="created",
                                candidate_snapshot=_candidate_run_item_snapshot(candidate),
                            )
                        )
                        await db.flush()
                    created_count += 1
                    created_candidates.append(candidate)
                except _CandidateGenerationDuplicate:
                    duplicate_count += 1
                    db.add(
                        KnowledgeCandidateRunItem(
                            run_id=run.id,
                            proposal_id=None,
                            rank=rank,
                            raw_score=candidate.strength,
                            dedupe_outcome="existing_skipped",
                            candidate_snapshot=_candidate_run_item_snapshot(candidate),
                        )
                    )
                except IntegrityError as exc:
                    if _is_expected_candidate_proposal_duplicate(exc):
                        duplicate_count += 1
                        db.add(
                            KnowledgeCandidateRunItem(
                                run_id=run.id,
                                proposal_id=None,
                                rank=rank,
                                raw_score=candidate.strength,
                                dedupe_outcome="existing_skipped",
                                candidate_snapshot=_candidate_run_item_snapshot(candidate),
                            )
                        )
                    else:
                        raise
            run.output_count = created_count
            response_candidates = created_candidates
        else:
            run.output_count = len(pending_candidates)
            response_candidates = [candidate for _rank, candidate in pending_candidates]
            for rank, candidate in pending_candidates:
                db.add(
                    KnowledgeCandidateRunItem(
                        run_id=run.id,
                        proposal_id=None,
                        rank=rank,
                        raw_score=candidate.strength,
                        dedupe_outcome="preview_only",
                        candidate_snapshot=_candidate_run_item_snapshot(candidate),
                    )
                )

        run.duplicate_count = duplicate_count
        run.error_count = collection_error_count
        run.state = "succeeded"
        run.finished_at = datetime.now(timezone.utc)
        response = KnowledgeCandidateGenerationResponse(
            run=KnowledgeCandidateRunResponse.model_validate(run),
            preview=[_candidate_preview(candidate, inputs_by_content_key) for candidate in response_candidates],
            preview_truncated=preview_truncated,
        )
        await db.commit()
        return response
    except Exception as exc:
        await db.rollback()
        # Validation and storage details may include identifiers from a private row.
        # Keep the API error fixed and preserve only the safe failure code in the ledger.
        await _record_candidate_run_failure(
            db,
            actor_id=actor_id,
            configuration_digest=configuration_digest,
            input_snapshot_digest=input_snapshot_digest,
            input_count=input_count,
            error_count=collection_error_count + 1,
            is_dry_run=body.dry_run,
            execution_options=execution_options,
            input_snapshot=input_snapshot,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "지식 관계 후보 생성에 실패했습니다. 실행 원장을 확인하세요.",
        ) from exc


@router.post(
    "/knowledge-candidate-runs/generate",
    response_model=KnowledgeCandidateGenerationResponse,
)
async def generate_knowledge_candidates(
    body: KnowledgeCandidateGenerationRequest,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """기존 관리자 수동 실행을 공용 후보 생성 서비스로 위임한다.

    수동 호출의 실행자 UUID와 ``manual`` trigger는 원장에 보존한다. 실제 실행도
    공개 관계가 아니라 ``pending_review`` proposal만 생성한다.
    """
    try:
        result = await generate_rule_based_candidate_run(
            db,
            invocation=CandidateGenerationInvocation(
                dry_run=body.dry_run,
                max_candidates=body.max_candidates,
                trigger_kind=MANUAL_TRIGGER_KIND,
                execution_principal=MANUAL_EXECUTION_PRINCIPAL,
                requested_by=actor_id,
            ),
        )
    except CandidateGenerationServiceError:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "지식 관계 후보 생성이 실패했습니다. 실행 원장을 확인하세요.",
        ) from None
    return KnowledgeCandidateGenerationResponse(
        run=KnowledgeCandidateRunResponse.model_validate(result.run),
        preview=result.preview,
        preview_truncated=bool(result.run.preview_truncated),
    )


@router.get(
    "/knowledge-candidate-runner/status",
    response_model=KnowledgeCandidateRunnerStatusResponse,
)
async def get_knowledge_candidate_runner_status(
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """안전한 scheduled runner 구성값과 마지막 원장 상태를 조회한다.

    이 응답은 API 컨테이너가 읽은 설정과 DB 원장을 보여 줄 뿐, runner 프로세스의
    실제 생존 여부나 다음 실행 성공을 보장하지 않는다.
    """
    last_run = (
        await db.execute(
            select(KnowledgeCandidateRun)
            .where(KnowledgeCandidateRun.trigger_kind == "scheduled")
            .order_by(KnowledgeCandidateRun.created_at.desc(), KnowledgeCandidateRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    try:
        runner_settings = scheduled_candidate_runner_settings_from_environment()
    except CandidateRunnerConfigurationError:
        return KnowledgeCandidateRunnerStatusResponse(
            configuration_valid=False,
            enabled=False,
            interval_seconds=None,
            max_candidates=None,
            execution_principal=None,
            last_run=(KnowledgeCandidateRunResponse.model_validate(last_run) if last_run else None),
        )
    return KnowledgeCandidateRunnerStatusResponse(
        configuration_valid=True,
        enabled=runner_settings.enabled,
        interval_seconds=runner_settings.interval_seconds,
        max_candidates=runner_settings.max_candidates,
        execution_principal=runner_settings.execution_principal,
        last_run=(KnowledgeCandidateRunResponse.model_validate(last_run) if last_run else None),
    )


@router.get("/knowledge-candidate-runs", response_model=KnowledgeCandidateRunListResponse)
async def list_knowledge_candidate_runs(
    run_state: str | None = Query(None, alias="state"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """후보 실행 원장을 조회한다. 실행 원장은 공개 그래프 권한이 아니다."""
    query = select(KnowledgeCandidateRun).order_by(
        KnowledgeCandidateRun.created_at.desc(),
        KnowledgeCandidateRun.id.desc(),
    )
    count_query = select(func.count()).select_from(KnowledgeCandidateRun)
    if run_state:
        query = query.where(KnowledgeCandidateRun.state == run_state)
        count_query = count_query.where(KnowledgeCandidateRun.state == run_state)
    total = int((await db.execute(count_query)).scalar() or 0)
    runs = (await db.execute(query.offset(offset).limit(limit))).scalars().all()
    return KnowledgeCandidateRunListResponse(
        items=[KnowledgeCandidateRunResponse.model_validate(run) for run in runs],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/knowledge-candidate-runs",
    response_model=KnowledgeCandidateRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_knowledge_candidate_dry_run(
    body: KnowledgeCandidateRunCreate,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """실제 worker 실행 전 재현 가능한 dry-run 메타데이터만 등록한다.

    이 수직 슬라이스에는 모델 호출·대량 임베딩 worker를 넣지 않는다. 따라서 이
    API는 후보를 만들거나 승인하지 않으며, 입력/알고리즘 version audit을 위한
    성공한 빈 dry-run 원장만 남긴다.
    """
    now = datetime.now(timezone.utc)
    run = KnowledgeCandidateRun(
        run_kind=body.run_kind,
        algorithm_version=body.algorithm_version,
        configuration_digest=body.configuration_digest,
        input_snapshot_digest=body.input_snapshot_digest,
        execution_options={},
        input_snapshot=[],
        requested_by=actor_id,
        state="succeeded",
        is_dry_run=True,
        started_at=now,
        finished_at=now,
        input_count=0,
        candidate_count_before_limit=0,
        preview_truncated=False,
        output_count=0,
        duplicate_count=0,
        error_count=0,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return KnowledgeCandidateRunResponse.model_validate(run)
