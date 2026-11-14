"""내부 마이크로서비스 API 호출 헬퍼."""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# 모듈 레벨 지속 클라이언트 (커넥션 풀 재사용)
_client: httpx.AsyncClient | None = None


def internal_headers() -> dict[str, str]:
    """내부 API 호출용 헤더를 반환한다."""
    headers: dict[str, str] = {}
    if settings.internal_api_key:
        headers["X-Internal-Key"] = settings.internal_api_key
    return headers


def _get_client() -> httpx.AsyncClient:
    """지속 httpx 클라이언트를 반환한다. 닫힌 경우 재생성."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


async def close_client() -> None:
    """앱 종료 시 클라이언트를 정리한다."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def service_get(base_url: str, path: str) -> dict | list | None:
    """내부 서비스 GET 호출. 실패 시 None 반환."""
    try:
        client = _get_client()
        resp = await client.get(f"{base_url}{path}", headers=internal_headers())
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning("서비스 호출 실패: %s%s — %s", base_url, path, e)
    return None
