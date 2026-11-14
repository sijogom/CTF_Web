"""요청에서 사용자 정보를 추출하는 의존성.

인증은 서명된 Bearer 토큰 또는 httpOnly 쿠키만 신뢰한다. Caddy가
``X-User-*`` 헤더를 forward-auth 결과로 복사하더라도 Community가 이를
인증 근거로 사용하지 않는다. 서비스 포트가 내부 네트워크에서 직접
접근되거나 SSRF가 발생해도 사용자·역할 스푸핑으로 이어지지 않도록 하는
fail-closed 경계다.
"""

from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status

from app.config import settings
from app.utils.auth_client import get_current_authorization


class _TokenAbsent:
    """토큰이 아예 없음을 나타내는 센티널 (검증 실패와 구분)."""
    pass

_NO_TOKEN = _TokenAbsent()


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
    """현재 auth-service 상태를 확인하고, 실패 시 접근을 차단한다."""
    is_active, role = await _current_authorization(request, user_id)
    if not is_active or not role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 비활성화된 사용자입니다.",
        )
    return role


def _decode_bearer_token(request: Request) -> dict | _TokenAbsent | None:
    """access_token 추출: httpOnly 쿠키 우선, Authorization 헤더 폴백.

    Returns:
        dict: 유효한 토큰 → 페이로드
        _TokenAbsent: 토큰 자체가 없음 → 익명 요청
        None: 토큰이 있었으나 검증 실패 → 401 반환 필수
    """
    # 1) httpOnly 쿠키
    token = request.cookies.get("access_token")
    # 2) Authorization 헤더 폴백
    if not token:
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return _NO_TOKEN  # 토큰 자체가 없음
        token = auth_header[7:]
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[settings.jwt_algorithm],
        )
        if payload.get("type") != "access":
            return None  # 토큰 있지만 타입 불일치
        return payload
    except jwt.PyJWTError:
        return None  # 토큰 있지만 검증 실패


async def get_optional_user_id(
    request: Request,
) -> UUID | None:
    """사용자 ID 추출 (선택적 — 비로그인 허용)."""
    result = _decode_bearer_token(request)

    # 유효한 토큰
    if isinstance(result, dict) and result.get("sub"):
        try:
            user_id = UUID(result["sub"])
        except ValueError:
            return None
        is_active, _ = await _current_authorization(request, str(user_id))
        return user_id if is_active else None

    # 토큰이 있었지만 검증 실패 (선택적이므로 None 반환)
    if result is None:
        return None

    return None


async def get_current_user_id(
    request: Request,
) -> UUID:
    """사용자 ID 추출 (필수 — 미인증 시 401)."""
    result = _decode_bearer_token(request)

    # 유효한 토큰
    if isinstance(result, dict) and result.get("sub"):
        try:
            user_id = UUID(result["sub"])
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 사용자 ID입니다.",
            )
        await _require_current_authorization(request, str(user_id))
        return user_id

    # 토큰이 있었지만 검증 실패 → 즉시 401
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 토큰입니다.",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증이 필요합니다.",
    )


async def get_optional_user_role(
    request: Request,
) -> str | None:
    """사용자 역할 추출 (선택적)."""
    result = _decode_bearer_token(request)

    # 유효한 토큰
    if isinstance(result, dict):
        user_id = result.get("sub")
        if not user_id:
            return None
        is_active, role = await _current_authorization(request, user_id)
        return role if is_active else None

    # 토큰이 있었지만 검증 실패 → 폴백 불허
    if result is None:
        return None

    return None


async def get_current_user_role(
    request: Request,
) -> str:
    """사용자 역할 추출."""
    result = _decode_bearer_token(request)

    # 유효한 토큰
    if isinstance(result, dict):
        user_id = result.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 사용자 ID입니다.",
            )
        return await _require_current_authorization(request, user_id)

    # 토큰이 있었지만 검증 실패 → 즉시 401 (폴백 불허)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 토큰입니다.",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증이 필요합니다.",
    )


async def get_current_username(
    request: Request,
) -> str:
    """사용자명 추출."""
    result = _decode_bearer_token(request)

    # 유효한 토큰
    if isinstance(result, dict) and result.get("username"):
        user_id = result.get("sub")
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 사용자 ID입니다.",
            )
        await _require_current_authorization(request, user_id)
        return result["username"]

    # 토큰이 있었지만 검증 실패 → 즉시 401 (폴백 불허)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않거나 만료된 토큰입니다.",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증이 필요합니다.",
    )


def require_role(*allowed_roles: str):
    """역할 기반 접근 제어 의존성 팩토리."""
    async def role_checker(
        request: Request,
    ) -> str:
        result = _decode_bearer_token(request)

        # 유효한 토큰
        if isinstance(result, dict):
            user_id = result.get("sub")
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="유효하지 않은 사용자 ID입니다.",
                )
            role = await _require_current_authorization(request, user_id)
        elif result is None:
            # 토큰이 있었지만 검증 실패 → 즉시 401
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않거나 만료된 토큰입니다.",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="인증이 필요합니다.",
            )

        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="권한이 없습니다.",
            )
        return role
    return role_checker
