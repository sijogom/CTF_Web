"""발행 시 결정적 지식 태그 투영 계약의 소스 회귀 테스트."""

from pathlib import Path
import ast
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeTagProjectionContractTests(unittest.TestCase):
    def _source(self, relative: str) -> str:
        return (SERVICE_ROOT / relative).read_text(encoding="utf-8")

    def test_projection_service_is_deterministic_and_provenance_bound(self) -> None:
        source = self._source("app/utils/knowledge_tag_projection.py")
        ast.parse(source)
        for required in (
            "normalize_legacy_tag_slugs",
            "challenge_metadata_tag_slugs",
            "project_rule_tag_mappings",
            "entity.is_published",
            "entity.challenge_type != \"wargame\"",
            "ContentStatus.APPROVED.value",
            '"origin": "rule"',
            "add_mapping_audit",
            "mapping_snapshot",
            "on_conflict_do_nothing",
            'CourseKnowledgeTag.origin != "model_candidate"',
            "CourseKnowledgeTag.approved_at.is_not(None)",
            "category=entity.category",
        ):
            self.assertIn(required, source)

    def test_projection_never_rewrites_manual_or_import_rows(self) -> None:
        source = self._source("app/utils/knowledge_tag_projection.py")
        self.assertIn('mapping.origin != "rule"', source)
        self.assertIn("수동/import/후보는 사용자 의도를 보존", source)

    def test_publish_boundaries_call_projection_before_commit(self) -> None:
        for relative in (
            "app/api/challenges.py",
            "app/api/courses.py",
            "app/api/lessons.py",
            "app/api/knowledge_tags.py",
        ):
            source = self._source(relative)
            ast.parse(source)
            self.assertIn("project_rule_tag_mappings", source)
            self.assertTrue("await project_rule_tag_mappings(" in source or "await replace_content_tag_mappings(" in source)

    def test_projection_is_not_model_candidate_approval(self) -> None:
        source = self._source("app/utils/knowledge_tag_projection.py")
        self.assertNotIn("KnowledgeRelationProposal", source)
        self.assertNotIn("pending_review", source)
        self.assertIn('"origin": "rule"', source)

    def test_challenge_category_is_an_exact_metadata_mapping_only(self) -> None:
        source = self._source("app/utils/knowledge_tag_projection.py")
        self.assertIn("문제의 공개 category/tags 메타데이터와 지식 태그 slug 일치", source)
        self.assertIn("category_slug = category.strip().lower()", source)
        helper_source = source.split("def challenge_metadata_tag_slugs", 1)[1].split("async def", 1)[0]
        self.assertNotIn("display_name", helper_source)

    def test_reconcile_resolves_only_the_selected_mapping_column(self) -> None:
        source = self._source("app/api/knowledge_tags.py")
        self.assertIn('getattr(model, f"{entity_type}_id")', source)
        self.assertNotIn('"challenge": model.challenge_id', source)

    def test_editing_a_mapping_does_not_approve_model_candidates(self) -> None:
        for relative in (
            "app/api/challenges.py",
            "app/api/lessons.py",
            "app/api/knowledge_tags.py",
        ):
            source = self._source(relative)
            ast.parse(source)
            helper = self._source("app/utils/content_tag_mappings.py")
            self.assertTrue("replace_content_tag_mappings" in source or '"model_candidate"' in source)
            self.assertNotIn("previous.approved_at =", helper)


if __name__ == "__main__":
    unittest.main()
