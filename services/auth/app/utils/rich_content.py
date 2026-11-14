"""Canonical rich-content validation, conversion, and plain-text helpers.

Public and database field names keep their historical ``*_markdown`` spelling,
but values crossing a write boundary are canonical HTML. Legacy Markdown and
plain text are converted once. Mixed, malformed, or unsupported markup is sent
to explicit review instead of being stripped silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from html import escape
from html.parser import HTMLParser
import re
from typing import Mapping
from urllib.parse import urlsplit

import nh3
from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic_core import PydanticCustomError


class RichContentKind(str, Enum):
    HTML = "html"
    MARKDOWN = "markdown"
    PLAIN = "plain"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class RichContentPolicy:
    allow_empty: bool = False
    max_visible_chars: int | None = None
    max_html_bytes: int | None = None


class RichContentError(ValueError):
    code = "rich_content_invalid"

    def __init__(self, message: str, *, field_name: str | None = None) -> None:
        self.message = message
        self.field_name = field_name
        super().__init__(f"{self.code}: {message}")

    def as_detail(self) -> dict[str, str]:
        detail = {"code": self.code, "message": self.message}
        if self.field_name:
            detail["field"] = self.field_name
        return detail


class RichContentReviewRequired(RichContentError):
    code = "rich_content_review_required"


class RichContentEmptyError(RichContentError):
    code = "rich_content_empty"


class RichContentTooLargeError(RichContentError):
    code = "rich_content_too_large"


_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_LIKE_PATTERN = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")
_BROKEN_TAG_LIKE_PATTERN = re.compile(r"<\s*/?\s*[A-Za-z]")
_UNSAFE_MARKDOWN_URL_PATTERN = re.compile(
    r"!?\[[^\]]*\]\(\s*(?:javascript|data|vbscript)\s*:",
    re.IGNORECASE,
)

_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "u", "s", "code", "pre", "span",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "blockquote", "a", "img", "div", "hr", "table", "thead",
    "tbody", "tfoot", "tr", "th", "td",
}
_VOID_TAGS = {"br", "hr", "img"}
_GENERIC_ATTRIBUTES = {"class", "title"}
_TAG_ATTRIBUTES = {
    "a": {"href", "target", "rel"},
    "img": {"src", "alt", "loading"},
    "ol": {"start"},
    "li": {"value"},
    "th": {"colspan", "rowspan", "scope", "align"},
    "td": {"colspan", "rowspan", "align"},
}
_NH3_ATTRIBUTES = {
    tag: set(_GENERIC_ATTRIBUTES) | set(_TAG_ATTRIBUTES.get(tag, set()))
    for tag in _ALLOWED_TAGS
}
_LINK_SCHEMES = {"http", "https", "mailto"}
_IMAGE_SCHEMES = {"http", "https"}
_REL_TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_CLASS_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CODE_LANGUAGE_CLASS_PATTERN = re.compile(
    r"^language-[A-Za-z0-9][A-Za-z0-9_+.#-]*$"
)
_INTEGER_PATTERN = re.compile(r"^-?[0-9]+$")
_POSITIVE_INTEGER_PATTERN = re.compile(r"^[1-9][0-9]*$")

_MARKDOWN_RENDERER = MarkdownIt(
    "commonmark",
    {"html": False, "breaks": True, "typographer": False},
).enable(["table", "strikethrough"])
_MARKDOWN_CLASSIFIER = MarkdownIt(
    "commonmark",
    {"html": True, "breaks": True, "typographer": False},
).enable(["table", "strikethrough"])
_PLAIN_BLOCK_TOKEN_TYPES = {"paragraph_open", "paragraph_close", "inline"}
_PLAIN_INLINE_TOKEN_TYPES = {"text", "softbreak"}
_BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
    "blockquote", "pre", "table", "thead", "tbody", "tfoot", "tr",
}


def _walk_tokens(tokens: list[Token]):
    for token in tokens:
        yield token
        if token.children:
            yield from _walk_tokens(token.children)


def _contains_raw_markdown_html(tokens: list[Token]) -> bool:
    return any(token.type in {"html_inline", "html_block"} for token in _walk_tokens(tokens))


def _has_markdown_structure(tokens: list[Token]) -> bool:
    for token in tokens:
        if token.type not in _PLAIN_BLOCK_TOKEN_TYPES:
            return True
        if token.type == "inline" and token.children:
            if any(child.type not in _PLAIN_INLINE_TOKEN_TYPES for child in token.children):
                return True
    return False


def _safe_url(value: str, *, tag: str, attribute: str, field_name: str) -> None:
    if not value or value != value.strip() or _CONTROL_CHARACTER_PATTERN.search(value):
        raise RichContentReviewRequired(
            f"{field_name}의 {attribute} URL이 비어 있거나 안전하게 보존할 수 없습니다.",
            field_name=field_name,
        )
    if any(char.isspace() for char in value):
        raise RichContentReviewRequired(
            f"{field_name}의 {attribute} URL에 공백을 사용할 수 없습니다.",
            field_name=field_name,
        )
    if value.startswith("//") or "\\" in value:
        raise RichContentReviewRequired(
            f"{field_name}의 {attribute} URL 참조 형식은 보존 검토가 필요합니다.",
            field_name=field_name,
        )
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    allowed_schemes = _IMAGE_SCHEMES if tag == "img" else _LINK_SCHEMES
    if scheme and scheme not in allowed_schemes:
        raise RichContentReviewRequired(
            f"{field_name}의 {attribute} URL 스킴은 보존 검토가 필요합니다.",
            field_name=field_name,
        )
    if value.lstrip().lower().startswith(("javascript:", "data:", "vbscript:")):
        raise RichContentReviewRequired(
            f"{field_name}의 {attribute} URL은 저장할 수 없습니다.",
            field_name=field_name,
        )


def _validate_attribute(
    tag: str,
    name: str,
    value: str | None,
    *,
    field_name: str,
) -> None:
    allowed = _GENERIC_ATTRIBUTES | _TAG_ATTRIBUTES.get(tag, set())
    if name.startswith("on") or name not in allowed:
        raise RichContentReviewRequired(
            f"{field_name}에 허용되지 않은 HTML 속성이 있어 검토가 필요합니다.",
            field_name=field_name,
        )
    if value is None:
        raise RichContentReviewRequired(
            f"{field_name}의 HTML 속성 값을 안전하게 보존할 수 없습니다.",
            field_name=field_name,
        )
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise RichContentReviewRequired(
            f"{field_name}의 HTML 속성에 제어 문자를 사용할 수 없습니다.",
            field_name=field_name,
        )
    if name in {"href", "src"}:
        _safe_url(value, tag=tag, attribute=name, field_name=field_name)
    elif name == "class":
        tokens = value.split()
        if not tokens or any(
            not _CLASS_TOKEN_PATTERN.fullmatch(token)
            and not (
                tag == "code"
                and _CODE_LANGUAGE_CLASS_PATTERN.fullmatch(token)
            )
            for token in tokens
        ):
            raise RichContentReviewRequired(
                f"{field_name}의 HTML class를 안전하게 보존할 수 없습니다.",
                field_name=field_name,
            )
    elif name == "target" and value != "_blank":
        raise RichContentReviewRequired(
            f"{field_name}의 링크 target은 보존 검토가 필요합니다.",
            field_name=field_name,
        )
    elif name == "rel":
        tokens = value.split()
        if not tokens or any(not _REL_TOKEN_PATTERN.fullmatch(token) for token in tokens):
            raise RichContentReviewRequired(
                f"{field_name}의 링크 rel을 안전하게 보존할 수 없습니다.",
                field_name=field_name,
            )
    elif name == "loading" and value not in {"lazy", "eager", "auto"}:
        raise RichContentReviewRequired(
            f"{field_name}의 이미지 loading 값을 보존할 수 없습니다.",
            field_name=field_name,
        )
    elif name in {"colspan", "rowspan"}:
        if not _POSITIVE_INTEGER_PATTERN.fullmatch(value) or int(value) > 100:
            raise RichContentReviewRequired(
                f"{field_name}의 표 병합 범위를 보존할 수 없습니다.",
                field_name=field_name,
            )
    elif name == "scope" and value not in {"row", "col", "rowgroup", "colgroup"}:
        raise RichContentReviewRequired(
            f"{field_name}의 표 scope 값을 보존할 수 없습니다.",
            field_name=field_name,
        )
    elif name == "align" and value not in {"left", "center", "right"}:
        raise RichContentReviewRequired(
            f"{field_name}의 표 정렬 값을 보존할 수 없습니다.",
            field_name=field_name,
        )
    elif name in {"start", "value"} and not _INTEGER_PATTERN.fullmatch(value):
        raise RichContentReviewRequired(
            f"{field_name}의 목록 번호 값을 보존할 수 없습니다.",
            field_name=field_name,
        )


class _HTMLInspector(HTMLParser):
    def __init__(self, *, field_name: str) -> None:
        super().__init__(convert_charrefs=False)
        self.field_name = field_name
        self.stack: list[str] = []
        self.saw_tag = False
        self.saw_top_level_text = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        normalized_tag = tag.lower()
        self.saw_tag = True
        if normalized_tag not in _ALLOWED_TAGS:
            raise RichContentReviewRequired(
                f"{self.field_name}에 지원하지 않는 HTML 태그가 있어 검토가 필요합니다.",
                field_name=self.field_name,
            )
        seen_attributes: set[str] = set()
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            if name in seen_attributes:
                raise RichContentReviewRequired(
                    f"{self.field_name}에 중복 HTML 속성이 있어 검토가 필요합니다.",
                    field_name=self.field_name,
                )
            seen_attributes.add(name)
            _validate_attribute(normalized_tag, name, raw_value, field_name=self.field_name)
        if normalized_tag == "img" and "src" not in seen_attributes:
            raise RichContentReviewRequired(
                f"{self.field_name}의 이미지 참조가 없습니다.",
                field_name=self.field_name,
            )
        if normalized_tag not in _VOID_TAGS:
            self.stack.append(normalized_tag)

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        normalized_tag = tag.lower()
        self.handle_starttag(normalized_tag, attrs)
        if normalized_tag not in _VOID_TAGS:
            self.handle_endtag(normalized_tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in _VOID_TAGS or normalized_tag not in _ALLOWED_TAGS:
            raise RichContentReviewRequired(
                f"{self.field_name}의 HTML 닫기 태그를 안전하게 해석할 수 없습니다.",
                field_name=self.field_name,
            )
        if not self.stack or self.stack[-1] != normalized_tag:
            raise RichContentReviewRequired(
                f"{self.field_name}의 HTML 구조가 올바르지 않아 검토가 필요합니다.",
                field_name=self.field_name,
            )
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if not self.stack and data.strip():
            self.saw_top_level_text = True

    def handle_comment(self, data: str) -> None:
        raise RichContentReviewRequired(
            f"{self.field_name}의 HTML 주석은 자동 변환하지 않습니다.",
            field_name=self.field_name,
        )

    def handle_decl(self, decl: str) -> None:
        raise RichContentReviewRequired(
            f"{self.field_name}의 HTML 선언은 저장할 수 없습니다.",
            field_name=self.field_name,
        )

    def handle_pi(self, data: str) -> None:
        raise RichContentReviewRequired(
            f"{self.field_name}의 처리 지시문은 저장할 수 없습니다.",
            field_name=self.field_name,
        )

    def unknown_decl(self, data: str) -> None:
        raise RichContentReviewRequired(
            f"{self.field_name}의 HTML 선언을 안전하게 해석할 수 없습니다.",
            field_name=self.field_name,
        )

    def finish(self) -> None:
        if self.stack:
            raise RichContentReviewRequired(
                f"{self.field_name}의 HTML 태그가 닫히지 않아 검토가 필요합니다.",
                field_name=self.field_name,
            )
        if self.saw_top_level_text:
            raise RichContentReviewRequired(
                f"{self.field_name}에 HTML과 일반 텍스트가 혼합되어 검토가 필요합니다.",
                field_name=self.field_name,
            )


def _inspect_html(value: str, *, field_name: str) -> _HTMLInspector:
    inspector = _HTMLInspector(field_name=field_name)
    try:
        inspector.feed(value)
        inspector.close()
        inspector.finish()
    except RichContentError:
        raise
    except Exception as exc:
        raise RichContentReviewRequired(
            f"{field_name} HTML을 안전하게 해석할 수 없어 검토가 필요합니다.",
            field_name=field_name,
        ) from exc
    return inspector


class _SafeLinkRelNormalizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.output: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        normalized_attrs = list(attrs)
        if tag.lower() == "a":
            target = next((value for name, value in normalized_attrs if name == "target"), None)
            if target == "_blank":
                rel_index = next(
                    (index for index, (name, _) in enumerate(normalized_attrs) if name == "rel"),
                    None,
                )
                rel_tokens: list[str] = []
                if rel_index is not None:
                    rel_tokens = (normalized_attrs[rel_index][1] or "").split()
                for required in ("noopener", "noreferrer"):
                    if required not in rel_tokens:
                        rel_tokens.append(required)
                if rel_index is None:
                    normalized_attrs.append(("rel", " ".join(rel_tokens)))
                else:
                    normalized_attrs[rel_index] = ("rel", " ".join(rel_tokens))
        rendered = "".join(
            f' {name}="{escape(value or "", quote=True)}"'
            for name, value in normalized_attrs
        )
        self.output.append(f"<{tag.lower()}{rendered}>")

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.output.append(f"</{tag.lower()}>")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() not in _VOID_TAGS:
            self.output.append(f"</{tag.lower()}>")

    def handle_data(self, data: str) -> None:
        self.output.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.output.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.output.append(f"&#{name};")


def _ensure_safe_link_rel(value: str) -> str:
    parser = _SafeLinkRelNormalizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.output)


class _HTMLPreservationRecorder(HTMLParser):
    """Record semantic source events for comparison with html5ever output.

    ``nh3`` remains the canonical HTML5 parser and sanitizer.  This recorder
    does not repair or serialize input; it only detects when the mature parser
    had to move text or tags, close/reopen formatting elements, or otherwise
    change the submitted tree.  Empty ``tbody`` boundaries are omitted because
    HTML5 parsers insert them for an otherwise lossless ``table > tr`` input.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[object, ...]] = []

    @staticmethod
    def _normalized_attributes(attrs) -> tuple[tuple[str, str], ...]:  # type: ignore[no-untyped-def]
        normalized: list[tuple[str, str]] = []
        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = raw_value or ""
            if name in {"class", "rel"}:
                value = " ".join(value.split())
            normalized.append((name, value))
        return tuple(sorted(normalized))

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        normalized_tag = tag.lower()
        normalized_attrs = self._normalized_attributes(attrs)
        if normalized_tag != "tbody" or normalized_attrs:
            self.events.append(("start", normalized_tag, normalized_attrs))

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag != "tbody" and normalized_tag not in _VOID_TAGS:
            self.events.append(("end", normalized_tag))

    def handle_data(self, data: str) -> None:
        normalized = data.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized:
            return
        if self.events and self.events[-1][0] == "text":
            previous = self.events.pop()
            self.events.append(("text", str(previous[1]) + normalized))
        else:
            self.events.append(("text", normalized))


def _html_preservation_signature(value: str) -> tuple[tuple[object, ...], ...]:
    recorder = _HTMLPreservationRecorder()
    recorder.feed(value)
    recorder.close()
    return tuple(recorder.events)


def classify_rich_content(value: str) -> RichContentKind:
    """Classify legacy content without mutating it."""

    if not isinstance(value, str):
        return RichContentKind.AMBIGUOUS
    source = value
    stripped = source.strip()
    if not stripped:
        return RichContentKind.PLAIN
    if _CONTROL_CHARACTER_PATTERN.search(source):
        return RichContentKind.AMBIGUOUS

    classifier_tokens = _MARKDOWN_CLASSIFIER.parse(source)
    if stripped.startswith("<"):
        try:
            inspector = _inspect_html(stripped, field_name="본문")
        except RichContentError:
            return RichContentKind.AMBIGUOUS
        if not inspector.saw_tag:
            return RichContentKind.AMBIGUOUS
        try:
            _sanitize_html_fragment(stripped, field_name="본문")
        except RichContentError:
            return RichContentKind.AMBIGUOUS
        return RichContentKind.HTML
    if _contains_raw_markdown_html(classifier_tokens):
        return RichContentKind.AMBIGUOUS
    if _UNSAFE_MARKDOWN_URL_PATTERN.search(source):
        return RichContentKind.AMBIGUOUS
    if _has_markdown_structure(classifier_tokens):
        return RichContentKind.MARKDOWN
    if _TAG_LIKE_PATTERN.search(source) or _BROKEN_TAG_LIKE_PATTERN.search(source):
        return RichContentKind.AMBIGUOUS
    return RichContentKind.PLAIN


class _ContentMetricsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text_parts: list[str] = []
        self.summary_parts: list[str] = []
        self.has_image = False

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        normalized_tag = tag.lower()
        if normalized_tag not in _VOID_TAGS:
            self.stack.append(normalized_tag)
        if normalized_tag == "br":
            self.summary_parts.append("\n")
        elif normalized_tag == "img":
            self.has_image = True
            alt = next((value for name, value in attrs if name.lower() == "alt"), None)
            if alt:
                self.text_parts.append(alt)
                self.summary_parts.append(alt)

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in {"th", "td"}:
            self.summary_parts.append("\t")
        elif normalized_tag in _BLOCK_TAGS:
            self.summary_parts.append("\n")
        if self.stack and self.stack[-1] == normalized_tag:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        in_pre = "pre" in self.stack
        if data.strip() or in_pre:
            self.text_parts.append(data)
            self.summary_parts.append(data)

    @property
    def visible_char_count(self) -> int:
        return len("".join(self.text_parts).strip())

    @property
    def plain_text(self) -> str:
        value = "".join(self.summary_parts)
        value = re.sub(r"[ \f\v]+", " ", value)
        value = re.sub(r"\t+\n", "\n", value)
        value = re.sub(r" *\n *", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def _content_metrics(value: str) -> _ContentMetricsParser:
    parser = _ContentMetricsParser()
    parser.feed(value)
    parser.close()
    return parser


def _sanitize_html_fragment(value: str, *, field_name: str) -> str:
    inspector = _inspect_html(value, field_name=field_name)
    if not inspector.saw_tag:
        raise RichContentReviewRequired(
            f"{field_name}을 HTML로 안전하게 해석할 수 없어 검토가 필요합니다.",
            field_name=field_name,
        )
    normalized_links = _ensure_safe_link_rel(value)
    source_signature = _html_preservation_signature(normalized_links)
    try:
        cleaned = nh3.clean(
            normalized_links,
            tags=set(_ALLOWED_TAGS),
            attributes=_NH3_ATTRIBUTES,
            strip_comments=True,
            link_rel=None,
            url_schemes=set(_LINK_SCHEMES),
        ).strip()
    except Exception as exc:
        raise RichContentReviewRequired(
            f"{field_name} HTML을 안전하게 정규화할 수 없어 검토가 필요합니다.",
            field_name=field_name,
        ) from exc
    _inspect_html(cleaned, field_name=field_name)
    if _html_preservation_signature(cleaned) != source_signature:
        raise RichContentReviewRequired(
            f"{field_name} HTML 구조를 자동 복구하면 원문 배치가 달라져 검토가 필요합니다.",
            field_name=field_name,
        )
    return cleaned


def _render_markdown(value: str) -> str:
    tokens = _MARKDOWN_RENDERER.parse(value)
    for token in _walk_tokens(tokens):
        if token.tag not in {"th", "td"} or not token.attrs:
            continue
        style = token.attrGet("style")
        if not style:
            continue
        match = re.fullmatch(r"text-align\s*:\s*(left|center|right);?", style)
        if not match:
            raise RichContentReviewRequired("표 정렬 형식을 안전하게 변환할 수 없습니다.")
        token.attrSet("align", match.group(1))
        token.attrs.pop("style", None)
    return _MARKDOWN_RENDERER.renderer.render(tokens, _MARKDOWN_RENDERER.options, {}).strip()


def _enforce_content_policy(
    canonical_html: str,
    *,
    field_name: str,
    allow_empty: bool,
    max_visible_chars: int | None,
    max_html_bytes: int | None,
) -> str:
    metrics = _content_metrics(canonical_html)
    if not metrics.plain_text and not metrics.has_image:
        if allow_empty:
            return ""
        raise RichContentEmptyError(f"{field_name}은(는) 비워 둘 수 없습니다.", field_name=field_name)
    if max_visible_chars is not None and metrics.visible_char_count > max_visible_chars:
        raise RichContentTooLargeError(
            f"{field_name}의 표시 텍스트는 {max_visible_chars}자를 초과할 수 없습니다.",
            field_name=field_name,
        )
    if max_html_bytes is not None and len(canonical_html.encode("utf-8")) > max_html_bytes:
        raise RichContentTooLargeError(
            f"{field_name}의 HTML 저장 크기는 {max_html_bytes}바이트를 초과할 수 없습니다.",
            field_name=field_name,
        )
    return canonical_html


def sanitize_rich_html(
    value: str,
    *,
    field_name: str = "본문",
    allow_empty: bool = False,
    max_visible_chars: int | None = None,
    max_html_bytes: int | None = None,
) -> str:
    """Validate already-HTML content and return canonical sanitized HTML."""

    if not isinstance(value, str):
        raise RichContentReviewRequired(f"{field_name}은 문자열이어야 합니다.", field_name=field_name)
    stripped = value.strip()
    if not stripped:
        if allow_empty:
            return ""
        raise RichContentEmptyError(f"{field_name}은(는) 비워 둘 수 없습니다.", field_name=field_name)
    if classify_rich_content(value) is not RichContentKind.HTML:
        raise RichContentReviewRequired(f"{field_name}은 검토가 필요한 형식입니다.", field_name=field_name)
    cleaned = _sanitize_html_fragment(stripped, field_name=field_name)
    return _enforce_content_policy(
        cleaned,
        field_name=field_name,
        allow_empty=allow_empty,
        max_visible_chars=max_visible_chars,
        max_html_bytes=max_html_bytes,
    )


def normalize_rich_content_for_write(
    value: str,
    *,
    field_name: str = "본문",
    allow_empty: bool = False,
    max_visible_chars: int | None = None,
    max_html_bytes: int | None = None,
    policy: RichContentPolicy | None = None,
) -> str:
    """Convert unambiguous legacy content to canonical sanitized HTML."""

    if policy is not None:
        allow_empty = policy.allow_empty
        max_visible_chars = policy.max_visible_chars
        max_html_bytes = policy.max_html_bytes
    if not isinstance(value, str):
        raise RichContentReviewRequired(f"{field_name}은 문자열이어야 합니다.", field_name=field_name)
    stripped = value.strip()
    if not stripped:
        if allow_empty:
            return ""
        raise RichContentEmptyError(f"{field_name}은(는) 비워 둘 수 없습니다.", field_name=field_name)
    if _CONTROL_CHARACTER_PATTERN.search(value):
        raise RichContentReviewRequired(
            f"{field_name}에 제어 문자를 포함할 수 없습니다.", field_name=field_name
        )

    kind = classify_rich_content(value)
    if kind is RichContentKind.AMBIGUOUS:
        raise RichContentReviewRequired(
            f"{field_name}에 혼합되거나 지원하지 않는 형식이 있어 원문 검토가 필요합니다.",
            field_name=field_name,
        )
    if kind is RichContentKind.HTML:
        canonical_html = _sanitize_html_fragment(stripped, field_name=field_name)
    else:
        rendered = _render_markdown(value)
        canonical_html = _sanitize_html_fragment(rendered, field_name=field_name)
    return _enforce_content_policy(
        canonical_html,
        field_name=field_name,
        allow_empty=allow_empty,
        max_visible_chars=max_visible_chars,
        max_html_bytes=max_html_bytes,
    )


def normalize_rich_content_mapping(
    value: Mapping[str, str] | None,
    *,
    field_name: str,
    allow_empty: bool = True,
    max_visible_chars: int | None = None,
    max_html_bytes: int | None = None,
) -> dict[str, str]:
    if value is None:
        return {}
    normalized: dict[str, str] = {}
    for language, content in value.items():
        if not isinstance(language, str) or not isinstance(content, str):
            raise RichContentReviewRequired(
                f"{field_name} 번역 값은 언어별 문자열이어야 합니다.", field_name=field_name
            )
        normalized[language] = normalize_rich_content_for_write(
            content,
            field_name=f"{field_name}({language})",
            allow_empty=allow_empty,
            max_visible_chars=max_visible_chars,
            max_html_bytes=max_html_bytes,
        )
    return normalized


def rich_content_to_plain_text(value: str, *, field_name: str = "본문") -> str:
    canonical = normalize_rich_content_for_write(value, field_name=field_name, allow_empty=True)
    return "" if not canonical else _content_metrics(canonical).plain_text


def rich_content_visible_char_count(value: str, *, field_name: str = "본문") -> int:
    canonical = normalize_rich_content_for_write(value, field_name=field_name, allow_empty=True)
    return 0 if not canonical else _content_metrics(canonical).visible_char_count


def rich_content_visible_char_count_for_read(value: str) -> int:
    """Count legacy content without turning an ambiguous read into a write."""

    try:
        return rich_content_visible_char_count(value)
    except RichContentError:
        return len(value)


def extract_plain_text_from_html(value: str, *, field_name: str = "본문") -> str:
    canonical = sanitize_rich_html(value, field_name=field_name, allow_empty=True)
    return "" if not canonical else _content_metrics(canonical).plain_text


def is_rich_content_empty(value: str) -> bool:
    try:
        canonical = normalize_rich_content_for_write(value, allow_empty=True)
    except RichContentError:
        return False
    if not canonical:
        return True
    metrics = _content_metrics(canonical)
    return not metrics.plain_text and not metrics.has_image


# Compatibility exports used by existing schemas and API modules.
def sanitize_rich_content(
    value: str,
    *,
    field_name: str,
    allow_empty: bool = False,
    max_visible_chars: int | None = None,
    max_html_bytes: int | None = None,
) -> str:
    try:
        return normalize_rich_content_for_write(
            value,
            field_name=field_name,
            allow_empty=allow_empty,
            max_visible_chars=max_visible_chars,
            max_html_bytes=max_html_bytes,
        )
    except RichContentError as exc:
        raise PydanticCustomError(
            exc.code,
            exc.message,
            {"field": exc.field_name or field_name},
        ) from exc


def is_rich_html(value: str) -> bool:
    return classify_rich_content(value) is RichContentKind.HTML


def to_safe_rich_html(value: str, *, field_name: str = "HTML 본문") -> str:
    return normalize_rich_content_for_write(value, field_name=field_name, allow_empty=True)
