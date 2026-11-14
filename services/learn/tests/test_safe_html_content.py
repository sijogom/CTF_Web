from __future__ import annotations

import unittest

from app.utils.safe_html import sanitize_rich_content


class SafeHtmlContentTests(unittest.TestCase):
    def test_tiptap_html_is_kept_with_only_safe_attributes(self) -> None:
        value = sanitize_rich_content(
            '<p class="prose">안내 <strong>내용</strong> '
            '<a href="https://example.org" target="_blank">링크</a></p>',
            field_name="본문",
        )
        self.assertIn("<strong>내용</strong>", value)
        self.assertIn('rel="noopener noreferrer"', value)
        self.assertNotIn("onclick", value)
        with self.assertRaises(ValueError):
            sanitize_rich_content('<p onclick="alert(1)">bad</p>', field_name="본문")

    def test_script_and_unsafe_urls_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_rich_content("<script>alert(1)</script>", field_name="본문")
        with self.assertRaises(ValueError):
            sanitize_rich_content('<a href="javascript:alert(1)">bad</a>', field_name="본문")

    def test_legacy_markdown_is_converted_to_canonical_html(self) -> None:
        self.assertEqual(
            sanitize_rich_content("## 기존 본문\n\n[안전한 링크](https://example.org)", field_name="본문"),
            '<h2>기존 본문</h2>\n<p><a href="https://example.org">안전한 링크</a></p>',
        )

    def test_empty_editor_html_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            sanitize_rich_content("<p></p>", field_name="본문")


if __name__ == "__main__":
    unittest.main()
