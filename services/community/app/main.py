from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.utils.auth_client import close_client, init_client

    # RS256 배포에서는 검증 공개키가 없으면 서비스를 시작하지 않는다.
    if settings.jwt_algorithm == "RS256" and not settings.jwt_public_key:
        raise RuntimeError(
            "RS256 알고리즘이 설정되었지만 공개키(JWT_PUBLIC_KEY_FILE)가 비어 있습니다. "
            "운영 환경에서는 외부 runtime secret mount를 확인하세요."
        )

    # 스키마 변경은 community-migrate Alembic 작업만 수행한다.
    # 서비스 시작 중 create_all/ALTER를 실행하면 부분 적용과 롤백 불가능 상태를 만들 수 있다.
    await init_client()
    try:
        yield
    finally:
        await close_client()
        await engine.dispose()


app = FastAPI(
    title="CTF 플랫폼 커뮤니티 서비스",
    version="0.1.0",
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)

from app.middleware.security_headers import SecurityHeadersMiddleware

app.add_middleware(SecurityHeadersMiddleware, is_production=settings.is_production)

from app.api.inquiries import router as inquiries_router
from app.api.internal import router as internal_router
from app.api.marketplace import router as marketplace_router
from app.api.moderation import router as moderation_router
from app.api.posts import router as posts_router
from app.api.reports import router as reports_router
import app.models.marketplace_settlement as _marketplace_settlement  # noqa: F401 - purchase operation/outbox registration
import app.models.notification_event as _notification_event  # noqa: F401 - comment notification outbox

app.include_router(posts_router, prefix="")
app.include_router(reports_router, prefix="")
app.include_router(marketplace_router, prefix="")
app.include_router(internal_router, prefix="")
app.include_router(inquiries_router, prefix="")
app.include_router(moderation_router, prefix="")


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "community"}
