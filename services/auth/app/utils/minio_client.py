"""MinIO 파일 스토리지 클라이언트 유틸리티."""

import io
import logging
import os
import re
import uuid
from urllib.parse import urlsplit

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)

_client: Minio | None = None

# --- 버킷 ---
BANNERS_BUCKET = "banners"
AVATAR_BUCKET = "avatars"

# --- 배너 설정 ---
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_BANNER_SIZE = 5 * 1024 * 1024  # 5MB

# --- 아바타 설정 ---
AVATAR_MAX_SIZE = 2 * 1024 * 1024  # 2MB
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class AvatarValidationError(ValueError):
    """SDK 예외와 구분되는 공개 입력 검증 실패."""


# 확장자 → Content-Type 매핑 (클라이언트 값 신뢰 안 함)
_EXTENSION_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def get_minio_client() -> Minio:
    """MinIO 클라이언트 싱글턴 반환."""
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _client


def ensure_bucket(bucket: str) -> None:
    """버킷이 없으면 생성하고 공개 읽기 정책 설정."""
    client = get_minio_client()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            # 공개 읽기 — 인증 없이 접근 가능
            policy = (
                '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                '"Principal":{"AWS":["*"]},'
                '"Action":["s3:GetObject"],'
                f'"Resource":["arn:aws:s3:::{bucket}/*"]'
                "}]}"
            )
            client.set_bucket_policy(bucket, policy)
            logger.info("MinIO 버킷 생성 (공개 읽기): %s", bucket)
    except S3Error:
        logger.warning("MinIO 버킷 확인/생성 실패: %s", bucket, exc_info=True)


def _get_extension(filename: str) -> str:
    """파일 확장자 추출 (소문자)."""
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ""


# ──────────────────────────────────────────────
# 배너
# ──────────────────────────────────────────────

def upload_banner(data: bytes, original_filename: str, content_type: str) -> str:
    """배너 이미지를 MinIO에 업로드하고 공개 URL 반환."""
    client = get_minio_client()
    ensure_bucket(BANNERS_BUCKET)

    ext = _get_extension(original_filename)
    object_name = f"{uuid.uuid4().hex}{ext}"

    client.put_object(
        BANNERS_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )

    # 서브패스 배포 시 COOKIE_BASE_PATH 환경변수로 prefix 추가
    import os
    base = os.environ.get("COOKIE_BASE_PATH", "")
    return f"{base}/storage/{BANNERS_BUCKET}/{object_name}"


# ──────────────────────────────────────────────
# 아바타
# ──────────────────────────────────────────────

def upload_avatar(data: bytes, original_filename: str, user_id: str) -> str:
    """아바타 이미지를 MinIO에 업로드하고 오브젝트 경로를 반환.

    사용자별 UUID 경로를 사용하여 기존 객체를 덮어쓰지 않는다.

    Args:
        data: 이미지 바이너리 데이터.
        original_filename: 원본 파일명 (확장자 추출용).
        user_id: 객체 소유 사용자 ID.

    Returns:
        오브젝트 경로 (예: "avatars/<user UUID>/<object UUID>.png").

    Raises:
        ValueError: 파일 크기 초과 또는 허용되지 않는 확장자.
    """
    if len(data) > AVATAR_MAX_SIZE:
        raise AvatarValidationError(
            f"아바타 파일 크기가 {AVATAR_MAX_SIZE // (1024 * 1024)}MB를 초과합니다."
        )

    ext = _get_extension(original_filename)
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS))
        raise AvatarValidationError(f"허용되지 않는 이미지 형식입니다. 허용: {allowed}")

    try:
        owner_id = str(uuid.UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError):
        raise AvatarValidationError("올바른 아바타 소유자가 아닙니다.") from None
    client = get_minio_client()
    ensure_bucket(AVATAR_BUCKET)

    object_name = f"{owner_id}/{uuid.uuid4().hex}{ext}"
    content_type = _EXTENSION_CONTENT_TYPE.get(ext, "application/octet-stream")

    client.put_object(
        AVATAR_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )

    logger.info("아바타 업로드 완료: %s/%s", AVATAR_BUCKET, object_name)
    return f"{AVATAR_BUCKET}/{object_name}"


def owned_avatar_object_name(avatar_url: str | None, user_id: str, *, base_path: str | None = None) -> str | None:
    """기존 URL을 삭제 권한으로 신뢰하지 않고 알려진 소유 키만 반환한다."""
    if not isinstance(avatar_url, str) or any(
        character in avatar_url for character in ("%", "\\", "?", "#")
    ) or any(ord(character) <= 32 or ord(character) == 127 for character in avatar_url):
        return None
    try:
        parsed = urlsplit(avatar_url)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.path != avatar_url:
        return None
    try:
        owner_id = str(uuid.UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError):
        return None
    base = os.environ.get("COOKIE_BASE_PATH", "") if base_path is None else base_path
    prefixes = [f"/storage/{AVATAR_BUCKET}/"]
    if base and re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", base):
        prefixes.append(f"{base}/storage/{AVATAR_BUCKET}/")
    for prefix in prefixes:
        if not avatar_url.startswith(prefix):
            continue
        object_name = avatar_url[len(prefix):]
        extension = r"\.(?:png|jpg|jpeg|gif|webp)"
        if re.fullmatch(re.escape(owner_id) + extension, object_name):
            return object_name
        match = re.fullmatch(re.escape(owner_id) + r"/([0-9a-f]{32})" + extension, object_name)
        if match and uuid.UUID(hex=match.group(1)).version == 4:
            return object_name
    return None


def delete_avatar(avatar_url: str | None, user_id: str) -> bool:
    """확정된 포인터 변경 뒤 정확한 소유 객체만 best-effort 정리한다."""
    object_name = owned_avatar_object_name(avatar_url, user_id)
    if object_name is None:
        return False
    try:
        get_minio_client().remove_object(AVATAR_BUCKET, object_name)
    except Exception:
        logger.warning("기존 아바타 정리를 완료하지 못했습니다.")
        return False
    return True
