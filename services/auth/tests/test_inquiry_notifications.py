import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urlsplit


_AUTH_ROOT = Path(__file__).parents[1]


def _load_access_path_validator():
    model_path = _AUTH_ROOT / "app" / "models" / "inquiry_notification.py"
    namespace = {"urlsplit": urlsplit}
    # 모델 모듈 전체는 SQLAlchemy를 필요로 하므로, 순수 경로 validator가 쓰는
    # 상수와 함수만 AST로 골라 실행한다.
    parsed = ast.parse(model_path.read_text(encoding="utf-8"), filename=str(model_path))
    selected = [
        node
        for node in parsed.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_ALLOWED_INQUIRY_PATHS"
                for target in node.targets
            )
        )
        or (
            isinstance(node, ast.FunctionDef)
            and node.name == "is_safe_inquiry_access_path"
        )
    ]
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(model_path), "exec"), namespace)
    return namespace["is_safe_inquiry_access_path"]


def _receipt_column_names() -> set[str]:
    model_path = _AUTH_ROOT / "app" / "models" / "inquiry_notification.py"
    tree = ast.parse(model_path.read_text(encoding="utf-8"), filename=str(model_path))
    receipt_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "InquiryNotificationReceipt"
    )
    return {
        target.id
        for statement in receipt_class.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }


def _inquiry_request_field_names() -> set[str]:
    internal_path = _AUTH_ROOT / "app" / "api" / "internal.py"
    tree = ast.parse(internal_path.read_text(encoding="utf-8"), filename=str(internal_path))
    request_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "InquiryNotificationRequest"
    )
    return {
        statement.target.id
        for statement in request_class.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    }


class InquiryNotificationPathTests(unittest.TestCase):
    def test_accepts_only_relative_inquiry_paths(self):
        validate_path = _load_access_path_validator()
        self.assertTrue(validate_path("/inquiry"))
        self.assertTrue(
            validate_path(
                "/inquiry/access#ticket=7b612b7e-3255-41df-88ba-d422648501be"
                "&challenge=98a8f3de-022d-4f75-9f5f-fa4f61a7f816&proof=opaque-hmac"
            )
        )

    def test_rejects_external_or_ambiguous_paths(self):
        validate_path = _load_access_path_validator()
        for unsafe_path in (
            "https://attacker.example/inquiry",
            "//attacker.example/inquiry",
            "/\\attacker.example/inquiry",
            "/outside",
            "/inquiry/../outside",
            "/inquiry?next=https://attacker.example",
            "/inquiry\nhttps://attacker.example",
        ):
            with self.subTest(unsafe_path=unsafe_path):
                self.assertFalse(validate_path(unsafe_path))


class InquiryNotificationReceiptTests(unittest.TestCase):
    def test_receipt_does_not_store_guest_email_or_message_body(self):
        columns = _receipt_column_names()
        self.assertNotIn("recipient_email", columns)
        self.assertNotIn("recipient_user_id", columns)
        self.assertNotIn("access_path", columns)
        self.assertNotIn("body", columns)
        self.assertIn("event_id", columns)
        self.assertIn("lease_expires_at", columns)


class InquiryNotificationContractTests(unittest.TestCase):
    def test_request_contract_excludes_raw_reply_and_external_url_fields(self):
        self.assertEqual(
            _inquiry_request_field_names(),
            {
                "event_id",
                "recipient_type",
                "recipient_email",
                "recipient_user_id",
                "kind",
                "access_path",
                "locale",
            },
        )

    def test_migration_follows_account_ban_revision(self):
        migration_path = (
            _AUTH_ROOT
            / "migrations"
            / "versions"
            / "20260813_002_inquiry_notification_receipts.py"
        )
        source = migration_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(migration_path))
        assignments = {
            target.id: ast.literal_eval(statement.value)
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}
        }
        self.assertEqual(assignments, {"revision": "20260813_002", "down_revision": "20260813_001"})
        self.assertIn("uq_inquiry_notification_receipts_event_id", source)


class InquiryNotificationTemplateTests(unittest.TestCase):
    def test_escapes_dynamic_name_and_url_without_loading_runtime_config(self):
        original_config = sys.modules.get("app.config")
        fake_config = types.ModuleType("app.config")
        fake_config.settings = types.SimpleNamespace()
        sys.modules["app.config"] = fake_config
        try:
            template_path = Path(__file__).parents[1] / "app" / "email" / "templates.py"
            spec = importlib.util.spec_from_file_location("inquiry_templates_test", template_path)
            assert spec and spec.loader
            templates = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(templates)
            _, rendered = templates.inquiry_notification_template(
                "reply_available",
                'https://ctf.example/inquiry&quot; onclick="alert(1)',
                recipient_name="<img src=x onerror=alert(1)>",
                locale="en",
            )
        finally:
            if original_config is None:
                sys.modules.pop("app.config", None)
            else:
                sys.modules["app.config"] = original_config

        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertNotIn("<img src=x onerror=alert(1)>", rendered)
        self.assertIn('href="https://ctf.example/inquiry&amp;quot; onclick=&quot;alert(1)"', rendered)


if __name__ == "__main__":
    unittest.main()
