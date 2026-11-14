"""지식 매핑 V2의 DB 독립 계약 테스트.

PostgreSQL migration과 실제 권한 경로는 VMware 단계에서 별도로 검증한다. 이 파일은
자동 후보가 민감 입력을 받아 공개 상태로 가는 것을 막는 순수 규칙, Pydantic 계약,
라우트 계약, ORM 제약의 회귀를 빠르게 잡는다.
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys
import unittest

from pydantic import ValidationError

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.utils.knowledge_mapping_v2 import (  # noqa: E402
    DETERMINISTIC_ASSIGNMENT_RESOURCE_HELPS_V1,
    DETERMINISTIC_CONTENT_TAG_AFFINITY_V1,
    DETERMINISTIC_TAG_PREPARES_FOR_V1,
    KnowledgeMappingValidationError,
    build_deterministic_tag_prepares_for_candidates,
    build_deterministic_content_tag_affinity_candidates,
    combined_profile_revision,
    decode_focus_continuation,
    deterministic_tag_prepares_for_candidate,
    deterministic_assignment_resource_helps_candidate,
    deterministic_content_tag_affinity_candidate,
    deterministic_tag_prepares_for_configuration,
    digest_json,
    encode_focus_continuation,
    next_proposal_state,
    normalize_allowed_profile,
    normalize_metadata_tag_slugs,
    public_evidence_summary,
    system_structure_edge_id,
    system_tag_edge_id,
    system_tag_parent_edge_id,
    validate_structured_evidence,
)
from app.schemas.knowledge import (  # noqa: E402
    KnowledgeCandidateGenerationRequest,
    KnowledgeCandidateRunCreate,
    KnowledgeRelationProposalCreate,
)


class KnowledgeMappingV2InputTests(unittest.TestCase):
    def test_metadata_tag_slugs_are_safe_and_deterministic(self) -> None:
        self.assertEqual(
            normalize_metadata_tag_slugs(
                category="Web",
                difficulty="beginner",
                tags=["client-side", "web", "DROP TABLE", "CTF{secret}", 42],
            ),
            ["beginner", "client-side", "drop-table", "web"],
        )

    def test_metadata_tag_slug_generation_does_not_read_descriptions(self) -> None:
        self.assertEqual(
            normalize_metadata_tag_slugs(
                category="misc",
                difficulty="introductory",
                tags=["  ", "한글 태그"],
            ),
            ["introductory", "misc"],
        )

    def test_tag_mapping_router_imports_runtime_annotation_types(self) -> None:
        """The router must import every annotation evaluated at module load."""
        source = (SERVICE_ROOT / "app" / "api" / "knowledge_tags.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.asname or alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertIn("ContentTagMappingItem", imported)
        self.assertIn("IntegrityError", imported)

    def test_allowed_profile_is_deterministic_and_bounded_to_allowlist(self) -> None:
        profile = normalize_allowed_profile(
            {
                "entity_type": "challenge",
                "entity_id": "42",
                "title": "SQL 입력 검증 기초",
                "category": "web",
                "learning_objectives": ["파라미터 바인딩 이해"],
                "approved_tag_slugs": ["sql-injection", "web"],
                "prerequisite_refs": [{"type": "lesson", "id": "11111111-1111-1111-1111-111111111111"}],
                "description_metadata": {
                    "summary": "입력 검증과 쿼리 바인딩을 연습합니다.",
                    "keywords": ["sql", "validation"],
                },
            }
        )
        self.assertEqual(profile["approved_tag_slugs"], ["sql-injection", "web"])
        self.assertEqual(digest_json(profile), digest_json(profile))
        self.assertEqual(
            combined_profile_revision("a" * 64, "b" * 64),
            combined_profile_revision("a" * 64, "b" * 64),
        )

    def test_profile_rejects_flag_email_and_unknown_fields(self) -> None:
        base = {"entity_type": "challenge", "entity_id": "7", "title": "안전한 입력"}
        with self.assertRaises(KnowledgeMappingValidationError):
            normalize_allowed_profile({**base, "title": "CTF{not-a-profile}"})
        with self.assertRaises(KnowledgeMappingValidationError):
            normalize_allowed_profile({**base, "description_metadata": {"summary": "test@example.com"}})
        with self.assertRaises(KnowledgeMappingValidationError):
            normalize_allowed_profile({**base, "raw_solution": "설명"})

    def test_structured_evidence_rejects_sensitive_keys_and_raw_flags(self) -> None:
        with self.assertRaises(KnowledgeMappingValidationError):
            validate_structured_evidence({"flag_text": "x"})
        with self.assertRaises(KnowledgeMappingValidationError):
            validate_structured_evidence({"summary": "CTF{private}"})
        self.assertEqual(validate_structured_evidence({"tag_overlap": 0.8}), {"tag_overlap": 0.8})

    def test_proposal_schema_requires_evidence_and_model_version(self) -> None:
        with self.assertRaises(ValidationError):
            KnowledgeRelationProposalCreate.model_validate(
                {
                    "source": {"type": "challenge", "id": "1"},
                    "target": {"type": "challenge", "id": "2"},
                    "relation_type": "helps",
                    "origin": "model_candidate",
                    "evidence": [{"evidence_kind": "model", "summary": "태그 일치"}],
                }
            )
        with self.assertRaises(ValidationError):
            KnowledgeRelationProposalCreate.model_validate(
                {
                    "source": {"type": "challenge", "id": "1"},
                    "target": {"type": "challenge", "id": "2"},
                    "relation_type": "helps",
                    "evidence": [{"evidence_kind": "manual", "summary": "CTF{private}"}],
                }
            )
        proposal = KnowledgeRelationProposalCreate.model_validate(
            {
                "source": {"type": "challenge", "id": "1"},
                "target": {"type": "lesson", "id": "11111111-1111-1111-1111-111111111111"},
                "relation_type": "prepares_for",
                "origin": "model_candidate",
                "quality_version": "embedding-policy-2026-08",
                "evidence": [{"evidence_kind": "model", "summary": "승인 태그와 교육 목표가 일치"}],
            }
        )
        self.assertEqual(proposal.relation_type, "prepares_for")
        with self.assertRaises(ValidationError):
            KnowledgeRelationProposalCreate.model_validate(
                {
                    "source": {"type": "challenge", "id": "1"},
                    "target": {"type": "challenge", "id": "2"},
                    "relation_type": "helps",
                    "evidence": [
                        {
                            "evidence_kind": "reference",
                            "summary": "공개 메타데이터 참조",
                            "locator": "https://example.invalid/ref?token=should-not-be-stored",
                        }
                    ],
                }
            )

    def test_candidate_run_is_dry_run_only_in_first_slice(self) -> None:
        valid_digest = "a" * 64
        run = KnowledgeCandidateRunCreate.model_validate(
            {
                "run_kind": "rule",
                "algorithm_version": "tag-overlap-v1",
                "configuration_digest": valid_digest,
                "input_snapshot_digest": valid_digest,
            }
        )
        self.assertTrue(run.dry_run)
        with self.assertRaises(ValidationError):
            KnowledgeCandidateRunCreate.model_validate(
                {
                    "run_kind": "rule",
                    "algorithm_version": "tag-overlap-v1",
                    "configuration_digest": valid_digest,
                    "input_snapshot_digest": valid_digest,
                    "dry_run": False,
                }
            )

class DeterministicCandidateGenerationRuleTests(unittest.TestCase):
    @staticmethod
    def _profile(entity_type: str, entity_id: str, tags: list[str], *, category: str = "web") -> dict:
        return {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": f"{entity_type}-{entity_id}",
            "category": category,
            "approved_tag_slugs": tags,
        }

    def test_tag_candidate_is_deterministic_and_never_uses_unapproved_fields(self) -> None:
        source = self._profile("course", "11111111-1111-1111-1111-111111111111", ["web", "sql"])
        target = self._profile("challenge", "41", ["sql", "web"])
        candidate = deterministic_tag_prepares_for_candidate(source, target)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.relation_type, "prepares_for")
        self.assertEqual(candidate.score_component["rule_version"], DETERMINISTIC_TAG_PREPARES_FOR_V1)
        self.assertEqual(candidate.score_component["shared_tag_slugs"], ["sql", "web"])
        self.assertLessEqual(candidate.confidence, 0.7)
        self.assertNotIn("title", candidate.score_component)
        self.assertNotIn("flag", candidate.score_component)

    def test_tag_candidate_rejects_non_matching_or_wrong_direction_profiles(self) -> None:
        source = self._profile("lesson", "11111111-1111-1111-1111-111111111111", ["web"])
        target = self._profile("challenge", "42", ["crypto"], category="crypto")
        self.assertIsNone(deterministic_tag_prepares_for_candidate(source, target))
        self.assertIsNone(
            deterministic_tag_prepares_for_candidate(
                self._profile("challenge", "1", ["web"]),
                self._profile("challenge", "2", ["web"]),
            )
        )

    def test_assignment_resource_candidate_uses_only_explicit_link_metadata(self) -> None:
        assignment = self._profile("assignment", "11111111-1111-1111-1111-111111111111", [])
        lesson = self._profile("lesson", "22222222-2222-2222-2222-222222222222", [])
        candidate = deterministic_assignment_resource_helps_candidate(assignment, lesson)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.relation_type, "helps")
        self.assertEqual(candidate.algorithm_version, DETERMINISTIC_ASSIGNMENT_RESOURCE_HELPS_V1)
        self.assertEqual(candidate.score_component["evidence_kind"], "education_resource_link")
        self.assertTrue(candidate.score_component["explicit_link"])
        self.assertIsNone(
            deterministic_assignment_resource_helps_candidate(
                self._profile("event", "11111111-1111-1111-1111-111111111111", []),
                lesson,
            )
        )

    def test_content_tag_candidates_are_explainable_and_bounded(self) -> None:
        content = self._profile("course", "11111111-1111-1111-1111-111111111111", [], category="web-security")
        tag = self._profile("knowledge_tag", "17", ["web-security"], category="knowledge_tag")
        candidate = deterministic_content_tag_affinity_candidate(content, tag)
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.relation_type, "tag_affinity")
        self.assertEqual(candidate.target_entity_type, "knowledge_tag")
        self.assertEqual(candidate.algorithm_version, DETERMINISTIC_CONTENT_TAG_AFFINITY_V1)
        self.assertIn("shared_token_count", candidate.score_component)
        self.assertNotIn("description", candidate.score_component)

        candidates, truncated, total = build_deterministic_content_tag_affinity_candidates(
            [content],
            [
                tag,
                self._profile("knowledge_tag", "18", ["web-security"], category="knowledge_tag"),
            ],
            max_candidates=1,
        )
        self.assertEqual(total, 2)
        self.assertTrue(truncated)
        self.assertEqual(len(candidates), 1)

    def test_exact_approved_tag_remains_candidate_when_content_has_other_metadata(self) -> None:
        content = self._profile(
            "challenge",
            "42",
            ["web", "beginner", "client-side"],
            category="web",
        )
        tag = self._profile("knowledge_tag", "17", ["web"], category="knowledge_tag")

        candidate = deterministic_content_tag_affinity_candidate(content, tag)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.score_component["tag_coverage"], 1.0)
        self.assertGreaterEqual(candidate.strength, 0.55)

    def test_candidate_order_is_input_order_independent_and_bounded(self) -> None:
        sources = [
            self._profile("course", "22222222-2222-2222-2222-222222222222", ["web"]),
            self._profile("lesson", "11111111-1111-1111-1111-111111111111", ["web", "sql"]),
        ]
        targets = [
            self._profile("challenge", "2", ["web"]),
            self._profile("challenge", "1", ["web", "sql"]),
        ]
        first, first_truncated, first_total = build_deterministic_tag_prepares_for_candidates(
            sources,
            targets,
            max_candidates=2,
        )
        second, second_truncated, second_total = build_deterministic_tag_prepares_for_candidates(
            list(reversed(sources)),
            list(reversed(targets)),
            max_candidates=2,
        )
        self.assertTrue(first_truncated)
        self.assertTrue(second_truncated)
        self.assertGreater(first_total, len(first))
        self.assertEqual(first_total, second_total)
        self.assertEqual(
            [(item.source_entity_id, item.target_entity_id, item.strength) for item in first],
            [(item.source_entity_id, item.target_entity_id, item.strength) for item in second],
        )

    def test_bounded_generation_keeps_the_highest_ranked_candidate(self) -> None:
        source = self._profile("course", "11111111-1111-1111-1111-111111111111", ["web", "sql"])
        candidates, truncated, total = build_deterministic_tag_prepares_for_candidates(
            [source],
            [
                self._profile("challenge", "lower", ["web", "sql", "crypto"]),
                self._profile("challenge", "higher", ["web", "sql"]),
            ],
            max_candidates=1,
        )
        self.assertTrue(truncated)
        self.assertEqual(total, 2)
        self.assertEqual([candidate.target_entity_id for candidate in candidates], ["higher"])

    def test_generation_request_keeps_dry_run_safe_default_but_allows_explicit_pending_generation(self) -> None:
        self.assertTrue(KnowledgeCandidateGenerationRequest().dry_run)
        self.assertFalse(KnowledgeCandidateGenerationRequest(dry_run=False, max_candidates=25).dry_run)
        with self.assertRaises(ValidationError):
            KnowledgeCandidateGenerationRequest(max_candidates=0)
        self.assertEqual(
            digest_json(deterministic_tag_prepares_for_configuration()),
            digest_json(deterministic_tag_prepares_for_configuration()),
        )


class CandidateLedgerP1Tests(unittest.TestCase):
    def test_input_snapshot_keeps_allowlisted_profile_and_registry_node_identity_only(self) -> None:
        """SQLAlchemy 없는 source-only 환경에서도 snapshot privacy contract를 고정한다."""
        source = (
            SERVICE_ROOT / "app" / "utils" / "knowledge_candidate_generation.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'CANDIDATE_INPUT_SNAPSHOT_SCHEMA_VERSION = "candidate-input-profile-node-v2"',
            source,
        )
        self.assertIn("def _candidate_node_snapshot(", source)
        self.assertIn('"registry": registry_snapshot', source)
        self.assertIn('"state": "absent"', source)
        self.assertIn('"state": "present"', source)
        self.assertIn("nodes_by_content_key: Mapping[tuple[str, str], KnowledgeContentNode]", source)
        self.assertIn("normalize_allowed_profile(item.profile)", source)
        self.assertNotIn('"href": item.content.href', source)
        self.assertNotIn('"display_name": item.content.display_name', source)

    def test_candidate_service_records_commit_boundary_and_only_expected_duplicates(self) -> None:
        service_source = (
            SERVICE_ROOT / "app" / "services" / "knowledge_candidate_generation.py"
        ).read_text(encoding="utf-8")
        relation_source = (
            SERVICE_ROOT / "app" / "api" / "knowledge_relations.py"
        ).read_text(encoding="utf-8")
        model_source = (SERVICE_ROOT / "app" / "models" / "knowledge.py").read_text(encoding="utf-8")
        schema_source = (SERVICE_ROOT / "app" / "schemas" / "knowledge.py").read_text(encoding="utf-8")

        self.assertIn("input_snapshot_schema_version", service_source)
        self.assertIn("requested_max_candidates", service_source)
        self.assertIn("nodes_by_content_key=nodes_by_content_key", service_source)
        self.assertIn("commit_attempted = False", service_source)
        self.assertIn("if not commit_attempted:", service_source)
        self.assertIn("connection loss after commit/rollback", service_source)
        self.assertNotIn("await db.refresh(run)", service_source)
        self.assertLess(
            service_source.index("result = CandidateGenerationResult("),
            service_source.index("await db.commit()", service_source.index("result = CandidateGenerationResult(")),
        )
        self.assertIn("getattr(original, \"constraint_name\", None)", service_source)
        self.assertIn("if not _is_expected_candidate_proposal_duplicate(exc):", service_source)
        self.assertEqual(
            service_source.count("_record_candidate_run_failure("),
            2,
            "실행 함수에는 commit 전 실패에만 단일 failure-ledger 경로가 있어야 한다.",
        )
        self.assertIn("_is_expected_unique_constraint_violation", relation_source)
        self.assertIn("if _is_expected_unique_constraint_violation(exc, \"uq_knowledge_relation_active_pair\")", relation_source)
        self.assertIn("if _is_expected_unique_constraint_violation(exc, \"uq_knowledge_relation_proposal_active_pair\")", relation_source)
        self.assertIn("_lock_candidate_relation_pair", service_source)
        self.assertIn("_candidate_pair_is_currently_blocked", service_source)
        self.assertIn("build_deterministic_content_tag_affinity_candidates", service_source)
        self.assertIn('"tag_affinity"', service_source)
        self.assertIn("pending_review", service_source)
        self.assertIn("_sync_tag_affinity_mapping", relation_source)
        self.assertIn("add_mapping_audit", relation_source)
        self.assertIn('_CANDIDATE_BLOCKING_RELATION_TYPES = ("requires", "prepares_for", "tag_affinity")', relation_source)
        self.assertIn("requested_max_candidates", model_source)
        self.assertIn("input_snapshot_schema_version", model_source)
        self.assertIn("requested_max_candidates", schema_source)
        self.assertIn("input_snapshot_schema_version", schema_source)
        self.assertIn('"tag_affinity"', schema_source)
        self.assertNotIn("db.add(KnowledgeRelation(", service_source)
        self.assertNotIn("KnowledgeProjectionOutbox", service_source)


class KnowledgeMappingV2StateAndPublicContractTests(unittest.TestCase):
    def test_tag_junctions_store_mapping_provenance_and_graph_consumes_it(self) -> None:
        from app.models.knowledge import ChallengeKnowledgeTag, CourseKnowledgeTag, LessonKnowledgeTag
        from app.schemas.knowledge import ContentTagMappingUpdate, ContentTagMappingResponse
        for model in (ChallengeKnowledgeTag, CourseKnowledgeTag, LessonKnowledgeTag):
            for field in ("direction", "strength", "confidence", "evidence_summary", "origin", "created_by", "approved_at", "source_relation_id"):
                self.assertIn(field, model.__table__.columns)
        self.assertIn("expected_revision", ContentTagMappingUpdate.model_fields)
        self.assertIn("mapping_revision", ContentTagMappingResponse.model_fields)
        helper = (SERVICE_ROOT / "app" / "utils" / "content_tag_mappings.py").read_text(encoding="utf-8")
        self.assertIn("add_mapping_audit", helper)
        self.assertIn("project_rule_tag_mappings", helper)
        self.assertNotIn("await db.commit()", helper)

    def test_projection_edge_ids_are_stable_and_namespace_separated(self) -> None:
        tag_edge = system_tag_edge_id(content_type="course", content_id="course-1", tag_id="7")
        self.assertEqual(
            tag_edge,
            system_tag_edge_id(content_type="course", content_id="course-1", tag_id="7"),
        )
        self.assertNotEqual(
            tag_edge,
            system_tag_edge_id(content_type="lesson", content_id="course-1", tag_id="7"),
        )
        self.assertNotEqual(
            tag_edge,
            system_structure_edge_id(course_id="course-1", lesson_id="lesson-1"),
        )
        parent_edge = system_tag_parent_edge_id(child_tag_id="7", parent_tag_id="2")
        self.assertEqual(parent_edge, system_tag_parent_edge_id(child_tag_id="7", parent_tag_id="2"))
        self.assertNotEqual(parent_edge, tag_edge)

    def test_v2_graph_projects_real_tag_and_course_structure_mappings(self) -> None:
        from app.services.knowledge_graph_pages import public_nodes_query, public_edges_query
        from sqlalchemy.dialects import postgresql
        edges = public_edges_query(public_nodes_query(), {"challenge", "course", "lesson", "knowledge_tag"},
                                   {"tag_affinity", "tag_parent", "contains"})
        sql = str(edges.select().compile(dialect=postgresql.dialect()))
        for table in ("challenge_knowledge_tags", "course_knowledge_tags", "lesson_knowledge_tags", "lessons", "knowledge_tags"):
            self.assertIn(table, sql)
        self.assertIn("approved_at", sql)
        self.assertIn("parent_id", sql)

    def test_tag_mapping_is_projection_only_and_does_not_auto_approve_candidates(self) -> None:
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        projection_source = graph_source[graph_source.index("async def _v2_tag_projection_edges"):]
        self.assertNotIn("db.add(KnowledgeRelation(", projection_source)
        self.assertNotIn("status=\"approved\"", projection_source)

    def test_review_state_transitions_are_fail_closed(self) -> None:
        self.assertEqual(next_proposal_state("pending_review", "approve"), "approved")
        self.assertEqual(next_proposal_state("pending_review", "reject"), "rejected")
        self.assertEqual(next_proposal_state("approved", "retire"), "retired")
        with self.assertRaises(KnowledgeMappingValidationError):
            next_proposal_state("draft", "approve")
        with self.assertRaises(KnowledgeMappingValidationError):
            next_proposal_state("rejected", "retire")

    def test_focus_continuation_is_metadata_only_and_round_trips(self) -> None:
        token = encode_focus_continuation(
            focus_type="challenge",
            focus_id="17",
            depth=2,
            types=["lesson", "challenge"],
            relation_types=["helps", "prepares_for"],
        )
        parsed = decode_focus_continuation(token)
        self.assertEqual(parsed["focus_type"], "challenge")
        self.assertEqual(parsed["focus_id"], "17")
        self.assertEqual(parsed["depth"], 2)
        self.assertNotIn("user", token)
        with self.assertRaises(KnowledgeMappingValidationError):
            decode_focus_continuation("v1.invalid!!!")

    def test_public_evidence_fails_closed_for_sensitive_summary(self) -> None:
        self.assertEqual(public_evidence_summary("승인 태그 일치"), "승인 태그 일치")
        self.assertEqual(public_evidence_summary("CTF{secret}"), "관리자가 승인한 관계 근거")

    def test_private_only_tag_cannot_drift_between_overview_search_and_focus(self) -> None:
        """태그 공개 범위는 공개 원본 연결 하나로만 판단하고 세 V2 진입점이 공유한다."""
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        relation_utils_source = (
            SERVICE_ROOT / "app" / "utils" / "knowledge_relations.py"
        ).read_text(encoding="utf-8")
        self.assertIn("def public_knowledge_tag_ids_query()", relation_utils_source)
        self.assertIn("Challenge.challenge_type == \"wargame\"", relation_utils_source)
        self.assertIn("Course.status == ContentStatus.APPROVED.value", relation_utils_source)
        self.assertIn("if public_only and not is_active", relation_utils_source)
        self.assertIn("href=f\"/learn?view=map&focus_type=knowledge_tag&focus_id={tag.id}\" if is_active else None", relation_utils_source)
        self.assertIn("source_updated_at=tag.updated_at or tag.created_at", relation_utils_source)
        # overview count와 search는 같은 query를 쓰고, focus는 resolve_content(public_only=True)를
        # 거치므로 private-only 태그가 세 경로 중 하나에만 새지 않는다.
        self.assertGreaterEqual(graph_source.count("public_knowledge_tag_ids_query()"), 2)
        self.assertIn("KnowledgeTag.id.in_(public_knowledge_tag_ids_query())", graph_source)
        self.assertIn("resolve_content(db, focus_type, normalized_id, public_only=True)", graph_source)

    def test_public_tag_scope_keeps_public_taxonomy_ancestors(self) -> None:
        """공개 자식의 부모 taxonomy가 tag_parent 투영에서 끊기지 않아야 한다."""
        relation_utils_source = (
            SERVICE_ROOT / "app" / "utils" / "knowledge_relations.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'select(directly_public_subquery.c.tag_id).cte(',
            relation_utils_source,
        )
        self.assertIn('"public_knowledge_tag_ids", recursive=True', relation_utils_source)
        self.assertIn('KnowledgeTag.parent_id.label("tag_id")', relation_utils_source)
        self.assertIn("join(public_tags, KnowledgeTag.id == public_tags.c.tag_id)", relation_utils_source)
        self.assertIn("public_tags = public_tags.union(ancestors)", relation_utils_source)
        self.assertIn("공개 자식의 분류 경로", relation_utils_source)

    def test_legacy_graph_uses_the_same_public_tag_scope(self) -> None:
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        # Legacy graph responses must not reintroduce private-only tags after V2
        # established the shared public-tag subquery.
        self.assertGreaterEqual(graph_source.count("KnowledgeTag.id.in_(public_knowledge_tag_ids_query())"), 4)

    def test_legacy_graph_keeps_the_public_wargame_boundary(self) -> None:
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        legacy_source = graph_source.split("async def _build_graph", 1)[1].split('@router.get("/knowledge-graph"', 1)[0]
        self.assertIn("Challenge.is_published.is_(True)", legacy_source)
        self.assertIn('Challenge.challenge_type == "wargame"', legacy_source)
        self.assertNotIn('Challenge.challenge_type == "competition"', legacy_source)

    def test_v2_routes_and_orm_contract_are_additive_in_source(self) -> None:
        """의존성 미설치 환경에서도 public contract 삭제 회귀를 정적으로 잡는다."""
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        relation_source = (SERVICE_ROOT / "app" / "api" / "knowledge_relations.py").read_text(encoding="utf-8")
        model_source = (SERVICE_ROOT / "app" / "models" / "knowledge.py").read_text(encoding="utf-8")
        schema_source = (SERVICE_ROOT / "app" / "schemas" / "knowledge.py").read_text(encoding="utf-8")
        for route in (
            '"/knowledge-map/v2/overview"',
            '"/knowledge-map/v2/canvas"',
            '"/knowledge-map/v2/focus"',
            '"/knowledge-map/v2/search"',
            '"/knowledge-map"',
        ):
            self.assertIn(route, graph_source)
        for route in (
            '"/knowledge-proposals"',
            '"/knowledge-proposals/{proposal_id}/review"',
            '"/knowledge-candidate-runs"',
            '"/knowledge-candidate-runs/generate"',
        ):
            self.assertIn(route, relation_source)
        for table_name in (
            "knowledge_relations",
            "knowledge_content_profiles",
            "knowledge_relation_proposals",
            "knowledge_candidate_runs",
            "knowledge_projection_outbox",
        ):
            self.assertIn(table_name, model_source)
        self.assertIn("prepares_for", model_source)
        self.assertIn("'event', 'assignment'", model_source)
        self.assertIn('"event", "assignment"', schema_source)
        self.assertIn('PUBLIC_CONTENT_TYPES = {', (SERVICE_ROOT / "app" / "utils" / "knowledge_relations.py").read_text(encoding="utf-8"))
        self.assertIn("KnowledgeRelationProposalCreate", schema_source)
        self.assertIn("KnowledgeCandidateGenerationRequest", schema_source)
        self.assertIn("class KnowledgeMapV2SearchResult", schema_source)
        self.assertIn("class KnowledgeMapV2CanvasResponse", schema_source)
        self.assertIn("async def _v2_build_canvas", graph_source)
        self.assertIn('"mode": "canvas"', graph_source)
        self.assertIn("entity_id: str", schema_source)
        self.assertIn("entity_id=str(challenge.id)", graph_source)
        self.assertIn("entity_id=str(course.id)", graph_source)
        self.assertIn("entity_id=str(lesson.id)", graph_source)
        self.assertIn("approved_at: datetime | None = None", schema_source)
        self.assertIn("approved_at=relation.approved_at", graph_source)
        self.assertIn("Literal[True]", schema_source)
        self.assertIn("collect_public_tag_candidate_inputs", relation_source)
        self.assertIn("state=\"pending_review\"", relation_source)
        self.assertIn("_lock_candidate_relation_pair", relation_source)
        self.assertIn("_assert_no_competing_candidate_relation", relation_source)
        self.assertIn("_is_expected_candidate_proposal_duplicate", relation_source)
        self.assertIn("candidate_count_before_limit", relation_source)
        self.assertIn("candidate_snapshot=_candidate_run_item_snapshot", relation_source)
        self.assertIn("content_types.add(focus_type)", graph_source)
        self.assertIn("omitted_counts[\"node_limit\"]", graph_source)
        self.assertIn("omitted_counts[\"edge_limit\"]", graph_source)
        self.assertIn("graph_page(db, context=context", graph_source)
        self.assertIn("next_cursor=next_cursor", graph_source)
        self.assertLess(
            relation_source.index("response = KnowledgeCandidateGenerationResponse("),
            relation_source.index("await db.commit()", relation_source.index("response = KnowledgeCandidateGenerationResponse(")),
        )

    def test_event_and_assignment_nodes_are_permission_filtered_and_explainable(self) -> None:
        graph_source = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        relation_source = (SERVICE_ROOT / "app" / "utils" / "knowledge_relations.py").read_text(encoding="utf-8")
        self.assertIn("is_publicly_visible", relation_source)
        self.assertIn("PlatformEvent", relation_source)
        self.assertIn("TeachingClassEnrollment", relation_source)
        self.assertIn("EducationRoleGrant", relation_source)
        self.assertIn("if public_only:", relation_source)
        self.assertIn("if not is_active or viewer_id is None:", relation_source)
        self.assertIn('"event" in content_types', graph_source)
        self.assertIn('"assignment" in content_types and viewer_id', graph_source)
        self.assertIn("viewer_id=Depends(get_optional_user_id)", graph_source)

    def test_full_canvas_projects_approved_registry_edges(self) -> None:
        from uuid import UUID
        from app.services.knowledge_graph_pages import public_nodes_query, public_edges_query
        from sqlalchemy.dialects import postgresql
        nodes = public_nodes_query(UUID("11111111-1111-1111-1111-111111111111"))
        edges = public_edges_query(nodes, {"challenge", "event", "assignment"}, {"helps"})
        sql = str(edges.select().compile(dialect=postgresql.dialect()))
        for name in ("knowledge_relations", "teaching_class_enrollments", "education_role_grants", "platform_events"):
            self.assertIn(name, sql)
        self.assertIn("page_source", sql)
        self.assertIn("page_target", sql)
        self.assertNotIn("content_i18n", sql)

    def test_graph_revision_invalidates_event_and_education_visibility_changes(self) -> None:
        migration = (SERVICE_ROOT / "migrations" / "versions" / "20261013_028_knowledge_mapping_ownership_revision.py").read_text(encoding="utf-8")
        for table in ("platform_events", "teaching_classes", "education_assignments", "teaching_class_enrollments", "education_role_grants"):
            self.assertIn('"' + table + '"', migration)
        self.assertIn("AFTER INSERT OR UPDATE OR DELETE", migration)
        self.assertIn("txid_current()", migration)
        graph = (SERVICE_ROOT / "app" / "api" / "knowledge_graph.py").read_text(encoding="utf-8")
        revision = graph.split("async def _v2_graph_revision", 1)[1].split("async def _v2_registry_node", 1)[0]
        self.assertIn("func.count(KnowledgeGraphRevision.id)", revision)
        self.assertIn("visible_until_at <= now", revision)

    def test_v2_migration_follows_dynamic_flag_revision_and_fails_closed_on_data(self) -> None:
        migration_source = (
            SERVICE_ROOT
            / "migrations"
            / "versions"
            / "20260813_006_knowledge_mapping_v2_core.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision: str = "20260813_006"', migration_source)
        self.assertIn('down_revision: Union[str, None] = "20260813_005"', migration_source)
        self.assertIn("knowledge_content_profiles", migration_source)
        self.assertIn("knowledge_relation_proposals", migration_source)
        self.assertIn("knowledge_projection_outbox", migration_source)
        self.assertIn("destructive downgrade", migration_source)

    def test_candidate_run_reproducibility_migration_is_ordered_and_fail_closed(self) -> None:
        migration_source = (
            SERVICE_ROOT
            / "migrations"
            / "versions"
            / "20260813_007_knowledge_candidate_run_reproducibility.py"
        ).read_text(encoding="utf-8")
        self.assertIn('revision: str = "20260813_007"', migration_source)
        self.assertIn('down_revision: Union[str, None] = "20260813_006"', migration_source)
        self.assertIn("input_snapshot", migration_source)
        self.assertIn("candidate_count_before_limit", migration_source)
        self.assertIn("candidate_snapshot", migration_source)
        self.assertIn("preview_only", migration_source)
        self.assertIn("destructive downgrade", migration_source)

    def test_scheduled_candidate_runner_is_disabled_by_default_and_pending_review_only(self) -> None:
        """DB/Compose를 기동하지 않고 scheduled runner의 안전 경계를 정적으로 확인한다."""
        service_source = (
            SERVICE_ROOT / "app" / "services" / "knowledge_candidate_generation.py"
        ).read_text(encoding="utf-8")
        worker_source = (
            SERVICE_ROOT / "app" / "workers" / "knowledge_candidate_runner.py"
        ).read_text(encoding="utf-8")
        relation_source = (SERVICE_ROOT / "app" / "api" / "knowledge_relations.py").read_text(encoding="utf-8")
        model_source = (SERVICE_ROOT / "app" / "models" / "knowledge.py").read_text(encoding="utf-8")
        schema_source = (SERVICE_ROOT / "app" / "schemas" / "knowledge.py").read_text(encoding="utf-8")
        migration_source = (
            SERVICE_ROOT
            / "migrations"
            / "versions"
            / "20260813_008_knowledge_candidate_scheduled_runner.py"
        ).read_text(encoding="utf-8")
        compose_source = (SERVICE_ROOT.parents[1] / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        production_compose_source = (
            SERVICE_ROOT.parents[1] / "infra" / "docker-compose.prod.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("scheduled_candidate_runner_settings_from_environment", service_source)
        self.assertIn("default=False", service_source)
        self.assertIn("pg_try_advisory_lock", service_source)
        self.assertIn("deduplication_key", service_source)
        self.assertIn("_RUNNING_CANDIDATE_RUN_RECOVERY_AFTER", service_source)
        self.assertIn("_is_recoverable_running_candidate_run", service_source)
        self.assertIn('state="pending_review"', service_source)
        self.assertNotIn("db.add(KnowledgeRelation(", service_source)
        self.assertNotIn("KnowledgeProjectionOutbox", service_source)
        self.assertIn("requested_by=None", worker_source)
        self.assertIn("asyncio.Lock", worker_source)
        self.assertNotIn("logger.exception", worker_source)
        self.assertIn("CandidateGenerationInvocation(", relation_source)
        self.assertIn("trigger_kind=MANUAL_TRIGGER_KIND", relation_source)
        self.assertIn('"/knowledge-candidate-runner/status"', relation_source)
        self.assertIn("trigger_kind = Column", model_source)
        self.assertIn("deduplication_key = Column", model_source)
        self.assertIn("requested_by: UUID | None", schema_source)
        self.assertIn("KnowledgeCandidateRunnerStatusResponse", schema_source)
        self.assertIn('revision: str = "20260813_008"', migration_source)
        self.assertIn('down_revision: Union[str, None] = "20260813_007"', migration_source)
        self.assertIn("destructive downgrade", migration_source)
        self.assertIn("KNOWLEDGE_CANDIDATE_RUNNER_ENABLED: ${KNOWLEDGE_CANDIDATE_RUNNER_ENABLED:-false}", compose_source)
        self.assertIn("knowledge-candidate-runner:", compose_source)
        self.assertIn("knowledge-candidate-runner:", production_compose_source)
        self.assertIn(
            "KNOWLEDGE_CANDIDATE_RUNNER_ENABLED: ${KNOWLEDGE_CANDIDATE_RUNNER_ENABLED:-true}",
            production_compose_source,
        )
        self.assertIn("pending_review", production_compose_source)
        self.assertIn("자동 승인·자동 공개는 하지 않는다", production_compose_source)
        self.assertIn("read_only: true", production_compose_source)

    def test_candidate_generation_uses_the_same_public_tag_scope_as_the_graph(self) -> None:
        candidate_source = (
            SERVICE_ROOT / "app" / "utils" / "knowledge_candidate_generation.py"
        ).read_text(encoding="utf-8")
        self.assertIn("public_knowledge_tag_ids_query", candidate_source)
        self.assertGreaterEqual(
            candidate_source.count("KnowledgeTag.id.in_(public_knowledge_tag_ids_query())"),
            3,
        )

    def test_public_course_and_lesson_pages_open_their_real_graph_focus(self) -> None:
        frontend_root = SERVICE_ROOT.parent.parent / "frontend"
        course_source = (frontend_root / "src" / "app" / "[locale]" / "learn" / "[courseId]" / "page.tsx").read_text(encoding="utf-8")
        lesson_source = (frontend_root / "src" / "app" / "[locale]" / "learn" / "[courseId]" / "[lessonId]" / "page.tsx").read_text(encoding="utf-8")
        challenge_source = (frontend_root / "src" / "app" / "[locale]" / "wargame" / "[id]" / "page.tsx").read_text(encoding="utf-8")
        self.assertIn("view=map&focus_type=course&focus_id=${course.id}", course_source)
        self.assertIn("view=map&focus_type=lesson&focus_id=${lesson.id}", lesson_source)
        self.assertIn("view=map&focus_type=challenge&focus_id=${challenge.id}", challenge_source)
        self.assertIn('t("viewMap")', course_source)
        self.assertIn('t("viewMap")', lesson_source)
        self.assertIn('t("viewMap")', challenge_source)


if __name__ == "__main__":
    unittest.main()
