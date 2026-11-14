"""문의 HTML 작성·저장·표시 경계를 DB 없이 검증한다."""

from pathlib import Path
import importlib.util
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _load_safe_html():
    source_path = SERVICE_ROOT / "app" / "utils" / "safe_html.py"
    spec = importlib.util.spec_from_file_location("community_safe_html", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RichContentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.safe_html = _load_safe_html()

    def test_plain_legacy_content_is_converted_to_canonical_html(self) -> None:
        self.assertEqual(
            self.safe_html.sanitize_rich_content("기존 문의 본문", field_name="문의 내용"),
            "<p>기존 문의 본문</p>",
        )

    def test_allowed_rich_content_is_normalized(self) -> None:
        result = self.safe_html.sanitize_rich_content(
            '<p class="lead">안내 <strong>내용</strong></p>',
            field_name="문의 내용",
        )
        self.assertIn("<p class=\"lead\">", result)
        self.assertIn("<strong>내용</strong>", result)

    def test_dangerous_tags_attributes_and_urls_are_rejected(self) -> None:
        for value in (
            "<script>alert(1)</script>",
            '<p onclick="alert(1)">x</p>',
            '<a href="javascript:alert(1)">x</a>',
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.safe_html.sanitize_rich_content(value, field_name="답변 내용")

    def test_inquiry_api_and_all_viewers_use_the_same_html_contract(self) -> None:
        api = (SERVICE_ROOT / "app" / "api" / "inquiries.py").read_text(encoding="utf-8")
        self.assertIn("from app.utils.safe_html import sanitize_rich_content", api)
        self.assertIn('field_name="문의 내용"', api)
        self.assertIn('field_name="답변 내용"', api)
        self.assertGreaterEqual(api.count("max_visible_chars=5000"), 2)
        for relative_path in (
            "../../frontend/src/app/[locale]/inquiry/page.tsx",
            "../../frontend/src/app/[locale]/inquiry/access/page.tsx",
            "../../frontend/src/app/[locale]/admin/inquiries/page.tsx",
        ):
            # This test runs from the service tree; the frontend lives beside it.
            source_path = SERVICE_ROOT / relative_path
            source = source_path.resolve().read_text(encoding="utf-8")
            self.assertIn("MarkdownRenderer", source)
        inquiry_page = (SERVICE_ROOT / "../../frontend/src/app/[locale]/inquiry/page.tsx").resolve().read_text(encoding="utf-8")
        admin_page = (SERVICE_ROOT / "../../frontend/src/app/[locale]/admin/inquiries/page.tsx").resolve().read_text(encoding="utf-8")
        self.assertIn("RichTextEditor", inquiry_page)
        self.assertIn("RichTextEditor", admin_page)

    def test_posts_and_marketplace_write_models_use_the_same_sanitizer(self) -> None:
        for relative_path, field_names in (
            ("app/api/posts.py", ("게시글 본문", "댓글 내용")),
            ("app/api/marketplace.py", ("상품 설명", "장터 댓글")),
        ):
            source = (SERVICE_ROOT / relative_path).read_text(encoding="utf-8")
            with self.subTest(relative_path=relative_path):
                self.assertIn("from app.utils.safe_html import", source)
                self.assertIn("sanitize_rich_content", source)
                for field_name in field_names:
                    self.assertIn(f'field_name="{field_name}"', source)


if __name__ == "__main__":
    unittest.main()
