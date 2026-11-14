from __future__ import annotations

from pathlib import Path
import json
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = PROJECT_ROOT / "frontend" / "src"


class RichTextAuthoringContractTests(unittest.TestCase):
    """작성 화면은 RichTextEditor를 사용하고 레거시 textarea 편집기로 회귀하지 않는다."""

    AUTHORING_FILES = (
        "app/[locale]/admin/competitions/new/page.tsx",
        "app/[locale]/admin/competitions/[id]/page.tsx",
        "app/[locale]/admin/courses/new/page.tsx",
        "app/[locale]/community/write/page.tsx",
        "components/admin/challenges/StepBasicInfo.tsx",
        "components/admin/courses/LessonForm.tsx",
        "components/admin/courses/LessonEditForm.tsx",
        "components/events/EventForm.tsx",
        "components/teaching/ClassForm.tsx",
        "components/teaching/AssignmentForm.tsx",
        "components/teaching/StudentClassDetail.tsx",
        "components/teaching/TeachingAssignmentDetail.tsx",
    )

    def test_all_active_authoring_surfaces_use_rich_text_editor(self) -> None:
        for relative_path in self.AUTHORING_FILES:
            source = (FRONTEND_ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("RichTextEditor", source, relative_path)
            self.assertNotIn("@/components/ui/MarkdownEditor", source, relative_path)

    def test_legacy_markdown_editor_is_not_an_active_import(self) -> None:
        for source_path in (FRONTEND_ROOT / "app", FRONTEND_ROOT / "components"):
            for path in source_path.rglob("*.tsx"):
                if path.name == "MarkdownEditor.tsx":
                    continue
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("@/components/ui/MarkdownEditor", source, str(path))

    def test_user_facing_locales_do_not_advertise_markdown_authoring(self) -> None:
        for locale in ("ko", "en", "ja", "zh"):
            messages = json.loads(
                (PROJECT_ROOT / "frontend" / "messages" / f"{locale}.json").read_text(encoding="utf-8")
            )
            flattened = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn("Markdown", flattened, locale)
            self.assertNotIn("마크다운", flattened, locale)


if __name__ == "__main__":
    unittest.main()
