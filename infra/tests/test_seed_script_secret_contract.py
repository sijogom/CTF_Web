"""공개 소스에는 데모 챌린지 시드를 포함하지 않는다."""
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class SeedPublicationTests(unittest.TestCase):
    def test_demo_seed_is_not_distributed(self):
        self.assertFalse((PROJECT_ROOT / "services/learn/seed_challenges.py").exists())

if __name__ == "__main__":
    unittest.main()
