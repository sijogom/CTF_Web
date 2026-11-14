"""MinIO 파일 스토리지 클라이언트 유틸리티."""

import io
import logging
import uuid
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from app.config import settings

logger = logging.getLogger(__name__)

_client: Minio | None = None

MARKETPLACE_BUCKET = "marketplace"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

# 장터 허용 확장자 (학습 자료 위주)
ALLOWED_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".7z", ".rar",  # 아카이브
    ".pdf", ".txt", ".md", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",  # 문서
    ".png", ".jpg", ".jpeg", ".gif", ".webp",  # 이미지
    ".json", ".yaml", ".yml", ".xml", ".csv",  # 데이터
    ".py", ".c", ".cpp", ".h", ".js", ".ts", ".go", ".java",  # 소스코드
}

# 차단 확장자 (실행 파일, 서버 스크립트)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".sh", ".bash",
    ".msi", ".dll", ".so", ".dylib",
    ".php", ".jsp", ".asp", ".aspx", ".cgi",
    ".com", ".scr", ".vbs", ".wsf", ".hta",
}

# 확장자 → Content-Type 매핑 (클라이언트 값 신뢰 안 함)
EXTENSION_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/x-rar-compressed",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".xml": "application/xml",
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


def ensure_bucket(bucket: str = MARKETPLACE_BUCKET) -> None:
    """버킷이 없으면 생성."""
    client = get_minio_client()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logger.info("MinIO 버킷 생성: %s", bucket)
    except S3Error:
        logger.warning("MinIO 버킷 확인/생성 실패: %s", bucket, exc_info=True)


def get_file_extension(filename: str) -> str:
    """파일 확장자 추출 (소문자)."""
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ""


def validate_filename(filename: str) -> str | None:
    """파일명 보안 검증. 문제가 있으면 에러 메시지 반환, 없으면 None."""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return "유효하지 않은 파일명입니다."

    # null 바이트 검사
    if "\x00" in filename:
        return "유효하지 않은 파일명입니다."

    ext = get_file_extension(filename)

    if ext in BLOCKED_EXTENSIONS:
        return f"보안상 {ext} 파일은 업로드할 수 없습니다."

    if ext and ext not in ALLOWED_EXTENSIONS:
        return f"허용되지 않는 파일 형식입니다: {ext}"

    return None


def upload_file(
    data: bytes,
    original_filename: str,
    content_type: str = "application/octet-stream",
    bucket: str = MARKETPLACE_BUCKET,
) -> str:
    """파일을 MinIO에 업로드하고 오브젝트 경로를 반환."""
    client = get_minio_client()
    ensure_bucket(bucket)

    # 고유 파일명 생성 (UUID + 원본 확장자)
    ext = get_file_extension(original_filename)
    object_name = f"{uuid.uuid4().hex}{ext}"

    # 서버에서 Content-Type 결정 (클라이언트 값 신뢰 안 함)
    safe_content_type = EXTENSION_CONTENT_TYPE.get(ext, "application/octet-stream")

    client.put_object(
        bucket,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=safe_content_type,
    )

    return f"{bucket}/{object_name}"


def get_presigned_url(
    object_path: str,
    expires: timedelta = timedelta(hours=1),
) -> str:
    """MinIO 오브젝트의 presigned download URL 생성."""
    client = get_minio_client()
    # object_path: "marketplace/abc123.zip"
    parts = object_path.split("/", 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else parts[0]

    return client.presigned_get_object(bucket, object_name, expires=expires)


def get_object_stream(object_path: str):
    """MinIO 오브젝트를 스트리밍으로 반환.

    Returns:
        (response, content_type, size) 튜플.
        response는 urllib3.HTTPResponse — 반드시 close() 필요 (StreamingResponse가 처리).
    """
    client = get_minio_client()
    parts = object_path.split("/", 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else parts[0]

    stat = client.stat_object(bucket, object_name)
    content_type = stat.content_type or "application/octet-stream"
    size = stat.size or 0

    response = client.get_object(bucket, object_name)
    return response, content_type, size
