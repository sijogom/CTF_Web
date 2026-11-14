"""챌린지 자동 번역 유틸리티.

Google Translate (deep-translator) 기반으로
챌린지 name/description을 지원 언어로 자동 번역합니다.
"""

import asyncio
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import logging
from functools import partial
import re

from deep_translator import GoogleTranslator

from app.utils.rich_content import normalize_rich_content_for_write

logger = logging.getLogger(__name__)

# 지원 언어 코드 (next-intl locale → Google Translate 언어 코드)
SUPPORTED_LANGS = {
    "ko": "ko",
    "en": "en",
    "ja": "ja",
    "zh": "zh-CN",
}
TRANSLATION_TIMEOUT_SECONDS = 20.0
MAX_RICH_TRANSLATION_CONCURRENCY = 4
_REFERENCE_TEXT_PATTERN = re.compile(
    r"^(?:https?://|mailto:|/|#|\.\.?/|[^\s@]+@[^\s@]+\.[^\s@]+)",
    re.IGNORECASE,
)


def _translate_sync(text: str, source: str, target: str) -> str:
    """동기 번역 (스레드풀에서 실행)."""
    if not text or not text.strip():
        return text
    translated = GoogleTranslator(source=source, target=target).translate(text)
    if not isinstance(translated, str) or not translated.strip():
        raise RuntimeError("translation returned no text")
    return translated


async def translate_text(text: str, source_lang: str, target_lang: str) -> str:
    """비동기 번역 — 블로킹 I/O를 스레드풀로 위임."""
    if source_lang == target_lang:
        return text
    src = SUPPORTED_LANGS.get(source_lang, source_lang)
    tgt = SUPPORTED_LANGS.get(target_lang, target_lang)
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, partial(_translate_sync, text, src, tgt)),
        timeout=TRANSLATION_TIMEOUT_SECONDS,
    )


async def auto_translate(text: str, source_lang: str = "ko") -> dict[str, str]:
    """텍스트를 모든 지원 언어로 번역하여 dict 반환.

    Returns:
        {"ko": "원문", "en": "translated", "ja": "翻訳", ...}
    """
    result: dict[str, str] = {}
    tasks = []
    target_langs = []

    for lang_key in SUPPORTED_LANGS:
        if lang_key == source_lang:
            result[lang_key] = text
        else:
            target_langs.append(lang_key)
            tasks.append(translate_text(text, source_lang, lang_key))

    if tasks:
        translations = await asyncio.gather(*tasks, return_exceptions=True)
        for lang_key, translated in zip(target_langs, translations):
            if isinstance(translated, Exception):
                logger.warning("번역 예외 (%s): %s", lang_key, type(translated).__name__)
            else:
                result[lang_key] = translated

    return result


@dataclass(frozen=True)
class _TextSlot:
    index: int


class _RichTextTemplateParser(HTMLParser):
    """Split only visible text nodes while retaining canonical tags/attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str | _TextSlot] = []
        self.text_nodes: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        self.parts.append(self.get_starttag_text() or f"<{tag}>")
        if tag.lower() not in {"br", "hr", "img"}:
            self.stack.append(tag.lower())

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        self.parts.append(self.get_starttag_text() or f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag.lower()}>")
        if self.stack and self.stack[-1] == tag.lower():
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not data.strip() or "pre" in self.stack or "code" in self.stack:
            self.parts.append(escape(data, quote=False))
            return
        whitespace = re.fullmatch(r"(\s*)(.*?)(\s*)", data, re.DOTALL)
        assert whitespace is not None
        leading, core, trailing = whitespace.groups()
        if not core or ("a" in self.stack and _REFERENCE_TEXT_PATTERN.match(core)):
            self.parts.append(escape(data, quote=False))
            return
        if leading:
            self.parts.append(escape(leading, quote=False))
        index = len(self.text_nodes)
        self.text_nodes.append(core)
        self.parts.append(_TextSlot(index))
        if trailing:
            self.parts.append(escape(trailing, quote=False))

    def rebuild(self, translated_nodes: list[str]) -> str:
        return "".join(
            escape(translated_nodes[part.index], quote=False)
            if isinstance(part, _TextSlot)
            else part
            for part in self.parts
        )


async def _translate_rich_html_to_language(
    canonical_html: str,
    source_lang: str,
    target_lang: str,
    *,
    max_visible_chars: int | None,
    semaphore: asyncio.Semaphore,
) -> str:
    parser = _RichTextTemplateParser()
    parser.feed(canonical_html)
    parser.close()
    translated_nodes = list(parser.text_nodes)
    async def translate_node(node: str) -> str:
        async with semaphore:
            return await translate_text(node, source_lang, target_lang)

    tasks = [translate_node(node) for node in parser.text_nodes]
    had_failure = False
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                had_failure = True
                logger.warning(
                    "본문 노드 번역 예외 (%s): %s",
                    target_lang,
                    type(result).__name__,
                )
            else:
                translated_nodes[index] = result
    rebuilt = parser.rebuild(translated_nodes)
    normalized = normalize_rich_content_for_write(
        rebuilt,
        field_name=f"번역 본문({target_lang})",
        max_visible_chars=max_visible_chars,
    )
    if had_failure:
        raise RuntimeError("one or more rich-content text nodes failed translation")
    return normalized


async def auto_translate_rich_content(
    value: str,
    source_lang: str = "ko",
    *,
    max_visible_chars: int | None = None,
) -> dict[str, str]:
    """Translate text nodes only; failed languages are omitted for retry/fallback."""

    canonical_html = normalize_rich_content_for_write(
        value,
        field_name="번역 원문",
        max_visible_chars=max_visible_chars,
    )
    result = {source_lang: canonical_html}
    target_langs = [lang for lang in SUPPORTED_LANGS if lang != source_lang]
    semaphore = asyncio.Semaphore(MAX_RICH_TRANSLATION_CONCURRENCY)
    tasks = [
        _translate_rich_html_to_language(
            canonical_html,
            source_lang,
            target_lang,
            max_visible_chars=max_visible_chars,
            semaphore=semaphore,
        )
        for target_lang in target_langs
    ]
    if tasks:
        translations = await asyncio.gather(*tasks, return_exceptions=True)
        for target_lang, translated in zip(target_langs, translations):
            if isinstance(translated, Exception):
                logger.warning(
                    "본문 번역 예외 (%s): %s",
                    target_lang,
                    type(translated).__name__,
                )
            else:
                result[target_lang] = translated
    return result
