"""파일 해석과 비파괴 계획의 순수 테스트. 실제 PostgreSQL 검증은 별도다."""

from __future__ import annotations

from copy import deepcopy
import ast
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from app.services.challenge_imports import (
    ENVIRONMENT_FIELDS, NEW_DEFAULTS, SCALAR_FIELDS,
    export_challenge_batch, export_challenge_record, import_challenge_batch, merge_child_slots, plan_import, prepare_import,
    validate_import_edges, validate_target_policy,
)


def challenge(identifier=10, name="Existing", **values):
    data = {name: None for name in SCALAR_FIELDS}
    data.update(deepcopy(NEW_DEFAULTS))
    data.update(id=identifier, name=name, name_i18n={}, description_i18n={}, solves=7,
                environment_revision=3, environment_verified_revision=3,
                environment_verified_image_id="sha256:verified", author_id="kept-author",
                hints=[], flags=[], dynamic_flag_policy=None)
    data.update(values)
    return SimpleNamespace(**data)


def make_plan(raw_records, existing=(), hints=(), flags=(), policies=(), version=2, mode="overwrite"):
    mode, records = prepare_import({"version": version, "mode": mode, "challenges": raw_records})
    targets, mapping, skipped, flags_by_parent = plan_import(records, mode, list(existing), list(hints), list(flags), list(policies))
    return records, targets, mapping, skipped, flags_by_parent


class ChallengeImportPreservationTests(unittest.TestCase):
    def test_preserving_path_is_separate_from_legacy_and_both_bind_one_handler(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        tree = ast.parse((SERVICE_ROOT / "app/api/challenges.py").read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "import_challenges")
        paths = [node.args[0].value for node in handler.decorator_list if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "post"]
        self.assertEqual(set(paths), {"/challenges/import", "/challenges/import/v2"})
        executions = []

        async def endpoint():
            executions.append("called")
            return {"created": 0, "updated": 0, "skipped": 0}

        legacy = FastAPI()
        legacy.post("/challenges/import")(endpoint)
        with TestClient(legacy) as client:
            for version in (1, 2):
                self.assertEqual(client.post("/challenges/import/v2", json={"version": version, "challenges": []}).status_code, 404)
        self.assertEqual(executions, [])
        current = FastAPI()
        for path in paths:
            current.post(path, status_code=201)(endpoint)
        self.assertTrue(all(route.endpoint is endpoint for route in current.routes if route.path in paths))
        with TestClient(current) as client:
            for path in paths:
                self.assertEqual(client.post(path, json={"challenges": []}).status_code, 201)
        self.assertEqual(len(executions), 2)

    def error(self, code, callback, status=422):
        with self.assertRaises(HTTPException) as caught:
            callback()
        self.assertEqual(caught.exception.status_code, status)
        self.assertEqual(caught.exception.detail["code"], code)

    def test_unknown_version_and_bool_are_rejected(self):
        for version in (True, False, 0, 3, "2", None):
            with self.subTest(version=version):
                self.error("unsupported_import_version", lambda: prepare_import({"version": version, "challenges": []}))

    def test_invalid_shape_and_blank_records_are_not_silently_skipped(self):
        self.error("invalid_challenges", lambda: prepare_import({"challenges": {}}))
        for record in (None, [], {}, {"name": "  "}, {"name": 42}):
            with self.subTest(record=record):
                self.error("invalid_challenge", lambda: prepare_import({"challenges": [record]}))

    def test_invalid_mode(self):
        self.error("invalid_import_mode", lambda: prepare_import({"challenges": [], "mode": "replace"}), 400)

    def test_v2_requires_source_id(self):
        self.error("missing_source_id", lambda: prepare_import({"version": 2, "challenges": [{"name": "A"}]}))

    def test_ids_are_positive_non_bool_integers(self):
        for identifier in (True, False, "1", 0, -1, None, 1.0):
            with self.subTest(identifier=identifier):
                self.error("invalid_source_id", lambda: prepare_import({"version": 2, "challenges": [{"id": identifier, "name": "A"}]}))

    def test_duplicate_source_id_and_name_rejected(self):
        self.error("duplicate_source_id", lambda: make_plan([{"id": 1, "name": "A"}, {"id": 1, "name": "B"}]))
        self.error("duplicate_challenge_name", lambda: make_plan([{"id": 1, "name": "A"}, {"id": 2, "name": "A"}]))

    def test_v1_without_references_remains_supported(self):
        _, targets, mapping, _, _ = make_plan([{"name": "Legacy", "flag_value": "synthetic-answer"}], version=1)
        self.assertEqual(targets[0].fields["flag_value"], "synthetic-answer")
        self.assertEqual(mapping, {})

    def test_legacy_reference_never_guesses_current_db_id(self):
        self.error("unresolved_prerequisite", lambda: make_plan([{"name": "A", "prerequisite_ids": [10]}], [challenge()], version=1))

    def test_v1_source_ids_can_prove_references(self):
        records, targets, mapping, _, _ = make_plan([
            {"id": 90, "name": "A", "prerequisite_ids": [91]},
            {"id": 91, "name": "B", "is_published": True},
        ], version=1)
        self.assertEqual(validate_import_edges(records, targets, mapping, [], set(), set()), {(-1, -2)})

    def test_null_non_nullable_fields_rejected(self):
        self.error("null_challenge_field", lambda: make_plan([{"id": 1, "name": "A", "value": None}]))

    def test_scalar_fields_are_schema_validated(self):
        for values in ({"category": "bad"}, {"value": -1}, {"xp": 5001}, {"container_port": 70000}, {"flag_type": "bad"}):
            with self.subTest(values=values):
                self.error("invalid_challenge_fields", lambda: make_plan([{"id": 1, "name": "A", **values}]))

    def test_html_and_translated_html_are_canonicalized(self):
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "A", "description": "Plain body", "description_i18n": {"en": "Translated body"}}])
        self.assertEqual(targets[0].fields["description"], "<p>Plain body</p>")
        self.assertEqual(targets[0].fields["description_i18n"], {"en": "<p>Translated body</p>"})

    def test_late_invalid_record_rejects_batch_before_any_mutation(self):
        existing = challenge()
        original = deepcopy(vars(existing))
        self.error("invalid_challenge_fields", lambda: make_plan([
            {"id": 1, "name": "Existing", "description": "new"},
            {"id": 2, "name": "Bad", "category": "unknown"},
        ], [existing]))
        self.assertEqual(vars(existing), original)

    def test_hints_flags_slots_and_ids_reject_ambiguity(self):
        for kind, child in (("hints", {"content": "A"}), ("flags", {"value": "synthetic-answer"})):
            with self.subTest(kind=kind):
                self.error("ambiguous_child_slot", lambda: make_plan([{"id": 1, "name": "A", kind: [{**child, "order_index": 0}, {**child, "order_index": 0}]}]))
                self.error("invalid_source_id", lambda: make_plan([{"id": 1, "name": "A", kind: [{**child, "id": True}]}]))
                self.error("duplicate_child_source_id", lambda: make_plan([
                    {"id": 1, "name": "A", kind: [{**child, "id": 44}]},
                    {"id": 2, "name": "B", kind: [{**child, "id": 44}]},
                ]))

    def test_missing_child_slots_follow_array_order(self):
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "A", "hints": [{"content": "First"}, {"content": "Second"}]}])
        self.assertEqual([item[1]["order_index"] for item in targets[0].hints], [0, 1])

    def test_child_match_uses_parent_slot_not_foreign_source_pk(self):
        own = SimpleNamespace(id=50, challenge_id=10, order_index=0, content="<p>Old</p>", cost=3)
        other = SimpleNamespace(id=999, challenge_id=20, order_index=0, content="<p>Other</p>", cost=3)
        _, targets, _, _, _ = make_plan([{"id": 88, "name": "Existing", "hints": [{"id": 999, "content": "New", "order_index": 0}]}], [challenge()], [own, other])
        child, values = targets[0].hints[0]
        self.assertIs(child, own)
        self.assertEqual(child.id, 50)
        self.assertNotIn("id", values)
        self.assertNotIn("source_id", values)
        self.assertEqual(other.content, "<p>Other</p>")

    def test_missing_or_empty_child_lists_never_plan_deletions(self):
        own = SimpleNamespace(id=50, challenge_id=10, order_index=0)
        for children in (None, ()):
            self.assertEqual(merge_child_slots([own], children, index=0), [])

    def test_existing_duplicate_slot_requires_editor(self):
        own = [SimpleNamespace(id=i, order_index=0) for i in (1, 2)]
        self.error("ambiguous_existing_child_slot", lambda: merge_child_slots(own, ({"order_index": 0, "source_id": 5, "content": "new"},), index=0), 409)

    def test_omitted_scalars_and_history_are_not_overwrite_fields(self):
        old = challenge()
        _, targets, _, _, _ = make_plan([{"id": 999, "name": "Existing", "description": "New", "solves": 0, "author_id": "attacker", "created_at": "bad"}], [old])
        self.assertEqual(set(targets[0].fields), {"name", "description"})
        self.assertEqual(old.solves, 7)
        self.assertEqual(old.author_id, "kept-author")
        self.assertEqual(targets[0].key, 10)

    def test_ambiguous_existing_name_is_conflict(self):
        self.error("ambiguous_existing_name", lambda: make_plan([{"id": 1, "name": "Existing"}], [challenge(), challenge(20)]), 409)

    def test_skip_existing_maps_reference_but_plans_no_mutation(self):
        old = challenge(is_published=True)
        records, targets, mapping, skipped, _ = make_plan([
            {"id": 999, "name": "Existing", "description": "Ignored", "prerequisite_ids": [888]},
            {"id": 888, "name": "New", "is_published": True, "prerequisite_ids": [999]},
        ], [old], mode="skip")
        self.assertEqual(skipped, 1)
        self.assertEqual([target.record.index for target in targets], [1])
        self.assertEqual(mapping[999], 10)
        self.assertEqual(validate_import_edges(records, targets, mapping, [old], set(), set()), {(-2, 10)})

    def test_new_database_local_id_collision_does_not_choose_target(self):
        collision = challenge(identifier=88, name="Unrelated", is_published=True)
        records, targets, mapping, _, _ = make_plan([
            {"id": 88, "name": "NewA", "is_published": True},
            {"id": 10, "name": "NewB", "prerequisite_ids": [88]},
        ], [collision])
        self.assertEqual(mapping[88], -1)
        self.assertEqual(validate_import_edges(records, targets, mapping, [collision], set(), set()), {(-2, -1)})

    def test_existing_prerequisites_preserved_and_new_are_union_only(self):
        a, b, c = challenge(10, "A"), challenge(20, "B", is_published=True), challenge(30, "C", is_published=True)
        records, targets, mapping, _, _ = make_plan([
            {"id": 1, "name": "A", "prerequisite_ids": [3]}, {"id": 2, "name": "B"}, {"id": 3, "name": "C"},
        ], [a, b, c])
        self.assertEqual(validate_import_edges(records, targets, mapping, [a, b, c], {(10, 20)}, set()), {(10, 30)})

    def test_no_id_legacy_overwrite_resolves_its_existing_source(self):
        old = challenge(10, "A")
        target = challenge(20, "B", is_published=True)
        records, targets, mapping, _, _ = make_plan([
            {"name": "A", "prerequisite_ids": [99]}, {"id": 99, "name": "B"},
        ], [old, target], version=1)
        self.assertEqual(validate_import_edges(records, targets, mapping, [old, target], set(), set()), {(10, 20)})

    def test_self_and_batch_cycle_are_rejected(self):
        for rows, code in (([{"id": 1, "name": "A", "is_published": True, "prerequisite_ids": [1]}], "self_prerequisite"),
                           ([{"id": 1, "name": "A", "is_published": True, "prerequisite_ids": [2]}, {"id": 2, "name": "B", "is_published": True, "prerequisite_ids": [1]}], "cyclic_prerequisite")):
            with self.subTest(code=code):
                records, targets, mapping, _, _ = make_plan(rows)
                self.error(code, lambda: validate_import_edges(records, targets, mapping, [], set(), set()))

    def test_existing_graph_cycle_created_by_new_edge_is_rejected(self):
        a, b, c = [challenge(i, name, is_published=True) for i, name in ((10, "A"), (20, "B"), (30, "C"))]
        records, targets, mapping, _, _ = make_plan([{"id": 1, "name": "A", "prerequisite_ids": [3]}, {"id": 3, "name": "C"}], [a, b, c])
        self.error("cyclic_prerequisite", lambda: validate_import_edges(records, targets, mapping, [a, b, c], {(30, 20), (20, 10)}, set()))

    def test_unrelated_legacy_cycle_and_existing_private_edge_are_preserved(self):
        a, b = challenge(10, "A"), challenge(20, "B")
        records, targets, mapping, _, _ = make_plan([{"id": 1, "name": "A", "prerequisite_ids": [2]}, {"id": 2, "name": "B"}], [a, b])
        self.assertEqual(validate_import_edges(records, targets, mapping, [a, b], {(10, 20), (30, 40), (40, 30)}, set()), set())

    def test_new_docker_and_competition_prerequisites_are_not_implicitly_allowed(self):
        for values in ({"env_type": "image", "is_published": True}, {"challenge_type": "competition", "is_published": True}, {"is_published": False}):
            records, targets, mapping, _, _ = make_plan([{"id": 1, "name": "A", "prerequisite_ids": [2]}, {"id": 2, "name": "B", **values}])
            self.error("unavailable_prerequisite", lambda: validate_import_edges(records, targets, mapping, [], set(), set()))

    def test_type_transition_cannot_break_retained_learning_or_incoming_links(self):
        old = challenge()
        records, targets, mapping, _, _ = make_plan([{"id": 1, "name": "Existing", "challenge_type": "competition"}], [old])
        self.error("competition_prerequisite", lambda: validate_import_edges(records, targets, mapping, [old], set(), {10}))
        self.error("referenced_challenge_type", lambda: validate_import_edges(records, targets, mapping, [old], {(20, 10)}, set()))

    def test_new_docker_is_private_and_port_is_detected(self):
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "A", "env_type": "dockerfile", "dockerfile_content": "FROM scratch\nEXPOSE 5000", "is_published": True}])
        self.assertFalse(targets[0].fields["is_published"])
        self.assertEqual(targets[0].fields["container_port"], 5000)

    def test_same_verified_environment_preserves_publication_and_revision(self):
        old = challenge(env_type="image", docker_image="fixed-image", is_published=True)
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "Existing", "env_type": "image", "docker_image": "fixed-image", "is_published": True}], [old])
        validate_target_policy(targets[0], [], lambda _: self.fail("not dynamic"))
        self.assertFalse(targets[0].invalidate_environment)
        self.assertEqual(old.environment_revision, 3)

    def test_published_environment_change_is_conflict_even_if_import_asks_private(self):
        old = challenge(env_type="image", docker_image="old", is_published=True)
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "Existing", "docker_image": "new", "is_published": False}], [old])
        self.error("environment_unpublish_required", lambda: validate_target_policy(targets[0], [], lambda _: None), 409)

    def test_draft_environment_change_invalidates_but_cannot_publish_in_same_batch(self):
        old = challenge(env_type="image", docker_image="old")
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "Existing", "docker_image": "new"}], [old])
        validate_target_policy(targets[0], [], lambda _: None)
        self.assertTrue(targets[0].invalidate_environment)
        targets[0].fields["is_published"] = True
        self.error("environment_verification_required", lambda: validate_target_policy(targets[0], [], lambda _: None))

    def test_static_dynamic_boundary_requires_explicit_editor(self):
        old = challenge()
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "Existing", "flag_type": "dynamic_instance", "env_type": "image", "dynamic_flag": {}}], [old])
        self.error("flag_mode_requires_editor", lambda: validate_target_policy(targets[0], [], lambda _: None), 409)

    def test_dynamic_policy_has_no_credentials_and_rejects_static_material(self):
        self.error("invalid_dynamic_policy", lambda: make_plan([{"id": 1, "name": "D", "dynamic_flag": {"hmac_key": "never-return"}}]))
        _, targets, _, _, _ = make_plan([{"id": 1, "name": "D", "flag_type": "dynamic_instance", "env_type": "image", "dynamic_flag": {}, "flag_value": "not-allowed"}])
        self.error("mixed_flag_modes", lambda: validate_target_policy(targets[0], [], lambda _: None))

    def test_static_export_contains_answer_and_stable_ids_but_not_history(self):
        old = challenge(flag_value="synthetic-answer", hints=[SimpleNamespace(id=5, content="<p>Hint</p>", cost=3, order_index=0)],
                        flags=[SimpleNamespace(id=6, type="static", value="synthetic-answer", case_sensitive=True, order_index=0)])
        result = export_challenge_record(old, [20])
        self.assertEqual((result["id"], result["hints"][0]["id"], result["flags"][0]["id"]), (10, 5, 6))
        self.assertEqual(result["flag_value"], "synthetic-answer")
        self.assertEqual(result["prerequisite_ids"], [20])
        self.assertNotIn("solves", result)
        self.assertNotIn("author_id", result)

    def test_dynamic_export_excludes_issuances_keys_and_static_material(self):
        policy = SimpleNamespace(enabled=True, delivery="file", runtime_path="/flag.txt", ttl_seconds=600,
                                 hmac_key_version="v1", credential="must-not-leak", policy_revision=8)
        old = challenge(flag_type="dynamic_instance", flag_value="legacy-do-not-export", dynamic_flag_policy=policy,
                        flags=[SimpleNamespace(id=6, type="static", value="legacy-do-not-export", case_sensitive=True, order_index=0)])
        old.dynamic_flag_issuances = ["must-not-export"]
        result = export_challenge_record(old, [])
        self.assertEqual(result["flag_value"], "")
        self.assertEqual(result["flags"], [])
        self.assertEqual(set(result["dynamic_flag"]), {"enabled", "delivery", "runtime_path", "ttl_seconds", "hmac_key_version"})
        self.assertNotIn("dynamic_flag_issuances", result)


def immediate(coroutine):
    """즉시 완료하는 AsyncMock만 구동한다. event loop나 socket을 만들지 않는다."""
    try:
        yielded = coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    coroutine.close()
    raise AssertionError(f"PURE_COROUTINE_SUSPENDED:{type(yielded).__name__}")


def rows(values):
    return SimpleNamespace(all=lambda: values)


class ChallengeImportTransactionContractTests(unittest.TestCase):
    def db(self):
        return SimpleNamespace(
            scalar=AsyncMock(return_value=True),
            scalars=AsyncMock(side_effect=[rows([challenge()]), rows([]), rows([]), rows([]), rows([])]),
            execute=AsyncMock(return_value=rows([])), flush=AsyncMock(), commit=AsyncMock(),
            rollback=AsyncMock(), add=Mock(),
        )

    def call(self, db, mode="overwrite"):
        return immediate(import_challenge_batch(db, {"version": 2, "mode": mode, "challenges": [{"id": 1, "name": "Existing", "description": "Changed"}]},
            actor_id=uuid4(), validate_dynamic_policy=Mock(), invalidate_environment=Mock(), revoke_active_issuances=AsyncMock()))

    def test_success_commits_once_after_projection_and_never_deletes(self):
        db = self.db()
        with patch("app.services.challenge_imports.project_rule_tag_mappings", new=AsyncMock()) as projection:
            self.assertEqual(self.call(db), {"created": 0, "updated": 1, "skipped": 0})
            projection.assert_awaited_once()
        db.commit.assert_awaited_once()
        db.rollback.assert_not_awaited()
        self.assertFalse(hasattr(db, "delete"))

    def test_skip_never_projects_and_commits_no_planned_writes(self):
        db = self.db()
        with patch("app.services.challenge_imports.project_rule_tag_mappings", new=AsyncMock()) as projection:
            self.assertEqual(self.call(db, "skip"), {"created": 0, "updated": 0, "skipped": 1})
            projection.assert_not_awaited()
        db.add.assert_not_called()

    def test_projection_failure_and_cancellation_roll_back_without_commit(self):
        for failure in (RuntimeError("injected"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                db = self.db()
                with patch("app.services.challenge_imports.project_rule_tag_mappings", new=AsyncMock(side_effect=failure)):
                    with self.assertRaises(type(failure)):
                        self.call(db)
                db.rollback.assert_awaited_once()
                db.commit.assert_not_awaited()

    def test_commit_connection_loss_is_unknown_even_when_rollback_works(self):
        db = self.db()
        db.commit.side_effect = DBAPIError("commit", {}, RuntimeError("connection lost"), connection_invalidated=True)
        with patch("app.services.challenge_imports.project_rule_tag_mappings", new=AsyncMock()):
            with self.assertRaises(HTTPException) as caught:
                self.call(db)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "import_outcome_unknown")
        db.rollback.assert_awaited_once()

    def test_rollback_failure_does_not_replace_unknown_result_with_false_cancel_claim(self):
        db = self.db()
        db.scalar.side_effect = DBAPIError("lock", {}, RuntimeError("connection lost"), connection_invalidated=True)
        db.rollback.side_effect = RuntimeError("rollback connection lost")
        with self.assertRaises(HTTPException) as caught:
            self.call(db)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail["code"], "import_outcome_unknown")
        db.commit.assert_not_awaited()

    def test_empty_import_never_acquires_db_lock(self):
        db = self.db()
        self.assertEqual(immediate(import_challenge_batch(db, {"challenges": []}, actor_id=uuid4(),
            validate_dynamic_policy=Mock(), invalidate_environment=Mock(), revoke_active_issuances=AsyncMock())),
            {"created": 0, "updated": 0, "skipped": 0})
        db.scalar.assert_not_awaited()

    def test_export_uses_readonly_repeatable_snapshot_before_any_select(self):
        db = SimpleNamespace(execute=AsyncMock(side_effect=[rows([]), rows([])]), scalars=AsyncMock(return_value=rows([])))
        self.assertEqual(immediate(export_challenge_batch(db)), {"version": 2, "challenges": []})
        self.assertEqual(str(db.execute.await_args_list[0].args[0]), "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")

    def test_postgres_test_scope_requires_new_exact_uuid_database_and_role(self):
        from test_challenge_import_postgres import validate_work_database_url
        name = "ctfi_l_" + "a" * 32
        url = f"postgresql+asyncpg://{name}:synthetic@127.0.0.1:15433/{name}"
        self.assertEqual(validate_work_database_url(url).database, name)
        invalid = [
            "postgresql+asyncpg://ctf_acceptance_learn:synthetic@127.0.0.1:15433/ctf_acceptance_learn",
            url.replace("@127.0.0.1", "@localhost"), url.replace(":15433/", ":5432/"),
            url.replace(f"//{name}:", "//other:"), url.replace("a" * 32, "A" * 32),
            *(url + query for query in (
                "?host=192.0.2.1", "?port=5432", "?database=other", "?user=other",
                "?host=127.0.0.1&host=192.0.2.1", "?ssl=disable",
            )),
        ]
        for value in invalid:
            with self.subTest(scope="invalid"):
                with self.assertRaises(ValueError):
                    validate_work_database_url(value)

    def test_failed_schema_create_never_registers_or_drops_an_unowned_schema(self):
        from test_challenge_import_postgres import ChallengeImportPostgresTests
        case = ChallengeImportPostgresTests()
        connection = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("schema exists")))
        transaction = AsyncMock()
        transaction.__aenter__.return_value = connection
        case.admin_engine = SimpleNamespace(begin=Mock(return_value=transaction), dispose=AsyncMock())
        case.schemas, case.engines = [], []
        with self.assertRaisesRegex(RuntimeError, "schema exists"):
            immediate(case.new_schema())
        self.assertEqual(case.schemas, [])
        self.assertEqual(case.engines, [])
        immediate(case.cleanup_owned_schemas())
        connection.execute.assert_awaited_once()
        case.admin_engine.dispose.assert_awaited_once()

    def test_setup_registers_cleanup_before_connection_failure(self):
        from test_challenge_import_postgres import ChallengeImportPostgresTests
        case = ChallengeImportPostgresTests()
        context = AsyncMock()
        context.__aenter__.side_effect = RuntimeError("connect unavailable")
        engine = SimpleNamespace(connect=Mock(return_value=context), dispose=AsyncMock())
        name = "ctfi_l_" + "a" * 32
        with patch("test_challenge_import_postgres.DATABASE_URL", f"postgresql+asyncpg://{name}:synthetic@127.0.0.1:15433/{name}"), \
                patch("test_challenge_import_postgres.create_async_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "connect unavailable"):
                immediate(case.asyncSetUp())
        self.assertEqual(len(case._cleanups), 1)
        callback, args, kwargs = case._cleanups.pop()
        immediate(callback(*args, **kwargs))
        engine.dispose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
