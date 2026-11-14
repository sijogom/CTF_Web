"""Deterministic revision and rich-content policy for editable course metadata."""

from __future__ import annotations

import hashlib
import json

from app.utils.rich_content import normalize_rich_content_for_write


def course_metadata_revision(title: str, description: str | None) -> str:
    """Hash the exact title/description pair using the public JSON contract."""

    payload = json.dumps(
        [title, description],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_course_description_for_write(description: str | None) -> str | None:
    """Canonicalize a provided or retained course description before a write."""

    if description is None:
        return None
    return normalize_rich_content_for_write(
        description,
        field_name="코스 설명",
        allow_empty=True,
    )
