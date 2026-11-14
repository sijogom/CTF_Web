"""크로스 DB 유효성 검증 — auth-service API 호출.

httpx.AsyncClient를 모듈 레벨에서 공유하여 커넥션 풀을 재사용한다.
lifespan에서 init_client() / close_client()를 호출해야 한다.
"""

from dataclasses import dataclass

import httpx

from app.config import settings

# 모듈 레벨 공유 클라이언트
_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class InquiryContact:
    email: str
    username: str


def get_client() -> httpx.AsyncClient:
    """공유 httpx 클라이언트를 반환한다. 초기화 전이면 에러를 발생시킨다."""
    if _client is None:
        raise RuntimeError("httpx 클라이언트가 초기화되지 않았습니다. lifespan에서 init_client()를 호출하세요.")
    return _client


async def init_client() -> None:
    """공유 httpx 클라이언트를 생성한다. INTERNAL_API_KEY가 설정되면 기본 헤더에 포함."""
    global _client  # noqa: PLW0603
    headers = {}
    if settings.internal_api_key:
        headers["X-Internal-Key"] = settings.internal_api_key
    _client = httpx.AsyncClient(timeout=5.0, headers=headers)


async def close_client() -> None:
    """공유 httpx 클라이언트를 닫는다."""
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.aclose()
        _client = None


async def verify_user_exists(user_id: str) -> bool:
    """auth-service에 사용자 존재 여부를 확인한다."""
    client = get_client()
    try:
        resp = await client.get(
            f"{settings.auth_service_url}/internal/users/{user_id}/exists",
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("exists", False)
        return False
    except httpx.RequestError:
        return False


async def get_current_authorization(user_id: str) -> tuple[bool, str | None]:
    """auth-service의 현재 계정 상태와 역할을 반환한다.

    JWT 서명은 신원 확인에만 사용한다. 비활성화와 역할 변경은 발급 후에도
    즉시 반영되어야 하므로 권한 판단 전에 auth-service 기준값을 확인한다.
    통신 실패는 fail-closed로 처리한다.
    """
    client = get_client()
    try:
        resp = await client.get(
            f"{settings.auth_service_url}/internal/users/{user_id}/authorization",
        )
        if resp.status_code != 200:
            return False, None
        data = resp.json()
        if not data.get("exists") or not data.get("is_active"):
            return False, None
        role = data.get("role")
        return True, role if isinstance(role, str) else None
    except httpx.RequestError:
        return False, None


async def get_inquiry_contact(user_id: str) -> InquiryContact | None:
    """Return only an active, verified member contact for a Community inquiry."""
    client = get_client()
    try:
        response = await client.get(
            f"{settings.auth_service_url}/internal/users/{user_id}/inquiry-contact",
        )
        if response.status_code != 200:
            return None
        data = response.json()
        email = data.get("email")
        username = data.get("username")
        if not isinstance(email, str) or not isinstance(username, str):
            return None
        return InquiryContact(email=email, username=username)
    except httpx.RequestError:
        return None
