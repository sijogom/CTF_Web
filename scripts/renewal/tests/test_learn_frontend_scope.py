"""031-to-032 Learn/frontend scope; all remote effects are in-memory fakes."""
from __future__ import annotations

import ast
import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activate_release as activate
import backup_images
import backup_release as backup
import ops
import quiesce_release as quiesce
import release_preflight as preflight
import stage_build_release as stage
import verify_release as verify
from test_auth_learn_community_frontend_scope import alcf_fixture, HEADS
from test_verify_release import category_projection, digest, receipts, row


WORKERS = set(verify.LEGACY_PROCESS_WORKERS)
TARGETS = {"learn-service", "frontend", *WORKERS}


def lf_fixture():
    source, old_plan, baseline, snapshot, triggers = alcf_fixture()
    next(item for item in snapshot["containers"] if item["service"] == "personal-notification-runner")["command"] = list(activate.PERSONAL_WORKER_COMMAND)
    baseline["expected_heads"] = dict(HEADS)
    baseline["managed_files"] = copy.deepcopy(source["files"])
    snapshot["code_files"] = verify.file_rows(baseline["managed_files"] + baseline["unmanaged_files"])
    inspection = copy.deepcopy(old_plan["inspection"])
    inspection["code_files"] = copy.deepcopy(snapshot["code_files"])
    for name, database in inspection["databases"].items():
        database["head"] = [HEADS[name]]
    source["files"] = verify.file_rows(source["files"] + [row("services/learn/migrations/versions/20261020_032_knowledge_tag_deletion_archive.py")])
    for path in ("services/learn/Dockerfile", "frontend/Dockerfile"):
        next(item for item in source["files"] if item["path"] == path)["sha256"] = digest("lf-new-" + path)
    graph = copy.deepcopy(old_plan["migrations"])
    graph["learn"]["head"] = "20261020_032"
    graph["learn"]["revisions"] = sorted(set(graph["learn"]["revisions"]) | {"20261020_032"})
    hashes = [{name: digest("lf-prior-" + name) for name in ("activation", "build", "verify")}]
    contract = preflight.make_lf_contract(source["files"], inspection, graph, baseline, hashes)
    plan = preflight.make_plan(source, inspection, graph, lf_contract=contract)
    return source, plan, baseline, snapshot, triggers


def lf_receipts():
    source, plan, baseline, snapshot, triggers = lf_fixture()
    binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
    build, activation = receipts({**baseline, "binding": binding}, snapshot)
    fields = {key: plan[key] for key in preflight.LF_FIELDS}
    build.update(fields)
    build["remote"].update(fields, frontend_only=False, learn_only=False,
                           retained_baseline=verify.baseline_summary(baseline, ("learn", "frontend")))
    build["remote"]["completed_images"] = [item for item in build["remote"]["completed_images"] if item["service"] in {"learn", "frontend"}]
    for item in build["remote"]["completed_images"]:
        item["image_id"] = "sha256:" + digest("lf-new-" + item["service"])
    built = verify.build_contract(build, binding, baseline=baseline, learn_frontend=True)
    managed = activate.selected_rows(source, learn_frontend=True)
    retained = [item for item in baseline["managed_files"] if item["path"].startswith(("services/auth/", "services/community/"))]
    activation.update(fields, frontend_only=False, learn_only=False, baseline_receipts=plan["lf_contract"]["baseline_receipts"],
        managed_files=managed, unmanaged_files=verify.file_rows(retained + baseline["unmanaged_files"]),
        executed_migrations=["learn"], schema={"heads": {name: [head] for name, head in plan["lf_contract"]["target_heads"].items()}},
        untargeted_runtime_before=preflight.code_runtime(snapshot, TARGETS),
        untargeted_runtime_after=preflight.code_runtime(snapshot, TARGETS))
    checked = verify.activation_contract(activation, binding, managed, built, baseline=baseline, learn_frontend=True)
    return source, plan, baseline, snapshot, triggers, build, activation, checked


def mock_activation(*, fail_migration=False, fail_up=False, schema_false=False, drift=False):
    source, plan, baseline, snapshot, triggers = lf_fixture()
    binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
    images = {name: "sha256:" + digest("lf-new-" + name) if name in {"learn", "frontend"} else image
              for name, image in baseline["images"].items()}
    current = copy.deepcopy(snapshot["containers"])
    for item in current:
        if item["service"] in TARGETS - {"frontend"}:
            item.update(running=False, pid=0, status="exited")
    original = copy.deepcopy(current)
    code = copy.deepcopy(snapshot["code_files"])
    heads = dict(HEADS)
    request = {**{key: plan[key] for key in preflight.LF_FIELDS}, "binding": binding,
        "frontend_only": False, "learn_only": False, "managed_files": activate.selected_rows(source, learn_frontend=True), "modes": {},
        "images": images, "image_bindings": {name: binding if name in {"learn", "frontend"} else baseline["image_bindings"][name] for name in images},
        "protected_files": baseline["protected_files"], "current_container_baseline": copy.deepcopy(current),
        "backend_baseline": baseline, "worker_templates": None, "expected_heads": plan["lf_contract"]["target_heads"],
        "plan_sha256": "1" * 64, "backup_receipt_sha256": "2" * 64, "quiescence_sha256": "3" * 64,
        "baseline_receipts": plan["lf_contract"]["baseline_receipts"],
        "migration_wrapper_sha256": activate.sha(activate.MIGRATION_SITE_CUSTOMIZE.encode())}
    namespace = {"REQUEST": request}
    exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<lf-activation-fake>", "exec"), namespace)
    commands, written = [], []
    def snap():
        return {"containers": copy.deepcopy(current), "code_files": copy.deepcopy(code), "protected_files": baseline["protected_files"],
                "source_labels": {item["container_id"]: item.get("source_labels", {}) for item in current}}
    def command(argv, log, timeout=600):
        commands.append(argv)
        if "run" in argv:
            assert "learn-migrate" in argv and "--rm" in argv and "--no-deps" in argv
            if fail_migration:
                raise namespace["ActivateError"]("ACTIVATION_COMMAND_FAILED")
            heads["learn"] = "20261020_032"
        elif "up" in argv:
            name = argv[-1]
            assert name in TARGETS
            item = next(item for item in current if item["service"] == name)
            item.update(container_id=digest("lf-new-" + name), image_id=images[verify.SERVICE_IMAGES[name]],
                        running=True, pid=888, status="running", source_labels=activate.source_labels(binding))
            if fail_up:
                raise TimeoutError("synthetic ambiguous up")
            if drift:
                next(item for item in current if item["service"] == "community-service")["restart_count"] += 1
        else:
            raise AssertionError("UNEXPECTED_MUTATION")
    def run(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ["docker", "stop"]:
            assert "--time" in argv and "-1" in argv
            for item in current:
                if item["container_id"] in argv:
                    assert item["service"] in TARGETS - {"frontend"}
                    item.update(running=False, pid=0, status="exited")
            return b""
        if argv[-3:] == ["config", "--format", "json"]:
            return json.dumps({"services": {name: {"image": images[verify.SERVICE_IMAGES[name]]} for name in TARGETS}}).encode()
        raise AssertionError("UNMOCKED_COMMAND")
    def sql(database, query):
        if "alembic_version" in query:
            return [heads[next(name for name, value in preflight.DATABASES.items() if value == database)]]
        if "pg_stat_activity" in query:
            assert database == "learn_db"
            return 0
        if query == verify.LF_TAG_SCHEMA_SQL:
            return {**verify.LF_TAG_SCHEMA_EXPECTED, **({"archive_columns": False} if schema_false else {})}
        if "knowledge_graph_revision_write" in query:
            return triggers
        if "json_agg(table_name)" in query:
            return list(activate.SCHEMA_TABLES[next(name for name, value in preflight.DATABASES.items() if value == database)])
        if "json_agg(table_name||" in query:
            return list(activate.SCHEMA_COLUMNS[next(name for name, value in preflight.DATABASES.items() if value == database)])
        raise AssertionError("UNMOCKED_SQL")
    def copy_source(*args):
        selected = {item["path"] for item in request["managed_files"]}
        code[:] = verify.file_rows(request["managed_files"] + [item for item in snapshot["code_files"] if item["path"] not in selected])
    model = {"services": {name: {} for name in (*verify.SERVICE_IMAGES, *verify.MIGRATION_IMAGES)}}
    with tempfile.TemporaryDirectory(dir=ops.TASK / "runtime" / "temp") as directory:
        root = Path(directory)
        (root / binding["release_nonce"]).mkdir()
        namespace.update(ROOT=root / "live", RELEASE_ROOT=str(root), safe_snapshot=snap, compose=lambda: model,
            check_images=lambda: None, containers=lambda: copy.deepcopy(current), sql=sql,
            database_rows=lambda _: copy.deepcopy(plan["inspection"]["databases"]),
            private_command=command, private_json=lambda path, value: written.append((str(path), copy.deepcopy(value))),
            run=run, copy_managed=copy_source, wait_service=lambda *args: None,
            probe_personal_worker=lambda _: {"health_status": 200, "ready_status": 200, "baseline_complete": True},
            wait_personal_dependency_recovery=lambda *args: (snap(), {}),
            category_compatibility=lambda: {"custom_category_count": 0, "custom_event_count": 0,
                "legacy_enum_image_rollback_forbidden": False, "automatic_backend_rollback_performed": False})
        with redirect_stdout(io.StringIO()) as output:
            namespace["activation_main"]()
        result = json.loads(output.getvalue())
        if "runtime_overlay" in result:
            result["runtime_overlay"] = Path(result["runtime_overlay"]).as_posix()
        with patch.object(activate, "RELEASE_ROOT", root.as_posix()):
            activate.validate_remote_receipt(result, request)
    return result, commands, current, original, written


class LearnFrontendScopeTests(unittest.TestCase):
    def setUp(self):
        self.external = [patch.object(subprocess, "run", side_effect=AssertionError("EXTERNAL_PROCESS_FORBIDDEN")),
                         patch.object(socket, "socket", side_effect=AssertionError("NETWORK_FORBIDDEN")),
                         patch.object(ops, "ssh", side_effect=AssertionError("SSH_FORBIDDEN")),
                         patch.object(ops, "remote_python", side_effect=AssertionError("REMOTE_FORBIDDEN"))]
        for guard in self.external:
            guard.start()
            self.addCleanup(guard.stop)

    def test_new_scope_is_explicit_and_mixed_flags_are_rejected(self):
        source, plan, *_ = lf_fixture()
        preflight.validate_plan(plan, source["fingerprint"])
        self.assertTrue(preflight.lf_phase(plan))
        self.assertEqual(preflight.application_phase_fields(plan), preflight.LF_FIELDS)
        for change in ({"learn_frontend": False}, {"learn_frontend": 1}, {"learn_only": True},
                       {"frontend_only": True}, {"auth_learn_frontend": True}, {"auth_learn_community_frontend": True},
                       {"release_phase": preflight.ALF_PHASE}):
            with self.subTest(change=change), self.assertRaises(preflight.ReleaseError):
                preflight.validate_plan({**plan, **change}, source["fingerprint"])
        for flag in ("frontend_only", "learn_only", "auth_learn_frontend", "auth_learn_community_frontend"):
            with self.subTest(flag=flag), self.assertRaises(verify.VerifyError):
                verify.release_families(learn_frontend=True, **{flag: True})

    def test_exact_heads_and_retained_schemas_are_mandatory(self):
        _, plan, *_ = lf_fixture()
        for name in ("auth", "learn", "community"):
            bad = copy.deepcopy(plan["lf_contract"])
            bad["target_heads"][name] = "unexpected"
            with self.subTest(name=name), self.assertRaises(preflight.ReleaseError):
                preflight.validate_lf_contract(bad)
        for name in ("auth", "community"):
            bad = copy.deepcopy(plan["inspection"])
            bad["databases"][name]["schema_sha256"] = "0" * 64
            with self.subTest(name=name), self.assertRaises(preflight.ReleaseError):
                preflight.validate_lf_contract(plan["lf_contract"], bad, plan["migrations"])

    def test_historical_migrations_and_retained_sources_cannot_change(self):
        source, plan, baseline, *_ = lf_fixture()
        for prefix in ("services/auth/", "services/community/", "services/learn/migrations/versions/20261018_031"):
            bad = copy.deepcopy(source["files"])
            next(item for item in bad if item["path"].startswith(prefix))["sha256"] = "0" * 64
            with self.subTest(prefix=prefix), self.assertRaises(preflight.ReleaseError):
                preflight.make_lf_contract(bad, plan["inspection"], plan["migrations"], baseline, plan["lf_contract"]["baseline_receipts"])

    def test_201_retained_rows_allow_order_only_but_reject_every_content_or_multiplicity_change(self):
        source, plan, baseline, _, _ = lf_fixture()
        prefixes = ("services/auth/", "services/community/")
        retained = [item for item in baseline["managed_files"] if item["path"].startswith(prefixes)]
        retained += [row(f"services/auth/app/retained_{index:03}.py") for index in range(201 - len(retained))]
        retained = verify.file_rows(retained)
        baseline = copy.deepcopy(baseline)
        baseline["managed_files"] = verify.file_rows([item for item in baseline["managed_files"] if not item["path"].startswith(prefixes)] + retained)
        inspection = copy.deepcopy(plan["inspection"])
        inspection["code_files"] = verify.file_rows(baseline["managed_files"] + baseline["unmanaged_files"])
        changed = [item for item in source["files"] if not item["path"].startswith(prefixes)]
        hashes = plan["lf_contract"]["baseline_receipts"]
        self.assertEqual(len(retained), 201)
        expected = preflight.make_lf_contract(changed + retained, inspection, plan["migrations"], baseline, hashes)
        self.assertEqual(preflight.make_lf_contract(changed + list(reversed(retained)), inspection, plan["migrations"], baseline, hashes), expected)
        mutations = {}
        for key, value in (("bytes", retained[0]["bytes"] + 1), ("sha256", "0" * 64)):
            bad = copy.deepcopy(retained)
            bad[0][key] = value
            mutations[key] = bad
        mutations["key"] = [{**retained[0], "unexpected": True}, *retained[1:]]
        mutations["added"] = retained + [row("services/community/app/unbound.py")]
        mutations["removed"] = retained[1:]
        mutations["duplicate"] = retained + [copy.deepcopy(retained[0])]
        for name, bad in mutations.items():
            with self.subTest(name=name), self.assertRaises(preflight.ReleaseError):
                preflight.make_lf_contract(changed + bad, inspection, plan["migrations"], baseline, hashes)

    def test_only_two_contexts_build_in_order(self):
        self.assertEqual(stage.selected_contexts(learn_frontend=True), (("learn", "services/learn"), ("frontend", "frontend")))
        self.assertEqual(verify.release_families(learn_frontend=True), ("learn", "frontend"))
        _, plan, *_ = lf_fixture()
        with self.assertRaises(stage.StageError):
            stage.load_preflight(None, None, {}, 1, learn_frontend=True)
        self.assertEqual(plan["backup_scope"]["databases"], ["learn_db"])

    def test_stop_and_backup_never_select_auth_or_community(self):
        _, plan, *_ = lf_fixture()
        targets = quiesce.select_targets(plan)
        self.assertEqual({item["service"] for item in targets["workers"]}, WORKERS)
        self.assertEqual([item["service"] for item in targets["apis"]], ["learn-service"])
        self.assertEqual({item["service"] for item in quiesce.action_targets(plan, "stop")}, {"learn-service", "minio", *WORKERS})
        self.assertEqual([item["service"] for item in quiesce.action_targets(plan, "resume-storage")], ["minio"])
        ids, _ = backup_images.selection(plan)
        self.assertEqual(set(ids), {next(item["image_id"] for item in plan["inspection"]["containers"] if item["service"] == name)
                                    for name in ("learn-service", "frontend")})
        self.assertEqual([item["name"] for item in backup.stream_specs(plan) if item["kind"] == "database"], ["learn_db.dump"])

    def test_lf_quiescence_requires_only_learn_sessions_drained_and_preserves_others(self):
        _, plan, *_ = lf_fixture()
        frozen = copy.deepcopy(plan["inspection"])
        targets = {item["container_id"] for item in quiesce.action_targets(plan, "stop")}
        for item in frozen["containers"]:
            if item["container_id"] in targets:
                item.update(running=False, pid=0, status="exited", finished_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        frozen["databases"]["learn"]["client_session_count"] = 0
        frozen["databases"]["auth"]["client_session_count"] = 7
        frozen["databases"]["community"]["client_session_count"] = 4
        proof = {"schema_version": 1, "kind": "renewal-quiescence", "task": preflight.TASK_ID,
                 "nonce": plan["nonce"], "plan_sha256": "1" * 64, "source_fingerprint": plan["source_fingerprint"],
                 "verified_at": datetime.now(timezone.utc).isoformat(), "drain_verified": True,
                 "external_writers_absent_verified": True, "fence_maintained_until_capture_complete": True,
                 "inspection": frozen, **{key: plan[key] for key in preflight.LF_FIELDS}}
        self.assertEqual(backup.validate_quiescence(proof, plan, "1" * 64), frozen)
        bad = copy.deepcopy(proof)
        bad["inspection"]["databases"]["learn"]["client_session_count"] = 1
        with self.assertRaises(preflight.ReleaseError):
            backup.validate_quiescence(bad, plan, "1" * 64)
        bad = copy.deepcopy(proof)
        next(item for item in bad["inspection"]["containers"] if item["service"] == "auth-service")["running"] = False
        with self.assertRaises(preflight.ReleaseError):
            backup.validate_quiescence(bad, plan, "1" * 64)

    def test_steps_and_overlay_have_exact_six_targets_and_one_migration(self):
        _, plan, baseline, snapshot, *_ = lf_fixture()
        workers = activate.derive_workers(snapshot["containers"], verify.SERVICE_IMAGES, learn_frontend=True)
        self.assertEqual(set(workers), WORKERS)
        steps = activate.activation_steps(workers, learn_frontend=True, migration_services=["learn"])
        self.assertEqual([name for action, name in steps if action == "migration"], ["learn"])
        self.assertEqual({name for action, name in steps if action == "up"}, TARGETS)
        self.assertNotIn(("baseline", "auth"), steps)
        model = {"services": {name: {} for name in (*verify.SERVICE_IMAGES, *verify.MIGRATION_IMAGES)}}
        overlay = activate.make_overlay(model, baseline["images"], baseline["binding"], workers, Path("/owned/runtime"),
                                        learn_frontend=True, migration_services=["learn"])
        self.assertEqual(set(overlay["services"]), TARGETS | {"learn-migrate"})
        self.assertEqual(overlay["services"]["learn-migrate"]["restart"], "no")
        with self.assertRaises(activate.ActivateError):
            activate.activation_steps(workers + ["personal-notification-runner"], learn_frontend=True, migration_services=["learn"])

    def test_existing_stopped_migration_containers_are_preserved_not_reexecuted(self):
        _, _, baseline, snapshot, *_ = lf_fixture()
        current = copy.deepcopy(snapshot["containers"])
        for family in ("auth", "learn", "community"):
            current.append({"service": family + "-migrate", "container_id": digest("old-job-" + family),
                            "image_id": baseline["images"][family], "running": False})
        workers = activate.derive_workers(current, verify.SERVICE_IMAGES, learn_frontend=True)
        self.assertEqual(set(workers), WORKERS)
        next(item for item in current if item["service"] == "learn-migrate")["running"] = True
        with self.assertRaisesRegex(activate.ActivateError, "LF_RUNNING_MIGRATION_FORBIDDEN"):
            activate.derive_workers(current, verify.SERVICE_IMAGES, learn_frontend=True)

    def test_build_and_activation_receipts_preserve_retained_images_and_sources(self):
        source, plan, baseline, _, _, build, activation, checked = lf_receipts()
        self.assertEqual(checked["expected_heads"]["learn"], "20261020_032")
        for name in ("auth", "community"):
            self.assertEqual(checked["images"][name], baseline["images"][name])
            self.assertEqual(checked["image_bindings"][name], baseline["image_bindings"][name])
        self.assertEqual(checked["managed_files"], verify.file_rows(activate.selected_rows(source)))
        self.assertEqual(set(checked["changed_services"]), TARGETS | {"learn-migrate"})

    def test_remote_success_executes_only_expected_new_services_once(self):
        result, commands, current, original, _ = mock_activation()
        self.assertEqual(result["status"], "activated", result.get("error"))
        self.assertEqual(result["executed_migrations"], ["learn"])
        self.assertEqual([argv[-1] for argv in commands if "up" in argv], ["learn-service", *sorted(WORKERS), "frontend"])
        self.assertEqual(sum("run" in argv for argv in commands), 1)
        self.assertFalse(any("stop" in argv for argv in commands))
        self.assertEqual([item for item in current if item["service"] not in TARGETS], [item for item in original if item["service"] not in TARGETS])

    def test_failed_or_ambiguous_operations_never_replay_or_restore(self):
        for failure in ("fail_migration", "fail_up", "schema_false", "drift"):
            with self.subTest(failure=failure):
                result, commands, current, original, _ = mock_activation(**{failure: True})
                self.assertEqual(result["status"], "partial-unverified")
                self.assertFalse(result["database_restore_performed"])
                self.assertFalse(result.get("frontend_image_rolled_back", False))
                self.assertLessEqual(sum("run" in argv for argv in commands), 1)
                ups = [argv[-1] for argv in commands if "up" in argv]
                self.assertEqual(len(ups), len(set(ups)))
                self.assertTrue(all(name in TARGETS for name in ups))

    def test_immutable_generated_programs_parse_and_tag_predicate_is_json(self):
        for module in (stage, activate, verify):
            ast.parse(module.remote_program())
        self.assertTrue(verify.LF_TAG_SCHEMA_SQL.startswith("SELECT json_build_object("))
        self.assertIn("audit_fk_absent", verify.LF_TAG_SCHEMA_SQL)
        self.assertIn("tag_deleted", verify.LF_TAG_SCHEMA_SQL)

    def verification_fixture(self):
        _, plan, _, before, triggers, _, _, request = lf_receipts()
        current = copy.deepcopy(before)
        current["code_files"] = verify.file_rows(request["managed_files"] + request["unmanaged_files"])
        for item in current["containers"]:
            name = item["service"]
            if name in TARGETS:
                item.update(image_id=request["images"][verify.SERVICE_IMAGES[name]], container_id=digest("lf-checked-" + name),
                            source_labels=activate.source_labels(request["binding"]))
            if name in WORKERS:
                item["command"] = ["python", "-m", verify.LEGACY_PROCESS_WORKERS[name]]
        labels = {image: activate.source_labels(request["image_bindings"][family]) for family, image in request["images"].items()}
        labels[request["images"]["frontend"]][verify.FRONTEND_ARGS_LABEL] = request["frontend_args_sha256"]
        heads = {name: [head] for name, head in plan["lf_contract"]["target_heads"].items()}
        state = {"auth_schema_sha256": plan["lf_contract"]["auth_schema_sha256"],
                 "community_schema_sha256": plan["lf_contract"]["community_schema_sha256"], "tag_schema": copy.deepcopy(verify.LF_TAG_SCHEMA_EXPECTED)}
        return request, current, labels, heads, triggers, state

    def test_independent_verifier_passes_exact_state_without_excluding_old_migration_containers(self):
        request, current, labels, heads, triggers, state = self.verification_fixture()
        value = verify.evaluate_snapshot(request, current, current, labels, heads, triggers,
                                        category_projection(), lf_states=[state, state])
        self.assertEqual(value["status"], "passed", value["checks"])
        self.assertEqual(len(value["checks"]), 17)
        verify.validate_result(value, request["binding"], expected_heads=request["expected_heads"],
                               learn_frontend=True, lf_contract=request["lf_contract"])
        self.assertEqual(value["checks"]["lf_untargeted_runtime"]["count"], len(request["untargeted_runtime"]))

    def test_independent_verifier_rejects_schema_false_and_non_boolean(self):
        request, current, labels, heads, triggers, state = self.verification_fixture()
        for field in verify.LF_TAG_SCHEMA_EXPECTED:
            for wrong in (False, 1, None):
                bad = copy.deepcopy(state)
                bad["tag_schema"][field] = wrong
                with self.subTest(field=field, wrong=wrong):
                    value = verify.evaluate_snapshot(request, current, current, labels, heads, triggers,
                                                    category_projection(), lf_states=[state, bad])
                    self.assertEqual(value["checks"]["lf_schema_preservation"]["status"], "failed")
        for family in ("auth", "community"):
            bad = {**state, family + "_schema_sha256": "0" * 64}
            value = verify.evaluate_snapshot(request, current, current, labels, heads, triggers,
                                            category_projection(), lf_states=[bad, bad])
            self.assertEqual(value["checks"]["lf_schema_preservation"]["status"], "failed")

    def test_independent_verifier_rejects_retained_runtime_changes(self):
        request, current, labels, heads, triggers, state = self.verification_fixture()
        for service in ("auth-service", "personal-notification-runner", "community-service", "minio"):
            bad = copy.deepcopy(current)
            next(item for item in bad["containers"] if item["service"] == service)["restart_count"] += 1
            value = verify.evaluate_snapshot(request, bad, bad, labels, heads, triggers,
                                            category_projection(), lf_states=[state, state])
            with self.subTest(service=service):
                self.assertEqual(value["checks"]["lf_untargeted_runtime"]["status"], "failed")

    def test_all_seven_cli_entrypoints_expose_only_explicit_new_mode(self):
        for module in (preflight, stage, backup_images, quiesce, backup, activate, verify):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
            arguments = [node.args[0].value for node in ast.walk(main) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                         and node.args and isinstance(node.args[0], ast.Constant)]
            with self.subTest(module=module.__name__):
                self.assertEqual(arguments.count("--learn-frontend"), 1)

    def test_preflight_cli_creates_a_new_lf_bound_plan_without_external_effects(self):
        source, plan, baseline, *_ = lf_fixture()
        captured = []
        with tempfile.TemporaryDirectory(dir=ops.TASK / "runtime" / "temp") as directory:
            destination = Path(directory) / "new-lf-plan.json"
            with patch.object(preflight, "task_path", side_effect=lambda p, base: p), \
                 patch.object(ops, "manifest", return_value=source), \
                 patch.object(preflight, "source_migrations", return_value=plan["migrations"]), \
                 patch.object(verify, "load_baseline_chain", return_value={"contract": baseline, "hashes": plan["lf_contract"]["baseline_receipts"]}), \
                 patch.object(ops, "remote_python", return_value=plan["inspection"]), \
                 patch.object(preflight, "exclusive_json", side_effect=lambda p, value: captured.append(value) or "1" * 64), \
                 patch.object(ops, "receipt", return_value=Path(directory) / "receipt.json"), \
                 redirect_stdout(io.StringIO()):
                preflight.main(["--learn-frontend", "--plan", str(destination), "--baseline-release", "a", "b", "v"])
        self.assertEqual(len(captured), 1)
        self.assertTrue(preflight.lf_phase(captured[0]))
        self.assertNotEqual(captured[0]["nonce"], plan["nonce"])
        self.assertEqual(captured[0]["backup_scope"]["databases"], ["learn_db"])

    def test_verify_cli_plans_lf_with_the_bound_baseline(self):
        source, plan, baseline, _, _, build, activation, request = lf_receipts()
        inventory = {"build": (build, "1" * 64), "activation": (activation, "2" * 64)}
        with patch.object(ops, "manifest", return_value=source), \
             patch.object(verify, "load_receipt", side_effect=lambda p: inventory[str(p)]), \
             patch.object(verify, "load_baseline_chain", return_value={"contract": baseline, "hashes": plan["lf_contract"]["baseline_receipts"]}), \
             redirect_stdout(io.StringIO()) as output:
            verify.main(["--learn-frontend", "--commit", request["binding"]["commit"], "--source-fingerprint", source["fingerprint"],
                         "--release-nonce", plan["nonce"], "--build-receipt", "build", "--activation-receipt", "activation",
                         "--baseline-release", "a", "b", "v"])
        value = json.loads(output.getvalue())
        self.assertTrue(preflight.lf_phase(value))
        self.assertFalse(value["execute"])
        self.assertEqual(value["managed_file_count"], len(activate.selected_rows(source, learn_frontend=True)))

    def test_activation_and_verification_snapshot_project_all_retained_labels_identically(self):
        _, _, baseline, snapshot, *_ = lf_fixture()
        current = copy.deepcopy(snapshot["containers"])
        prior = next(item for item in current if item["service"] == "learn-service")
        for name in ("learn-migrate", "env-orchestrator"):
            extra = copy.deepcopy(prior)
            extra.update(service=name, container_id=digest("preserved-" + name),
                         running=name == "env-orchestrator", pid=432 if name == "env-orchestrator" else 0)
            current.append(extra)
        configurations = {item["container_id"]: {"Labels": item.pop("source_labels", {}), "Cmd": item.get("command")} for item in current}
        request = {"binding": baseline["binding"], "release_phase": preflight.LF_PHASE}
        snapshots = []
        for module, entry, function in ((activate, "activation_main", "safe_snapshot"), (verify, "verify_main", "verify_snapshot")):
            namespace = {"REQUEST": request}
            exec(compile(module.remote_program().rsplit(entry + "()", 1)[0], "<lf-label-projection>", "exec"), namespace)
            def run(argv, **kwargs):
                self.assertEqual(argv[:2], ["docker", "inspect"])
                return json.dumps([{"Id": identifier, "Config": configurations[identifier]} for identifier in argv[2:]]).encode()
            namespace.update(compose=lambda: {}, protected_rows=lambda _: baseline["protected_files"],
                             containers=lambda: copy.deepcopy(current), code_rows=lambda _: snapshot["code_files"], run=run)
            snapshots.append(namespace[function]())
        self.assertEqual(preflight.code_runtime(snapshots[0], TARGETS), preflight.code_runtime(snapshots[1], TARGETS))


if __name__ == "__main__":
    unittest.main()
