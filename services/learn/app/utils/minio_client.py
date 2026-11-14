"""MinIO 파일 스토리지 클라이언트 — 챌린지 파일 + 레슨 에셋 관리."""

import io
import hashlib
import logging
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta

from minio import Minio
from minio.error import S3Error

from app.config import settings
from app.utils.clamd_scanner import (
    ClamdMalwareDetectedError,
    ClamdScannerUnavailableError,
    scan_stream_with_clamd,
)

logger = logging.getLogger(__name__)

_client: Minio | None = None

CHALLENGES_BUCKET = "challenges"
CHALLENGE_PENDING_BUCKET = "challenge-pending"
LESSON_ASSETS_BUCKET = "lesson-assets"
# 교육 자료는 레슨 에셋과 버킷·prefix를 분리한다. 이 버킷을 공개 정책으로 바꾸지
# 않으며, 모든 다운로드는 education API의 인가된 스트리밍 경로만 통과한다.
EDUCATION_ASSETS_BUCKET = "education-private-assets"
CHALLENGE_PRIVATE_BUCKET = "challenge-private-assets"

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_ATTACHMENT_SIZE = 50 * 1024 * 1024  # 50MB
MAX_ATTACHMENTS_PER_LESSON = 10
# "교수 파일 업로드 제한 해제"를 무제한 object storage로 해석하지 않는다. 아래
# 플랫폼 전역 안전 상한은 교육 API의 수업 단위 quota와 함께 적용된다.
MAX_EDUCATION_ASSET_SIZE = 250 * 1024 * 1024  # 파일 하나 250 MiB
MAX_EDUCATION_ASSET_CLASS_TOTAL_SIZE = 2 * 1024 * 1024 * 1024  # 수업 하나 2 GiB
MAX_EDUCATION_ASSETS_PER_CLASS = 200


class BuildContextStorageUnavailableError(RuntimeError):
    """Raised when a build-context read cannot be completed safely.

    Callers must surface only the stable error code, never a storage or
    presigned-URL failure detail.  In particular, a failed list operation must
    not be represented as an empty context because that would permit an
    unintended legacy Dockerfile fallback.
    """

    def __init__(self) -> None:
        super().__init__("build context storage unavailable")


class EducationAssetScannerUnavailableError(RuntimeError):
    """The malware scanner could not make a trustworthy clean verdict."""

    def __init__(self) -> None:
        super().__init__("education asset scanner is not ready")


class EducationAssetMalwareDetectedError(RuntimeError):
    """The scanner returned a malware or configured-limit detection verdict."""

    def __init__(self) -> None:
        # Do not persist or expose the signature text. It is not necessary for
        # the uploader to act on the result and unnecessarily reveals internals.
        super().__init__("education asset malware detected")


# 빌드 컨텍스트 파일 제한
MAX_BUILD_FILE_SIZE = 50 * 1024 * 1024  # 개별 파일 50MB
MAX_BUILD_TOTAL_SIZE = 100 * 1024 * 1024  # 전체 합산 100MB

# 허용 확장자 (보안: 실행 파일 차단)
ALLOWED_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".tar.gz", ".7z",  # 아카이브
    ".py", ".c", ".cpp", ".h", ".js", ".ts", ".go", ".rs", ".java",  # 소스코드
    ".txt", ".md", ".pdf", ".json", ".yaml", ".yml", ".xml",  # 문서
    ".png", ".jpg", ".jpeg", ".gif", ".svg",  # 이미지
    ".elf", ".bin",  # CTF 바이너리 (의도적 허용)
    ".pcap", ".pcapng",  # 포렌식
    ".dockerfile",  # 도커
}

# 레슨 이미지 허용 확장자 (SVG 제외 — XSS 위험)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

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
}

# 레슨 첨부파일 허용 확장자
ATTACHMENT_EXTENSIONS = ALLOWED_EXTENSIONS | {".html", ".css", ".webp"}

# 차단 확장자 (서버 실행 가능 파일)
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".sh", ".bash",
    ".msi", ".dll", ".so", ".dylib",
    ".php", ".jsp", ".asp", ".aspx", ".cgi",
}


def get_minio_client() -> Minio:
    """MinIO 클라이언트 싱글턴."""
    global _client
    if _client is None:
        _client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
    return _client


def ensure_buckets() -> None:
    """필요한 버킷을 생성."""
    client = get_minio_client()
    for bucket in (
        CHALLENGES_BUCKET,
        CHALLENGE_PENDING_BUCKET,
        LESSON_ASSETS_BUCKET,
        EDUCATION_ASSETS_BUCKET,
    ):
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
                logger.info("MinIO 버킷 생성: %s", bucket)
        except S3Error:
            logger.warning("MinIO 버킷 생성 실패: %s", bucket, exc_info=True)


def validate_filename(filename: str) -> str | None:
    """파일명 보안 검증. 문제가 있으면 에러 메시지 반환, 없으면 None."""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return "유효하지 않은 파일명입니다."
    if "\x00" in filename:
        return "유효하지 않은 파일명입니다."

    ext = ""
    if "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()

    if ext in BLOCKED_EXTENSIONS:
        return f"보안상 {ext} 파일은 업로드할 수 없습니다."

    return None


def ensure_challenge_private_bucket() -> None:
    """새 버킷의 기본 private 정책을 유지하고 기존 정책은 덮어쓰지 않는다."""
    client = get_minio_client()
    if not client.bucket_exists(CHALLENGE_PRIVATE_BUCKET):
        try:
            client.make_bucket(CHALLENGE_PRIVATE_BUCKET)
        except S3Error as error:
            if error.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise
    try:
        policy = client.get_bucket_policy(CHALLENGE_PRIVATE_BUCKET)
    except S3Error as error:
        if error.code != "NoSuchBucketPolicy":
            raise
    else:
        if policy and policy.strip():
            raise RuntimeError("challenge private bucket has an unexpected policy")


def put_private_challenge_object(object_path: str, stream, size: int, content_type: str) -> None:
    ensure_challenge_private_bucket()
    bucket, key = object_path.split("/", 1)
    if bucket != CHALLENGE_PRIVATE_BUCKET or not key.startswith("attachments/"):
        raise ValueError("invalid challenge attachment path")
    stream.seek(0)
    get_minio_client().put_object(bucket, key, stream, size, content_type=content_type)


def stat_challenge_object(object_path: str):
    bucket, key = object_path.split("/", 1)
    try:
        return get_minio_client().stat_object(bucket, key)
    except S3Error as error:
        if error.code == "NoSuchKey":
            return None
        raise


def verify_challenge_object(object_path: str, size: int, sha256: str) -> str:
    """없음과 불명을 구분한다. 불일치는 절대 재PUT 허용 근거가 아니다."""
    stat = stat_challenge_object(object_path)
    if stat is None:
        return "missing"
    if stat.size != size:
        raise ValueError("challenge attachment size mismatch")
    bucket, key = object_path.split("/", 1)
    response = get_minio_client().get_object(bucket, key)
    try:
        digest, actual_size = hashlib.sha256(), 0
        for chunk in response.stream(64 * 1024):
            actual_size += len(chunk)
            if actual_size > size:
                raise ValueError("challenge attachment size mismatch")
            digest.update(chunk)
        if actual_size != size or digest.hexdigest() != sha256:
            raise ValueError("challenge attachment hash mismatch")
    finally:
        close_object_stream(response)
    return "verified"


def copy_pending_challenge_object(source: str, destination: str) -> None:
    """승인 복사는 원본 pending을 제거하지 않는다. DB rollback과 객체는 별개다."""
    from minio.commonconfig import CopySource
    ensure_challenge_private_bucket()
    source_bucket, source_key = source.split("/", 1)
    bucket, key = destination.split("/", 1)
    if source_bucket != CHALLENGE_PENDING_BUCKET or bucket != CHALLENGE_PRIVATE_BUCKET:
        raise ValueError("invalid pending attachment source")
    get_minio_client().copy_object(bucket, key, CopySource(source_bucket, source_key))


def get_file_extension(filename: str) -> str:
    """파일 확장자 추출 (소문자)."""
    if "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return ""


# ── 챌린지 파일 관련 ──


def upload_challenge_file(
    data: bytes,
    original_filename: str,
    category: str,
    difficulty: str,
    submission_id: str,
    content_type: str = "application/octet-stream",
    pending: bool = True,
) -> str:
    """챌린지 파일을 MinIO에 업로드.

    경로: {bucket}/{category}/{difficulty}/{submission_id}/{filename}
    """
    client = get_minio_client()
    ensure_buckets()

    bucket = CHALLENGE_PENDING_BUCKET if pending else CHALLENGES_BUCKET

    ext = get_file_extension(original_filename)
    safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    object_name = f"{category}/{difficulty}/{submission_id}/{safe_name}"

    # 확장자 기반 Content-Type 결정 (클라이언트 값은 폴백으로만 사용)
    safe_content_type = EXTENSION_CONTENT_TYPE.get(ext, content_type)

    client.put_object(
        bucket,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=safe_content_type,
    )

    return f"{bucket}/{object_name}"


def move_to_approved(
    pending_path: str,
    challenge_id: int,
    category: str,
    difficulty: str,
) -> str:
    """pending 버킷에서 approved 버킷으로 파일 이동."""
    client = get_minio_client()

    parts = pending_path.split("/", 1)
    src_bucket = parts[0]
    src_object = parts[1] if len(parts) > 1 else parts[0]

    filename = src_object.rsplit("/", 1)[-1]
    dest_object = f"{category}/{difficulty}/{challenge_id}/{filename}"

    from minio.commonconfig import CopySource
    client.copy_object(
        CHALLENGES_BUCKET,
        dest_object,
        CopySource(src_bucket, src_object),
    )
    client.remove_object(src_bucket, src_object)

    return f"{CHALLENGES_BUCKET}/{dest_object}"


def delete_pending_files(submission_id: str, category: str, difficulty: str) -> None:
    """거부된 제출의 파일을 삭제."""
    client = get_minio_client()
    prefix = f"{category}/{difficulty}/{submission_id}/"

    try:
        objects = client.list_objects(CHALLENGE_PENDING_BUCKET, prefix=prefix, recursive=True)
        for obj in objects:
            client.remove_object(CHALLENGE_PENDING_BUCKET, obj.object_name)
    except S3Error:
        logger.warning("pending 파일 삭제 실패: %s", prefix, exc_info=True)


def get_presigned_url(
    object_path: str,
    expires: timedelta = timedelta(hours=1),
) -> str:
    """presigned download URL 생성."""
    client = get_minio_client()
    parts = object_path.split("/", 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else parts[0]
    return client.presigned_get_object(bucket, object_name, expires=expires)


# ── 레슨 이미지/첨부파일 관련 ──


def upload_lesson_image(
    data: bytes,
    filename: str,
) -> dict[str, str]:
    """레슨 에디터에서 이미지 업로드 → 내부 경로 + 마크다운 텍스트 반환."""
    client = get_minio_client()
    ensure_buckets()

    ext = get_file_extension(filename)
    # 서버가 Content-Type 결정 (C-2: 클라이언트 값 신뢰 안 함)
    safe_content_type = EXTENSION_CONTENT_TYPE.get(ext, "application/octet-stream")
    folder_id = uuid.uuid4().hex[:12]
    safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    object_name = f"images/{folder_id}/{safe_name}"

    client.put_object(
        LESSON_ASSETS_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=safe_content_type,
    )

    # I-1: 내부 경로 저장 (presigned URL 만료 방지)
    import os
    base = os.environ.get("BASE_PATH", "")
    internal_path = f"{base}/api/learn/lesson-images/{folder_id}/{safe_name}"

    return {
        "url": internal_path,
        "object_path": f"{LESSON_ASSETS_BUCKET}/{object_name}",
        "markdown": f"![{filename}]({internal_path})",
    }


_LESSON_IMAGE_OBJECT_PATH_PATTERN = re.compile(
    r"^lesson-assets/images/[a-f0-9]{12}/[a-f0-9]{8}\.(png|jpg|jpeg|gif|webp)$"
)


def delete_lesson_image(object_path: str) -> None:
    """본문 이미지 lifecycle worker 전용 삭제 경로.

    일반 첨부파일 삭제 helper와 분리해 prefix를 검증하고, 저장소 오류를
    호출자에게 전달한다. worker가 재시도할 수 있어야 하므로 예외를 삼키지 않는다.
    """
    if not _LESSON_IMAGE_OBJECT_PATH_PATTERN.fullmatch(object_path):
        raise ValueError("invalid_lesson_image_object_path")
    bucket, object_name = object_path.split("/", 1)
    get_minio_client().remove_object(bucket, object_name)


def upload_lesson_attachment(
    data: bytes,
    filename: str,
    lesson_id: str,
) -> dict[str, str]:
    """레슨 첨부파일 업로드 → 메타데이터 반환."""
    client = get_minio_client()
    ensure_buckets()

    ext = get_file_extension(filename)
    safe_content_type = EXTENSION_CONTENT_TYPE.get(ext, "application/octet-stream")
    safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    object_name = f"attachments/{lesson_id}/{safe_name}"

    client.put_object(
        LESSON_ASSETS_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=safe_content_type,
    )

    return {
        "filename": safe_name,
        "original_name": filename,
        "size": len(data),
        "content_type": safe_content_type,
        "object_path": f"{LESSON_ASSETS_BUCKET}/{object_name}",
    }


def delete_lesson_files(lesson_id: str) -> None:
    """레슨 삭제 시 관련 첨부파일 정리."""
    client = get_minio_client()
    prefix = f"attachments/{lesson_id}/"

    try:
        objects = client.list_objects(LESSON_ASSETS_BUCKET, prefix=prefix, recursive=True)
        for obj in objects:
            client.remove_object(LESSON_ASSETS_BUCKET, obj.object_name)
        logger.info("레슨 첨부파일 삭제 완료: %s", prefix)
    except S3Error:
        logger.warning("레슨 첨부파일 삭제 실패: %s", prefix, exc_info=True)


def delete_lesson_attachment(object_path: str) -> None:
    """특정 첨부파일 1개 삭제."""
    client = get_minio_client()
    parts = object_path.split("/", 1)
    bucket = parts[0]
    object_name = parts[1] if len(parts) > 1 else parts[0]

    try:
        client.remove_object(bucket, object_name)
    except S3Error:
        logger.warning("첨부파일 삭제 실패: %s", object_path, exc_info=True)


def get_lesson_file_url(
    object_path: str,
    expires: timedelta = timedelta(hours=1),
) -> str:
    """레슨 파일의 presigned download URL 생성."""
    return get_presigned_url(object_path, expires)


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


def close_object_stream(stream) -> None:
    """Release a MinIO/urllib3 response even if a client disconnects early."""
    try:
        stream.close()
    except Exception:
        logger.warning("MinIO object stream close failed", exc_info=True)
    try:
        stream.release_conn()
    except Exception:
        logger.warning("MinIO object stream connection release failed", exc_info=True)


def iter_object_stream(stream, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Yield bounded chunks and always return the underlying HTTP connection."""
    try:
        while chunk := stream.read(chunk_size):
            yield chunk
    finally:
        close_object_stream(stream)


# ── 교육 private 자료 관련 ──


# 학생 제출 파일은 V1 범위가 아니다. 교수 자료도 브라우저에서 실행될 수 있는 active
# content는 허용하지 않으며, 서버의 Content-Disposition attachment와 함께 제공한다.
EDUCATION_ASSET_EXTENSIONS = {
    ".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml",
    ".zip", ".tar", ".gz", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".py", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".js", ".ts",
    ".pcap", ".pcapng",
}
_EDUCATION_ASSET_UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_EDUCATION_ASSET_OBJECT_PATH_PATTERN = re.compile(
    rf"^{re.escape(EDUCATION_ASSETS_BUCKET)}/classes/{_EDUCATION_ASSET_UUID_PATTERN}/"
    rf"(?:class-assets/{_EDUCATION_ASSET_UUID_PATTERN}|"
    rf"assignments/{_EDUCATION_ASSET_UUID_PATTERN}/{_EDUCATION_ASSET_UUID_PATTERN})\.[a-z0-9]+$"
)


def validate_education_asset_filename(filename: str) -> str | None:
    """교육 private 자료 파일명을 allowlist·경로 규칙으로 검증한다."""
    error = validate_filename(filename)
    if error:
        return error
    extension = get_file_extension(filename)
    if extension not in EDUCATION_ASSET_EXTENSIONS:
        return "허용되지 않는 교육 자료 파일 형식입니다."
    return None


def _education_asset_object_name(
    *,
    class_id: str,
    asset_id: str,
    scope: str,
    assignment_id: str | None,
    extension: str,
) -> str:
    if scope == "class" and assignment_id is None:
        return f"classes/{class_id}/class-assets/{asset_id}{extension}"
    if scope == "assignment" and assignment_id:
        return f"classes/{class_id}/assignments/{assignment_id}/{asset_id}{extension}"
    raise ValueError("교육 자료 대상이 올바르지 않습니다.")


def upload_education_asset(
    *,
    data_stream,
    byte_size: int,
    original_filename: str,
    class_id: str,
    asset_id: str,
    scope: str,
    assignment_id: str | None,
) -> dict[str, str | int]:
    """private education bucket에 자료를 저장하고 내부 메타데이터만 반환한다.

    호출자는 파일 전체를 메모리에 모으지 않은 seekable spooled stream을 전달한다.
    object path는 DB 저장용이며 절대로 API 응답에 포함하지 않는다.
    """
    filename_error = validate_education_asset_filename(original_filename)
    if filename_error:
        raise ValueError(filename_error)
    if byte_size <= 0 or byte_size > MAX_EDUCATION_ASSET_SIZE:
        raise ValueError("교육 자료 파일 크기가 플랫폼 안전 상한을 벗어났습니다.")

    extension = get_file_extension(original_filename)
    object_name = _education_asset_object_name(
        class_id=class_id,
        asset_id=asset_id,
        scope=scope,
        assignment_id=assignment_id,
        extension=extension,
    )
    client = get_minio_client()
    ensure_buckets()
    data_stream.seek(0)
    client.put_object(
        EDUCATION_ASSETS_BUCKET,
        object_name,
        data_stream,
        length=byte_size,
        content_type=EXTENSION_CONTENT_TYPE.get(extension, "application/octet-stream"),
    )
    return {
        "object_path": f"{EDUCATION_ASSETS_BUCKET}/{object_name}",
        "stored_name": f"{asset_id}{extension}",
        "original_name": original_filename,
        "content_type": EXTENSION_CONTENT_TYPE.get(extension, "application/octet-stream"),
        "byte_size": byte_size,
    }


def scan_education_asset_before_storage(
    *,
    data_stream,
    byte_size: int,
    original_filename: str,
) -> str:
    """Return a trustworthy scanner state before an asset reaches MinIO.

    The scanner is an internal clamd service.  Its TCP protocol is intentionally
    used only on the private Compose network and every network, timeout, stream
    length, or reply-format failure denies storage.  ``UploadFile.file`` is
    already a seekable multipart spool, so this scans bounded chunks and then
    rewinds it for MinIO instead of building another large in-memory copy.

    The development bypass remains intentionally explicit for local interface
    work. It is ignored in production and never claims a clean verdict.
    """
    del original_filename
    if not settings.is_production and settings.education_asset_scan_development_bypass:
        try:
            data_stream.seek(0)
        except (AttributeError, OSError) as exc:
            raise EducationAssetScannerUnavailableError() from exc
        logger.warning("Education asset malware scan bypassed in explicit development mode")
        return "development_bypass"

    try:
        return scan_stream_with_clamd(
            data_stream=data_stream,
            byte_size=byte_size,
            max_byte_size=MAX_EDUCATION_ASSET_SIZE,
            host=settings.education_asset_scanner_host,
            port=settings.education_asset_scanner_port,
            connect_timeout_seconds=settings.education_asset_scanner_connect_timeout_seconds,
            io_timeout_seconds=settings.education_asset_scanner_io_timeout_seconds,
        )
    except ClamdMalwareDetectedError as exc:
        raise EducationAssetMalwareDetectedError() from exc
    except ClamdScannerUnavailableError as exc:
        raise EducationAssetScannerUnavailableError() from exc


def is_education_asset_object_path(object_path: str) -> bool:
    """DB 오염 시 private bucket 밖의 객체를 삭제·다운로드하지 않게 막는다."""
    return bool(_EDUCATION_ASSET_OBJECT_PATH_PATTERN.fullmatch(object_path))


def delete_education_asset(object_path: str) -> None:
    """교육 private 자료 하나를 삭제한다. 허용 prefix 밖 대상은 fail-closed."""
    if not is_education_asset_object_path(object_path):
        raise ValueError("교육 자료 저장 경로가 올바르지 않습니다.")
    bucket, object_name = object_path.split("/", 1)
    get_minio_client().remove_object(bucket, object_name)


# ── 빌드 컨텍스트 파일 관련 ──

# 빌드 컨텍스트 허용 확장자 (.sh 등 빌드에 필요한 파일 포함)
BUILD_CONTEXT_EXTENSIONS = {
    ".py", ".c", ".cpp", ".h", ".rs", ".go", ".java", ".js", ".ts",
    ".html", ".css", ".sql", ".json", ".yaml", ".yml", ".toml",
    ".txt", ".md", ".conf", ".cfg", ".ini", ".sh", ".bash",
    ".dockerfile", ".xml", ".rb", ".php", ".pl",
    ".zip", ".tar", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",  # 이미지 (웹 챌린지용)
    ".wav", ".mp3",  # 오디오 (포렌식 챌린지용)
    ".pcap", ".pcapng",  # 패킷 캡처
    ".elf", ".bin",  # CTF 바이너리
}


def validate_build_filename(filename: str) -> str | None:
    """빌드 컨텍스트 파일명 보안 검증. 문제가 있으면 에러 메시지 반환.

    하위 디렉토리 경로(예: ``app/main.py``, ``bot/utils/helper.py``)를 허용한다.
    경로 탐색 공격(``..``, 절대경로, 백슬래시)은 차단한다.
    """
    if not filename:
        return "유효하지 않은 파일명입니다."
    if "\x00" in filename:
        return "유효하지 않은 파일명입니다."
    # 경로 탐색 공격 방지
    if ".." in filename or "\\" in filename:
        return "유효하지 않은 파일명입니다."
    # 절대경로 차단
    if filename.startswith("/"):
        return "유효하지 않은 파일명입니다."
    # 경로의 마지막 부분(파일명)을 검사
    basename = filename.rsplit("/", 1)[-1]
    if not basename:
        return "유효하지 않은 파일명입니다."
    # Dockerfile (확장자 없음) 특별 허용
    if basename == "Dockerfile" or basename.startswith("Dockerfile."):
        return None
    ext = get_file_extension(basename)
    if not ext:
        return None  # 확장자 없는 파일도 허용 (Makefile 등)
    if ext not in BUILD_CONTEXT_EXTENSIONS:
        return f"빌드 컨텍스트에서 {ext} 파일은 허용되지 않습니다."
    return None


def upload_build_file(
    challenge_id: int,
    filename: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> dict:
    """빌드 컨텍스트 파일을 MinIO에 업로드."""
    client = get_minio_client()
    ensure_buckets()

    object_name = f"build-context/{challenge_id}/{filename}"
    ext = get_file_extension(filename)
    safe_content_type = EXTENSION_CONTENT_TYPE.get(ext, content_type)

    client.put_object(
        CHALLENGES_BUCKET,
        object_name,
        io.BytesIO(data),
        length=len(data),
        content_type=safe_content_type,
    )

    return {"name": filename, "size": len(data), "key": object_name}


def list_build_files(challenge_id: int) -> list[dict]:
    """빌드 컨텍스트 파일 목록 조회."""
    prefix = f"build-context/{challenge_id}/"
    result = []

    try:
        client = get_minio_client()
        objects = client.list_objects(CHALLENGES_BUCKET, prefix=prefix, recursive=True)
        for obj in objects:
            name = obj.object_name.removeprefix(prefix)
            if name:
                result.append({"name": name, "size": obj.size or 0})
    except Exception as error:
        logger.warning(
            "빌드 파일 목록 조회 실패: challenge=%d",
            challenge_id,
            exc_info=True,
        )
        raise BuildContextStorageUnavailableError() from error

    return result


def delete_build_file(challenge_id: int, filename: str) -> None:
    """특정 빌드 컨텍스트 파일 삭제."""
    client = get_minio_client()
    object_name = f"build-context/{challenge_id}/{filename}"
    try:
        client.remove_object(CHALLENGES_BUCKET, object_name)
    except Exception as error:
        logger.warning("빌드 파일 삭제 실패: %s", object_name, exc_info=True)
        # 삭제 실패를 성공으로 삼키면 다음 빌드가 오래된 컨텍스트를 다시
        # 읽을 수 있다. 호출자가 503으로 중단하고 재시도하도록 명시한다.
        raise BuildContextStorageUnavailableError() from error


def delete_all_build_files(challenge_id: int) -> None:
    """챌린지의 빌드 컨텍스트 파일 전체 삭제."""
    client = get_minio_client()
    prefix = f"build-context/{challenge_id}/"
    try:
        objects = client.list_objects(CHALLENGES_BUCKET, prefix=prefix, recursive=True)
        for obj in objects:
            client.remove_object(CHALLENGES_BUCKET, obj.object_name)
        logger.info("빌드 컨텍스트 파일 전체 삭제: challenge=%d", challenge_id)
    except Exception as error:
        logger.warning("빌드 컨텍스트 파일 삭제 실패: challenge=%d", challenge_id, exc_info=True)
        # DB 삭제를 계속 진행하면 object-store에 고아 컨텍스트가 남고,
        # 동일 challenge ID의 후속 빌드가 그 파일을 재사용할 수 있다.
        raise BuildContextStorageUnavailableError() from error


def get_build_file_urls(challenge_id: int) -> list[dict]:
    """빌드 파일별 presigned URL 반환."""
    prefix = f"build-context/{challenge_id}/"
    result = []

    try:
        client = get_minio_client()
        objects = client.list_objects(CHALLENGES_BUCKET, prefix=prefix, recursive=True)
        for obj in objects:
            name = obj.object_name.removeprefix(prefix)
            if name:
                url = client.presigned_get_object(
                    CHALLENGES_BUCKET, obj.object_name, expires=timedelta(minutes=30)
                )
                result.append({"name": name, "url": url})
    except Exception as error:
        logger.warning(
            "빌드 파일 URL 생성 실패: challenge=%d",
            challenge_id,
            exc_info=True,
        )
        raise BuildContextStorageUnavailableError() from error

    return result
