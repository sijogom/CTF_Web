"""Redis 기반 Rate Limiting 미들웨어.

Redis 장애 시에도 메모리 기반 간이 카운터로 기본 제한을 적용한다.
"""

import logging
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.redis import redis_client

logger = logging.getLogger(__name__)

# 경로별 Rate Limit 규칙 (요청 수, 시간 윈도우 초)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/login": (5, 60),              # 로그인: 분당 5회
    "/register": (3, 60),           # 회원가입: 분당 3회
    "/forgot-password": (3, 300),   # 비밀번호 재설정 요청: 5분에 3회
    "/reset-password": (3, 300),    # 비밀번호 재설정 실행: 5분에 3회
    "/send-verification": (3, 300), # 인증 메일 재발송: 5분에 3회
    "/admin/email/send": (5, 600),  # 관리자 이메일 발송: 10분에 5회
}

# 민감한 GET 경로별 Rate Limit (토큰 브루트포스 방지)
GET_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/discord/sso/start": (10, 60),
    "/discord/sso/callback": (10, 60),
    "/verify-email": (10, 60),        # 이메일 인증: 분당 10회
    "/email-unsubscribe": (10, 60),   # 수신 거부: 분당 10회
}

# 글로벌 기본 제한 (분당 60회)
DEFAULT_LIMIT = (60, 60)

# 메모리 기반 간이 카운터 (Redis 장애 시 폴백)
# 구조: {key: (count, window_start_timestamp)}
_memory_counters: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
# 메모리 카운터 최대 항목 수 (메모리 사용량 제한)
_MEMORY_COUNTER_MAX_ENTRIES = 10000
# 메모리 폴백 호출 횟수 (주기적 정리용)
_memory_fallback_count = 0


def _memory_rate_check(key: str, max_requests: int, window: int) -> bool:
    """메모리 기반 간이 rate limit 체크. 초과 시 True 반환."""
    now = time.monotonic()
    count, window_start = _memory_counters[key]

    # 윈도우가 만료되었으면 리셋
    if now - window_start >= window:
        _memory_counters[key] = (1, now)
        return False

    if count >= max_requests:
        return True

    _memory_counters[key] = (count + 1, window_start)
    return False


def _cleanup_memory_counters() -> None:
    """만료된 항목 정리 (항목 수가 임계치를 넘으면 실행)."""
    if len(_memory_counters) <= _MEMORY_COUNTER_MAX_ENTRIES:
        return
    now = time.monotonic()
    expired_keys = [
        k for k, (_, start) in _memory_counters.items()
        if now - start >= 600  # 10분 이상 된 항목 정리
    ]
    for k in expired_keys:
        del _memory_counters[k]


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path

        # GET/HEAD/OPTIONS: 민감 경로만 제한, 나머지는 통과
        if request.method in ("GET", "HEAD", "OPTIONS"):
            if path not in GET_RATE_LIMITS:
                return await call_next(request)
            max_requests, window = GET_RATE_LIMITS[path]
        else:
            # POST/PATCH/DELETE 등: 경로별 규칙 또는 기본 제한
            max_requests, window = DEFAULT_LIMIT
            if path in RATE_LIMITS:
                max_requests, window = RATE_LIMITS[path]

        key = f"rate_limit:{client_ip}:{path}"

        if redis_client is None:
            if _memory_rate_check(key, max_requests, window):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."},
                    headers={"Retry-After": str(window)},
                )
            return await call_next(request)

        try:
            current = await redis_client.get(key)
            if current and int(current) >= max_requests:
                ttl = await redis_client.ttl(key)
                retry_after = max(ttl, 1)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."},
                    headers={"Retry-After": str(retry_after)},
                )

            pipe = redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window, nx=True)  # 첫 요청 시점 기준 고정 윈도우
            await pipe.execute()
        except Exception as exc:
            global _memory_fallback_count
            logger.warning("Redis rate-limit 오류 (ip=%s, path=%s): %s", client_ip, path, exc)

            # 메모리 기반 간이 카운터로 폴백 (보안/일반 경로 모두 동일 제한)
            if _memory_rate_check(key, max_requests, window):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해주세요."},
                    headers={"Retry-After": str(window)},
                )

            # 주기적으로 만료된 항목 정리 (임계치 초과 또는 100회마다)
            _memory_fallback_count += 1
            if _memory_fallback_count % 100 == 0:
                _cleanup_memory_counters()

        return await call_next(request)
