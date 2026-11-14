"""Additive four-context contracts. All remote actions use in-memory fakes."""
from __future__ import annotations

import copy
import ast
from contextlib import redirect_stdout
from datetime import datetime, timezone
import inspect
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
from test_auth_learn_frontend_scope import scope_fixture
from test_learn_release_scope import baseline_chain
from test_verify_release import category_projection, digest, receipts, row


WORKERS = set(verify.SERVICE_IMAGES) - {*activate.API_ORDER, "frontend"}
TARGETS = set(verify.SERVICE_IMAGES)
HEADS = {"auth": "20261018_006", "learn": "20261018_031", "community": "20261013_005"}
PRIOR_TARGET_UNMANAGED = [
    {"path": "frontend/C\u044f\u0410\u2551Program", "bytes": 0,
     "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    {"path": "frontend/local-session-cookies.txt", "bytes": 135,
     "sha256": "be7a52d6d1b2e5e2c7a9e338f3ab71b4b2e76797f19cc06d5899aece2701365b"},
]


def alcf_fixture():
    source, old_plan, baseline, snapshot, triggers = scope_fixture()
    historical = [row("services/" + family + "/migrations/versions/" + head + "_existing.py")
                  for family, head in baseline["expected_heads"].items()]
    baseline["managed_files"] = verify.file_rows(baseline["managed_files"] + historical)
    baseline["unmanaged_files"] = verify.file_rows(baseline["unmanaged_files"] + copy.deepcopy(PRIOR_TARGET_UNMANAGED))
    snapshot["code_files"] = verify.file_rows(baseline["managed_files"] + baseline["unmanaged_files"])
    inspection = copy.deepcopy(old_plan["inspection"])
    inspection["code_files"] = copy.deepcopy(snapshot["code_files"])
    source["files"] = verify.file_rows(source["files"] + historical + [
        row("services/auth/migrations/versions/20261018_006_competition_teams.py"),
        row("services/learn/migrations/versions/20261018_031_challenge_attachments.py")])
    next(r for r in source["files"] if r["path"] == "services/community/Dockerfile")["sha256"] = digest("approved-community-fix")
    graph = {name: {"head": head, "revisions": sorted({baseline["expected_heads"][name], head})} for name, head in HEADS.items()}
    graph["learn"]["revisions"] = ["20261013_028", "20261013_029", "20261018_030", "20261018_031"]
    hashes = [{name: digest(str(i) + name) for name in ("activation", "build", "verify")} for i in range(5)]
    contract = preflight.make_alcf_contract(source["files"], inspection, graph, baseline, hashes)
    plan = preflight.make_plan(source, inspection, graph, alcf_contract=contract)
    return source, plan, baseline, snapshot, triggers


def alcf_receipts():
    source, plan, baseline, snapshot, triggers = alcf_fixture()
    binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
    build, activation = receipts({**baseline, "binding": binding}, snapshot)
    fields = {key: plan[key] for key in preflight.ALCF_FIELDS}
    build.update(fields)
    build["remote"].update(fields, frontend_only=False, learn_only=False,
                           retained_baseline=verify.baseline_summary(baseline, tuple(HEADS) + ("frontend",)))
    for item in build["remote"]["completed_images"]:
        item["image_id"] = "sha256:" + digest("new-" + item["service"])
    built = verify.build_contract(build, binding, baseline=baseline, auth_learn_community_frontend=True)
    managed = activate.selected_rows(source, auth_learn_community_frontend=True)
    activation.update(fields, frontend_only=False, learn_only=False, baseline_receipts=plan["alcf_contract"]["baseline_receipts"],
        managed_files=managed, unmanaged_files=baseline["unmanaged_files"], executed_migrations=["auth", "learn"],
        schema={"heads": {name: [head] for name, head in HEADS.items()}},
        untargeted_runtime_before=preflight.code_runtime(snapshot, TARGETS),
        untargeted_runtime_after=preflight.code_runtime(snapshot, TARGETS))
    checked = verify.activation_contract(activation, binding, managed, built, baseline=baseline, auth_learn_community_frontend=True)
    return source, plan, baseline, snapshot, triggers, build, activation, checked


def mock_activation(*, fail_up=False, fail_migration=False, stop_unknown=False, drift=False, fail_health=None, unmanaged_change=None):
    source, plan, baseline, snapshot, _ = alcf_fixture()
    binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
    images = {name: "sha256:" + digest("new-" + name) for name in baseline["images"]}
    current = copy.deepcopy(snapshot["containers"])
    for item in current:
        if item["service"] in TARGETS - {"frontend"}:
            item.update(running=False, pid=0, status="exited")
    original = copy.deepcopy(current)
    code = copy.deepcopy(snapshot["code_files"])
    if unmanaged_change == "missing":
        code.remove(next(item for item in code if item["path"] == PRIOR_TARGET_UNMANAGED[0]["path"]))
    elif unmanaged_change == "changed":
        next(item for item in code if item["path"] == PRIOR_TARGET_UNMANAGED[1]["path"])["sha256"] = "0" * 64
    elif unmanaged_change == "new":
        code.append(row("frontend/unbound-extra.txt"))
    elif unmanaged_change == "forged":
        baseline["unmanaged_files"] = [item for item in baseline["unmanaged_files"] if item not in PRIOR_TARGET_UNMANAGED]
    heads = dict(plan["alcf_contract"]["previous_heads"])
    request = {**{key: plan[key] for key in preflight.ALCF_FIELDS}, "binding": binding,
        "frontend_only": False, "learn_only": False, "managed_files": activate.selected_rows(source), "modes": {},
        "images": images, "image_bindings": {name: binding for name in images}, "protected_files": baseline["protected_files"],
        "current_container_baseline": copy.deepcopy(current), "backend_baseline": baseline, "worker_templates": None,
        "expected_heads": HEADS, "plan_sha256": "1" * 64, "backup_receipt_sha256": "2" * 64, "quiescence_sha256": "3" * 64,
        "baseline_receipts": plan["alcf_contract"]["baseline_receipts"],
        "migration_wrapper_sha256": activate.sha(activate.MIGRATION_SITE_CUSTOMIZE.encode())}
    namespace = {"REQUEST": request}
    exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<alcf-activation-mock>", "exec"), namespace)
    commands, written = [], []
    def snapshot_now():
        return {"containers": copy.deepcopy(current), "code_files": copy.deepcopy(code), "protected_files": baseline["protected_files"],
                "source_labels": {item["container_id"]: item.get("source_labels", {}) for item in current}}
    def execute_command(argv, log, timeout=600):
        commands.append(argv)
        if "run" in argv:
            name = next(name for name in HEADS if name + "-migrate" in argv)
            if fail_migration:
                raise namespace["ActivateError"]("ACTIVATION_COMMAND_FAILED")
            heads[name] = HEADS[name]
        elif "up" in argv:
            name = argv[-1]
            item = next(item for item in current if item["service"] == name)
            item.update(container_id=digest("new-" + name), image_id=images[verify.SERVICE_IMAGES[name]], running=True,
                        pid=888, status="running", source_labels=activate.source_labels(binding))
            if fail_up:
                raise TimeoutError("uncertain synthetic up")
    def run(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ["docker", "stop"]:
            if stop_unknown:
                raise TimeoutError("uncertain synthetic stop")
            for item in current:
                if item["container_id"] in argv:
                    item.update(running=False, pid=0, status="exited")
            return b""
        if argv[-3:] == ["config", "--format", "json"]:
            return json.dumps({"services": {name: {"image": images[verify.SERVICE_IMAGES[name]]} for name in TARGETS}}).encode()
        raise AssertionError("UNMOCKED_EXTERNAL_COMMAND")
    def sql(database, query):
        if "alembic_version" in query:
            return [heads[next(name for name, value in preflight.DATABASES.items() if value == database)]]
        if "pg_stat_activity" in query:
            return 0
        raise AssertionError("UNMOCKED_DATABASE_QUERY")
    def copy_source(*args):
        code[:] = verify.file_rows(request["managed_files"] + baseline["unmanaged_files"])
    def wait(service, *args):
        if service == fail_health:
            raise namespace["ActivateError"]("TARGET_HEALTH_UNVERIFIED")
        if drift:
            next(item for item in current if item["service"] == "postgres")["restart_count"] += 1
    model = {"services": {name: {} for name in (*verify.SERVICE_IMAGES, *verify.MIGRATION_IMAGES)}}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / binding["release_nonce"]).mkdir()
        namespace.update(ROOT=root / "live", RELEASE_ROOT=str(root), safe_snapshot=snapshot_now, compose=lambda: model,
            check_images=lambda: None, containers=lambda: copy.deepcopy(current), sql=sql,
            database_rows=lambda _: {"community": {"schema_sha256": plan["alcf_contract"]["community_schema_sha256"]}},
            private_command=execute_command, private_json=lambda path, value: written.append((str(path), copy.deepcopy(value))),
            run=run, copy_managed=copy_source, wait_service=wait,
            notification_state=lambda: {"ready": True, "notifications": 2, "events": 3},
            schema_contract=lambda: {"heads": {name: [head] for name, head in heads.items()}, "trigger_count": 18, "required_schema_verified": True},
            category_compatibility=lambda: {"custom_category_count": 2, "custom_event_count": 1,
                "legacy_enum_image_rollback_forbidden": True, "automatic_backend_rollback_performed": False})
        with redirect_stdout(io.StringIO()) as output:
            namespace["activation_main"]()
        result = json.loads(output.getvalue())
        if "runtime_overlay" in result:
            result["runtime_overlay"] = Path(result["runtime_overlay"]).as_posix()
        with patch.object(activate, "RELEASE_ROOT", root.as_posix()):
            activate.validate_remote_receipt(result, request)
    return result, request, commands, current, original, written


class AuthLearnCommunityFrontendTests(unittest.TestCase):
    def test_explicit_phase_is_not_alf_or_code_only(self):
        source, plan, *_ = alcf_fixture()
        preflight.validate_plan(plan, source["fingerprint"])
        self.assertTrue(preflight.alcf_phase(plan))
        self.assertFalse(preflight.alf_phase(plan))
        self.assertFalse(preflight.code_only_phase(plan))
        for delta in ({"auth_learn_community_frontend": False}, {"auth_learn_community_frontend": 1},
                      {"frontend_only": True}, {"learn_only": True}, {"auth_learn_frontend": True},
                      {"release_phase": preflight.ALF_PHASE}, {"alcf_contract": {}}):
            with self.subTest(delta=delta), self.assertRaises(preflight.ReleaseError):
                preflight.validate_plan({**plan, **delta}, source["fingerprint"])

    def test_all_seven_clis_offer_a_separate_mutually_exclusive_scope(self):
        for module in (preflight, stage, quiesce, backup_images, backup, activate, verify):
            body = inspect.getsource(module.main)
            self.assertIn('add_mutually_exclusive_group()', body)
            self.assertIn('scope.add_argument("--auth-learn-community-frontend"', body)

    def test_heads_are_source_derived_and_community_has_no_migration(self):
        _, plan, *_ = alcf_fixture()
        contract = plan["alcf_contract"]
        self.assertEqual(contract["target_heads"], HEADS)
        self.assertEqual(contract["migration_services"], ["auth", "learn"])
        self.assertEqual(len(contract["baseline_receipts"]), 5)
        for changed in (["learn", "auth"], ["auth"], ["auth", "learn", "community"]):
            with self.assertRaisesRegex(preflight.ReleaseError, "MIGRATION_SCOPE"):
                preflight.validate_alcf_contract({**contract, "migration_services": changed})

    def test_live_source_heads_schema_and_all_four_previous_images_are_bound(self):
        _, plan, *_ = alcf_fixture()
        for part in ("source", "heads", "schema", "image", "duplicate"):
            inspection = copy.deepcopy(plan["inspection"])
            if part == "source": next(r for r in inspection["code_files"] if r["path"].startswith("frontend/"))["sha256"] = "0" * 64
            if part == "heads": inspection["databases"]["auth"]["head"] = ["foreign"]
            if part == "schema": inspection["databases"]["community"]["schema_sha256"] = "0" * 64
            if part == "image": next(r for r in inspection["containers"] if r["service"] == "community-service")["image_id"] = "sha256:" + "0" * 64
            if part == "duplicate": inspection["containers"].append(copy.deepcopy(next(r for r in inspection["containers"] if r["service"] == "frontend")))
            with self.subTest(part=part), self.assertRaises(preflight.ReleaseError):
                preflight.validate_alcf_contract(plan["alcf_contract"], inspection, plan["migrations"])

    def test_real_prior_target_rows_are_bound_but_never_selected_for_apply(self):
        source, plan, baseline, *_ = alcf_fixture()
        contract = plan["alcf_contract"]
        self.assertEqual(contract["unmanaged_in_target_families"], PRIOR_TARGET_UNMANAGED)
        self.assertEqual(contract["previous_source_files"], baseline["managed_files"])
        self.assertTrue(set(r["path"] for r in PRIOR_TARGET_UNMANAGED).isdisjoint(
            r["path"] for r in activate.selected_rows(source, auth_learn_community_frontend=True)))
        preflight.validate_alcf_contract(contract, plan["inspection"], plan["migrations"])

    def test_prior_target_rows_reject_missing_changed_or_new_live_files(self):
        _, plan, *_ = alcf_fixture()
        for change in ("missing", "changed", "new"):
            inspection = copy.deepcopy(plan["inspection"])
            if change == "missing":
                inspection["code_files"].remove(PRIOR_TARGET_UNMANAGED[0])
            elif change == "changed":
                next(r for r in inspection["code_files"] if r["path"] == PRIOR_TARGET_UNMANAGED[1]["path"])["sha256"] = "0" * 64
            else:
                inspection["code_files"].append(row("frontend/unbound-extra.txt"))
            with self.subTest(change=change), self.assertRaisesRegex(preflight.ReleaseError, "LIVE_BASELINE_SOURCE_CHANGED"):
                preflight.validate_alcf_contract(plan["alcf_contract"], inspection, plan["migrations"])

    def test_prior_target_rows_require_canonical_sorted_unique_disjoint_scope(self):
        _, plan, *_ = alcf_fixture()
        contract = plan["alcf_contract"]
        invalid = [list(reversed(PRIOR_TARGET_UNMANAGED)), PRIOR_TARGET_UNMANAGED * 2,
                   [contract["previous_source_files"][0]], [row("infra/unmanaged.txt")],
                   [row("frontend/../outside.txt")], [row("frontend//extra.txt")],
                   [row("frontend/./extra.txt")], [row("frontend/extra\\file.txt")],
                   [{**PRIOR_TARGET_UNMANAGED[0], "bytes": True}],
                   [{**PRIOR_TARGET_UNMANAGED[0], "sha256": "forged"}]]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(preflight.ReleaseError):
                preflight.validate_alcf_contract({**contract, "unmanaged_in_target_families": rows})

    def test_prior_unmanaged_cannot_become_new_managed_payload(self):
        source, plan, baseline, *_ = alcf_fixture()
        with self.assertRaisesRegex(preflight.ReleaseError, "UNMANAGED_SOURCE_APPLY_FORBIDDEN"):
            preflight.make_alcf_contract(source["files"] + [PRIOR_TARGET_UNMANAGED[0]], plan["inspection"],
                plan["migrations"], baseline, plan["alcf_contract"]["baseline_receipts"])

    def test_empty_prior_target_unmanaged_remains_valid(self):
        source, plan, baseline, *_ = alcf_fixture()
        baseline["unmanaged_files"] = [r for r in baseline["unmanaged_files"] if r not in PRIOR_TARGET_UNMANAGED]
        inspection = copy.deepcopy(plan["inspection"])
        inspection["code_files"] = verify.file_rows(baseline["managed_files"] + baseline["unmanaged_files"])
        contract = preflight.make_alcf_contract(source["files"], inspection, plan["migrations"], baseline,
            plan["alcf_contract"]["baseline_receipts"])
        self.assertEqual(contract["unmanaged_in_target_families"], [])

    def test_old_migration_bytes_cannot_be_changed_or_removed(self):
        source, plan, baseline, *_ = alcf_fixture()
        for remove in (False, True):
            changed = copy.deepcopy(source["files"])
            target = plan["alcf_contract"]["retained_migration_files"][0]
            if remove: changed.remove(target)
            else: next(r for r in changed if r["path"] == target["path"])["sha256"] = "0" * 64
            with self.assertRaisesRegex(preflight.ReleaseError, "HISTORICAL_MIGRATION"):
                preflight.make_alcf_contract(changed, plan["inspection"], plan["migrations"], baseline, plan["alcf_contract"]["baseline_receipts"])

    def test_four_build_contexts_and_unchanged_default_and_alf(self):
        self.assertEqual([name for name, _ in stage.selected_contexts(auth_learn_community_frontend=True)], ["auth", "learn", "community", "frontend"])
        self.assertEqual(stage.selected_contexts(auth_learn_community_frontend=True), stage.selected_contexts())
        self.assertEqual([name for name, _ in stage.selected_contexts(auth_learn_frontend=True)], ["auth", "learn", "frontend"])
        for kwargs in ({"frontend_only": True}, {"learn_only": True}, {"auth_learn_frontend": True}):
            with self.assertRaises(stage.StageError):
                stage.selected_contexts(auth_learn_community_frontend=True, **kwargs)

    def test_quiescence_fences_all_approved_writers_and_minio_only(self):
        _, plan, *_ = alcf_fixture()
        targets = quiesce.select_targets(plan)
        self.assertEqual({r["service"] for r in targets["apis"]}, set(activate.API_ORDER))
        self.assertEqual({r["service"] for r in targets["workers"]}, WORKERS)
        self.assertEqual(WORKERS, {"personal-notification-runner", "marketplace-settlement-runner", "comment-notification-runner",
                                  "community-inquiry-runner", *verify.LEGACY_PROCESS_WORKERS})
        stopped = {r["service"] for r in quiesce.action_targets(plan, "stop")}
        self.assertEqual(stopped, TARGETS - {"frontend"} | {"minio"})
        self.assertFalse(stopped & {"postgres", "redis", "ctfd", "env-orchestrator"})
        bad = copy.deepcopy(plan)
        unknown = copy.deepcopy(next(r for r in bad["inspection"]["containers"] if r["service"] == "community-service"))
        unknown.update(service="unknown-writer", container_id="0" * 64)
        bad["inspection"]["containers"].append(unknown)
        with self.assertRaisesRegex(preflight.ReleaseError, "SAME_IMAGE"):
            quiesce.select_targets(bad)

    def test_three_database_fence_rejects_community_clients(self):
        source, plan, *_ = alcf_fixture()
        frozen = copy.deepcopy(plan["inspection"])
        ids = {r["container_id"] for r in quiesce.action_targets(plan, "stop")}
        for item in frozen["containers"]:
            if item["container_id"] in ids: item.update(running=False, pid=0, status="exited", finished_at="2026-10-18T00:00:00Z")
        for item in frozen["databases"].values(): item["client_session_count"] = 0
        claim = {"schema_version": 1, "kind": "renewal-quiescence", "task": preflight.TASK_ID, "nonce": plan["nonce"],
            "plan_sha256": "a" * 64, "source_fingerprint": source["fingerprint"], "verified_at": datetime.now(timezone.utc).isoformat(),
            "drain_verified": True, "external_writers_absent_verified": True, "fence_maintained_until_capture_complete": True,
            "inspection": frozen, **{key: plan[key] for key in preflight.ALCF_FIELDS}}
        backup.validate_quiescence(claim, plan, "a" * 64)
        self.assertEqual(plan["backup_scope"]["databases"], list(preflight.DATABASES.values()))
        self.assertEqual([r["name"] for r in backup.stream_specs(plan) if r["kind"] == "database"], [n + ".dump" for n in preflight.DATABASES.values()])
        for name in preflight.DATABASES:
            bad = copy.deepcopy(claim)
            bad["inspection"]["databases"][name]["client_session_count"] = 1
            with self.assertRaisesRegex(preflight.ReleaseError, "CLIENTS_REMAIN"):
                backup.validate_quiescence(bad, plan, "a" * 64)

    def test_four_image_backup_and_three_database_receipt_are_required(self):
        source, plan, baseline, *_ = alcf_fixture()
        ids, _ = backup_images.selection(plan)
        self.assertEqual(ids, sorted(baseline["images"].values()))
        value = {"schema_version": 1, "kind": "renewal-stream-backup", "nonce": plan["nonce"],
            "source_fingerprint": source["fingerprint"], "plan_sha256": "a" * 64, "quiescence_sha256": "b" * 64,
            "quiescence_verified": True, "release_eligible": True, "image_backup_verified": True, "ctfd_db_included": False,
            "database_count": 3, "cross_service_consistency_verified": True, "captured_databases": list(preflight.DATABASES.values()),
            "selected_image_ids": ids, "image_backup_receipt_sha256": "c" * 64, "files": [{}] * 8,
            **{key: plan[key] for key in preflight.ALCF_FIELDS}}
        binding = verify.binding_for("1" * 40, source["fingerprint"], plan["nonce"])
        activate.backup_contract(value, binding, "a" * 64, "b" * 64, plan["inspection"]["containers"], auth_learn_community_frontend=True)
        for delta in ({"database_count": 2}, {"captured_databases": ["platform_db", "learn_db"]},
                      {"cross_service_consistency_verified": False}, {"selected_image_ids": ids[:-1]}):
            with self.assertRaises(activate.ActivateError):
                activate.backup_contract({**value, **delta}, binding, "a" * 64, "b" * 64, plan["inspection"]["containers"], auth_learn_community_frontend=True)

    def test_overlay_and_steps_skip_unchanged_community_migration_only(self):
        _, plan, baseline, snapshot, *_ = alcf_fixture()
        model = {"services": {name: {} for name in (*verify.SERVICE_IMAGES, *verify.MIGRATION_IMAGES)}}
        workers = activate.derive_workers(snapshot["containers"], model["services"])
        overlay = activate.make_overlay(model, baseline["images"], baseline["binding"], workers, Path("/runtime"),
            auth_learn_community_frontend=True, migration_services=["auth", "learn"])
        self.assertEqual(set(overlay["services"]), TARGETS | {"auth-migrate", "learn-migrate"})
        steps = activate.activation_steps(workers, auth_learn_community_frontend=True, migration_services=["auth", "learn"])
        self.assertEqual([name for action, name in steps if action == "migration"], ["auth", "learn"])
        self.assertIn(("baseline", "auth"), steps)
        self.assertIn(("health", "community-service"), steps)
        self.assertEqual({name for action, name in steps if action == "health"}, TARGETS)
        self.assertIn(("migration", "community"), activate.activation_steps(workers))

    def test_build_and_activation_keep_all_new_images_and_old_chain(self):
        _, plan, baseline, _, _, build, activation, checked = alcf_receipts()
        self.assertEqual(checked["expected_heads"], HEADS)
        self.assertEqual(checked["alcf_contract"]["baseline_receipts"], plan["alcf_contract"]["baseline_receipts"])
        self.assertEqual(set(checked["changed_services"]), TARGETS | {"auth-migrate", "learn-migrate"})
        self.assertTrue(all(checked["images"][family] != image for family, image in baseline["images"].items()))
        for field in ("previous_images", "previous_source_files", "previous_heads", "unmanaged_in_target_families"):
            bad = copy.deepcopy(build)
            bad["alcf_contract"][field] = [] if field in ("previous_source_files", "unmanaged_in_target_families") else {}
            with self.assertRaises((preflight.ReleaseError, verify.VerifyError)):
                verify.build_contract(bad, build["binding"], baseline=baseline, auth_learn_community_frontend=True)
        built = verify.build_contract(build, build["binding"], baseline=baseline, auth_learn_community_frontend=True)
        for executed in ([], ["learn", "auth"], ["auth", "learn", "community"]):
            with self.assertRaisesRegex(verify.VerifyError, "MIGRATIONS_INCOMPLETE"):
                verify.activation_contract({**activation, "executed_migrations": executed}, build["binding"], activation["managed_files"], built,
                    baseline=baseline, auth_learn_community_frontend=True)

    def test_forged_prior_unmanaged_is_rejected_against_receipt_baseline(self):
        _, _, baseline, _, _, build, activation, _ = alcf_receipts()
        forged = copy.deepcopy(build)
        forged["alcf_contract"]["unmanaged_in_target_families"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(verify.VerifyError, "ALCF_BASELINE_CHANGED"):
            verify.build_contract(forged, build["binding"], baseline=baseline, auth_learn_community_frontend=True)
        built = verify.build_contract(build, build["binding"], baseline=baseline, auth_learn_community_frontend=True)
        for change in ("missing", "changed", "new"):
            altered = copy.deepcopy(activation)
            if change == "missing":
                altered["unmanaged_files"].remove(PRIOR_TARGET_UNMANAGED[0])
            elif change == "changed":
                altered["unmanaged_files"] = [{**r, "sha256": "0" * 64} if r["path"] == PRIOR_TARGET_UNMANAGED[0]["path"] else r
                                             for r in altered["unmanaged_files"]]
            else:
                altered["unmanaged_files"].append(row("frontend/unbound-extra.txt"))
            with self.subTest(change=change), self.assertRaisesRegex(verify.VerifyError, "ALCF_UNMANAGED_FILES_CHANGED"):
                verify.activation_contract(altered, build["binding"], activation["managed_files"], built,
                    baseline=baseline, auth_learn_community_frontend=True)

    def test_independent_verify_preserves_strict_health_and_schema(self):
        _, plan, _, snapshot, triggers, _, _, request = alcf_receipts()
        snapshot = copy.deepcopy(snapshot)
        snapshot["code_files"] = verify.file_rows(request["managed_files"] + request["unmanaged_files"])
        labels = activate.source_labels(request["binding"])
        for item in snapshot["containers"]:
            if item["service"] in TARGETS:
                item["image_id"] = request["images"][verify.SERVICE_IMAGES[item["service"]]]
                item["source_labels"] = dict(labels)
        image_labels = {image: dict(labels) for image in request["images"].values()}
        image_labels[request["images"]["frontend"]][verify.FRONTEND_ARGS_LABEL] = request["frontend_args_sha256"]
        states = [{"community_schema_sha256": plan["alcf_contract"]["community_schema_sha256"],
                   "reason_nullable": True, "team_schema": True, "attachment_schema": True}] * 2
        def evaluate(*, before=snapshot, schema=states):
            return verify.evaluate_snapshot(request, before, before, image_labels,
                {name: [head] for name, head in HEADS.items()}, triggers, categories=category_projection(), alcf_states=schema)
        result = evaluate()
        self.assertEqual(result["status"], "passed")
        verify.validate_result(result, request["binding"], expected_heads=HEADS, auth_learn_community_frontend=True, alcf_contract=plan["alcf_contract"])
        for key in states[0]:
            altered = copy.deepcopy(states)
            altered[1][key] = "0" * 64 if key == "community_schema_sha256" else False
            self.assertEqual(evaluate(schema=altered)["checks"]["alcf_schema_preservation"]["status"], "failed")
        for change in ("missing", "changed", "new"):
            altered = copy.deepcopy(snapshot)
            if change == "missing":
                altered["code_files"].remove(PRIOR_TARGET_UNMANAGED[0])
            elif change == "changed":
                next(r for r in altered["code_files"] if r["path"] == PRIOR_TARGET_UNMANAGED[0]["path"])["sha256"] = "0" * 64
            else:
                altered["code_files"].append(row("frontend/unbound-extra.txt"))
            with self.subTest(unmanaged=change):
                self.assertEqual(evaluate(before=altered)["checks"]["unmanaged_files"]["status"], "failed")
        bad = copy.deepcopy(snapshot)
        next(r for r in bad["containers"] if r["service"] == "community-service")["health"] = "unhealthy"
        self.assertEqual(evaluate(before=bad)["checks"]["target_health"]["status"], "failed")
        del result["checks"]["event_category_registry"]
        with self.assertRaisesRegex(verify.VerifyError, "CATEGORY_CHECK_MISSING"):
            verify.validate_result(result, request["binding"], expected_heads=HEADS, auth_learn_community_frontend=True, alcf_contract=plan["alcf_contract"])

    def test_actual_remote_programs_compile_and_contract_dependencies_exist(self):
        for module in (stage, activate, verify):
            compile(module.remote_program(), "<pure-remote-compile>", "exec")
        namespace = {"REQUEST": {}}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<pure-remote-definitions>", "exec"), namespace)
        namespace["validate_alcf_contract"](alcf_fixture()[1]["alcf_contract"])

    def test_actual_snapshot_label_planes_match_and_reject_untargeted_drift(self):
        _, _, _, snapshot, triggers, _, _, request = alcf_receipts()
        current = copy.deepcopy(snapshot["containers"])
        for name in ("env-orchestrator", "redis", "ctfd"):
            current.append({"service": name, "container_id": digest(name), "image_id": "sha256:" + digest(name + "-image"),
                            "running": True, "pid": 777, "started_at": "2026-10-18T00:00:00Z", "restart_count": 0,
                            "finished_at": "0001-01-01T00:00:00Z", "mounts": []})
        for item in current:
            item.pop("source_labels", None)
        labels = {item["container_id"]: activate.source_labels(request["binding"]) for item in current}
        env_id = next(item["container_id"] for item in current if item["service"] == "env-orchestrator")
        labels[env_id] = activate.source_labels(verify.binding_for("e" * 40, "f" * 64, "retained-env-release"))
        inspected = []
        def run(argv):
            self.assertEqual(argv[:2], ["docker", "inspect"])
            ids = argv[2:]
            inspected.extend(ids)
            return json.dumps([{"Id": identifier, "Config": {"Labels": labels[identifier],
                "Cmd": next(item.get("command") for item in current if item["container_id"] == identifier)}} for identifier in ids]).encode()
        common = {"json": json, "SERVICE_IMAGES": verify.SERVICE_IMAGES, "LEGACY_PROCESS_WORKERS": verify.LEGACY_PROCESS_WORKERS,
            "PERSONAL_WORKER": activate.PERSONAL_WORKER, "source_labels": activate.source_labels,
            "compose": lambda: {}, "protected_rows": lambda _: snapshot["protected_files"],
            "containers": lambda: copy.deepcopy(current), "code_rows": lambda _: snapshot["code_files"], "run": run}
        def actual_function(text, name, extra=None):
            node = next(item for item in ast.parse(text).body if isinstance(item, ast.FunctionDef) and item.name == name)
            namespace = {**common, **(extra or {})}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<actual-remote-snapshot>", "exec"), namespace)
            return namespace[name]
        activation_snapshot = actual_function(activate.REMOTE, "safe_snapshot", {"REQUEST": request})
        verify_snapshot = actual_function(verify.REMOTE, "verify_snapshot")
        activation_before, activation_after = activation_snapshot(), activation_snapshot()
        activation_plane = preflight.code_runtime(activation_before, TARGETS)
        self.assertEqual(set(inspected), {item["container_id"] for item in current})
        self.assertEqual(activation_plane, preflight.code_runtime(activation_after, TARGETS))
        independent = verify_snapshot()
        verified_plane = preflight.code_runtime(independent, TARGETS)
        self.assertEqual(activation_plane, verified_plane)
        request["untargeted_runtime"] = activation_plane
        def evaluate(view):
            return verify.evaluate_snapshot(request, view, view, {}, {name: [head] for name, head in HEADS.items()}, triggers,
                                            categories=category_projection(), alcf_states=[])["checks"]["alcf_untargeted_runtime"]["status"]
        self.assertEqual(evaluate(independent), "passed")
        labels[env_id]["io.ctf-web.release-nonce"] = "changed-env-release"
        drifted = verify_snapshot()
        self.assertNotEqual(activation_plane, preflight.code_runtime(drifted, TARGETS))
        self.assertEqual(evaluate(drifted), "failed")
        for phase in (None, preflight.ALF_PHASE):
            inspected.clear()
            actual_function(activate.REMOTE, "safe_snapshot", {"REQUEST": {"binding": request["binding"], "release_phase": phase}})()
            self.assertEqual(set(inspected), {item["container_id"] for item in current if item["service"] in verify.SERVICE_IMAGES})

    def test_five_triples_are_preserved_when_the_new_phase_extends_the_chain(self):
        triples, inventory = baseline_chain(full_learn_head=verify.LEARN_CATEGORY_HEAD,
            families=(None, "frontend", "frontend", "frontend", "frontend"))
        history = [row("services/" + name + "/migrations/versions/" + head + "_old.py")
                   for name, head in {**verify.EXPECTED_HEADS, "learn": verify.LEARN_CATEGORY_HEAD}.items()]
        hashes = []
        for index, triple in enumerate(triples):
            activation, build, checked = [copy.deepcopy(inventory[p][0]) for p in triple]
            field = "managed_files" if index == 0 else "unmanaged_files"
            activation[field] = verify.file_rows(activation[field] + history)
            if index:
                activation["baseline_receipts"] = copy.deepcopy(hashes)
                checked["baseline_receipts"] = copy.deepcopy(hashes)
            build_sha, activation_sha = verify.sha(verify.canonical(build)), verify.sha(verify.canonical(activation))
            checked.update(build_receipt_sha256=build_sha, activation_receipt_sha256=activation_sha)
            checked_sha = verify.sha(verify.canonical(checked))
            for p, value, fingerprint in zip(triple, (activation, build, checked), (activation_sha, build_sha, checked_sha)):
                inventory[p] = (value, fingerprint)
            hashes.append({"activation": activation_sha, "build": build_sha, "verify": checked_sha})
        with patch.object(verify, "load_receipt", side_effect=lambda p: copy.deepcopy(inventory[str(p)])):
            chain = verify.load_baseline_chain(triples)
            baseline = chain["contract"]
            _, plan, _, snapshot, _ = alcf_fixture()
            contract = copy.deepcopy(plan["alcf_contract"])
            contract.update(previous_source_files=baseline["managed_files"], retained_migration_files=history,
                            previous_images=baseline["images"], baseline_receipts=hashes,
                            unmanaged_in_target_families=[r for r in baseline["unmanaged_files"] if r["path"].startswith(verify.CONTEXTS)])
            contract["retained_migration_files"] = verify.file_rows(history)
            preflight.validate_alcf_contract(contract)
            fields = dict(zip(preflight.ALCF_FIELDS, (True, preflight.ALCF_PHASE, contract)))
            binding = verify.binding_for("7" * 40, "8" * 64, "new-four-context-chain")
            build, activation = receipts({**baseline, "binding": binding}, snapshot)
            build.update(fields)
            build["remote"].update(fields, retained_baseline=verify.baseline_summary(baseline, tuple(HEADS) + ("frontend",)))
            activation.update(fields, frontend_only=False, learn_only=False, baseline_receipts=hashes,
                managed_files=baseline["managed_files"], unmanaged_files=baseline["unmanaged_files"],
                executed_migrations=["auth", "learn"], schema={"heads": {name: [head] for name, head in HEADS.items()}},
                untargeted_runtime_before=preflight.code_runtime(snapshot, TARGETS), untargeted_runtime_after=preflight.code_runtime(snapshot, TARGETS))
            build_sha, activation_sha = verify.sha(verify.canonical(build)), verify.sha(verify.canonical(activation))
            checked = copy.deepcopy(inventory[triples[-1][2]][0])
            checked.update(fields, binding=binding, frontend_only=False, learn_only=False, baseline_receipts=hashes,
                           build_receipt_sha256=build_sha, activation_receipt_sha256=activation_sha)
            checked["checks"].update(alcf_untargeted_runtime={"status": "passed", "count": 1}, alcf_schema_preservation={"status": "passed", "count": 2})
            final = ("new-activation", "new-build", "new-verify")
            for p, value in zip(final, (activation, build, checked)):
                inventory[p] = (value, verify.sha(verify.canonical(value)))
            result = verify.load_baseline_chain(triples + [final])
            self.assertEqual(result["hashes"][:5], hashes)
            self.assertEqual(result["contract"]["expected_heads"], HEADS)
            bad = inventory[final[0]][0]
            bad["alcf_contract"]["baseline_receipts"] = hashes[:-1]
            with self.assertRaisesRegex(verify.VerifyError, "BASELINE_CHAIN_CHANGED"):
                verify.load_baseline_chain(triples + [final])

    def test_remote_schema_checks_team_attachment_and_nullable_reason(self):
        _, plan, *_ = alcf_fixture()
        request = {**{key: plan[key] for key in preflight.ALCF_FIELDS}, "expected_heads": HEADS}
        namespace = {"REQUEST": request}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<schema-pure>", "exec"), namespace)
        failing = set()
        def sql(database, query):
            if "alembic_version" in query:
                return [HEADS[next(name for name, db in preflight.DATABASES.items() if database == db)]]
            if query in (activate.ALCF_TEAM_SCHEMA_SQL, activate.ALCF_ATTACHMENT_SCHEMA_SQL):
                return query not in failing
            if "table_name='submission_flag_reveal_audits'" in query:
                return [] if "reason" in failing else [{"type": "text", "nullable": "YES"}]
            if "information_schema.tables" in query:
                return list(activate.SCHEMA_TABLES[next(name for name, db in preflight.DATABASES.items() if database == db)]) + ["platform_event_categories", "platform_event_category_audit_events"]
            if "information_schema.columns" in query:
                return list(activate.SCHEMA_COLUMNS[next(name for name, db in preflight.DATABASES.items() if database == db)])
            if "knowledge_graph_revision_write" in query:
                return [{"table": table, "enabled": "O", "function": "bump_knowledge_graph_revision", "type": 28} for table in verify.TRIGGER_TABLES]
            raise AssertionError("UNMOCKED_SQL")
        namespace.update(sql=sql, containers=lambda: [{"service": "postgres", "container_id": "a" * 64, "running": True}],
            database_rows=lambda _: {"community": {"schema_sha256": plan["alcf_contract"]["community_schema_sha256"]}})
        self.assertTrue(namespace["schema_contract"]()["required_schema_verified"])
        for query in (activate.ALCF_TEAM_SCHEMA_SQL, activate.ALCF_ATTACHMENT_SCHEMA_SQL, "reason"):
            failing.add(query)
            with self.assertRaises(namespace["ActivateError"]):
                namespace["schema_contract"]()
            failing.clear()

    def test_full_mock_activation_updates_all_apis_workers_without_community_job(self):
        result, request, commands, current, original, written = mock_activation()
        self.assertEqual(result["status"], "activated")
        self.assertEqual(result["executed_migrations"], ["auth", "learn"])
        self.assertTrue(result["notification_baseline_verified"])
        self.assertEqual([argv[-1] for argv in commands if "up" in argv], [name for action, name in activate.activation_steps(sorted(WORKERS), auth_learn_community_frontend=True, migration_services=["auth", "learn"]) if action == "up"])
        self.assertFalse(any("community-migrate" in argv for argv in commands))
        self.assertEqual(preflight.code_runtime({"containers": current}, TARGETS), preflight.code_runtime({"containers": original}, TARGETS))
        self.assertEqual(written[0][1]["alcf_contract"], request["alcf_contract"])
        self.assertEqual([r for r in result["unmanaged_files"] if r["path"].startswith(verify.CONTEXTS)], PRIOR_TARGET_UNMANAGED)

    def test_remote_activation_rejects_prior_target_drift_before_apply(self):
        for change in ("missing", "changed", "new", "forged"):
            result, _, commands, _, _, written = mock_activation(unmanaged_change=change)
            with self.subTest(change=change):
                self.assertEqual(result["status"], "partial-unverified")
                self.assertEqual(commands, [])
                self.assertFalse(any("activation.complete" in filename for filename, _ in written))

    def test_unknown_up_is_inspected_and_fenced_never_retried_or_restored(self):
        result, _, commands, *_ = mock_activation(fail_up=True, stop_unknown=True)
        self.assertEqual(result["status"], "partial-unverified")
        self.assertTrue(result["recovery_outcome_unverified"])
        self.assertFalse(result["database_restore_performed"])
        self.assertFalse(result["old_migration_jobs_executed"])
        self.assertEqual(sum("up" in argv for argv in commands), 1)
        self.assertEqual(sum(argv[:2] == ["docker", "stop"] for argv in commands), 1)
        self.assertTrue(any(argv[:4] == ["docker", "stop", "--time", "-1"] for argv in commands))

    def test_failed_migration_runs_once_and_never_starts_old_writers(self):
        result, _, commands, *_ = mock_activation(fail_migration=True)
        self.assertEqual(result["status"], "partial-unverified")
        self.assertEqual(result["executed_migrations"], [])
        self.assertEqual(sum("run" in argv for argv in commands), 1)
        self.assertFalse(any("up" in argv for argv in commands))
        self.assertTrue(result["writer_fence_maintained"])

    def test_frontend_health_failure_after_new_heads_cannot_restart_old_029_image(self):
        result, request, commands, current, original, written = mock_activation(fail_health="frontend")
        self.assertEqual(request["alcf_contract"]["previous_heads"]["learn"], "20261013_029")
        self.assertEqual(result["schema"]["heads"]["learn"], ["20261018_031"])
        self.assertEqual(result["status"], "partial-unverified")
        self.assertTrue(result["writer_fence_maintained"])
        self.assertFalse(result.get("frontend_image_rolled_back", False))
        self.assertFalse(result["database_restore_performed"])
        self.assertFalse(any("rollback" in filename for filename, _ in written))
        self.assertEqual(sum(argv[-1] == "frontend" for argv in commands if "up" in argv), 1)
        self.assertEqual(preflight.code_runtime({"containers": current}, TARGETS), preflight.code_runtime({"containers": original}, TARGETS))
        self.assertFalse(any(item["running"] for item in current if item["service"] in TARGETS - {"frontend"}))

    def test_untargeted_restart_cannot_pass(self):
        result, *_ = mock_activation(drift=True)
        self.assertEqual(result["status"], "partial-unverified")
        self.assertIn("UNTARGETED", result["error"])


if __name__ == "__main__":
    unittest.main()
