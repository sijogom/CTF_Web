"""Pure three-context release contracts; every external operation is mocked."""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activate_release as activate
import backup_images
import backup_release as backup
import quiesce_release as quiesce
import release_preflight as preflight
import stage_build_release as stage
import verify_release as verify
from test_learn_release_scope import baseline_chain
from test_release_backup import fixtures
import test_stage_build_release as stage_tests
from test_verify_release import category_projection, digest, fixture, receipts, row


WORKERS = {"personal-notification-runner", *verify.LEGACY_PROCESS_WORKERS}
TARGETS = {"auth-service", "learn-service", "frontend", *WORKERS}


def scope_fixture(*, auth_head="20261013_005", learn_head="20261018_030"):
    request, snapshot, _, _, triggers = fixture()
    _, _, base_plan, *_ = fixtures()
    previous = {**verify.EXPECTED_HEADS, "learn": verify.LEARN_CATEGORY_HEAD}
    graph = {name: {"head": head, "revisions": sorted({previous[name], head})}
             for name, head in {"auth": auth_head, "learn": learn_head, "community": previous["community"]}.items()}
    graph["learn"]["revisions"] = sorted(set(graph["learn"]["revisions"]) | {"20261013_028"})
    for item in snapshot["containers"]:
        item.update(status="running", finished_at="0001-01-01T00:00:00Z", mounts=[])
        item.setdefault("pid", 900)
        item.setdefault("started_at", "2026-10-13T06:00:00Z")
    minio = copy.deepcopy(next(item for item in base_plan["inspection"]["containers"] if item["service"] == "minio"))
    snapshot["containers"].append(minio)
    snapshot["containers"].sort(key=lambda item: (item["service"], item["container_id"]))
    snapshot["source_labels"] = {item["container_id"]: item.get("source_labels", {}) for item in snapshot["containers"]}
    inspection = copy.deepcopy(base_plan["inspection"])
    inspection["containers"] = [{key: value for key, value in item.items() if key not in {"source_labels", "command"}} for item in snapshot["containers"]]
    inspection["code_files"] = copy.deepcopy(snapshot["code_files"])
    for name, item in inspection["databases"].items():
        item["head"] = [previous[name]]
    baseline = {**request, "expected_heads": previous, "image_bindings": request["image_bindings"],
                "containers_before": verify.container_rows(snapshot["containers"])}
    source = {"fingerprint": "f" * 64, "files": copy.deepcopy(request["managed_files"])}
    if "20261018_030" in graph["learn"]["revisions"]:
        source["files"].append(row("services/learn/migrations/versions/20261018_030_submission_flag_reveal_reason.py"))
    source["files"] = verify.file_rows(source["files"])
    hashes = [{"activation": "a" * 64, "build": "b" * 64, "verify": "c" * 64}]
    contract = preflight.make_alf_contract(source["files"], inspection, graph, baseline, hashes)
    plan = preflight.make_plan(source, inspection, graph, alf_contract=contract)
    return source, plan, baseline, snapshot, triggers


def scoped_receipts():
    source, plan, baseline, snapshot, triggers = scope_fixture()
    binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
    build, activation = receipts({**baseline, "binding": binding}, snapshot)
    fields = {key: plan[key] for key in preflight.ALF_FIELDS}
    build.update(fields)
    remote = build["remote"]
    remote.update(fields, frontend_only=False, learn_only=False,
                  retained_baseline=verify.baseline_summary(baseline, ("auth", "learn", "frontend")))
    remote["completed_images"] = [item for item in remote["completed_images"] if item["service"] != "community"]
    for item in remote["completed_images"]:
        item["image_id"] = "sha256:" + digest("new-" + item["service"])
    built = verify.build_contract(build, binding, baseline=baseline, auth_learn_frontend=True)
    managed = activate.selected_rows(source, auth_learn_frontend=True)
    activation.update(fields, frontend_only=False, learn_only=False, baseline_receipts=plan["alf_contract"]["baseline_receipts"],
                      managed_files=managed, unmanaged_files=verify.file_rows(plan["alf_contract"]["community_source_files"] + baseline["unmanaged_files"]),
                      schema={"heads": {name: [head] for name, head in plan["alf_contract"]["target_heads"].items()}},
                      executed_migrations=list(plan["alf_contract"]["migration_services"]),
                      untargeted_runtime_before=preflight.code_runtime(snapshot, TARGETS),
                      untargeted_runtime_after=preflight.code_runtime(snapshot, TARGETS))
    verified = verify.activation_contract(activation, binding, managed, built, baseline=baseline, auth_learn_frontend=True)
    return source, plan, baseline, snapshot, triggers, build, activation, verified


def mock_activation(*, auth_head="20261013_005", learn_head="20261018_030", fail_up=False, fail_migration=False, stop_unknown=False, community_drift=False):
    source, plan, baseline, snapshot, _ = scope_fixture(auth_head=auth_head, learn_head=learn_head)
    binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
    old_images = baseline["images"]
    images = {name: "sha256:" + digest("new-" + name) if name != "community" else image for name, image in old_images.items()}
    current = copy.deepcopy(snapshot["containers"])
    for item in current:
        if item["service"] in TARGETS - {"frontend"}: item.update(running=False, pid=0, status="exited")
    original = copy.deepcopy(current)
    code = copy.deepcopy(snapshot["code_files"])
    heads = dict(plan["alf_contract"]["previous_heads"])
    request = {**{key: plan[key] for key in preflight.ALF_FIELDS}, "binding": binding, "frontend_only": False, "learn_only": False,
        "managed_files": activate.selected_rows(source, auth_learn_frontend=True), "modes": {}, "images": images,
        "image_bindings": {name: baseline["binding"] if name == "community" else binding for name in images},
        "protected_files": baseline["protected_files"], "current_container_baseline": copy.deepcopy(current),
        "backend_baseline": baseline, "worker_templates": None, "expected_heads": plan["alf_contract"]["target_heads"],
        "plan_sha256": "1" * 64, "backup_receipt_sha256": "2" * 64, "quiescence_sha256": "3" * 64,
        "baseline_receipts": plan["alf_contract"]["baseline_receipts"], "migration_wrapper_sha256": activate.sha(activate.MIGRATION_SITE_CUSTOMIZE.encode())}
    namespace = {"REQUEST": request}
    exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<alf-activation-mock>", "exec"), namespace)
    commands, written = [], []
    def snap():
        return {"containers": copy.deepcopy(current), "code_files": copy.deepcopy(code), "protected_files": baseline["protected_files"],
                "source_labels": {item["container_id"]: item.get("source_labels", {}) for item in current}}
    def execute_command(argv, log, timeout=600):
        commands.append(argv)
        if "run" in argv:
            name = next(name for name in ("auth", "learn") if name + "-migrate" in argv)
            if fail_migration: raise namespace["ActivateError"]("ACTIVATION_COMMAND_FAILED")
            heads[name] = plan["alf_contract"]["target_heads"][name]
        elif "up" in argv:
            name = argv[-1]
            item = next(item for item in current if item["service"] == name)
            item.update(container_id=digest("new-" + name), image_id=images[verify.SERVICE_IMAGES[name]], running=True, pid=888, status="running",
                        source_labels=activate.source_labels(binding))
            if fail_up: raise TimeoutError("synthetic uncertain up")
    def run(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ["docker", "stop"]:
            if stop_unknown: raise TimeoutError("synthetic uncertain stop")
            for item in current:
                if item["container_id"] in argv: item.update(running=False, pid=0, status="exited")
            return b""
        if argv[-3:] == ["config", "--format", "json"]:
            return json.dumps({"services": {name: {"image": images[verify.SERVICE_IMAGES[name]]} for name in TARGETS}}).encode()
        raise AssertionError("UNMOCKED_EXTERNAL_COMMAND")
    def sql(database, query):
        if "alembic_version" in query:
            return [heads[next(name for name, value in preflight.DATABASES.items() if value == database)]]
        if "pg_stat_activity" in query: return 0
        raise AssertionError("UNMOCKED_DATABASE_QUERY")
    def copy_source(*args):
        code[:] = verify.file_rows(request["managed_files"] + plan["alf_contract"]["community_source_files"] + baseline["unmanaged_files"])
    def wait(*args):
        if community_drift:
            next(item for item in current if item["service"] == "community-service")["restart_count"] += 1
    model = {"services": {name: {} for name in (*verify.SERVICE_IMAGES, *verify.MIGRATION_IMAGES)}}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        release = root / binding["release_nonce"]
        release.mkdir()
        namespace.update(ROOT=root / "live", RELEASE_ROOT=str(root), safe_snapshot=snap, compose=lambda: model,
            check_images=lambda: None, containers=lambda: copy.deepcopy(current), sql=sql,
            database_rows=lambda _: {"community": {"schema_sha256": plan["alf_contract"]["community_schema_sha256"]}},
            private_command=execute_command, private_json=lambda path, value: written.append((str(path), copy.deepcopy(value))),
            run=run, copy_managed=copy_source, wait_service=wait,
            schema_contract=lambda: {"heads": {name: [head] for name, head in heads.items()}, "trigger_count": 18, "required_schema_verified": True},
            category_compatibility=lambda: {"custom_category_count": 2, "custom_event_count": 1,
                "legacy_enum_image_rollback_forbidden": True, "automatic_backend_rollback_performed": False})
        with redirect_stdout(io.StringIO()) as output: namespace["activation_main"]()
        result = json.loads(output.getvalue())
        if "runtime_overlay" in result: result["runtime_overlay"] = Path(result["runtime_overlay"]).as_posix()
        with patch.object(activate, "RELEASE_ROOT", root.as_posix()):
            activate.validate_remote_receipt(result, request)
    return result, commands, current, original, written


class AuthLearnFrontendTests(unittest.TestCase):
    def test_scope_is_explicit_and_never_aliases_a_legacy_phase(self):
        source, plan, *_ = scope_fixture()
        preflight.validate_plan(plan, source["fingerprint"])
        self.assertTrue(preflight.alf_phase(plan))
        self.assertFalse(preflight.code_only_phase(plan))
        for change in ({"auth_learn_frontend": False}, {"auth_learn_frontend": 1}, {"learn_only": True},
                       {"frontend_only": True}, {"release_phase": preflight.LEARN_CODE_PHASE}, {"alf_contract": {}}):
            with self.subTest(change=change), self.assertRaises(preflight.ReleaseError):
                preflight.validate_plan({**plan, **change}, source["fingerprint"])
        old = fixtures()[2]
        self.assertFalse(preflight.alf_phase(old))
        self.assertNotIn("auth_learn_frontend", old)

    def test_heads_and_migrations_are_source_derived_including_later_approved_heads(self):
        for auth, learn, expected in (("20261013_005", "20261018_030", ["learn"]),
                                      ("20261018_006", "20261018_031", ["auth", "learn"]),
                                      ("20261013_005", "20261013_029", [])):
            _, plan, *_ = scope_fixture(auth_head=auth, learn_head=learn)
            self.assertEqual(plan["alf_contract"]["migration_services"], expected)
            self.assertEqual(plan["alf_contract"]["target_heads"]["auth"], auth)
            self.assertEqual(plan["alf_contract"]["target_heads"]["learn"], learn)
            self.assertEqual(verify.EXPECTED_HEADS["learn"], "20261013_028")
            self.assertEqual(verify.LEARN_CATEGORY_HEAD, "20261013_029")

    def test_foreign_source_heads_schema_or_community_rows_cannot_be_retained(self):
        source, plan, baseline, *_ = scope_fixture()
        for part in ("source", "live", "baseline", "graph", "schema"):
            s, p, b = copy.deepcopy(source), copy.deepcopy(plan), copy.deepcopy(baseline)
            if part == "source": next(item for item in s["files"] if item["path"].startswith("services/community/"))["sha256"] = "0" * 64
            if part == "live": next(item for item in p["inspection"]["code_files"] if item["path"].startswith("services/community/"))["sha256"] = "0" * 64
            if part == "baseline": b["expected_heads"]["auth"] = "foreign"
            if part == "graph": p["migrations"]["community"]["head"] = "next_community"
            if part == "schema": p["inspection"]["databases"]["community"]["schema_sha256"] = "0" * 64
            with self.subTest(part=part), self.assertRaises(preflight.ReleaseError):
                if part == "schema": preflight.validate_alf_contract(p["alf_contract"], p["inspection"], p["migrations"])
                else: preflight.make_alf_contract(s["files"], p["inspection"], p["migrations"], b, p["alf_contract"]["baseline_receipts"])

    def test_contexts_builds_and_overlay_are_exactly_selected(self):
        _, plan, baseline, snapshot, *_ = scope_fixture()
        self.assertEqual([name for name, _ in stage.selected_contexts(auth_learn_frontend=True)], ["auth", "learn", "frontend"])
        commands = stage.build_commands(Path("/source"), baseline["binding"],
            {"NEXT_PUBLIC_SITE_URL": "https://ctf.example.com/ctf", "NEXT_PUBLIC_ENV_PROXY_BASE": "https://ctf.example.com/ctf/lab", "NEXT_PUBLIC_ENV_ACCESS_MODE": "proxy"}, auth_learn_frontend=True)
        self.assertEqual([name for name, _ in commands], ["auth", "learn", "frontend"])
        model = {"services": {name: {} for name in (*verify.SERVICE_IMAGES, *verify.MIGRATION_IMAGES)}}
        workers = activate.derive_workers(snapshot["containers"], model["services"], auth_learn_frontend=True)
        self.assertEqual(set(workers), WORKERS)
        overlay = activate.make_overlay(model, baseline["images"], baseline["binding"], workers, Path("/runtime"),
                                        auth_learn_frontend=True, migration_services=plan["alf_contract"]["migration_services"])
        self.assertEqual(set(overlay["services"]), TARGETS | {"learn-migrate"})
        steps = activate.activation_steps(workers, auth_learn_frontend=True, migration_services=["learn"])
        self.assertEqual([name for action, name in steps if action == "migration"], ["learn"])
        self.assertFalse(any(action == "baseline" or name.startswith("community") for action, name in steps))
        self.assertEqual(steps[-3:], [("up", "frontend"), ("health", "frontend"), ("verify", "all")])
        for kwargs in ({"frontend_only": True}, {"learn_only": True}):
            with self.assertRaises(stage.StageError): stage.selected_contexts(auth_learn_frontend=True, **kwargs)

    def test_quiescence_selects_only_two_api_images_and_five_workers(self):
        _, plan, *_ = scope_fixture()
        selected = quiesce.select_targets(plan)
        self.assertEqual({item["service"] for item in selected["apis"]}, {"auth-service", "learn-service"})
        self.assertEqual({item["service"] for item in selected["workers"]}, WORKERS)
        self.assertEqual({item["service"] for item in quiesce.action_targets(plan, "stop")}, TARGETS - {"frontend"} | {"minio"})
        unknown = copy.deepcopy(next(item for item in plan["inspection"]["containers"] if item["service"] == "learn-service"))
        unknown.update(service="unexpected-consumer", container_id="0" * 64)
        plan["inspection"]["containers"].append(unknown)
        with self.assertRaisesRegex(preflight.ReleaseError, "SAME_IMAGE"):
            quiesce.select_targets(plan)

    def test_two_database_fence_allows_community_clients_but_not_target_clients(self):
        source, plan, *_ = scope_fixture()
        frozen = copy.deepcopy(plan["inspection"])
        targets = {item["container_id"] for item in quiesce.action_targets(plan, "stop")}
        for item in frozen["containers"]:
            if item["container_id"] in targets: item.update(running=False, pid=0, status="exited", finished_at="2026-10-18T00:00:00Z")
        for name in ("auth", "learn"): frozen["databases"][name]["client_session_count"] = 0
        claim = {"schema_version": 1, "kind": "renewal-quiescence", "task": preflight.TASK_ID, "nonce": plan["nonce"],
            "plan_sha256": "a" * 64, "source_fingerprint": source["fingerprint"], "verified_at": datetime.now(timezone.utc).isoformat(),
            "drain_verified": True, "external_writers_absent_verified": True, "fence_maintained_until_capture_complete": True,
            "inspection": frozen, **{key: plan[key] for key in preflight.ALF_FIELDS}}
        backup.validate_quiescence(claim, plan, "a" * 64)
        self.assertEqual(plan["backup_scope"]["databases"], ["platform_db", "learn_db"])
        self.assertEqual([item["name"] for item in backup.stream_specs(plan) if item["kind"] == "database"], ["platform_db.dump", "learn_db.dump"])
        for name in ("auth", "learn"):
            bad = copy.deepcopy(claim); bad["inspection"]["databases"][name]["client_session_count"] = 1
            with self.assertRaisesRegex(preflight.ReleaseError, "CLIENTS_REMAIN"): backup.validate_quiescence(bad, plan, "a" * 64)
        bad = copy.deepcopy(claim)
        next(item for item in bad["inspection"]["containers"] if item["service"] == "community-service")["restart_count"] += 1
        with self.assertRaisesRegex(preflight.ReleaseError, "UNRELATED"): backup.validate_quiescence(bad, plan, "a" * 64)

    def test_image_selection_backs_up_three_immutable_images_without_community(self):
        _, plan, baseline, *_ = scope_fixture()
        ids, _ = backup_images.selection(plan)
        self.assertEqual(ids, sorted(baseline["images"][name] for name in ("auth", "learn", "frontend")))
        self.assertNotIn(baseline["images"]["community"], ids)

    def test_backup_contract_requires_scoped_two_database_and_three_image_evidence(self):
        source, plan, baseline, *_ = scope_fixture()
        binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
        value = {"schema_version": 1, "kind": "renewal-stream-backup", "nonce": plan["nonce"],
            "source_fingerprint": source["fingerprint"], "plan_sha256": "a" * 64, "quiescence_sha256": "b" * 64,
            "quiescence_verified": True, "release_eligible": True, "image_backup_verified": True,
            "ctfd_db_included": False, "database_count": 2, "cross_service_consistency_verified": False,
            "auth_learn_object_consistency_verified": True, "captured_databases": ["platform_db", "learn_db"],
            "selected_image_ids": sorted(baseline["images"][name] for name in ("auth", "learn", "frontend")),
            "image_backup_receipt_sha256": "c" * 64, "files": [{}] * 7, **{key: plan[key] for key in preflight.ALF_FIELDS}}
        activate.backup_contract(value, binding, "a" * 64, "b" * 64, plan["inspection"]["containers"], auth_learn_frontend=True)
        for key, replacement in (("database_count", 3), ("cross_service_consistency_verified", True),
                ("auth_learn_object_consistency_verified", False), ("captured_databases", ["learn_db"]),
                ("selected_image_ids", sorted(baseline["images"].values()))):
            with self.subTest(key=key), self.assertRaises(activate.ActivateError):
                activate.backup_contract({**value, key: replacement}, binding, "a" * 64, "b" * 64, plan["inspection"]["containers"], auth_learn_frontend=True)

    def test_build_and_activation_merge_retained_community_with_its_old_binding(self):
        _, plan, baseline, _, _, build, activation, checked = scoped_receipts()
        self.assertEqual(checked["images"]["community"], baseline["images"]["community"])
        self.assertEqual(checked["image_bindings"]["community"], baseline["binding"])
        self.assertEqual(checked["expected_heads"], plan["alf_contract"]["target_heads"])
        self.assertEqual(set(checked["changed_services"]), TARGETS | {"learn-migrate"})
        bad = copy.deepcopy(build)
        bad["remote"]["completed_images"].append({"service": "community"})
        with self.assertRaises(verify.VerifyError): verify.build_contract(bad, build["binding"], baseline=baseline, auth_learn_frontend=True)
        bad = copy.deepcopy(activation); bad["unmanaged_files"] = []
        with self.assertRaises(verify.VerifyError): verify.activation_contract(bad, build["binding"], activation["managed_files"], verify.build_contract(build, build["binding"], baseline=baseline, auth_learn_frontend=True), baseline=baseline, auth_learn_frontend=True)
        for key, replacement in (("executed_migrations", []), ("executed_migrations", ["auth", "learn"])):
            with self.subTest(replacement=replacement), self.assertRaisesRegex(verify.VerifyError, "ALF_MIGRATIONS_INCOMPLETE"):
                verify.activation_contract({**activation, key: replacement}, build["binding"], activation["managed_files"],
                    verify.build_contract(build, build["binding"], baseline=baseline, auth_learn_frontend=True), baseline=baseline, auth_learn_frontend=True)

    def test_independent_verifier_requires_retained_process_schema_and_reason_contract(self):
        _, plan, _, snapshot, triggers, _, _, request = scoped_receipts()
        snapshot = copy.deepcopy(snapshot)
        for item in snapshot["containers"]:
            if item["service"] in TARGETS:
                item["image_id"] = request["images"][verify.SERVICE_IMAGES[item["service"]]]
                item["source_labels"] = activate.source_labels(request["binding"])
        snapshot["code_files"] = verify.file_rows(request["managed_files"] + request["unmanaged_files"])
        labels = {image: activate.source_labels(request["image_bindings"][name]) for name, image in request["images"].items()}
        labels[request["images"]["frontend"]][verify.FRONTEND_ARGS_LABEL] = request["frontend_args_sha256"]
        schema = {"community_schema_sha256": plan["alf_contract"]["community_schema_sha256"], "reason_nullable": True}
        def evaluate(after=snapshot, states=None):
            return verify.evaluate_snapshot(request, snapshot, after, labels,
                {name: [head] for name, head in request["expected_heads"].items()}, triggers, category_projection(), alf_states=states if states is not None else [schema, schema])
        value = evaluate()
        self.assertEqual(value["status"], "passed", value["checks"])
        verify.validate_result(value, request["binding"], auth_learn_frontend=True, alf_contract=plan["alf_contract"])
        for key in ("community_schema_sha256", "reason_nullable"):
            bad = {**schema, key: "0" * 64 if key.endswith("sha256") else False}
            self.assertEqual(evaluate(states=[schema, bad])["checks"]["alf_schema_preservation"]["status"], "failed")
        after = copy.deepcopy(snapshot)
        next(item for item in after["containers"] if item["service"] == "community-service")["pid"] += 1
        self.assertEqual(evaluate(after)["checks"]["alf_untargeted_runtime"]["status"], "failed")

    def test_generated_remote_programs_include_new_actual_helpers(self):
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<alf-activate>", "exec"), namespace)
        _, plan, *_ = scope_fixture()
        namespace["validate_alf_contract"](plan["alf_contract"])
        self.assertEqual([name for action, name in namespace["activation_steps"](sorted(WORKERS), auth_learn_frontend=True, migration_services=["learn"]) if action == "migration"], ["learn"])
        for program in (stage.remote_program(), verify.remote_program()): compile(program, "<alf-remote>", "exec")

    def test_serialized_remote_build_runs_exact_three_contexts_with_scoped_preflight(self):
        _, plan, *_ = scope_fixture()
        fixture = stage_tests.StageBuildTests()
        fixture.setUp()
        try:
            result, built, _, _ = fixture._remote_execution_fixture(alf_contract=plan["alf_contract"])
            self.assertEqual(result["status"], "built-not-activated", result.get("error"))
            self.assertEqual(built, ["auth", "learn", "frontend"])
            self.assertEqual(result["alf_contract"], plan["alf_contract"])
            self.assertEqual(result["retained_baseline"]["images"].keys(), {"community"})
            result.pop("remote_receipt")
            stage.validate_remote_receipt(result, fixture.remote_request)
            self.assertEqual(result["live_before"], result["live_after"])
        finally:
            fixture.tearDown()

    def test_serialized_remote_activation_runs_one_migration_and_exact_eight_targets(self):
        result, commands, current, original, _ = mock_activation()
        self.assertEqual(result["status"], "activated", result.get("error"))
        self.assertEqual(result["executed_migrations"], ["learn"])
        self.assertEqual({command[-1] for command in commands if "up" in command}, TARGETS)
        self.assertEqual(len([command for command in commands if "run" in command]), 1)
        self.assertFalse(any("--baseline-only" in command or "rollback" in str(command) for command in commands))
        for item in original:
            if item["service"] not in TARGETS:
                self.assertEqual(item, next(other for other in current if other["container_id"] == item["container_id"]))
        self.assertEqual(result["untargeted_runtime_before"], result["untargeted_runtime_after"])

    def test_uncertain_first_backend_up_is_fenced_even_without_any_migration(self):
        result, commands, current, original, _ = mock_activation(learn_head="20261013_029", fail_up=True)
        self.assertEqual(result["status"], "partial-unverified")
        self.assertEqual(result["executed_migrations"], [])
        self.assertTrue(result["writer_fence_maintained"])
        stops = [command for command in commands if command[:2] == ["docker", "stop"]]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][2:4], ["--time", "-1"])
        self.assertEqual(len([command for command in commands if "up" in command]), 1)
        self.assertFalse(any("run" in command or "rollback" in str(command) for command in commands))
        self.assertFalse(result["database_restore_performed"])
        for item in original:
            if item["service"].startswith("community") or item["service"] in {"marketplace-settlement-runner", "comment-notification-runner"}:
                self.assertEqual(item, next(other for other in current if other["container_id"] == item["container_id"]))

    def test_serialized_remote_activation_migrates_both_changed_source_heads_in_order(self):
        result, commands, *_ = mock_activation(auth_head="20261018_006", learn_head="20261018_031")
        self.assertEqual(result["status"], "activated", result.get("error"))
        self.assertEqual(result["executed_migrations"], ["auth", "learn"])
        self.assertEqual([next(name for name in ("auth-migrate", "learn-migrate") if name in command)
                          for command in commands if "run" in command], ["auth-migrate", "learn-migrate"])
        self.assertEqual(result["schema"]["heads"]["auth"], ["20261018_006"])
        self.assertEqual(result["schema"]["heads"]["learn"], ["20261018_031"])

    def test_unknown_stop_or_failed_migration_remains_failed_without_replay_or_restore(self):
        for kwargs in ({"fail_up": True, "stop_unknown": True}, {"fail_migration": True}, {"community_drift": True}):
            with self.subTest(kwargs=kwargs):
                result, commands, *_ = mock_activation(**kwargs)
                self.assertEqual(result["status"], "partial-unverified")
                self.assertFalse(result["database_restore_performed"])
                self.assertLessEqual(len([command for command in commands if command[:2] == ["docker", "stop"]]), 1)
                self.assertFalse(any("rollback" in str(command) or "--baseline-only" in command for command in commands))
                if kwargs.get("stop_unknown"):
                    self.assertTrue(result["recovery_outcome_unverified"])
                    self.assertFalse(result["writer_fence_maintained"])

    def test_legacy_chain_can_add_only_a_bound_explicit_new_scope_node(self):
        triples, inventory = baseline_chain(full_learn_head=verify.LEARN_CATEGORY_HEAD, families=(None,))
        with patch.object(verify, "load_receipt", side_effect=lambda path: inventory[str(path)]):
            baseline = verify.load_baseline_chain(triples)["contract"]
        _, plan, _, snapshot, _, build, activation, _ = scoped_receipts()
        prior_hashes = [{name: inventory[str(path)][1] for name, path in zip(("activation", "build", "verify"), triples[0])}]
        for item in (build, build["remote"], activation):
            item["alf_contract"]["baseline_receipts"] = prior_hashes
        build["remote"]["retained_baseline"] = verify.baseline_summary(baseline, ("auth", "learn", "frontend"))
        activation["baseline_receipts"] = prior_hashes
        build_sha = verify.sha(verify.canonical(build)); activation_sha = verify.sha(verify.canonical(activation))
        checked = copy.deepcopy(inventory[triples[0][2]][0])
        checked.update({**{key: activation[key] for key in preflight.ALF_FIELDS}, "binding": build["binding"], "frontend_only": False,
            "learn_only": False, "baseline_receipts": prior_hashes, "build_receipt_sha256": build_sha, "activation_receipt_sha256": activation_sha})
        checked["checks"].update({"alf_schema_preservation": {"status": "passed", "count": 2}, "alf_untargeted_runtime": {"status": "passed", "count": 6}})
        new = ("new-activation", "new-build", "new-verify")
        for path, value in zip(new, (activation, build, checked)): inventory[path] = (value, verify.sha(verify.canonical(value)))
        with patch.object(verify, "load_receipt", side_effect=lambda path: inventory[str(path)]):
            value = verify.load_baseline_chain(triples + [new])
            self.assertEqual(value["contract"]["expected_heads"]["learn"], "20261018_030")
            self.assertEqual(value["contract"]["images"]["community"], baseline["images"]["community"])
            self.assertEqual(value["backup_anchor"]["activation"], activation)
            bad_verify = copy.deepcopy(checked)
            bad_verify["alf_contract"]["community_schema_sha256"] = "0" * 64
            inventory[new[2]] = (bad_verify, verify.sha(verify.canonical(bad_verify)))
            with self.assertRaisesRegex(verify.VerifyError, "ALF_VERIFY_CONTRACT_CHANGED"):
                verify.load_baseline_chain(triples + [new])
            inventory[new[2]] = (checked, verify.sha(verify.canonical(checked)))
            bad = copy.deepcopy(activation); bad["alf_contract"]["baseline_receipts"] = []
            inventory[new[0]] = (bad, verify.sha(verify.canonical(bad)))
            with self.assertRaises(Exception): verify.load_baseline_chain(triples + [new])

    def test_new_scope_cli_three_legacy_baseline_arguments_keep_bound_029_heads(self):
        source, plan, baseline, _, _, build, activation, _ = scoped_receipts()
        binding = build["binding"]
        values = {"new-build": build, "new-activation": activation}
        triple = [Path("old-activation"), Path("old-build"), Path("old-verify")]
        chain = {"contract": baseline, "hashes": plan["alf_contract"]["baseline_receipts"]}
        argv = ["--commit", binding["commit"], "--source-fingerprint", binding["source_fingerprint"],
            "--release-nonce", binding["release_nonce"], "--build-receipt", "new-build", "--activation-receipt", "new-activation",
            "--auth-learn-frontend", "--baseline-activation-receipt", str(triple[0]),
            "--baseline-build-receipt", str(triple[1]), "--baseline-verify-receipt", str(triple[2])]
        with patch.object(verify.ops, "manifest", return_value=source), \
                patch.object(verify, "load_receipt", side_effect=lambda path: (values[str(path)], "a" * 64)), \
                patch.object(verify, "load_baseline_chain", return_value=chain) as loader, \
                patch.object(verify.ops, "ssh", side_effect=AssertionError("ACTUAL_FORBIDDEN")) as ssh, \
                patch.object(verify.ops, "receipt", side_effect=AssertionError("WRITE_FORBIDDEN")) as write, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(verify.main(argv), 0)
        loader.assert_called_once_with([tuple(triple)])
        ssh.assert_not_called()
        write.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertEqual(value["baseline_receipts"], chain["hashes"])
        self.assertEqual(value["alf_contract"]["previous_heads"]["learn"], verify.LEARN_CATEGORY_HEAD)
        self.assertFalse(value["learn_only"])
        self.assertEqual(value["release_phase"], preflight.ALF_PHASE)
        self.assertNotIn("baseline_activation_receipt_sha256", value)


if __name__ == "__main__":
    unittest.main()
