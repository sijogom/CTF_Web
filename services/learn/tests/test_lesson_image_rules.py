"""레슨 편집기 이미지 업로드의 DB 비의존 안전 계약 테스트."""

from pathlib import Path
import sys
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.lesson_image_validation import validate_lesson_image_bytes  # noqa: E402


class LessonImageValidationTests(unittest.TestCase):
    def test_extension_and_binary_signature_must_match(self) -> None:
        valid_cases = (
            (".png", b"\x89PNG\r\n\x1a\nremaining"),
            (".jpg", b"\xff\xd8\xff\xe0remaining"),
            (".jpeg", b"\xff\xd8\xff\xe1remaining"),
            (".gif", b"GIF89aremaining"),
            (".webp", b"RIFF\x00\x00\x00\x00WEBPremaining"),
        )
        for extension, data in valid_cases:
            with self.subTest(extension=extension):
                self.assertIsNone(validate_lesson_image_bytes(data=data, extension=extension))

        self.assertIsNotNone(validate_lesson_image_bytes(data=b"<html>not-an-image", extension=".png"))
        self.assertIsNotNone(validate_lesson_image_bytes(data=b"GIF89a", extension=".jpg"))
        self.assertIsNotNone(validate_lesson_image_bytes(data=b"RIFF\x00\x00\x00\x00NOPE", extension=".webp"))

    def test_upload_route_keeps_server_side_extension_size_signature_and_close_contracts(self) -> None:
        route_source = (SERVICE_ROOT / "app" / "api" / "lessons.py").read_text(encoding="utf-8")
        self.assertIn("ext not in IMAGE_EXTENSIONS", route_source)
        self.assertIn("MAX_IMAGE_SIZE", route_source)
        self.assertIn("validate_lesson_image_bytes", route_source)
        self.assertIn("await file.close()", route_source)
        self.assertIn("require_role(\"admin\", \"moderator\")", route_source)

    def test_storage_helper_keeps_svg_out_of_lesson_image_extensions(self) -> None:
        storage_source = (SERVICE_ROOT / "app" / "utils" / "minio_client.py").read_text(encoding="utf-8")
        image_extensions_section = storage_source.split("IMAGE_EXTENSIONS =", 1)[1].split("# 확장자", 1)[0]
        self.assertNotIn(".svg", image_extensions_section)


if __name__ == "__main__":
    unittest.main()
