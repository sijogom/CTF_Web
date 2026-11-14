"""플래그 제출 감사와 비가역 저장 메타데이터를 만든다."""

from __future__ import annotations

import hashlib
import hmac
import base64
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken


SUBMISSION_REDACTION_MARKER = "[static-regex flag redacted]"
SUBMISSION_STORAGE_VERSION = "redacted-hmac-v1"
DYNAMIC_SUBMISSION_STORAGE_VERSION = "dynamic-issuance-v1"
CHALLENGE_SUBMISSION_FLAG_REDACTION_VERSION = "challenge-submission-flag-redacted-hmac-v1"
CHALLENGE_SUBMISSION_FLAG_ENCRYPTION_VERSION = "challenge-submission-flag-fernet-v1"
SUBMITTED_FLAG_ENCRYPTION_VERSION = "submitted-flag-fernet-v1"
SUBMITTED_FLAG_DERIVED_KEY_VERSION = "derived-internal-v1"


class FlagStorageUnavailable(RuntimeError):
    """플래그 암호화 키가 없거나 형식이 잘못되어 저장을 거부할 때 사용한다."""


def _fernet_from_key(key: str | None) -> Fernet:
    if not key or not key.strip():
        raise FlagStorageUnavailable("플래그 저장 암호화 키가 준비되지 않았습니다.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise FlagStorageUnavailable("플래그 저장 암호화 키 형식이 올바르지 않습니다.") from exc


def encrypt_challenge_submission_flag(flag_value: str, *, encryption_key: str | None) -> bytes:
    """문제 등록 요청 플래그를 Fernet 암호문으로 만든다. 원문은 반환하지 않는다."""
    return _fernet_from_key(encryption_key).encrypt(flag_value.encode("utf-8"))


def decrypt_challenge_submission_flag(
    ciphertext: bytes | bytearray | memoryview,
    *,
    encryption_key: str | None,
) -> str:
    """승인 트랜잭션 내부에서만 암호문을 복호화한다."""
    encrypted = bytes(ciphertext)
    try:
        plaintext = _fernet_from_key(encryption_key).decrypt(encrypted)
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise FlagStorageUnavailable("플래그 암호문을 검증할 수 없습니다.") from exc
    return plaintext.decode("utf-8")


def _submitted_flag_fernet_key(
    encryption_key: str | None,
    fallback_secret: str | None,
) -> str:
    """Return the configured key, or a deterministic key derived from the internal secret.

    The fallback keeps existing deployments that predate a dedicated flag key working without
    ever storing or returning the secret. An empty internal secret is still a hard failure.
    """
    if encryption_key and encryption_key.strip():
        return encryption_key.strip()
    if fallback_secret and fallback_secret.strip():
        digest = hashlib.sha256(
            b"ctf-web/submission-flag-storage/v1:" + fallback_secret.strip().encode("utf-8")
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii")
    raise FlagStorageUnavailable("제출 플래그 저장 암호화 키가 준비되지 않았습니다.")


def encrypt_submitted_flag(
    submitted_flag: str,
    *,
    encryption_key: str | None,
    fallback_secret: str | None,
) -> bytes:
    """Encrypt a user's submitted flag before the Submission row is committed."""
    key = _submitted_flag_fernet_key(encryption_key, fallback_secret)
    return _fernet_from_key(key).encrypt(submitted_flag.encode("utf-8"))


def decrypt_submitted_flag(
    ciphertext: bytes | bytearray | memoryview,
    *,
    encryption_key: str | None,
    fallback_secret: str | None,
) -> str:
    """Decrypt a submitted flag for the restricted administrator reveal endpoint."""
    encrypted = bytes(ciphertext)
    key = _submitted_flag_fernet_key(encryption_key, fallback_secret)
    try:
        plaintext = _fernet_from_key(key).decrypt(encrypted)
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise FlagStorageUnavailable("제출 플래그 암호문을 검증할 수 없습니다.") from exc
    return plaintext.decode("utf-8")


def build_submission_list_metadata(submitted_flag: str | None) -> dict[str, int | bool]:
    """원문을 포함하지 않는 제출 목록용 메타데이터를 반환한다.

    이 함수의 반환값은 관리자 목록 API와 UI에만 사용한다. 정확한 원문
    열람은 별도의 암호화 저장·권한·감사 계약이 구현되기 전까지 제공하지 않는다.
    """
    value = submitted_flag or ""
    return {
        "submitted_flag_recorded": bool(value),
        "submitted_flag_length": len(value),
    }


def build_challenge_submission_flag_metadata(
    flag_value: str | None,
    *,
    fingerprint_secret: str | None,
    flag_length: int | None = None,
    storage_version: str | None = None,
    flag_recorded: bool | None = None,
) -> dict[str, object]:
    """문제 등록 요청 상세에서 플래그 원문을 절대 반환하지 않는 메타데이터다.

    승인 처리기는 DB 행의 원문을 내부에서 사용하지만, 관리자 상세 HTTP 응답은
    원문을 내려서는 안 된다. 지문은 검색·감사 상관관계가 필요한 경우에만 내부 키로
    계산하며, 키가 없으면 생략한다. 이 함수의 반환값에는 원문이 포함되지 않는다.
    """
    value = flag_value or ""
    fingerprint: str | None = None
    if value and fingerprint_secret:
        digest = hmac.new(
            fingerprint_secret.encode("utf-8"),
            b"learn/challenge-submission-flag/v1:" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        fingerprint = f"hmac-sha256:{digest}"
    recorded = bool(value) if flag_recorded is None else flag_recorded
    return {
        "flag_value_masked": "[redacted]" if recorded else None,
        "flag_value_recorded": recorded,
        "flag_value_length": flag_length if flag_length is not None else len(value),
        "flag_value_fingerprint": fingerprint,
        "flag_value_fingerprint_algorithm": "hmac-sha256" if fingerprint else None,
        "flag_value_storage_version": storage_version or CHALLENGE_SUBMISSION_FLAG_REDACTION_VERSION,
    }


def build_submission_storage_metadata(
    submitted_flag: str | None,
    *,
    fingerprint_secret: str | None,
) -> dict[str, object]:
    """원문 대신 저장할 길이·키드 지문·버전과 고정 마커를 만든다.

    점수 판정은 호출자가 끝내고 이 함수는 원문을 반환하거나 보관하지 않는다. 비밀키가
    없는 개발 환경에서는 지문을 생략하지만 원문은 여전히 고정 마커로 대체한다.
    """
    value = submitted_flag or ""
    fingerprint: bytes | None = None
    if value and fingerprint_secret:
        fingerprint = hmac.new(
            fingerprint_secret.encode("utf-8"),
            b"learn/submission-storage/v1:" + value.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    return {
        "submitted_flag_marker": SUBMISSION_REDACTION_MARKER,
        "submitted_flag_length": len(value),
        "submitted_flag_fingerprint": fingerprint,
        "submitted_flag_storage_version": SUBMISSION_STORAGE_VERSION,
    }


def merge_stored_submission_audit_metadata(
    metadata: Mapping[str, object],
    *,
    submitted_flag_length: int | None,
    submitted_flag_fingerprint: bytes | bytearray | memoryview | str | None,
    submitted_flag_storage_version: str | None,
) -> dict[str, object]:
    """신규 저장 컬럼이 있으면 관리자 메타데이터를 그 값으로 교체한다.

    레거시 행은 컬럼이 NULL일 수 있으므로 기존 마스킹/HMAC 계산을 유지한다. 새 행의
    fingerprint는 DB의 digest를 그대로 포맷하며, 원문을 다시 계산하거나 반환하지 않는다.
    """
    if submitted_flag_length is None:
        return dict(metadata)

    merged = dict(metadata)
    recorded = submitted_flag_length > 0
    merged["submitted_flag_recorded"] = recorded
    merged["submitted_flag_length"] = submitted_flag_length
    merged["submitted_flag_masked"] = "[redacted]" if recorded else None
    merged["submitted_flag_storage_version"] = submitted_flag_storage_version

    digest: bytes | None = None
    if isinstance(submitted_flag_fingerprint, memoryview):
        digest = submitted_flag_fingerprint.tobytes()
    elif isinstance(submitted_flag_fingerprint, (bytes, bytearray)):
        digest = bytes(submitted_flag_fingerprint)
    elif isinstance(submitted_flag_fingerprint, str) and submitted_flag_fingerprint:
        # 이미 포맷된 값은 내부 migration/fixture 호환용으로만 허용한다.
        merged["submitted_flag_fingerprint"] = submitted_flag_fingerprint
        merged["submitted_flag_fingerprint_algorithm"] = "hmac-sha256"
        return merged

    if digest and len(digest) == hashlib.sha256().digest_size:
        merged["submitted_flag_fingerprint"] = f"hmac-sha256:{digest.hex()}"
        merged["submitted_flag_fingerprint_algorithm"] = "hmac-sha256"
    elif submitted_flag_storage_version == SUBMISSION_STORAGE_VERSION:
        merged["submitted_flag_fingerprint"] = None
        merged["submitted_flag_fingerprint_algorithm"] = None
    return merged
