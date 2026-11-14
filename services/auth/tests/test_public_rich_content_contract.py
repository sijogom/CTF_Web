from __future__ import annotations

import unittest

from app.utils.safe_html import sanitize_rich_content, to_safe_rich_html


class PublicRichContentContractTests(unittest.TestCase):
    def test_html_is_normalized_and_unsafe_markup_is_rejected(self) -> None:
        value = sanitize_rich_content(
            '<p>공지 <strong>내용</strong> <a href="https://example.org" target="_blank">링크</a></p>',
            field_name="본문",
        )
        self.assertIn("<strong>내용</strong>", value)
        self.assertIn('rel="noopener noreferrer"', value)
        with self.assertRaises(ValueError):
            sanitize_rich_content('<p onclick="alert(1)">bad</p>', field_name="본문")

    def test_legacy_plain_text_is_wrapped_for_email_html(self) -> None:
        self.assertEqual(
            to_safe_rich_html("첫 줄\n둘째 줄", field_name="이메일 본문"),
            "<p>첫 줄<br>\n둘째 줄</p>",
        )

    def test_empty_optional_content_remains_empty(self) -> None:
        self.assertEqual(sanitize_rich_content("", field_name="알림 내용", allow_empty=True), "")


if __name__ == "__main__":
    unittest.main()
