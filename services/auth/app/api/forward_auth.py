"""Caddy forward_auth 엔드포인트.

Caddy의 forward_auth 디렉티브가 이 엔드포인트를 호출하여
JWT를 검증하고, 사용자 정보를 응답 헤더로 반환한다.

community-service, learn-service 등 하위 서비스는
Caddy가 주입하는 X-User-ID, X-User-Role, X-User-Name 헤더를 신뢰한다.
"""

from fastapi import APIRouter, Request, Response, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.jwt.token import decode_token
from app.models.user import User
from app.utils.bans import get_active_ban

router = APIRouter(tags=["내부 인증"])


@router.get("/verify-token")
async def verify_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """JWT 검증 후 사용자 정보를 응답 헤더로 반환.

    Caddy forward_auth 모드에서 사용:
    - 유효한 JWT가 있으면: 200 + X-User-ID, X-User-Role, X-User-Name 헤더
    - JWT가 없거나 유효하지 않으면: 200 (헤더 없음)
      → 공개 API는 미인증도 접근 가능해야 하므로 항상 200 반환.
        인증 필수 여부는 하위 서비스의 Depends(get_current_user_id)가 판단.
    """
    # 토큰 추출: httpOnly 쿠키 우선, Authorization 헤더 폴백
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        return Response(status_code=200)

    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return Response(status_code=200)

    user_id = payload.get("sub")
    if not user_id:
        return Response(status_code=200)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or await get_active_ban(db, user.id):
        return Response(status_code=200)

    resp = Response(status_code=200)
    resp.headers["X-User-ID"] = str(user.id)
    resp.headers["X-User-Role"] = user.role or "user"
    # HTTP 헤더는 Latin-1만 허용 — CJK 사용자명은 URL 인코딩
    from urllib.parse import quote
    resp.headers["X-User-Name"] = quote(user.username or "", safe="")
    return resp
