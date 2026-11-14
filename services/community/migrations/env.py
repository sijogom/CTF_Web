"""Community 서비스용 비동기 Alembic 환경.

운영 DB URL은 앱과 동일하게 ``app.config.settings.database_url``에서만 읽는다.
이 파일은 URL이나 비밀값을 출력하지 않으며, schema 변경은 Compose의 일회성 migration
job에서 앱 서비스보다 먼저 실행된다.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.models.post import Base
import app.models.inquiry  # noqa: F401 - Base.metadata 등록
import app.models.marketplace  # noqa: F401 - Base.metadata 등록
import app.models.moderation  # noqa: F401 - Base.metadata 등록
import app.models.marketplace_settlement  # noqa: F401
import app.models.notification_event  # noqa: F401


config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
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
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
