"""챌린지(워게임 문제) API — 목록/상세/제출/힌트/생성."""

import hmac
import logging
import multiprocessing
import re
from datetime import datetime, timezone
import threading
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import delete as sa_delete, select, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db, async_session
from app.services.challenge_attachments import attachment_descriptors, bind_attachments, require_edit_draft, resolve_attachment
from app.api.deps import (
    get_current_user_id,
    get_optional_user_id,
    get_optional_user_role,
    require_role,
)
from app.models.challenge import (
    Challenge,
    ChallengeEnvironmentVerification,
    ChallengeFlagPolicy,
    ChallengePrerequisite,
    DynamicFlagIssuance,
    Flag,
    Hint,
    Submission,
    HintUnlock,
)
from app.schemas.challenge import (
    ChallengeCreate,
    ChallengeUpdate,
    ChallengeListItem,
    ChallengeListResponse,
    ChallengeDetailResponse,
    DynamicFlagPolicyInput,
    HintItem,
    FlagSubmit,
    LearningContentReference,
    LearningPathChallengeResponse,
    SubmitResponse,
    HintUnlockResponse,
)
from app.schemas.submission import SubmissionAuditDateTime
from app.utils.auth_client import (
    get_client as get_auth_client,
    verify_competition_challenge_access,
)
from app.utils.minio_client import (
    BuildContextStorageUnavailableError,
    validate_build_filename,
    upload_build_file,
    list_build_files,
    delete_build_file,
    delete_all_build_files,
    get_object_stream,
    close_object_stream,
    iter_object_stream,
    MAX_BUILD_FILE_SIZE,
    MAX_BUILD_TOTAL_SIZE,
)
from app.utils.safe_html import (
    RichContentError,
    normalize_rich_content_for_write,
    normalize_rich_content_mapping,
)
from app.utils.translator import auto_translate, auto_translate_rich_content
from collections import defaultdict
from app.models.knowledge import (
    ChallengeLearningPrerequisite,
    ChallengeKnowledgeTag,
    ChallengeRecommendedPrerequisite,
    KnowledgeTag,
    CourseKnowledgeTag,
)
from app.models.course import ContentStatus, Course, Lesson, UserProgress
from app.utils.knowledge_relations import (
    get_approved_helpful_content,
    public_tag_mapping_clause,
    resolve_content,
)
from app.utils.learning_path import build_challenge_learning_path_projection
from app.utils.knowledge_mapping_v2 import public_evidence_summary
from app.utils.knowledge_tag_audit import (
    add_mapping_audit as _add_mapping_audit,
    mapping_snapshot as _mapping_snapshot,
)
from app.utils.knowledge_tag_projection import project_rule_tag_mappings
from app.utils.content_tag_mappings import replace_content_tag_mappings
from app.schemas.knowledge import ContentTagMappingUpdate
from app.utils.environment_verification import (
    detect_dockerfile_exposed_port,
    environment_configuration_changed,
    environment_is_verified_for_runtime,
    environment_status,
    flag_policy_configuration_changed,
    next_environment_revision,
)
from app.models.challenge_image_cleanup import ChallengeImageCleanupJob
from app.services.challenge_imports import export_challenge_batch, import_challenge_batch
from app.utils.dynamic_flags import (
    DYNAMIC_FLAG_TYPE,
    DYNAMIC_SUBMISSION_MARKER,
    DynamicFlagConfigurationError,
    DynamicFlagFormatError,
    dynamic_token_matches,
    normalize_build_context_file,
    parse_dynamic_flag,
    parse_dynamic_flag_keyring,
    resolve_dynamic_hmac_key,
    dynamic_policy_configuration_changed,
)
from app.utils.flag_audit import (
    DYNAMIC_SUBMISSION_STORAGE_VERSION,
    SUBMISSION_STORAGE_VERSION,
    SUBMISSION_REDACTION_MARKER,
    build_submission_storage_metadata,
    encrypt_submitted_flag,
    SUBMITTED_FLAG_DERIVED_KEY_VERSION,
)

logger = logging.getLogger(__name__)
DEFAULT_DOCKER_CONTAINER_PORT = 8080


async def _validate_knowledge_tag_ids(db: AsyncSession, tag_ids: list[int]) -> None:
    """문제 편집기의 태그 입력도 junction FK 오류 전에 명시적 4xx로 끝낸다."""

    if any(tag_id <= 0 for tag_id in tag_ids) or len(set(tag_ids)) != len(tag_ids):
        raise HTTPException(status_code=422, detail="지식 태그 ID 목록이 올바르지 않습니다.")
    if not tag_ids:
        return
    result = await db.execute(
        select(KnowledgeTag.id).where(KnowledgeTag.id.in_(tag_ids))
    )
    if {tag_id for tag_id in result.scalars().all()} != set(tag_ids):
        raise HTTPException(status_code=404, detail="존재하지 않는 지식 태그가 포함되어 있습니다.")


def _build_context_storage_unavailable() -> HTTPException:
    """Keep object-storage details out of the administrator API response."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="빌드 컨텍스트를 확인할 수 없습니다. 잠시 후 다시 시도하세요. (build_context_unavailable)",
    )


def _environment_snapshot(challenge: Challenge) -> dict[str, object]:
    """부분 PATCH와 현재 컨테이너 실행 계약을 비교할 최소 스냅샷."""
    return {
        "env_type": challenge.env_type,
        "dockerfile_content": challenge.dockerfile_content,
        "docker_image": challenge.docker_image,
        "container_port": challenge.container_port,
    }


def _allow_compatibility_flag_healthcheck(
    *,
    env_type: str | None,
    flag_type: str | None,
    dockerfile_content: str | None,
) -> None:
    """Allow a generic Dockerfile/flag.txt context in every flag mode.

    ``flag.txt`` uploaded with static/regex content is normalized to a
    non-secret placeholder by the build-context helper.  That placeholder is
    also added when a legacy Dockerfile has a flag-aware HEALTHCHECK but no
    uploaded file, so gacha-like images can build and become reachable without
    ever receiving a participant flag.  Only ``dynamic_instance`` receives
    the real per-instance read-only mount.  A service that genuinely requires
    the real flag in static/regex mode can still fail its own readiness check;
    that failure is reported as healthcheck/readiness failure rather than an
    upload-time mode mismatch.
    """
    del env_type, flag_type, dockerfile_content


def _invalidate_environment_verification(challenge: Challenge) -> None:
    """환경 입력 또는 빌드 컨텍스트 변경 후 이전 성공을 재사용하지 못하게 한다."""
    challenge.environment_revision = next_environment_revision(challenge.environment_revision)
    challenge.environment_verified_revision = None
    challenge.environment_verified_at = None
    challenge.environment_verified_by = None
    challenge.environment_verified_image_id = None
    challenge.environment_last_checked_at = datetime.now(timezone.utc)
    challenge.environment_last_failure_code = "configuration_changed"


async def _revoke_active_dynamic_issuances_for_stale_environment(
    db: AsyncSession,
    *,
    challenge_id: int,
) -> None:
    """환경 또는 policy revision 변경 뒤 기존 인스턴스 flag를 즉시 무효화한다.

    이 호출은 challenge row lock을 이미 잡은 관리 변경 경로에서만 사용한다. Env가
    runtime file을 제거·컨테이너를 종료하는 lifecycle 작업과 별개로, Learn 제출 검증은
    이 transaction이 commit되는 즉시 기존 raw flag를 받아들이지 않는다.
    """
    await db.execute(
        update(DynamicFlagIssuance)
        .where(
            DynamicFlagIssuance.challenge_id == challenge_id,
            DynamicFlagIssuance.revoked_at.is_(None),
            DynamicFlagIssuance.consumed_at.is_(None),
        )
        .values(
            revoked_at=datetime.now(timezone.utc),
            revocation_reason="environment_stale",
        )
    )


def _environment_is_ready(challenge: Challenge) -> bool:
    return environment_is_verified_for_runtime(
        challenge.env_type,
        challenge.environment_revision,
        challenge.environment_verified_revision,
        challenge.environment_verified_image_id
    )


def _environment_status(challenge: Challenge) -> str:
    current_status = environment_status(
        challenge.env_type,
        challenge.environment_revision,
        challenge.environment_verified_revision,
    )
    if current_status == "ready" and not _environment_is_ready(challenge):
        return "verification_required"
    return current_status


def _require_environment_ready_for_publication(challenge: Challenge) -> None:
    if _environment_is_ready(challenge):
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="현재 컨테이너 환경 revision의 검증이 필요합니다. 이미지 빌드 후 환경 검증을 완료하세요. (environment_verification_required)",
    )


def _require_dynamic_flag_feature() -> None:
    """동적 플래그 rollout feature가 켜져 있는지 확인한다.

    정책 초안도 feature가 꺼진 서버에서는 만들지 않아, UI/API가 배포 순서보다 먼저
    dynamic_instance를 활성화하는 일을 막는다. keyring 오류는 발급·공개 직전에 별도로
    fail-closed 처리한다.
    """
    if not settings.dynamic_instance_flags_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="동적 인스턴스 플래그 기능이 아직 활성화되지 않았습니다. (dynamic_flag_feature_disabled)",
        )


def _resolve_dynamic_policy_key_version(hmac_key_version: str) -> bytes:
    """관리 입력 또는 저장 정책의 HMAC key version을 비밀 없이 해석한다."""
    try:
        keyring = parse_dynamic_flag_keyring(settings.dynamic_flag_hmac_keyring)
        return resolve_dynamic_hmac_key(
            keyring=keyring,
            key_version=hmac_key_version,
        )
    except DynamicFlagConfigurationError as error:
        # key/value를 포함하지 않는 내부 예외만 기록한다. 정책 metadata와 raw flag는
        # 로그에 남기지 않는다.
        logger.warning("동적 플래그 HMAC 설정을 사용할 수 없습니다: %s", error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="동적 플래그 검증 환경을 준비할 수 없습니다. 관리자에게 문의하세요.",
        ) from error


def _resolve_dynamic_policy_key(policy: ChallengeFlagPolicy) -> bytes:
    """저장된 정책의 HMAC key를 비밀을 노출하지 않고 해석한다."""
    return _resolve_dynamic_policy_key_version(policy.hmac_key_version)


def _require_dynamic_policy_ready_for_publication(
    policy: ChallengeFlagPolicy | None,
) -> None:
    """공개 전에 policy·feature·key version이 모두 준비됐는지 fail-closed 확인한다."""
    if policy is None or not policy.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="동적 플래그 정책이 활성화되지 않았습니다. (dynamic_flag_policy_required)",
        )
    _require_dynamic_flag_feature()
    _resolve_dynamic_policy_key(policy)


async def _validate_and_consume_dynamic_flag(
    *,
    challenge: Challenge,
    submitted: str,
    user_id: UUID,
    competition_id: str | None,
    db: AsyncSession,
) -> DynamicFlagIssuance | None:
    """동적 flag를 검증하고 정답 issuance 하나를 원자적으로 소비한다.

    invalid locator, owner/context 불일치, 만료·폐기·환경 drift는 모두 일반 오답으로
    처리한다. caller input 및 raw token을 로그·응답에 넣지 않는다.
    """
    policy_result = await db.execute(
        select(ChallengeFlagPolicy).where(
            ChallengeFlagPolicy.challenge_id == challenge.id
        )
    )
    policy = policy_result.scalar_one_or_none()
    if policy is None or not policy.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="동적 플래그 검증 환경을 준비할 수 없습니다. 관리자에게 문의하세요.",
        )
    _require_dynamic_flag_feature()
    key = _resolve_dynamic_policy_key(policy)

    parsed = parse_dynamic_flag(submitted)
    if parsed is None:
        return None

    issuance_result = await db.execute(
        select(DynamicFlagIssuance)
        .where(DynamicFlagIssuance.instance_locator == parsed.locator)
        .with_for_update()
    )
    issuance = issuance_result.scalar_one_or_none()
    if issuance is None:
        return None

    now = datetime.now(timezone.utc)
    # locator가 존재하더라도 대상과 관계 없는 제출에서는 세부 사유를 노출하지 않는다.
    if (
        issuance.challenge_id != challenge.id
        or issuance.user_id != user_id
        or issuance.competition_id != competition_id
        or issuance.revoked_at is not None
        or issuance.consumed_at is not None
        or issuance.expires_at <= now
        or issuance.policy_revision != policy.policy_revision
        or issuance.hmac_key_version != policy.hmac_key_version
        or issuance.environment_revision != challenge.environment_revision
        or issuance.verified_image_id != challenge.environment_verified_image_id
        or not _environment_is_ready(challenge)
    ):
        return None

    if not dynamic_token_matches(
        expected_hmac=bytes(issuance.token_hmac),
        key=key,
        locator=parsed.locator,
        token=parsed.token,
    ):
        return None

    # 이 row lock은 같은 issuance의 동시 정답 제출을 직렬화한다. 이후 submission과
    # consumed_at은 한 transaction에서 commit되므로 실패 시 함께 rollback된다.
    issuance.consumed_at = now
    return issuance

# Regex 플래그 매칭 타임아웃 (ReDoS 방지)
_REGEX_TIMEOUT_SEC = 2
_REGEX_CONCURRENCY = threading.Semaphore(4)  # 동시 regex 프로세스 수 제한

# ──── 제출 Rate Limiting (Redis 기반) ────


async def _check_rate_limit(user_id: str) -> bool:
    """Redis 기반 분당 오답 제출 횟수 제한. True이면 제한 초과."""
    from app.redis import redis_client

    if not redis_client:
        return False  # Redis 미연결 시 제한 비활성화
    if settings.submit_rate_limit <= 0:
        return False  # 0 = 무제한

    key = f"submit_rate:{user_id}"
    try:
        count = await redis_client.get(key)
        return int(count) >= settings.submit_rate_limit if count else False
    except Exception:
        return False  # Redis 장애 시 제한 비활성화


async def _record_wrong_attempt(user_id: str) -> None:
    """오답 제출 시 Redis 카운터 증가 (60초 TTL)."""
    from app.redis import redis_client

    if not redis_client:
        return
    key = f"submit_rate:{user_id}"
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 60)
    except Exception:
        pass  # Redis 장애 시 무시


def _check_flags(submitted: str, flag_entries: list) -> bool:
    """복수 플래그 중 하나라도 매치되면 True 반환."""
    for flag in flag_entries:
        if flag.type == "regex":
            if _match_regex(submitted, flag.value, flag.case_sensitive):
                return True
        else:
            # static: timing-safe 비교
            correct = flag.value.strip()
            if not flag.case_sensitive:
                if hmac.compare_digest(submitted.lower().encode(), correct.lower().encode()):
                    return True
            else:
                if hmac.compare_digest(submitted.encode(), correct.encode()):
                    return True
    return False


async def _record_competition_solve(
    *,
    competition_id: str,
    user_id: UUID,
    challenge_id: int,
    accepted_at: datetime,
) -> bool:
    """Auth 서비스에 CTF 풀이를 기록하고, 확인된 성공만 True로 반환한다.

    Auth의 기록 API는 멱등이므로, 이전 응답 유실 뒤 재시도해도 같은 풀이를 한 번만
    반영한다. 실패를 정답 성공으로 숨기지 않고 호출자에게 보류 상태를 남긴다.
    """
    try:
        response = await get_auth_client().post(
            f"{settings.auth_service_url}/internal/competition/solve",
            json={
                "competition_id": competition_id,
                "user_id": str(user_id),
                "challenge_id": challenge_id,
                "accepted_at": accepted_at.isoformat(),
            },
        )
        if response.status_code == 200:
            return True
        logger.warning(
            "대회 풀이 기록 거부 또는 실패 (competition=%s, user=%s, challenge=%d, status=%d)",
            competition_id,
            user_id,
            challenge_id,
            response.status_code,
        )
    except Exception as exc:
        logger.warning(
            "대회 풀이 기록 요청 실패 (competition=%s, user=%s, challenge=%d): %s",
            competition_id,
            user_id,
            challenge_id,
            exc,
        )
    return False


async def _require_competition_context(
    *,
    challenge: Challenge,
    competition_id: str | None,
    user_id: UUID,
    purpose: str,
) -> str:
    """CTF 문제의 외부 요청을 Auth의 배정 권한 계약으로 연결한다.

    CTF 문제를 일반 워게임 요청으로 처리하거나, 워게임 문제에 임의 대회 ID를 덧붙이는
    요청을 모두 차단한다. Auth를 확인할 수 없을 때도 허용하지 않는다.
    """
    if challenge.challenge_type != "competition":
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    if not challenge.is_published:
        # 대회 매핑 실수나 직접 API 호출로 비공개 문제의 설명·힌트·환경에 접근하는 일을
        # 막는다. Auth의 매핑 검증과 별도로 Learn이 자신의 공개 상태를 최종 보장한다.
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    if not _environment_is_ready(challenge):
        # 대회 목록은 Auth가 같은 조건으로 이미 필터한다. 직접 URL로 파일·힌트·제출
        # 경로만 우회하는 것을 막기 위해 Learn도 동일한 현재 revision 조건을 강제한다.
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    try:
        normalized_competition_id = str(UUID(competition_id or ""))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="올바른 대회 ID 형식이 아닙니다.")

    allowed = await verify_competition_challenge_access(
        competition_id=normalized_competition_id,
        user_id=str(user_id),
        challenge_id=challenge.id,
        purpose=purpose,
    )
    if not allowed:
        raise HTTPException(
            status_code=403,
            detail="배정된 진행 중인 대회 문제만 접근할 수 있습니다.",
        )
    return normalized_competition_id


def _regex_worker(submitted: str, pattern: str, case_sensitive: bool, result_pipe) -> None:
    """별도 프로세스에서 정규식 매칭 수행 (GIL 우회)."""
    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        matched = re.compile(pattern, flags).fullmatch(submitted) is not None
        result_pipe.send(matched)
    except re.error:
        result_pipe.send(False)
    finally:
        result_pipe.close()


def _match_regex(submitted: str, pattern: str, case_sensitive: bool) -> bool:
    """정규식 플래그 매치 — 별도 프로세스 기반 타임아웃으로 ReDoS 방지.

    Python GIL은 C 레벨 regex를 중단시킬 수 없으므로
    threading 대신 multiprocessing으로 실행하고, 타임아웃 시 프로세스를 kill한다.
    """
    if not _REGEX_CONCURRENCY.acquire(timeout=5):
        logger.warning("정규식 동시 실행 제한 초과: pattern=%s", pattern)
        return False
    try:
        try:
            parent_conn, child_conn = multiprocessing.Pipe(duplex=False)
        except OSError:
            logger.warning("정규식 매칭 Pipe 생성 실패: pattern=%s", pattern)
            return False

        try:
            proc = multiprocessing.Process(
                target=_regex_worker,
                args=(submitted, pattern, case_sensitive, child_conn),
                daemon=True,
            )
            proc.start()
            child_conn.close()  # 부모 측 쓰기 핸들 닫기
        except OSError:
            parent_conn.close()
            child_conn.close()
            logger.warning("정규식 매칭 프로세스 생성 실패: pattern=%s", pattern)
            return False

        try:
            if parent_conn.poll(timeout=_REGEX_TIMEOUT_SEC):
                try:
                    result = parent_conn.recv()
                except (EOFError, OSError):
                    result = False
            else:
                logger.warning("정규식 매칭 타임아웃 (%ds): pattern=%s", _REGEX_TIMEOUT_SEC, pattern)
                proc.kill()
                result = False
        finally:
            proc.join(timeout=1)
            if proc.is_alive():
                proc.kill()
            parent_conn.close()
        return result
    finally:
        _REGEX_CONCURRENCY.release()



def _legacy_learning_prerequisite_is_enforceable(
    prerequisite_type: str,
    *,
    target_is_public: bool,
    course_has_lessons: bool | None = None,
) -> bool:
    """legacy 필수 학습 선행조건에 대한 공통 접근 정책을 반환한다.

    신규 행은 공개·승인·완료 가능한 콘텐츠만 참조할 수 있다. 반면 기존 행은
    삭제·비공개·잘못된 대상 또는 빈 코스를 계속 가리킬 수 있다. 이런 stale
    행은 사용자가 해결할 수 있는 선행조건이 아니라 운영 정리 대상이므로,
    모든 호출 경로에서 제외해 영구 잠금을 만들지 않는다.
    """
    if prerequisite_type == "course":
        return target_is_public and course_has_lessons is True
    if prerequisite_type == "lesson":
        return target_is_public
    return False


async def _check_learning_prerequisites(
    challenge_id: int,
    user_id,
    db: AsyncSession,
) -> tuple[bool, list[dict]]:
    """학습 선행조건 검증. (locked, details) 반환."""
    lp_result = await db.execute(
        select(ChallengeLearningPrerequisite).where(
            ChallengeLearningPrerequisite.challenge_id == challenge_id
        )
    )
    lp_rows = lp_result.scalars().all()
    if not lp_rows:
        return False, []

    details = []
    all_completed = True

    for lp in lp_rows:
        completed = False
        name = ""
        is_enforceable = False
        lesson = None

        if lp.prerequisite_type == "course":
            course = await db.get(Course, lp.prerequisite_target_id)
            course_is_available = bool(
                course
                and not course.is_deleted
                and course.is_published
                and course.status == ContentStatus.APPROVED.value
            )
            total = 0
            if course_is_available:
                total = int(
                    (
                        await db.scalar(
                            select(func.count(Lesson.id)).where(
                                Lesson.course_id == course.id
                            )
                        )
                    )
                    or 0
                )
            course_is_available = _legacy_learning_prerequisite_is_enforceable(
                lp.prerequisite_type,
                target_is_public=course_is_available,
                course_has_lessons=total > 0,
            )
            is_enforceable = course_is_available
            name = course.title if course_is_available else "현재 이용할 수 없는 코스"
            if course_is_available and user_id:
                done = await db.scalar(
                    select(func.count(UserProgress.id))
                    .join(Lesson, Lesson.id == UserProgress.lesson_id)
                    .where(
                        Lesson.course_id == course.id,
                        UserProgress.user_id == user_id,
                        UserProgress.completed.is_(True),
                    )
                )
                completed = total > 0 and done >= total

        elif lp.prerequisite_type == "lesson":
            lesson_result = await db.execute(
                select(Lesson, Course)
                .join(Course, Course.id == Lesson.course_id)
                .where(Lesson.id == lp.prerequisite_target_id)
            )
            lesson_row = lesson_result.first()
            lesson = lesson_row[0] if lesson_row else None
            lesson_course = lesson_row[1] if lesson_row else None
            lesson_is_available = bool(
                lesson
                and lesson_course
                and not lesson_course.is_deleted
                and lesson_course.is_published
                and lesson_course.status == ContentStatus.APPROVED.value
            )
            lesson_is_available = _legacy_learning_prerequisite_is_enforceable(
                lp.prerequisite_type,
                target_is_public=lesson_is_available,
            )
            is_enforceable = lesson_is_available
            name = lesson.title if lesson_is_available else "현재 이용할 수 없는 레슨"
            if lesson_is_available and user_id:
                prog = await db.execute(
                    select(UserProgress).where(
                        UserProgress.user_id == user_id,
                        UserProgress.lesson_id == lesson.id,
                        UserProgress.completed.is_(True),
                    )
                )
                completed = prog.scalar_one_or_none() is not None

        # 보관 삭제·비공개·미승인 콘텐츠가 과거 선행조건으로 남아 있어도 사용자를
        # 영구적으로 잠그면 안 된다. 삭제 워크플로가 관계를 정리하기 전의 안전장치로
        # 해당 조건은 공개 검증에서 제외하고 운영자가 로그로 정리할 수 있게 남긴다.
        if not is_enforceable:
            logger.warning(
                "사용할 수 없는 학습 선행조건을 건너뜁니다: challenge=%s type=%s target=%s",
                challenge_id,
                lp.prerequisite_type,
                lp.prerequisite_target_id,
            )
            continue

        if not completed:
            all_completed = False

        item = {
            "type": lp.prerequisite_type,
            "id": str(lp.prerequisite_target_id),
            "name": name,
            "completed": completed,
        }
        # 레슨인 경우 코스 ID도 포함 (프론트 라우팅용)
        if lp.prerequisite_type == "lesson" and lesson and lesson_is_available:
            item["course_id"] = str(lesson.course_id)
        details.append(item)

    return not all_completed, details


async def _validate_learning_content_references(
    db: AsyncSession,
    references: list[LearningContentReference],
    *,
    field_name: str,
    require_completable_course: bool = False,
) -> None:
    """관리자가 지정한 학습 참조가 실제로 접근 가능한 공개 콘텐츠인지 확인한다.

    요청 스키마는 type/UUID/중복을 먼저 검증하고, 이 함수는 DB 존재성·삭제·공개·승인
    상태를 한 번 더 검증한다. 따라서 malformed body, 숨김 콘텐츠, 삭제된 참조가
    500 오류나 사용자 영구 잠금으로 번지는 일을 막는다.
    """
    if not references:
        return

    course_ids = {item.target_id for item in references if item.type == "course"}
    lesson_ids = {item.target_id for item in references if item.type == "lesson"}
    available_course_ids: set[UUID] = set()
    available_lesson_ids: set[UUID] = set()

    if course_ids:
        course_result = await db.execute(
            select(Course.id).where(
                Course.id.in_(course_ids),
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            )
        )
        available_course_ids = set(course_result.scalars().all())

    if lesson_ids:
        lesson_result = await db.execute(
            select(Lesson.id)
            .join(Course, Course.id == Lesson.course_id)
            .where(
                Lesson.id.in_(lesson_ids),
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            )
        )
        available_lesson_ids = set(lesson_result.scalars().all())

    unavailable = [
        f"{item.type}:{item.target_id}"
        for item in references
        if (
            item.type == "course" and item.target_id not in available_course_ids
        ) or (
            item.type == "lesson" and item.target_id not in available_lesson_ids
        )
    ]
    if unavailable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{field_name}에는 삭제되지 않았고 공개·승인된 코스 또는 레슨만 지정할 수 있습니다: "
                + ", ".join(unavailable)
            ),
        )

    # A mandatory empty course can never reach a completed state.  Reject it
    # at the write boundary instead of creating a lock that list/detail/submit
    # paths would be forced to interpret differently.  Recommendations remain
    # informational and therefore are not subject to this completion rule.
    if require_completable_course and available_course_ids:
        lesson_count_result = await db.execute(
            select(Lesson.course_id, func.count(Lesson.id))
            .where(Lesson.course_id.in_(available_course_ids))
            .group_by(Lesson.course_id)
        )
        lesson_count_by_course = {
            course_id: int(total)
            for course_id, total in lesson_count_result.all()
        }
        empty_course_ids = [
            str(course_id)
            for course_id in sorted(available_course_ids, key=str)
            if lesson_count_by_course.get(course_id, 0) == 0
        ]
        if empty_course_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{field_name}의 코스 선행조건에는 최소 1개의 레슨이 필요합니다: "
                    + ", ".join(empty_course_ids)
                ),
            )


async def _validate_challenge_prerequisites(
    db: AsyncSession,
    prerequisite_ids: list[int],
    *,
    challenge_id: int | None,
    source_challenge_type: str,
) -> None:
    """Validate mandatory challenge prerequisites before replacing rows.

    Wargame prerequisites must be publicly visible wargame challenges. CTF
    contests deliberately do not support mandatory prerequisites: their
    authoritative access contract is competition assignment and schedule, and
    cross-competition prerequisites could otherwise create a permanent lock.
    """
    if not prerequisite_ids:
        return
    if source_challenge_type == "competition":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CTF 문제에는 강제 선행 문제를 지정할 수 없습니다.",
        )
    if len(set(prerequisite_ids)) != len(prerequisite_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="같은 선행 문제를 중복 지정할 수 없습니다.",
        )
    if any(prerequisite_id <= 0 for prerequisite_id in prerequisite_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="선행 문제 ID는 양수여야 합니다.",
        )
    if challenge_id is not None and challenge_id in prerequisite_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="문제 자신을 선행 문제로 지정할 수 없습니다.",
        )

    target_result = await db.execute(
        select(Challenge.id, Challenge.is_published, Challenge.challenge_type).where(
            Challenge.id.in_(prerequisite_ids)
        )
    )
    targets = {
        target_id: (bool(is_published), challenge_type)
        for target_id, is_published, challenge_type in target_result.all()
    }
    missing_ids = [
        prerequisite_id
        for prerequisite_id in prerequisite_ids
        if prerequisite_id not in targets
    ]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="존재하지 않는 선행 문제가 포함되어 있습니다.",
        )

    invalid_target_ids = [
        prerequisite_id
        for prerequisite_id in prerequisite_ids
        if (
            not targets[prerequisite_id][0]
            or targets[prerequisite_id][1] != source_challenge_type
        )
    ]
    if invalid_target_ids:
        detail = "워게임 선행 문제는 공개된 워게임 문제여야 합니다."
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )

    # Edges are stored as ``challenge -> prerequisite``.  Simulate replacing
    # this challenge's outgoing rows, then make sure no candidate can reach
    # the challenge again.  The traversal is read-only and works even when a
    # legacy malformed cycle already exists elsewhere in the graph.
    if challenge_id is None:
        return
    edge_query = select(
        ChallengePrerequisite.challenge_id,
        ChallengePrerequisite.prerequisite_id,
    ).where(ChallengePrerequisite.challenge_id != challenge_id)
    edge_rows = (await db.execute(edge_query)).all()
    adjacency: dict[int, set[int]] = defaultdict(set)
    for source_id, target_id in edge_rows:
        adjacency[source_id].add(target_id)

    for prerequisite_id in prerequisite_ids:
        to_visit = [prerequisite_id]
        visited: set[int] = set()
        while to_visit:
            current_id = to_visit.pop()
            if current_id == challenge_id:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="선행 문제 조건에 순환 참조를 만들 수 없습니다.",
                )
            if current_id in visited:
                continue
            visited.add(current_id)
            to_visit.extend(adjacency.get(current_id, set()) - visited)


def _validate_competition_mandatory_prerequisites(
    *,
    challenge_type: str,
    prerequisite_ids: list[int],
    learning_prerequisites: list[object],
) -> None:
    """Keep CTF access scoped exclusively to its competition contract.

    A competition challenge can be assigned to different competitions at
    different times. Mandatory challenge or learning prerequisites therefore
    cannot be explained or enforced safely through the generic wargame tables:
    an unassigned prerequisite would permanently lock an otherwise assigned
    challenge. Informational recommendations remain allowed.
    """
    if challenge_type != "competition":
        return
    if prerequisite_ids or learning_prerequisites:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="CTF 문제에는 강제 선행 문제 또는 선행 학습 조건을 지정할 수 없습니다.",
        )


async def _get_prerequisite_details(
    prereq_ids: list[int],
    solved_ids: set[int],
    db: AsyncSession,
) -> list[dict]:
    """챌린지 선행조건의 이름+풀이여부 상세 반환."""
    if not prereq_ids:
        return []
    result = await db.execute(
        select(Challenge.id, Challenge.name).where(
            Challenge.id.in_(prereq_ids),
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
    )
    return [
        {"id": row[0], "name": row[1], "solved": row[0] in solved_ids}
        for row in result.all()
    ]


async def _get_challenge_tags(
    challenge_ids: list[int],
    db: AsyncSession,
) -> dict[int, list[str]]:
    """챌린지 ID → 태그 slug 목록 맵."""
    if not challenge_ids:
        return {}
    result = await db.execute(
        select(ChallengeKnowledgeTag.challenge_id, KnowledgeTag.slug)
        .join(KnowledgeTag, KnowledgeTag.id == ChallengeKnowledgeTag.tag_id)
        .where(
            ChallengeKnowledgeTag.challenge_id.in_(challenge_ids),
            public_tag_mapping_clause(ChallengeKnowledgeTag),
        )
    )
    tag_map: dict[int, list[str]] = defaultdict(list)
    for cid, slug in result.all():
        tag_map[cid].append(slug)
    return tag_map


async def _get_recommended_prerequisites(
    challenge_id: int,
    db: AsyncSession,
) -> list[dict]:
    """관리자 설정 추천 + 같은 태그 기반 자동 추천을 병합하여 반환."""
    result: list[dict] = []
    seen: set[str] = set()

    # 1) 관리자가 설정한 추천 선행조건
    rec_result = await db.execute(
        select(ChallengeRecommendedPrerequisite).where(
            ChallengeRecommendedPrerequisite.challenge_id == challenge_id
        )
    )
    for rec in rec_result.scalars().all():
        name = ""
        if rec.rec_type == "challenge":
            try:
                rec_challenge_id = int(rec.rec_target_id)
            except ValueError:
                continue
            ch = await db.execute(
                select(Challenge.name).where(
                    Challenge.id == rec_challenge_id,
                    Challenge.is_published.is_(True),
                    Challenge.challenge_type == "wargame",
                )
            )
            row = ch.first()
            if not row:
                continue
            name = row[0]
        elif rec.rec_type == "course":
            from uuid import UUID as _UUID
            try:
                co = await db.get(Course, _UUID(rec.rec_target_id))
            except ValueError:
                continue
            if not co or co.is_deleted or not co.is_published or co.status != ContentStatus.APPROVED.value:
                continue
            name = co.title
        elif rec.rec_type == "lesson":
            from uuid import UUID as _UUID
            try:
                lesson_id = _UUID(rec.rec_target_id)
            except ValueError:
                continue
            lesson_row = await db.execute(
                select(Lesson, Course)
                .join(Course, Course.id == Lesson.course_id)
                .where(Lesson.id == lesson_id)
            )
            row = lesson_row.first()
            if not row:
                continue
            ls, lesson_course = row
            if lesson_course.is_deleted or not lesson_course.is_published or lesson_course.status != ContentStatus.APPROVED.value:
                continue
            name = ls.title
        else:
            continue
        key = f"{rec.rec_type}-{rec.rec_target_id}"
        if key not in seen:
            item = {"type": rec.rec_type, "id": str(rec.rec_target_id), "name": name}
            if rec.rec_type == "lesson" and ls:
                item["course_id"] = str(ls.course_id)
            result.append(item)
            seen.add(key)

    # 2) 같은 태그 기반 자동 추천 (코스만)
    tag_result = await db.execute(
        select(ChallengeKnowledgeTag.tag_id).where(
            ChallengeKnowledgeTag.challenge_id == challenge_id,
            public_tag_mapping_clause(ChallengeKnowledgeTag),
        )
    )
    tag_ids = [r[0] for r in tag_result.all()]
    if tag_ids:
        existing_lp = await db.execute(
            select(ChallengeLearningPrerequisite.prerequisite_target_id).where(
                ChallengeLearningPrerequisite.challenge_id == challenge_id,
                ChallengeLearningPrerequisite.prerequisite_type == "course",
            )
        )
        existing_ids = {r[0] for r in existing_lp.all()}

        course_result = await db.execute(
            select(Course.id, Course.title)
            .join(CourseKnowledgeTag, CourseKnowledgeTag.course_id == Course.id)
            .where(
                CourseKnowledgeTag.tag_id.in_(tag_ids),
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
                public_tag_mapping_clause(CourseKnowledgeTag),
            )
            .distinct()
        )
        for cid, cname in course_result.all():
            key = f"course-{cid}"
            if cid not in existing_ids and key not in seen:
                result.append({"type": "course", "id": str(cid), "name": cname})
                seen.add(key)

    return result


def _learning_path_step(
    *,
    entity_type: str,
    entity_id: str,
    name: str,
    status_value: str,
    href: str,
    course_id: str | None = None,
    completed_units: int | None = None,
    total_units: int | None = None,
) -> dict:
    """Create the stable public step shape shared by path response sections."""
    step: dict = {
        "type": entity_type,
        "id": entity_id,
        "name": name,
        "status": status_value,
        "href": href,
        "graph_focus": {
            "focus_type": entity_type,
            "focus_id": entity_id,
        },
    }
    if course_id:
        step["course_id"] = course_id
    if completed_units is not None:
        step["completed_units"] = completed_units
    if total_units is not None:
        step["total_units"] = total_units
    return step


async def _resolve_public_learning_path_step(
    db: AsyncSession,
    *,
    entity_type: str,
    entity_id: str,
    user_id: UUID | None,
) -> dict | None:
    """Resolve one public content item and attach only the caller's progress.

    This is intentionally used after a public visibility check, so a caller
    never receives a title, source ID, or link for deleted, unpublished, or
    competition-only content.  ``None`` is therefore a safe signal for a
    legacy unavailable prerequisite or a stale recommendation.
    """
    if entity_type == "challenge":
        try:
            challenge_id = int(entity_id)
        except (TypeError, ValueError):
            return None
        if challenge_id <= 0:
            return None
        challenge = (
            await db.execute(
                select(Challenge).where(
                    Challenge.id == challenge_id,
                    Challenge.is_published.is_(True),
                    Challenge.challenge_type == "wargame",
                )
            )
        ).scalar_one_or_none()
        if not challenge:
            return None
        solved = False
        if user_id:
            solved = bool(
                await db.scalar(
                    select(func.count()).select_from(Submission).where(
                        Submission.user_id == user_id,
                        Submission.challenge_id == challenge.id,
                        Submission.is_correct.is_(True),
                        Submission.competition_id.is_(None),
                    )
                )
            )
        return _learning_path_step(
            entity_type="challenge",
            entity_id=str(challenge.id),
            name=challenge.name,
            status_value="unknown" if not user_id else ("completed" if solved else "not_started"),
            href=f"/wargame/{challenge.id}",
        )

    if entity_type == "course":
        try:
            course_id = UUID(str(entity_id))
        except (TypeError, ValueError):
            return None
        course = await db.get(Course, course_id)
        if not (
            course
            and not course.is_deleted
            and course.is_published
            and course.status == ContentStatus.APPROVED.value
        ):
            return None
        total_units = int(
            (await db.scalar(select(func.count(Lesson.id)).where(Lesson.course_id == course.id)))
            or 0
        )
        if total_units == 0:
            return None
        completed_units = 0
        if user_id:
            completed_units = int(
                (
                    await db.scalar(
                        select(func.count(UserProgress.id))
                        .join(Lesson, Lesson.id == UserProgress.lesson_id)
                        .where(
                            Lesson.course_id == course.id,
                            UserProgress.user_id == user_id,
                            UserProgress.completed.is_(True),
                        )
                    )
                )
                or 0
            )
        status_value = "unknown"
        if user_id:
            status_value = (
                "completed"
                if completed_units >= total_units
                else ("in_progress" if completed_units else "not_started")
            )
        return _learning_path_step(
            entity_type="course",
            entity_id=str(course.id),
            name=course.title,
            status_value=status_value,
            href=f"/learn/{course.id}",
            completed_units=completed_units if user_id else None,
            total_units=total_units,
        )

    if entity_type == "lesson":
        try:
            lesson_id = UUID(str(entity_id))
        except (TypeError, ValueError):
            return None
        lesson_row = (
            await db.execute(
                select(Lesson, Course)
                .join(Course, Course.id == Lesson.course_id)
                .where(Lesson.id == lesson_id)
            )
        ).first()
        if not lesson_row:
            return None
        lesson, course = lesson_row
        if not (
            not course.is_deleted
            and course.is_published
            and course.status == ContentStatus.APPROVED.value
        ):
            return None
        completed = False
        if user_id:
            completed = bool(
                await db.scalar(
                    select(func.count(UserProgress.id)).where(
                        UserProgress.user_id == user_id,
                        UserProgress.lesson_id == lesson.id,
                        UserProgress.completed.is_(True),
                    )
                )
            )
        return _learning_path_step(
            entity_type="lesson",
            entity_id=str(lesson.id),
            name=lesson.title,
            status_value="unknown" if not user_id else ("completed" if completed else "not_started"),
            href=f"/learn/{course.id}/{lesson.id}",
            course_id=str(course.id),
            completed_units=1 if completed else 0 if user_id else None,
            total_units=1,
        )

    if entity_type not in {"knowledge_tag", "hint"}:
        return None
    content = await resolve_content(db, entity_type, entity_id, public_only=True)
    if not content or not content.href:
        return None
    return _learning_path_step(
        entity_type=content.entity_type,
        entity_id=content.entity_id,
        name=content.display_name,
        status_value="unknown",
        href=content.href,
        course_id=content.course_id,
    )


def _unavailable_learning_path_step(
    *,
    position: int,
    target_challenge_id: int,
) -> dict:
    """Represent a stale mandatory reference without disclosing its source."""
    return {
        "type": "challenge",
        "id": f"unavailable-{position}",
        "name": "사용할 수 없는 선행 콘텐츠",
        "status": "unavailable",
        "href": "/inquiry",
        "graph_focus": {
            "focus_type": "challenge",
            "focus_id": str(target_challenge_id),
        },
    }


async def _build_challenge_learning_path(
    db: AsyncSession,
    *,
    challenge: Challenge,
    user_id: UUID | None,
) -> dict:
    """Project the existing prerequisite source tables into a public path."""
    required_steps: list[dict] = []

    challenge_prerequisites = (
        await db.execute(
            select(ChallengePrerequisite.prerequisite_id)
            .where(ChallengePrerequisite.challenge_id == challenge.id)
            .order_by(ChallengePrerequisite.prerequisite_id)
        )
    ).scalars().all()
    for prerequisite_id in challenge_prerequisites:
        step = await _resolve_public_learning_path_step(
            db,
            entity_type="challenge",
            entity_id=str(prerequisite_id),
            user_id=user_id,
        )
        # Existing access checks ignore archived, unpublished, or otherwise
        # unavailable legacy rows so they cannot permanently lock a learner.
        # The explanation endpoint must use that exact policy instead of
        # inventing an unavailable step that disagrees with submit/detail.
        if step is not None:
            required_steps.append(step)

    learning_prerequisites = (
        await db.execute(
            select(ChallengeLearningPrerequisite)
            .where(ChallengeLearningPrerequisite.challenge_id == challenge.id)
            .order_by(
                ChallengeLearningPrerequisite.prerequisite_type,
                ChallengeLearningPrerequisite.prerequisite_target_id,
            )
        )
    ).scalars().all()
    for prerequisite in learning_prerequisites:
        step = await _resolve_public_learning_path_step(
            db,
            entity_type=prerequisite.prerequisite_type,
            entity_id=str(prerequisite.prerequisite_target_id),
            user_id=user_id,
        )
        # Match _check_learning_prerequisites(): stale legacy learning rows
        # are an operational cleanup concern, not a user-facing hard lock.
        if step is not None and _legacy_learning_prerequisite_is_enforceable(
            prerequisite.prerequisite_type,
            target_is_public=True,
            course_has_lessons=(
                bool(step.get("total_units", 0))
                if prerequisite.prerequisite_type == "course"
                else None
            ),
        ):
            required_steps.append(step)

    required_keys = {(step["type"], step["id"]) for step in required_steps}
    recommended_steps: list[dict] = []
    recommendation_keys: set[tuple[str, str]] = set()
    for recommendation in await _get_recommended_prerequisites(challenge.id, db):
        entity_type = str(recommendation.get("type", ""))
        entity_id = str(recommendation.get("id", ""))
        key = (entity_type, entity_id)
        if key in required_keys or key in recommendation_keys:
            continue
        step = await _resolve_public_learning_path_step(
            db,
            entity_type=entity_type,
            entity_id=entity_id,
            user_id=user_id,
        )
        if step is not None:
            recommended_steps.append(step)
            recommendation_keys.add(key)

    helpful_content: list[dict] = []
    helpful_keys: set[tuple[str, str, str]] = set()
    for content in await get_approved_helpful_content(challenge.id, db):
        entity_type = str(content.get("type", ""))
        entity_id = str(content.get("id", ""))
        relation_type = str(content.get("relation_type", ""))
        if relation_type not in {"prepares_for", "recommends", "helps"}:
            continue
        key = (entity_type, entity_id, relation_type)
        if key in helpful_keys:
            continue
        step = await _resolve_public_learning_path_step(
            db,
            entity_type=entity_type,
            entity_id=entity_id,
            user_id=user_id,
        )
        if step is None:
            continue
        helpful_content.append(
            {
                "type": step["type"],
                "id": step["id"],
                "name": step["name"],
                "status": step["status"],
                "href": step["href"],
                "graph_focus": step["graph_focus"],
                "course_id": step.get("course_id"),
                "relation_type": relation_type,
                "strength": float(content["strength"]),
                "confidence": float(content["confidence"]),
                "evidence_summary": public_evidence_summary(
                    content.get("evidence_summary")
                ),
                "origin": str(content["origin"]),
            }
        )
        helpful_keys.add(key)

    return build_challenge_learning_path_projection(
        target={
            "type": "challenge",
            "id": str(challenge.id),
            "name": challenge.name,
            "href": f"/wargame/{challenge.id}",
        },
        is_authenticated=user_id is not None,
        required_steps=required_steps,
        recommended_steps=recommended_steps,
        helpful_content=helpful_content,
    )


router = APIRouter(tags=["챌린지"])


def _escape_like(value: str) -> str:
    """LIKE 패턴에서 특수 와일드카드 문자를 이스케이프한다."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _localized(i18n_dict: dict | None, fallback: str, lang: str) -> str:
    """i18n dict에서 lang에 해당하는 값을 꺼내고, 없으면 fallback 반환."""
    if i18n_dict and lang in i18n_dict:
        return i18n_dict[lang]
    return fallback


async def _run_auto_translate(challenge_id: int, name: str, description: str) -> None:
    """백그라운드에서 자동 번역을 실행하고 DB에 저장."""
    try:
        name_i18n = await auto_translate(name, source_lang="ko")
        desc_i18n = await auto_translate_rich_content(
            description,
            source_lang="ko",
            max_visible_chars=10000,
        )

        async with async_session() as session:
            result = await session.execute(
                select(Challenge).where(Challenge.id == challenge_id)
            )
            ch = result.scalar_one_or_none()
            if ch and ch.name == name and ch.description == description:
                ch.name_i18n = name_i18n
                ch.description_i18n = desc_i18n
                await session.commit()
                logger.info("자동 번역 완료: challenge #%d", challenge_id)
    except Exception as error:
        logger.error("자동 번역 실패: challenge #%d — %s", challenge_id, type(error).__name__)


# ──── Export / Import (고정 경로 → {challenge_id} 매칭보다 먼저 등록) ────


@router.get("/challenges/export")
async def export_challenges(
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin")),
):
    """전체 챌린지를 원본 ID가 포함된 JSON으로 내보내기 (관리자 전용)."""
    return await export_challenge_batch(db)


def _validate_import_dynamic_policy(policy: DynamicFlagPolicyInput) -> None:
    _require_dynamic_flag_feature()
    _resolve_dynamic_policy_key_version(policy.hmac_key_version)


@router.post("/challenges/import/v2", status_code=status.HTTP_201_CREATED)
@router.post("/challenges/import", status_code=status.HTTP_201_CREATED)
async def import_challenges(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin")),
):
    """기존 식별자와 사용자 이력을 보존하며 JSON 문제 파일을 반영한다."""
    return await import_challenge_batch(
        db, body, actor_id=actor_id,
        validate_dynamic_policy=_validate_import_dynamic_policy,
        invalidate_environment=_invalidate_environment_verification,
        revoke_active_issuances=_revoke_active_dynamic_issuances_for_stale_environment,
    )


# ──── 목록 조회 ────

@router.get("/challenges", response_model=ChallengeListResponse)
async def list_challenges(
    category: str = Query("", description="카테고리 필터"),
    difficulty: str = Query("", description="난이도 필터"),
    challenge_type: str = Query("", description="문제 유형 필터 (wargame, competition)"),
    search: str = Query("", description="제목 검색어"),
    sort: str = Query("newest", description="정렬 기준 (newest, solves, points)"),
    show_all: bool = Query(False, description="관리자용: 비공개 포함"),
    lang: str = Query("ko", description="응답 언어 (ko, en, ja, zh)"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    role: str | None = Depends(get_optional_user_role),
    user_id: UUID | None = Depends(get_optional_user_id),
):
    """챌린지 목록 조회 (카테고리/난이도 필터, 검색, 페이지네이션)."""
    query = select(Challenge)
    # 관리자/모더레이터가 show_all=true 요청 시 비공개 포함
    is_admin = role in ("admin", "moderator")
    if not (show_all and is_admin):
        query = query.where(Challenge.is_published.is_(True))

    # Public learning listings never expose competition rows. Competition
    # visibility is governed by the Auth assignment/schedule path, even when
    # a caller tries to request ``challenge_type=competition`` directly.
    if not is_admin:
        query = query.where(Challenge.challenge_type == "wargame")
    elif challenge_type in ("wargame", "competition"):
        query = query.where(Challenge.challenge_type == challenge_type)

    if category:
        query = query.where(Challenge.category == category)
    if difficulty:
        query = query.where(Challenge.difficulty == difficulty)
    if search:
        safe_search = _escape_like(search)
        query = query.where(Challenge.name.ilike(f"%{safe_search}%", escape="\\"))

    # 정렬
    if sort == "solves":
        query = query.order_by(Challenge.solves.desc(), Challenge.id.desc())
    elif sort == "points":
        query = query.order_by(Challenge.value.desc(), Challenge.id.desc())
    else:  # newest
        query = query.order_by(Challenge.created_at.desc(), Challenge.id.desc())

    # 전체 수
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    # 페이지네이션
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    challenges = result.scalars().all()

    # 선행조건 맵 구축: {challenge_id: [prerequisite_id, ...]}
    challenge_ids = [c.id for c in challenges]
    prereq_map: dict[int, list[int]] = {cid: [] for cid in challenge_ids}
    if challenge_ids:
        prereq_result = await db.execute(
            select(ChallengePrerequisite)
            .join(Challenge, Challenge.id == ChallengePrerequisite.prerequisite_id)
            .where(
                ChallengePrerequisite.challenge_id.in_(challenge_ids),
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            )
        )
        for p in prereq_result.scalars().all():
            prereq_map[p.challenge_id].append(p.prerequisite_id)

    # 학습 선행조건 배치 조회
    learning_prereq_map: dict[int, list] = {cid: [] for cid in challenge_ids}
    if challenge_ids:
        lp_result = await db.execute(
            select(ChallengeLearningPrerequisite).where(
                ChallengeLearningPrerequisite.challenge_id.in_(challenge_ids)
            )
        )
        for lp in lp_result.scalars().all():
            learning_prereq_map[lp.challenge_id].append(lp)

    # 지식 태그 배치 조회
    tag_map = await _get_challenge_tags(challenge_ids, db)

    # 유저 풀이 상태 — 선행조건 충족 여부 + solved 표시용 (워게임 전용: competition_id IS NULL)
    solved_ids: set[int] = set()
    if user_id:
        solved_result = await db.execute(
            select(Submission.challenge_id).where(
                Submission.user_id == user_id,
                Submission.is_correct.is_(True),
                Submission.competition_id.is_(None),
            )
        )
        solved_ids = {row[0] for row in solved_result.all()}

    # 학습 선행조건 완료 여부 판단용 배치 조회
    all_lessons: list = []
    available_course_ids: set = set()
    available_lesson_ids: set = set()
    completed_lesson_ids: set = set()
    lp_target_ids: set = set()
    for lps in learning_prereq_map.values():
        for lp in lps:
            lp_target_ids.add(lp.prerequisite_target_id)
    if lp_target_ids:
        all_lessons = (await db.execute(
            select(Lesson)
            .join(Course, Course.id == Lesson.course_id)
            .where(
                Lesson.course_id.in_(lp_target_ids) | Lesson.id.in_(lp_target_ids),
                Course.is_deleted.is_(False),
                Course.is_published.is_(True),
                Course.status == ContentStatus.APPROVED.value,
            )
        )).scalars().all()
        available_course_ids = {lesson.course_id for lesson in all_lessons}
        available_lesson_ids = {lesson.id for lesson in all_lessons}
        if user_id:
            prog_result = await db.execute(
                select(UserProgress.lesson_id).where(
                    UserProgress.user_id == user_id,
                    UserProgress.completed.is_(True),
                    UserProgress.lesson_id.in_(available_lesson_ids),
                )
            )
            completed_lesson_ids = {r[0] for r in prog_result.all()}

    items = [
        ChallengeListItem(
            id=c.id,
            name=_localized(c.name_i18n, c.name, lang),
            category=c.category,
            difficulty=c.difficulty,
            value=c.value,
            xp=c.xp,
            solves=c.solves,
            tags=c.tags or [],
            author=c.author_name,
            challenge_type=c.challenge_type,
            env_type=c.env_type or "none",
            is_published=c.is_published,
        )
        for c in challenges
    ]

    # 선행조건 + 잠금 상태 + solved 표시 추가
    items_with_prereqs = []
    for item in items:
        prereqs = prereq_map.get(item.id, [])
        if user_id:
            challenge_locked = bool(prereqs) and not all(pid in solved_ids for pid in prereqs)
        else:
            challenge_locked = bool(prereqs)
        lp_rows = learning_prereq_map.get(item.id, [])
        # 삭제·비공개·미승인 콘텐츠를 가리키는 과거 조건은 목록에서도 잠금을 만들지
        # 않는다. 상세·제출 경로와 같은 공개성 기준을 사용해야 목록과 실제 접근이
        # 서로 다르게 보이지 않는다.
        active_lp_rows = [
            lp
            for lp in lp_rows
            if _legacy_learning_prerequisite_is_enforceable(
                lp.prerequisite_type,
                target_is_public=(
                    lp.prerequisite_target_id in available_course_ids
                    if lp.prerequisite_type == "course"
                    else lp.prerequisite_target_id in available_lesson_ids
                ),
                course_has_lessons=(
                    lp.prerequisite_target_id in available_course_ids
                    if lp.prerequisite_type == "course"
                    else None
                ),
            )
        ]
        learning_locked = False
        if active_lp_rows and not user_id:
            learning_locked = True
        elif active_lp_rows and user_id:
            for lp in active_lp_rows:
                if lp.prerequisite_type == "course":
                    course_id = lp.prerequisite_target_id
                    co_lessons = [ls for ls in all_lessons if ls.course_id == course_id]
                    # 공통 정책상 legacy 빈 코스는 이미 제외되어 있다.
                    # 이 분기에는 공개되고 완료 가능한 코스만 들어온다.
                    if not co_lessons or not all(ls.id in completed_lesson_ids for ls in co_lessons):
                        learning_locked = True
                        break
                elif lp.prerequisite_type == "lesson":
                    if lp.prerequisite_target_id not in completed_lesson_ids:
                        learning_locked = True
                        break
        locked = challenge_locked or learning_locked
        d = item.model_dump()
        d["prerequisite_ids"] = prereqs
        d["locked"] = locked
        d["solved"] = item.id in solved_ids
        d["knowledge_tags"] = tag_map.get(item.id, [])
        d["prerequisite_details"] = await _get_prerequisite_details(prereqs, solved_ids, db)
        items_with_prereqs.append(d)

    return {"items": items_with_prereqs, "total": total, "page": page, "size": size}


# ──── 상세 조회 ────

@router.get("/admin/challenges/solve-metrics")
async def get_admin_challenge_solve_metrics(
    submission_context: str = Query("all", description="all / wargame / ctf"),
    challenge_type: str = Query("all", description="all / wargame / competition"),
    challenge_id: int | None = Query(None, description="문제 ID 정확 일치"),
    challenge_search: str | None = Query(None, max_length=200, description="문제명 부분 검색"),
    competition_id: str | None = Query(None, max_length=36, description="대회 ID 정확 일치"),
    submitted_from: SubmissionAuditDateTime | None = Query(None, description="시간대 포함 ISO 8601 시작 시각"),
    submitted_to: SubmissionAuditDateTime | None = Query(None, description="시간대 포함 ISO 8601 종료 시각"),
    sort: str = Query(
        "latest_solved_at_desc",
        description="latest_solved_at_desc / solver_count_desc / solve_rate_desc / challenge_name_asc",
    ),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """관리자 전용 문제별 제출 집계를 반환한다.

    ``solve_rate``는 같은 필터 범위에서 정답을 맞힌 고유 사용자 수를 시도한 고유
    사용자 수로 나눈 값이다. Learn은 전체 계정 수를 소유하지 않으므로 플랫폼 전체
    참여율로 해석하지 않는다. ``latest_solved_at``에는 정답 행만 반영한다.
    """
    valid_contexts = {"all", "wargame", "ctf"}
    valid_challenge_types = {"all", "wargame", "competition"}
    valid_sorts = {
        "latest_solved_at_desc",
        "solver_count_desc",
        "solve_rate_desc",
        "challenge_name_asc",
    }
    if submission_context not in valid_contexts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="submission_context 값이 올바르지 않습니다.",
        )
    if challenge_type not in valid_challenge_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="challenge_type 값이 올바르지 않습니다.",
        )
    if sort not in valid_sorts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sort 값이 올바르지 않습니다.",
        )

    def normalize_time(value: datetime | None, field_name: str) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field_name}에는 시간대가 포함된 ISO 8601 시간이 필요합니다.",
            )
        return value.astimezone(timezone.utc)

    submitted_from = normalize_time(submitted_from, "submitted_from")
    submitted_to = normalize_time(submitted_to, "submitted_to")
    if submitted_from and submitted_to and submitted_from > submitted_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="submitted_from은 submitted_to보다 늦을 수 없습니다.",
        )

    submission_filters = []
    if submission_context == "wargame":
        submission_filters.append(Submission.competition_id.is_(None))
    elif submission_context == "ctf":
        submission_filters.append(Submission.competition_id.is_not(None))
    if competition_id:
        normalized_competition_id = competition_id.strip()
        if not normalized_competition_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="competition_id는 공백만 사용할 수 없습니다.",
            )
        submission_filters.append(Submission.competition_id == normalized_competition_id)
    if submitted_from:
        submission_filters.append(Submission.submitted_at >= submitted_from)
    if submitted_to:
        submission_filters.append(Submission.submitted_at <= submitted_to)

    aggregate_query = select(
        Submission.challenge_id.label("challenge_id"),
        func.count(func.distinct(Submission.user_id)).label("attempting_user_count"),
        func.count(func.distinct(Submission.user_id))
        .filter(Submission.is_correct.is_(True))
        .label("solver_count"),
        func.max(Submission.submitted_at)
        .filter(Submission.is_correct.is_(True))
        .label("latest_solved_at"),
    )
    if submission_filters:
        aggregate_query = aggregate_query.where(*submission_filters)
    submission_metrics = aggregate_query.group_by(Submission.challenge_id).subquery()

    challenge_filters = []
    if challenge_id is not None:
        challenge_filters.append(Challenge.id == challenge_id)
    if challenge_type != "all":
        challenge_filters.append(Challenge.challenge_type == challenge_type)
    if challenge_search is not None:
        normalized_challenge_search = challenge_search.strip()
        if not normalized_challenge_search:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="challenge_search는 공백만 사용할 수 없습니다.",
            )
        challenge_filters.append(
            Challenge.name.ilike(
                f"%{_escape_like(normalized_challenge_search)}%",
                escape="\\",
            )
        )

    # Context, competition, and time filters describe a submission population.
    # When any is active, rows and total must cover only challenges represented
    # by that same population instead of reintroducing unrelated zero rows via
    # the outer join.
    if submission_filters:
        challenge_filters.append(
            Challenge.id.in_(select(submission_metrics.c.challenge_id))
        )

    total_query = select(func.count()).select_from(Challenge)
    if challenge_filters:
        total_query = total_query.where(*challenge_filters)
    total = (await db.execute(total_query)).scalar() or 0

    solver_count = func.coalesce(submission_metrics.c.solver_count, 0)
    attempting_user_count = func.coalesce(submission_metrics.c.attempting_user_count, 0)
    solve_rate_order = (
        solver_count * 100.0 / func.nullif(attempting_user_count, 0)
    ).desc().nullslast()
    sort_orders = {
        "latest_solved_at_desc": (
            submission_metrics.c.latest_solved_at.desc().nullslast(),
            Challenge.id.asc(),
        ),
        "solver_count_desc": (solver_count.desc(), Challenge.id.asc()),
        "solve_rate_desc": (solve_rate_order, Challenge.id.asc()),
        "challenge_name_asc": (Challenge.name.asc(), Challenge.id.asc()),
    }

    query = (
        select(
            Challenge.id.label("challenge_id"),
            Challenge.name.label("challenge_name"),
            Challenge.challenge_type,
            Challenge.is_published,
            attempting_user_count.label("attempting_user_count"),
            solver_count.label("solver_count"),
            submission_metrics.c.latest_solved_at,
        )
        .outerjoin(
            submission_metrics,
            submission_metrics.c.challenge_id == Challenge.id,
        )
    )
    if challenge_filters:
        query = query.where(*challenge_filters)
    rows = (
        await db.execute(
            query.order_by(*sort_orders[sort]).offset((page - 1) * size).limit(size)
        )
    ).all()

    items = []
    for row in rows:
        attempts = int(row.attempting_user_count or 0)
        solvers = int(row.solver_count or 0)
        items.append(
            {
                "challenge_id": row.challenge_id,
                "challenge_name": row.challenge_name,
                "challenge_type": row.challenge_type,
                "is_published": row.is_published,
                "attempting_user_count": attempts,
                "solver_count": solvers,
                "solve_rate": round((solvers / attempts) * 100, 2) if attempts else 0.0,
                "latest_solved_at": (
                    row.latest_solved_at.isoformat() if row.latest_solved_at else None
                ),
            }
        )

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "submission_context": submission_context,
    }


@router.get(
    "/learning-path/challenges/{challenge_id}",
    response_model=LearningPathChallengeResponse,
)
async def get_challenge_learning_path(
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: UUID | None = Depends(get_optional_user_id),
):
    """Explain a public wargame challenge's authoritative learning path.

    This endpoint deliberately does not use administrator visibility overrides
    and does not accept a competition context. CTF access stays governed by
    the existing assignment and schedule checks, while this path remains a
    safe explanation for public learning content only.
    """
    challenge = (
        await db.execute(
            select(Challenge).where(
                Challenge.id == challenge_id,
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            )
        )
    ).scalar_one_or_none()
    if not challenge:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="챌린지를 찾을 수 없습니다.",
        )
    return await _build_challenge_learning_path(
        db,
        challenge=challenge,
        user_id=user_id,
    )


@router.get("/challenges/{challenge_id}")
async def get_challenge(
    challenge_id: int,
    lang: str = Query("ko", description="응답 언어 (ko, en, ja, zh)"),
    db: AsyncSession = Depends(get_db),
    role: str | None = Depends(get_optional_user_role),
    user_id: UUID | None = Depends(get_optional_user_id),
):
    """챌린지 상세 조회 — 관리자/모더레이터는 비공개 문제도 조회 가능."""
    is_admin = role in ("admin", "moderator")

    query = (
        select(Challenge)
        .options(
            selectinload(Challenge.hints),
            selectinload(Challenge.flags),
            selectinload(Challenge.dynamic_flag_policy),
        )
        .where(Challenge.id == challenge_id)
    )
    if not is_admin:
        query = query.where(Challenge.is_published.is_(True))

    result = await db.execute(query)
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="챌린지를 찾을 수 없습니다.",
        )

    # 관리자: 힌트 내용 포함, 일반 사용자: id+cost만
    # CTF 문제는 일반 워게임 상세 URL로 직접 열람할 수 없다. 대회 참가·배정 확인은
    # Auth 서비스의 CTF 접근 계약을 통해서만 수행한다.
    if challenge.challenge_type == "competition" and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="챌린지를 찾을 수 없습니다.",
        )

    if is_admin:
        hints_data = [{"id": h.id, "content": h.content, "cost": h.cost} for h in challenge.hints]
    else:
        hints_data = [{"id": h.id, "cost": h.cost} for h in challenge.hints]

    response: dict = {
        "id": challenge.id,
        "name": _localized(challenge.name_i18n, challenge.name, lang),
        "category": challenge.category,
        "difficulty": challenge.difficulty,
        "value": challenge.value,
        "solves": challenge.solves,
        "tags": challenge.tags or [],
        "author": challenge.author_name,
        "description": _localized(challenge.description_i18n, challenge.description, lang),
        "files": challenge.files or [],
        "attachment_revision": challenge.attachment_revision,
        "hints": hints_data,
        "max_attempts": challenge.max_attempts,
        "env_type": challenge.env_type,
        "container_port": challenge.container_port,
        "challenge_type": challenge.challenge_type,
        "environment_ready": _environment_is_ready(challenge),
        "environment_status": _environment_status(challenge),
    }

    # Direct challenge detail is the authoritative re-entry path for wargame
    # completion. CTF solves stay scoped to their competition and never count
    # as global challenge progress.
    solved = False
    if user_id is not None and challenge.challenge_type == "wargame":
        solved = (
            await db.execute(
                select(Submission.id)
                .where(
                    Submission.user_id == user_id,
                    Submission.challenge_id == challenge_id,
                    Submission.is_correct.is_(True),
                    Submission.competition_id.is_(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none() is not None
    response["solved"] = solved

    # 선행조건 조회
    prereq_result = await db.execute(
        select(ChallengePrerequisite.prerequisite_id)
        .join(Challenge, Challenge.id == ChallengePrerequisite.prerequisite_id)
        .where(
            ChallengePrerequisite.challenge_id == challenge_id,
            Challenge.is_published.is_(True),
            Challenge.challenge_type == "wargame",
        )
    )
    prereq_ids = [row[0] for row in prereq_result.all()]

    # 선행조건 충족 여부 (관리자는 제한 없음)
    locked = False
    solved_prereqs: set = set()
    if prereq_ids and not is_admin:
        if user_id:
            solved_result = await db.execute(
                select(Submission.challenge_id).where(
                    Submission.user_id == user_id,
                    Submission.is_correct.is_(True),
                    Submission.challenge_id.in_(prereq_ids),
                    Submission.competition_id.is_(None),
                )
            )
            solved_prereqs = {row[0] for row in solved_result.all()}
            locked = not all(pid in solved_prereqs for pid in prereq_ids)
        else:
            # 비로그인: 선행조건이 있으면 무조건 잠금
            locked = True

    # 학습 선행조건 검증
    learning_locked = False
    learning_details = []
    if not is_admin:
        learning_locked, learning_details = await _check_learning_prerequisites(
            challenge_id, user_id, db
        )
    locked = locked or learning_locked

    response["prerequisite_ids"] = prereq_ids
    response["locked"] = locked

    response["learning_prerequisites_detail"] = learning_details
    _solved = solved_prereqs if (prereq_ids and user_id and not is_admin) else set()
    response["prerequisite_details"] = await _get_prerequisite_details(prereq_ids, _solved, db)
    response["knowledge_tags"] = [
        r[0] for r in (await db.execute(
            select(KnowledgeTag.slug)
            .join(ChallengeKnowledgeTag, ChallengeKnowledgeTag.tag_id == KnowledgeTag.id)
            .where(
                ChallengeKnowledgeTag.challenge_id == challenge_id,
                public_tag_mapping_clause(ChallengeKnowledgeTag),
            )
        )).all()
    ]
    response["recommended_prerequisites"] = await _get_recommended_prerequisites(
        challenge_id, db
    )
    response["helpful_content"] = await get_approved_helpful_content(challenge_id, db)

    # 잠금 상태면 설명/파일/힌트 숨김 (관리자 제외)
    if locked and not is_admin:
        response["description"] = ""
        response["files"] = []
        response["hints"] = []

    # 관리자에게만 추가 필드 제공
    if is_admin:
        response["xp"] = challenge.xp
        response["flag_type"] = challenge.flag_type
        # dynamic_instance의 실제 정답은 DB에 존재하지 않는다. 기존 관리자 API
        # 호환을 위해 빈 문자열과 안전한 policy metadata만 반환한다.
        response["flag_value"] = "" if challenge.flag_type == DYNAMIC_FLAG_TYPE else challenge.flag_value
        response["flags"] = [
            {
                "id": f.id,
                "type": f.type,
                "value": f.value,
                "case_sensitive": f.case_sensitive,
            }
            for f in challenge.flags if challenge.flag_type != DYNAMIC_FLAG_TYPE
        ]
        if challenge.flag_type == DYNAMIC_FLAG_TYPE:
            policy = challenge.dynamic_flag_policy
            response["dynamic_flag"] = (
                {
                    "enabled": policy.enabled,
                    "delivery": policy.delivery,
                    "runtime_path": policy.runtime_path,
                    "ttl_seconds": policy.ttl_seconds,
                    "policy_revision": policy.policy_revision,
                    "hmac_key_version": policy.hmac_key_version,
                }
                if policy is not None else None
            )
        response["env_type"] = challenge.env_type
        response["dockerfile_content"] = challenge.dockerfile_content
        response["docker_image"] = challenge.docker_image
        response["environment_revision"] = challenge.environment_revision
        response["environment_verified_revision"] = challenge.environment_verified_revision
        response["environment_verified_at"] = (
            challenge.environment_verified_at.isoformat()
            if challenge.environment_verified_at else None
        )
        response["environment_last_checked_at"] = (
            challenge.environment_last_checked_at.isoformat()
            if challenge.environment_last_checked_at else None
        )
        response["environment_last_failure_code"] = challenge.environment_last_failure_code
        response["is_published"] = challenge.is_published
        response["knowledge_tag_ids"] = [
            r[0] for r in (await db.execute(
                select(ChallengeKnowledgeTag.tag_id)
                .where(ChallengeKnowledgeTag.challenge_id == challenge_id)
            )).all()
        ]
        lp_raw = await db.execute(
            select(ChallengeLearningPrerequisite).where(
                ChallengeLearningPrerequisite.challenge_id == challenge_id
            )
        )
        response["learning_prerequisites_raw"] = [
            {"type": lp.prerequisite_type, "target_id": str(lp.prerequisite_target_id)}
            for lp in lp_raw.scalars().all()
        ]

        rec_raw = await db.execute(
            select(ChallengeRecommendedPrerequisite).where(
                ChallengeRecommendedPrerequisite.challenge_id == challenge_id
            )
        )
        rec_challenges = []
        rec_learning = []
        for rec in rec_raw.scalars().all():
            if rec.rec_type == "challenge":
                rec_challenges.append(int(rec.rec_target_id))
            else:
                rec_learning.append({"type": rec.rec_type, "target_id": rec.rec_target_id})
        response["recommended_challenges"] = rec_challenges
        response["recommended_learning_raw"] = rec_learning

    response["attachments"] = [] if locked else [item.model_dump(mode="json") for item in await attachment_descriptors(db, challenge)]
    return response


@router.get("/competitions/{competition_id}/challenges/{challenge_id}")
async def get_competition_challenge(
    competition_id: str,
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """배정된 대회 참가자 전용 문제 상세.

    일반 워게임 상세와 분리해 설명·첨부파일·힌트·실습 환경 메타데이터를 하나의 대회
    컨텍스트에서 반환한다. 첨부파일 다운로드와 힌트 해금도 같은 competition_id를 받아
    각각 권한을 다시 확인한다.
    """
    result = await db.execute(
        select(Challenge)
        .options(selectinload(Challenge.hints))
        .where(Challenge.id == challenge_id, Challenge.is_published.is_(True))
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="대회 문제를 찾을 수 없습니다.")

    normalized_competition_id = await _require_competition_context(
        challenge=challenge,
        competition_id=competition_id,
        user_id=user_id,
        purpose="view",
    )
    attachments = [item.model_dump(mode="json") for item in await attachment_descriptors(
        db, challenge, competition_id=normalized_competition_id)]
    return {
        "id": challenge.id,
        "competition_id": normalized_competition_id,
        "attachments": attachments,
        "attachment_revision": challenge.attachment_revision,
        "name": challenge.name,
        "description": challenge.description or "",
        "files": [
            {
                "id": index,
                "name": object_path.rsplit("/", 1)[-1],
                "download_path": (
                    f"/api/learn/competitions/{normalized_competition_id}/"
                    f"challenges/{challenge.id}/files/{index}"
                ),
            }
            for index, object_path in enumerate(challenge.files or [])
            if isinstance(object_path, str) and "/" in object_path
        ],
        "hints": [{"id": hint.id, "cost": hint.cost} for hint in challenge.hints],
        "env_type": challenge.env_type or "none",
        "container_port": challenge.container_port,
        "environment_ready": _environment_is_ready(challenge),
        "environment_status": _environment_status(challenge),
    }


@router.get("/competitions/{competition_id}/challenges/{challenge_id}/files/{file_index}")
async def download_competition_challenge_file(
    competition_id: str,
    challenge_id: int,
    file_index: int,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """대회 배정·진행 중 상태를 다시 확인한 뒤 첨부파일을 스트리밍한다."""
    if file_index < 0:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")

    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id, Challenge.is_published.is_(True))
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="첨부파일을 찾을 수 없습니다.")
    await _require_competition_context(
        challenge=challenge,
        competition_id=competition_id,
        user_id=user_id,
        purpose="view",
    )

    return await resolve_attachment(db, challenge, file_index=file_index)


# ──── 플래그 제출 ────

@router.post("/challenges/{challenge_id}/submit", response_model=SubmitResponse)
async def submit_flag(
    challenge_id: int,
    body: FlagSubmit,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """플래그 제출."""
    # 챌린지 조회 (공개된 챌린지만 제출 허용)
    result = await db.execute(
        select(Challenge).where(
            Challenge.id == challenge_id,
            Challenge.is_published.is_(True),
        )
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    # dynamic_instance의 policy/environment 변경은 비공개 상태에서만 허용된다.
    # 동적 제출은 문제 row를 잠가 해당 변경과 직렬화하고, policy revision/image
    # binding을 읽는 동안 공개 상태가 바뀌어 이전 issuance가 소비되는 경합을 줄인다.
    # 기존 static/regex 제출은 이 lock 경로를 쓰지 않아 기존 처리량 계약을 유지한다.
    if challenge.flag_type == DYNAMIC_FLAG_TYPE:
        dynamic_challenge_result = await db.execute(
            select(Challenge)
            .where(
                Challenge.id == challenge_id,
                Challenge.is_published.is_(True),
            )
            .with_for_update()
        )
        challenge = dynamic_challenge_result.scalar_one_or_none()
        if challenge is None:
            raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    is_competition = bool(body.competition_id)
    competition_id: str | None = None
    if is_competition:
        try:
            competition_id = str(UUID(body.competition_id or ""))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="올바른 대회 ID 형식이 아닙니다.")

        # 정답은 대회 종료 전에 Learn DB에 확정됐지만 Auth 서비스 일시 장애로
        # reward_pending인 경우가 있다. 이 경우에는 새 플래그를 검증하지 않고 저장된
        # submitted_at만 사용해 점수 기록을 재시도한다. Auth는 그 원래 시각, 참가자,
        # 배정, 대회 매핑을 다시 검증하므로 종료 후 새 제출을 허용하는 경로가 아니다.
        pending_result = await db.execute(
            select(Submission).where(
                Submission.user_id == user_id,
                Submission.challenge_id == challenge_id,
                Submission.competition_id == competition_id,
                Submission.is_correct.is_(True),
                Submission.reward_pending.is_(True),
            )
        )
        pending_submission = pending_result.scalar_one_or_none()
        if pending_submission:
            score_recorded = await _record_competition_solve(
                competition_id=competition_id,
                user_id=user_id,
                challenge_id=challenge_id,
                accepted_at=pending_submission.submitted_at,
            )
            if score_recorded:
                pending_submission.reward_pending = False
                await db.commit()
                return SubmitResponse(status="correct", message="정답입니다.")
            return SubmitResponse(
                status="score_sync_pending",
                message="정답 제출은 기록되었지만 대회 점수 반영을 확인하지 못했습니다. 잠시 후 다시 제출해주세요.",
            )

        competition_id = await _require_competition_context(
            challenge=challenge,
            competition_id=competition_id,
            user_id=user_id,
            purpose="submit",
        )
    elif challenge.challenge_type == "competition":
        # CTF 문제는 대회 컨텍스트가 없는 일반 워게임 제출을 허용하지 않는다.
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    # Mandatory prerequisite tables belong to the public wargame learning
    # path. CTF access is deliberately governed only by its assigned
    # competition context; legacy CTF prerequisite rows are ignored instead
    # of turning an incorrectly mapped contest challenge into a permanent
    # participant lock.
    prereq_ids: list[int] = []
    if not is_competition:
        prerequisite_query = (
            select(ChallengePrerequisite.prerequisite_id)
            .where(ChallengePrerequisite.challenge_id == challenge_id)
            .join(Challenge, Challenge.id == ChallengePrerequisite.prerequisite_id)
            .where(
                Challenge.is_published.is_(True),
                Challenge.challenge_type == "wargame",
            )
        )
        prereq_result = await db.execute(prerequisite_query)
        prereq_ids = [row[0] for row in prereq_result.all()]
    if prereq_ids:
        prerequisite_context = (
            Submission.competition_id == competition_id
            if is_competition
            else Submission.competition_id.is_(None)
        )
        solved_result = await db.execute(
            select(Submission.challenge_id).where(
                Submission.user_id == user_id,
                Submission.is_correct.is_(True),
                Submission.challenge_id.in_(prereq_ids),
                prerequisite_context,
            )
        )
        solved_prereqs = {row[0] for row in solved_result.all()}
        if not all(pid in solved_prereqs for pid in prereq_ids):
            raise HTTPException(
                status_code=403,
                detail="선행조건 챌린지를 먼저 풀어야 합니다.",
            )

    if not is_competition:
        # 학습 선행조건 검증
        learning_locked, _ = await _check_learning_prerequisites(
            challenge_id, user_id, db
        )
        if learning_locked:
            raise HTTPException(
                status_code=403,
                detail="선행 학습을 먼저 완료해야 합니다.",
            )

    # Rate Limit 체크 (분당 오답 제출 횟수 제한)
    if await _check_rate_limit(str(user_id)):
        raise HTTPException(
            status_code=429,
            detail="제출이 너무 많습니다. 잠시 후 다시 시도해주세요.",
            headers={"Retry-After": "60"},
        )

    # 이미 풀었는지 확인 — 워게임(competition_id=NULL)과 대회는 별도 추적
    solved_filter = [
        Submission.user_id == user_id,
        Submission.challenge_id == challenge_id,
        Submission.is_correct.is_(True),
    ]
    if is_competition:
        solved_filter.append(Submission.competition_id == competition_id)
    else:
        solved_filter.append(Submission.competition_id.is_(None))
    solved_result = await db.execute(select(Submission).where(*solved_filter))
    existing_submission = solved_result.scalar_one_or_none()
    if existing_submission:
        if is_competition and existing_submission.reward_pending:
            score_recorded = await _record_competition_solve(
                competition_id=competition_id,
                user_id=user_id,
                challenge_id=challenge_id,
                accepted_at=existing_submission.submitted_at,
            )
            if score_recorded:
                existing_submission.reward_pending = False
                await db.commit()
                return SubmitResponse(status="correct", message="정답입니다.")
            return SubmitResponse(
                status="score_sync_pending",
                message="정답 제출은 기록되었지만 대회 점수 반영을 확인하지 못했습니다. 잠시 후 다시 제출해주세요.",
            )
        return SubmitResponse(status="already_solved", message="이미 풀었던 문제입니다.")

    # 최대 시도 횟수 체크 (0 = 무제한) — 워게임/대회 별도 카운트
    if challenge.max_attempts > 0:
        attempt_filter = [
            Submission.user_id == user_id,
            Submission.challenge_id == challenge_id,
        ]
        if is_competition:
            attempt_filter.append(Submission.competition_id == competition_id)
        else:
            attempt_filter.append(Submission.competition_id.is_(None))
        attempt_count_result = await db.execute(
            select(func.count()).select_from(Submission).where(*attempt_filter)
        )
        attempt_count = attempt_count_result.scalar() or 0
        if attempt_count >= challenge.max_attempts:
            return SubmitResponse(
                status="incorrect",
                message=f"최대 시도 횟수({challenge.max_attempts}회)를 초과했습니다.",
            )

    # 플래그 비교 — dynamic_instance는 별도 issuance 원장을 사용하고, 모든 제출
    # 원문은 관리자 제한 열람을 위한 Fernet 암호문으로만 보관한다.
    submitted = body.flag.strip()
    dynamic_issuance: DynamicFlagIssuance | None = None
    stored_submitted_flag = SUBMISSION_REDACTION_MARKER
    stored_submitted_flag_length = len(submitted)
    stored_submitted_flag_fingerprint: bytes | None = None
    stored_submitted_flag_storage_version = SUBMISSION_STORAGE_VERSION
    try:
        stored_submitted_flag_ciphertext = encrypt_submitted_flag(
            submitted,
            encryption_key=settings.flag_storage_encryption_key,
            fallback_secret=settings.internal_api_key,
        )
    except FlagStorageUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="제출 원문을 안전하게 저장할 수 없어 제출을 처리하지 못했습니다.",
        ) from exc
    stored_submitted_flag_key_version = (
        settings.flag_storage_encryption_key_version
        if settings.flag_storage_encryption_key
        else SUBMITTED_FLAG_DERIVED_KEY_VERSION
    )
    if challenge.flag_type == DYNAMIC_FLAG_TYPE:
        dynamic_issuance = await _validate_and_consume_dynamic_flag(
            challenge=challenge,
            submitted=submitted,
            user_id=user_id,
            competition_id=competition_id,
            db=db,
        )
        is_correct = dynamic_issuance is not None
        stored_submitted_flag = DYNAMIC_SUBMISSION_MARKER
        stored_submitted_flag_storage_version = DYNAMIC_SUBMISSION_STORAGE_VERSION
    else:
        flags_result = await db.execute(
            select(Flag)
            .where(Flag.challenge_id == challenge_id)
            .order_by(Flag.order_index)
        )
        flag_entries = flags_result.scalars().all()

        if flag_entries:
            is_correct = _check_flags(submitted, flag_entries)
        else:
            # 폴백: 기존 단일 플래그 (timing-safe)
            correct = challenge.flag_value.strip()
            is_correct = hmac.compare_digest(submitted.encode(), correct.encode())
        storage_metadata = build_submission_storage_metadata(
            submitted,
            fingerprint_secret=settings.internal_api_key,
        )
        stored_submitted_flag = str(storage_metadata["submitted_flag_marker"])
        stored_submitted_flag_length = int(storage_metadata["submitted_flag_length"])
        stored_submitted_flag_fingerprint = storage_metadata["submitted_flag_fingerprint"]  # type: ignore[assignment]
        stored_submitted_flag_storage_version = str(storage_metadata["submitted_flag_storage_version"])

    # 제출 기록 저장
    submission = Submission(
        user_id=user_id,
        challenge_id=challenge_id,
        submitted_flag=stored_submitted_flag,
        submitted_flag_length=stored_submitted_flag_length,
        submitted_flag_fingerprint=stored_submitted_flag_fingerprint,
        submitted_flag_storage_version=stored_submitted_flag_storage_version,
        submitted_flag_ciphertext=stored_submitted_flag_ciphertext,
        submitted_flag_key_version=stored_submitted_flag_key_version,
        is_correct=is_correct,
        competition_id=competition_id,
        dynamic_flag_issuance_id=dynamic_issuance.id if dynamic_issuance else None,
    )
    db.add(submission)

    await db.commit()

    if is_correct:
        if is_competition:
            # CTF 대회: 대회 스코어보드에만 기록, 워게임 포인트/XP/solves 변경 없음
            score_recorded = await _record_competition_solve(
                competition_id=competition_id,
                user_id=user_id,
                challenge_id=challenge_id,
                accepted_at=submission.submitted_at,
            )
            if not score_recorded:
                submission.reward_pending = True
                await db.commit()
                return SubmitResponse(
                    status="score_sync_pending",
                    message="정답 제출은 기록되었지만 대회 점수 반영을 확인하지 못했습니다. 잠시 후 다시 제출해주세요.",
                )
        else:
            # 워게임: solves 카운트 증가 + 포인트/XP 지급
            await db.execute(
                update(Challenge)
                .where(Challenge.id == challenge_id)
                .values(solves=Challenge.solves + 1)
            )
            await db.commit()

            # auth-service에 포인트/XP 지급 요청 — 최대 2회 재시도
            reward_success = False
            for attempt in range(3):
                try:
                    client = get_auth_client()
                    resp = await client.post(
                        f"{settings.auth_service_url}/internal/reward",
                        json={
                            "user_id": str(user_id),
                            "points": challenge.value,
                            "xp": challenge.xp,
                        },
                    )
                    if resp.status_code == 200:
                        reward_success = True
                        break
                except Exception as e:
                    logger.warning("포인트/XP 지급 시도 %d 실패 (user=%s, challenge=%d): %s", attempt + 1, user_id, challenge_id, e)

            if not reward_success:
                logger.error("포인트/XP 지급 최종 실패 — 수동 보상 필요 (user=%s, challenge=%d, points=%d, xp=%d)",
                             user_id, challenge_id, challenge.value, challenge.xp)
                submission.reward_pending = True

        return SubmitResponse(status="correct", message="정답입니다!")

    # 오답: rate limit 기록
    await _record_wrong_attempt(str(user_id))
    return SubmitResponse(status="incorrect", message="플래그가 올바르지 않습니다.")


# ──── 힌트 언락 ────

@router.post(
    "/challenges/{challenge_id}/hints/{hint_id}/unlock",
    response_model=HintUnlockResponse,
)
async def unlock_hint(
    challenge_id: int,
    hint_id: int,
    competition_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """힌트 언락."""
    # 힌트 조회
    result = await db.execute(
        select(Hint).where(Hint.id == hint_id, Hint.challenge_id == challenge_id)
    )
    hint = result.scalar_one_or_none()
    if not hint:
        raise HTTPException(status_code=404, detail="힌트를 찾을 수 없습니다.")

    challenge_result = await db.execute(
        select(Challenge).where(
            Challenge.id == challenge_id,
            Challenge.is_published.is_(True),
        )
    )
    challenge = challenge_result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    if challenge.challenge_type == "competition":
        await _require_competition_context(
            challenge=challenge,
            competition_id=competition_id,
            user_id=user_id,
            purpose="view",
        )
    elif competition_id:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    # 이미 언락했는지 확인
    existing = await db.execute(
        select(HintUnlock).where(
            HintUnlock.user_id == user_id,
            HintUnlock.hint_id == hint_id,
        )
    )
    if not existing.scalar_one_or_none():
        # CTF는 일반 사용자 포인트·장터 화폐와 분리한다. 대회 힌트의 점수 패널티는
        # 향후 대회 전용 모델로 설계하기 전까지 일반 포인트를 차감하지 않는다.
        is_competition_hint = challenge.challenge_type == "competition"
        if hint.cost > 0 and not is_competition_hint:
            try:
                client = get_auth_client()
                resp = await client.post(
                    f"{settings.auth_service_url}/internal/points/deduct",
                    json={"user_id": str(user_id), "amount": hint.cost},
                )
                if resp.status_code == 400:
                    raise HTTPException(status_code=400, detail="포인트가 부족합니다.")
                if resp.status_code != 200:
                    raise HTTPException(status_code=502, detail="포인트 차감 서버 오류")
            except HTTPException:
                raise
            except Exception as e:
                logger.error("힌트 포인트 차감 실패: %s", e)
                raise HTTPException(status_code=502, detail="포인트 차감 중 오류가 발생했습니다.")

        # 새 언락 기록 저장
        unlock = HintUnlock(user_id=user_id, hint_id=hint_id)
        db.add(unlock)
        await db.commit()

    return HintUnlockResponse(content=hint.content)


# ──── 챌린지 생성 (admin/moderator 전용) ────

@router.post("/challenges", status_code=status.HTTP_201_CREATED)
async def create_challenge(
    body: ChallengeCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """챌린지 등록 — 생성 후 백그라운드에서 자동 번역."""
    require_edit_draft(body)
    if body.flag_type == DYNAMIC_FLAG_TYPE:
        _require_dynamic_flag_feature()
        # ChallengeCreate의 schema가 dynamic policy와 Docker 환경을 함께 검증한다.
        # 여기서는 type checker와 이후 정책 생성의 불변식만 명시한다.
        if body.dynamic_flag is None or not body.dynamic_flag.enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="동적 플래그 정책이 필요합니다. (dynamic_flag_policy_required)",
            )
        _resolve_dynamic_policy_key_version(body.dynamic_flag.hmac_key_version)
    _allow_compatibility_flag_healthcheck(
        env_type=body.env_type,
        flag_type=body.flag_type,
        dockerfile_content=body.dockerfile_content,
    )
    _validate_competition_mandatory_prerequisites(
        challenge_type=body.challenge_type,
        prerequisite_ids=body.prerequisite_ids,
        learning_prerequisites=list(body.learning_prerequisites),
    )
    await _validate_learning_content_references(
        db,
        body.learning_prerequisites,
        field_name="learning_prerequisites",
        require_completable_course=True,
    )
    await _validate_learning_content_references(
        db,
        body.recommended_learning,
        field_name="recommended_learning",
    )
    await _validate_challenge_prerequisites(
        db,
        body.prerequisite_ids,
        challenge_id=None,
        source_challenge_type=body.challenge_type,
    )
    await _validate_knowledge_tag_ids(db, body.knowledge_tag_ids)
    if body.is_published and body.env_type != "none":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="컨테이너 환경 문제는 생성 후 이미지 빌드와 환경 검증을 완료한 뒤 공개하세요. (environment_verification_required)",
        )

    container_port = body.container_port
    # API callers may omit a port when the Dockerfile provides one explicit TCP
    # EXPOSE hint. An explicitly supplied value remains authoritative and is
    # still checked by the verification container later.
    if body.env_type == "dockerfile" and "container_port" not in body.model_fields_set:
        detected_port = detect_dockerfile_exposed_port(body.dockerfile_content)
        if detected_port is not None:
            container_port = detected_port

    challenge = Challenge(
        name=body.name,
        category=body.category,
        difficulty=body.difficulty,
        description=body.description,
        value=body.value,
        xp=body.xp,
        max_attempts=body.max_attempts,
        flag_type=body.flag_type,
        flag_value=body.flag_value,
        tags=body.tags,
        files=body.files,
        env_type=body.env_type,
        dockerfile_content=body.dockerfile_content,
        docker_image=body.docker_image,
        container_port=container_port,
        challenge_type=body.challenge_type,
        author_id=user_id,
        is_published=body.is_published,
        thumbnail_url=body.thumbnail_url,
    )
    db.add(challenge)
    await db.flush()  # id 확보

    # 힌트 저장
    for i, hint_data in enumerate(body.hints):
        hint = Hint(
            challenge_id=challenge.id,
            content=hint_data.content,
            cost=hint_data.cost,
            order_index=i,
        )
        db.add(hint)

    # 선행조건 저장
    for pid in body.prerequisite_ids:
        db.add(ChallengePrerequisite(challenge_id=challenge.id, prerequisite_id=pid))

    # 지식 태그 저장: 명시적 관리자 매핑은 그래프 provenance 원장에도 남긴다.
    tag_mapping_now = datetime.now(timezone.utc)
    for tid in body.knowledge_tag_ids:
        new_mapping = ChallengeKnowledgeTag(
            challenge_id=challenge.id,
            tag_id=tid,
            direction="content_to_knowledge_tag",
            strength=1,
            confidence=1,
            evidence_summary="관리자가 생성 시 연결한 지식 태그",
            origin="manual",
            created_at=tag_mapping_now,
            created_by=user_id,
            approved_at=tag_mapping_now,
        )
        db.add(new_mapping)
        _add_mapping_audit(
            db,
            entity_type="challenge",
            entity_id=str(challenge.id),
            tag_id=tid,
            action="attach",
            actor_id=user_id,
            snapshot=_mapping_snapshot(new_mapping, fallback_now=tag_mapping_now),
        )

    # 학습 선행조건 저장
    for lp in body.learning_prerequisites:
        db.add(ChallengeLearningPrerequisite(
            challenge_id=challenge.id,
            prerequisite_type=lp.type,
            prerequisite_target_id=lp.target_id,
        ))

    # 추천 선행 문제 저장
    for cid in body.recommended_challenges:
        db.add(ChallengeRecommendedPrerequisite(
            challenge_id=challenge.id, rec_type="challenge", rec_target_id=str(cid)
        ))

    # 추천 선행 학습 저장
    for rl in body.recommended_learning:
        db.add(ChallengeRecommendedPrerequisite(
            challenge_id=challenge.id, rec_type=rl.type, rec_target_id=str(rl.target_id)
        ))

    # 플래그 저장 (flags 배열이 있으면 사용, 없으면 flag_value로 자동 생성)
    if body.flag_type == DYNAMIC_FLAG_TYPE:
        policy = body.dynamic_flag
        assert policy is not None  # 위의 request contract 검증
        db.add(ChallengeFlagPolicy(
            challenge_id=challenge.id,
            mode=DYNAMIC_FLAG_TYPE,
            delivery=policy.delivery,
            runtime_path=policy.runtime_path,
            ttl_seconds=policy.ttl_seconds,
            policy_revision=1,
            hmac_key_version=policy.hmac_key_version,
            enabled=policy.enabled,
        ))
    elif body.flags:
        for i, flag_data in enumerate(body.flags):
            flag = Flag(
                challenge_id=challenge.id,
                type=flag_data.type,
                value=flag_data.value,
                case_sensitive=flag_data.case_sensitive,
                order_index=i,
            )
            db.add(flag)
    elif body.flag_value.strip():
        # 레거시 호환: flag_value가 있으면 단일 Flag 레코드 생성
        flag = Flag(
            challenge_id=challenge.id,
            type=body.flag_type,
            value=body.flag_value,
            case_sensitive=True,
            order_index=0,
        )
        db.add(flag)

    await project_rule_tag_mappings(
        db,
        entity_type="challenge",
        entity=challenge,
        actor_id=user_id,
    )
    await bind_attachments(db, challenge, actor_id=user_id, attachment_ids=body.attachment_ids,
                           attachment_draft_id=body.attachment_draft_id, previous_files=[], new_challenge=True)
    attachments = [item.model_dump(mode="json") for item in await attachment_descriptors(db, challenge)]
    await db.commit()
    await db.refresh(challenge)

    # 백그라운드에서 자동 번역 실행
    background_tasks.add_task(
        _run_auto_translate, challenge.id, challenge.name, challenge.description
    )

    return {
        "id": challenge.id,
        "name": challenge.name,
        "category": challenge.category,
        "message": "챌린지가 등록되었습니다. 번역이 백그라운드에서 진행됩니다.",
        "attachment_revision": challenge.attachment_revision,
        "attachments": attachments,
    }


# ──── 챌린지 수정 (admin/moderator 전용) ────

@router.patch("/challenges/{challenge_id}")
async def update_challenge(
    challenge_id: int,
    body: ChallengeUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    actor_id: UUID = Depends(get_current_user_id),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """챌린지 수정 (부분 업데이트) — name/description 변경 시 자동 재번역."""
    require_edit_draft(body)
    result = await db.execute(
        select(Challenge)
        .options(selectinload(Challenge.hints))
        .where(Challenge.id == challenge_id)
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    update_data = body.model_dump(exclude_unset=True)
    previous_files = list(challenge.files or [])
    attachment_ids = update_data.pop("attachment_ids", None)
    attachment_draft_id = update_data.pop("attachment_draft_id", None)
    needs_retranslate = "name" in update_data or "description" in update_data
    environment_before = _environment_snapshot(challenge)

    # A write to any part of a legacy challenge also canonicalizes its body when
    # conversion is lossless. Ambiguous legacy rows remain untouched and return
    # a structured review error to the editor.
    try:
        if "description" not in update_data:
            challenge.description = normalize_rich_content_for_write(
                challenge.description or "",
                field_name="문제 설명",
                allow_empty=True,
                max_visible_chars=10000,
            )
        if "description_i18n" not in update_data and challenge.description_i18n:
            challenge.description_i18n = normalize_rich_content_mapping(
                challenge.description_i18n,
                field_name="문제 설명 번역",
                allow_empty=True,
                max_visible_chars=10000,
            )
    except RichContentError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error.as_detail(),
        ) from error

    if "name" in update_data and "name_i18n" not in update_data:
        update_data["name_i18n"] = {}
    if "description" in update_data and "description_i18n" not in update_data:
        update_data["description_i18n"] = {}

    # 힌트/플래그/선행조건은 별도 처리 (전체 교체) — Pydantic 객체에서 직접 꺼냄
    has_new_hints = "hints" in update_data
    has_new_flags = "flags" in update_data
    has_new_dynamic_flag = "dynamic_flag" in update_data
    has_new_prereqs = "prerequisite_ids" in update_data
    has_new_learning_prereqs = "learning_prerequisites" in update_data
    update_data.pop("hints", None)
    update_data.pop("flags", None)
    update_data.pop("dynamic_flag", None)
    update_data.pop("prerequisite_ids", None)
    update_data.pop("knowledge_tag_ids", None)
    update_data.pop("knowledge_tag_mapping", None)
    mapping_input = body.knowledge_tag_mapping
    if mapping_input is None and body.knowledge_tag_ids is not None:
        mapping_input = ContentTagMappingUpdate(tag_ids=body.knowledge_tag_ids)
    update_data.pop("learning_prerequisites", None)
    has_new_rec_challenges = "recommended_challenges" in update_data
    has_new_rec_learning = "recommended_learning" in update_data
    update_data.pop("recommended_challenges", None)
    update_data.pop("recommended_learning", None)

    requested_publish = update_data.get("is_published")
    current_flag_type = challenge.flag_type
    target_flag_type = update_data.get("flag_type", current_flag_type)
    target_challenge_type = update_data.get("challenge_type", challenge.challenge_type)
    target_env_type = update_data.get("env_type", challenge.env_type)
    target_dockerfile_content = update_data.get(
        "dockerfile_content", challenge.dockerfile_content
    )
    # Dockerfile만 교체하는 PATCH에서도 EXPOSE를 안전한 포트 힌트로 사용한다.
    # 명시적인 container_port가 함께 오면 운영자가 의도한 포트이므로 덮어쓰지
    # 않는다. 이 보정은 특정 문제명/포트에 의존하지 않으며, 기존 문제의 Dockerfile
    # 교체 후 예전 기본값(8080)이 남아 프록시가 다른 포트로 접속하는 회귀를 막는다.
    if (
        target_env_type == "dockerfile"
        and "container_port" not in update_data
        and "dockerfile_content" in update_data
    ):
        detected_port = detect_dockerfile_exposed_port(target_dockerfile_content)
        if detected_port is not None:
            update_data["container_port"] = detected_port
    current_is_dynamic = current_flag_type == DYNAMIC_FLAG_TYPE
    target_is_dynamic = target_flag_type == DYNAMIC_FLAG_TYPE
    current_flag_rows: list[Flag] = []
    proposed_flag_rows = []
    if has_new_flags:
        current_flag_rows = list(
            (
                await db.execute(
                    select(Flag)
                    .where(Flag.challenge_id == challenge.id)
                    .order_by(Flag.order_index, Flag.id)
                    .with_for_update()
                )
            ).scalars().all()
        )
        proposed_flag_rows = list(body.flags or [])
    dynamic_policy_input = body.dynamic_flag
    existing_dynamic_policy: ChallengeFlagPolicy | None = None
    if current_is_dynamic or target_is_dynamic:
        policy_result = await db.execute(
            select(ChallengeFlagPolicy)
            .where(ChallengeFlagPolicy.challenge_id == challenge.id)
            .with_for_update()
        )
        existing_dynamic_policy = policy_result.scalar_one_or_none()

    if target_challenge_type == "competition":
        incoming_prerequisite_ids = list(body.prerequisite_ids or []) if has_new_prereqs else []
        incoming_learning_prerequisites = (
            list(body.learning_prerequisites or []) if has_new_learning_prereqs else []
        )
        _validate_competition_mandatory_prerequisites(
            challenge_type=target_challenge_type,
            prerequisite_ids=incoming_prerequisite_ids,
            learning_prerequisites=incoming_learning_prerequisites,
        )
        # A type transition must not carry existing mandatory wargame rows
        # into a CTF problem. For an already-legacy CTF row, unrelated edits
        # stay possible and submit ignores the stale rows below; an admin can
        # explicitly clear them with empty replacement lists.
        if challenge.challenge_type != target_challenge_type:
            retained_prerequisite_ids = incoming_prerequisite_ids
            retained_learning_prerequisites = incoming_learning_prerequisites
            if not has_new_prereqs:
                retained_prerequisite_ids = list(
                    (
                        await db.execute(
                            select(ChallengePrerequisite.prerequisite_id).where(
                                ChallengePrerequisite.challenge_id == challenge.id
                            )
                        )
                    ).scalars().all()
                )
            if not has_new_learning_prereqs:
                retained_learning_prerequisites = list(
                    (
                        await db.execute(
                            select(ChallengeLearningPrerequisite.id).where(
                                ChallengeLearningPrerequisite.challenge_id == challenge.id
                            )
                        )
                    ).scalars().all()
                )
            _validate_competition_mandatory_prerequisites(
                challenge_type=target_challenge_type,
                prerequisite_ids=retained_prerequisite_ids,
                learning_prerequisites=retained_learning_prerequisites,
            )

    if has_new_dynamic_flag and not target_is_dynamic:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="dynamic_flag 정책은 dynamic_instance 플래그에서만 사용할 수 있습니다.",
        )

    _allow_compatibility_flag_healthcheck(
        env_type=target_env_type,
        flag_type=target_flag_type,
        dockerfile_content=target_dockerfile_content,
    )

    # PATCH에서 dynamic_flag: null은 기존 정책을 "유지"하라는 뜻이 아니다. 정책을
    # 비워 둔 동적 문제를 만들거나, 무관한 PATCH에서 policy revision을 바꾸는 모호성을
    # 막기 위해 명시적인 null은 거절한다. 정책을 바꾸지 않을 때는 필드를 생략해야 한다.
    if has_new_dynamic_flag and dynamic_policy_input is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="dynamic_instance 정책은 null일 수 없습니다. 변경하지 않으려면 dynamic_flag 필드를 생략하세요.",
        )

    if target_is_dynamic:
        _require_dynamic_flag_feature()
        if update_data.get("env_type", challenge.env_type) == "none":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic_instance 플래그에는 Docker 실습 환경이 필요합니다.",
            )
        if (body.flag_value is not None and body.flag_value.strip()) or (
            body.flags is not None and len(body.flags) > 0
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic_instance 플래그에는 정적 flag_value 또는 flags를 지정할 수 없습니다.",
            )
        if dynamic_policy_input is not None and not dynamic_policy_input.enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic_instance 정책을 비활성화할 수 없습니다. 정적 또는 regex 플래그로 전환하세요.",
            )
        if not current_is_dynamic and dynamic_policy_input is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="dynamic_instance 플래그에는 dynamic_flag 정책이 필요합니다.",
            )
        if current_is_dynamic and existing_dynamic_policy is None and dynamic_policy_input is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="동적 플래그 정책이 누락되어 수정할 수 없습니다. 복구 절차를 사용하세요.",
            )
        policy_key_version = (
            dynamic_policy_input.hmac_key_version
            if dynamic_policy_input is not None
            else existing_dynamic_policy.hmac_key_version
        )
        _resolve_dynamic_policy_key_version(policy_key_version)
    elif current_is_dynamic:
        # dynamic_instance에서 빠져나갈 때는 이전 raw flag를 재사용하지 않고 새 정적
        # 또는 regex 검증 재료를 명시적으로 받는다.
        if target_flag_type == "regex":
            has_replacement = body.flags is not None and len(body.flags) > 0
        else:
            has_replacement = (
                (body.flags is not None and len(body.flags) > 0)
                or (body.flag_value is not None and bool(body.flag_value.strip()))
            )
        if not has_replacement:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="동적 플래그를 전환하려면 새 정적 또는 regex 플래그를 명시해야 합니다.",
            )

    current_dynamic_policy_values = (
        {
            "enabled": existing_dynamic_policy.enabled,
            "delivery": existing_dynamic_policy.delivery,
            "runtime_path": existing_dynamic_policy.runtime_path,
            "ttl_seconds": existing_dynamic_policy.ttl_seconds,
            "hmac_key_version": existing_dynamic_policy.hmac_key_version,
        }
        if existing_dynamic_policy is not None
        else None
    )
    proposed_dynamic_policy_values = (
        {
            "enabled": dynamic_policy_input.enabled,
            "delivery": dynamic_policy_input.delivery,
            "runtime_path": dynamic_policy_input.runtime_path,
            "ttl_seconds": dynamic_policy_input.ttl_seconds,
            "hmac_key_version": dynamic_policy_input.hmac_key_version,
        }
        if dynamic_policy_input is not None
        else None
    )
    # 같은 정책을 다시 저장하는 것은 멱등 편집이다. 실제 정책 변경이나 모드
    # 전환만 환경 검증과 기존 issuance를 무효화한다.
    dynamic_policy_changed = current_is_dynamic != target_is_dynamic or (
        has_new_dynamic_flag
        and dynamic_policy_configuration_changed(
            current_dynamic_policy_values,
            proposed_dynamic_policy_values,
        )
    )
    environment_changed = environment_configuration_changed(environment_before, update_data)
    flag_policy_changed = (
        target_env_type != "none"
        and flag_policy_configuration_changed(
            current_type=current_flag_type,
            current_value=challenge.flag_value,
            current_flags=current_flag_rows,
            proposed_type=target_flag_type,
            proposed_value=update_data.get("flag_value", challenge.flag_value),
            proposed_flags=proposed_flag_rows,
        )
    )
    verification_inputs_changed = (
        environment_changed or dynamic_policy_changed or flag_policy_changed
    )
    if verification_inputs_changed and challenge.is_published:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="공개된 컨테이너 환경 또는 플래그 정책은 먼저 비공개로 전환한 뒤 수정하세요. (environment_unpublish_required)",
        )
    if requested_publish is True and verification_inputs_changed:
        # 같은 PATCH 안에서 환경 입력을 바꾸면 기존 검증 revision이 아직 challenge에
        # 남아 있으므로, 공개 요청을 명시적으로 거절해야 stale 검증을 재사용하지 않는다.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="환경 설정을 변경한 뒤에는 빌드와 환경 검증을 다시 완료하세요. (environment_verification_required)",
        )
    if requested_publish is True:
        _require_environment_ready_for_publication(challenge)
        if target_is_dynamic:
            # static -> dynamic 전환에서는 policy row를 아직 flush하지 않았다. 공개는
            # 같은 PATCH에서 허용하지 않아야 하며 위 requested_publish+변경 guard가
            # 이를 거절한다. 현재 dynamic 문제의 publish 요청은 기존 policy를 검사한다.
            _require_dynamic_policy_ready_for_publication(existing_dynamic_policy)

    if has_new_learning_prereqs and body.learning_prerequisites is not None:
        await _validate_learning_content_references(
            db,
            body.learning_prerequisites,
            field_name="learning_prerequisites",
            require_completable_course=True,
        )
    if has_new_rec_learning and body.recommended_learning is not None:
        await _validate_learning_content_references(
            db,
            body.recommended_learning,
            field_name="recommended_learning",
        )
    if has_new_prereqs and body.prerequisite_ids is not None:
        await _validate_challenge_prerequisites(
            db,
            body.prerequisite_ids,
            challenge_id=challenge.id,
            source_challenge_type=update_data.get(
                "challenge_type",
                challenge.challenge_type,
            ),
        )
    elif "challenge_type" in update_data:
        existing_prerequisite_ids = list(
            (
                await db.execute(
                    select(ChallengePrerequisite.prerequisite_id).where(
                        ChallengePrerequisite.challenge_id == challenge.id
                    )
                )
            ).scalars().all()
        )
        await _validate_challenge_prerequisites(
            db,
            existing_prerequisite_ids,
            challenge_id=challenge.id,
            source_challenge_type=update_data["challenge_type"],
        )

    for field, value in update_data.items():
        setattr(challenge, field, value)

    if verification_inputs_changed:
        _invalidate_environment_verification(challenge)
        await _revoke_active_dynamic_issuances_for_stale_environment(
            db,
            challenge_id=challenge.id,
        )

    # dynamic_instance는 flags/flag_value를 사용하지 않는다. mode 전환 시 이전 정적
    # 검증 재료를 삭제하고 별도 정책 row만 남겨 raw flag와 섞이지 않게 한다.
    if target_is_dynamic:
        challenge.flag_value = ""
        await db.execute(sa_delete(Flag).where(Flag.challenge_id == challenge.id))
        if existing_dynamic_policy is None:
            policy = dynamic_policy_input
            assert policy is not None
            db.add(ChallengeFlagPolicy(
                challenge_id=challenge.id,
                mode=DYNAMIC_FLAG_TYPE,
                delivery=policy.delivery,
                runtime_path=policy.runtime_path,
                ttl_seconds=policy.ttl_seconds,
                policy_revision=1,
                hmac_key_version=policy.hmac_key_version,
                enabled=policy.enabled,
            ))
        elif (
            dynamic_policy_input is not None
            and dynamic_policy_configuration_changed(
                current_dynamic_policy_values,
                proposed_dynamic_policy_values,
            )
        ):
            existing_dynamic_policy.delivery = dynamic_policy_input.delivery
            existing_dynamic_policy.runtime_path = dynamic_policy_input.runtime_path
            existing_dynamic_policy.ttl_seconds = dynamic_policy_input.ttl_seconds
            existing_dynamic_policy.hmac_key_version = dynamic_policy_input.hmac_key_version
            existing_dynamic_policy.enabled = dynamic_policy_input.enabled
            existing_dynamic_policy.policy_revision += 1
            existing_dynamic_policy.updated_at = datetime.now(timezone.utc)
    elif current_is_dynamic and existing_dynamic_policy is not None:
        await db.delete(existing_dynamic_policy)

    # 힌트가 제공되면 기존 힌트 삭제 후 재생성
    if has_new_hints and body.hints is not None:
        for old_hint in challenge.hints:
            await db.delete(old_hint)
        for i, hint_data in enumerate(body.hints):
            hint = Hint(
                challenge_id=challenge.id,
                content=hint_data.content,
                cost=hint_data.cost,
                order_index=i,
            )
            db.add(hint)

    # 플래그가 제공되면 기존 플래그 삭제 후 재생성
    if has_new_flags and body.flags is not None and not target_is_dynamic:
        await db.execute(
            sa_delete(Flag).where(Flag.challenge_id == challenge.id)
        )
        for i, flag_data in enumerate(body.flags):
            flag = Flag(
                challenge_id=challenge.id,
                type=flag_data.type,
                value=flag_data.value,
                case_sensitive=flag_data.case_sensitive,
                order_index=i,
            )
            db.add(flag)

    # 선행조건이 제공되면 기존 삭제 후 재생성
    if has_new_prereqs and body.prerequisite_ids is not None:
        await db.execute(
            sa_delete(ChallengePrerequisite).where(
                ChallengePrerequisite.challenge_id == challenge.id
            )
        )
        for pid in body.prerequisite_ids:
            db.add(ChallengePrerequisite(challenge_id=challenge.id, prerequisite_id=pid))


    # 학습 선행조건 업데이트
    if has_new_learning_prereqs and body.learning_prerequisites is not None:
        await db.execute(
            sa_delete(ChallengeLearningPrerequisite).where(
                ChallengeLearningPrerequisite.challenge_id == challenge_id
            )
        )
        for lp in body.learning_prerequisites:
            db.add(ChallengeLearningPrerequisite(
                challenge_id=challenge.id,
                prerequisite_type=lp.type,
                prerequisite_target_id=lp.target_id,
            ))

    # 추천 선행조건 업데이트
    if has_new_rec_challenges and body.recommended_challenges is not None:
        await db.execute(
            sa_delete(ChallengeRecommendedPrerequisite).where(
                ChallengeRecommendedPrerequisite.challenge_id == challenge_id,
                ChallengeRecommendedPrerequisite.rec_type == "challenge",
            )
        )
        for cid in body.recommended_challenges:
            db.add(ChallengeRecommendedPrerequisite(
                challenge_id=challenge.id, rec_type="challenge", rec_target_id=str(cid)
            ))

    if has_new_rec_learning and body.recommended_learning is not None:
        await db.execute(
            sa_delete(ChallengeRecommendedPrerequisite).where(
                ChallengeRecommendedPrerequisite.challenge_id == challenge_id,
                ChallengeRecommendedPrerequisite.rec_type.in_(["course", "lesson"]),
            )
        )
        for rl in body.recommended_learning:
            db.add(ChallengeRecommendedPrerequisite(
                challenge_id=challenge.id, rec_type=rl.type, rec_target_id=str(rl.target_id)
            ))

    if mapping_input is not None:
        await replace_content_tag_mappings(
            db, entity_type="challenge", entity=challenge, body=mapping_input, actor_id=actor_id,
        )
    else:
        await project_rule_tag_mappings(
            db, entity_type="challenge", entity=challenge, actor_id=actor_id,
        )
    await bind_attachments(db, challenge, actor_id=actor_id, attachment_ids=attachment_ids,
                           attachment_draft_id=attachment_draft_id, previous_files=previous_files)
    attachments = [item.model_dump(mode="json") for item in await attachment_descriptors(db, challenge)]
    await db.commit()

    # name 또는 description이 변경되면 자동 재번역
    if needs_retranslate:
        background_tasks.add_task(
            _run_auto_translate, challenge.id, challenge.name, challenge.description
        )

    return {
        "id": challenge.id,
        "name": challenge.name,
        "is_published": challenge.is_published,
        "message": "챌린지가 수정되었습니다.",
        "attachment_revision": challenge.attachment_revision,
        "attachments": attachments,
    }


# ──── 챌린지 삭제 (admin 전용) ────

_CLEANUP_PUBLIC_ERROR_CODES = {
    "env_orchestrator_unavailable",
    "invalid_cleanup_response",
    "challenge_image_cleanup_failed",
    "cleanup_challenge_exists",
    "cleanup_target_rejected",
}


def _challenge_cleanup_status(job: ChallengeImageCleanupJob) -> dict:
    """정리 원장의 내부 주소나 하위 오류 본문은 관리자에게 전달하지 않는다."""
    error_code = job.last_error_code
    if error_code and error_code not in _CLEANUP_PUBLIC_ERROR_CODES:
        error_code = "challenge_image_cleanup_failed"
    return {
        "challenge_id": job.challenge_id,
        "job_id": str(job.id),
        "status": job.status,
        "cleanup_job_id": str(job.id),
        "cleanup_status": job.status,
        "attempt_count": job.attempt_count,
        "last_error_code": error_code,
        "image_cleanup_pending": bool(job.image_reference) and job.status != "completed",
    }


async def _latest_challenge_cleanup(
    db: AsyncSession, challenge_id: int, *, for_update: bool = False,
) -> ChallengeImageCleanupJob | None:
    query = (
        select(ChallengeImageCleanupJob)
        .where(ChallengeImageCleanupJob.challenge_id == challenge_id)
        .order_by(
            ChallengeImageCleanupJob.updated_at.desc(),
            ChallengeImageCleanupJob.created_at.desc(),
            ChallengeImageCleanupJob.id.desc(),
        )
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def _deleted_challenge_cleanup(
    db: AsyncSession, challenge_id: int, *, for_update: bool = False, job_id: UUID | None = None,
) -> ChallengeImageCleanupJob:
    current = (await db.execute(select(Challenge.id).where(Challenge.id == challenge_id))).scalar_one_or_none()
    if current is not None:
        raise HTTPException(status_code=409, detail={
            "code": "cleanup_challenge_exists", "message": "현재 존재하는 문제는 정리할 수 없습니다.",
        })
    if job_id is None:
        job = await _latest_challenge_cleanup(db, challenge_id, for_update=for_update)
    else:
        query = select(ChallengeImageCleanupJob).where(
            ChallengeImageCleanupJob.id == job_id,
            ChallengeImageCleanupJob.challenge_id == challenge_id,
        )
        if for_update:
            query = query.with_for_update()
        job = (await db.execute(query)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="문제 정리 기록을 찾을 수 없습니다.")
    return job


@router.get("/challenge-cleanup-jobs")
async def list_challenge_cleanup_jobs(
    cleanup_status: str | None = Query(default=None, alias="status", pattern="^(pending|processing|failed)$"),
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin")),
):
    """현재 문제가 없는 미완료 정리만 관리 목록에 표시한다."""
    conditions = [
        ChallengeImageCleanupJob.status.in_(
            [cleanup_status] if cleanup_status else ["pending", "processing", "failed"]
        ),
        ~select(Challenge.id).where(Challenge.id == ChallengeImageCleanupJob.challenge_id).exists(),
    ]
    total = (await db.execute(
        select(func.count()).select_from(ChallengeImageCleanupJob).where(*conditions)
    )).scalar() or 0
    jobs = (await db.execute(
        select(ChallengeImageCleanupJob).where(*conditions)
        .order_by(
            ChallengeImageCleanupJob.updated_at.desc(),
            ChallengeImageCleanupJob.created_at.desc(),
            ChallengeImageCleanupJob.id.desc(),
        )
        .limit(100)
    )).scalars().all()
    return {"items": [_challenge_cleanup_status(job) for job in jobs], "total": total}


@router.get("/challenges/{challenge_id}/cleanup")
async def get_challenge_cleanup(
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin")),
):
    """삭제한 문제의 정리 상태를 확인한다."""
    return _challenge_cleanup_status(await _deleted_challenge_cleanup(db, challenge_id))


@router.post("/challenges/{challenge_id}/cleanup/retry")
async def retry_challenge_cleanup(
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin")),
    job_id: UUID | None = None,
):
    """누적 시도 횟수를 보존하고 실패한 정리를 명시적으로 다시 시도한다."""
    job = await _deleted_challenge_cleanup(db, challenge_id, for_update=True, job_id=job_id)
    now = datetime.now(timezone.utc)
    if job.status == "processing" and job.lease_until is not None and job.lease_until > now:
        raise HTTPException(status_code=409, detail={
            "code": "cleanup_in_progress", "message": "문제 정리가 진행 중입니다. 상태를 다시 확인하세요.",
        })
    if job.status in {"pending", "failed", "processing"}:
        job.status = "pending"
        job.available_at = now
        job.updated_at = now
        job.lease_until = None
        await db.commit()
    return {**_challenge_cleanup_status(job), "retry_budget_reset": False}


@router.delete("/challenges/{challenge_id}")
async def delete_challenge(
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin")),
):
    """챌린지 삭제 (관련 힌트/제출 기록도 삭제)."""
    result = await db.execute(
        select(Challenge)
        .options(selectinload(Challenge.hints))
        .where(Challenge.id == challenge_id)
        .with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        job = await _latest_challenge_cleanup(db, challenge_id)
        if job is not None:
            return {"message": "챌린지가 삭제되었습니다.", **_challenge_cleanup_status(job)}
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    # 모든 삭제에 원장을 남기되 외부 image 이름은 정리 권한으로 사용하지 않는다.
    # 빈 reference는 임의의 image 이름을 삭제 권한으로 전달하지 않는 계약이다.
    platform_image_reference = f"ctf-challenge-{challenge_id}:latest"
    image_cleanup_pending = challenge.docker_image == platform_image_reference
    image_reference = platform_image_reference if image_cleanup_pending else ""
    cleanup_job = (await db.execute(
        select(ChallengeImageCleanupJob).where(
            ChallengeImageCleanupJob.challenge_id == challenge_id,
            ChallengeImageCleanupJob.image_reference == image_reference,
        ).with_for_update()
    )).scalar_one_or_none()
    if cleanup_job is None:
        cleanup_job = ChallengeImageCleanupJob(
            id=uuid4(),
            challenge_id=challenge_id,
            image_reference=image_reference,
            status="pending",
            attempt_count=0,
        )
        db.add(cleanup_job)
    else:
        # 복원된 동일 ID의 재삭제도 원장의 누적 시도·이전 완료 이력을 보존한다.
        now = datetime.now(timezone.utc)
        cleanup_job.status = "pending"
        cleanup_job.available_at = now
        cleanup_job.updated_at = now
        cleanup_job.lease_until = None

    # 선행조건 삭제 (이 챌린지가 선행조건인 경우도 포함)
    await db.execute(
        sa_delete(ChallengePrerequisite).where(
            (ChallengePrerequisite.challenge_id == challenge_id)
            | (ChallengePrerequisite.prerequisite_id == challenge_id)
        )
    )

    # 벌크 삭제: 힌트 언락 → 제출 → 힌트 순서 (FK 의존성)
    hint_ids = [h.id for h in challenge.hints]
    if hint_ids:
        await db.execute(sa_delete(HintUnlock).where(HintUnlock.hint_id.in_(hint_ids)))
    await db.execute(sa_delete(Submission).where(Submission.challenge_id == challenge_id))
    if hint_ids:
        await db.execute(sa_delete(Hint).where(Hint.id.in_(hint_ids)))

    # MinIO 빌드 컨텍스트 파일 정리
    try:
        delete_all_build_files(challenge.id)
    except BuildContextStorageUnavailableError:
        # 컨텍스트 삭제를 확인하지 못한 상태에서 DB를 지우면 다음 등록이나
        # 재빌드가 오래된 Dockerfile/flag.txt를 읽을 수 있으므로 삭제를
        # 원자적으로 중단하고 운영자가 저장소를 복구한 뒤 재시도하게 한다.
        raise _build_context_storage_unavailable() from None

    # 챌린지 삭제
    await db.execute(sa_delete(Challenge).where(Challenge.id == challenge_id))
    await db.commit()

    return {
        "message": "챌린지가 삭제되었습니다.",
        "image_cleanup_pending": image_cleanup_pending,
        "cleanup_job_id": str(cleanup_job.id),
        "cleanup_status": cleanup_job.status,
    }


# ──── 빌드 컨텍스트 파일 관리 (admin/moderator 전용) ────


@router.post("/challenges/{challenge_id}/build-files")
async def upload_build_files(
    challenge_id: int,
    files: list[UploadFile],
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """빌드 컨텍스트 파일 업로드 (멀티파일)."""
    # 챌린지 존재 확인
    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id).with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")
    if challenge.is_published:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="공개된 컨테이너 환경은 먼저 비공개로 전환한 뒤 빌드 파일을 수정하세요. (environment_unpublish_required)",
        )

    if not files:
        raise HTTPException(status_code=400, detail="업로드할 파일이 없습니다.")

    # 기존 파일 총 크기 확인
    try:
        existing = list_build_files(challenge_id)
    except BuildContextStorageUnavailableError:
        raise _build_context_storage_unavailable() from None
    existing_total = sum(f["size"] for f in existing)
    existing_size_by_name = {f["name"]: f["size"] for f in existing}

    # 모든 파일을 먼저 검증·정규화한 뒤 MinIO에 기록한다. 앞 파일을 저장한
    # 다음 뒤 파일에서 4xx가 나면 빌드 컨텍스트가 부분 갱신되는 회귀를 막는다.
    prepared_uploads: list[tuple[str, bytes]] = []
    seen_filenames: set[str] = set()
    new_total = 0
    detected_container_port: int | None = None

    for file in files:
        filename = file.filename or "unknown"

        # 파일명 검증
        err = validate_build_filename(filename)
        if err:
            raise HTTPException(status_code=400, detail=f"{filename}: {err}")
        if filename in seen_filenames:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"중복된 빌드 파일명입니다: {filename}",
            )
        seen_filenames.add(filename)
        # 제한을 넘는 파일 전체를 메모리에 올리지 않는다. +1 바이트만 읽어
        # 초과 여부를 판정하고, 정상 파일은 기존 정규화 경로로 전달한다.
        try:
            data = await file.read(MAX_BUILD_FILE_SIZE + 1)
        finally:
            # Starlette가 요청 종료 시 정리하더라도, 대용량·다중 파일 업로드에서
            # 다음 파일 검증으로 넘어가기 전에 임시 파일 핸들을 즉시 반환한다.
            await file.close()

        # A Dockerfile supplied only through the build context is authoritative for
        # legacy challenges whose database field is empty. Validate it at upload time
        # so a static/regex challenge cannot defer a flag-dependent healthcheck failure
        # until the first instance proxy request.
        if filename == "Dockerfile":
            try:
                uploaded_dockerfile = data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Dockerfile은 UTF-8 텍스트여야 합니다. (invalid_dockerfile_encoding)",
                ) from None
            _allow_compatibility_flag_healthcheck(
                env_type=challenge.env_type,
                flag_type=challenge.flag_type,
                dockerfile_content=uploaded_dockerfile,
            )

        # Build-context-only legacy challenges may still carry the schema
        # default port even though their uploaded root Dockerfile exposes a
        # different TCP port. Keep an explicitly configured port authoritative;
        # only use EXPOSE as a safe hint when the legacy Dockerfile field is
        # empty and the stored value is still the platform default.
        if filename == "Dockerfile" and not challenge.dockerfile_content.strip():
            try:
                detected_container_port = detect_dockerfile_exposed_port(
                    data.decode("utf-8")
                )
            except UnicodeDecodeError:
                detected_container_port = None

        # 개별 파일 크기 검증
        if len(data) > MAX_BUILD_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"{filename}: 파일 크기가 50MB를 초과합니다.",
            )

        try:
            stored_data = normalize_build_context_file(
                filename,
                data,
                challenge.flag_type,
            )
        except DynamicFlagFormatError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from None

        # Keep the raw upload only in request memory. The object store and any later
        # Docker build see the non-secret placeholder, never the author's flag value.
        new_total += len(stored_data)

        prepared_uploads.append((filename, stored_data))

    # 같은 이름의 기존 파일은 교체되므로 기존 크기를 중복 계산하지 않는다.
    # 새 파일 추가와 파일 교체를 모두 반영한 최종 컨텍스트 크기를 검증해야
    # 정상적인 Dockerfile/flag 교체가 100MB 제한에 걸리지 않는다.
    replaced_total = sum(
        existing_size_by_name.get(filename, 0)
        for filename, _stored_data in prepared_uploads
    )
    final_total = existing_total - replaced_total + new_total
    if final_total > MAX_BUILD_TOTAL_SIZE:
        raise HTTPException(
            status_code=400,
            detail="빌드 컨텍스트 전체 크기가 100MB를 초과합니다.",
        )

    uploaded = []
    try:
        for filename, stored_data in prepared_uploads:
            info = upload_build_file(challenge_id, filename, stored_data)
            uploaded.append({"name": info["name"], "size": info["size"]})
    except Exception as error:
        # 저장 중 MinIO 오류가 나면 이번 요청에서 이미 기록한 파일만 best-effort
        # 롤백한다. 롤백 실패는 원래 오류를 가리지 않도록 로그로 남긴다.
        for item in uploaded:
            try:
                delete_build_file(challenge_id, item["name"])
            except Exception:
                logger.warning(
                    "부분 빌드 컨텍스트 정리 실패: challenge=%d filename=%s",
                    challenge_id,
                    item["name"],
                    exc_info=True,
                )
        logger.error(
            "빌드 컨텍스트 저장 실패: challenge=%d uploaded=%d",
            challenge_id,
            len(uploaded),
            exc_info=True,
        )
        raise _build_context_storage_unavailable() from error

    # 빌드 컨텍스트가 바뀌면 같은 docker_image 태그라도 실제 실행 파일은 달라진다.
    if uploaded:
        if (
            detected_container_port is not None
            and challenge.container_port == DEFAULT_DOCKER_CONTAINER_PORT
            and not challenge.dockerfile_content.strip()
        ):
            challenge.container_port = detected_container_port
        _invalidate_environment_verification(challenge)
        await _revoke_active_dynamic_issuances_for_stale_environment(
            db,
            challenge_id=challenge.id,
        )
        await db.commit()

    return {
        "files": uploaded,
        "container_port": challenge.container_port,
        "message": f"{len(uploaded)}개 파일 업로드 완료",
    }


@router.get("/challenges/{challenge_id}/build-files")
async def get_build_files(
    challenge_id: int,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """빌드 컨텍스트 파일 목록 조회."""
    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id).with_for_update()
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    try:
        files = list_build_files(challenge_id)
    except BuildContextStorageUnavailableError:
        raise _build_context_storage_unavailable() from None
    return {"files": files}


@router.delete("/challenges/{challenge_id}/build-files/{filename:path}")
async def remove_build_file(
    challenge_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _role: str = Depends(require_role("admin", "moderator")),
):
    """빌드 컨텍스트 파일 삭제."""
    # 파일명 검증 (경로 순회 방지)
    err = validate_build_filename(filename)
    if err:
        raise HTTPException(status_code=400, detail=err)

    result = await db.execute(
        select(Challenge).where(Challenge.id == challenge_id).with_for_update()
    )
    challenge = result.scalar_one_or_none()
    if not challenge:
        raise HTTPException(status_code=404, detail="챌린지를 찾을 수 없습니다.")

    if challenge.is_published:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="공개된 컨테이너 환경은 먼저 비공개로 전환한 뒤 빌드 파일을 수정하세요. (environment_unpublish_required)",
        )
    try:
        delete_build_file(challenge_id, filename)
    except BuildContextStorageUnavailableError:
        raise _build_context_storage_unavailable() from None
    _invalidate_environment_verification(challenge)
    await _revoke_active_dynamic_issuances_for_stale_environment(
        db,
        challenge_id=challenge.id,
    )
    await db.commit()
    return {"message": f"{filename} 파일이 삭제되었습니다."}
