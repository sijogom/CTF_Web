"""Learn 서비스용 비동기 Alembic 환경.

운영 DB의 기준 URL은 ``app.config.settings.database_url``이다. 이 파일은 비밀값을
출력하지 않으며, 실제 upgrade는 배포 전 단일 migration job에서만 수행한다.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.models.course import Base
import app.models.event  # noqa: F401 - 플랫폼 이벤트·감사 테이블 등록
import app.models.education  # noqa: F401 - 강의·과제 교육 운영 테이블 등록
import app.models.challenge  # noqa: F401 - Base.metadata 등록
import app.models.challenge_attachment  # noqa: F401 - 일반 첨부 원장 등록
import app.models.challenge_submission  # noqa: F401 - Base.metadata 등록
import app.models.knowledge  # noqa: F401 - Base.metadata 등록
import app.models.lesson_image  # noqa: F401 - 레슨 본문 이미지 lifecycle 테이블 등록
import app.models.challenge_image_cleanup  # noqa: F401 - 문제 이미지 cleanup outbox 등록


config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL 검토가 필요할 때만 사용한다."""
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
    # The pristine Learn bootstrap job owns a single transaction containing
    # create_all -> schema contract verification -> Alembic stamp.  Reusing
    # its explicitly supplied sync connection avoids committing unversioned
    # application tables before the revision marker is written.
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        do_run_migrations(supplied_connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
