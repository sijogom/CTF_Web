import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.ban import BanAppealReview, ManualBanCreate
from app.utils.ban_state import is_ban_active


class ManualBanSchemaTests(unittest.TestCase):
    def test_temporary_ban_requires_positive_duration(self):
        body = ManualBanCreate(
            target_user_id=uuid4(),
            public_reason="반복된 자동 플래그 대입",
            evidence_summary="관리자 검토에서 동일한 자동화 요청 패턴을 확인했습니다.",
            policy_version="manual-review-v1",
            duration_hours=12,
        )
        self.assertFalse(body.permanent)
        self.assertEqual(body.duration_hours, 12)

    def test_permanent_ban_cannot_include_a_duration(self):
        with self.assertRaises(ValidationError):
            ManualBanCreate(
                target_user_id=uuid4(),
                public_reason="중대한 정책 위반",
                evidence_summary="관리자 검토에서 충분한 근거와 적용 정책을 확인했습니다.",
                policy_version="manual-review-v1",
                permanent=True,
                duration_hours=12,
            )

    def test_review_cannot_lift_a_rejected_appeal(self):
        with self.assertRaises(ValidationError):
            BanAppealReview(
                decision="rejected",
                reviewer_note="근거를 재검토했으나 밴 유지가 필요합니다.",
                lift_ban=True,
                lift_reason="해제하지 않습니다.",
            )


class BanStateTests(unittest.TestCase):
    def test_active_state_requires_unlifted_and_unexpired_window(self):
        now = datetime.now(timezone.utc)
        active = SimpleNamespace(
            started_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            lifted_at=None,
        )
        expired = SimpleNamespace(
            started_at=now - timedelta(hours=2),
            expires_at=now - timedelta(minutes=1),
            lifted_at=None,
        )
        lifted = SimpleNamespace(
            started_at=now - timedelta(hours=2),
            expires_at=None,
            lifted_at=now - timedelta(minutes=1),
        )

        self.assertTrue(is_ban_active(active, now=now))
        self.assertFalse(is_ban_active(expired, now=now))
        self.assertFalse(is_ban_active(lifted, now=now))


if __name__ == "__main__":
    unittest.main()
