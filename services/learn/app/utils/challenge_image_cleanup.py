"""문제 삭제와 Docker image tag 정리의 내부 계약."""

from __future__ import annotations

import httpx

from app.config import settings
from app.utils.auth_client import get_client


class ChallengeImageCleanupError(RuntimeError):
    """내부 image 정리 요청이 안전하게 완료되지 않은 경우."""


class ChallengeImageInUseError(ChallengeImageCleanupError):
    """해당 문제의 인스턴스 컨테이너가 남아 있어 삭제할 수 없는 경우."""


async def cleanup_challenge_image(challenge_id: int) -> str:
    """Env에 정확한 challenge tag 정리를 요청하고 공개 가능한 상태만 반환한다."""

    if challenge_id <= 0:
        raise ChallengeImageCleanupError("유효하지 않은 챌린지 ID입니다.")
    if not settings.env_orchestrator_url or not settings.internal_api_key:
        raise ChallengeImageCleanupError("문제 환경 정리 경계가 설정되지 않았습니다.")

    client = get_client()
    try:
        response = await client.post(
            f"{settings.env_orchestrator_url.rstrip('/')}/internal/challenges/{challenge_id}/cleanup-image",
        )
    except (httpx.RequestError, RuntimeError) as error:
        raise ChallengeImageCleanupError("문제 환경 정리 서비스에 연결할 수 없습니다.") from error

    if response.status_code == 409:
        raise ChallengeImageInUseError("문제 환경 컨테이너를 먼저 종료해야 합니다.")
    if response.status_code != 200:
        raise ChallengeImageCleanupError("문제 image를 정리할 수 없습니다.")

    try:
        status = response.json().get("status")
    except (TypeError, ValueError):
        raise ChallengeImageCleanupError("문제 환경 정리 응답이 올바르지 않습니다.") from None
    if status not in {"removed", "absent"}:
        raise ChallengeImageCleanupError("문제 환경 정리 상태가 올바르지 않습니다.")
    return status
