"""요청의 서명된 JWT에서 사용자 정보를 추출하는 의존성.

인증 근거는 httpOnly ``access_token`` 쿠키 또는 ``Authorization: Bearer`` JWT뿐이다.
Caddy ``forward_auth``가 편의를 위해 ``X-User-*`` 응답 헤더를 전달할 수 있어도 Learn은
그 헤더를 인증·인가에 사용하지 않는다. 따라서 직접 또는 내부 네트워크 요청에서 원시
식별자 헤더를 위조해도 인증될 수 없다.
"""

import logging
from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status

from app.config import settings
from app.utils.auth_client import get_current_authorization

logger = logging.getLogger(__name__)


class _TokenAbsent:
    """토큰이 아예 없음을 나타내는 센티널(검증 실패와 구분)."""


_NO_TOKEN = _TokenAbsent()


def _user_id_from_payload(payload: dict) -> UUID | None:
    """검증된 JWT payload의 ``sub``를 UUID로 안전하게 해석한다."""
    subject = payload.get("sub")
    if not isinstance(subject, str):
        return None
    try:
        return UUID(subject)
    except (TypeError, ValueError):
        return None


async def _current_authorization(request: Request, user_id: str) -> tuple[bool, str | None]:
    """요청 단위 캐시를 사용해 현재 auth-service 상태를 조회한다."""
    cache = getattr(request.state, "_ctf_authorization_cache", None)
    if cache is None:
        cache = {}
        request.state._ctf_authorization_cache = cache
    if user_id not in cache:
        cache[user_id] = await get_current_authorization(user_id)
    return cache[user_id]


async def _require_current_authorization(request: Request, user_id: str) -> str:
    """현재 auth-service 상태를 확인하고, 실패 또는 비활성 계정을 차단한다."""
    is_active, role = await _current_authorization(request, user_id)
    if not is_active or not role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 비활성화된 사용자입니다.",
        )
    return role


def _decode_bearer_token(request: Request) -> dict | _TokenAbsent | None:
    """access_token 추출: httpOnly 쿠키 우선, Authorization 헤더 대체.

    Returns:
        dict: 유효한 access token payload
        _TokenAbsent: 토큰 자체가 없음
        None: 토큰은 있었으나 검증 실패
    """
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return _NO_TOKEN
        token = auth_header[7:]
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "access":
            return None
        return payload
    except jwt.PyJWTError:
        return None


async def get_optional_user_id(request: Request) -> UUID | None:
    """서명된 JWT가 있을 때만 현재 활성 사용자 ID를 반환한다."""
    result = _decode_bearer_token(request)
    if not isinstance(result, dict):
        return None

    user_id = _user_id_from_payload(result)
    if user_id is None:
        return None
    is_active, _ = await _current_authorization(request, str(user_id))
    return user_id if is_active else None


async def get_current_user_id(request: Request) -> UUID:
    """서명된 JWT와 현재 Auth 상태를 모두 통과한 사용자 ID를 반환한다."""
    result = _decode_bearer_token(request)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 토큰입니다.",
        )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
        )

    user_id = _user_id_from_payload(result)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 사용자 ID입니다.",
        )
    await _require_current_authorization(request, str(user_id))
    return user_id


async def get_current_user_name(request: Request) -> str:
    """서명된 JWT의 사용자명을 반환하고 현재 계정 상태를 재확인한다."""
    result = _decode_bearer_token(request)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 토큰입니다.",
        )
    if not isinstance(result, dict):
        return "unknown"

    user_id = _user_id_from_payload(result)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 사용자 ID입니다.",
        )
    await _require_current_authorization(request, str(user_id))
    username = result.get("username")
    return username if isinstance(username, str) and username else "unknown"


async def get_optional_user_role(request: Request) -> str | None:
    """서명된 JWT가 있을 때만 현재 활성 사용자 역할을 반환한다."""
    result = _decode_bearer_token(request)
    if not isinstance(result, dict):
        return None

    user_id = _user_id_from_payload(result)
    if user_id is None:
        return None
    is_active, role = await _current_authorization(request, str(user_id))
    return role if is_active else None


async def get_current_user_role(request: Request) -> str:
    """서명된 JWT와 현재 Auth 상태를 모두 통과한 사용자 역할을 반환한다."""
    result = _decode_bearer_token(request)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 토큰입니다.",
        )
    if not isinstance(result, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="인증이 필요합니다.",
        )

    user_id = _user_id_from_payload(result)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 사용자 ID입니다.",
        )
    return await _require_current_authorization(request, str(user_id))


def require_role(*allowed_roles: str):
    """역할 기반 접근 제어 의존성 팩토리."""

    async def role_checker(request: Request) -> str:
        result = _decode_bearer_token(request)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않거나 만료된 토큰입니다.",
            )
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="인증이 필요합니다.",
            )

        user_id = _user_id_from_payload(result)
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 사용자 ID입니다.",
            )
        role = await _require_current_authorization(request, str(user_id))

        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="권한이 없습니다.",
            )
        return role

    return role_checker
