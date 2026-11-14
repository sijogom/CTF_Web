"""민감 데이터 대칭 암호화 유틸 — Fernet (AES-128-CBC + HMAC-SHA256)."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from app.config import settings

# 고정 salt — 키 파생 재현성 보장 (랜덤 salt 사용 시 복호화 불가)
_HKDF_SALT = b"ctf-platform-fernet-key-v1"


def _derive_key() -> bytes:
    """JWT 시크릿 키에서 HKDF로 32바이트 Fernet 키를 안전하게 파생한다."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=b"fernet-encryption-key",
    )
    derived = hkdf.derive(settings.jwt_secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


def _derive_key_legacy() -> bytes:
    """이전 버전 호환: SHA-256 해시 기반 키 파생 (마이그레이션용)."""
    secret = settings.jwt_secret_key.encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_value(plaintext: str) -> str:
    """평문을 암호화하여 반환. 빈 문자열이면 그대로 반환."""
    if not plaintext:
        return plaintext
    f = Fernet(_derive_key())
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_value(ciphertext: str) -> str:
    """암호문을 복호화하여 반환.

    시도 순서: HKDF 키 → 레거시 SHA-256 키 → 평문 반환 (마이그레이션 호환).
    """
    if not ciphertext:
        return ciphertext
    # 1. 현재 HKDF 키로 시도
    try:
        return Fernet(_derive_key()).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        pass
    # 2. 레거시 SHA-256 키로 시도 (기존 데이터 호환)
    try:
        return Fernet(_derive_key_legacy()).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        pass
    # 3. 암호화 이전에 저장된 평문 값 — 그대로 반환
    return ciphertext
