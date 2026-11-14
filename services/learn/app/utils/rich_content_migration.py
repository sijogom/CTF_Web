"""Dry-run-first, reversible canonical HTML migration support.

The engine never logs or prints content. A reviewed manifest is the recovery
record: it stores exact before/after values, hashes, bounded previews, and
component-level review reasons outside deployable service directories.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Literal
from uuid import UUID

from sqlalchemy import Table, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.utils.rich_content import (
    RichContentError,
    classify_rich_content,
    normalize_rich_content_for_write,
    rich_content_to_plain_text,
)


MigrationValueKind = Literal["scalar", "mapping", "hint_list"]
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MANIFEST_VERSION = 1
_PREVIEW_LIMIT = 160


class RichContentMigrationError(RuntimeError):
    """Base error that never embeds a rich-content value."""


class RichContentMigrationConflict(RichContentMigrationError):
    """The current row no longer matches the reviewed manifest."""


class RichContentMigrationPartialFailure(RichContentMigrationError):
    """A batch failed after zero or more earlier batches committed."""

    def __init__(self, message: str, *, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class RichContentFieldSpec:
    table: Table
    column_name: str
    kind: MigrationValueKind = "scalar"
    field_name: str = "본문"
    allow_empty: bool = False
    max_visible_chars: int | None = None
    nullable: bool = False
    guard_columns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.table.name):
            raise ValueError("안전하지 않은 테이블 식별자입니다.")
        if self.column_name not in self.table.c:
            raise ValueError(f"{self.table.name}.{self.column_name} 컬럼이 없습니다.")
        primary_keys = list(self.table.primary_key.columns)
        if len(primary_keys) != 1:
            raise ValueError(f"{self.table.name}에는 단일 기본 키가 필요합니다.")
        for column_name in self.guard_columns:
            if column_name not in self.table.c:
                raise ValueError(f"{self.table.name}.{column_name} guard 컬럼이 없습니다.")

    @property
    def primary_key(self):
        return list(self.table.primary_key.columns)[0]

    @property
    def content_column(self):
        return self.table.c[self.column_name]

    @property
    def key(self) -> str:
        return f"{self.table.name}.{self.column_name}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _serialized(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def value_sha256(value: Any) -> str:
    return hashlib.sha256(_serialized(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_target_fingerprint(engine: AsyncEngine, service: str) -> str:
    """Bind a manifest to one non-secret database connection identity."""

    url = engine.url
    identity = {
        "service": service,
        "drivername": url.drivername,
        "host": url.host or "",
        "port": url.port,
        "database": url.database or "",
    }
    return value_sha256(identity)


def _scalar_preview(value: str) -> str:
    try:
        preview = rich_content_to_plain_text(value)
    except RichContentError:
        preview = re.sub(r"\s+", " ", value).strip()
    return preview[:_PREVIEW_LIMIT]


def _preview(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _scalar_preview(value)
    if isinstance(value, dict):
        parts = [
            f"{key}: {_scalar_preview(item)}"
            for key, item in sorted(value.items())
            if isinstance(key, str) and isinstance(item, str)
        ]
        return " | ".join(parts)[:_PREVIEW_LIMIT]
    if isinstance(value, list):
        parts: list[str] = []
        for index, item in enumerate(value):
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                parts.append(f"{index}: {_scalar_preview(item['content'])}")
        return " | ".join(parts)[:_PREVIEW_LIMIT]
    return ""


def _normalize_scalar(
    value: Any,
    *,
    spec: RichContentFieldSpec,
    component: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    if value is None and spec.nullable:
        return None, {
            "component": component,
            "status": "already_canonical",
            "kind": "null",
            "before_hash": value_sha256(None),
            "after_hash": value_sha256(None),
            "before_preview": "",
            "after_preview": "",
        }
    if not isinstance(value, str):
        return value, {
            "component": component,
            "status": "error",
            "code": "rich_content_invalid_type",
            "kind": "invalid",
            "before_hash": value_sha256(value),
            "after_hash": value_sha256(value),
            "before_preview": "",
            "after_preview": "",
        }

    kind = classify_rich_content(value).value
    try:
        normalized = normalize_rich_content_for_write(
            value,
            field_name=spec.field_name,
            allow_empty=spec.allow_empty,
            max_visible_chars=spec.max_visible_chars,
        )
    except RichContentError as error:
        return value, {
            "component": component,
            "status": "review_required",
            "code": error.code,
            "kind": kind,
            "before_hash": value_sha256(value),
            "after_hash": value_sha256(value),
            "before_preview": _preview(value),
            "after_preview": _preview(value),
        }
    return normalized, {
        "component": component,
        "status": "already_canonical" if normalized == value else "convertible",
        "kind": kind,
        "before_hash": value_sha256(value),
        "after_hash": value_sha256(normalized),
        "before_preview": _preview(value),
        "after_preview": _preview(normalized),
    }


def plan_value(spec: RichContentFieldSpec, value: Any) -> dict[str, Any]:
    components: list[dict[str, Any]] = []

    if spec.kind == "scalar":
        normalized, component = _normalize_scalar(value, spec=spec)
        components.append(component)
    elif spec.kind == "mapping":
        if not isinstance(value, dict):
            normalized = value
            components.append({
                "component": None,
                "status": "error",
                "code": "rich_content_invalid_mapping",
                "kind": "invalid",
            })
        else:
            candidate: dict[str, Any] = {}
            for language in sorted(value):
                content = value[language]
                if not isinstance(language, str):
                    candidate[language] = content
                    components.append({
                        "component": str(language),
                        "status": "error",
                        "code": "rich_content_invalid_language",
                        "kind": "invalid",
                    })
                    continue
                normalized_content, component = _normalize_scalar(
                    content,
                    spec=spec,
                    component=language,
                )
                candidate[language] = normalized_content
                components.append(component)
            normalized = candidate
    elif spec.kind == "hint_list":
        if not isinstance(value, list):
            normalized = value
            components.append({
                "component": None,
                "status": "error",
                "code": "rich_content_invalid_hint_list",
                "kind": "invalid",
            })
        else:
            candidate_list: list[Any] = []
            for index, hint in enumerate(value):
                if not isinstance(hint, dict) or "content" not in hint:
                    candidate_list.append(hint)
                    components.append({
                        "component": str(index),
                        "status": "error",
                        "code": "rich_content_invalid_hint",
                        "kind": "invalid",
                    })
                    continue
                normalized_content, component = _normalize_scalar(
                    hint.get("content"),
                    spec=spec,
                    component=str(index),
                )
                candidate_list.append({**hint, "content": normalized_content})
                components.append(component)
            normalized = candidate_list
    else:  # pragma: no cover - Literal plus dataclass construction guard
        raise ValueError(f"지원하지 않는 migration kind: {spec.kind}")

    blocked = any(
        component["status"] in {"review_required", "error"}
        for component in components
    )
    if blocked:
        # JSON documents are the transaction unit. A single bad language/hint
        # preserves the whole original document; successful siblings are not
        # partially written.
        normalized = value
        status = "review_required"
        reason = "component_review_required_atomic_field_preserved"
    elif normalized == value:
        status = "already_canonical"
        reason = None
    else:
        status = "convertible"
        reason = None

    return {
        "status": status,
        "reason": reason,
        "before_hash": value_sha256(value),
        "after_hash": value_sha256(normalized),
        "before_value": _json_safe(value),
        "after_value": _json_safe(normalized),
        "before_preview": _preview(value),
        "after_preview": _preview(normalized),
        "components": components,
    }


def _find_workspace_root() -> Path | None:
    candidates = [Path(__file__).resolve(), Path.cwd().resolve()]
    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / "services").is_dir() and (parent / "frontend").is_dir():
                return parent
    return None


def default_manifest_path(service: str) -> Path:
    root = _find_workspace_root()
    if root is None:
        raise RichContentMigrationError(
            "비배포 .verification/output 경로를 찾지 못했습니다. "
            "--manifest-out으로 안전한 복구 경로를 지정하세요."
        )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        root
        / ".verification"
        / "output"
        / "rich-content"
        / f"{service}-{timestamp}.json"
    )


def write_manifest_exclusive(path: Path, manifest: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    return file_sha256(path)


def _receipt_path(manifest_path: Path, mode: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return manifest_path.with_name(
        f"{manifest_path.stem}.{mode}-{timestamp}.receipt.json"
    )


def _replace_owned_receipt(path: Path, receipt: dict[str, Any]) -> str:
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    return file_sha256(path)


def _guard_hash(spec: RichContentFieldSpec, row_mapping: Any) -> str:
    values = {
        name: _json_safe(row_mapping[spec.table.c[name]])
        for name in spec.guard_columns
    }
    return value_sha256(values)


async def build_manifest(
    engine: AsyncEngine,
    *,
    service: str,
    environment: str,
    specs: Iterable[RichContentFieldSpec],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    field_contracts: list[dict[str, Any]] = []

    async with engine.connect() as connection:
        for spec in specs:
            selected = [
                spec.primary_key,
                spec.content_column,
                *(spec.table.c[name] for name in spec.guard_columns),
            ]
            statement = select(*selected).order_by(spec.primary_key)
            result = await connection.stream(statement)
            async for row in result:
                mapping = row._mapping
                entry = {
                    "table": spec.table.name,
                    "column": spec.column_name,
                    "primary_key_column": spec.primary_key.name,
                    "primary_key": str(mapping[spec.primary_key]),
                    "guard_columns": list(spec.guard_columns),
                    "guard_hash_at_scan": _guard_hash(spec, mapping),
                }
                entry.update(plan_value(spec, mapping[spec.content_column]))
                entries.append(entry)
            field_contracts.append({
                "table": spec.table.name,
                "column": spec.column_name,
                "primary_key_column": spec.primary_key.name,
                "kind": spec.kind,
                "allow_empty": spec.allow_empty,
                "max_visible_chars": spec.max_visible_chars,
                "nullable": spec.nullable,
                "guard_columns": list(spec.guard_columns),
            })

    summary = dict(Counter(entry["status"] for entry in entries))
    return {
        "manifest_version": _MANIFEST_VERSION,
        "service": service,
        "environment": environment,
        "database_target_fingerprint": database_target_fingerprint(engine, service),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "atomic_unit": (
            "scalar column or whole JSON mapping/list; component failures preserve "
            "the entire original column"
        ),
        "fields": field_contracts,
        "summary": summary,
        "entries": entries,
    }


def load_reviewed_manifest(
    path: Path,
    *,
    expected_sha256: str,
    service: str,
    environment: str,
    expected_environment: str,
    target_fingerprint: str,
) -> dict[str, Any]:
    actual_sha256 = file_sha256(path)
    if not hmac.compare_digest(actual_sha256.lower(), expected_sha256.lower()):
        raise RichContentMigrationError("검토된 manifest SHA-256이 현재 파일과 일치하지 않습니다.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != _MANIFEST_VERSION:
        raise RichContentMigrationError("지원하지 않는 rich-content manifest 버전입니다.")
    if manifest.get("service") != service:
        raise RichContentMigrationError("manifest 서비스가 실행 대상과 일치하지 않습니다.")
    if not expected_environment or environment != expected_environment:
        raise RichContentMigrationError("현재 환경이 --expected-environment와 일치하지 않습니다.")
    if manifest.get("environment") != environment:
        raise RichContentMigrationError("manifest를 생성한 환경과 현재 환경이 일치하지 않습니다.")
    if not hmac.compare_digest(
        str(manifest.get("database_target_fingerprint", "")),
        target_fingerprint,
    ):
        raise RichContentMigrationError("manifest 데이터베이스 대상이 현재 연결과 일치하지 않습니다.")
    if not isinstance(manifest.get("entries"), list):
        raise RichContentMigrationError("manifest entries 형식이 올바르지 않습니다.")
    return manifest


def _deserialize_primary_key(spec: RichContentFieldSpec, value: str) -> Any:
    python_type = spec.primary_key.type.python_type
    if python_type is UUID:
        return UUID(value)
    if python_type is int:
        return int(value)
    return value


def _mutation_statement(spec: RichContentFieldSpec, dialect):
    quote = dialect.identifier_preparer.quote
    table_name = quote(spec.table.name)
    column_name = quote(spec.column_name)
    primary_key_name = quote(spec.primary_key.name)
    return text(
        f"UPDATE {table_name} SET {column_name} = :replacement "
        f"WHERE {primary_key_name} = :primary_key "
        f"AND {column_name} IS NOT DISTINCT FROM :expected"
    ).bindparams(
        bindparam("replacement", type_=spec.content_column.type),
        bindparam("primary_key", type_=spec.primary_key.type),
        bindparam("expected", type_=spec.content_column.type),
    )


def _entry_value(entry: dict[str, Any], name: str) -> Any:
    return entry.get(name)


async def execute_manifest(
    engine: AsyncEngine,
    *,
    manifest: dict[str, Any],
    specs: Iterable[RichContentFieldSpec],
    mode: Literal["apply", "rollback"],
    batch_size: int = 100,
) -> dict[str, int]:
    if batch_size < 1:
        raise RichContentMigrationError("batch_size는 1 이상이어야 합니다.")
    specs_by_key = {spec.key: spec for spec in specs}
    actionable = [
        entry for entry in manifest["entries"]
        if entry.get("status") == "convertible"
    ]
    counts: Counter[str] = Counter()
    completed_batches = 0

    async with engine.connect() as connection:
        for batch_start in range(0, len(actionable), batch_size):
            batch = actionable[batch_start:batch_start + batch_size]
            batch_counts: Counter[str] = Counter()
            try:
                async with connection.begin():
                    for entry in batch:
                        key = f"{entry.get('table')}.{entry.get('column')}"
                        spec = specs_by_key.get(key)
                        if spec is None:
                            raise RichContentMigrationError(
                                f"manifest 필드 {key}가 현재 서비스 계약에 없습니다."
                            )
                        if entry.get("primary_key_column") != spec.primary_key.name:
                            raise RichContentMigrationError(
                                f"manifest 기본 키 계약이 {key}와 일치하지 않습니다."
                            )
    
                        primary_key = _deserialize_primary_key(
                            spec,
                            str(entry.get("primary_key")),
                        )
                        selected = [
                            spec.content_column,
                            *(spec.table.c[name] for name in spec.guard_columns),
                        ]
                        row = (
                            await connection.execute(
                                select(*selected)
                                .where(spec.primary_key == primary_key)
                                .with_for_update()
                            )
                        ).first()
                        if row is None:
                            raise RichContentMigrationConflict(
                                f"{key} 대상 행을 찾을 수 없습니다: {primary_key}"
                            )
                        mapping = row._mapping
                        current = mapping[spec.content_column]
                        expected_name = "before_value" if mode == "apply" else "after_value"
                        target_name = "after_value" if mode == "apply" else "before_value"
                        expected_hash_name = "before_hash" if mode == "apply" else "after_hash"
                        target_hash_name = "after_hash" if mode == "apply" else "before_hash"
                        expected = _entry_value(entry, expected_name)
                        target = _entry_value(entry, target_name)
    
                        current_hash = value_sha256(current)
                        if current_hash == entry.get(target_hash_name) and current == target:
                            batch_counts[f"already_{mode}"] += 1
                            continue
                        if current_hash != entry.get(expected_hash_name) or current != expected:
                            raise RichContentMigrationConflict(
                                f"{key} 본문이 검토 후 변경되었습니다: {primary_key}"
                            )
    
                        guards_before = _guard_hash(spec, mapping)
                        result = await connection.execute(
                            _mutation_statement(spec, connection.dialect),
                            {
                                "replacement": target,
                                "primary_key": primary_key,
                                "expected": expected,
                            },
                        )
                        if result.rowcount != 1:
                            raise RichContentMigrationConflict(
                                f"{key} 조건부 갱신이 충돌했습니다: {primary_key}"
                            )
    
                        verified = (
                            await connection.execute(
                                select(*selected).where(spec.primary_key == primary_key)
                            )
                        ).first()
                        if verified is None:
                            raise RichContentMigrationConflict(
                                f"{key} 갱신 후 행이 사라졌습니다: {primary_key}"
                            )
                        verified_mapping = verified._mapping
                        if (
                            value_sha256(verified_mapping[spec.content_column])
                            != entry.get(target_hash_name)
                            or verified_mapping[spec.content_column] != target
                        ):
                            raise RichContentMigrationConflict(
                                f"{key} 갱신 결과 해시가 일치하지 않습니다: {primary_key}"
                            )
                        if _guard_hash(spec, verified_mapping) != guards_before:
                            raise RichContentMigrationConflict(
                                f"{key} 갱신 중 메타데이터가 변경되었습니다: {primary_key}"
                            )
                        batch_counts[mode] += 1
            except Exception as error:
                failure_result = dict(counts)
                failure_result.update({
                    "actionable": len(actionable),
                    "completed_batches": completed_batches,
                    "failed_batch_index": batch_start // batch_size,
                    "committed_rows": counts.get(mode, 0),
                    "partial_changes_committed": counts.get(mode, 0) > 0,
                    "failure_type": type(error).__name__,
                })
                raise RichContentMigrationPartialFailure(
                    "rich-content migration batch가 실패했습니다. "
                    "이전 완료 batch는 commit되었을 수 있습니다.",
                    result=failure_result,
                ) from error
            counts.update(batch_counts)
            completed_batches += 1

    counts["actionable"] = len(actionable)
    counts["completed_batches"] = completed_batches
    counts["skipped_review_required"] = sum(
        entry.get("status") == "review_required"
        for entry in manifest["entries"]
    )
    counts["skipped_already_canonical"] = sum(
        entry.get("status") == "already_canonical"
        for entry in manifest["entries"]
    )
    return dict(counts)


def create_argument_parser(service: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"{service} rich-content canonical HTML migration",
    )
    parser.add_argument(
        "--mode",
        choices=("dry-run", "apply", "rollback"),
        default="dry-run",
    )
    parser.add_argument("--manifest-out", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--reviewed-manifest-sha256")
    parser.add_argument("--expected-environment")
    parser.add_argument("--batch-size", type=int, default=100)
    return parser


def run_cli(
    *,
    service: str,
    environment: str,
    engine: AsyncEngine,
    specs: Iterable[RichContentFieldSpec],
) -> None:
    parser = create_argument_parser(service)
    arguments = parser.parse_args()
    specs = tuple(specs)

    if arguments.mode == "dry-run":
        if arguments.manifest is not None or arguments.reviewed_manifest_sha256:
            parser.error("dry-run은 --manifest/--reviewed-manifest-sha256를 받지 않습니다.")
        path = arguments.manifest_out or default_manifest_path(service)
        manifest = asyncio.run(
            build_manifest(
                engine,
                service=service,
                environment=environment,
                specs=specs,
            )
        )
        digest = write_manifest_exclusive(path.resolve(), manifest)
        print(json.dumps({
            "mode": "dry-run",
            "service": service,
            "manifest": str(path.resolve()),
            "manifest_sha256": digest,
            "summary": manifest["summary"],
        }, ensure_ascii=False))
        return

    if arguments.manifest_out is not None:
        parser.error("apply/rollback은 --manifest-out을 받지 않습니다.")
    if arguments.manifest is None:
        parser.error("apply/rollback에는 --manifest가 필요합니다.")
    if not arguments.reviewed_manifest_sha256:
        parser.error("apply/rollback에는 --reviewed-manifest-sha256가 필요합니다.")
    if not arguments.expected_environment:
        parser.error("apply/rollback에는 --expected-environment가 필요합니다.")

    manifest = load_reviewed_manifest(
        arguments.manifest.resolve(),
        expected_sha256=arguments.reviewed_manifest_sha256,
        service=service,
        environment=environment,
        expected_environment=arguments.expected_environment,
        target_fingerprint=database_target_fingerprint(engine, service),
    )
    receipt_path = _receipt_path(arguments.manifest.resolve(), arguments.mode)
    base_receipt = {
        "receipt_version": 1,
        "service": service,
        "mode": arguments.mode,
        "manifest_sha256": arguments.reviewed_manifest_sha256.lower(),
        "database_target_fingerprint": database_target_fingerprint(engine, service),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "started",
    }
    write_manifest_exclusive(receipt_path, base_receipt)
    try:
        result = asyncio.run(
            execute_manifest(
                engine,
                manifest=manifest,
                specs=specs,
                mode=arguments.mode,
                batch_size=arguments.batch_size,
            )
        )
    except RichContentMigrationPartialFailure as error:
        failed_receipt = {
            **base_receipt,
            "status": "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "result": error.result,
        }
        receipt_sha256 = _replace_owned_receipt(receipt_path, failed_receipt)
        print(json.dumps({
            "mode": arguments.mode,
            "service": service,
            "status": "failed",
            "manifest_sha256": arguments.reviewed_manifest_sha256.lower(),
            "receipt": str(receipt_path),
            "receipt_sha256": receipt_sha256,
            "result": error.result,
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from error
    completed_receipt = {
        **base_receipt,
        "status": "completed",
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    }
    receipt_sha256 = _replace_owned_receipt(receipt_path, completed_receipt)
    print(json.dumps({
        "mode": arguments.mode,
        "service": service,
        "manifest_sha256": arguments.reviewed_manifest_sha256.lower(),
        "receipt": str(receipt_path),
        "receipt_sha256": receipt_sha256,
        "result": result,
    }, ensure_ascii=False))
