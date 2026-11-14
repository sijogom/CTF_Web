from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings

# Long-lived services can retain a PostgreSQL connection after the server or
# network closes it.  Pre-ping discards that stale connection before a request
# uses it; recycle bounds the maximum idle lifetime without exposing details to
# callers as a 500 from the first query.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=1800,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
