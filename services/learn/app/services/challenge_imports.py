"""관리자 문제 파일의 외부 식별자 해석과 비파괴적 일괄 반영."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.challenge import (
    Challenge, ChallengeFlagPolicy, ChallengePrerequisite, Flag, Hint,
)
from app.models.knowledge import ChallengeLearningPrerequisite
from app.models.challenge_attachment import ChallengeAttachment
from app.services.challenge_attachments import bind_attachments
from app.schemas.challenge_attachment import AttachmentFields
from app.schemas.challenge import ChallengeUpdate, DynamicFlagPolicyInput, FlagCreate, HintCreate
from app.utils.dynamic_flags import DYNAMIC_FLAG_TYPE, dynamic_policy_configuration_changed
from app.utils.environment_verification import (
    detect_dockerfile_exposed_port, environment_configuration_changed,
    environment_is_verified_for_runtime, flag_policy_configuration_changed,
)
from app.utils.knowledge_tag_projection import project_rule_tag_mappings
from app.utils.safe_html import (
    RichContentError, normalize_rich_content_for_write, normalize_rich_content_mapping,
)


IMPORT_LOCK_KEY = 712358719340183204
SCALAR_FIELDS = frozenset({
    "name", "name_i18n", "category", "difficulty", "description", "description_i18n",
    "value", "xp", "max_attempts", "flag_type", "flag_value", "tags", "files",
    "env_type", "dockerfile_content", "docker_image", "container_port",
    "challenge_type", "thumbnail_url", "is_published",
})
NEW_DEFAULTS = {
    "category": "web", "difficulty": "beginner", "description": "",
    "value": 100, "xp": 50, "max_attempts": 0,
    "flag_type": "static", "flag_value": "", "tags": [], "files": [],
    "env_type": "none", "dockerfile_content": "", "docker_image": "",
    "container_port": 8080, "challenge_type": "wargame", "is_published": False,
}
ENVIRONMENT_FIELDS = ("env_type", "dockerfile_content", "docker_image", "container_port")
POLICY_FIELDS = ("enabled", "delivery", "runtime_path", "ttl_seconds", "hmac_key_version")


def import_error(code: str, message: str, *, index: int | None = None, status: int = 422):
    detail: dict[str, object] = {"code": code, "message": message}
    if index is not None:
        detail["record_index"] = index
    return HTTPException(status_code=status, detail=detail)


@dataclass(frozen=True)
class ImportRecord:
    index: int
    source_id: int | None
    fields: dict[str, Any]
    hints: tuple[dict[str, Any], ...] | None
    flags: tuple[dict[str, Any], ...] | None
    prerequisite_ids: tuple[int, ...]
    dynamic_flag_present: bool
    dynamic_flag: DynamicFlagPolicyInput | None
    attachment_ids: tuple[UUID, ...] | None = None
    attachment_draft_id: UUID | None = None


@dataclass
class ImportTarget:
    record: ImportRecord
    key: int
    entity: Challenge | None
    fields: dict[str, Any]
    hints: list[tuple[Hint | None, dict[str, Any]]]
    flags: list[tuple[Flag | None, dict[str, Any]]]
    policy: ChallengeFlagPolicy | None
    invalidate_environment: bool = False


def _positive_id(value: object, *, index: int) -> int:
    if type(value) is not int or value <= 0:
        raise import_error("invalid_source_id", "원본 ID는 양의 정수여야 합니다.", index=index)
    return value


def _prepare_children(raw: object, *, kind: str, index: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        raise import_error("invalid_children", "힌트와 플래그 목록은 배열이어야 합니다.", index=index)
    result = []
    slots: set[int] = set()
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise import_error("invalid_child", "힌트와 플래그 항목은 객체여야 합니다.", index=index)
        slot = item.get("order_index", position)
        if type(slot) is not int or slot < 0 or slot in slots:
            raise import_error("ambiguous_child_slot", "자식 순서는 음이 아닌 고유 정수여야 합니다.", index=index)
        slots.add(slot)
        source_id = _positive_id(item["id"], index=index) if "id" in item else None
        try:
            if kind == "hints":
                content = normalize_rich_content_for_write(
                    item.get("content", ""), field_name="가져올 힌트", max_visible_chars=2000,
                )
                child = HintCreate.model_validate({"content": content, "cost": item.get("cost", 0)})
            else:
                child = FlagCreate.model_validate(item)
        except (ValidationError, RichContentError, TypeError, ValueError) as error:
            raise import_error("invalid_child_content", "힌트 또는 플래그 입력을 확인하세요.", index=index) from error
        values = child.model_dump()
        values.update(order_index=slot, source_id=source_id)
        result.append(values)
    return tuple(result)


def prepare_import(body: dict) -> tuple[str, list[ImportRecord]]:
    version = body.get("version", 1)
    if type(version) is not int or version not in (1, 2):
        raise import_error("unsupported_import_version", "지원하지 않는 가져오기 버전입니다.")
    mode = body.get("mode", "skip")
    if mode not in ("skip", "overwrite"):
        raise import_error("invalid_import_mode", "mode는 skip 또는 overwrite이어야 합니다.", status=400)
    raw_records = body.get("challenges")
    if not isinstance(raw_records, list):
        raise import_error("invalid_challenges", "challenges는 배열이어야 합니다.")
    names: set[str] = set()
    source_ids: set[int] = set()
    child_ids: dict[str, set[int]] = {"hints": set(), "flags": set()}
    records = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"].strip():
            raise import_error("invalid_challenge", "문제 항목과 이름을 확인하세요.", index=index)
        if raw["name"] in names:
            raise import_error("duplicate_challenge_name", "파일 안에 같은 이름의 문제가 있습니다.", index=index)
        names.add(raw["name"])
        source_id = _positive_id(raw["id"], index=index) if "id" in raw else None
        if version == 2 and source_id is None:
            raise import_error("missing_source_id", "버전 2 문제에는 원본 ID가 필요합니다.", index=index)
        if source_id is not None:
            if source_id in source_ids:
                raise import_error("duplicate_source_id", "파일 안의 원본 문제 ID가 중복됩니다.", index=index)
            source_ids.add(source_id)
        fields = {key: value for key, value in raw.items() if key in SCALAR_FIELDS}
        if any(value is None and key != "thumbnail_url" for key, value in fields.items()):
            raise import_error("null_challenge_field", "비울 수 없는 문제 필드가 포함되어 있습니다.", index=index)
        try:
            if "description" in fields:
                fields["description"] = normalize_rich_content_for_write(
                    fields["description"], field_name="가져올 문제 설명", allow_empty=True, max_visible_chars=10000,
                )
            if "description_i18n" in fields:
                fields["description_i18n"] = normalize_rich_content_mapping(
                    fields["description_i18n"], field_name="가져올 문제 설명 번역", allow_empty=True, max_visible_chars=10000,
                )
            fields = ChallengeUpdate.model_validate(fields).model_dump(exclude_unset=True)
        except (ValidationError, RichContentError, TypeError, ValueError) as error:
            raise import_error("invalid_challenge_fields", "문제 내용 또는 필드 형식을 확인하세요.", index=index) from error
        children = {}
        for kind in ("hints", "flags"):
            children[kind] = _prepare_children(raw[kind], kind=kind, index=index) if kind in raw else None
            for child in children[kind] or ():
                child_id = child["source_id"]
                if child_id is not None:
                    if child_id in child_ids[kind]:
                        raise import_error("duplicate_child_source_id", "파일 안의 원본 자식 ID가 중복됩니다.", index=index)
                    child_ids[kind].add(child_id)
        raw_prerequisites = raw.get("prerequisite_ids", [])
        if not isinstance(raw_prerequisites, list):
            raise import_error("invalid_prerequisite_ids", "선행 문제 ID 목록은 배열이어야 합니다.", index=index)
        prerequisites = tuple(_positive_id(value, index=index) for value in raw_prerequisites)
        if len(set(prerequisites)) != len(prerequisites):
            raise import_error("duplicate_prerequisite", "같은 선행 문제를 중복 지정할 수 없습니다.", index=index)
        policy = None
        try:
            attachment_input = AttachmentFields.model_validate({key: raw[key] for key in
                ("attachment_ids", "attachment_draft_id") if key in raw})
        except ValidationError as error:
            raise import_error("invalid_attachment_refs", "첨부 참조 형식을 확인하세요.", index=index) from error
        if "dynamic_flag" in raw and raw["dynamic_flag"] is not None:
            try:
                policy = DynamicFlagPolicyInput.model_validate(raw["dynamic_flag"])
            except ValidationError as error:
                raise import_error("invalid_dynamic_policy", "동적 플래그 정책을 확인하세요.", index=index) from error
        records.append(ImportRecord(index, source_id, fields, children["hints"], children["flags"],
                                    prerequisites, "dynamic_flag" in raw, policy,
                                    tuple(attachment_input.attachment_ids) if attachment_input.attachment_ids is not None else None,
                                    attachment_input.attachment_draft_id))
    for record in records:
        if any(value not in source_ids for value in record.prerequisite_ids):
            raise import_error("unresolved_prerequisite", "선행 문제의 원본 ID가 같은 파일에 없습니다. 원본 ID가 포함된 버전 2 파일을 사용하세요.", index=record.index)
    return mode, records


def merge_child_slots(existing: Iterable[Any], incoming: tuple[dict[str, Any], ...] | None, *, index: int):
    if incoming is None:
        return []
    by_slot: dict[int, list[Any]] = defaultdict(list)
    for child in existing:
        by_slot[child.order_index].append(child)
    changes = []
    for child in incoming:
        matches = by_slot.get(child["order_index"], [])
        if len(matches) > 1:
            raise import_error("ambiguous_existing_child_slot", "기존 자식 순서가 중복되어 대상을 확정할 수 없습니다.", index=index, status=409)
        # source_id는 외부 파일 식별자이며 로컬 자식 PK로 조회하지 않는다.
        values = {key: value for key, value in child.items() if key != "source_id"}
        changes.append((matches[0] if matches else None, values))
    return changes


def _effective_flags(existing: list[Flag], changes: list[tuple[Flag | None, dict]]) -> list[dict]:
    replacements = {id(child): values for child, values in changes if child is not None}
    result = [replacements.get(id(child), {"type": child.type, "value": child.value,
              "case_sensitive": child.case_sensitive, "order_index": child.order_index}) for child in existing]
    result.extend(values for child, values in changes if child is None)
    return sorted(result, key=lambda value: value["order_index"])


def plan_import(records: list[ImportRecord], mode: str, challenges: list[Challenge],
                hints: list[Hint], flags: list[Flag], policies: list[ChallengeFlagPolicy]):
    by_name: dict[str, list[Challenge]] = defaultdict(list)
    for challenge in challenges:
        by_name[challenge.name].append(challenge)
    hints_by_parent: dict[int, list[Hint]] = defaultdict(list)
    flags_by_parent: dict[int, list[Flag]] = defaultdict(list)
    for hint in hints:
        hints_by_parent[hint.challenge_id].append(hint)
    for flag in flags:
        flags_by_parent[flag.challenge_id].append(flag)
    policies_by_parent = {policy.challenge_id: policy for policy in policies}
    targets = []
    mapping = {}
    skipped = 0
    for record in records:
        matches = by_name.get(record.fields["name"], [])
        if len(matches) > 1:
            raise import_error("ambiguous_existing_name", "같은 이름의 기존 문제가 여러 개입니다.", index=record.index, status=409)
        entity = matches[0] if matches else None
        key = entity.id if entity is not None else -(record.index + 1)
        if record.source_id is not None:
            mapping[record.source_id] = key
        if entity is not None and mode == "skip":
            skipped += 1
            continue
        fields = dict(record.fields) if entity is not None else {**NEW_DEFAULTS, **record.fields}
        target_env = fields.get("env_type", entity.env_type if entity is not None else "none")
        if "container_port" not in record.fields and "dockerfile_content" in record.fields and target_env == "dockerfile":
            detected = detect_dockerfile_exposed_port(fields["dockerfile_content"])
            if detected is not None:
                fields["container_port"] = detected
        if entity is None and target_env != "none":
            fields["is_published"] = False
        targets.append(ImportTarget(record, key, entity, fields,
            merge_child_slots(hints_by_parent[key], record.hints, index=record.index),
            merge_child_slots(flags_by_parent[key], record.flags, index=record.index), policies_by_parent.get(key)))
    return targets, mapping, skipped, flags_by_parent


def validate_import_edges(records: list[ImportRecord], targets: list[ImportTarget], mapping: dict[int, int],
                          challenges: list[Challenge], existing_edges: set[tuple[int, int]],
                          learning_sources: set[int]) -> set[tuple[int, int]]:
    nodes = {challenge.id: (challenge.challenge_type, challenge.is_published) for challenge in challenges}
    writable = {target.record.index: target.key for target in targets}
    for target in targets:
        old_type, old_published = nodes.get(target.key, ("wargame", False))
        nodes[target.key] = (target.fields.get("challenge_type", old_type), target.fields.get("is_published", old_published))
        if target.entity is not None and nodes[target.key][0] != old_type:
            if (any(source == target.key for source, _ in existing_edges) or target.key in learning_sources) and nodes[target.key][0] == "competition":
                raise import_error("competition_prerequisite", "강제 선행조건이 있는 문제를 CTF로 바꿀 수 없습니다.", index=target.record.index)
            if any(destination == target.key for _, destination in existing_edges):
                raise import_error("referenced_challenge_type", "다른 문제의 선행 문제 유형은 가져오기로 바꿀 수 없습니다.", index=target.record.index)
    additions = set()
    for record in records:
        if record.index not in writable:
            continue
        source = writable[record.index]
        for source_destination in record.prerequisite_ids:
            edge = (source, mapping[source_destination])
            if edge in existing_edges:
                continue
            if edge[0] == edge[1]:
                raise import_error("self_prerequisite", "문제 자신을 선행 문제로 지정할 수 없습니다.", index=record.index)
            if nodes[edge[0]][0] != "wargame" or nodes[edge[1]] != ("wargame", True):
                raise import_error("unavailable_prerequisite", "새 선행 문제는 공개된 워게임 문제여야 합니다.", index=record.index)
            additions.add(edge)
    graph: dict[int, set[int]] = defaultdict(set)
    for source, destination in existing_edges | additions:
        graph[source].add(destination)
    # 기존의 무관한 legacy cycle은 보존하되 추가 edge가 cycle을 만드는지는 전량 확인한다.
    for source, destination in additions:
        pending = [destination]
        visited = set()
        while pending:
            current = pending.pop()
            if current == source:
                raise import_error("cyclic_prerequisite", "선행 문제 조건에 순환 참조를 만들 수 없습니다.")
            if current not in visited:
                visited.add(current)
                pending.extend(graph[current] - visited)
    return additions


def validate_target_policy(target: ImportTarget, current_flags: list[Flag],
                           validate_dynamic_policy: Callable[[DynamicFlagPolicyInput], None]) -> None:
    record, entity, fields = target.record, target.entity, target.fields
    current_type = entity.flag_type if entity is not None else "static"
    flag_type = fields.get("flag_type", current_type)
    is_dynamic = flag_type == DYNAMIC_FLAG_TYPE
    if entity is not None and (current_type == DYNAMIC_FLAG_TYPE) != is_dynamic:
        raise import_error("flag_mode_requires_editor", "플래그 모드 전환은 기존 문제 편집기에서 진행하세요.", index=record.index, status=409)
    if is_dynamic:
        if fields.get("env_type", entity.env_type if entity is not None else "none") == "none":
            raise import_error("dynamic_environment_required", "동적 플래그에는 Docker 실습 환경이 필요합니다.", index=record.index)
        if fields.get("flag_value", entity.flag_value if entity is not None else "") or record.flags or current_flags:
            raise import_error("mixed_flag_modes", "동적 플래그에는 정적 정답을 함께 지정할 수 없습니다.", index=record.index)
        if record.dynamic_flag_present and record.dynamic_flag is None:
            raise import_error("missing_dynamic_policy", "동적 플래그 정책은 null일 수 없습니다.", index=record.index)
        policy = record.dynamic_flag
        if policy is None and target.policy is not None:
            policy = DynamicFlagPolicyInput.model_validate({key: getattr(target.policy, key) for key in POLICY_FIELDS})
        if policy is None or not policy.enabled:
            raise import_error("missing_dynamic_policy", "활성화된 동적 플래그 정책이 필요합니다.", index=record.index)
        validate_dynamic_policy(policy)
        fields["flag_value"] = ""
    elif record.dynamic_flag is not None:
        raise import_error("unexpected_dynamic_policy", "동적 정책은 동적 플래그에서만 사용할 수 있습니다.", index=record.index)
    if entity is None:
        return
    before = {key: getattr(entity, key) for key in ENVIRONMENT_FIELDS}
    old_policy = {key: getattr(target.policy, key) for key in POLICY_FIELDS} if target.policy is not None else None
    new_policy = record.dynamic_flag.model_dump() if record.dynamic_flag is not None else old_policy
    changed = environment_configuration_changed(before, fields)
    changed |= is_dynamic and dynamic_policy_configuration_changed(old_policy, new_policy)
    changed |= fields.get("env_type", entity.env_type) != "none" and flag_policy_configuration_changed(
        current_type=current_type, current_value=entity.flag_value, current_flags=current_flags,
        proposed_type=flag_type, proposed_value=fields.get("flag_value", entity.flag_value),
        proposed_flags=_effective_flags(current_flags, target.flags),
    )
    if changed and entity.is_published:
        raise import_error("environment_unpublish_required", "공개된 환경과 플래그 정책은 먼저 비공개로 전환한 뒤 수정하세요.", index=record.index, status=409)
    if fields.get("is_published") is True:
        if changed or not environment_is_verified_for_runtime(
            entity.env_type, entity.environment_revision, entity.environment_verified_revision, entity.environment_verified_image_id,
        ):
            raise import_error("environment_verification_required", "환경 설정을 변경한 뒤에는 빌드와 환경 검증을 다시 완료하세요.", index=record.index)
    target.invalidate_environment = changed


async def import_challenge_batch(
    db: AsyncSession, body: dict, *, actor_id: UUID,
    validate_dynamic_policy: Callable[[DynamicFlagPolicyInput], None],
    invalidate_environment: Callable[[Challenge], None],
    revoke_active_issuances: Callable[..., Awaitable[None]],
) -> dict[str, int]:
    mode, records = prepare_import(body)
    if not records:
        return {"created": 0, "updated": 0, "skipped": 0}
    commit_started = False
    try:
        if not await db.scalar(select(func.pg_try_advisory_xact_lock(IMPORT_LOCK_KEY))):
            raise import_error("import_in_progress", "다른 가져오기가 진행 중입니다. 완료 후 다시 시도하세요.", status=409)
        # 일반 편집도 부모를 먼저 잠근다. NOWAIT로 부분 잠금 대기와 교착을 피한다.
        challenges = list((await db.scalars(select(Challenge).order_by(Challenge.id).with_for_update(nowait=True))).all())
        hints = list((await db.scalars(select(Hint).order_by(Hint.challenge_id, Hint.order_index, Hint.id).with_for_update(nowait=True))).all())
        flags = list((await db.scalars(select(Flag).order_by(Flag.challenge_id, Flag.order_index, Flag.id).with_for_update(nowait=True))).all())
        policies = list((await db.scalars(select(ChallengeFlagPolicy).order_by(ChallengeFlagPolicy.challenge_id).with_for_update(nowait=True))).all())
        targets, mapping, skipped, flags_by_parent = plan_import(records, mode, challenges, hints, flags, policies)
        for target in targets:
            validate_target_policy(target, flags_by_parent[target.key], validate_dynamic_policy)
        edges = set((await db.execute(select(ChallengePrerequisite.challenge_id, ChallengePrerequisite.prerequisite_id).with_for_update(nowait=True))).all())
        learning_sources = set((await db.scalars(select(ChallengeLearningPrerequisite.challenge_id))).all())
        additions = validate_import_edges(records, targets, mapping, challenges, edges, learning_sources)
        local_ids = {challenge.id: challenge.id for challenge in challenges}
        for target in targets:
            if target.entity is None:
                target.entity = Challenge(**target.fields)
                db.add(target.entity)
        await db.flush()
        for target in targets:
            entity = target.entity
            assert entity is not None
            previous_files = list(entity.files or []) if target.key > 0 else []
            local_ids[target.key] = entity.id
            if target.key > 0:
                for name, value in target.fields.items():
                    setattr(entity, name, value)
            for model, changes in ((Hint, target.hints), (Flag, target.flags)):
                for child, values in changes:
                    if child is None:
                        db.add(model(challenge_id=entity.id, **values))
                    else:
                        for name, value in values.items():
                            setattr(child, name, value)
            policy = target.record.dynamic_flag
            if policy is not None:
                values = policy.model_dump()
                if target.policy is None:
                    db.add(ChallengeFlagPolicy(challenge_id=entity.id, mode=DYNAMIC_FLAG_TYPE, policy_revision=1, **values))
                elif dynamic_policy_configuration_changed({key: getattr(target.policy, key) for key in POLICY_FIELDS}, values):
                    for name, value in values.items():
                        setattr(target.policy, name, value)
                    target.policy.policy_revision += 1
                    target.policy.updated_at = datetime.now(timezone.utc)
            if target.invalidate_environment:
                invalidate_environment(entity)
                await revoke_active_issuances(db, challenge_id=entity.id)
            if (target.record.attachment_ids is not None or target.record.attachment_draft_id is not None
                    or previous_files != (entity.files or [])):
                await bind_attachments(db, entity, actor_id=actor_id,
                    attachment_ids=target.record.attachment_ids,
                    attachment_draft_id=target.record.attachment_draft_id, previous_files=previous_files,
                    new_challenge=target.key < 0)
        for source, destination in sorted(additions):
            db.add(ChallengePrerequisite(challenge_id=local_ids[source], prerequisite_id=local_ids[destination]))
        await db.flush()
        for target in targets:
            await project_rule_tag_mappings(db, entity_type="challenge", entity=target.entity, actor_id=actor_id)
        commit_started = True
        await db.commit()
        return {"created": sum(target.key < 0 for target in targets),
                "updated": sum(target.key > 0 for target in targets), "skipped": skipped}
    except DBAPIError as error:
        rollback_confirmed = await _rollback_import(db)
        code = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
        if not rollback_confirmed:
            raise import_error("import_outcome_unknown", "처리 결과를 확인할 수 없습니다. 다시 가져오기 전에 현재 목록을 확인하세요.", status=503) from error
        if code in {"55P03", "40P01", "40001"}:
            raise import_error("import_conflict", "다른 편집과 충돌했습니다. 현재 상태를 확인한 뒤 다시 시도하세요.", status=409) from error
        if commit_started and (error.connection_invalidated or code is None or code.startswith("08")):
            raise import_error("import_outcome_unknown", "처리 결과를 확인할 수 없습니다. 다시 가져오기 전에 현재 목록을 확인하세요.", status=503) from error
        raise import_error("import_storage_rejected", "가져오기 저장 검증에 실패하여 전체 변경을 취소했습니다.") from error
    except BaseException:
        await _rollback_import(db)
        raise


async def _rollback_import(db: AsyncSession) -> bool:
    try:
        await db.rollback()
        return True
    except BaseException:
        # 원래 실패/취소를 보존한다. 실패한 rollback을 성공으로 보고하지 않는다.
        return False


async def export_challenge_batch(db: AsyncSession) -> dict:
    # Endpoint의 첫 DB 작업이다. 모든 관계 query가 동일한 읽기 전용 snapshot을 쓴다.
    await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    challenges = list((await db.scalars(select(Challenge).options(
        selectinload(Challenge.hints), selectinload(Challenge.flags), selectinload(Challenge.dynamic_flag_policy),
    ).order_by(Challenge.id))).all())
    edges = (await db.execute(select(ChallengePrerequisite.challenge_id, ChallengePrerequisite.prerequisite_id)
                              .order_by(ChallengePrerequisite.challenge_id, ChallengePrerequisite.prerequisite_id))).all()
    prerequisites: dict[int, list[int]] = defaultdict(list)
    for source, destination in edges:
        prerequisites[source].append(destination)
    assets = list((await db.scalars(select(ChallengeAttachment).where(ChallengeAttachment.challenge_id.is_not(None))
        .order_by(ChallengeAttachment.challenge_id, ChallengeAttachment.position, ChallengeAttachment.id))).all())
    by_parent = defaultdict(list)
    for asset in assets:
        by_parent[asset.challenge_id].append(asset)
    records = []
    for challenge in challenges:
        record = export_challenge_record(challenge, prerequisites[challenge.id])
        record["attachment_ids"] = [str(row.id) for row in by_parent[challenge.id]]
        record["attachments"] = [{"id": str(row.id), "name": row.original_name,
            "byte_size": row.byte_size, "sha256": row.sha256} for row in by_parent[challenge.id]]
        records.append(record)
    return {"version": 2, "challenges": records}


def export_challenge_record(challenge: Challenge, prerequisites: list[int]) -> dict:
    result = {name: getattr(challenge, name) for name in SCALAR_FIELDS}
    is_dynamic = challenge.flag_type == DYNAMIC_FLAG_TYPE
    result.update({
        "id": challenge.id,
        "flag_value": "" if is_dynamic else challenge.flag_value,
        "tags": challenge.tags or [], "files": challenge.files or [],
        "hints": [{"id": child.id, "content": child.content, "cost": child.cost, "order_index": child.order_index}
                  for child in sorted(challenge.hints, key=lambda item: (item.order_index, item.id))],
        "flags": [] if is_dynamic else [{"id": child.id, "type": child.type, "value": child.value,
                    "case_sensitive": child.case_sensitive, "order_index": child.order_index}
                   for child in sorted(challenge.flags, key=lambda item: (item.order_index, item.id))],
        "prerequisite_ids": prerequisites,
        "dynamic_flag": ({name: getattr(challenge.dynamic_flag_policy, name) for name in POLICY_FIELDS}
                         if is_dynamic and challenge.dynamic_flag_policy is not None else None),
    })
    return result
