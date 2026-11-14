"""Build-context storage failures must remain fail-closed and safe."""

import ast
import sys
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))


class BuildContextStorageRuleTests(unittest.TestCase):
    def test_storage_failures_raise_a_typed_error_instead_of_returning_empty(self) -> None:
        source = (SERVICE_ROOT / "app" / "utils" / "minio_client.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }

        self.assertIn("class BuildContextStorageUnavailableError", source)
        for name in (
            "list_build_files",
            "get_build_file_urls",
            "delete_build_file",
            "delete_all_build_files",
        ):
            self.assertIn("except Exception as error", functions[name])
            self.assertIn(
                "raise BuildContextStorageUnavailableError() from error",
                functions[name],
            )

    def test_internal_contract_exposes_only_safe_build_context_failure(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "internal.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("BuildContextStorageUnavailableError", source)
        self.assertIn("status.HTTP_503_SERVICE_UNAVAILABLE", source)
        self.assertIn("build_context_unavailable", source)
        self.assertIn('"flag_type": challenge.flag_type', source)
        self.assertIn('"environment_status": _environment_status_for_runtime(', source)


if __name__ == "__main__":
    unittest.main()
