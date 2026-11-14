import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

_APP_ROOT = str(Path(__file__).resolve().parents[1])
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.models.user import Base
from app.models.audit import AuditLog  # noqa: F401
from app.models.ban import AccountBan, BanAppeal  # noqa: F401
from app.models.competition import (  # noqa: F401
    Competition,
    CompetitionAssignment,
    CompetitionChallenge,
    CompetitionDeletionConfirmation,
    CompetitionParticipant,
    CompetitionSolve,
    CompetitionTeamAssignment,
    CompetitionTeamMember,
    CompetitionTeamRegistration,
    CompetitionTeamSolve,
)
from app.models.inquiry_notification import InquiryNotificationReceipt  # noqa: F401
from app.models.notification import Notification  # noqa: F401
from app.models.team import Team, TeamMember  # noqa: F401
import app.models.point_transfer  # noqa: F401
import app.models.personal_notification  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    # The explicit Auth bootstrap job stamps a verified legacy baseline while
    # its metadata transaction is still open.  Reusing that synchronous
    # connection prevents an unversioned core schema from being committed
    # before the revision marker write succeeds.
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        do_run_migrations(supplied_connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
