"""동적 인스턴스 플래그의 순수 생성·파싱·HMAC 검증 도구.

이 모듈은 raw flag를 저장하지 않는다. raw flag는 Env orchestrator의 인스턴스 생성
요청에 대한 단 한 번의 내부 응답에서만 사용하며, Learn DB에는 locator와 HMAC digest만
남는다. DB 조회·HTTP·로그는 이 모듈에 두지 않아 테스트에서도 비밀 설정을 요구하지
않는다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Mapping


DYNAMIC_FLAG_TYPE = "dynamic_instance"
DYNAMIC_FLAG_VERSION = "v1"
DYNAMIC_FLAG_RUNTIME_PATH = "/flag.txt"
DYNAMIC_SUBMISSION_MARKER = "[dynamic-instance flag redacted]"
# A build-context placeholder is deliberately not a valid platform flag. It may be
# copied into an image layer for Dockerfiles that use ``COPY flag.txt``; Env replaces
# it with the per-instance read-only runtime mount later.
DYNAMIC_BUILD_FLAG_PLACEHOLDER = b"CTF_DYNAMIC_FLAG_PLACEHOLDER\n"

MIN_DYNAMIC_FLAG_TTL_SECONDS = 60
MAX_DYNAMIC_FLAG_TTL_SECONDS = 86_400

# ``policy_revision`` is a consequence of a real input change, not an input
# itself.  Keeping this field list in one pure helper makes repeated admin
# saves idempotent and preserves a freshly verified environment.
DYNAMIC_POLICY_CONFIGURATION_FIELDS = (
    "enabled",
    "delivery",
    "runtime_path",
    "ttl_seconds",
    "hmac_key_version",
)

_LOCATOR_RE = re.compile(r"^[A-Z2-7]{16,64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_FLAG_RE = re.compile(
    rf"^FLAG\{{{DYNAMIC_FLAG_VERSION}_([A-Z2-7]{{16,64}})_([A-Za-z0-9_-]{{43,128}})\}}$"
)
_KEY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BASE64URL_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


class DynamicFlagConfigurationError(ValueError):
    """동적 플래그 keyring 또는 feature 설정이 안전하지 않을 때 발생한다."""


class DynamicFlagFormatError(ValueError):
    """동적 플래그 형식 또는 정책 입력이 계약과 다를 때 발생한다."""


def dynamic_policy_configuration_changed(
    current: Mapping[str, object] | None,
    proposed: Mapping[str, object] | None,
) -> bool:
    """실제 동적 플래그 정책 입력이 바뀌었는지 확인한다.

    관리 화면은 편집 저장·환경 동기화 때 정책 객체를 매번 함께 보낼 수 있다.
    값이 같은 재저장은 policy revision과 환경 검증을 무효화해서는 안 된다.
    """

    if current is None or proposed is None:
        return True
    return any(
        current.get(field) != proposed.get(field)
        for field in DYNAMIC_POLICY_CONFIGURATION_FIELDS
    )


def normalize_build_context_file(filename: str, data: bytes, flag_type: str | None) -> bytes:
    """빌드 컨텍스트에 저장할 파일을 정규화한다.

    ``flag.txt``는 Dockerfile이 참조할 수 있는 선택적 컨텍스트 파일이다. 업로더가
    제출한 원문은 플래그 모드와 무관하게 MinIO나 Docker 이미지 레이어에 저장하지
    않고 고정 placeholder로 치환한다. 실제 인스턴스별 플래그의 read-only 런타임
    마운트와 발급은 dynamic_instance에서만 수행한다.
    """
    if filename.rsplit("/", 1)[-1] != "flag.txt":
        return data
    return DYNAMIC_BUILD_FLAG_PLACEHOLDER


@dataclass(frozen=True)
class ParsedDynamicFlag:
    """제출 문자열에서 추출한 공개 locator와 비밀 token.

    token은 호출자 메모리에만 존재해야 하며, 이 객체를 로그 또는 직렬화 응답에 넣으면
    안 된다.
    """

    locator: str
    token: str


def validate_dynamic_runtime_path(value: str) -> str:
    """V1의 유일한 안전한 container-side mount path를 검증한다."""
    if value != DYNAMIC_FLAG_RUNTIME_PATH:
        raise DynamicFlagFormatError(
            f"동적 플래그 V1의 runtime path는 {DYNAMIC_FLAG_RUNTIME_PATH}만 허용됩니다."
        )
    return value


def validate_dynamic_key_version(value: str) -> str:
    """keyring lookup에 사용할 비밀 없는 key version 식별자를 검증한다."""
    if not _KEY_VERSION_RE.fullmatch(value):
        raise DynamicFlagFormatError("동적 플래그 key version 형식이 올바르지 않습니다.")
    return value


def parse_dynamic_flag_keyring(raw_keyring: str) -> dict[str, bytes]:
    """`version=base64url-key` 형식의 keyring을 메모리에서만 해석한다.

    key는 256 bit 이상이어야 한다. 오류 메시지에는 실제 key 또는 입력 조각을 포함하지
    않아 로그·응답 경로에서 비밀이 노출되지 않도록 한다.
    """
    if not raw_keyring or not raw_keyring.strip():
        return {}

    parsed: dict[str, bytes] = {}
    for entry in raw_keyring.split(","):
        version, separator, encoded_key = entry.strip().partition("=")
        if not separator:
            raise DynamicFlagConfigurationError("동적 플래그 keyring 형식이 올바르지 않습니다.")
        validate_dynamic_key_version(version)
        if not encoded_key:
            raise DynamicFlagConfigurationError("동적 플래그 keyring 값이 비어 있습니다.")
        if not _BASE64URL_KEY_RE.fullmatch(encoded_key) or "=" in encoded_key.rstrip("="):
            raise DynamicFlagConfigurationError("동적 플래그 keyring 인코딩이 올바르지 않습니다.")
        try:
            padded = encoded_key + ("=" * (-len(encoded_key) % 4))
            key = base64.b64decode(
                padded.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError) as error:
            raise DynamicFlagConfigurationError("동적 플래그 keyring 인코딩이 올바르지 않습니다.") from error
        if len(key) < 32:
            raise DynamicFlagConfigurationError("동적 플래그 key는 256 bit 이상이어야 합니다.")
        if version in parsed:
            raise DynamicFlagConfigurationError("동적 플래그 key version이 중복되었습니다.")
        parsed[version] = key
    return parsed


def resolve_dynamic_hmac_key(
    *,
    keyring: Mapping[str, bytes],
    key_version: str,
) -> bytes:
    """발급 또는 검증에 필요한 특정 version의 HMAC key를 반환한다."""
    validate_dynamic_key_version(key_version)
    key = keyring.get(key_version)
    if key is None or len(key) < 32:
        raise DynamicFlagConfigurationError("요청한 동적 플래그 key version을 사용할 수 없습니다.")
    return key


def generate_dynamic_locator() -> str:
    """DB lookup용 공개 locator를 CSPRNG로 만든다. token 자체는 아니다."""
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")


def generate_dynamic_token() -> str:
    """CSPRNG 256 bit token을 base64url로 생성한다."""
    return secrets.token_urlsafe(32)


def build_dynamic_flag(*, locator: str, token: str) -> str:
    """컨테이너에 단 한 번 전달할 raw dynamic flag 문자열을 만든다."""
    if not _LOCATOR_RE.fullmatch(locator) or not _TOKEN_RE.fullmatch(token):
        raise DynamicFlagFormatError("동적 플래그 구성 요소 형식이 올바르지 않습니다.")
    return f"FLAG{{{DYNAMIC_FLAG_VERSION}_{locator}_{token}}}"


def parse_dynamic_flag(value: str) -> ParsedDynamicFlag | None:
    """사용자 제출값을 형식만 파싱한다. 실패 시 입력값을 되돌려 주지 않는다."""
    matched = _FLAG_RE.fullmatch(value)
    if not matched:
        return None
    return ParsedDynamicFlag(locator=matched.group(1), token=matched.group(2))


def dynamic_token_hmac(*, key: bytes, locator: str, token: str) -> bytes:
    """DB에 저장할 token HMAC을 계산한다.

    locator를 함께 domain separation input에 넣어 다른 issuance의 token digest가 재사용될
    수 없게 한다. raw token은 이 함수의 반환값에 포함되지 않는다.
    """
    if len(key) < 32:
        raise DynamicFlagConfigurationError("동적 플래그 key는 256 bit 이상이어야 합니다.")
    if not _LOCATOR_RE.fullmatch(locator) or not _TOKEN_RE.fullmatch(token):
        raise DynamicFlagFormatError("동적 플래그 구성 요소 형식이 올바르지 않습니다.")
    material = b"ctf-web/dynamic-flag/v1\x00" + locator.encode("ascii") + b"\x00" + token.encode("ascii")
    return hmac.new(key, material, hashlib.sha256).digest()


def dynamic_token_matches(*, expected_hmac: bytes, key: bytes, locator: str, token: str) -> bool:
    """timing-safe 비교로 submitted token이 issuance와 맞는지 확인한다."""
    if len(expected_hmac) != hashlib.sha256().digest_size:
        return False
    candidate = dynamic_token_hmac(key=key, locator=locator, token=token)
    return hmac.compare_digest(candidate, expected_hmac)
