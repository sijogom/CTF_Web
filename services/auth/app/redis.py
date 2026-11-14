import redis.asyncio as aioredis
from app.config import settings

# Redis 비동기 클라이언트 (Refresh Token Rotation, 세션 관리 전용)
redis_client = aioredis.from_url(
    settings.redis_url,
    decode_responses=True,  # bytes → str 자동 디코드
    encoding="utf-8",
)


async def close_redis() -> None:
    """앱 종료 시 Redis 연결 정리 (main.py lifespan에서 호출)"""
    await redis_client.aclose()
