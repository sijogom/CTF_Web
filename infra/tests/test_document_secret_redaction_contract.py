"""공개 소스에 내부 문서·목업이 다시 포함되지 않도록 확인한다."""
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class PublicSourceBoundaryTests(unittest.TestCase):
    def test_internal_material_is_not_distributed(self):
        for name in ("docs", "mockups", "MEMORY.md"):
            self.assertFalse((PROJECT_ROOT / name).exists(), name)

if __name__ == "__main__":
    unittest.main()
