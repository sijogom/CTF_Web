import asyncio
import unittest
from unittest.mock import patch

from app.utils import translator


class RichContentTranslationTests(unittest.TestCase):
    def test_translates_text_nodes_and_preserves_boundaries_and_references(self) -> None:
        source = (
            '<p><strong>중요</strong> 내용 '
            '<a href="https://example.org/x?q=1&amp;y=2">https://example.org/x?q=1&amp;y=2</a> '
            '<a href="/next">이동</a><code>let x = 1</code>'
            '<img src="/lesson-assets/a.png?X-Signature=AbC123" alt="도식"></p>'
        )

        async def trimmed_stub(text: str, source_lang: str, target_lang: str) -> str:
            return f"T[{text.strip()}]"

        with patch.object(translator, "translate_text", new=trimmed_stub):
            result = asyncio.run(translator.auto_translate_rich_content(source, "ko"))

        self.assertEqual(result["ko"], source)
        expected = (
            '<p><strong>T[중요]</strong> T[내용] '
            '<a href="https://example.org/x?q=1&amp;y=2">https://example.org/x?q=1&amp;y=2</a> '
            '<a href="/next">T[이동]</a><code>let x = 1</code>'
            '<img src="/lesson-assets/a.png?X-Signature=AbC123" alt="도식"></p>'
        )
        self.assertEqual(result["en"], expected)
        self.assertEqual(result["ja"], expected)
        self.assertEqual(result["zh"], expected)

    def test_failed_language_is_not_cached_as_success(self) -> None:
        async def partial_failure(text: str, source_lang: str, target_lang: str) -> str:
            if target_lang == "en":
                raise TimeoutError("synthetic timeout")
            return f"{target_lang}:{text}"

        with patch.object(translator, "translate_text", new=partial_failure):
            result = asyncio.run(
                translator.auto_translate_rich_content("<p>본문</p>", "ko")
            )

        self.assertEqual(result["ko"], "<p>본문</p>")
        self.assertNotIn("en", result)
        self.assertIn("ja", result)
        self.assertIn("zh", result)

    def test_external_translation_concurrency_is_bounded(self) -> None:
        active = 0
        maximum = 0

        async def measured_stub(text: str, source_lang: str, target_lang: str) -> str:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return text

        source = "".join(f"<p>node {index}</p>" for index in range(12))
        with patch.object(translator, "translate_text", new=measured_stub):
            asyncio.run(translator.auto_translate_rich_content(source, "ko"))

        self.assertLessEqual(maximum, translator.MAX_RICH_TRANSLATION_CONCURRENCY)


if __name__ == "__main__":
    unittest.main()

