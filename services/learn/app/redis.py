import logging

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

# Redis 비동기 클라이언트 (제출 Rate Limit 등 공유 상태 관리)
# redis_url이 비어있거나 연결 실패 시 None — 기능이 graceful하게 비활성화된다.
redis_client: aioredis.Redis | None = None


async def init_redis() -> None:
    """앱 시작 시 Redis 연결 초기화 (main.py lifespan에서 호출)."""
    global redis_client
    if not settings.redis_url:
        logger.warning("REDIS_URL이 설정되지 않았습니다. Redis 기반 기능이 비활성화됩니다.")
        return
    try:
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            encoding="utf-8",
        )
        await client.ping()
        redis_client = client
        logger.info("Redis 연결 성공")
    except Exception as e:
        logger.warning("Redis 연결 실패 — Rate Limit 등이 비활성화됩니다: %s", e)
        redis_client = None


async def close_redis() -> None:
    """앱 종료 시 Redis 연결 정리 (main.py lifespan에서 호출)."""
    global redis_client
    if redis_client:
        await redis_client.aclose()
        redis_client = None
