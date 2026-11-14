from __future__ import annotations

import unittest
from pathlib import Path


class ChallengeRichContentContractTests(unittest.TestCase):
    def test_schema_sanitizes_description_for_create_and_update(self) -> None:
        source = Path(__file__).parents[1] / "app" / "schemas" / "challenge.py"
        text = source.read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count('@field_validator("description")'), 2)
        self.assertGreaterEqual(text.count('field_name="문제 설명"'), 2)
        self.assertGreaterEqual(text.count("max_visible_chars=10000"), 2)

    def test_preview_renders_description_and_hint_as_safe_content(self) -> None:
        source = (
            Path(__file__).parents[3]
            / "frontend"
            / "src"
            / "components"
            / "admin"
            / "challenges"
            / "StepPreview.tsx"
        )
        text = source.read_text(encoding="utf-8")
        self.assertIn('import { MarkdownRenderer }', text)
        self.assertGreaterEqual(text.count("<MarkdownRenderer content="), 2)


if __name__ == "__main__":
    unittest.main()
