"""P0 관리자 제출 감사 슬라이스의 정적 계약 검증.

저장소의 로컬 가상환경이 정적 CI에서 없을 수 있어 FastAPI 애플리케이션 전체를
import하지 않는다. 대신 원문 플래그가 관리자 목록 응답이나 UI 필드로 되돌아가지
않는 필수 경계를 검증한다.
"""

import ast
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
SUBMISSIONS_API = SERVICE_ROOT / "app" / "api" / "submissions.py"
CHALLENGES_API = SERVICE_ROOT / "app" / "api" / "challenges.py"
CHALLENGE_MODEL = SERVICE_ROOT / "app" / "models" / "challenge.py"
ADMIN_PAGE = SERVICE_ROOT.parents[1] / "frontend" / "src" / "app" / "[locale]" / "admin" / "submissions" / "page.tsx"


class SubmissionAuditContractTests(unittest.TestCase):
    def _load_audit_metadata_builder(self):
        """소스에서 순수 응답 메타데이터 함수만 분리 실행한다."""
        tree = ast.parse(SUBMISSIONS_API.read_text(encoding="utf-8"))
        function_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "build_admin_submission_audit_metadata"
        )
        module = ast.Module(body=[function_node], type_ignores=[])
        namespace = {
            "hashlib": hashlib,
            "hmac": hmac,
            "settings": SimpleNamespace(internal_api_key=""),
            "build_submission_list_metadata": lambda value: {
                "submitted_flag_recorded": bool(value),
                "submitted_flag_length": len(value),
            },
        }
        exec(compile(module, str(SUBMISSIONS_API), "exec"), namespace)
        return namespace["build_admin_submission_audit_metadata"]

    def _load_submission_order(self):
        class Column:
            def __init__(self, name: str) -> None:
                self.name = name

            def asc(self) -> str:
                return f"{self.name}.asc"

            def desc(self) -> str:
                return f"{self.name}.desc"

        tree = ast.parse(SUBMISSIONS_API.read_text(encoding="utf-8"))
        function_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_submission_log_order"
        )
        module = ast.Module(body=[function_node], type_ignores=[])
        namespace = {
            "_validate_audit_choice": lambda *_args: None,
            "_AUDIT_SORTS": set(),
            "Submission": SimpleNamespace(
                submitted_at=Column("submitted_at"),
                id=Column("id"),
                is_correct=Column("is_correct"),
            ),
            "Challenge": SimpleNamespace(name=Column("challenge_name")),
        }
        exec(compile(module, str(SUBMISSIONS_API), "exec"), namespace)
        return namespace["_submission_log_order"]

    def test_redacted_metadata_uses_keyed_fingerprint_without_raw_value(self) -> None:
        raw_value = "CTF{do-not-expose-this-value}"
        build_metadata = self._load_audit_metadata_builder()

        metadata = build_metadata(raw_value, fingerprint_secret="unit-test-secret")

        self.assertEqual(metadata["submitted_flag_masked"], "[redacted]")
        self.assertEqual(metadata["submitted_flag_length"], len(raw_value))
        self.assertTrue(metadata["submitted_flag_recorded"])
        self.assertEqual(metadata["submitted_flag_fingerprint_algorithm"], "hmac-sha256")
        self.assertTrue(metadata["submitted_flag_fingerprint"].startswith("hmac-sha256:"))
        self.assertNotIn(raw_value, metadata.values())

    def test_admin_log_response_has_restricted_reveal_not_plaintext(self) -> None:
        source = SUBMISSIONS_API.read_text(encoding="utf-8")
        endpoint_start = source.index("async def get_submission_logs(")
        reveal_start = source.index('@flag_logs_router.get("/logs/{submission_id}/flag")')
        endpoint = source[endpoint_start:reveal_start]

        self.assertIn("build_admin_submission_audit_metadata(sub.submitted_flag)", endpoint)
        self.assertIn('"submitted_flag_masked"', source)
        self.assertIn("reveal_submission_flag", source)
        self.assertIn("decrypt_submitted_flag", source)
        self.assertIn("SubmissionFlagRevealAudit", source)
        self.assertIn('"submitted_flag_revealable"', endpoint)
        self.assertIn('"submission_context"', endpoint)
        self.assertIn('"challenge_type"', endpoint)
        self.assertNotIn('"submitted_flag":', endpoint)

    def test_audit_filters_and_aggregate_route_are_present(self) -> None:
        submission_source = SUBMISSIONS_API.read_text(encoding="utf-8")
        metrics_source = CHALLENGES_API.read_text(encoding="utf-8")

        for parameter in (
            "username:",
            "challenge_search:",
            "competition_search:",
            "submission_context:",
            "submitted_from:",
            "submitted_to:",
            "sort:",
        ):
            self.assertIn(parameter, submission_source)
        self.assertIn("_escape_like", submission_source)
        self.assertIn("_search_user_ids_by_username", submission_source)
        self.assertIn("_submission_log_order", submission_source)
        self.assertIn('@router.get("/admin/challenges/solve-metrics")', metrics_source)
        self.assertIn('"solver_count"', metrics_source)
        self.assertIn('"solve_rate"', metrics_source)
        self.assertIn('"latest_solved_at"', metrics_source)

    def test_every_submission_sort_has_a_unique_pagination_tie_break(self) -> None:
        order = self._load_submission_order()

        for sort in (
            "submitted_at_desc",
            "submitted_at_asc",
            "challenge_name_asc",
            "result_desc",
        ):
            self.assertTrue(order(sort)[-1].startswith("id."), sort)

    def test_ui_applies_exact_competition_scope_to_logs_and_metrics(self) -> None:
        source = ADMIN_PAGE.read_text(encoding="utf-8")

        self.assertIn(
            'logParams.set("competition_id", competitionSearch.trim())',
            source,
        )
        self.assertIn(
            'metricParams.set("competition_id", competitionSearch.trim())',
            source,
        )
        self.assertIn("page: String(metricPage)", source)
        self.assertIn("setMetricPage", source)

    def test_ui_uses_explicit_reveal_without_hmac_display(self) -> None:
        source = ADMIN_PAGE.read_text(encoding="utf-8")

        self.assertIn("submitted_flag_masked", source)
        self.assertIn("submitted_flag_revealable", source)
        self.assertIn('useTranslations("admin")', source)
        self.assertIn('t("submissionReveal")', source)
        self.assertNotIn("submitted_flag_fingerprint", source)
        self.assertNotIn("submitted_flag: string;", source)
        self.assertIn("/api/learn/submissions/logs", source)
        self.assertIn("/api/learn/admin/challenges/solve-metrics", source)

        for locale, expected in {
            "ko": ("원문 열람", "사유를 기록하고 열람", "열람 사유를 입력하세요."),
            "en": ("View original", "Record reason and view", "Enter a reason for access."),
            "ja": ("原文を閲覧", "理由を記録して閲覧", "閲覧理由を入力してください。"),
            "zh": ("查看原文", "记录原因并查看", "请输入查阅原因。"),
        }.items():
            with self.subTest(locale=locale):
                messages = json.loads(
                    (SERVICE_ROOT.parents[1] / "frontend" / "messages" / f"{locale}.json").read_text(encoding="utf-8")
                )["admin"]
                self.assertEqual(
                    tuple(messages[key] for key in (
                        "submissionReveal", "submissionRevealConfirm", "revealReasonError_required"
                    )),
                    expected,
                )

    def test_ui_requires_reason_before_posting_explicit_reveal(self) -> None:
        source = ADMIN_PAGE.read_text(encoding="utf-8")
        reveal = source[source.index("const revealFlag = async () => {"):source.index("const visibleFlags:")]

        self.assertIn("const normalized = normalizeRevealReason(revealReason);", reveal)
        self.assertRegex(reveal, r"if \(normalized\.error\) \{[^\n]*return; \}")
        self.assertLess(reveal.index("if (normalized.error)"), reveal.index("await apiFetch<unknown>"))
        self.assertIn("`/api/learn/submissions/logs/${target.scope.submissionId}/flag`", reveal)
        self.assertIn('method: "POST", body: { reason: normalized.reason }', reveal)
        self.assertNotIn('method: "GET"', reveal)
        self.assertIn('htmlFor="submission-reveal-reason"', source)
        textarea_start = source.index("<textarea")
        textarea = source[textarea_start:source.index("/>", textarea_start)]
        self.assertIn('id="submission-reveal-reason"', textarea)
        self.assertRegex(textarea, r"(?m)^\s+required\s*$")
        self.assertIn('t("revealReasonLabel")', source)
        self.assertIn('t("submissionRevealConfirm")', source)

    def test_model_records_the_legacy_plaintext_retention_boundary(self) -> None:
        source = CHALLENGE_MODEL.read_text(encoding="utf-8")

        self.assertIn("보존 경계", source)
        self.assertIn("API 응답", source)


if __name__ == "__main__":
    unittest.main()
