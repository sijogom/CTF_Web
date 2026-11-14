"""내부 서비스용 API — auth-service에서 프로필 데이터 조회 시 사용."""

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, func, text as sa_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models.challenge import (
    Challenge,
    ChallengeEnvironmentVerification,
    ChallengeFlagPolicy,
    DynamicFlagIssuance,
    Submission,
)
from app.models.challenge_image_cleanup import ChallengeImageCleanupJob
from app.utils.auth_client import (
    get_current_authorization,
    verify_competition_challenge_access,
)
from app.utils.challenge_access import user_can_access_challenge_for_instance
from app.utils.dynamic_flags import (
    DYNAMIC_FLAG_TYPE,
    DynamicFlagConfigurationError,
    build_dynamic_flag,
    dynamic_token_hmac,
    generate_dynamic_locator,
    generate_dynamic_token,
    parse_dynamic_flag_keyring,
    resolve_dynamic_hmac_key,
)
from app.utils.minio_client import (
    BuildContextStorageUnavailableError,
    get_build_file_urls,
)
from app.utils.environment_verification import (
    environment_is_verified_for_runtime,
    environment_status,
    normalize_environment_failure_code,
)

logger = logging.getLogger(__name__)


def _environment_status_for_runtime(
    env_type: str | None,
    environment_revision: int | None,
    environment_verified_revision: int | None,
    environment_verified_image_id: str | None,
) -> str:
    """Return a UI-safe status that cannot claim readiness without an image ID."""
    current_status = environment_status(
        env_type,
        environment_revision,
        environment_verified_revision,
    )
    if current_status == "ready" and not environment_is_verified_for_runtime(
        env_type,
        environment_revision,
        environment_verified_revision,
        environment_verified_image_id,
    ):
        return "verification_required"
    return current_status


def verify_internal_key(request: Request) -> None:
    """X-Internal-Key 헤더를 검증한다. 키가 설정되어 있으면 반드시 일치해야 한다."""
    if not settings.internal_api_key:
        if settings.is_production:
            raise HTTPException(status_code=503, detail="내부 API 키 미설정")
        logger.warning(
            "INTERNAL_API_KEY 미설정 — 개발 환경에서 내부 API 인증을 건너뜁니다. (path=%s)",
            request.url.path,
        )
        return  # 개발 환경에서 키 미설정 시 통과
    key = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(key, settings.internal_api_key):
        raise HTTPException(status_code=403, detail="내부 API 인증 실패")


def verify_dynamic_flag_internal_key(request: Request) -> None:
    """raw dynamic flag 경로에는 개발 환경의 내부 API 무인증 예외를 적용하지 않는다."""
    if not settings.internal_api_key:
        raise HTTPException(
            status_code=503,
            detail="동적 플래그 내부 API 인증을 사용할 수 없습니다.",
        )
    key = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(key, settings.internal_api_key):
        raise HTTPException(status_code=403, detail="내부 API 인증 실패")


def verify_cleanup_internal_key(request: Request) -> None:
    """삭제 증거에는 개발 환경의 내부 API 무인증 예외도 허용하지 않는다."""
    if not settings.internal_api_key:
        raise HTTPException(status_code=503, detail="문제 정리 내부 API 인증을 사용할 수 없습니다.")
    key = request.headers.get("X-Internal-Key", "")
    if not hmac.compare_digest(key, settings.internal_api_key):
        raise HTTPException(status_code=403, detail="내부 API 인증 실패")


def is_env_orchestrator_build_request(request: Request) -> bool:
    """공개 문제 재빌드는 Env-orchestrator의 명시적 내부 작업만 허용한다."""
    return hmac.compare_digest(
        request.headers.get("X-Env-Orchestrator-Build", ""),
        "v1",
    )


router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_key)])


@router.get(
    "/challenges/{challenge_id}/cleanup-target",
    dependencies=[Depends(verify_cleanup_internal_key)],
)
async def challenge_cleanup_target(
    challenge_id: int,
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """삭제 커밋과 정확한 정리 원장을 확인한 Env 전용 삭제 증거."""
    current = (await db.execute(select(Challenge.id).where(Challenge.id == challenge_id))).scalar_one_or_none()
    if current is not None:
        raise HTTPException(status_code=409, detail={
            "code": "cleanup_challenge_exists", "message": "현재 존재하는 문제는 정리할 수 없습니다.",
        })
    job = (await db.execute(
        select(ChallengeImageCleanupJob).where(
            ChallengeImageCleanupJob.id == job_id,
            ChallengeImageCleanupJob.challenge_id == challenge_id,
        )
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="문제 정리 기록을 찾을 수 없습니다.")
    if job.image_reference not in {"", f"ctf-challenge-{challenge_id}:latest"}:
        raise HTTPException(status_code=409, detail={
            "code": "cleanup_target_rejected", "message": "정리 대상의 소유권을 확인할 수 없습니다.",
        })
    return {
        "job_id": str(job.id),
        "challenge_id": challenge_id,
        "deleted": True,
        "image_reference": job.image_reference,
    }


_DYNAMIC_FLAG_ISSUE_CLOCK_SKEW_SECONDS = 60
_DYNAMIC_FLAG_REVOCATION_REASONS = {
    "instance_stopped",
    "instance_expired",
    "instance_failed",
    "environment_stale",
    "user_banned",
    "reissued",
    "cleanup_failed",
}


def _require_dynamic_instance_feature() -> None:
    if not settings.dynamic_instance_flags_enabled:
        raise HTTPException(
            status_code=503,
            detail="동적 플래그 발급 기능을 사용할 수 없습니다.",
        )


def _resolve_dynamic_issue_key(policy: ChallengeFlagPolicy) -> bytes:
    """정책 key version을 현재 server-side keyring에서 해석한다.

    key material, keyring 형식의 실제 입력, raw flag/token은 로그와 응답에 포함하지 않는다.
    """
    try:
        keyring = parse_dynamic_flag_keyring(settings.dynamic_flag_hmac_keyring)
        return resolve_dynamic_hmac_key(
            keyring=keyring,
            key_version=policy.hmac_key_version,
        )
    except DynamicFlagConfigurationError as error:
        logger.warning("동적 플래그 HMAC keyring을 사용할 수 없습니다: %s", error)
        raise HTTPException(
            status_code=503,
            detail="동적 플래그 발급 기능을 사용할 수 없습니다.",
        ) from error


class DynamicFlagIssueRequest(BaseModel):
    """Env orchestrator의 instance-bound one-shot dynamic flag 요청."""

    model_config = ConfigDict(extra="forbid")

    challenge_id: int = Field(..., ge=1)
    instance_id: str = Field(
        ...,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$",
    )
    user_id: UUID
    competition_id: UUID | None = None
    environment_revision: int = Field(..., ge=1)
    verified_image_id: str = Field(..., min_length=1, max_length=128)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def require_timezone_aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("동적 플래그 만료 시각에는 시간대 정보가 필요합니다.")
        return value.astimezone(timezone.utc)


class DynamicFlagRevokeRequest(BaseModel):
    """인스턴스 종료·실패·drift 뒤 issuance를 멱등하게 무효화하는 요청."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(
        ...,
        min_length=3,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$",
    )
    instance_locator: str | None = Field(
        None,
        min_length=16,
        max_length=64,
        pattern=r"^[A-Z2-7]{16,64}$",
    )
    reason: str = Field(..., min_length=1, max_length=64)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if value not in _DYNAMIC_FLAG_REVOCATION_REASONS:
            raise ValueError("동적 플래그 폐기 사유가 올바르지 않습니다.")
        return value


@router.get("/stats")
async def platform_learn_stats(db: AsyncSession = Depends(get_db)):
    """플랫폼 학습 통계 (auth-service 대시보드용)."""
    challenge_count = (
        await db.execute(
            select(func.count()).select_from(Challenge).where(Challenge.is_published.is_(True))
        )
    ).scalar() or 0

    total_solves = (
        await db.execute(
            select(func.count()).select_from(Submission).where(Submission.is_correct.is_(True))
        )
    ).scalar() or 0

    return {
        "total_challenges": challenge_count,
        "total_solves": total_solves,
    }


@router.get("/users/{user_id}/summary")
async def user_summary(user_id: str, db: AsyncSession = Depends(get_db)):
    """사용자 풀이 요약 (공개 문제 총 정답 수)."""
    result = await db.execute(
        select(func.count())
        .select_from(Submission)
        .join(Challenge, Challenge.id == Submission.challenge_id)
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            Submission.competition_id.is_(None),
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
    )
    total_solves = result.scalar() or 0
    return {"total_solves": total_solves}


# 난이도별 가중치 — beginner 1개 풀었다고 100% 찍히는 것을 방지
DIFFICULTY_WEIGHT = {
    "beginner": 1,
    "easy": 2,
    "medium_low": 3,
    "medium": 4,
    "medium_high": 6,
    "hard": 8,
    "expert": 12,
    "impossible": 16,
}


@router.get("/users/{user_id}/stats")
async def user_stats(user_id: str, db: AsyncSession = Depends(get_db)):
    """카테고리별 풀이 통계 (난이도 가중치 기반).

    value = 사용자가 풀이한 문제들의 가중치 합
    max   = 해당 카테고리 전체 공개 문제의 가중치 합
    """
    # 사용자가 풀이한 워게임 문제의 카테고리+난이도별 가중치 합
    solved_result = await db.execute(
        select(Challenge.category, Challenge.difficulty, func.count())
        .join(Submission, Submission.challenge_id == Challenge.id)
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            Submission.competition_id.is_(None),
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
        .group_by(Challenge.category, Challenge.difficulty)
    )
    solved_weighted: dict[str, int] = {}
    for cat, diff, cnt in solved_result.all():
        w = DIFFICULTY_WEIGHT.get(diff, 1)
        solved_weighted[cat] = solved_weighted.get(cat, 0) + cnt * w

    # 카테고리별 전체 워게임 공개 문제의 가중치 합
    total_result = await db.execute(
        select(Challenge.category, Challenge.difficulty, func.count())
        .where(
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
        .group_by(Challenge.category, Challenge.difficulty)
    )
    total_weighted: dict[str, int] = {}
    for cat, diff, cnt in total_result.all():
        w = DIFFICULTY_WEIGHT.get(diff, 1)
        total_weighted[cat] = total_weighted.get(cat, 0) + cnt * w

    label_map = {
        "web": "Web",
        "pwn": "Pwn",
        "rev": "Reversing",
        "crypto": "Crypto",
        "forensic": "Forensic",
        "misc": "Misc",
    }
    categories = ["web", "pwn", "rev", "crypto", "forensic", "misc"]

    return [
        {
            "label": label_map.get(cat, cat),
            "value": solved_weighted.get(cat, 0),
            "max": total_weighted.get(cat, 0),
        }
        for cat in categories
    ]


@router.get("/users/{user_id}/history")
async def user_history(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """풀이 이력 (정답만, 최신순)."""
    result = await db.execute(
        select(
            Submission.id,
            Challenge.name,
            Challenge.category,
            Challenge.value,
            Submission.submitted_at,
        )
        .join(Challenge, Challenge.id == Submission.challenge_id)
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
        )
        .order_by(Submission.submitted_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "category": row.category,
            "value": row.value,
            "solved_at": row.submitted_at.isoformat() if row.submitted_at else None,
        }
        for row in result.all()
    ]


@router.get("/users/{user_id}/activity")
async def user_activity(
    user_id: str,
    days: int = Query(182, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """활동 히트맵 데이터 (날짜별 제출 횟수)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(
            func.date(Submission.submitted_at).label("date"),
            func.count().label("count"),
        )
        .where(
            Submission.user_id == user_id,
            Submission.submitted_at >= since,
        )
        .group_by(func.date(Submission.submitted_at))
    )
    return {str(row.date): row.count for row in result.all()}


@router.get("/challenges/{challenge_id}/env")
async def challenge_env_info(
    challenge_id: int,
    purpose: Literal["instance", "verification"] = Query("instance"),
    db: AsyncSession = Depends(get_db),
):
    """Env orchestrator용 환경 정보.

    인스턴스 생성은 공개된 현재 revision 검증 환경만 읽는다. 초안 환경 조회는 관리자
    검증 컨테이너를 시작하는 내부 purpose에서만 허용한다.
    """
    query = (
        select(Challenge)
        .options(selectinload(Challenge.dynamic_flag_policy))
        .where(Challenge.id == challenge_id)
    )
    if purpose == "instance":
        query = query.where(Challenge.is_published.is_(True))
    result = await db.execute(query)
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    if challenge.env_type == "none":
        raise HTTPException(status_code=400, detail="이 챌린지는 실습 환경이 없습니다.")

    # static/regex environments do not receive a real platform flag mount.
    # Build-context handling supplies a non-secret placeholder when a legacy
    # Dockerfile HEALTHCHECK only needs a readable flag.txt.  The actual
    # readiness result remains authoritative: an application that truly needs
    # a participant flag will report healthcheck_unhealthy instead of being
    # rejected before a generic Dockerfile build can run.

    if not challenge.docker_image:
        raise HTTPException(
            status_code=400,
            detail="Docker 이미지가 설정되지 않았습니다. 관리자 페이지에서 이미지를 빌드하거나 지정해주세요.",
        )

    ready = environment_is_verified_for_runtime(
        challenge.env_type,
        challenge.environment_revision,
        challenge.environment_verified_revision,
        challenge.environment_verified_image_id,
    )
    if purpose == "instance" and not ready:
        raise HTTPException(
            status_code=409,
            detail="현재 컨테이너 환경 revision이 검증되지 않았습니다. 관리자에게 환경 검증을 요청하세요. (environment_verification_required)",
        )

    return {
        "env_type": challenge.env_type,
        "docker_image": challenge.docker_image,
        "container_port": challenge.container_port,
        "challenge_type": challenge.challenge_type,
        "environment_revision": challenge.environment_revision,
        "environment_ready": ready,
        # Env orchestrator가 mutable image tag drift를 감지할 수 있게 현재 검증
        # image ID만 전달한다. 준비 상태가 아니면 빈 값이며 instance 목적은 앞서 차단된다.
        "environment_verified_image_id": challenge.environment_verified_image_id if ready else None,
        # HMAC key, raw flag, issuance locator, instance metadata는 Env 정보 응답에
        # 포함하지 않는다. dynamic_instance가 아니면 null로 구분한다.
        "dynamic_flag": (
            {
                "enabled": challenge.dynamic_flag_policy.enabled,
                "runtime_path": challenge.dynamic_flag_policy.runtime_path,
                "ttl_seconds": challenge.dynamic_flag_policy.ttl_seconds,
                "policy_revision": challenge.dynamic_flag_policy.policy_revision,
            }
            if challenge.flag_type == DYNAMIC_FLAG_TYPE
            and challenge.dynamic_flag_policy is not None
            else None
        ),
    }


@router.post(
    "/dynamic-flags/issue",
    dependencies=[Depends(verify_dynamic_flag_internal_key)],
)
async def issue_dynamic_flag(
    body: DynamicFlagIssueRequest,
    db: AsyncSession = Depends(get_db),
):
    """Env orchestrator에 한 번만 raw dynamic flag를 전달한다.

    이 endpoint는 internal service identity 경계 뒤에만 있으며, 정상 응답의 `flag`는
    Env가 즉시 private runtime file에 쓰는 용도다. DB·Redis·로그·다른 API 응답에는
    raw 값이 남지 않는다.
    """
    _require_dynamic_instance_feature()
    try:
        is_active, _role = await get_current_authorization(str(body.user_id))
    except (RuntimeError, ValueError) as error:
        # Auth client가 초기화되지 않았거나 안전하게 해석할 수 없는 응답이면 동적
        # 플래그를 발급하지 않는다. user_id, key material, raw flag는 기록하지 않는다.
        logger.warning("동적 플래그 발급 전 계정 상태를 확인할 수 없습니다: %s", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="동적 플래그 발급 권한을 확인할 수 없습니다. 잠시 후 다시 시도하세요.",
        ) from error
    if not is_active:
        # 외부 Env caller에 계정 상태 세부를 전달하지 않는다.
        raise HTTPException(status_code=403, detail="동적 플래그 발급 권한이 없습니다.")

    result = await db.execute(
        select(Challenge)
        .options(selectinload(Challenge.dynamic_flag_policy))
        .where(Challenge.id == body.challenge_id)
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if challenge is None:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    policy = challenge.dynamic_flag_policy
    if (
        not challenge.is_published
        or challenge.flag_type != DYNAMIC_FLAG_TYPE
        or policy is None
        or not policy.enabled
    ):
        raise HTTPException(status_code=409, detail="동적 플래그를 발급할 수 없는 문제 상태입니다.")
    if not environment_is_verified_for_runtime(
        challenge.env_type,
        challenge.environment_revision,
        challenge.environment_verified_revision,
        challenge.environment_verified_image_id,
    ):
        raise HTTPException(status_code=409, detail="현재 실습 환경 검증이 필요합니다.")
    if body.environment_revision != challenge.environment_revision or not hmac.compare_digest(
        body.verified_image_id,
        challenge.environment_verified_image_id or "",
    ):
        raise HTTPException(status_code=409, detail="현재 실습 환경 revision과 일치하지 않습니다.")

    competition_id = str(body.competition_id) if body.competition_id else None
    if challenge.challenge_type == "competition":
        if competition_id is None or not await verify_competition_challenge_access(
            competition_id=competition_id,
            user_id=str(body.user_id),
            challenge_id=challenge.id,
            purpose="environment",
        ):
            raise HTTPException(status_code=403, detail="동적 플래그 발급 권한이 없습니다.")
    elif competition_id is not None:
        raise HTTPException(status_code=422, detail="워게임 문제에는 competition_id를 사용할 수 없습니다.")

    # 동적 flag는 컨테이너 파일로 정답 원문을 전달하므로, 화면의 locked 표기나 Env의
    # 요청만 신뢰해서는 안 된다. 제출과 같은 선행 문제·학습 조건을 Learn에서 다시
    # 확인해 잠긴 문제의 인스턴스 발급 경로를 fail-closed로 막는다.
    try:
        access_allowed = await user_can_access_challenge_for_instance(
            challenge_id=challenge.id,
            user_id=body.user_id,
            competition_id=competition_id,
            db=db,
        )
    except Exception as error:
        # 선행조건 조회 오류에서 발급을 계속하면 잠긴 문제의 raw flag가 컨테이너에
        # 전달될 수 있다. DB 세부·원문 flag는 기록하지 않는 fail-closed 경계다.
        logger.warning(
            "동적 플래그 발급 전 Learn 접근 조건을 확인할 수 없습니다: %s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="동적 플래그 발급 권한을 확인할 수 없습니다. 잠시 후 다시 시도하세요.",
        ) from error
    if not access_allowed:
        raise HTTPException(status_code=403, detail="동적 플래그 발급 권한이 없습니다.")

    now = datetime.now(timezone.utc)
    expires_at = body.expires_at.astimezone(timezone.utc)
    max_expiry = now + timedelta(
        seconds=policy.ttl_seconds + _DYNAMIC_FLAG_ISSUE_CLOCK_SKEW_SECONDS
    )
    if expires_at <= now or expires_at > max_expiry:
        raise HTTPException(status_code=422, detail="동적 플래그 만료 시간이 정책 범위를 벗어났습니다.")

    key = _resolve_dynamic_issue_key(policy)

    # row가 아직 없는 신규 instance의 동시 issue도 하나의 transaction으로 직렬화한다.
    # PostgreSQL-only 서비스이므로 advisory lock으로 active partial unique race를 막는다.
    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtext(:instance_id))"),
        {"instance_id": body.instance_id},
    )
    prior_result = await db.execute(
        select(DynamicFlagIssuance)
        .where(DynamicFlagIssuance.instance_id == body.instance_id)
        .with_for_update()
    )
    prior_issuances = prior_result.scalars().all()
    for prior in prior_issuances:
        if prior.revoked_at is not None or prior.consumed_at is not None:
            continue
        prior_is_live = prior.expires_at > now
        binding_matches = (
            prior.challenge_id == challenge.id
            and prior.user_id == body.user_id
            and prior.competition_id == competition_id
            and prior.environment_revision == challenge.environment_revision
            and prior.verified_image_id == challenge.environment_verified_image_id
        )
        if prior_is_live and not binding_matches:
            raise HTTPException(status_code=409, detail="인스턴스 동적 플래그 결합 정보가 일치하지 않습니다.")
        # 같은 instance의 연장 재발급 또는 만료 row는 새 locator를 만들기 전에 명시
        # revoke한다. 이력은 보존하고 active partial unique만 해제한다.
        prior.revoked_at = now
        prior.revocation_reason = "reissued"

    issuance: DynamicFlagIssuance | None = None
    raw_flag: str | None = None
    for _attempt in range(3):
        locator = generate_dynamic_locator()
        token = generate_dynamic_token()
        candidate = DynamicFlagIssuance(
            instance_locator=locator,
            instance_id=body.instance_id,
            challenge_id=challenge.id,
            user_id=body.user_id,
            competition_id=competition_id,
            environment_revision=challenge.environment_revision,
            verified_image_id=challenge.environment_verified_image_id,
            policy_revision=policy.policy_revision,
            hmac_key_version=policy.hmac_key_version,
            token_hmac=dynamic_token_hmac(key=key, locator=locator, token=token),
            issued_at=now,
            expires_at=expires_at,
        )
        try:
            async with db.begin_nested():
                db.add(candidate)
                await db.flush()
        except IntegrityError:
            # 랜덤 locator collision은 재시도하되 input/token을 기록하지 않는다.
            continue
        issuance = candidate
        raw_flag = build_dynamic_flag(locator=locator, token=token)
        break

    if issuance is None or raw_flag is None:
        raise HTTPException(status_code=503, detail="동적 플래그 발급 기능을 사용할 수 없습니다.")

    await db.commit()
    return {
        "flag": raw_flag,
        "instance_locator": issuance.instance_locator,
        "expires_at": issuance.expires_at.isoformat(),
        "runtime_path": policy.runtime_path,
    }


@router.post(
    "/dynamic-flags/revoke",
    dependencies=[Depends(verify_dynamic_flag_internal_key)],
)
async def revoke_dynamic_flag(
    body: DynamicFlagRevokeRequest,
    db: AsyncSession = Depends(get_db),
):
    """인스턴스 lifecycle 이벤트 뒤 dynamic issuance를 멱등하게 폐기한다."""
    query = select(DynamicFlagIssuance).where(
        DynamicFlagIssuance.instance_id == body.instance_id
    )
    if body.instance_locator is not None:
        query = query.where(DynamicFlagIssuance.instance_locator == body.instance_locator)
    result = await db.execute(query.with_for_update())
    issuances = result.scalars().all()

    revoked = 0
    now = datetime.now(timezone.utc)
    for issuance in issuances:
        if issuance.revoked_at is None and issuance.consumed_at is None:
            issuance.revoked_at = now
            issuance.revocation_reason = body.reason
            revoked += 1
    if revoked:
        await db.commit()
    return {"revoked": revoked}


class EnvironmentVerificationRecord(BaseModel):
    """Env orchestrator가 실제 준비 확인 뒤 남기는 최소 결과."""

    revision: int = Field(..., ge=1)
    status: Literal["ready", "failed"]
    failure_code: str | None = Field(None, max_length=64)
    image_reference: str = Field(..., min_length=1, max_length=500)
    image_id: str | None = Field(None, min_length=1, max_length=128)
    verified_by: UUID

    @model_validator(mode="after")
    def validate_failure_contract(self):
        if self.status == "ready" and self.failure_code is not None:
            raise ValueError("준비 완료 검증에는 failure_code를 지정할 수 없습니다.")
        if self.status == "ready" and not self.image_id:
            raise ValueError("준비 완료 검증에는 Docker image ID가 필요합니다.")
        if self.status == "failed":
            self.failure_code = normalize_environment_failure_code(self.failure_code)
        return self


@router.post("/challenges/{challenge_id}/environment-verifications")
async def record_environment_verification(
    challenge_id: int,
    body: EnvironmentVerificationRecord,
    db: AsyncSession = Depends(get_db),
):
    """현재 environment revision의 실제 검증 결과를 원자적으로 기록한다."""
    result = await db.execute(
        select(Challenge)
        .where(Challenge.id == challenge_id)
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    if challenge.env_type == "none":
        raise HTTPException(status_code=422, detail="컨테이너 환경이 없는 문제는 검증할 수 없습니다.")
    if body.revision != challenge.environment_revision:
        raise HTTPException(
            status_code=409,
            detail="검증 중 환경 설정이 변경되었습니다. 현재 revision을 다시 검증하세요. (stale_environment_revision)",
        )
    if not hmac.compare_digest(body.image_reference, challenge.docker_image):
        raise HTTPException(
            status_code=409,
            detail="검증 이미지가 현재 문제 설정과 다릅니다. (stale_environment_revision)",
        )

    checked_at = datetime.now(timezone.utc)
    db.add(
        ChallengeEnvironmentVerification(
            challenge_id=challenge.id,
            revision=body.revision,
            status=body.status,
            failure_code=body.failure_code,
            image_reference=body.image_reference,
            image_id=body.image_id,
            verified_by=body.verified_by,
            checked_at=checked_at,
        )
    )
    challenge.environment_last_checked_at = checked_at
    challenge.environment_last_failure_code = body.failure_code
    if body.status == "ready":
        challenge.environment_verified_revision = body.revision
        challenge.environment_verified_at = checked_at
        challenge.environment_verified_by = body.verified_by
        challenge.environment_verified_image_id = body.image_id
    else:
        # 같은 revision의 과거 성공 기록도 새 실패 결과보다 우선하지 않는다.
        challenge.environment_verified_revision = None
        challenge.environment_verified_at = None
        challenge.environment_verified_by = None
        challenge.environment_verified_image_id = None

    await db.commit()
    return {
        "status": body.status,
        "revision": body.revision,
        "checked_at": checked_at.isoformat(),
        "environment_status": environment_status(
            challenge.env_type,
            challenge.environment_revision,
            challenge.environment_verified_revision,
        ),
    }


@router.get("/challenges/{challenge_id}/dockerfile")
async def challenge_dockerfile(
    challenge_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Dockerfile 내용 조회 (env-orchestrator 빌드용 — 레거시/폴백)."""
    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id)
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    if challenge.env_type != "dockerfile":
        raise HTTPException(status_code=400, detail="이 챌린지는 Dockerfile 타입이 아닙니다.")

    if challenge.is_published and not is_env_orchestrator_build_request(request):
        raise HTTPException(
            status_code=409,
            detail="공개된 컨테이너 환경은 먼저 비공개로 전환한 뒤 다시 빌드하세요. (environment_unpublish_required)",
        )

    if not challenge.dockerfile_content:
        raise HTTPException(status_code=400, detail="Dockerfile 내용이 비어있습니다.")

    return {
        "dockerfile_content": challenge.dockerfile_content,
        "container_port": challenge.container_port,
        "environment_revision": challenge.environment_revision,
        "flag_type": challenge.flag_type,
    }


@router.get("/challenges/{challenge_id}/build-files")
async def challenge_build_files(
    challenge_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """빌드 컨텍스트 파일 목록 + presigned URL (env-orchestrator 빌드용)."""
    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id)
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    if challenge.env_type != "dockerfile":
        raise HTTPException(status_code=400, detail="이 챌린지는 Dockerfile 타입이 아닙니다.")

    if challenge.is_published and not is_env_orchestrator_build_request(request):
        raise HTTPException(
            status_code=409,
            detail="공개된 컨테이너 환경은 먼저 비공개로 전환한 뒤 빌드 파일을 사용하세요. (environment_unpublish_required)",
        )

    try:
        files = get_build_file_urls(challenge_id)
    except BuildContextStorageUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="빌드 컨텍스트를 확인할 수 없습니다. 잠시 후 다시 시도하세요. (build_context_unavailable)",
        ) from None

    return {
        "container_port": challenge.container_port,
        "environment_revision": challenge.environment_revision,
        "flag_type": challenge.flag_type,
        "files": files,
    }


class DockerImageUpdate(BaseModel):
    docker_image: str = Field(..., min_length=1, max_length=500)
    source_revision: int = Field(..., ge=1)


@router.patch("/challenges/{challenge_id}/docker-image")
async def update_docker_image(
    challenge_id: int,
    body: DockerImageUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Docker 이미지명 업데이트 (env-orchestrator 빌드 완료 후 호출)."""
    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id).with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    if challenge.is_published:
        if not is_env_orchestrator_build_request(request):
            raise HTTPException(
                status_code=409,
                detail="공개된 컨테이너 환경은 먼저 비공개로 전환한 뒤 이미지 revision을 갱신하세요. (environment_unpublish_required)",
            )
        if body.docker_image != challenge.docker_image:
            raise HTTPException(
                status_code=409,
                detail="공개된 문제의 Docker image tag는 변경할 수 없습니다. (published_image_immutable)",
            )
    if body.source_revision != challenge.environment_revision:
        raise HTTPException(
            status_code=409,
            detail="빌드 원본 환경 revision이 현재 설정과 다릅니다. 현재 환경을 다시 빌드하세요. (stale_environment_revision)",
        )

    # 공개 문제는 내부 orchestrator가 동일 tag를 재빌드해 이미지가 사라진/손상된
    # 배포를 복구할 수 있도록 허용한다. 공개 상태에서 tag나 환경 revision을 바꾸지는
    # 않으며, 새 image ID는 다음 검증 단계에서 다시 기록한다.
    challenge.docker_image = body.docker_image
    if not challenge.is_published:
        challenge.environment_revision = max(1, challenge.environment_revision or 1) + 1
    challenge.environment_verified_revision = None
    challenge.environment_verified_at = None
    challenge.environment_verified_by = None
    challenge.environment_verified_image_id = None
    challenge.environment_last_checked_at = datetime.now(timezone.utc)
    challenge.environment_last_failure_code = "image_rebuilt"
    await db.commit()
    return {
        "message": "Docker 이미지가 업데이트되었습니다. 환경 검증이 다시 필요합니다.",
        "docker_image": body.docker_image,
        "environment_revision": challenge.environment_revision,
    }


@router.get("/users/{user_id}/badge-data")
async def user_badge_data(user_id: str, db: AsyncSession = Depends(get_db)):
    """뱃지 평가에 필요한 풀이 데이터 일괄 조회.

    반환:
        category_solves: 카테고리별 정답 수 (web: 5, pwn: 3, ...)
        has_night_solve: 새벽 3~5시 정답 여부
        has_early_bird: 문제 출제 후 1시간 내 첫 정답 여부
        has_speed_solve: 60초 이내 풀이 또는 first blood 여부
        perfectionist_categories: 카테고리 전체 풀이 목록
        course_completions: 완료한 코스 수
    """
    from app.models.course import UserProgress, Lesson, Course

    # 1) 카테고리별 정답 수 (워게임 공개 문제만)
    cat_result = await db.execute(
        select(Challenge.category, func.count())
        .join(Submission, Submission.challenge_id == Challenge.id)
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            Submission.competition_id.is_(None),
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
        .group_by(Challenge.category)
    )
    category_solves: dict[str, int] = {row[0]: row[1] for row in cat_result.all()}

    # 2) 새벽 3~5시 정답 여부 (night_owl)
    night_result = await db.execute(
        select(func.count())
        .select_from(Submission)
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            func.extract("hour", Submission.submitted_at).between(3, 4),
        )
    )
    has_night_solve = (night_result.scalar() or 0) > 0

    # 3) 문제 출제 후 1시간 내 정답 여부 (early_bird)
    # 해석: 사용자가 문제 생성 후 1시간 이내에 풀었는지 (최초 풀이자 여부는 무관)
    # 주의: timedelta → PostgreSQL INTERVAL 변환에 의존 (asyncpg 사용 전제)
    early_result = await db.execute(
        select(func.count())
        .select_from(Submission)
        .join(Challenge, Challenge.id == Submission.challenge_id)
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            Submission.submitted_at <= Challenge.created_at + timedelta(hours=1),
        )
    )
    has_early_bird = (early_result.scalar() or 0) > 0

    # 4) 카테고리 전체 풀이 (perfectionist)
    # 카테고리별 워게임 공개 문제 수
    total_per_cat_result = await db.execute(
        select(Challenge.category, func.count())
        .where(
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
        .group_by(Challenge.category)
    )
    total_per_cat = {row[0]: row[1] for row in total_per_cat_result.all()}

    perfectionist_categories: list[str] = [
        cat for cat, total in total_per_cat.items()
        if total > 0 and category_solves.get(cat, 0) >= total
    ]

    # 5) 완료한 코스 수
    # 코스의 모든 레슨을 완료한 경우 코스 완료로 간주
    course_result = await db.execute(
        select(Course.id, func.count(Lesson.id).label("total_lessons"))
        .join(Lesson, Lesson.course_id == Course.id)
        .where(Course.is_published.is_(True))
        .group_by(Course.id)
    )
    all_courses = {row[0]: row[1] for row in course_result.all()}

    completed_result = await db.execute(
        select(Lesson.course_id, func.count(UserProgress.id).label("done"))
        .join(Lesson, Lesson.id == UserProgress.lesson_id)
        .where(
            UserProgress.user_id == user_id,
            UserProgress.completed.is_(True),
        )
        .group_by(Lesson.course_id)
    )
    completed_per_course = {row[0]: row[1] for row in completed_result.all()}

    course_completions = sum(
        1 for cid, total in all_courses.items()
        if completed_per_course.get(cid, 0) >= total
    )

    # 6) speed_demon: 빠른 풀이 여부 (2가지 조건 중 하나 충족 시 인정)
    #   조건 A: 사용자의 첫 제출부터 정답까지 60초 이내
    #   조건 B: 해당 챌린지의 첫 번째 정답자 (first blood)

    # 조건 A: 사용자별·챌린지별 첫 제출 시각 기준 60초 이내 정답
    first_attempt_sq = (
        select(
            Submission.challenge_id,
            func.min(Submission.submitted_at).label("first_attempt_at"),
        )
        .where(Submission.user_id == user_id)
        .group_by(Submission.challenge_id)
        .subquery()
    )
    speed_result = await db.execute(
        select(func.count())
        .select_from(Submission)
        .join(
            first_attempt_sq,
            Submission.challenge_id == first_attempt_sq.c.challenge_id,
        )
        .where(
            Submission.user_id == user_id,
            Submission.is_correct.is_(True),
            Submission.submitted_at <= first_attempt_sq.c.first_attempt_at + timedelta(seconds=60),
        )
    )
    has_speed_solve = (speed_result.scalar() or 0) > 0

    # 조건 B: first blood (해당 챌린지의 최초 정답자)
    if not has_speed_solve:
        first_solve_sq = (
            select(
                Submission.challenge_id,
                func.min(Submission.submitted_at).label("first_at"),
            )
            .where(Submission.is_correct.is_(True))
            .group_by(Submission.challenge_id)
            .subquery()
        )
        fb_result = await db.execute(
            select(func.count())
            .select_from(Submission)
            .join(
                first_solve_sq,
                (Submission.challenge_id == first_solve_sq.c.challenge_id)
                & (Submission.submitted_at == first_solve_sq.c.first_at),
            )
            .where(
                Submission.user_id == user_id,
                Submission.is_correct.is_(True),
            )
        )
        has_speed_solve = (fb_result.scalar() or 0) > 0

    return {
        "category_solves": category_solves,
        "has_night_solve": has_night_solve,
        "has_early_bird": has_early_bird,
        "has_speed_solve": has_speed_solve,
        "perfectionist_categories": perfectionist_categories,
        "course_completions": course_completions,
    }


class BulkChallengeNamesRequest(BaseModel):
    challenge_ids: list[int]


@router.post("/challenges/batch-names")
async def batch_challenge_names(
    req: BulkChallengeNamesRequest,
    db: AsyncSession = Depends(get_db),
):
    """챌린지 ID 목록으로 이름을 일괄 조회 (competition 표시용)."""
    if not req.challenge_ids:
        return {}
    result = await db.execute(
        select(Challenge.id, Challenge.name)
        .where(Challenge.id.in_(req.challenge_ids))
    )
    return {str(row.id): row.name for row in result.all()}


@router.post("/challenges/batch-info")
async def batch_challenge_info(
    req: BulkChallengeNamesRequest,
    db: AsyncSession = Depends(get_db),
):
    """챌린지 ID 목록의 CTF 매핑 검증용 메타데이터를 일괄 조회한다.

    이 엔드포인트는 내부 키가 필요한 경로다. Auth 서비스는 반환된 공개 상태와 유형을
    사용해 잘못된 대회 매핑을 fail-closed로 제외한다.
    """
    if not req.challenge_ids:
        return {}
    result = await db.execute(
        select(
            Challenge.id,
            Challenge.name,
            Challenge.description,
            Challenge.env_type,
            Challenge.container_port,
            Challenge.challenge_type,
            Challenge.is_published,
            Challenge.environment_revision,
            Challenge.environment_verified_revision,
            Challenge.environment_verified_image_id,
        )
        .where(Challenge.id.in_(req.challenge_ids))
    )
    return {
        str(r.id): {
            "name": r.name,
            "description": r.description or "",
            "env_type": r.env_type or "none",
            "container_port": r.container_port,
            "challenge_type": r.challenge_type,
            "is_published": r.is_published,
            "environment_ready": environment_is_verified_for_runtime(
                r.env_type,
                r.environment_revision,
                r.environment_verified_revision,
                r.environment_verified_image_id,
            ),
            "environment_status": _environment_status_for_runtime(
                r.env_type,
                r.environment_revision,
                r.environment_verified_revision,
                r.environment_verified_image_id,
            ),
        }
        for r in result.all()
    }


class BulkSolvesRequest(BaseModel):
    user_ids: list[str]
    category: str | None = None


@router.post("/users/solves")
async def bulk_user_solves(
    req: BulkSolvesRequest,
    db: AsyncSession = Depends(get_db),
):
    """사용자별 정답 수 일괄 조회 (랭킹 API용).

    category가 지정되면 해당 카테고리 문제의 정답 수만 반환한다.
    """
    if not req.user_ids:
        return {}

    query = (
        select(Submission.user_id, func.count())
        .where(
            Submission.user_id.in_(req.user_ids),
            Submission.is_correct.is_(True),
        )
    )

    if req.category:
        query = (
            query
            .join(Challenge, Challenge.id == Submission.challenge_id)
            .where(Challenge.category == req.category)
        )

    query = query.group_by(Submission.user_id)
    result = await db.execute(query)
    return {str(row[0]): row[1] for row in result.all()}
