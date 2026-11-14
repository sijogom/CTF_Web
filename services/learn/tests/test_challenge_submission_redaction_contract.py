from pathlib import Path
import unittest


API_PATH = Path(__file__).parents[1] / "app" / "api" / "submissions.py"


class ChallengeSubmissionRedactionContractTests(unittest.TestCase):
    def test_detail_uses_redacted_flag_metadata_and_hides_minio_paths(self) -> None:
        source = API_PATH.read_text(encoding="utf-8")

        self.assertIn("build_challenge_submission_flag_metadata", source)
        self.assertIn("**flag_metadata", source)
        self.assertNotIn('"flag_value": sub.flag_value', source)
        self.assertNotIn('"path": path', source)


if __name__ == "__main__":
    unittest.main()
