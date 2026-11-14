"""지식 매핑 V2의 안전한 스냅샷·상태 전이 공통 규칙.

이 모듈은 DB, 모델 호출, 모델 API 호출을 하지 않는다. 후보 생성 입력과 공개용
근거를 작고 결정적인 자료형으로 고정해, 풀이·플래그·개인정보가 V2 원장에 섞이는
경로를 줄인다.
"""
from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import heapq
import json
import re
from typing import Any, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5


SAFE_PROFILE_SCHEMA_VERSION = "v2-safe-profile-1"
MAX_PROFILE_BYTES = 8_192
MAX_EVIDENCE_BYTES = 4_096

V2_RELATION_TYPES = {
    "requires",
    "prepares_for",
    "recommends",
    "helps",
    "related",
    "similar",
    "tag_affinity",
}
PROPOSAL_REVIEW_ACTIONS = {"approve", "reject", "retire"}
PROPOSAL_ACTIVE_STATES = {"draft", "pending_review"}

# The first executable generator intentionally uses only administrator-approved
# metadata. It is a deterministic baseline, not an opaque model decision.
DETERMINISTIC_TAG_PREPARES_FOR_V1 = "deterministic-tag-prepares-for-v1"
DETERMINISTIC_ASSIGNMENT_RESOURCE_HELPS_V1 = "deterministic-assignment-resource-helps-v1"
DETERMINISTIC_CONTENT_TAG_AFFINITY_V1 = "deterministic-content-tag-affinity-v1"
DETERMINISTIC_CANDIDATE_RULESET_V2 = "deterministic-candidate-rules-v2"
_TAG_PREPARES_MIN_SCORE = Decimal("0.420")
_TAG_PREPARES_MAX_CONFIDENCE = Decimal("0.700")
_TAG_PREPARES_SCORE_QUANTUM = Decimal("0.001")

_PROFILE_KEYS = {
    "entity_type",
    "entity_id",
    "title",
    "category",
    "difficulty",
    "learning_objectives",
    "approved_tag_slugs",
    "prerequisite_refs",
    "description_metadata",
}
_DESCRIPTION_METADATA_KEYS = {"summary", "keywords"}
_PREREQUISITE_KEYS = {"type", "id"}
_FORBIDDEN_KEY_PARTS = {
    "flag",
    "answer",
    "solution",
    "submission",
    "email",
    "password",
    "token",
    "cookie",
    "ip",
    "user_agent",
    "prompt",
    "secret",
}
_RAW_FLAG_PATTERN = re.compile(r"(?:ctf|flag)\{", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?:answer|solution|정답|풀이)\s*[:=]",
    re.IGNORECASE,
)
_SHA256_HEX_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def normalize_metadata_tag_slugs(
    *,
    category: str | None = None,
    difficulty: str | None = None,
    tags: Sequence[Any] = (),
) -> list[str]:
    """Return safe, deterministic taxonomy keys from public content metadata.

    This is deliberately a candidate-input helper, not an auto-approval path.
    Only the allowlisted ASCII slug vocabulary is emitted; arbitrary display
    text, descriptions, flags, and user/account fields never become graph
    tags.  The caller still has to resolve an existing ``KnowledgeTag`` and
    send the resulting relation through administrator review.
    """
    values: list[Any] = [category, difficulty, *tags]
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        raw_value = value.strip()
        if _RAW_FLAG_PATTERN.search(raw_value) or re.search(
            r"(?:^|[-_\s])(flag|answer|solution|secret|token)(?:$|[-_\s])",
            raw_value,
            re.IGNORECASE,
        ):
            continue
        candidate = re.sub(r"[^a-z0-9-]+", "-", raw_value.casefold()).strip("-")
        if candidate and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", candidate):
            normalized.add(candidate)
    return sorted(normalized)


def system_tag_edge_id(*, content_type: str, content_id: str, tag_id: str) -> UUID:
    """Return a stable id for a read-only manual tag projection edge.

    Junction tables remain the source of truth for tag membership.  The edge is
    deliberately derived at read time, so it cannot bypass the pending-review
    boundary used by candidate relations or be mistaken for an auto-approved
    model relation.
    """
    edge_key = ":".join(
        (
            "ctf-web",
            "manual-tag-projection-v1",
            content_type,
            str(content_id),
            str(tag_id),
        )
    )
    return uuid5(NAMESPACE_URL, edge_key)


def system_tag_parent_edge_id(*, child_tag_id: str, parent_tag_id: str) -> UUID:
    """Return a stable id for a public knowledge-tag taxonomy edge."""
    edge_key = ":".join(
        (
            "ctf-web",
            "knowledge-tag-parent-projection-v1",
            str(child_tag_id),
            str(parent_tag_id),
        )
    )
    return uuid5(NAMESPACE_URL, edge_key)


def system_structure_edge_id(*, course_id: str, lesson_id: str) -> UUID:
    """Return a stable id for the authoritative course->lesson projection."""
    edge_key = ":".join(
        (
            "ctf-web",
            "course-lesson-projection-v1",
            str(course_id),
            str(lesson_id),
        )
    )
    return uuid5(NAMESPACE_URL, edge_key)


class KnowledgeMappingValidationError(ValueError):
    """V2 원장에 저장할 수 없는 입력이라는 뜻의 명시적 예외."""


@dataclass(frozen=True)
class DeterministicTagPreparesForCandidate:
    """공개 메타데이터만으로 만든 검토 대기 관계 후보의 순수 계산 결과.

    이 값에는 콘텐츠 본문, 플래그, 사용자 식별자, 첨부파일 위치가 들어가지 않는다.
    DB 저장과 관리자 승인 전환은 이 모듈의 책임이 아니다.
    """

    source_entity_type: str
    source_entity_id: str
    target_entity_type: str
    target_entity_id: str
    relation_type: str
    strength: float
    confidence: float
    evidence_summary: str
    score_component: dict[str, Any]
    source_profile_digest: str
    target_profile_digest: str
    content_revision: str
    algorithm_version: str = DETERMINISTIC_TAG_PREPARES_FOR_V1


# 태그가 아직 연결되지 않은 공개 콘텐츠도 관리자 검토 큐에 올릴 수 있도록
# 같은 후보 원장 형식을 재사용한다. 이 후보는 승인 전까지 junction/그래프에
# 쓰이지 않으며, 원문 본문이나 플래그가 아닌 제목·분류 메타데이터 토큰만 사용한다.
DeterministicContentTagAffinityCandidate = DeterministicTagPreparesForCandidate


def _candidate_sort_key(candidate: DeterministicTagPreparesForCandidate) -> tuple[float, float, str, str, str, str]:
    return (
        -candidate.strength,
        -candidate.confidence,
        candidate.source_entity_type,
        candidate.source_entity_id,
        candidate.target_entity_type,
        candidate.target_entity_id,
    )


@dataclass(frozen=True)
class _WorstCandidateHeapEntry:
    """min-heap의 루트가 정렬상 가장 나쁜 후보가 되도록 비교를 뒤집는다."""

    candidate: DeterministicTagPreparesForCandidate

    def __lt__(self, other: "_WorstCandidateHeapEntry") -> bool:
        return _candidate_sort_key(self.candidate) > _candidate_sort_key(other.candidate)


def canonical_json(value: Any) -> str:
    """재현 가능한 digest를 위한 정규 JSON 직렬화."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest_json(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_text(value: Any, *, field_name: str, maximum: int, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise KnowledgeMappingValidationError(f"{field_name}은(는) 필요합니다.")
        return None
    if not isinstance(value, str):
        raise KnowledgeMappingValidationError(f"{field_name}은(는) 문자열이어야 합니다.")
    normalized = value.strip()
    if required and not normalized:
        raise KnowledgeMappingValidationError(f"{field_name}은(는) 비어 있을 수 없습니다.")
    if len(normalized) > maximum:
        raise KnowledgeMappingValidationError(f"{field_name}은(는) {maximum}자를 초과할 수 없습니다.")
    _assert_text_is_safe(normalized, field_name=field_name)
    return normalized or None


def _assert_text_is_safe(value: str, *, field_name: str) -> None:
    """명백한 플래그·정답 원문과 식별성 높은 이메일을 V2 입력에서 차단한다.

    이는 민감 정보 탐지의 완전한 대체물이 아니다. V2 입력 계약을 구조화 메타데이터로
    한정하고, 이 방어선은 실수로 플래그/풀이를 넣는 흔한 경우를 fail-closed로 막는다.
    """
    if _RAW_FLAG_PATTERN.search(value) or _SENSITIVE_ASSIGNMENT_PATTERN.search(value):
        raise KnowledgeMappingValidationError(
            f"{field_name}에는 플래그 또는 풀이·정답 원문을 넣을 수 없습니다."
        )
    if "@" in value:
        raise KnowledgeMappingValidationError(
            f"{field_name}에는 이메일 주소 등 개인 식별 정보를 넣을 수 없습니다."
        )


def _assert_safe_key(key: str, *, field_name: str) -> None:
    normalized = key.strip().lower().replace("-", "_")
    if not normalized:
        raise KnowledgeMappingValidationError(f"{field_name}의 키는 비어 있을 수 없습니다.")
    # ``ip``처럼 짧은 금지 토큰을 단순 부분 문자열로 찾으면
    # ``description`` 같은 정상 allowlist 키까지 막는다. 식별자 토큰 단위로만
    # 검사해 ``flag_text``, ``user_agent`` 등은 거부하면서 정상 키는 허용한다.
    tokens = {token for token in normalized.split("_") if token}
    if tokens & _FORBIDDEN_KEY_PARTS:
        raise KnowledgeMappingValidationError(
            f"{field_name}에는 민감 데이터로 해석될 수 있는 키를 사용할 수 없습니다."
        )


def _ensure_bounded_json(value: Any, *, field_name: str, maximum_bytes: int) -> None:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise KnowledgeMappingValidationError(f"{field_name}은(는) JSON 값이어야 합니다.") from exc
    if len(encoded) > maximum_bytes:
        raise KnowledgeMappingValidationError(
            f"{field_name}은(는) {maximum_bytes}바이트를 초과할 수 없습니다."
        )


def _validate_structured_value(value: Any, *, field_name: str, depth: int = 0) -> None:
    if depth > 4:
        raise KnowledgeMappingValidationError(f"{field_name}의 중첩 깊이가 너무 깊습니다.")
    if isinstance(value, dict):
        if len(value) > 30:
            raise KnowledgeMappingValidationError(f"{field_name}의 항목 수가 너무 많습니다.")
        for key, nested in value.items():
            if not isinstance(key, str) or len(key) > 100:
                raise KnowledgeMappingValidationError(f"{field_name}의 키 형식이 올바르지 않습니다.")
            _assert_safe_key(key, field_name=field_name)
            _validate_structured_value(nested, field_name=field_name, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > 30:
            raise KnowledgeMappingValidationError(f"{field_name}의 배열 항목 수가 너무 많습니다.")
        for nested in value:
            _validate_structured_value(nested, field_name=field_name, depth=depth + 1)
    elif isinstance(value, str):
        _validate_text(value, field_name=field_name, maximum=600)
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        raise KnowledgeMappingValidationError(f"{field_name}은(는) JSON으로 표현 가능한 값이어야 합니다.")


def normalize_allowed_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """V2 후보 생성기에 넘길 허용 메타데이터만 정규화한다."""
    if not isinstance(profile, dict):
        raise KnowledgeMappingValidationError("콘텐츠 프로필은 객체여야 합니다.")
    unknown_keys = set(profile) - _PROFILE_KEYS
    if unknown_keys:
        raise KnowledgeMappingValidationError(
            "허용되지 않은 콘텐츠 프로필 필드가 있습니다: " + ", ".join(sorted(unknown_keys))
        )
    for key in profile:
        _assert_safe_key(key, field_name="콘텐츠 프로필")

    normalized: dict[str, Any] = {
        "entity_type": _validate_text(profile.get("entity_type"), field_name="entity_type", maximum=24, required=True),
        "entity_id": _validate_text(profile.get("entity_id"), field_name="entity_id", maximum=64, required=True),
        "title": _validate_text(profile.get("title"), field_name="title", maximum=200, required=True),
    }
    for field_name, maximum in (("category", 120), ("difficulty", 80)):
        value = _validate_text(profile.get(field_name), field_name=field_name, maximum=maximum)
        if value is not None:
            normalized[field_name] = value

    objectives = profile.get("learning_objectives", [])
    if not isinstance(objectives, list) or len(objectives) > 20:
        raise KnowledgeMappingValidationError("learning_objectives 형식 또는 개수가 올바르지 않습니다.")
    normalized_objectives = [
        _validate_text(item, field_name="learning_objectives", maximum=300, required=True)
        for item in objectives
    ]
    if normalized_objectives:
        normalized["learning_objectives"] = normalized_objectives

    tag_slugs = profile.get("approved_tag_slugs", [])
    if not isinstance(tag_slugs, list) or len(tag_slugs) > 50:
        raise KnowledgeMappingValidationError("approved_tag_slugs 형식 또는 개수가 올바르지 않습니다.")
    normalized_tags: list[str] = []
    for tag_slug in tag_slugs:
        tag = _validate_text(tag_slug, field_name="approved_tag_slugs", maximum=100, required=True)
        if not re.fullmatch(r"[a-z0-9-]+", tag or ""):
            raise KnowledgeMappingValidationError("approved_tag_slugs는 안전한 slug 형식이어야 합니다.")
        normalized_tags.append(tag)
    if len(set(normalized_tags)) != len(normalized_tags):
        raise KnowledgeMappingValidationError("approved_tag_slugs에 중복된 값이 있습니다.")
    if normalized_tags:
        normalized["approved_tag_slugs"] = sorted(normalized_tags)

    prereqs = profile.get("prerequisite_refs", [])
    if not isinstance(prereqs, list) or len(prereqs) > 50:
        raise KnowledgeMappingValidationError("prerequisite_refs 형식 또는 개수가 올바르지 않습니다.")
    normalized_prereqs: list[dict[str, str]] = []
    for item in prereqs:
        if not isinstance(item, dict) or set(item) != _PREREQUISITE_KEYS:
            raise KnowledgeMappingValidationError("prerequisite_refs 항목은 type과 id만 포함해야 합니다.")
        entity_type = _validate_text(item.get("type"), field_name="prerequisite_refs.type", maximum=24, required=True)
        entity_id = _validate_text(item.get("id"), field_name="prerequisite_refs.id", maximum=64, required=True)
        normalized_prereqs.append({"type": entity_type or "", "id": entity_id or ""})
    if normalized_prereqs:
        normalized["prerequisite_refs"] = sorted(
            normalized_prereqs,
            key=lambda item: (item["type"], item["id"]),
        )

    metadata = profile.get("description_metadata")
    if metadata is not None:
        if not isinstance(metadata, dict) or set(metadata) - _DESCRIPTION_METADATA_KEYS:
            raise KnowledgeMappingValidationError(
                "description_metadata에는 summary와 keywords만 사용할 수 있습니다."
            )
        normalized_metadata: dict[str, Any] = {}
        if "summary" in metadata:
            summary = _validate_text(metadata["summary"], field_name="description_metadata.summary", maximum=500)
            if summary:
                normalized_metadata["summary"] = summary
        if "keywords" in metadata:
            keywords = metadata["keywords"]
            if not isinstance(keywords, list) or len(keywords) > 20:
                raise KnowledgeMappingValidationError("description_metadata.keywords 형식이 올바르지 않습니다.")
            normalized_metadata["keywords"] = sorted(
                _validate_text(keyword, field_name="description_metadata.keywords", maximum=80, required=True)
                for keyword in keywords
            )
        if normalized_metadata:
            normalized["description_metadata"] = normalized_metadata

    _ensure_bounded_json(normalized, field_name="콘텐츠 프로필", maximum_bytes=MAX_PROFILE_BYTES)
    return normalized


def profile_for_resolved_content(
    *,
    entity_type: str,
    entity_id: str,
    display_name: str,
) -> dict[str, Any]:
    """현재 원본에서 안전하게 얻을 수 있는 최소 프로필을 만든다."""
    return normalize_allowed_profile(
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": display_name,
        }
    )


def validate_structured_evidence(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """근거의 점수 구성은 작고 구조화된 JSON으로만 허용한다."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise KnowledgeMappingValidationError("score_component는 객체여야 합니다.")
    _validate_structured_value(value, field_name="score_component")
    _ensure_bounded_json(value, field_name="score_component", maximum_bytes=MAX_EVIDENCE_BYTES)
    return value


def validate_sha256_digest(value: str, *, field_name: str) -> str:
    normalized = _validate_text(value, field_name=field_name, maximum=64, required=True)
    if not _SHA256_HEX_PATTERN.fullmatch(normalized or ""):
        raise KnowledgeMappingValidationError(f"{field_name}은(는) SHA-256 16진수 digest여야 합니다.")
    return normalized or ""


def combined_profile_revision(source_digest: str, target_digest: str) -> str:
    """양 끝점의 프로필 기준을 방향을 보존한 하나의 revision으로 묶는다."""
    return digest_json({"source": source_digest, "target": target_digest})


def deterministic_tag_prepares_for_configuration() -> dict[str, Any]:
    """규칙 후보 생성의 고정 설정을 digest 가능한 형태로 반환한다."""
    return {
        "algorithm_version": DETERMINISTIC_TAG_PREPARES_FOR_V1,
        "source_types": ["course", "lesson"],
        "target_types": ["challenge"],
        "relation_type": "prepares_for",
        "minimum_shared_tag_count": 1,
        "minimum_score": float(_TAG_PREPARES_MIN_SCORE),
        "score_weights": {
            "target_tag_coverage": 0.45,
            "source_tag_coverage": 0.30,
            "jaccard": 0.20,
            "category_match": 0.05,
        },
        "confidence_cap": float(_TAG_PREPARES_MAX_CONFIDENCE),
    }


def deterministic_content_tag_affinity_configuration() -> dict[str, Any]:
    """콘텐츠 메타데이터와 기존 공개 태그의 자동 후보 규칙 계약."""
    return {
        "algorithm_version": DETERMINISTIC_CONTENT_TAG_AFFINITY_V1,
        "source_types": ["challenge", "course", "lesson"],
        "target_types": ["knowledge_tag"],
        "relation_type": "tag_affinity",
        "minimum_token_length": 2,
        "minimum_score": 0.55,
        "score_weights": {
            "tag_coverage": 0.45,
            "slug_exact_match": 0.35,
            "token_overlap": 0.10,
            "category_match": 0.10,
        },
        "confidence_cap": 0.85,
    }


def _metadata_tokens(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {
        token.casefold()
        for token in re.findall(r"[\w가-힣]{2,}", value)
        if len(token.strip()) >= 2
        and not token.isdigit()
        and not (len(token) >= 8 and all(char in "0123456789abcdefABCDEF" for char in token))
    }


def _content_tag_tokens(profile: dict[str, Any]) -> set[str]:
    normalized = normalize_allowed_profile(profile)
    tokens = _metadata_tokens(normalized.get("title"))
    tokens.update(_metadata_tokens(normalized.get("category")))
    tokens.update(_metadata_tokens(normalized.get("difficulty")))
    for slug in normalized.get("approved_tag_slugs", []):
        tokens.update(_metadata_tokens(slug))
    metadata = normalized.get("description_metadata")
    if isinstance(metadata, dict):
        tokens.update(_metadata_tokens(metadata.get("summary")))
        for keyword in metadata.get("keywords", []):
            tokens.update(_metadata_tokens(keyword))
    return tokens


def _knowledge_tag_tokens(profile: dict[str, Any]) -> set[str]:
    """Return only the approved taxonomy vocabulary for a tag candidate.

    A tag's database id/title/category are registry metadata, not semantic
    evidence.  Using them in the overlap denominator made a valid slug match
    fall below the threshold and could make candidate generation depend on
    arbitrary display labels.  The approved slug (and its normalized pieces)
    is the only tag-side vocabulary used by this deterministic rule.
    """
    normalized = normalize_allowed_profile(profile)
    tokens: set[str] = set()
    for slug in normalized.get("approved_tag_slugs", []):
        tokens.update(_metadata_tokens(slug))
    return tokens


def deterministic_content_tag_affinity_candidate(
    content_profile: dict[str, Any],
    tag_profile: dict[str, Any],
) -> DeterministicContentTagAffinityCandidate | None:
    """안전 메타데이터만으로 콘텐츠→지식 태그 후보를 계산한다.

    이 함수는 태그가 없는 콘텐츠를 자동으로 공개 태그에 연결하지 않는다.
    반환값은 ``KnowledgeRelationProposal(state=pending_review)``의 입력으로만
    사용되어야 하며, 승인 시에만 실제 junction이 생성된다.
    """
    normalized_content = normalize_allowed_profile(content_profile)
    normalized_tag = normalize_allowed_profile(tag_profile)
    source_type, source_id, source_digest = _candidate_profile_identity(normalized_content)
    target_type, target_id, target_digest = _candidate_profile_identity(normalized_tag)
    if source_type not in {"challenge", "course", "lesson"} or target_type != "knowledge_tag":
        return None
    content_tokens = _content_tag_tokens(normalized_content)
    tag_tokens = _knowledge_tag_tokens(normalized_tag)
    if not content_tokens or not tag_tokens:
        return None
    shared_tokens = tuple(sorted(content_tokens & tag_tokens))
    if not shared_tokens:
        return None
    token_overlap = len(shared_tokens) / max(len(content_tokens), len(tag_tokens))
    tag_coverage = len(shared_tokens) / max(len(tag_tokens), 1)
    tag_slugs = normalized_tag.get("approved_tag_slugs", [])
    slug_tokens = set()
    for tag_slug in tag_slugs:
        slug_tokens.update(_metadata_tokens(tag_slug))
    slug_exact_match = 1.0 if slug_tokens and slug_tokens.issubset(content_tokens) else 0.0
    category_match = 1.0 if (
        normalized_content.get("category")
        and normalized_content.get("category") == normalized_tag.get("category")
    ) else 0.0
    raw_score = (
        0.45 * tag_coverage
        + 0.35 * slug_exact_match
        + 0.10 * token_overlap
        + 0.10 * category_match
    )
    if raw_score < 0.55:
        return None
    strength = _quantize_candidate_score(Decimal(str(raw_score)))
    confidence = _quantize_candidate_score(
        min(Decimal("0.85"), Decimal("0.45") + Decimal("0.10") * min(len(shared_tokens), 4))
    )
    score_component = {
        "rule_version": DETERMINISTIC_CONTENT_TAG_AFFINITY_V1,
        "shared_token_count": len(shared_tokens),
        "shared_tokens": list(shared_tokens),
        "tag_coverage": _quantize_candidate_score(Decimal(str(tag_coverage))),
        "token_overlap": _quantize_candidate_score(Decimal(str(token_overlap))),
        "slug_exact_match": bool(slug_exact_match),
        "category_match": bool(category_match),
        "raw_score": strength,
        "minimum_score": 0.55,
    }
    return DeterministicContentTagAffinityCandidate(
        source_entity_type=source_type,
        source_entity_id=source_id,
        target_entity_type=target_type,
        target_entity_id=target_id,
        relation_type="tag_affinity",
        strength=strength,
        confidence=confidence,
        evidence_summary=f"공개 메타데이터 토큰 {len(shared_tokens)}개가 태그 표기와 일치",
        score_component=score_component,
        source_profile_digest=source_digest,
        target_profile_digest=target_digest,
        content_revision=combined_profile_revision(source_digest, target_digest),
        algorithm_version=DETERMINISTIC_CONTENT_TAG_AFFINITY_V1,
    )


def build_deterministic_content_tag_affinity_candidates(
    content_profiles: Sequence[dict[str, Any]],
    tag_profiles: Sequence[dict[str, Any]],
    *,
    max_candidates: int,
) -> tuple[list[DeterministicContentTagAffinityCandidate], bool, int]:
    """콘텐츠×태그 후보를 bounded top-K로 계산한다."""
    if max_candidates < 1:
        raise KnowledgeMappingValidationError("max_candidates는 1 이상이어야 합니다.")
    candidates: list[DeterministicContentTagAffinityCandidate] = []
    for content in content_profiles:
        for tag in tag_profiles:
            candidate = deterministic_content_tag_affinity_candidate(content, tag)
            if candidate:
                candidates.append(candidate)
    candidates.sort(key=_candidate_sort_key)
    total = len(candidates)
    return candidates[:max_candidates], total > max_candidates, total


def _quantize_candidate_score(value: Decimal) -> float:
    return float(value.quantize(_TAG_PREPARES_SCORE_QUANTUM, rounding=ROUND_HALF_UP))


def _candidate_profile_identity(profile: dict[str, Any]) -> tuple[str, str, str]:
    normalized = normalize_allowed_profile(profile)
    return (
        str(normalized["entity_type"]),
        str(normalized["entity_id"]),
        digest_json(normalized),
    )


def deterministic_tag_prepares_for_candidate(
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
) -> DeterministicTagPreparesForCandidate | None:
    """공통 승인 태그로 교육 콘텐츠 → 워게임 문제 후보를 계산한다.

    입력 프로필은 먼저 allowlist 정규화를 거치므로, 함수가 본문·플래그·개인정보를
    우회해서 사용할 수 없다. 공통 태그가 없거나 계산 점수가 기준보다 낮으면 후보를
    만들지 않는다.
    """
    normalized_source = normalize_allowed_profile(source_profile)
    normalized_target = normalize_allowed_profile(target_profile)
    source_type, source_id, source_digest = _candidate_profile_identity(normalized_source)
    target_type, target_id, target_digest = _candidate_profile_identity(normalized_target)
    if source_type not in {"course", "lesson"} or target_type != "challenge":
        return None
    if source_type == target_type and source_id == target_id:
        return None

    source_tags = set(normalized_source.get("approved_tag_slugs", []))
    target_tags = set(normalized_target.get("approved_tag_slugs", []))
    shared_tags = tuple(sorted(source_tags & target_tags))
    if not shared_tags:
        return None
    union_tags = source_tags | target_tags
    if not union_tags:
        return None

    shared_count = Decimal(len(shared_tags))
    source_coverage = shared_count / Decimal(len(source_tags))
    target_coverage = shared_count / Decimal(len(target_tags))
    jaccard = shared_count / Decimal(len(union_tags))
    category_match = Decimal(
        1
        if normalized_source.get("category")
        and normalized_source.get("category") == normalized_target.get("category")
        else 0
    )
    raw_score = (
        Decimal("0.45") * target_coverage
        + Decimal("0.30") * source_coverage
        + Decimal("0.20") * jaccard
        + Decimal("0.05") * category_match
    )
    if raw_score < _TAG_PREPARES_MIN_SCORE:
        return None

    confidence = min(
        _TAG_PREPARES_MAX_CONFIDENCE,
        Decimal("0.35")
        + Decimal("0.10") * min(Decimal(len(shared_tags)), Decimal("3"))
        + Decimal("0.05") * category_match,
    )
    rounded_score = _quantize_candidate_score(raw_score)
    rounded_confidence = _quantize_candidate_score(confidence)
    evidence_summary = (
        f"승인된 지식 태그 {len(shared_tags)}개가 공통이며 "
        + ("카테고리가 일치합니다." if category_match else "카테고리 일치는 확인되지 않았습니다.")
    )
    score_component = {
        "rule_version": DETERMINISTIC_TAG_PREPARES_FOR_V1,
        "shared_tag_count": len(shared_tags),
        "shared_tag_slugs": list(shared_tags),
        "source_tag_coverage": _quantize_candidate_score(source_coverage),
        "target_tag_coverage": _quantize_candidate_score(target_coverage),
        "jaccard": _quantize_candidate_score(jaccard),
        "category_match": bool(category_match),
        "minimum_score": float(_TAG_PREPARES_MIN_SCORE),
        "raw_score": rounded_score,
    }
    return DeterministicTagPreparesForCandidate(
        source_entity_type=source_type,
        source_entity_id=source_id,
        target_entity_type=target_type,
        target_entity_id=target_id,
        relation_type="prepares_for",
        strength=rounded_score,
        confidence=rounded_confidence,
        evidence_summary=evidence_summary,
        score_component=score_component,
        source_profile_digest=source_digest,
        target_profile_digest=target_digest,
        content_revision=combined_profile_revision(source_digest, target_digest),
    )


def build_deterministic_tag_prepares_for_candidates(
    source_profiles: Sequence[dict[str, Any]],
    target_profiles: Sequence[dict[str, Any]],
    *,
    max_candidates: int,
) -> tuple[list[DeterministicTagPreparesForCandidate], bool, int]:
    """태그 역색인과 bounded top-K로 재현 가능한 후보 순서를 만든다.

    모든 source×target 조합을 메모리에 쌓지 않는다. 공통 승인 태그가 있는 쌍만
    계산하고, ``max_candidates``개의 최상위 후보만 유지한다. 세 번째 반환값은
    제한 전 후보 수라 실행 원장이 결과를 재현·감사할 수 있다.
    """
    if max_candidates < 1:
        raise KnowledgeMappingValidationError("max_candidates는 1 이상이어야 합니다.")

    normalized_sources = [normalize_allowed_profile(profile) for profile in source_profiles]
    normalized_targets = [normalize_allowed_profile(profile) for profile in target_profiles]
    normalized_sources.sort(key=_candidate_profile_identity)
    normalized_targets.sort(key=_candidate_profile_identity)

    targets_by_tag: dict[str, list[dict[str, Any]]] = {}
    for target_profile in normalized_targets:
        for tag in target_profile.get("approved_tag_slugs", []):
            targets_by_tag.setdefault(tag, []).append(target_profile)
    for candidates_for_tag in targets_by_tag.values():
        candidates_for_tag.sort(key=_candidate_profile_identity)

    top_candidates: list[_WorstCandidateHeapEntry] = []
    candidate_count_before_limit = 0
    for source_profile in normalized_sources:
        seen_targets: set[tuple[str, str]] = set()
        for tag in source_profile.get("approved_tag_slugs", []):
            for target_profile in targets_by_tag.get(tag, []):
                target_type, target_id, _target_digest = _candidate_profile_identity(target_profile)
                target_key = (target_type, target_id)
                if target_key in seen_targets:
                    continue
                seen_targets.add(target_key)
                candidate = deterministic_tag_prepares_for_candidate(source_profile, target_profile)
                if candidate:
                    candidate_count_before_limit += 1
                    entry = _WorstCandidateHeapEntry(candidate)
                    if len(top_candidates) < max_candidates:
                        heapq.heappush(top_candidates, entry)
                    elif _candidate_sort_key(candidate) < _candidate_sort_key(top_candidates[0].candidate):
                        heapq.heapreplace(top_candidates, entry)

    candidates = sorted((entry.candidate for entry in top_candidates), key=_candidate_sort_key)
    return candidates, candidate_count_before_limit > max_candidates, candidate_count_before_limit


def deterministic_assignment_resource_helps_candidate(
    source_profile: dict[str, Any],
    target_profile: dict[str, Any],
) -> DeterministicTagPreparesForCandidate | None:
    """강의에 명시적으로 연결된 리소스의 과제 도움 관계 후보를 만든다.

    이 규칙은 제목 유사도나 본문 추측을 사용하지 않는다. 교육 도메인의
    ``EducationResourceLink``가 실제로 연결한 공개 리소스만 대상으로 하며,
    최종 공개는 별도 관리자 검토를 통과한 뒤에만 가능하다.
    """
    normalized_source = normalize_allowed_profile(source_profile)
    normalized_target = normalize_allowed_profile(target_profile)
    source_type, source_id, source_digest = _candidate_profile_identity(normalized_source)
    target_type, target_id, target_digest = _candidate_profile_identity(normalized_target)
    if source_type != "assignment" or target_type not in {"course", "lesson", "challenge"}:
        return None
    if source_id == target_id:
        return None

    algorithm_version = DETERMINISTIC_ASSIGNMENT_RESOURCE_HELPS_V1
    strength = 0.900
    confidence = 0.900
    evidence_summary = "강의에 등록된 과제와 공개 학습 리소스의 명시적 연결"
    score_component = {
        "rule_version": algorithm_version,
        "evidence_kind": "education_resource_link",
        "target_type": target_type,
        "explicit_link": True,
    }
    return DeterministicTagPreparesForCandidate(
        source_entity_type=source_type,
        source_entity_id=source_id,
        target_entity_type=target_type,
        target_entity_id=target_id,
        relation_type="helps",
        strength=strength,
        confidence=confidence,
        evidence_summary=evidence_summary,
        score_component=score_component,
        source_profile_digest=source_digest,
        target_profile_digest=target_digest,
        content_revision=combined_profile_revision(source_digest, target_digest),
        algorithm_version=algorithm_version,
    )


def public_evidence_summary(value: str | None) -> str | None:
    """공개 그래프에서 명백히 민감한 근거 요약을 fail-closed로 감춘다."""
    if not value:
        return None
    try:
        normalized = _validate_text(value, field_name="근거 요약", maximum=1_000)
    except KnowledgeMappingValidationError:
        return "관리자가 승인한 관계 근거"
    return normalized


def next_proposal_state(current_state: str, action: str) -> str:
    """검토 API가 사용할 수 있는 좁은 상태 전이를 단일 규칙으로 고정한다."""
    if action not in PROPOSAL_REVIEW_ACTIONS:
        raise KnowledgeMappingValidationError("지원하지 않는 제안서 검토 동작입니다.")
    if action == "approve":
        if current_state != "pending_review":
            raise KnowledgeMappingValidationError("검토 대기 제안서만 승인할 수 있습니다.")
        return "approved"
    if action == "reject":
        if current_state not in {"draft", "pending_review"}:
            raise KnowledgeMappingValidationError("초안 또는 검토 대기 제안서만 반려할 수 있습니다.")
        return "rejected"
    if current_state != "approved":
        raise KnowledgeMappingValidationError("승인된 제안서만 은퇴할 수 있습니다.")
    return "retired"


def encode_focus_continuation(
    *,
    focus_type: str,
    focus_id: str,
    depth: int,
    types: list[str],
    relation_types: list[str],
) -> str:
    """다음 로컬 그래프 요청을 위한 비밀 없는 재시작 토큰.

    토큰은 권한을 부여하지 않는다. 서버는 사용 시 원본 공개 상태와 모든 필터를 다시
    확인한다. 토큰에 사용자·세션·콘텐츠 원문을 넣지 않는다.
    """
    payload = {
        "v": 1,
        "focus_type": focus_type,
        "focus_id": focus_id,
        "depth": depth,
        "types": sorted(types),
        "relation_types": sorted(relation_types),
    }
    encoded = urlsafe_b64encode(canonical_json(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"v1.{encoded}"


def decode_focus_continuation(value: str) -> dict[str, Any]:
    """서명 없는 continuation의 형식만 검증한다. 권한 검증은 호출자 책임이다."""
    if not isinstance(value, str) or len(value) > 600 or not value.startswith("v1."):
        raise KnowledgeMappingValidationError("그래프 continuation 형식이 올바르지 않습니다.")
    encoded = value[3:]
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        parsed = json.loads(urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KnowledgeMappingValidationError("그래프 continuation 형식이 올바르지 않습니다.") from exc
    if not isinstance(parsed, dict) or parsed.get("v") != 1:
        raise KnowledgeMappingValidationError("지원하지 않는 그래프 continuation 버전입니다.")
    expected_keys = {"v", "focus_type", "focus_id", "depth", "types", "relation_types"}
    if set(parsed) != expected_keys:
        raise KnowledgeMappingValidationError("그래프 continuation 필드가 올바르지 않습니다.")
    if not isinstance(parsed["focus_type"], str) or not isinstance(parsed["focus_id"], str):
        raise KnowledgeMappingValidationError("그래프 continuation 중심 정보가 올바르지 않습니다.")
    if not isinstance(parsed["depth"], int) or not 0 <= parsed["depth"] <= 3:
        raise KnowledgeMappingValidationError("그래프 continuation 깊이가 올바르지 않습니다.")
    for field_name in ("types", "relation_types"):
        items = parsed[field_name]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise KnowledgeMappingValidationError("그래프 continuation 필터가 올바르지 않습니다.")
    return parsed
