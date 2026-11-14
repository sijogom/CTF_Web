"""규칙 기반 지식 관계 후보 생성의 공용 실행 서비스.

관리자 API와 주기 worker는 이 모듈의 같은 진입점을 사용한다. 후보 생성은
``pending_review`` proposal만 만들며, 공개 관계, outbox, 문제 잠금, 점수, 기존
선행 조건을 직접 변경하지 않는다.

원장은 안전한 allowlist snapshot과 digest만 저장한다. 본문, 플래그, 개인정보,
예외 원문은 이 모듈의 결과나 오류 코드에 포함하지 않는다.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import (
    KnowledgeCandidateRun,
    KnowledgeCandidateRunItem,
    KnowledgeContentNode,
    KnowledgeRelation,
    KnowledgeRelationProposal,
    KnowledgeRelationProposalEvidence,
    KnowledgeRelationProposalReview,
)
from app.schemas.knowledge import (
    KnowledgeCandidatePreview,
    KnowledgeContentSummary,
)
from app.utils.knowledge_candidate_generation import (
    CANDIDATE_INPUT_SNAPSHOT_SCHEMA_VERSION,
    CandidateContentInput,
    candidate_input_snapshot,
    collect_public_tag_candidate_inputs,
    ensure_candidate_node_and_profile,
    existing_content_nodes,
)
from app.utils.knowledge_mapping_v2 import (
    build_deterministic_content_tag_affinity_candidates,
    DETERMINISTIC_ASSIGNMENT_RESOURCE_HELPS_V1,
    DETERMINISTIC_CANDIDATE_RULESET_V2,
    deterministic_assignment_resource_helps_candidate,
    deterministic_content_tag_affinity_configuration,
    build_deterministic_tag_prepares_for_candidates,
    deterministic_tag_prepares_for_configuration,
    digest_json,
)


MANUAL_TRIGGER_KIND = "manual"
SCHEDULED_TRIGGER_KIND = "scheduled"
MANUAL_EXECUTION_PRINCIPAL = "admin_api"
_SCHEDULED_RUNNER_LOCK_NAMESPACE = "knowledge-candidate-runner-v1"
_ACTIVE_RELATION_STATUSES = ("draft", "pending_review", "approved")
_ACTIVE_PROPOSAL_STATES = ("draft", "pending_review")
_CANDIDATE_BLOCKING_RELATION_TYPES = ("requires", "prepares_for", "helps", "tag_affinity")
_EXECUTION_PRINCIPAL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_RUNNING_CANDIDATE_RUN_RECOVERY_AFTER = timedelta(minutes=30)


class CandidateGenerationServiceError(RuntimeError):
    """호출자에게 노출해도 안전한 고정 오류 코드만 전달한다."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CandidateRunnerConfigurationError(ValueError):
    """비밀값을 포함하지 않는 worker 설정 검증 오류."""


class _CandidateGenerationDuplicate(RuntimeError):
    """후보 하나의 savepoint만 되돌리고 실행 전체는 계속하기 위한 내부 신호."""


@dataclass(frozen=True)
class CandidateGenerationInvocation:
    """API와 scheduled worker가 공통으로 전달하는 최소 실행 문맥."""

    dry_run: bool
    max_candidates: int
    trigger_kind: Literal["manual", "scheduled"]
    execution_principal: str
    requested_by: UUID | None


@dataclass(frozen=True)
class CandidateGenerationResult:
    """공용 실행 결과. preview는 API 직렬화 전용의 안전한 요약이다."""

    run: KnowledgeCandidateRun
    preview: list[KnowledgeCandidatePreview]
    preview_truncated: bool
    reused_existing_run: bool


@dataclass(frozen=True)
class ScheduledCandidateRunnerSettings:
    """환경 변수에서 읽는 최소 scheduled runner 설정.

    API와 worker가 같은 파서를 사용하므로, 관리자 상태 API가 worker와 다른 기본값을
    보여 주지 않는다. 이 값은 구성 상태이며 컨테이너 생존/실행 성공 증명은 아니다.
    """

    enabled: bool
    interval_seconds: int
    max_candidates: int
    execution_principal: str

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "ScheduledCandidateRunnerSettings":
        values = os.environ if environ is None else environ
        execution_principal = values.get(
            "KNOWLEDGE_CANDIDATE_RUNNER_ID",
            "knowledge-candidate-runner",
        ).strip()
        if not _EXECUTION_PRINCIPAL_PATTERN.fullmatch(execution_principal):
            raise CandidateRunnerConfigurationError("invalid runner identity")
        return cls(
            enabled=_read_boolean_setting(
                values,
                "KNOWLEDGE_CANDIDATE_RUNNER_ENABLED",
                default=False,
            ),
            interval_seconds=_read_bounded_integer_setting(
                values,
                "KNOWLEDGE_CANDIDATE_RUNNER_INTERVAL_SECONDS",
                default=900,
                minimum=60,
                maximum=86_400,
            ),
            max_candidates=_read_bounded_integer_setting(
                values,
                "KNOWLEDGE_CANDIDATE_RUNNER_MAX_CANDIDATES",
                default=100,
                minimum=1,
                maximum=500,
            ),
            execution_principal=execution_principal,
        )


def _read_boolean_setting(values: Mapping[str, str], setting_name: str, *, default: bool) -> bool:
    raw_value = values.get(setting_name, str(default)).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise CandidateRunnerConfigurationError(f"{setting_name} is invalid")


def _read_bounded_integer_setting(
    values: Mapping[str, str],
    setting_name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = values.get(setting_name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise CandidateRunnerConfigurationError(f"{setting_name} is invalid") from exc
    if not minimum <= value <= maximum:
        raise CandidateRunnerConfigurationError(f"{setting_name} is invalid")
    return value


def scheduled_candidate_runner_settings_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ScheduledCandidateRunnerSettings:
    """worker/API가 공유하는 보수적 운영 설정을 읽는다."""
    return ScheduledCandidateRunnerSettings.from_environment(environ)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_recoverable_running_candidate_run(
    run: KnowledgeCandidateRun,
    *,
    now: datetime | None = None,
) -> bool:
    """비정상 종료 뒤 남은 오래된 running 원장만 다음 실행이 회복한다.

    아직 실행 중일 수 있는 원장을 즉시 덮어쓰지 않는다. ``started_at``이 없거나
    시간대 정보가 없는 레거시 행도 안전을 위해 회복 대상으로 간주하지 않는다.
    """
    if run.state != "running" or run.started_at is None or run.started_at.tzinfo is None:
        return False
    reference_time = now or _utc_now()
    return run.started_at <= reference_time - _RUNNING_CANDIDATE_RUN_RECOVERY_AFTER


def _candidate_pair_advisory_lock_key(source_node_id: UUID, target_node_id: UUID) -> int:
    payload = f"knowledge-relation-pair-v1:{source_node_id}:{target_node_id}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big", signed=True)


def _scheduled_runner_advisory_lock_key() -> int:
    payload = _SCHEDULED_RUNNER_LOCK_NAMESPACE.encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big", signed=True)


async def try_acquire_scheduled_candidate_runner_lock(db: AsyncSession) -> bool:
    """PostgreSQL 세션 잠금으로 여러 runner 컨테이너의 동시 실행을 막는다."""
    result = await db.execute(select(func.pg_try_advisory_lock(_scheduled_runner_advisory_lock_key())))
    return bool(result.scalar_one())


async def release_scheduled_candidate_runner_lock(db: AsyncSession) -> None:
    """정상 경로에서 세션 잠금을 즉시 해제한다.

    세션이 닫혀도 PostgreSQL이 잠금을 해제하므로, 이 함수 실패는 worker가 예외 원문을
    기록하지 않고 세션 정리로 넘어갈 수 있다.
    """
    result = await db.execute(select(func.pg_advisory_unlock(_scheduled_runner_advisory_lock_key())))
    if not bool(result.scalar_one()):
        raise RuntimeError("scheduled runner lock was not held by the bound connection")
    await db.commit()


def _validate_invocation(invocation: CandidateGenerationInvocation) -> None:
    if not 1 <= invocation.max_candidates <= 500:
        raise CandidateGenerationServiceError("candidate_generation_invalid_options")
    if not _EXECUTION_PRINCIPAL_PATTERN.fullmatch(invocation.execution_principal):
        raise CandidateGenerationServiceError("candidate_generation_invalid_principal")
    if invocation.trigger_kind == MANUAL_TRIGGER_KIND:
        if invocation.requested_by is None:
            raise CandidateGenerationServiceError("candidate_generation_manual_actor_required")
        return
    if invocation.trigger_kind == SCHEDULED_TRIGGER_KIND and invocation.requested_by is None:
        return
    raise CandidateGenerationServiceError("candidate_generation_invalid_trigger")


def _execution_options(invocation: CandidateGenerationInvocation) -> dict[str, object]:
    """재현과 idempotency에 필요한 비밀 없는 실행 옵션만 만든다."""
    return {
        "max_candidates": invocation.max_candidates,
        "dry_run": invocation.dry_run,
        "trigger_kind": invocation.trigger_kind,
        "execution_principal": invocation.execution_principal,
    }


def _generation_configuration_options(invocation: CandidateGenerationInvocation) -> dict[str, object]:
    """후보 결과에 영향을 주는 옵션만 configuration digest에 넣는다.

    runner principal은 감사용 실행 주체이지 후보 생성 규칙이 아니므로, 컨테이너 이름이
    바뀌어도 같은 scheduled 설정/snapshot 원장을 다시 만들지 않는다.
    """
    return {
        "max_candidates": invocation.max_candidates,
        "dry_run": invocation.dry_run,
        "trigger_kind": invocation.trigger_kind,
    }


def _run_deduplication_key(configuration_digest: str, input_snapshot_digest: str) -> str:
    """동일 실행 설정과 안전 snapshot의 단일 원장 키를 만든다."""
    return digest_json(
        {
            "configuration_digest": configuration_digest,
            "input_snapshot_digest": input_snapshot_digest,
        }
    )


async def _lock_candidate_relation_pair(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    relation_type: str,
    is_directed: bool,
) -> None:
    if not is_directed or relation_type not in _CANDIDATE_BLOCKING_RELATION_TYPES:
        return
    await db.execute(
        select(func.pg_advisory_xact_lock(_candidate_pair_advisory_lock_key(source_node_id, target_node_id)))
    )


def _is_expected_candidate_proposal_duplicate(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = (
        getattr(diagnostic, "constraint_name", None)
        or getattr(original, "constraint_name", None)
    )
    return constraint_name == "uq_knowledge_relation_proposal_active_pair"


def _is_expected_candidate_run_duplicate(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)
    constraint_name = (
        getattr(diagnostic, "constraint_name", None)
        or getattr(original, "constraint_name", None)
    )
    return constraint_name == "uq_knowledge_candidate_runs_deduplication_key"


def _candidate_content_key(item: CandidateContentInput) -> tuple[str, str]:
    return item.content.entity_type, item.content.entity_id


async def _candidate_duplicate_index(
    db: AsyncSession,
    nodes_by_content_key: dict[tuple[str, str], KnowledgeContentNode],
) -> tuple[set[tuple[UUID, UUID, str]], set[tuple[UUID, UUID, str, str]]]:
    """수동/승인 관계를 보존하는 읽기 전용 중복 인덱스."""
    node_ids = [node.id for node in nodes_by_content_key.values()]
    if not node_ids:
        return set(), set()
    relation_rows = await db.execute(
        select(
            KnowledgeRelation.source_node_id,
            KnowledgeRelation.target_node_id,
            KnowledgeRelation.relation_type,
        ).where(
            KnowledgeRelation.source_node_id.in_(node_ids),
            KnowledgeRelation.target_node_id.in_(node_ids),
            KnowledgeRelation.is_directed.is_(True),
            KnowledgeRelation.relation_type.in_(_CANDIDATE_BLOCKING_RELATION_TYPES),
            KnowledgeRelation.status.in_(_ACTIVE_RELATION_STATUSES),
        )
    )
    blocking_pairs = {
        (source_node_id, target_node_id, relation_type)
        for source_node_id, target_node_id, relation_type in relation_rows.all()
    }
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
    same_revision_history: set[tuple[UUID, UUID, str, str]] = set()
    for source_node_id, target_node_id, relation_type, proposal_state, content_revision in proposal_rows.all():
        if proposal_state in _ACTIVE_PROPOSAL_STATES:
            blocking_pairs.add((source_node_id, target_node_id, relation_type))
        elif relation_type in _CANDIDATE_BLOCKING_RELATION_TYPES and proposal_state in {"rejected", "retired"}:
            same_revision_history.add((source_node_id, target_node_id, relation_type, content_revision))
    return blocking_pairs, same_revision_history


async def _candidate_pair_is_currently_blocked(
    db: AsyncSession,
    *,
    source_node_id: UUID,
    target_node_id: UUID,
    relation_type: str,
    content_revision: str,
) -> bool:
    relation = await db.execute(
        select(KnowledgeRelation.id).where(
            KnowledgeRelation.source_node_id == source_node_id,
            KnowledgeRelation.target_node_id == target_node_id,
            KnowledgeRelation.is_directed.is_(True),
            KnowledgeRelation.relation_type == relation_type,
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
            KnowledgeRelationProposal.relation_type == relation_type,
        )
    )
    for proposal_state, relation_type, existing_revision in proposal.all():
        if proposal_state in _ACTIVE_PROPOSAL_STATES:
            return True
        if (
            relation_type in _CANDIDATE_BLOCKING_RELATION_TYPES
            and proposal_state in {"rejected", "retired"}
            and existing_revision == content_revision
        ):
            return True
    return False


def _summary_from_content(item: CandidateContentInput) -> KnowledgeContentSummary:
    content = item.content
    return KnowledgeContentSummary(
        type=content.entity_type,
        id=content.entity_id,
        name=content.display_name,
        is_active=content.is_active,
        href=content.href,
        course_id=content.course_id,
    )


def _candidate_preview(
    candidate: object,
    inputs_by_content_key: dict[tuple[str, str], CandidateContentInput],
) -> KnowledgeCandidatePreview:
    source = inputs_by_content_key[(candidate.source_entity_type, candidate.source_entity_id)]
    target = inputs_by_content_key[(candidate.target_entity_type, candidate.target_entity_id)]
    return KnowledgeCandidatePreview(
        source=_summary_from_content(source),
        target=_summary_from_content(target),
        relation_type=candidate.relation_type,
        strength=candidate.strength,
        confidence=candidate.confidence,
        evidence_summary=candidate.evidence_summary,
        score_component=candidate.score_component,
    )


def _candidate_run_item_snapshot(candidate: object) -> dict[str, object]:
    """후보 결과를 재현할 최소 identity/digest만 원장 item에 남긴다."""
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


def _proposal_snapshot(proposal: KnowledgeRelationProposal) -> dict[str, object]:
    """본문·플래그 없이 상태 전이 검토에 필요한 최소 snapshot만 남긴다."""
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


async def _find_candidate_run(
    db: AsyncSession,
    *,
    deduplication_key: str,
    for_update: bool = False,
) -> KnowledgeCandidateRun | None:
    statement = select(KnowledgeCandidateRun).where(
        KnowledgeCandidateRun.deduplication_key == deduplication_key
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


def _new_candidate_run(
    *,
    invocation: CandidateGenerationInvocation,
    configuration_digest: str,
    input_snapshot_digest: str,
    input_snapshot_schema_version: str,
    execution_options: dict[str, object],
    input_snapshot: list[dict],
    requested_max_candidates: int,
    input_count: int,
    candidate_count_before_limit: int,
    preview_truncated: bool,
    collection_error_count: int,
    deduplication_key: str,
) -> KnowledgeCandidateRun:
    return KnowledgeCandidateRun(
        run_kind="rule",
        algorithm_version=DETERMINISTIC_CANDIDATE_RULESET_V2,
        configuration_digest=configuration_digest,
        input_snapshot_digest=input_snapshot_digest,
        input_snapshot_schema_version=input_snapshot_schema_version,
        execution_options=execution_options,
        input_snapshot=input_snapshot,
        requested_max_candidates=requested_max_candidates,
        requested_by=invocation.requested_by,
        trigger_kind=invocation.trigger_kind,
        execution_principal=invocation.execution_principal,
        deduplication_key=deduplication_key,
        attempt_count=1,
        state="running",
        is_dry_run=invocation.dry_run,
        started_at=_utc_now(),
        input_count=input_count,
        candidate_count_before_limit=candidate_count_before_limit,
        preview_truncated=preview_truncated,
        output_count=0,
        duplicate_count=0,
        error_count=0,
    )


async def _prepare_candidate_run(
    db: AsyncSession,
    *,
    invocation: CandidateGenerationInvocation,
    configuration_digest: str,
    input_snapshot_digest: str,
    input_snapshot_schema_version: str,
    execution_options: dict[str, object],
    input_snapshot: list[dict],
    requested_max_candidates: int,
    input_count: int,
    candidate_count_before_limit: int,
    preview_truncated: bool,
    collection_error_count: int,
    deduplication_key: str,
) -> tuple[KnowledgeCandidateRun, bool, bool]:
    """기존 원장을 재사용하거나 실패 원장을 같은 행에서 재시도 상태로 바꾼다.

    반환값은 ``(run, is_reused, is_new)``이다. 성공/실행 중 원장은 새 item이나
    proposal을 만들지 않는다. 실패/취소 원장만 같은 행에서 다음 시도로 재개한다.
    """
    existing = await _find_candidate_run(db, deduplication_key=deduplication_key, for_update=True)
    if existing:
        if existing.state == "succeeded":
            return existing, True, False
        if existing.state == "running" and not _is_recoverable_running_candidate_run(existing):
            return existing, True, False
        # A retried failed/stale run must describe this invocation, not retain
        # the previous attempt's input profile/node snapshot or cap.
        existing.configuration_digest = configuration_digest
        existing.input_snapshot_digest = input_snapshot_digest
        existing.input_snapshot_schema_version = input_snapshot_schema_version
        existing.execution_options = execution_options
        existing.input_snapshot = input_snapshot
        existing.requested_max_candidates = requested_max_candidates
        existing.requested_by = invocation.requested_by
        existing.trigger_kind = invocation.trigger_kind
        existing.execution_principal = invocation.execution_principal
        existing.is_dry_run = invocation.dry_run
        existing.input_count = input_count
        existing.candidate_count_before_limit = candidate_count_before_limit
        existing.preview_truncated = preview_truncated
        existing.output_count = 0
        existing.duplicate_count = 0
        existing.error_count = collection_error_count
        existing.state = "running"
        existing.started_at = _utc_now()
        existing.finished_at = None
        existing.error_code = None
        existing.attempt_count += 1
        return existing, False, False

    return (
        _new_candidate_run(
            invocation=invocation,
            configuration_digest=configuration_digest,
            input_snapshot_digest=input_snapshot_digest,
            input_snapshot_schema_version=input_snapshot_schema_version,
            execution_options=execution_options,
            input_snapshot=input_snapshot,
            requested_max_candidates=requested_max_candidates,
            input_count=input_count,
            candidate_count_before_limit=candidate_count_before_limit,
            preview_truncated=preview_truncated,
            collection_error_count=collection_error_count,
            deduplication_key=deduplication_key,
        ),
        False,
        True,
    )


async def _record_candidate_run_failure(
    db: AsyncSession,
    *,
    invocation: CandidateGenerationInvocation,
    configuration_digest: str,
    input_snapshot_digest: str,
    input_snapshot_schema_version: str,
    input_count: int,
    candidate_count_before_limit: int,
    preview_truncated: bool,
    collection_error_count: int,
    execution_options: dict[str, object],
    input_snapshot: list[dict],
    requested_max_candidates: int,
    deduplication_key: str,
) -> KnowledgeCandidateRun | None:
    """실패도 동일 dedupe 원장에 안전 코드만 누적한다."""
    existing = await _find_candidate_run(db, deduplication_key=deduplication_key, for_update=True)
    now = _utc_now()
    if existing:
        # commit 호출 뒤 연결 오류가 난 경우에는 DB가 이미 성공으로 반영됐을 수 있다.
        # 이 상태를 새 failed 원장으로 덮거나 이중 기록하지 않는다.
        if existing.state in {"succeeded", "running"}:
            return existing
        existing.state = "failed"
        existing.finished_at = now
        existing.candidate_count_before_limit = candidate_count_before_limit
        existing.preview_truncated = preview_truncated
        existing.error_count = max(existing.error_count + 1, collection_error_count + 1)
        existing.error_code = "candidate_generation_failed"
        existing.attempt_count += 1
        await db.commit()
        return existing

    failed_run = KnowledgeCandidateRun(
        run_kind="rule",
        algorithm_version=DETERMINISTIC_CANDIDATE_RULESET_V2,
        configuration_digest=configuration_digest,
        input_snapshot_digest=input_snapshot_digest,
        input_snapshot_schema_version=input_snapshot_schema_version,
        execution_options=execution_options,
        input_snapshot=input_snapshot,
        requested_max_candidates=requested_max_candidates,
        requested_by=invocation.requested_by,
        trigger_kind=invocation.trigger_kind,
        execution_principal=invocation.execution_principal,
        deduplication_key=deduplication_key,
        attempt_count=1,
        state="failed",
        is_dry_run=invocation.dry_run,
        started_at=now,
        finished_at=now,
        input_count=input_count,
        candidate_count_before_limit=candidate_count_before_limit,
        preview_truncated=preview_truncated,
        output_count=0,
        duplicate_count=0,
        error_count=max(1, collection_error_count + 1),
        error_code="candidate_generation_failed",
    )
    db.add(failed_run)
    await db.commit()
    return failed_run


async def generate_rule_based_candidate_run(
    db: AsyncSession,
    *,
    invocation: CandidateGenerationInvocation,
) -> CandidateGenerationResult:
    """공개 allowlist만으로 규칙 후보를 만들거나 기존 동일 원장을 재사용한다.

    ``dry_run=False``여도 생성 대상은 ``KnowledgeRelationProposal``의
    ``pending_review`` 상태로 제한된다. 이 함수는 ``KnowledgeRelation`` 및
    projection outbox를 생성하거나 승인/공개 상태를 바꾸지 않는다.
    """
    _validate_invocation(invocation)
    execution_options = _execution_options(invocation)
    configuration_digest = digest_json(
        {
            "algorithms": {
                "tag_prepares_for": deterministic_tag_prepares_for_configuration(),
                "content_tag_affinity": deterministic_content_tag_affinity_configuration(),
                "assignment_resource_helps": DETERMINISTIC_ASSIGNMENT_RESOURCE_HELPS_V1,
            },
            "generation_options": _generation_configuration_options(invocation),
        }
    )
    input_snapshot_digest = digest_json([])
    input_snapshot: list[dict] = []
    input_snapshot_schema_version = CANDIDATE_INPUT_SNAPSHOT_SCHEMA_VERSION
    input_count = 0
    candidate_count_before_limit = 0
    preview_truncated = False
    collection_error_count = 0
    requested_max_candidates = invocation.max_candidates
    deduplication_key = _run_deduplication_key(configuration_digest, input_snapshot_digest)
    # ``commit``이 예외를 내면 DB가 실제로 반영됐는지 이 세션만으로는 판별할 수 없다.
    # 그 뒤에 별도 failed 행을 쓰면 성공 run과 실패 run을 이중 기록할 수 있으므로,
    # commit 시도 뒤에는 원장을 추가 변경하지 않고 다음 복구/조회에 맡긴다.
    commit_attempted = False

    try:
        collection = await collect_public_tag_candidate_inputs(db)
        inputs_by_key = {
            _candidate_content_key(item): item
            for item in collection.explicit_inputs
        }
        # Existing tag inputs are richer (category/difficulty/approved tags) and
        # therefore win when an explicit education resource points at the same
        # content node. Explicit candidates are rebuilt against this canonical
        # profile below so their digest remains consistent with the snapshot.
        inputs_by_key.update(
            {
                _candidate_content_key(item): item
                for item in [*collection.sources, *collection.targets]
            }
        )
        inputs_by_key.update(
            {
                _candidate_content_key(item): item
                for item in collection.tag_inputs
            }
        )
        all_inputs = [inputs_by_key[key] for key in sorted(inputs_by_key)]
        input_count = len(all_inputs)
        collection_error_count = collection.error_count
        nodes_by_content_key = await existing_content_nodes(db, all_inputs)
        input_snapshot = candidate_input_snapshot(
            all_inputs,
            nodes_by_content_key=nodes_by_content_key,
        )
        input_snapshot_digest = digest_json(input_snapshot)
        deduplication_key = _run_deduplication_key(configuration_digest, input_snapshot_digest)
        tag_candidates, tag_preview_truncated, tag_count_before_limit = build_deterministic_tag_prepares_for_candidates(
            [item.profile for item in collection.sources],
            [item.profile for item in collection.targets],
            max_candidates=invocation.max_candidates,
        )
        content_tag_candidates, content_tag_preview_truncated, content_tag_count_before_limit = (
            build_deterministic_content_tag_affinity_candidates(
                [item.profile for item in [*collection.sources, *collection.targets]],
                [item.profile for item in collection.tag_inputs],
                max_candidates=invocation.max_candidates,
            )
        )
        canonical_explicit_candidates = []
        for explicit_candidate in collection.explicit_candidates:
            source_input = inputs_by_key.get(
                (explicit_candidate.source_entity_type, explicit_candidate.source_entity_id)
            )
            target_input = inputs_by_key.get(
                (explicit_candidate.target_entity_type, explicit_candidate.target_entity_id)
            )
            if not source_input or not target_input:
                collection_error_count += 1
                continue
            canonical_candidate = deterministic_assignment_resource_helps_candidate(
                source_input.profile,
                target_input.profile,
            )
            if canonical_candidate:
                canonical_explicit_candidates.append(canonical_candidate)

        explicit_candidates = sorted(
            canonical_explicit_candidates,
            key=lambda candidate: (
                -candidate.strength,
                -candidate.confidence,
                candidate.source_entity_type,
                candidate.source_entity_id,
                candidate.target_entity_type,
                candidate.target_entity_id,
            ),
        )
        merged_candidates = sorted(
            [*tag_candidates, *content_tag_candidates, *explicit_candidates],
            key=lambda candidate: (
                -candidate.strength,
                -candidate.confidence,
                candidate.source_entity_type,
                candidate.source_entity_id,
                candidate.target_entity_type,
                candidate.target_entity_id,
            ),
        )
        candidate_count_before_limit = (
            tag_count_before_limit
            + content_tag_count_before_limit
            + len(explicit_candidates)
        )
        preview_truncated = (
            tag_preview_truncated
            or content_tag_preview_truncated
            or len(merged_candidates) > invocation.max_candidates
        )
        candidates = merged_candidates[: invocation.max_candidates]
        inputs_by_content_key = {_candidate_content_key(item): item for item in all_inputs}
        blocking_pairs, same_revision_history = await _candidate_duplicate_index(db, nodes_by_content_key)

        pending_candidates = []
        precheck_skipped = []
        duplicate_count = 0
        for rank, candidate in enumerate(candidates, start=1):
            source_node = nodes_by_content_key.get((candidate.source_entity_type, candidate.source_entity_id))
            target_node = nodes_by_content_key.get((candidate.target_entity_type, candidate.target_entity_id))
            if source_node and target_node:
                pair = (source_node.id, target_node.id)
                relation_key = (*pair, candidate.relation_type)
                history_key = (*relation_key, candidate.content_revision)
                if relation_key in blocking_pairs or history_key in same_revision_history:
                    duplicate_count += 1
                    precheck_skipped.append(
                        (
                            rank,
                            candidate,
                            "existing_skipped" if relation_key in blocking_pairs else "rejected_precheck",
                        )
                    )
                    continue
            pending_candidates.append((rank, candidate))

        run, reused_existing_run, is_new_run = await _prepare_candidate_run(
            db,
            invocation=invocation,
            configuration_digest=configuration_digest,
            input_snapshot_digest=input_snapshot_digest,
            input_snapshot_schema_version=input_snapshot_schema_version,
            execution_options=execution_options,
            input_snapshot=input_snapshot,
            requested_max_candidates=requested_max_candidates,
            input_count=input_count,
            candidate_count_before_limit=candidate_count_before_limit,
            preview_truncated=preview_truncated,
            collection_error_count=collection_error_count,
            deduplication_key=deduplication_key,
        )
        if reused_existing_run:
            # 동일 snapshot/config의 완료 원장은 다시 proposal/item을 만들지 않는다.
            preview = (
                [_candidate_preview(candidate, inputs_by_content_key) for candidate in candidates]
                if run.state == "succeeded"
                else []
            )
            return CandidateGenerationResult(
                run=run,
                preview=preview,
                preview_truncated=bool(run.preview_truncated),
                reused_existing_run=True,
            )

        if is_new_run:
            db.add(run)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if not _is_expected_candidate_run_duplicate(exc):
                raise
            existing = await _find_candidate_run(db, deduplication_key=deduplication_key)
            if not existing:
                raise
            return CandidateGenerationResult(
                run=existing,
                preview=[],
                preview_truncated=bool(existing.preview_truncated),
                reused_existing_run=True,
            )

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

        if not invocation.dry_run:
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
                            relation_type=candidate.relation_type,
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
                            relation_type=candidate.relation_type,
                            content_revision=candidate.content_revision,
                        ):
                            raise _CandidateGenerationDuplicate()
                        proposal = KnowledgeRelationProposal(
                            source_node_id=source_node.id,
                            target_node_id=target_node.id,
                            source_profile_id=source_profile.id,
                            target_profile_id=target_profile.id,
                            relation_type=candidate.relation_type,
                            is_directed=True,
                            strength=candidate.strength,
                            confidence=candidate.confidence,
                            origin="rule",
                            evidence_summary=candidate.evidence_summary,
                            quality_version=candidate.algorithm_version,
                            generation_run_id=run.id,
                            state="pending_review",
                            content_revision=candidate.content_revision,
                            created_by=invocation.requested_by,
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
                                generator_version=candidate.algorithm_version,
                            )
                        )
                        db.add(
                            KnowledgeRelationProposalReview(
                                proposal_id=proposal.id,
                                reviewer_id=invocation.requested_by,
                                action="submitted",
                                reason="규칙 기반 후보 생성",
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
                    if not _is_expected_candidate_proposal_duplicate(exc):
                        raise
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
        run.error_count += collection_error_count
        run.error_code = None
        run.state = "succeeded"
        run.finished_at = _utc_now()
        preview = [_candidate_preview(candidate, inputs_by_content_key) for candidate in response_candidates]
        # Pydantic preview 객체와 결과 container를 commit 전에 모두 완성한다. 따라서
        # commit 성공 뒤 응답 객체를 만들다 실패해 별도 failed 원장이 생길 경로가 없다.
        result = CandidateGenerationResult(
            run=run,
            preview=preview,
            preview_truncated=bool(run.preview_truncated),
            reused_existing_run=False,
        )
        commit_attempted = True
        await db.commit()
        return result
    except CandidateGenerationServiceError:
        raise
    except Exception:
        try:
            await db.rollback()
        except Exception:
            # connection loss after commit/rollback is not evidence that the prior
            # transaction failed. Do not let cleanup create a second ledger path.
            pass
        if not commit_attempted:
            try:
                await _record_candidate_run_failure(
                    db,
                    invocation=invocation,
                    configuration_digest=configuration_digest,
                    input_snapshot_digest=input_snapshot_digest,
                    input_snapshot_schema_version=input_snapshot_schema_version,
                    input_count=input_count,
                    candidate_count_before_limit=candidate_count_before_limit,
                    preview_truncated=preview_truncated,
                    collection_error_count=collection_error_count,
                    execution_options=execution_options,
                    input_snapshot=input_snapshot,
                    requested_max_candidates=requested_max_candidates,
                    deduplication_key=deduplication_key,
                )
            except Exception:
                try:
                    await db.rollback()
                except Exception:
                    pass
        raise CandidateGenerationServiceError("candidate_generation_failed") from None
