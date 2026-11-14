"""지식맵 기반 개인화 추천의 안전 경계를 고정하는 소스 계약 테스트."""

import ast
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]


class KnowledgePersonalizationRulesTests(unittest.TestCase):
    def test_recommendation_api_requires_consent_and_approved_public_relations(self) -> None:
        source = (SERVICE_ROOT / "app" / "api" / "recommend.py").read_text(encoding="utf-8")
        ast.parse(source)
        for contract in (
            '"/me/recommendation-consent"',
            '"/me/recommendations"',
            "RecommendationConsentEvent",
            "pg_advisory_xact_lock",
            "status == \"approved\"",
            "RECOMMENDATION_RELATION_TYPES",
            "public_only=True",
            "Submission.competition_id.is_(None)",
            "consent_required=True",
        ):
            self.assertIn(contract, source)
        self.assertNotIn("KnowledgeRelationProposal", source)

    def test_consent_models_are_append_only_and_scoped(self) -> None:
        source = (SERVICE_ROOT / "app" / "models" / "knowledge.py").read_text(encoding="utf-8")
        for contract in (
            '"recommendation_consents"',
            '"recommendation_consent_events"',
            "uq_recommendation_consent_user_scope",
            "ck_recommendation_consent_scope",
            "ck_recommendation_consent_event_action",
            "action IN ('enabled', 'disabled')",
        ):
            self.assertIn(contract, source)

    def test_migration_is_ordered_and_refuses_partial_schema(self) -> None:
        source = (
            SERVICE_ROOT
            / "migrations"
            / "versions"
            / "20260814_020_recommendation_consent_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision = "20260814_020"', source)
        self.assertIn('down_revision = "20260814_019"', source)
        self.assertIn("partially present", source)
        self.assertIn("destructive downgrade is refused", source)

    def test_frontend_keeps_consent_recommendations_and_map_separate(self) -> None:
        component_path = (
            SERVICE_ROOT.parents[1]
            / "frontend"
            / "src"
            / "components"
            / "learn"
            / "LearningRecommendations.tsx"
        )
        page = (
            SERVICE_ROOT.parents[1]
            / "frontend"
            / "src"
            / "app"
            / "[locale]"
            / "learn"
            / "page.tsx"
        ).read_text(encoding="utf-8")
        self.assertTrue(component_path.exists())
        self.assertIn("LearningRecommendations", page)
        component = component_path.read_text(encoding="utf-8")
        self.assertIn("recommendation-consent", component)
        self.assertIn("no_history", component)
        self.assertIn("switchView", page)
        self.assertIn('writeUrlQuery({ view: nextView === "map" ? "map" : null }, "push")', page)
        history_writer = (SERVICE_ROOT.parents[1] / "frontend" / "src" / "lib" / "url-query.ts").read_text(encoding="utf-8")
        self.assertIn('window.history[mode === "push" ? "pushState" : "replaceState"]', history_writer)
        self.assertRegex(history_writer, r'window\.history\[[^\]]+\]\(\s*null,')
        self.assertIn("window.addEventListener(\"popstate\"", page)
        self.assertIn('t("viewMap")', page)

    def test_design_requires_opt_in_and_mermaid_flow(self) -> None:
        design = (
            SERVICE_ROOT.parents[1]
            / "docs"
            / "design"
            / "knowledge-personalization-v1-design.md"
        ).read_text(encoding="utf-8")
        self.assertIn("enabled=false", design)
        self.assertIn("pending_review", design)
        self.assertIn("```mermaid", design)
        self.assertIn("graph_revision", design)


if __name__ == "__main__":
    unittest.main()
