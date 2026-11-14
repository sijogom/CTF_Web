"""레슨 편집기 이미지의 확장자와 최소 파일 서명 검증."""

from __future__ import annotations


_IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
}


def validate_lesson_image_bytes(*, data: bytes, extension: str) -> str | None:
    """Return a safe validation error when an image does not match its extension.

    UploadFile.content_type is controlled by the client, so it cannot be the
    basis for the storage content type.  This intentionally performs only a
    small, dependency-free signature check; a full image decoder/antivirus
    scan belongs to the deployment-time malware scanning pipeline and must not
    be claimed by this helper.
    """

    normalized_extension = extension.lower()
    if normalized_extension == ".webp":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            return "파일 내용이 WebP 이미지 형식과 일치하지 않습니다."
        return None

    signatures = _IMAGE_SIGNATURES.get(normalized_extension)
    if signatures is None:
        return "허용되지 않는 이미지 형식입니다."
    if not any(data.startswith(signature) for signature in signatures):
        return "파일 내용이 확장자에 맞는 이미지 형식과 일치하지 않습니다."
    return None
