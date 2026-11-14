from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine
from app.redis import close_redis
from app.utils.internal_api import close_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # JWT 키 검증 — RS256 설정인데 유효한 PEM 키가 없으면 즉시 실패
    if settings.jwt_algorithm == "RS256":
        if not settings.jwt_secret_key.startswith("-----BEGIN"):
            raise RuntimeError(
                "RS256 알고리즘이 설정되었으나 JWT_SECRET_KEY_FILE에서 읽은 private key가 유효하지 않습니다. "
                "운영 환경에서는 ctfctl init-jwt-secrets와 외부 key source를 사용하세요."
            )
        if not settings.jwt_public_key.startswith("-----BEGIN"):
            raise RuntimeError(
                "RS256 알고리즘이 설정되었으나 JWT_PUBLIC_KEY_FILE에서 읽은 public key가 유효하지 않습니다. "
                "운영 환경에서는 외부 key source를 확인하세요."
            )

    # 스키마 DDL은 auth-migrate만 수행한다. Compose는 auth-migrate 성공을 시작
    # 조건으로 사용하고, 직접 개발 실행도 먼저 같은 명령을 실행해야 한다. 앱 시작이
    # migration 실패·누락을 우회하여 테이블이나 컬럼을 만들지 않도록 여기에는
    # runtime schema fallback을 두지 않는다.

    # PostgreSQL의 관리자 설정을 Redis 캐시에 복구한다. Redis 장애는 서비스 기동을
    # 막지 않지만, 이후 각 소비 경로는 DB 기준 설정을 다시 읽는다.
    from app.utils.admin_settings import rehydrate_admin_settings_cache
    if not await rehydrate_admin_settings_cache():
        logger.warning("관리자 설정 Redis 캐시 복구 보류: PostgreSQL 기준 읽기를 계속 사용합니다.")

    # 최초 부트스트랩은 명시적 설정이 있을 때만 수행하고 기존 계정을 변경하지 않는다.
    from app.scripts.create_admin import create_admin, is_configured_admin_password
    if is_configured_admin_password(settings.admin_password):
        await create_admin(
            email=settings.admin_email,
            username=settings.admin_username,
            password=settings.admin_password,
            update_existing=False,
        )
    else:
        logger.warning("관리자 계정 부트스트랩 생략: ADMIN_PASSWORD를 명시적으로 설정해야 합니다.")

    yield
    # 종료 시: 엔진 + Redis + httpx 클라이언트 정리
    await engine.dispose()
    await close_redis()
    await close_client()


app = FastAPI(
    title="CTF 플랫폼 인증 서비스",
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

# 미들웨어 실행 순서: Starlette는 추가 역순으로 실행한다.
# 요청 흐름: Rate Limiting → CSRF 검증 → CORS → 라우터
# Rate Limiting을 마지막에 add해야 첫 번째로 실행된다.
from app.middleware.csrf import CSRFMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
app.add_middleware(CSRFMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware, is_production=settings.is_production)


from app.api.register import router as register_router
from app.api.login import router as login_router
from app.api.refresh import router as refresh_router
from app.api.me import router as me_router
from app.api.logout import router as logout_router
from app.api.internal import router as internal_router
from app.api.admin import router as admin_router
from app.api.admin_email import router as admin_email_router
from app.api.verify import router as verify_router
from app.api.password import router as password_router
from app.api.competition import router as competition_router
from app.api.gamification import router as gamification_router
from app.api.teams import router as teams_router
from app.api.metrics import router as metrics_router
from app.api.maintenance import router as maintenance_router
from app.api.rankings import router as rankings_router
from app.api.activities import router as activities_router
from app.api.forward_auth import router as forward_auth_router
from app.api.stats import router as stats_router
from app.api.discord import router as discord_router
from app.api.discord_sso import router as discord_sso_router
from app.api.baekjoon import router as baekjoon_router
from app.api.notification import router as notification_router
from app.api.bans import router as bans_router
from app.api.public_settings import router as public_settings_router
from app.api.internal_notifications import router as internal_notifications_router
import app.models.point_transfer as _point_transfer  # noqa: F401 - settlement ledger registration
import app.models.personal_notification as _personal_notification  # noqa: F401 - recipient notification ledgers

app.include_router(register_router, prefix="")
app.include_router(login_router, prefix="")
app.include_router(refresh_router, prefix="")
app.include_router(me_router, prefix="")
app.include_router(logout_router, prefix="")
app.include_router(internal_router, prefix="/internal")
app.include_router(internal_notifications_router, prefix="/internal")
app.include_router(admin_router, prefix="/admin")
app.include_router(admin_email_router, prefix="/admin")
app.include_router(verify_router, prefix="")
app.include_router(password_router, prefix="")
app.include_router(competition_router, prefix="")
app.include_router(gamification_router, prefix="")
app.include_router(teams_router, prefix="")
app.include_router(metrics_router, prefix="")
app.include_router(maintenance_router, prefix="")
app.include_router(rankings_router, prefix="")
app.include_router(activities_router, prefix="")
app.include_router(forward_auth_router, prefix="")
app.include_router(stats_router, prefix="")
app.include_router(discord_router, prefix="")
app.include_router(discord_sso_router, prefix="")
app.include_router(baekjoon_router, prefix="")
app.include_router(notification_router, prefix="")
app.include_router(bans_router, prefix="")
app.include_router(public_settings_router, prefix="")


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "auth"}
