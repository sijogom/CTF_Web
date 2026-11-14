from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import engine
import app.models.event  # noqa: F401 - 플랫폼 이벤트·감사 테이블 등록
import app.models.education  # noqa: F401 - 강의·과제 교육 운영 테이블 등록
import app.models.challenge  # noqa: F401 — Challenge/Hint/Submission 테이블 등록
import app.models.challenge_submission  # noqa: F401 — ChallengeSubmission 테이블 등록
import app.models.knowledge  # noqa: F401 — 지식 태그 테이블 등록
import app.models.lesson_image  # noqa: F401 — 레슨 본문 이미지 lifecycle 테이블 등록
import app.models.challenge_image_cleanup  # noqa: F401 — 문제 이미지 cleanup outbox 등록
import app.models.challenge_attachment  # noqa: F401 - 일반 첨부 원장 등록
from app.redis import init_redis, close_redis
from app.utils.auth_client import close_client, init_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    # JWT 공개키 검증 (RS256 사용 시 필수)
    if settings.jwt_algorithm == "RS256" and not settings.jwt_public_key:
        raise RuntimeError(
            "RS256 알고리즘이 설정되었으나 공개키(JWT_PUBLIC_KEY_FILE)가 비어있습니다. "
            "운영 환경에서는 외부 runtime secret mount를 확인하세요."
        )
    # Runtime worker는 schema DDL을 실행하지 않는다. 깨끗한 Learn DB는 Compose의
    # ``app.scripts.learn_migrate`` job만 bootstrap하고, 기존 versioned DB는 같은
    # job이 Alembic upgrade를 마친 뒤 이 worker를 시작한다. 부분 schema를
    # create_all/ALTER로 우연히 보정하면 손상 상태를 정상으로 오인할 수 있으므로
    # 의도적으로 fail-closed한다.
    # Redis 초기화 (Rate Limit 등)
    await init_redis()
    # 공유 httpx 클라이언트 초기화
    await init_client()
    yield
    # 종료 시: Redis + httpx 클라이언트 + 엔진 정리
    await close_redis()
    await close_client()
    await engine.dispose()


app = FastAPI(
    title="CTF 플랫폼 학습 서비스",
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


from app.api.challenges import router as challenges_router
from app.api.challenge_attachments import router as challenge_attachments_router
from app.api.courses import router as courses_router
from app.api.lessons import router as lessons_router
from app.api.progress import router as progress_router
from app.api.recommend import router as recommend_router
from app.api.internal import router as internal_router
from app.api.submissions import router as submissions_router
from app.api.submissions import flag_logs_router
from app.api.knowledge_tags import router as knowledge_tags_router
from app.api.knowledge_graph import router as knowledge_graph_router
from app.api.knowledge_relations import router as knowledge_relations_router
from app.api.knowledge_contents import router as knowledge_contents_router
from app.api.events import router as events_router
from app.api.education import router as education_router

app.include_router(challenges_router, prefix="")
app.include_router(challenge_attachments_router, prefix="")
app.include_router(courses_router, prefix="")
app.include_router(lessons_router, prefix="")
app.include_router(progress_router, prefix="")
app.include_router(recommend_router, prefix="")
app.include_router(internal_router, prefix="")
app.include_router(submissions_router, prefix="")
app.include_router(flag_logs_router, prefix="")
app.include_router(knowledge_tags_router, prefix="")
app.include_router(knowledge_graph_router, prefix="")
app.include_router(knowledge_relations_router, prefix="")
app.include_router(knowledge_contents_router, prefix="")
app.include_router(events_router, prefix="")
app.include_router(education_router, prefix="")


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "learn"}
