"""Run the only supported Compose migration/bootstrap path for Learn.

Fresh databases are initialized explicitly here, never as a side effect of an
application worker.  Existing databases take the normal Alembic upgrade path;
partial core or education schemas fail before any DDL is attempted.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Collection
from pathlib import Path

from sqlalchemy import CheckConstraint, DateTime, UniqueConstraint, inspect, text

from app.database import engine
from app.models.course import Base
import app.models.challenge  # noqa: F401 - register all Learn tables in Base.metadata
import app.models.challenge_attachment  # noqa: F401 - register attachment ledger
import app.models.challenge_submission  # noqa: F401 - register all Learn tables in Base.metadata
import app.models.education  # noqa: F401 - register all Learn tables in Base.metadata
import app.models.event  # noqa: F401 - register all Learn tables in Base.metadata
import app.models.knowledge  # noqa: F401 - register all Learn tables in Base.metadata
import app.models.lesson_image  # noqa: F401 - register all Learn tables in Base.metadata
import app.models.challenge_image_cleanup  # noqa: F401 - register all Learn tables in Base.metadata
from app.utils.learn_schema_bootstrap_contract import (
    ALEMBIC_VERSION_TABLE,
    EDUCATION_TABLES,
    LearnSchemaPreparation,
    classify_learn_schema_tables,
)


logger = logging.getLogger(__name__)
SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _metadata_table_names() -> frozenset[str]:
    return frozenset(Base.metadata.tables)


def _normalized_sql(value: object) -> str:
    return " ".join(str(value).lower().split())


def _types_compatible(expected: object, actual: object, *, dialect) -> bool:
    """Compare SQLAlchemy types without losing PostgreSQL timezone metadata."""
    if isinstance(expected, DateTime) and isinstance(actual, DateTime):
        return bool(getattr(expected, "timezone", False)) == bool(
            getattr(actual, "timezone", False)
        )
    return _normalized_sql(expected.compile(dialect=dialect)) == _normalized_sql(actual)


def _metadata_schema_contract_missing(
    connection,
    *,
    table_names: Collection[str] | None = None,
    require_exact_table_set: bool = False,
    check_server_defaults: bool = False,
) -> list[str]:
    """Compare database objects with the current full metadata after bootstrap.

    This verification occurs only immediately after ``create_all`` on a known
    empty database.  It is intentionally stricter than a table-name check, so
    a failed/limited DDL run cannot be stamped as Alembic head.
    """
    inspector = inspect(connection)
    missing: list[str] = []
    expected_table_names = set(Base.metadata.tables)
    selected_table_names = expected_table_names if table_names is None else set(table_names)
    actual_table_names = set(inspector.get_table_names())
    if require_exact_table_set:
        unexpected = actual_table_names - expected_table_names
        if unexpected:
            missing.append(f"unexpected_tables:{','.join(sorted(unexpected))}")
        absent = expected_table_names - actual_table_names
        if absent:
            missing.append(f"missing_tables:{','.join(sorted(absent))}")

    for table_name in sorted(selected_table_names):
        if table_name not in actual_table_names:
            missing.append(f"{table_name}.table")
            continue
        table = Base.metadata.tables[table_name]
        actual_column_info = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        actual_columns = set(actual_column_info)
        for column in sorted(set(table.columns.keys()) - actual_columns):
            missing.append(f"{table_name}.column:{column}")
        for column in table.columns:
            actual = actual_column_info.get(column.name)
            if actual is None:
                continue
            if bool(actual.get("nullable")) != bool(column.nullable):
                missing.append(f"{table_name}.nullable:{column.name}")
            if not _types_compatible(
                column.type,
                actual.get("type"),
                dialect=connection.dialect,
            ):
                missing.append(f"{table_name}.type:{column.name}")
            if (
                check_server_defaults
                and not column.primary_key
                and not column.autoincrement
                and bool(actual.get("default")) != bool(column.server_default)
            ):
                missing.append(f"{table_name}.server_default:{column.name}")

        actual_primary_key = set(
            (inspector.get_pk_constraint(table_name) or {}).get("constrained_columns") or []
        )
        expected_primary_key = set(table.primary_key.columns.keys())
        if actual_primary_key != expected_primary_key:
            missing.append(f"{table_name}.primary_key")

        expected_indexes = {index.name for index in table.indexes if index.name}
        actual_indexes = {
            index["name"] for index in inspector.get_indexes(table_name) if index.get("name")
        }
        for index_name in sorted(expected_indexes - actual_indexes):
            missing.append(f"{table_name}.index:{index_name}")

        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint) and constraint.name
        }
        actual_checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
            if constraint.get("name")
        }
        for check_name in sorted(expected_checks - actual_checks):
            missing.append(f"{table_name}.check:{check_name}")

        expected_unique = {
            frozenset(column.name for column in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        expected_unique.update(
            frozenset({column.name}) for column in table.columns if column.unique
        )
        actual_unique = {
            frozenset(constraint.get("column_names") or [])
            for constraint in inspector.get_unique_constraints(table_name)
            if constraint.get("column_names")
        }
        actual_unique.update(
            frozenset(index.get("column_names") or [])
            for index in inspector.get_indexes(table_name)
            if index.get("unique") and index.get("column_names")
        )
        for columns in expected_unique - actual_unique:
            missing.append(f"{table_name}.unique:{','.join(sorted(columns))}")

        expected_foreign_keys = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.elements[0].column.table.name,
                (constraint.elements[0].ondelete or "").lower(),
            )
            for constraint in table.foreign_key_constraints
        }
        actual_foreign_keys = {
            (
                tuple(foreign_key.get("constrained_columns") or []),
                foreign_key.get("referred_table"),
                str((foreign_key.get("options") or {}).get("ondelete") or "").lower(),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
            if foreign_key.get("referred_table")
        }
        for columns, referred_table, ondelete in expected_foreign_keys - actual_foreign_keys:
            missing.append(
                f"{table_name}.foreign_key:{','.join(columns)}->{referred_table}:{ondelete or 'default'}"
            )
    return missing


def _verify_bootstrapped_metadata(connection) -> None:
    missing = _metadata_schema_contract_missing(
        connection,
        require_exact_table_set=True,
        check_server_defaults=True,
    )
    if not missing:
        return
    raise RuntimeError(
        "빈 Learn DB bootstrap 후 metadata schema 계약이 불완전합니다: "
        f"{', '.join(missing[:12])}. Alembic revision을 stamp하지 않습니다."
    )


def _verify_version_marker(connection) -> None:
    """Reject an empty/malformed marker before an existing DB reaches Alembic.

    A bare ``alembic_version`` table is not proof that an interrupted legacy
    schema is safe to upgrade.  Alembic validates the concrete revision later;
    this preflight only establishes that a single non-empty marker exists.
    """
    inspector = inspect(connection)
    columns = {column["name"] for column in inspector.get_columns(ALEMBIC_VERSION_TABLE)}
    if "version_num" not in columns:
        raise RuntimeError("Learn Alembic version marker에 version_num column이 없습니다.")
    rows = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if len(rows) != 1 or not isinstance(rows[0], str) or not rows[0].strip():
        raise RuntimeError(
            "Learn Alembic version marker가 비어 있거나 여러 revision을 포함합니다. "
            "자동 upgrade를 중단합니다."
        )


def _verify_existing_education_table_group(
    connection,
    table_names: Collection[str],
) -> None:
    """Reject only a partial education table group before Alembic upgrade.

    A future additive revision may legitimately add education columns or tables
    that are present in current ORM metadata but absent at the database's
    current revision.  Full metadata comparison therefore belongs after the
    upgrade, not in this pre-upgrade safety gate.
    """
    del connection
    present = set(table_names).intersection(EDUCATION_TABLES)
    if not present or present == set(EDUCATION_TABLES):
        return
    raise RuntimeError(
        "기존 교육 운영 table group이 부분 생성 상태입니다. Alembic upgrade를 시작하지 않습니다: "
        f"{', '.join(sorted(present))}."
    )


def _verify_upgraded_metadata(connection) -> None:
    """Verify the current full ORM contract only after Alembic reaches head."""
    missing = _metadata_schema_contract_missing(connection)
    if not missing:
        return
    raise RuntimeError(
        "Learn Alembic upgrade 후 metadata schema 계약이 불완전합니다: "
        f"{', '.join(missing[:12])}."
    )


async def verify_upgraded_schema() -> None:
    """Open a fresh connection after Alembic and verify the resulting schema."""
    async with engine.connect() as connection:
        await connection.run_sync(_verify_version_marker)
        await connection.run_sync(_verify_upgraded_metadata)


async def dispose_engine() -> None:
    """Release asyncpg connections before another asyncio.run() loop starts."""
    await engine.dispose()


async def prepare_and_dispose() -> LearnSchemaPreparation:
    """Prepare the schema and dispose its loop-bound pool before returning."""
    try:
        return await prepare_learn_schema()
    finally:
        await dispose_engine()


async def verify_and_dispose() -> None:
    """Verify the schema and always release its loop-bound pool."""
    try:
        await verify_upgraded_schema()
    finally:
        await dispose_engine()


def _stamp_bootstrapped_head(connection) -> None:
    """Stamp the verified pristine schema in the same transaction as create_all.

    Using a subprocess here would commit schema DDL first and could leave a
    non-empty, unversioned DB if the later stamp failed.  ``migrations.env``
    accepts this explicit synchronous connection so both operations commit or
    roll back together.
    """
    _stamp_revision(connection, "head")


def _install_bootstrapped_database_hooks(connection) -> None:
    """Install non-metadata DDL before the pristine schema is stamped."""
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    configuration = Config(str(SERVICE_ROOT / "alembic.ini"))
    configuration.set_main_option("script_location", str(SERVICE_ROOT / "migrations"))
    script = ScriptDirectory.from_config(configuration).get_revision("20261013_028")
    if script is None:
        raise RuntimeError("Graph revision migration is missing; bootstrap is not stamped.")
    with Operations.context(MigrationContext.configure(connection)):
        script.module.upgrade()


def _stamp_revision(connection, revision: str) -> None:
    """Stamp a caller-approved revision using the current transaction."""
    from alembic import command
    from alembic.config import Config

    alembic_config = Config(str(SERVICE_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(SERVICE_ROOT / "migrations"))
    alembic_config.attributes["connection"] = connection
    command.stamp(alembic_config, revision)


async def prepare_learn_schema() -> LearnSchemaPreparation:
    """Perform no DDL unless the DB is provably completely empty."""
    expected_tables = _metadata_table_names()
    async with engine.begin() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        preparation = classify_learn_schema_tables(
            table_names=table_names,
            application_table_names=expected_tables,
            allow_legacy_adoption=os.getenv("LEARN_SCHEMA_ADOPT_LEGACY") == "1",
        )
        if preparation is LearnSchemaPreparation.BOOTSTRAP_EMPTY:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(_install_bootstrapped_database_hooks)
            await connection.run_sync(_verify_bootstrapped_metadata)
            await connection.run_sync(_stamp_bootstrapped_head)
        elif preparation is LearnSchemaPreparation.ADOPT_LEGACY:
            # The exact legacy table set was checked above.  Only the explicit
            # operator-controlled adoption path may create the marker; normal
            # startup remains fail-closed for unversioned databases.
            await connection.run_sync(_stamp_revision, "20260813_000")
        else:
            await connection.run_sync(_verify_version_marker)
            await connection.run_sync(_verify_existing_education_table_group, table_names)
        return preparation


def _run_alembic(preparation: LearnSchemaPreparation) -> None:
    if preparation is LearnSchemaPreparation.BOOTSTRAP_EMPTY:
        return
    command = ["alembic", "-c", str(SERVICE_ROOT / "alembic.ini"), "upgrade", "head"]
    # Fixed arguments only: DATABASE_URL remains in the process environment and
    # is never interpolated into a command or log message.
    subprocess.run(command, cwd=SERVICE_ROOT, check=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        preparation = asyncio.run(prepare_and_dispose())
        logger.info("Learn schema preparation mode: %s", preparation.value)
        _run_alembic(preparation)
        asyncio.run(verify_and_dispose())
    except Exception:
        logger.exception("Learn schema migration/bootstrap failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
