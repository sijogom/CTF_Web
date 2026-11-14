"""Inspect, or reset only an empty task-owned renewal database."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    if url.host not in {"127.0.0.1", "localhost"} or url.port != 15433 or url.database != "ctf_acceptance_learn":
        raise RuntimeError("OWNED_RENEWAL_DATABASE_REQUIRED")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            version = await connection.scalar(text("SHOW server_version"))
            if not str(version).startswith("16."):
                raise RuntimeError("POSTGRESQL_16_REQUIRED")
            tables = (await connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
            if "--reset-empty-owned" in sys.argv:
                for table in tables:
                    if table in {"alembic_version", "knowledge_graph_revision"}:
                        continue
                    # Identifiers came from the task database catalog, not request input.
                    name = connection.dialect.identifier_preparer.quote(table)
                    if await connection.scalar(text(f"SELECT count(*) FROM {name}")):
                        raise RuntimeError("OWNED_DATABASE_HAS_APPLICATION_DATA")
                await connection.execute(text("DROP SCHEMA public CASCADE"))
                await connection.execute(text("CREATE SCHEMA public AUTHORIZATION CURRENT_USER"))
                result = {"server_version": version, "reset_empty_owned": True, "database": url.database}
            else:
                columns = (await connection.execute(text("SELECT column_name,data_type,is_identity FROM information_schema.columns WHERE table_schema='public' AND table_name='knowledge_graph_revision' ORDER BY ordinal_position"))).mappings().all()
                triggers = await connection.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname='knowledge_graph_revision_write' AND NOT tgisinternal"))
                head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                result = {"server_version": version, "head": head, "tables": len(tables), "revision_columns": [dict(row) for row in columns], "revision_triggers": triggers}
        print(json.dumps(result))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
