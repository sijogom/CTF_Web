import json
from pathlib import Path
import unittest

from pydantic import BaseModel, ValidationError, field_validator

from app.utils.rich_content import (
    RichContentError,
    RichContentTooLargeError,
    classify_rich_content,
    normalize_rich_content_for_write,
    rich_content_to_plain_text,
    rich_content_visible_char_count,
    sanitize_rich_content,
)


VECTORS_PATH = Path(__file__).resolve().parents[2] / "rich_content_contract_vectors.json"


class _RichPayload(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return sanitize_rich_content(value, field_name="본문")


class RichContentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

    def test_normalization_vectors_are_idempotent(self) -> None:
        for vector in self.vectors["normalize"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(
                    classify_rich_content(vector["input"]).value,
                    vector["kind"],
                )
                result = normalize_rich_content_for_write(
                    vector["input"],
                    allow_empty=vector.get("allow_empty", False),
                )
                self.assertEqual(result, vector["expected_html"])
                if result:
                    self.assertEqual(normalize_rich_content_for_write(result), result)

    def test_ambiguous_and_unsafe_vectors_have_stable_code(self) -> None:
        for vector in self.vectors["reject"]:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(
                    classify_rich_content(vector["input"]).value,
                    "ambiguous",
                )
                with self.assertRaises(RichContentError) as caught:
                    normalize_rich_content_for_write(vector["input"])
                self.assertEqual(caught.exception.code, vector["code"])
                self.assertNotIn(vector["input"], caught.exception.as_detail()["message"])

    def test_plain_text_and_code_point_count_vectors(self) -> None:
        for vector in self.vectors["plain_text"]:
            with self.subTest(value=vector["input"]):
                self.assertEqual(rich_content_to_plain_text(vector["input"]), vector["expected"])
                self.assertEqual(
                    rich_content_visible_char_count(vector["input"]),
                    vector["visible_code_points"],
                )

    def test_visible_and_serialized_limits_are_independent(self) -> None:
        decorated = '<p><strong class="token-keyword">한😀a</strong></p>'
        self.assertEqual(
            normalize_rich_content_for_write(decorated, max_visible_chars=3),
            decorated,
        )
        with self.assertRaises(RichContentTooLargeError):
            normalize_rich_content_for_write(decorated, max_visible_chars=2)
        with self.assertRaises(RichContentTooLargeError):
            normalize_rich_content_for_write(
                decorated,
                max_visible_chars=3,
                max_html_bytes=len(decorated.encode("utf-8")) - 1,
            )

    def test_pydantic_validation_exposes_structured_error_type(self) -> None:
        with self.assertRaises(ValidationError) as caught:
            _RichPayload(content='<p onclick="alert(1)">본문</p>')
        error = caught.exception.errors(include_url=False)[0]
        self.assertEqual(error["type"], "rich_content_review_required")
        self.assertEqual(error["ctx"]["field"], "본문")


if __name__ == "__main__":
    unittest.main()
