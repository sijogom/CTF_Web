from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class EventCategoryEditContractTests(unittest.TestCase):
    def test_admin_event_form_sends_category_on_create_and_update(self) -> None:
        form = (PROJECT_ROOT / "frontend" / "src" / "components" / "events" / "EventForm.tsx").read_text(encoding="utf-8")
        detail = (PROJECT_ROOT / "frontend" / "src" / "app" / "[locale]" / "admin" / "events" / "[id]" / "page.tsx").read_text(encoding="utf-8")
        schema = (PROJECT_ROOT / "services" / "learn" / "app" / "schemas" / "event.py").read_text(encoding="utf-8")
        self.assertIn('value={values.category}', form)
        self.assertIn('update("category", event.target.value as EventCategory)', form)
        self.assertIn('category: values.category', form)
        self.assertIn('method: "PATCH"', detail)
        self.assertIn('category: EventCategory | None = None', schema)


if __name__ == "__main__":
    unittest.main()
