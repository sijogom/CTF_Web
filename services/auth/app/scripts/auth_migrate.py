"""Run the supported Auth migration/bootstrap path.

Auth has a pre-Alembic core.  Empty databases receive only that historical
core before baseline stamping; complete unversioned cores are validated and
stamped without ``create_all``.  Both paths then use the normal forward
Alembic chain.  Versioned databases never receive runtime bootstrap DDL.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, text

from app.database import engine
from app.models.user import Base
import app.models.audit  # noqa: F401 - register all Auth tables in Base.metadata
import app.models.ban  # noqa: F401 - register all Auth tables in Base.metadata
import app.models.competition  # noqa: F401 - register all Auth tables in Base.metadata
import app.models.inquiry_notification  # noqa: F401 - register all Auth tables in Base.metadata
import app.models.notification  # noqa: F401 - register all Auth tables in Base.metadata
import app.models.team  # noqa: F401 - register all Auth tables in Base.metadata
import app.models.point_transfer  # noqa: F401 - register settlement ledger
import app.models.personal_notification  # noqa: F401 - register notification ledgers
from app.utils.auth_schema_bootstrap_contract import (
    ALEMBIC_VERSION_TABLE,
    LEGACY_CORE_TABLES,
    AuthSchemaPreparation,
    classify_auth_schema_tables,
    validate_current_auth_schema,
    validate_legacy_core_schema,
)


logger = logging.getLogger(__name__)
SERVICE_ROOT = Path(__file__).resolve().parents[2]
LEGACY_BASELINE_REVISION = "20260813_000"


def _metadata_table_names() -> frozenset[str]:
    table_names = frozenset(Base.metadata.tables)
    missing_core = LEGACY_CORE_TABLES - table_names
    if missing_core:
        raise RuntimeError(
            "Auth migration bootstrap 모델 등록이 불완전합니다: "
            f"{', '.join(sorted(missing_core))}."
        )
    return table_names


def _legacy_core_metadata_tables() -> tuple:
    """Return only the historical core needed for a truly empty database."""

    _metadata_table_names()
    return tuple(
        Base.metadata.tables[table_name] for table_name in sorted(LEGACY_CORE_TABLES)
    )


def _table_columns(connection) -> dict[str, frozenset[str]]:
    inspector = inspect(connection)
    return {
        table_name: frozenset(
            column["name"] for column in inspector.get_columns(table_name)
        )
        for table_name in inspector.get_table_names()
    }


def _verify_legacy_core_contract(connection) -> None:
    """Validate revision-000 requirements before writing its marker."""

    validate_legacy_core_schema(_table_columns(connection))


def _verify_current_schema_contract(connection) -> None:
    """Fail closed after ``upgrade head`` when required runtime schema is absent."""

    _verify_version_marker(connection)
    validate_current_auth_schema(_table_columns(connection))


def _verify_version_marker(connection) -> None:
    inspector = inspect(connection)
    if ALEMBIC_VERSION_TABLE not in inspector.get_table_names():
        raise RuntimeError("Auth Alembic version marker 테이블이 없습니다.")
    columns = {
        column["name"] for column in inspector.get_columns(ALEMBIC_VERSION_TABLE)
    }
    if "version_num" not in columns:
        raise RuntimeError("Auth Alembic version marker에 version_num 컬럼이 없습니다.")
    versions = list(
        connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
    )
    if len(versions) != 1 or not isinstance(versions[0], str) or not versions[0].strip():
        raise RuntimeError(
            "Auth Alembic version marker가 비어 있거나 여러 revision을 포함합니다. "
            "자동 upgrade를 중단합니다."
        )


def _stamp_legacy_baseline(connection) -> None:
    """Write the explicit baseline only after the revision-000 contract passes."""

    from alembic import command
    from alembic.config import Config

    config = Config(str(SERVICE_ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    command.stamp(config, LEGACY_BASELINE_REVISION)


async def prepare_auth_schema() -> AuthSchemaPreparation:
    """Prepare only pristine or deliberately classified legacy databases.

    A complete legacy database must not be checked against current metadata
    here.  It is checked only against the revision-000 core contract, stamped,
    then upgraded through revisions 001--003.
    """

    application_table_names = _metadata_table_names()
    async with engine.begin() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )
        preparation = classify_auth_schema_tables(
            table_names=table_names,
            application_table_names=application_table_names,
        )
        if preparation is AuthSchemaPreparation.BOOTSTRAP_EMPTY:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=_legacy_core_metadata_tables(),
            )

        if preparation in {
            AuthSchemaPreparation.BOOTSTRAP_EMPTY,
            AuthSchemaPreparation.ADOPT_COMPLETE_LEGACY,
        }:
            await connection.run_sync(_verify_legacy_core_contract)
            await connection.run_sync(_stamp_legacy_baseline)
        else:
            await connection.run_sync(_verify_version_marker)
    return preparation


def _run_alembic_upgrade() -> None:
    command = ["alembic", "-c", str(SERVICE_ROOT / "alembic.ini"), "upgrade", "head"]
    # Fixed arguments only. DATABASE_URL remains in the process environment and
    # is never inserted into a command or log message.
    subprocess.run(command, cwd=SERVICE_ROOT, check=True)


async def verify_auth_schema_post_upgrade() -> None:
    """Check the stable current contract after Alembic reaches head."""

    async with engine.connect() as connection:
        await connection.run_sync(_verify_current_schema_contract)


async def run_auth_migration_flow() -> AuthSchemaPreparation:
    """Keep the shared async engine on one event loop for the whole job."""

    try:
        preparation = await prepare_auth_schema()
        _run_alembic_upgrade()
        await verify_auth_schema_post_upgrade()
        return preparation
    finally:
        await engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        preparation = asyncio.run(run_auth_migration_flow())
        logger.info("Auth schema preparation mode: %s", preparation.value)
    except Exception:
        logger.exception("Auth schema migration/bootstrap failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
