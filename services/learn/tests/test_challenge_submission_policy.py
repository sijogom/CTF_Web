import sys
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.challenge_submission_policy import (  # noqa: E402
    user_submission_flag_type_is_allowed,
)


class ChallengeSubmissionFlagPolicyTests(unittest.TestCase):
    def test_regular_submission_allows_only_legacy_non_instance_modes(self) -> None:
        self.assertTrue(user_submission_flag_type_is_allowed("static"))
        self.assertTrue(user_submission_flag_type_is_allowed("regex"))
        self.assertFalse(user_submission_flag_type_is_allowed("dynamic_instance"))
        self.assertFalse(user_submission_flag_type_is_allowed("dynamic"))
        self.assertFalse(user_submission_flag_type_is_allowed(""))


if __name__ == "__main__":
    unittest.main()
