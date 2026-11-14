"""플랫폼 이벤트 V1의 DB 비의존 계약 테스트."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

from pydantic import ValidationError


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.schemas.event import EventCreate, EventRestoreRequest, EventUpdate  # noqa: E402
from app.utils.event_visibility import event_phase, is_publicly_visible  # noqa: E402


UTC = timezone.utc


def _payload(**overrides) -> dict:
    baseline = {
        "title": "보안 워크숍",
        "summary": "플랫폼 보안 학습 행사",
        "content_markdown": "## 안내\n\n실습형 보안 워크숍입니다.",
        "category": "workshop",
        "location": "교내 강의실",
        "external_url": "https://example.org/events/security-workshop",
        "starts_at": datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        "ends_at": datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        "visible_from_at": datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
        "visible_until_at": datetime(2026, 8, 21, 0, 0, tzinfo=UTC),
        "status": "published",
    }
    baseline.update(overrides)
    return baseline


class EventSchemaTests(unittest.TestCase):
    def test_create_normalizes_offset_datetimes_to_utc(self) -> None:
        kst = timezone(timedelta(hours=9))
        event = EventCreate(
            **_payload(
                starts_at=datetime(2026, 8, 20, 18, 0, tzinfo=kst),
                ends_at=datetime(2026, 8, 20, 21, 0, tzinfo=kst),
                visible_from_at=datetime(2026, 8, 10, 9, 0, tzinfo=kst),
                visible_until_at=datetime(2026, 8, 21, 9, 0, tzinfo=kst),
            )
        )
        self.assertEqual(event.starts_at, datetime(2026, 8, 20, 9, 0, tzinfo=UTC))
        self.assertEqual(event.starts_at.tzinfo, UTC)

    def test_create_rejects_naive_or_invalid_schedule(self) -> None:
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(starts_at=datetime(2026, 8, 20, 9, 0)))
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(ends_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC)))
        with self.assertRaises(ValidationError):
            EventCreate(
                **_payload(visible_until_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC))
            )

    def test_content_and_external_url_follow_safe_input_contract(self) -> None:
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(content_markdown="<script>alert(1)</script>"))
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(content_markdown="[unsafe](javascript:alert(1))"))
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(external_url="https://user:password@example.org/event"))
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(external_url="ftp://example.org/event"))
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(external_url="https://example.org/\nnext"))
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(external_url=" https://example.org/event"))

    def test_write_requests_reject_unknown_fields_instead_of_silently_ignoring_them(self) -> None:
        with self.assertRaises(ValidationError):
            EventCreate(**_payload(unexpected_field="ignored-before"))
        with self.assertRaises(ValidationError):
            EventUpdate(unexpected_field="ignored-before")

    def test_update_only_allows_archive_via_archive_endpoint_contract(self) -> None:
        with self.assertRaises(ValidationError):
            EventUpdate(status="archived")
        self.assertEqual(EventUpdate(category="seminar").category, "seminar")
        self.assertEqual(EventRestoreRequest(status="published").status, "published")


class EventVisibilityTests(unittest.TestCase):
    def test_public_visibility_requires_published_and_open_window(self) -> None:
        now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
        kwargs = {
            "visible_from_at": datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
            "visible_until_at": datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
            "now": now,
        }
        self.assertTrue(is_publicly_visible(status="published", **kwargs))
        self.assertFalse(is_publicly_visible(status="draft", **kwargs))
        self.assertFalse(
            is_publicly_visible(
                status="published",
                visible_from_at=datetime(2026, 8, 16, 0, 0, tzinfo=UTC),
                visible_until_at=None,
                now=now,
            )
        )
        self.assertFalse(
            is_publicly_visible(
                status="published",
                visible_from_at=datetime(2026, 8, 10, 0, 0, tzinfo=UTC),
                visible_until_at=now,
                now=now,
            )
        )

    def test_event_phase_is_time_based_and_not_status_based(self) -> None:
        starts_at = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
        ends_at = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        self.assertEqual(
            event_phase(starts_at=starts_at, ends_at=ends_at, now=datetime(2026, 8, 20, 8, 59, tzinfo=UTC)),
            "upcoming",
        )
        self.assertEqual(
            event_phase(starts_at=starts_at, ends_at=ends_at, now=datetime(2026, 8, 20, 10, 0, tzinfo=UTC)),
            "active",
        )
        self.assertEqual(
            event_phase(starts_at=starts_at, ends_at=ends_at, now=ends_at),
            "ended",
        )


class EventSourceContractTests(unittest.TestCase):
    def test_router_enforces_server_side_roles_visibility_and_auditing(self) -> None:
        router_source = (SERVICE_ROOT / "app" / "api" / "events.py").read_text(encoding="utf-8")
        self.assertIn('require_role(*_EVENT_MANAGER_ROLES)', router_source)
        self.assertIn('is_publicly_visible(', router_source)
        self.assertIn('with_for_update()', router_source)
        self.assertIn('"event.create"', router_source)
        self.assertIn('"event.update"', router_source)
        self.assertIn('category: EventCategory | None = None', (SERVICE_ROOT / "app" / "schemas" / "event.py").read_text(encoding="utf-8"))
        self.assertIn('"changed_fields": sorted(updates)', router_source)
        self.assertIn('"event.archive"', router_source)
        self.assertIn('"event.restore"', router_source)
        self.assertIn('PlatformEventAuditEvent', router_source)
        self.assertNotIn('app.models.competition', router_source)
        self.assertNotIn('app.models.community', router_source)

    def test_model_migration_and_router_registration_are_additive(self) -> None:
        model_source = (SERVICE_ROOT / "app" / "models" / "event.py").read_text(encoding="utf-8")
        migration_source = (
            SERVICE_ROOT / "migrations" / "versions" / "20260813_009_platform_events.py"
        ).read_text(encoding="utf-8")
        main_source = (SERVICE_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        migration_env_source = (SERVICE_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

        self.assertIn('__tablename__ = "platform_events"', model_source)
        self.assertIn('__tablename__ = "platform_event_audit_events"', model_source)
        self.assertIn('ForeignKey("platform_events.id", ondelete="RESTRICT")', model_source)
        self.assertIn('down_revision: Union[str, None] = "20260813_008"', migration_source)
        self.assertIn('if event_count or audit_count:', migration_source)
        self.assertIn('destructive downgrade', migration_source)
        self.assertNotIn('competitions', migration_source)
        self.assertNotIn('community', migration_source.lower())
        self.assertIn('from app.api.events import router as events_router', main_source)
        self.assertIn('app.include_router(events_router, prefix="")', main_source)
        self.assertIn('import app.models.event', migration_env_source)


if __name__ == "__main__":
    unittest.main()
