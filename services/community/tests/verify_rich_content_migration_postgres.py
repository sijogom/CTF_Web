"""Acceptance-only PostgreSQL behavior check for rich-content migration.

This script creates one uniquely named table in the task-owned acceptance
database, exercises the product migration engine and CLI receipts, then drops
that exact table. It prints only hashes, counts, and boolean assertions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB

from app.config import settings
from app.database import engine
from app.utils.rich_content_migration import (
    RichContentFieldSpec,
    file_sha256,
    run_cli,
    value_sha256,
)


_ACCEPTANCE_DATABASE_HOST = "127.0.0.1"
_ACCEPTANCE_DATABASE_PORT = 15432
_ACCEPTANCE_DATABASE_NAME = "ctf_acceptance_community"
_ACCEPTANCE_ENVIRONMENT = "development"


def _assert_acceptance_target(*, database_url=None, environment: str | None = None) -> None:  # type: ignore[no-untyped-def]
    """Fail before connecting unless this is the exact task-owned database."""

    url = engine.url if database_url is None else database_url
    actual_environment = settings.environment if environment is None else environment
    if not (
        url.host == _ACCEPTANCE_DATABASE_HOST
        and url.port == _ACCEPTANCE_DATABASE_PORT
        and url.database == _ACCEPTANCE_DATABASE_NAME
        and actual_environment == _ACCEPTANCE_ENVIRONMENT
    ):
        raise RuntimeError("acceptance database target does not match the owned fixture")


def _contract_table(name: str) -> Table:
    metadata = MetaData()
    return Table(
        name,
        metadata,
        Column("id", Integer, primary_key=True),
        Column("content", Text, nullable=False),
        Column("localized", JSONB, nullable=False),
        Column("hints", JSONB, nullable=False),
        Column("author_id", String(64), nullable=False),
        Column("status", String(32), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )


def _specs(table: Table) -> tuple[RichContentFieldSpec, ...]:
    guards = ("author_id", "status", "created_at", "updated_at")
    return (
        RichContentFieldSpec(
            table,
            "content",
            field_name="PostgreSQL acceptance 본문",
            guard_columns=guards,
        ),
        RichContentFieldSpec(
            table,
            "localized",
            kind="mapping",
            field_name="PostgreSQL acceptance 번역",
            guard_columns=guards,
        ),
        RichContentFieldSpec(
            table,
            "hints",
            kind="hint_list",
            field_name="PostgreSQL acceptance 힌트",
            guard_columns=guards,
        ),
    )


def _last_json(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AssertionError("migration command did not return a JSON result")


def _run_tool(
    table_name: str,
    arguments: list[str],
    *,
    expected_returncode: int,
) -> tuple[subprocess.CompletedProcess[str], dict | None]:
    environment = os.environ.copy()
    environment["RICH_MIGRATION_CONTRACT_TABLE"] = table_name
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "_tool", *arguments],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != expected_returncode:
        raise AssertionError(
            f"migration command returned {completed.returncode}, "
            f"expected {expected_returncode}"
        )
    combined = "\n".join((completed.stdout, completed.stderr))
    try:
        payload = _last_json(combined)
    except AssertionError:
        payload = None
    return completed, payload


async def _fetch_rows(table: Table) -> dict[int, dict]:
    async with engine.connect() as connection:
        result = await connection.execute(select(table).order_by(table.c.id))
        return {
            int(row._mapping[table.c.id]): dict(row._mapping)
            for row in result
        }


def _guard_hashes(table: Table, rows: dict[int, dict]) -> dict[int, str]:
    return {
        row_id: value_sha256({
            name: row[name]
            for name in ("author_id", "status", "created_at", "updated_at")
        })
        for row_id, row in rows.items()
    }


async def _run_contract(output_root: Path) -> dict:
    _assert_acceptance_target()
    resolved_output = output_root.resolve()
    if ".verification" not in {
        part.lower() for part in resolved_output.parts
    }:
        raise RuntimeError("acceptance output must stay under .verification")
    resolved_output.mkdir(parents=True, exist_ok=True)

    run_id = uuid4().hex[:12]
    table_name = f"rich_content_migration_acceptance_{run_id}"
    table = _contract_table(table_name)
    original_one = "첫 번째 **본문**"
    original_two = "둘째 본문"
    concurrent_two = "동시 편집 본문"
    original_mapping_one = {"ko": "첫 **문장**", "en": "<p>Ready</p>"}
    original_mapping_two = {"ko": "정상 **후보**", "en": "문자열 <FLAG> 예제"}
    original_hints_one = [
        {"content": "첫 *힌트*", "cost": 3},
        {"content": "<p>Second</p>", "cost": 5},
    ]
    original_hints_two = [{"content": "두 번째 **힌트**", "cost": 7}]
    expected_mapping_one = {"ko": "<p>첫 <strong>문장</strong></p>", "en": "<p>Ready</p>"}
    expected_hints_one = [
        {"content": "<p>첫 <em>힌트</em></p>", "cost": 3},
        {"content": "<p>Second</p>", "cost": 5},
    ]
    expected_hints_two = [{"content": "<p>두 번째 <strong>힌트</strong></p>", "cost": 7}]
    timestamp = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
    manifest_path = resolved_output / f"{table_name}.manifest.json"
    mismatch_path = resolved_output / f"{table_name}.target-mismatch.json"

    table_created = False
    try:
        async with engine.begin() as connection:
            await connection.run_sync(table.create)
            table_created = True
            await connection.execute(
                insert(table),
                [
                    {
                        "id": 1,
                        "content": original_one,
                        "localized": original_mapping_one,
                        "hints": original_hints_one,
                        "author_id": "author-one",
                        "status": "published",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                    {
                        "id": 2,
                        "content": original_two,
                        "localized": original_mapping_two,
                        "hints": original_hints_two,
                        "author_id": "author-two",
                        "status": "draft",
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                ],
            )
        initial_rows = await _fetch_rows(table)
        initial_guards = _guard_hashes(table, initial_rows)

        _, dry_run = _run_tool(
            table_name,
            ["--mode", "dry-run", "--manifest-out", str(manifest_path)],
            expected_returncode=0,
        )
        assert dry_run is not None
        manifest_sha256 = str(dry_run["manifest_sha256"])

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        blocked_mapping = next(
            entry
            for entry in manifest["entries"]
            if entry["column"] == "localized" and entry["primary_key"] == "2"
        )
        component_statuses = {
            component["component"]: component["status"]
            for component in blocked_mapping["components"]
        }
        if not (
            blocked_mapping["status"] == "review_required"
            and blocked_mapping["before_value"] == original_mapping_two
            and blocked_mapping["after_value"] == original_mapping_two
            and blocked_mapping["before_hash"] == blocked_mapping["after_hash"]
            and component_statuses == {"en": "review_required", "ko": "convertible"}
        ):
            raise AssertionError("JSONB mapping component failure was not field-atomic")

        mismatch_manifest = dict(manifest)
        mismatch_manifest["database_target_fingerprint"] = "0" * 64
        mismatch_path.write_text(
            json.dumps(mismatch_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        mismatch_digest = file_sha256(mismatch_path)
        mismatch_result, _ = _run_tool(
            table_name,
            [
                "--mode", "apply",
                "--manifest", str(mismatch_path),
                "--reviewed-manifest-sha256", mismatch_digest,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=1,
        )
        mismatch_blocked = (
            "manifest 데이터베이스 대상이 현재 연결과 일치하지 않습니다."
            in mismatch_result.stderr
        )
        if not mismatch_blocked:
            raise AssertionError("runtime target fingerprint mismatch was not blocked")

        async with engine.begin() as connection:
            await connection.execute(
                update(table).where(table.c.id == 2).values(content=concurrent_two)
            )

        _, partial_apply = _run_tool(
            table_name,
            [
                "--mode", "apply",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=2,
        )
        assert partial_apply is not None
        partial_apply_receipt = json.loads(
            Path(partial_apply["receipt"]).read_text(encoding="utf-8")
        )
        if not (
            partial_apply["result"]["completed_batches"] == 1
            and partial_apply["result"]["committed_rows"] == 1
            and partial_apply["result"]["partial_changes_committed"] is True
            and partial_apply_receipt["status"] == "failed"
        ):
            raise AssertionError("partial apply receipt did not preserve progress")

        rows_after_partial_apply = await _fetch_rows(table)
        if rows_after_partial_apply[1]["content"] != "<p>첫 번째 <strong>본문</strong></p>":
            raise AssertionError("first batch was not committed")
        if rows_after_partial_apply[2]["content"] != concurrent_two:
            raise AssertionError("concurrent content was overwritten")
        if _guard_hashes(table, rows_after_partial_apply) != initial_guards:
            raise AssertionError("metadata changed during partial apply")
        if not (
            rows_after_partial_apply[1]["localized"] == original_mapping_one
            and rows_after_partial_apply[1]["hints"] == original_hints_one
            and rows_after_partial_apply[2]["localized"] == original_mapping_two
            and rows_after_partial_apply[2]["hints"] == original_hints_two
        ):
            raise AssertionError("later JSONB fields changed before their committed batch")

        _, retry_conflict = _run_tool(
            table_name,
            [
                "--mode", "apply",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=2,
        )
        assert retry_conflict is not None
        if retry_conflict["result"]["partial_changes_committed"] is not False:
            raise AssertionError("idempotent skipped batch was reported as a new partial write")

        _, partial_rollback = _run_tool(
            table_name,
            [
                "--mode", "rollback",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=2,
        )
        assert partial_rollback is not None
        if not (
            partial_rollback["result"]["completed_batches"] == 1
            and partial_rollback["result"]["committed_rows"] == 1
            and partial_rollback["result"]["partial_changes_committed"] is True
        ):
            raise AssertionError("partial rollback progress was not recorded")
        rows_after_partial_rollback = await _fetch_rows(table)
        if rows_after_partial_rollback[1]["content"] != original_one:
            raise AssertionError("rollback did not restore the matching first row")
        if rows_after_partial_rollback[2]["content"] != concurrent_two:
            raise AssertionError("rollback overwrote concurrent content")

        async with engine.begin() as connection:
            await connection.execute(
                update(table).where(table.c.id == 2).values(content=original_two)
            )

        _, full_apply = _run_tool(
            table_name,
            [
                "--mode", "apply",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=0,
        )
        assert full_apply is not None
        if full_apply["result"].get("apply") != 5:
            raise AssertionError("full apply did not update all scalar and JSONB fields")

        rows_after_full_apply = await _fetch_rows(table)
        if not (
            rows_after_full_apply[1]["localized"] == expected_mapping_one
            and rows_after_full_apply[1]["hints"] == expected_hints_one
            and rows_after_full_apply[2]["localized"] == original_mapping_two
            and rows_after_full_apply[2]["hints"] == expected_hints_two
        ):
            raise AssertionError("JSONB apply did not preserve the reviewed field units")

        _, idempotent_apply = _run_tool(
            table_name,
            [
                "--mode", "apply",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=0,
        )
        assert idempotent_apply is not None
        if idempotent_apply["result"].get("already_apply") != 5:
            raise AssertionError("apply rerun was not idempotent")

        _, full_rollback = _run_tool(
            table_name,
            [
                "--mode", "rollback",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=0,
        )
        assert full_rollback is not None
        if full_rollback["result"].get("rollback") != 5:
            raise AssertionError("full rollback did not restore scalar and JSONB originals")

        _, idempotent_rollback = _run_tool(
            table_name,
            [
                "--mode", "rollback",
                "--manifest", str(manifest_path),
                "--reviewed-manifest-sha256", manifest_sha256,
                "--expected-environment", settings.environment,
                "--batch-size", "1",
            ],
            expected_returncode=0,
        )
        assert idempotent_rollback is not None
        if idempotent_rollback["result"].get("already_rollback") != 5:
            raise AssertionError("rollback rerun was not idempotent")

        final_rows = await _fetch_rows(table)
        if (
            final_rows[1]["content"] != original_one
            or final_rows[2]["content"] != original_two
            or final_rows[1]["localized"] != original_mapping_one
            or final_rows[2]["localized"] != original_mapping_two
            or final_rows[1]["hints"] != original_hints_one
            or final_rows[2]["hints"] != original_hints_two
        ):
            raise AssertionError("final rollback did not restore originals")
        guard_preserved = _guard_hashes(table, final_rows) == initial_guards
        if not guard_preserved:
            raise AssertionError("metadata guards changed")

        return {
            "postgresql_behavior_verified": True,
            "target_fingerprint_mismatch_blocked": mismatch_blocked,
            "partial_apply_receipt_verified": True,
            "retry_conflict_verified": True,
            "partial_rollback_receipt_verified": True,
            "idempotent_apply_verified": True,
            "idempotent_rollback_verified": True,
            "metadata_guards_preserved": guard_preserved,
            "jsonb_mapping_binding_verified": True,
            "jsonb_hint_list_binding_verified": True,
            "jsonb_component_failure_field_atomic": True,
            "jsonb_original_rollback_verified": True,
            "manifest_sha256": manifest_sha256,
            "fixture_rows": 2,
        }
    finally:
        if table_created:
            async with engine.begin() as connection:
                await connection.run_sync(table.drop, checkfirst=True)
        await engine.dispose()


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "_tool":
        _assert_acceptance_target()
        table_name = os.environ.get("RICH_MIGRATION_CONTRACT_TABLE", "")
        if not table_name.startswith("rich_content_migration_acceptance_"):
            raise RuntimeError("acceptance table identity is missing")
        table = _contract_table(table_name)
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        run_cli(
            service="community",
            environment=settings.environment,
            engine=engine,
            specs=_specs(table),
        )
        return
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_rich_content_migration_postgres.py OUTPUT_DIR")
    result = asyncio.run(_run_contract(Path(sys.argv[1])))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
