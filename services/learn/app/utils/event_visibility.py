"""플랫폼 이벤트의 UTC 공개·진행 상태 순수 함수."""

from __future__ import annotations

from datetime import datetime, timezone


UTC = timezone.utc


def normalize_utc_datetime(value: datetime, *, field_name: str) -> datetime:
    """timezone-aware datetime만 받고 UTC로 정규화한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}에는 시간대가 포함된 UTC 시각이 필요합니다.")
    return value.astimezone(UTC)


def validate_event_schedule(
    *,
    starts_at: datetime,
    ends_at: datetime,
    visible_from_at: datetime,
    visible_until_at: datetime | None,
) -> None:
    """행사와 공개 기간의 순서를 검증한다."""
    if ends_at <= starts_at:
        raise ValueError("종료 시각은 시작 시각보다 뒤여야 합니다.")
    if visible_until_at is not None and visible_until_at <= visible_from_at:
        raise ValueError("공개 종료 시각은 공개 시작 시각보다 뒤여야 합니다.")


def is_publicly_visible(
    *,
    status: str,
    visible_from_at: datetime,
    visible_until_at: datetime | None,
    now: datetime,
) -> bool:
    """서버 기준 공개 조건을 판정한다."""
    return (
        status == "published"
        and visible_from_at <= now
        and (visible_until_at is None or now < visible_until_at)
    )


def event_phase(*, starts_at: datetime, ends_at: datetime, now: datetime) -> str:
    """UTC 기준 예정·진행·종료 상태를 계산한다."""
    if now < starts_at:
        return "upcoming"
    if now >= ends_at:
        return "ended"
    return "active"
