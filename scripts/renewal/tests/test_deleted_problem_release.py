"""Pure, project-local tests. Never contacts SSH, Docker or production."""
from __future__ import annotations

import ast
import copy
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deleted_problem_release as RELEASE

BINDING = {"commit": "a" * 40, "source_fingerprint": "b" * 64, "release_nonce": "11111111-2222-4333-8444-555555555555"}
ARGS = {"NEXT_PUBLIC_SITE_URL": "https://example.invalid/ctf", "NEXT_PUBLIC_ENV_PROXY_BASE": "", "NEXT_PUBLIC_ENV_ACCESS_MODE": "proxy"}
TMP = RELEASE.ops.TASK / "checks" / "deleted-problem-release-tests"


def row(service, index, family=None, project="ctf_web"):
    image = {"learn": "a", "env": "b", "frontend": "c"}.get(family, "d")
    return {"container_id": f"{index:064x}", "image_id": "sha256:" + image * 64, "project": project, "service": service,
            "running": True, "pid": 1000 + index, "status": "running", "health": None if service in RELEASE.WORKERS else "healthy",
            "started_at": "fixture-start", "finished_at": "fixture-finish", "restart_count": 0, "runtime_sha256": "e" * 64,
            "labels_sha256": "f" * 64, "ports": {"8100/tcp": [{"HostPort": "8100"}]}, "mounts": []}


def snapshot():
    rows = [row(service, index + 1, family) for index, (service, family) in enumerate(RELEASE.FAMILIES.items())]
    rows.extend([row("minio", 20), row("someone-else", 21, project="other-project"), row("postgres", 22)])
    return {"containers": rows, "code_files": [{"path": "services/auth/app/main.py", "bytes": 1, "sha256": "1" * 64}, {"path": "services/learn/app/main.py", "bytes": 2, "sha256": "2" * 64}],
            "protected_files": [{"path": "protected-opaque", "bytes": 10, "sha256": "3" * 64}],
            "databases": {name: {"head": [head], "schema_sha256": "4" * 64, "database": name} for name, head in RELEASE.HEADS.items()}, "frontend_args": copy.deepcopy(ARGS)}


def remote_namespace():
    program = ast.parse(RELEASE.remote_program("inspect", {"scope": RELEASE.SCOPE}))
    assert isinstance(program.body[-1], ast.Expr)
    program.body.pop()
    namespace = {}
    # The injected target is Linux. Only its file-lock import is substituted for
    # pure Windows tests; no command or target behavior is simulated as live.
    with patch.dict(sys.modules, {"fcntl": ModuleType("fcntl")}):
        exec(compile(program, "<isolated-release>", "exec"), namespace)
    return namespace


def dump_catalog_fixture(tables, *, include_data=True):
    lines = ["; Archive created at 2026-10-23 00:00:00 UTC", ";     Format: CUSTOM", "; Selected TOC Entries:", ";"]
    for index, name in enumerate(tables, 1):
        lines.append(f"{index}; 1259 {16000 + index} TABLE public {name} platform")
        if include_data:
            lines.append(f"{index + 1000}; 0 {16000 + index} TABLE DATA public {name} platform")
    return ("\n".join(lines) + "\n").encode()


class ReleaseContracts(unittest.TestCase):
    def test_scope_exact_seven_consumers_and_three_contexts(self):
        self.assertEqual(len(RELEASE.TARGETS), 7)
        self.assertEqual([family for family, _ in RELEASE.CONTEXTS], ["learn", "env", "frontend"])
        self.assertEqual(RELEASE.ACTIVATION_GROUPS[0], ("env-orchestrator",))
        self.assertEqual(RELEASE.ACTIVATION_GROUPS[-1], ("frontend",))
        self.assertEqual(RELEASE.SCOPE["migrations"], [])
        self.assertEqual(RELEASE.SCOPE["backup_only_services"], ["minio"])
        bad = copy.deepcopy(RELEASE.SCOPE)
        bad["services"].append("auth-service")
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "EXACT"):
            RELEASE.validate_scope(bad)

    def test_binding_requires_full_hash_and_fresh_uuid4(self):
        RELEASE.validate_binding(BINDING)
        for key, value in (("commit", "a" * 7), ("source_fingerprint", "b" * 63), ("release_nonce", "not-a-uuid"), ("release_nonce", "11111111-2222-1333-8444-555555555555")):
            with self.subTest(key=key, value=value), self.assertRaises(Exception):
                RELEASE.validate_binding(BINDING | {key: value})

    def test_source_paths_preserve_original_helper_scope(self):
        RELEASE.context_name("services/env-orchestrator/cmd/main.go")
        RELEASE.context_name("services/learn/app/main.py")
        RELEASE.context_name("frontend/src/app/page.tsx")
        for path in ("services/auth/app/main.py", "infra/.env", "frontend/../services/env-orchestrator/x", "services/env-orchestrator/.env", "services/env-orchestrator/data/state", "frontend/node_modules/x"):
            with self.subTest(path=path), self.assertRaises(Exception):
                RELEASE.context_name(path)
        with self.assertRaises(Exception):
            RELEASE.stage.safe_source_path("services/env-orchestrator/cmd/main.go")

    def test_unknown_image_consumer_is_not_controlled(self):
        current = snapshot()["containers"]
        RELEASE.selected_consumers(current)
        outsider = row("other", 99, "learn", "someone-else")
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "UNKNOWN_SHARED_IMAGE_CONSUMER"):
            RELEASE.selected_consumers(current + [outsider])

    def test_shared_learn_workers_require_one_image(self):
        current = snapshot()["containers"]
        current[2]["image_id"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "FAMILY_IMAGE_MISMATCH"):
            RELEASE.selected_consumers(current)

    def test_duplicate_or_missing_target_fails(self):
        current = snapshot()["containers"]
        for rows in (current[1:], current + [copy.deepcopy(current[0])]):
            with self.assertRaises(RELEASE.DeletedProblemReleaseError):
                RELEASE.selected_consumers(rows)

    def test_other_project_and_existing_labs_preserved_exactly(self):
        before = snapshot()
        RELEASE.preserved_snapshot(before, copy.deepcopy(before))
        for field, value in (("running", False), ("image_id", "sha256:" + "0" * 64), ("pid", 0), ("restart_count", 1), ("mounts", ["changed"])):
            after = copy.deepcopy(before)
            after["containers"][8][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "NON_TARGET_CONTAINER_CHANGED"):
                RELEASE.preserved_snapshot(before, after, changed=RELEASE.TARGETS)

    def test_non_target_addition_or_deletion_fails(self):
        before = snapshot()
        for rows in (before["containers"][:-1], before["containers"] + [row("unrelated", 100)]):
            with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "NON_TARGET_CONTAINER_SET_CHANGED"):
                RELEASE.preserved_snapshot(before, before | {"containers": rows}, changed=RELEASE.TARGETS)

    def test_exact_stop_fence_allows_only_process_transition(self):
        before, after = snapshot(), snapshot()
        for value in after["containers"][:7]:
            value.update({"running": False, "pid": 0, "status": "exited", "health": None, "finished_at": "stopped", "ports": {}})
        RELEASE.preserved_snapshot(before, after, stopped=True)
        after["containers"][0]["image_id"] = "sha256:" + "0" * 64
        with self.assertRaises(RELEASE.DeletedProblemReleaseError):
            RELEASE.preserved_snapshot(before, after, stopped=True)

    def test_minio_pause_resume_keeps_identity_image_mounts(self):
        before, after = snapshot(), snapshot()
        after["containers"][7].update({"pid": 7000, "started_at": "resumed", "finished_at": "stopped"})
        RELEASE.preserved_snapshot(before, after, minio_resumed=True)
        after["containers"][7]["mounts"] = ["changed"]
        with self.assertRaises(RELEASE.DeletedProblemReleaseError):
            RELEASE.preserved_snapshot(before, after, minio_resumed=True)

    def test_changed_targets_still_preserve_runtime(self):
        before, after = snapshot(), snapshot()
        for value in after["containers"][:7]:
            value["container_id"] = "f" + value["container_id"][1:]
            value["image_id"] = "sha256:" + {"env-orchestrator": "1", "frontend": "2"}.get(value["service"], "3") * 64
        RELEASE.preserved_snapshot(before, after, changed=RELEASE.TARGETS)
        after["containers"][0]["runtime_sha256"] = "0" * 64
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "TARGET_RUNTIME_CHANGED"):
            RELEASE.preserved_snapshot(before, after, changed=RELEASE.TARGETS)

    def test_protected_schema_and_unmanaged_source_preserved(self):
        before = snapshot()
        for key in ("protected_files", "databases", "code_files"):
            after = copy.deepcopy(before)
            after[key] = [] if key != "databases" else {}
            with self.subTest(key=key), self.assertRaises(RELEASE.DeletedProblemReleaseError):
                RELEASE.preserved_snapshot(before, after, changed=RELEASE.TARGETS)

    def test_build_commands_are_serial_and_bound_with_existing_args(self):
        commands = RELEASE.build_commands(Path("/private/source"), BINDING, ARGS)
        self.assertEqual([family for family, _ in commands], ["learn", "env", "frontend"])
        for family, command in commands:
            self.assertEqual(command[:2], ["docker", "build"])
            self.assertIn(BINDING["commit"] + "-" + BINDING["release_nonce"], command[command.index("--tag") + 1])
            self.assertIn("io.ctf-web.source-fingerprint=" + BINDING["source_fingerprint"], command)
            self.assertNotIn("--force-rm", command)
        self.assertEqual(commands[-1][1][-1], "/private/source/frontend")
        self.assertIn("production", commands[-1][1])
        self.assertIn("NEXT_PUBLIC_SITE_URL=" + ARGS["NEXT_PUBLIC_SITE_URL"], commands[-1][1])

    def test_remote_compiles_and_uses_same_new_scope_not_old_029(self):
        namespace = remote_namespace()
        self.assertEqual(namespace["HEADS"], RELEASE.HEADS)
        self.assertEqual(namespace["build_commands"](Path("/private/source"), BINDING, ARGS), RELEASE.build_commands(Path("/private/source"), BINDING, ARGS))
        namespace["_STAGE"]["safe_source_path"]("services/env-orchestrator/cmd/main.go")
        self.assertEqual(RELEASE.stage.CONTEXTS[0][0], "auth")

    def test_intent_cannot_be_replayed_and_preserves_original(self):
        namespace = remote_namespace()
        TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            root = Path(directory)
            namespace["REMOTE_ROOT"] = str(root)
            namespace["REQUEST"] = {"plan": {"binding": BINDING}, "plan_sha256": "e" * 64}
            namespace["intent"](root, "activate-env", {"service": "env-orchestrator"})
            original = (root / "activate-env.intent.json").read_bytes()
            with self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "INTENT_EXISTS"):
                namespace["intent"](root, "activate-env", {"different": True})
            self.assertEqual((root / "activate-env.intent.json").read_bytes(), original)

    def test_container_control_rejects_foreign_project_before_command(self):
        namespace = remote_namespace()
        execute = Mock()
        namespace["bounded"] = execute
        foreign = row("env-orchestrator", 100, "env", project="someone-else")
        with self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "CONTROL_SCOPE"):
            namespace["control_container"](foreign["container_id"], foreign, "stop")
        execute.assert_not_called()

    def test_readonly_snapshot_remains_available_for_partial_learn_rollout(self):
        namespace = remote_namespace()
        current = snapshot()["containers"]
        current[2]["image_id"] = "sha256:" + "0" * 64
        namespace["compose"] = lambda: {"services": {"frontend": {"build": {"args": ARGS}}}}
        namespace["protected_rows"] = lambda model: [{"path": "opaque-" + str(index)} for index in range(6)]
        namespace["daemon_rows"] = lambda: current
        namespace["details"] = lambda identifier: {"Config": {"Env": [key + "=" + value for key, value in ARGS.items()]}}
        namespace["database_snapshot"] = lambda identifier: snapshot()["databases"]
        namespace["code_rows"] = lambda protected: []
        self.assertEqual(namespace["snapshot"]()["containers"], current)

    def test_status_returns_current_daemon_even_when_full_snapshot_fails(self):
        namespace = remote_namespace()
        namespace["snapshot"] = Mock(side_effect=namespace["DeletedProblemReleaseError"]("FRONTEND_IDENTITY_AMBIGUOUS"))
        namespace["daemon_rows"] = lambda: [{"container_id": "f" * 64, "service": "unrelated"}]
        namespace["owned_test_status"] = lambda nonce: []
        TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            result = namespace["status"](Path(directory), {"binding": BINDING, "inspection": snapshot()})
        self.assertFalse(result["inspection_complete"])
        self.assertEqual(result["snapshot_error"], "FRONTEND_IDENTITY_AMBIGUOUS")
        self.assertEqual(result["snapshot"]["containers"][0]["service"], "unrelated")
        self.assertFalse(result["replay_or_restore_performed"])

    def test_minio_stop_reply_lost_after_actual_stop_resumes_exact_container(self):
        namespace = remote_namespace()
        before = snapshot()
        minio = before["containers"][7]
        stopped = copy.deepcopy(before)
        for value in stopped["containers"][:7]:
            value.update({"running": False, "pid": 0, "status": "exited", "health": None, "finished_at": "stopped", "ports": {}})
        namespace["completed"] = lambda directory, action: {"status": "passed"}
        namespace["snapshot"] = Mock(side_effect=[before, stopped])
        namespace["run"] = lambda command, limit=120: b"10 /data\n" if "du" in command else b"1024\n"
        namespace["private_json"] = Mock()
        namespace["intent"] = Mock()
        namespace["runtime_hash"] = lambda value: value["runtime_sha256"]
        stopped_minio = {"Id": minio["container_id"], "Image": minio["image_id"], "runtime_sha256": minio["runtime_sha256"],
                         "Config": {"Labels": {"com.docker.compose.project": "ctf_web", "com.docker.compose.service": "minio"}},
                         "State": {"Running": False, "Pid": 0, "Status": "exited"}}
        namespace["details"] = lambda identifier: copy.deepcopy(stopped_minio) if identifier == minio["container_id"] else {"Id": identifier}
        controlled = []

        def control(identifier, original, action):
            controlled.append((identifier, original["service"], action))
            if original["service"] == "minio" and action == "stop":
                raise RuntimeError("stop reply lost after actual stop")

        namespace["control_container"] = control
        namespace["capture_command"] = Mock()
        with patch.object(namespace["os"], "statvfs", return_value=SimpleNamespace(f_bavail=10 ** 12, f_frsize=1), create=True):
            with self.assertRaisesRegex(RuntimeError, "stop reply lost"):
                namespace["fence_backup"](TMP, {"binding": BINDING, "inspection": before, "managed_files": [before["code_files"][1]]})
        self.assertEqual(controlled[-2:], [(minio["container_id"], "minio", "stop"), (minio["container_id"], "minio", "start")])
        self.assertEqual(len([entry for entry in controlled if entry[2] == "start"]), 1)
        self.assertEqual(namespace["intent"].call_args_list[-1].args[1], "minio-start")
        namespace["capture_command"].assert_not_called()

    def test_minio_resume_rejects_identity_or_unconfirmed_stop_without_control(self):
        original = snapshot()["containers"][7]
        baseline = {"Id": original["container_id"], "Image": original["image_id"], "runtime_sha256": original["runtime_sha256"],
                    "Config": {"Labels": {"com.docker.compose.project": "ctf_web", "com.docker.compose.service": "minio"}},
                    "State": {"Running": False, "Pid": 0, "Status": "exited"}}
        for changed in ({"Id": "0" * 64}, {"Image": "sha256:" + "0" * 64}, {"runtime_sha256": "0" * 64},
                        {"State": {"Running": False, "Pid": 12, "Status": "exited"}}, {"State": {"Running": False, "Pid": 0, "Status": "dead"}},
                        {"Config": {"Labels": {"com.docker.compose.project": "someone-else", "com.docker.compose.service": "minio"}}}):
            with self.subTest(changed=changed):
                namespace = remote_namespace()
                namespace["details"] = lambda identifier: copy.deepcopy(baseline | changed)
                namespace["runtime_hash"] = lambda value: value["runtime_sha256"]
                namespace["control_container"] = Mock()
                namespace["intent"] = Mock()
                with self.assertRaises(namespace["DeletedProblemReleaseError"]):
                    namespace["resume_backup_minio"](TMP, original)
                namespace["control_container"].assert_not_called()
                namespace["intent"].assert_not_called()

    def test_minio_already_running_has_no_resume_action(self):
        namespace = remote_namespace()
        original = snapshot()["containers"][7]
        namespace["details"] = lambda identifier: {"Id": original["container_id"], "Image": original["image_id"], "runtime_sha256": original["runtime_sha256"],
            "Config": {"Labels": {"com.docker.compose.project": "ctf_web", "com.docker.compose.service": "minio"}}, "State": {"Running": True, "Pid": 1, "Status": "running"}}
        namespace["runtime_hash"] = lambda value: value["runtime_sha256"]
        namespace["control_container"] = Mock()
        namespace["intent"] = Mock()
        result = namespace["resume_backup_minio"](TMP, original)
        self.assertEqual(result["action"], "none")
        namespace["control_container"].assert_not_called()
        namespace["intent"].assert_not_called()

    def test_release_heads_match_canonical_migration_graph_without_migrating(self):
        actual = RELEASE.release_preflight.source_migrations(RELEASE.ops.SOURCE)
        expected = {"auth": "20261018_006", "learn": "20261020_032", "community": "20261013_005"}
        self.assertEqual({name: value["head"] for name, value in actual.items()}, expected)
        self.assertEqual(RELEASE.HEADS, expected)
        self.assertEqual(remote_namespace()["HEADS"], expected)
        self.assertEqual(RELEASE.SCOPE["migrations"], [])

    def test_real_remote_inspection_main_is_consumable_by_client_plan(self):
        namespace = remote_namespace()
        observed_snapshot = snapshot()
        namespace["snapshot"] = lambda: copy.deepcopy(observed_snapshot)
        with redirect_stdout(io.StringIO()) as remote_output:
            namespace["main"]()
        observed = json.loads(remote_output.getvalue())
        self.assertEqual(observed["kind"], "deleted-problem-inspection")
        self.assertEqual(observed["status"], "passed")
        rows = [{"path": context + "/Dockerfile", "bytes": 1, "sha256": "1" * 64} for _, context in RELEASE.CONTEXTS]
        modes = {value["path"]: 0o644 for value in rows}
        inspection = RELEASE.ops.TASK / "evidence" / "fixture-inspection.json"
        plan_path = RELEASE.ops.TASK / "evidence" / "fixture-plan.json"
        with patch.object(RELEASE, "load_json", return_value=(observed, "2" * 64)), \
             patch.object(RELEASE.ops, "manifest", return_value={"fingerprint": BINDING["source_fingerprint"], "files": rows}), \
             patch.object(RELEASE.stage, "bind_checkout", return_value=modes), \
             patch.object(RELEASE, "selected_rows", return_value=rows), \
             patch.object(RELEASE, "make_archive", return_value=b"fixture"), \
             patch.object(RELEASE, "validate_plan") as validate, \
             patch.object(RELEASE, "receipt", return_value=plan_path) as write_receipt, \
             patch.object(RELEASE.ops, "remote_python") as remote, redirect_stdout(io.StringIO()):
            result = RELEASE.main(["plan", "--inspection", str(inspection), "--commit", BINDING["commit"], "--nonce", BINDING["release_nonce"]])
        self.assertEqual(result, 0)
        self.assertEqual(write_receipt.call_args.args[1]["inspection"], observed_snapshot)
        validate.assert_called_once()
        remote.assert_not_called()

    def test_failed_remote_inspection_uses_same_kind_but_client_rejects_it(self):
        namespace = remote_namespace()
        namespace["snapshot"] = Mock(side_effect=namespace["DeletedProblemReleaseError"]("SCHEMA_HEAD_MISMATCH_NO_MIGRATION_ALLOWED"))
        namespace["release_directory"] = Mock()
        namespace["intent"] = Mock()
        with redirect_stdout(io.StringIO()) as remote_output:
            namespace["main"]()
        observed = json.loads(remote_output.getvalue())
        self.assertEqual(observed["kind"], "deleted-problem-inspection")
        self.assertEqual(observed["status"], "uncertain")
        self.assertEqual(observed["code"], "SCHEMA_HEAD_MISMATCH_NO_MIGRATION_ALLOWED")
        namespace["release_directory"].assert_not_called()
        namespace["intent"].assert_not_called()
        with patch.object(RELEASE, "load_json", return_value=(observed, "2" * 64)), \
             patch.object(RELEASE.ops, "manifest") as manifest, \
             patch.object(RELEASE.ops, "remote_python") as remote, \
             patch.object(RELEASE, "receipt") as write_receipt, redirect_stdout(io.StringIO()):
            result = RELEASE.main(["plan", "--inspection", "fixture", "--commit", BINDING["commit"], "--nonce", BINDING["release_nonce"]])
        self.assertEqual(result, 1)
        manifest.assert_not_called()
        remote.assert_not_called()
        write_receipt.assert_not_called()

    def test_full_native_dump_catalog_without_database_object_is_valid(self):
        namespace = remote_namespace()
        tables = ["challenges", "challenge_image_cleanup_jobs", *[f"fixture_table_{index:02}" for index in range(55)]]
        catalog = dump_catalog_fixture(tables)
        self.assertNotIn(b"DATABASE", catalog)
        result = namespace["validate_learn_dump_catalog"](0, catalog, sorted(tables))
        self.assertEqual(result["table_count"], 57)
        self.assertEqual(result["table_data_count"], 57)
        self.assertEqual(result["native_list_sha256"], RELEASE.payload_digest(catalog))

    def test_dump_catalog_rejects_schema_only_or_missing_table_data(self):
        namespace = remote_namespace()
        tables = ["challenges", "challenge_image_cleanup_jobs", "users"]
        full = dump_catalog_fixture(tables)
        missing_data = b"\n".join(line for line in full.splitlines() if b"TABLE DATA public users " not in line) + b"\n"
        for catalog in (dump_catalog_fixture(tables, include_data=False), missing_data):
            with self.subTest(catalog=catalog), self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "PUBLIC_TABLE_OR_DATA_MISMATCH"):
                namespace["validate_learn_dump_catalog"](0, catalog, tables)

    def test_dump_catalog_rejects_wrong_missing_extra_or_duplicate_public_table(self):
        namespace = remote_namespace()
        tables = ["challenges", "challenge_image_cleanup_jobs", "users"]
        for names in (["challenges", "challenge_image_cleanup_jobs", "wrong_table"], tables[:-1], tables + ["extra_table"], tables + ["users"]):
            with self.subTest(names=names), self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "PUBLIC_TABLE_OR_DATA_MISMATCH"):
                namespace["validate_learn_dump_catalog"](0, dump_catalog_fixture(names), tables)

    def test_dump_catalog_rejects_native_failure_even_with_plausible_output(self):
        namespace = remote_namespace()
        tables = ["challenges", "challenge_image_cleanup_jobs"]
        with self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "RESTORE_CATALOG_INVALID"):
            namespace["validate_learn_dump_catalog"](1, dump_catalog_fixture(tables), tables)

    def test_dump_catalog_requires_authoritative_valid_catalog_and_cleanup_tables(self):
        namespace = remote_namespace()
        for tables in ([], ["challenges"], ["challenge_image_cleanup_jobs"], ["challenges", "challenge_image_cleanup_jobs", "challenges"], ["challenges", "challenge_image_cleanup_jobs", "quoted table"]):
            with self.subTest(tables=tables), self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "PUBLIC_TABLE_CATALOG_INVALID"):
                namespace["validate_learn_dump_catalog"](0, dump_catalog_fixture(tables), tables)

    def test_authoritative_table_catalog_query_is_read_only_constant_sql(self):
        namespace = remote_namespace()
        expected = ["challenge_image_cleanup_jobs", "challenges"]
        namespace["run"] = Mock(return_value=json.dumps(expected).encode())
        self.assertEqual(namespace["learn_public_table_names"]("f" * 64), expected)
        command = namespace["run"].call_args.args[0]
        self.assertEqual(command[:4], ["docker", "exec", "f" * 64, "sh"])
        self.assertIn("default_transaction_read_only=on", command[5])
        self.assertIn("statement_timeout=15000", command[5])
        self.assertIn("lock_timeout=3000", command[5])
        self.assertEqual(command[-1], namespace["LEARN_PUBLIC_TABLES_SQL"])
        self.assertIn("WHERE schemaname='public'", command[-1])

    def test_dependent_health_exception_is_explicit_maintenance_only(self):
        before = snapshot()
        before["containers"].append(row(RELEASE.DEPENDENT_HEALTH_SERVICE, 30))
        after = copy.deepcopy(before)
        after["containers"][-1]["health"] = "unhealthy"
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "NON_TARGET_CONTAINER_CHANGED"):
            RELEASE.preserved_snapshot(before, after)
        changes = RELEASE.preserved_snapshot(before, after, learn_maintenance=True)
        self.assertEqual(changes, [{"service": RELEASE.DEPENDENT_HEALTH_SERVICE, "container_id": before["containers"][-1]["container_id"],
                                    "from": "healthy", "to": "unhealthy", "phase": "learn-maintenance", "controlled": False}])

    def test_maintenance_never_allows_dependent_restart_identity_or_foreign_change(self):
        before = snapshot()
        before["containers"].append(row(RELEASE.DEPENDENT_HEALTH_SERVICE, 30))
        for field, value in (("container_id", "0" * 64), ("image_id", "sha256:" + "0" * 64), ("pid", 999), ("restart_count", 1),
                             ("runtime_sha256", "0" * 64), ("mounts", ["changed"]), ("running", False), ("status", "exited"),
                             ("labels_sha256", "0" * 64), ("ports", {}), ("started_at", "restarted")):
            after = copy.deepcopy(before)
            after["containers"][-1].update({"health": "unhealthy", field: value})
            with self.subTest(field=field), self.assertRaises(RELEASE.DeletedProblemReleaseError):
                RELEASE.preserved_snapshot(before, after, learn_maintenance=True)
        for index in (7, 8, 9):
            after = copy.deepcopy(before)
            after["containers"][index]["health"] = "unhealthy"
            with self.subTest(other=index), self.assertRaises(RELEASE.DeletedProblemReleaseError):
                RELEASE.preserved_snapshot(before, after, learn_maintenance=True)
        foreign = copy.deepcopy(before)
        foreign["containers"][-1]["project"] = "someone-else"
        changed = copy.deepcopy(foreign)
        changed["containers"][-1]["health"] = "unhealthy"
        with self.assertRaises(RELEASE.DeletedProblemReleaseError):
            RELEASE.preserved_snapshot(foreign, changed, learn_maintenance=True)

    def test_final_dependent_verification_waits_for_original_healthy_identity(self):
        before = snapshot()
        before["containers"].append(row(RELEASE.DEPENDENT_HEALTH_SERVICE, 30))
        unhealthy = copy.deepcopy(before)
        unhealthy["containers"][-1]["health"] = "unhealthy"
        namespace = remote_namespace()
        namespace["snapshot"] = Mock(side_effect=[unhealthy, copy.deepcopy(before)])
        namespace["time"] = SimpleNamespace(monotonic=Mock(return_value=0), sleep=Mock())
        namespace["bounded"] = Mock()
        namespace["control_container"] = Mock()
        final, observations = namespace["await_original_dependent_health"]({"inspection": before})
        self.assertEqual(final, before)
        self.assertEqual([(entry["from"], entry["to"]) for entry in observations], [("healthy", "unhealthy"), ("unhealthy", "healthy")])
        namespace["time"].sleep.assert_called_once_with(1)
        namespace["bounded"].assert_not_called()
        namespace["control_container"].assert_not_called()
        namespace["snapshot"] = Mock(return_value=unhealthy)
        namespace["time"] = SimpleNamespace(monotonic=Mock(side_effect=[0, 121]), sleep=Mock())
        with self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "RECOVERY_UNCONFIRMED"):
            namespace["await_original_dependent_health"]({"inspection": before})
        namespace["snapshot"] = Mock()
        with self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "ORIGINAL_DEPENDENT_HEALTHY_BASELINE_REQUIRED"):
            namespace["await_original_dependent_health"]({"inspection": unhealthy})
        namespace["snapshot"].assert_not_called()

    def test_managed_source_contract_keeps_old_extras_exact_and_sorts_rows(self):
        def item(name, data):
            return {"path": name, "bytes": len(data), "sha256": RELEASE.payload_digest(data)}
        managed = [item("frontend/src/page.tsx", b"new frontend"), item("services/learn/app/main.py", b"new learn"), item("services/env-orchestrator/cmd/main.go", b"new env")]
        extras = [item("frontend/C\u044f\u0410\u2551Program", b"opaque existing artifact"), item("frontend/local-session-cookies.txt", b"opaque existing artifact 2"), item("docs/design/existing.md", b"original document")]
        original = [item(value["path"], b"old managed") for value in managed] + extras
        current = list(reversed(managed + extras))
        result = RELEASE.managed_source_contract(original, current, list(reversed(managed)))
        self.assertEqual(result["managed_file_count"], 3)
        self.assertEqual(result["preserved_extra_file_count"], 3)
        self.assertEqual(result, remote_namespace()["managed_source_contract"](list(reversed(original)), current, managed))
        self.assertEqual(RELEASE.managed_source_contract(original, original, managed, require_managed=False)["preserved_extra_file_count"], 3)

    def test_managed_source_contract_rejects_deleted_modified_moved_or_added_extra(self):
        managed = [{"path": "frontend/src/page.tsx", "bytes": 3, "sha256": RELEASE.payload_digest(b"new")}]
        extra = {"path": "frontend/local-session-cookies.txt", "bytes": 3, "sha256": RELEASE.payload_digest(b"old")}
        original = managed + [extra]
        for extras in ([], [extra | {"sha256": "0" * 64}], [extra | {"path": "frontend/moved.txt"}], [extra, extra | {"path": "frontend/additional.txt"}]):
            with self.subTest(extras=extras), self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "UNMANAGED_SOURCE_CHANGED"):
                RELEASE.managed_source_contract(original, managed + extras, managed)

    def test_managed_source_contract_rejects_missing_or_changed_managed_subset(self):
        managed = [{"path": "frontend/src/page.tsx", "bytes": 3, "sha256": RELEASE.payload_digest(b"new")}]
        for current in ([], [managed[0] | {"sha256": "0" * 64}], [managed[0] | {"bytes": 2}]):
            with self.subTest(current=current), self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "MANAGED_SOURCE_MISSING_OR_CHANGED"):
                RELEASE.managed_source_contract([], current, managed)

    def test_activation_missing_staged_file_fails_before_first_live_write(self):
        namespace = remote_namespace()
        before = snapshot()
        stopped = copy.deepcopy(before)
        for value in stopped["containers"][:7]:
            value.update({"running": False, "pid": 0, "status": "exited", "health": None, "finished_at": "stopped", "ports": {}})
        namespace["snapshot"] = lambda: stopped
        namespace["completed"] = lambda directory, action: {"fenced_services": list(RELEASE.TARGETS), "minio_resumed_same_identity": True}
        namespace["bounded"] = Mock()
        namespace["private_json"] = Mock()
        TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TMP) as temporary:
            root = Path(temporary)
            release = root / "release"
            live = root / "live"
            (release / "source/frontend").mkdir(parents=True)
            (release / "source/frontend/a.txt").write_bytes(b"new")
            (live / "frontend").mkdir(parents=True)
            (live / "frontend/a.txt").write_bytes(b"original")
            (release / "fence.snapshot.json").write_text(json.dumps(stopped), encoding="utf-8")
            namespace["ROOT"] = live
            managed = [{"path": name, "bytes": 3, "sha256": RELEASE.payload_digest(b"new")} for name in ("frontend/a.txt", "services/learn/app/missing.py")]
            plan = {"binding": BINDING, "inspection": before, "managed_files": managed, "modes": {value["path"]: 0o644 for value in managed}}
            with self.assertRaisesRegex(namespace["DeletedProblemReleaseError"], "ACTIVATION_SOURCE_MISSING_OR_LINK"):
                namespace["activate"](release, plan)
            self.assertEqual((live / "frontend/a.txt").read_bytes(), b"original")
            self.assertEqual((release / "source/frontend/a.txt").read_bytes(), b"new")
        namespace["bounded"].assert_not_called()
        namespace["private_json"].assert_not_called()

    def test_no_prune_remove_migration_or_restore_command_in_remote(self):
        tree = ast.parse(Path(RELEASE.__file__).with_name("deleted_problem_release_remote.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                literals = [item.value for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                self.assertNotIn("prune", literals)
                self.assertNotIn("rm", literals)
                self.assertNotIn("down", literals)
                self.assertNotIn("upgrade", literals)
                self.assertNotIn("flushdb", literals)

    def test_cli_inspect_without_execute_has_no_network(self):
        with patch.object(RELEASE.ops, "remote_python") as remote, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(RELEASE.main(["inspect"]), 0)
        remote.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["network"])

    def test_expired_or_tool_changed_plan_rejected_before_remote(self):
        plan = {"kind": "deleted-problem-release-plan", "schema_version": 1, "binding": BINDING, "scope": RELEASE.SCOPE,
                "inspection": snapshot(), "managed_files": [{"path": "services/learn/app/main.py", "bytes": 1, "sha256": "a" * 64}],
                "modes": {"services/learn/app/main.py": 0o644}, "tool_sources": RELEASE.tools_binding(),
                "created_at": (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()}
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "PLAN_EXPIRED"):
            RELEASE.validate_plan(plan, current=False)
        plan["tool_sources"] = {}
        with self.assertRaisesRegex(RELEASE.DeletedProblemReleaseError, "RELEASE_TOOLS_CHANGED"):
            RELEASE.validate_plan(plan, current=False)


if __name__ == "__main__":
    unittest.main()
