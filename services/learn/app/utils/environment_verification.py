"""문제 컨테이너 환경의 공개 가능 상태를 계산하는 순수 함수.

이 모듈은 SQLAlchemy·Docker에 의존하지 않는다. API, internal service, 테스트가 같은
revision 규칙을 사용하도록 환경 설정 변경과 검증 상태 판정을 한 곳에 둔다.
"""

from collections.abc import Iterable, Mapping
import re
from typing import Any


ENVIRONMENT_CONFIGURATION_FIELDS = frozenset(
    {
        "env_type",
        "dockerfile_content",
        "docker_image",
        "container_port",
    }
)

ENVIRONMENT_FAILURE_CODES = frozenset(
    {
        "container_exited",
        "container_mismatch",
        "healthcheck_unhealthy",
        "inspect_failed",
        "invalid_environment",
        "network_unavailable",
        "port_unreachable",
        "start_timeout",
        "verification_in_progress",
        "verification_record_failed",
        "verification_execution_failed",
    }
)

_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(.+?)\s*(?:#.*)?$", re.IGNORECASE)
_EXPOSE_PORT_RE = re.compile(r"^(\d{1,5})(?:/(?:tcp|udp))?$", re.IGNORECASE)
_HEALTHCHECK_RE = re.compile(r"^\s*HEALTHCHECK(?:\s|$)", re.IGNORECASE)
_FLAG_HEALTHCHECK_MARKERS = (
    "flag.txt",
    "flag_path",
    "get('flag')",
    'get("flag")',
    "flag ==",
    "flag==",
)


def detect_dockerfile_exposed_port(dockerfile_content: str | None) -> int | None:
    """Dockerfile의 첫 번째 유효한 TCP EXPOSE 포트를 제안값으로 반환한다.

    EXPOSE는 실제 리스닝을 보장하지 않으므로 실행 계약의 권위값이 아니다. 다만 API
    요청에서 `container_port`를 생략한 일반 Docker 문제의 흔한 8080/5000 불일치를
    줄이기 위해, 숫자 포트 하나를 안전하게 읽어 기본값 대신 사용할 수 있게 한다.
    UDP만 선언했거나 변수/범위를 사용한 EXPOSE는 추측하지 않고 None을 반환한다.
    """
    if not dockerfile_content:
        return None
    for line in dockerfile_content.splitlines():
        match = _EXPOSE_RE.match(line)
        if not match:
            continue
        for token in match.group(1).split():
            port_match = _EXPOSE_PORT_RE.fullmatch(token)
            if not port_match:
                continue
            port = int(port_match.group(1))
            if 1 <= port <= 65535 and not token.lower().endswith("/udp"):
                return port
    return None


def dockerfile_healthcheck_depends_on_flag(dockerfile_content: str | None) -> bool:
    """Return whether a Dockerfile HEALTHCHECK expects a runtime flag.

    A flag-dependent healthcheck is a valid contract only for
    ``dynamic_instance``.  Static/regex problems deliberately do not receive a
    platform flag mount; detecting this at authoring and instance lookup time
    prevents a published problem from failing later as an opaque proxy 502.
    Dockerfile line continuations are joined, while comments and unrelated
    RUN/CMD instructions are ignored.
    """
    if not dockerfile_content:
        return False
    lines = dockerfile_content.splitlines()
    for index, line in enumerate(lines):
        if not _HEALTHCHECK_RE.match(line):
            continue
        instruction = line.strip()
        cursor = index
        while instruction.endswith("\\") and cursor + 1 < len(lines):
            instruction = instruction[:-1].rstrip() + " " + lines[cursor + 1].strip()
            cursor += 1
        normalized = instruction.lower()
        if any(marker in normalized for marker in _FLAG_HEALTHCHECK_MARKERS):
            return True
    return False


def environment_is_ready(
    env_type: str | None,
    environment_revision: int | None,
    environment_verified_revision: int | None,
) -> bool:
    """현재 환경 revision이 실제 준비 확인을 통과했는지 반환한다."""
    if env_type in (None, "none"):
        return True
    if not isinstance(environment_revision, int) or environment_revision < 1:
        return False
    return environment_verified_revision == environment_revision


def environment_is_verified_for_runtime(
    env_type: str | None,
    environment_revision: int | None,
    environment_verified_revision: int | None,
    environment_verified_image_id: str | None,
) -> bool:
    """실행 가능한 현재 환경인지 확인한다.

    revision 일치만으로는 mutable Docker tag가 다른 이미지로 바뀐 경우를 막을 수 없다.
    컨테이너 문제는 같은 revision의 Docker inspect image ID까지 있어야 한다. 환경이 없는
    문제는 기존 공개·풀이 흐름을 그대로 유지한다.
    """
    if not environment_is_ready(
        env_type,
        environment_revision,
        environment_verified_revision,
    ):
        return False
    return env_type in (None, "none") or bool(environment_verified_image_id)


def environment_status(
    env_type: str | None,
    environment_revision: int | None,
    environment_verified_revision: int | None,
) -> str:
    """UI/API용 비밀 없는 상태 문자열을 반환한다."""
    if env_type in (None, "none"):
        return "not_required"
    if environment_is_ready(env_type, environment_revision, environment_verified_revision):
        return "ready"
    if environment_verified_revision is None:
        return "legacy_unverified" if environment_revision == 1 else "verification_required"
    return "verification_required"


def environment_configuration_changed(
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> bool:
    """부분 update가 실제 환경 실행 계약을 바꾸는지 확인한다."""
    return any(
        field in proposed and current.get(field) != proposed[field]
        for field in ENVIRONMENT_CONFIGURATION_FIELDS
    )


def flag_policy_configuration_changed(
    *,
    current_type: str | None,
    current_value: str | None,
    current_flags: Iterable[Any],
    proposed_type: str | None,
    proposed_value: str | None,
    proposed_flags: Iterable[Any],
) -> bool:
    """Return whether the effective static/regex flag policy changed.

    API models and Pydantic inputs expose the same three fields but use
    different object types. Normalizing both here keeps revision decisions
    independent from ORM identity and never emits the secret values.
    """

    def field(entry: Any, name: str, default: Any = None) -> Any:
        if isinstance(entry, Mapping):
            return entry.get(name, default)
        return getattr(entry, name, default)

    def signature(
        flag_type: str | None,
        flag_value: str | None,
        flags: Iterable[Any],
    ) -> tuple[str, str, tuple[tuple[str, str, bool], ...]]:
        return (
            flag_type or "",
            flag_value or "",
            tuple(
                (
                    str(field(item, "type", "")),
                    str(field(item, "value", "")),
                    bool(field(item, "case_sensitive", True)),
                )
                for item in flags
            ),
        )

    return signature(current_type, current_value, current_flags) != signature(
        proposed_type,
        proposed_value,
        proposed_flags,
    )


def next_environment_revision(current_revision: int | None) -> int:
    """손상된 과거 값에도 fail-closed로 새 revision을 부여한다."""
    if not isinstance(current_revision, int) or current_revision < 1:
        return 1
    return current_revision + 1


def normalize_environment_failure_code(value: str | None) -> str:
    """원장에 남길 실패 코드를 allowlist 안으로 정규화한다.

    실행 세부 오류나 downstream 응답을 원장·API에 복사하지 않는다. 알려진 준비
    오류는 보존하고, 그 밖의 실행 오류는 일반화한 코드 하나로 수렴한다.
    """
    if value in ENVIRONMENT_FAILURE_CODES:
        return value
    return "verification_execution_failed"
