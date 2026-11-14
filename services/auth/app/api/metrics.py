"""기본 메트릭 엔드포인트."""

import time
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis import redis_client

router = APIRouter(tags=["모니터링"])

_start_time = time.time()


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)):
    """서비스 상태 메트릭."""
    uptime = time.time() - _start_time

    # DB 연결 상태
    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    # Redis 연결 상태
    redis_ok = True
    try:
        await redis_client.ping()
    except Exception:
        redis_ok = False

    return {
        "service": "auth",
        "uptime_seconds": round(uptime, 2),
        "database": "connected" if db_ok else "disconnected",
        "redis": "connected" if redis_ok else "disconnected",
    }
