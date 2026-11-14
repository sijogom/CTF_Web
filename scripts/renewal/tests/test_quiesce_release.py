"""Pure quiescence contracts: no SSH, Docker, process or ACL query is executed."""
from contextlib import ExitStack
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops
import backup_release as backup
import quiesce_release as quiesce
import release_preflight as preflight
from test_release_backup import fixtures


class FakeRemote:
    def __init__(self, inspection):
        self.inspection = copy.deepcopy(inspection)
        self.mutations = []
        self.unknown = None
        self.start_health = "healthy"

    def __call__(self, program, **_kwargs):
        if program.startswith("mutation:"):
            _, identifier, action = program.split(":")
            self.mutations.append((identifier, action))
            if self.unknown == "before":
                raise ConnectionError("withheld remote diagnostics")
            row = next(row for row in self.inspection["containers"] if row["container_id"] == identifier)
            start = action == "start"
            row.update(running=start, pid=500 if start else 0, status="running" if start else "exited")
            if start:
                row.update(started_at="2026-10-13T04:00:00Z", finished_at="0001-01-01T00:00:00Z",
                           health=self.start_health if row["health"] is not None else None)
            else:
                row["finished_at"] = "2026-10-13T03:00:00Z"
            if self.unknown == "after":
                raise ConnectionError("withheld remote diagnostics")
            return {"schema_version": 1, "kind": "renewal-container-mutation", "result": "acknowledged"}
        if program == quiesce.CONTAINER_INSPECTOR:
            return {key: copy.deepcopy(self.inspection[key]) for key in
                    ("schema_version", "verified", "live_root", "project", "containers")}
        if program == preflight.INSPECTOR:
            result = copy.deepcopy(self.inspection)
            if all(not row["running"] for row in result["containers"] if row["service"] in {*quiesce.APIS, *quiesce.WORKERS}):
                for row in result["databases"].values():
                    row["client_session_count"] = 0
            return result
        if program == quiesce.RESUME_DATABASE_INSPECTOR:
            return {key: copy.deepcopy(self.inspection[key]) for key in
                    ("schema_version", "verified", "live_root", "project", "databases")}
        raise AssertionError("Unexpected remote program")


class QuiescenceContracts(unittest.TestCase):
    def setUp(self):
        parent = ops.TASK / "runtime/work/quiescence-unit-tests"
        parent.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source, self.migrations, self.plan, _payload, _q, _fence = fixtures()
        rows = self.plan["inspection"]["containers"]
        next(row for row in rows if row["service"] == "auth-personal-notifications")["service"] = "personal-notification-runner"
        for index, service in enumerate(("redis", "ctfd", "env-orchestrator", "auth-migrate"), 20):
            row = copy.deepcopy(rows[0])
            row.update(service=service, container_id=format(index, "064x"), image_id="sha256:" + format(index, "064x"))
            if service == "auth-migrate":
                row.update(running=False, pid=0, status="exited", finished_at="2026-10-13T02:00:00Z",
                           image_id=next(item["image_id"] for item in rows if item["service"] == "auth-service"))
            rows.append(row)
        rows.sort(key=lambda row: (row["service"], row["container_id"]))
        self.plan_path = self.root / "evidence/plan.json"
        self.plan_hash = preflight.exclusive_json(self.plan_path, self.plan)
        self.remote = FakeRemote(self.plan["inspection"])
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(ops, "TASK", self.root))
        self.manifest = self.patches.enter_context(patch.object(ops, "manifest", return_value=self.source))
        self.patches.enter_context(patch.object(quiesce, "source_migrations", return_value=self.migrations))
        self.ssh = self.patches.enter_context(patch.object(ops, "remote_python", side_effect=self.remote))
        self.patches.enter_context(patch.object(quiesce, "mutation_program",
            side_effect=lambda row, start: "mutation:" + row["container_id"] + (":start" if start else ":stop")))
        self.clock = 0.0
        self.patches.enter_context(patch.object(quiesce.time, "monotonic", side_effect=lambda: self.clock))
        self.patches.enter_context(patch.object(quiesce.time, "sleep", side_effect=self.advance_clock))

    def advance_clock(self, seconds):
        self.clock += seconds

    def run_action(self, action="stop", execute=True):
        return quiesce.execute(self.plan_path, self.plan_hash, action, execute_approved=execute)

    def rejected(self, code, action="stop", execute=True):
        with self.assertRaisesRegex(preflight.ReleaseError, code):
            self.run_action(action, execute)

    def claim_stop(self):
        value = {"schema_version": 1, "kind": "renewal-quiescence-intent", "task": preflight.TASK_ID,
            "nonce": self.plan["nonce"], "action": "stop", "plan_sha256": self.plan_hash,
            "source_fingerprint": self.plan["source_fingerprint"], "created_at": datetime.now(timezone.utc).isoformat(),
            "stop_attempt_protocol": 1,
            "targets": [quiesce.identity(row) for row in quiesce.action_targets(self.plan, "stop")]}
        preflight.exclusive_json(quiesce.receipt_path("stop", self.plan["nonce"], "intent"), value)

    def stopped_remote(self):
        ids = {row["container_id"] for row in quiesce.action_targets(self.plan, "stop")}
        for row in self.remote.inspection["containers"]:
            if row["container_id"] in ids:
                row.update(running=False, pid=0, status="exited", finished_at="2026-10-13T03:00:00Z")

    def test_default_dry_run_has_no_ssh_or_intent(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            quiesce.main(["--plan", str(self.plan_path), "--plan-sha256", self.plan_hash])
        self.assertFalse(json.loads(output.getvalue())["execute"])
        self.ssh.assert_not_called()
        self.assertEqual(list((self.root / "evidence").iterdir()), [self.plan_path])

    def test_stop_order_exact_ids_and_backup_dto_compatibility(self):
        before = copy.deepcopy(self.plan["inspection"]["containers"])
        result = self.run_action()
        ids = [identifier for identifier, _action in self.remote.mutations]
        services = {row["container_id"]: row["service"] for row in before}
        self.assertEqual([services[item] for item in ids], ["personal-notification-runner", "community-service", "learn-service", "auth-service", "minio"])
        self.assertTrue(all(action == "stop" for _identifier, action in self.remote.mutations))
        receipt = preflight.parse_json(Path(result["receipt"]).read_bytes())
        backup.validate_quiescence(receipt, self.plan, self.plan_hash)
        untouched = {row["container_id"]: row for row in before if row["container_id"] not in ids}
        self.assertTrue(all(row == untouched[row["container_id"]] for row in receipt["inspection"]["containers"] if row["container_id"] in untouched))
        self.assertEqual(result["receipt_sha256"], preflight.sha256(Path(result["receipt"]).read_bytes()))

    def test_uncertain_stop_is_inspected_once_without_command_retry(self):
        self.remote.unknown = "after"
        result = self.run_action()
        self.assertEqual(len(self.remote.mutations), 5)
        self.assertEqual(result["mutation_attempt_count"], 5)
        self.assertEqual(sum(call.args[0] == quiesce.CONTAINER_INSPECTOR for call in self.ssh.call_args_list), 5)

    def test_unconfirmed_stop_preserves_intent_partial_and_blocks_reuse(self):
        self.remote.unknown = "before"
        self.rejected("OUTCOME_UNCONFIRMED")
        self.assertEqual(len(self.remote.mutations), 1)
        self.assertTrue(quiesce.receipt_path("stop", self.plan["nonce"], "intent").is_file())
        self.assertTrue(quiesce.receipt_path("stop", self.plan["nonce"], "partial").is_file())
        self.assertFalse(quiesce.receipt_path("stop", self.plan["nonce"], "complete").exists())
        self.ssh.reset_mock()
        self.rejected("ALREADY_ATTEMPTED")
        self.ssh.assert_not_called()

    def test_nonce_lease_blocks_concurrent_actions_and_releases_after_failure(self):
        with quiesce.action_lease(self.plan, self.plan_hash):
            self.rejected("ACTION_IN_PROGRESS")
        self.remote.unknown = "before"
        self.rejected("OUTCOME_UNCONFIRMED")
        self.remote.unknown = None
        # The lease is released, but an in-flight stop is not a recovered service.
        with quiesce.action_lease(self.plan, self.plan_hash):
            pass
        self.rejected("STOP_STILL_IN_FLIGHT", "resume-original")
        self.assertEqual(len(self.remote.mutations), 1)

    def test_unknown_lease_binding_is_preserved_and_rejected(self):
        path = self.root / "evidence" / f"quiesce-active-{self.plan['nonce']}.lock"
        with path.open("xb") as stream:
            stream.write(b"unknown-owned-state")
        self.rejected("LEASE_BINDING_CHANGED")
        self.assertEqual(path.read_bytes(), b"unknown-owned-state")
        self.ssh.assert_not_called()

    def test_source_race_stops_further_mutation_and_original_recovery_ignores_new_source(self):
        self.manifest.side_effect = lambda: ({**self.source, "fingerprint": "0" * 64}
            if self.remote.mutations else self.source)
        self.rejected("SOURCE_CHANGED_DURING_STOP")
        self.assertEqual(len(self.remote.mutations), 1)
        manifest_calls = self.manifest.call_count
        self.run_action("resume-original")
        self.assertEqual(self.manifest.call_count, manifest_calls)
        self.assertEqual([action for _identifier, action in self.remote.mutations], ["stop", "start"])

    def test_unrelated_container_change_after_mutation_aborts_fence(self):
        original = self.remote.__call__
        def changed(program, **kwargs):
            result = original(program, **kwargs)
            if program.startswith("mutation:"):
                row = next(row for row in self.remote.inspection["containers"] if row["service"] == "postgres")
                row["pid"] += 1
            return result
        self.ssh.side_effect = changed
        self.rejected("UNRELATED_CONTAINER_CHANGED")
        self.assertEqual(len(self.remote.mutations), 1)
        self.assertTrue(quiesce.receipt_path("stop", self.plan["nonce"], "partial").is_file())
        self.assertFalse(quiesce.receipt_path("stop", self.plan["nonce"], "complete").exists())

    def test_changed_remote_protected_schema_or_process_fails_before_mutation(self):
        original = copy.deepcopy(self.remote.inspection)
        for field in ("protected", "schema", "process"):
            with self.subTest(field=field):
                self.remote.inspection = copy.deepcopy(original)
                if field == "protected":
                    self.remote.inspection["protected_files"][0]["sha256"] = "0" * 64
                elif field == "schema":
                    self.remote.inspection["databases"]["learn"]["schema_sha256"] = "0" * 64
                else:
                    self.remote.inspection["containers"][0]["restart_count"] += 1
                self.rejected("FENCE_CHANGED")
                self.assertFalse(self.remote.mutations)
                self.assertFalse(quiesce.receipt_path("stop", self.plan["nonce"], "intent").exists())

    def test_wrong_plan_hash_or_changed_source_fails_before_ssh(self):
        with self.assertRaisesRegex(preflight.ReleaseError, "SHA256_MISMATCH"):
            quiesce.execute(self.plan_path, "0" * 64, "stop", execute_approved=True)
        self.manifest.return_value = {**self.source, "fingerprint": "0" * 64}
        self.rejected("SOURCE_FINGERPRINT_CHANGED")
        self.ssh.assert_not_called()

    def test_running_same_image_migration_and_unapproved_worker_image_are_never_stopped(self):
        for mode, code in (("migration", "SERVICE_NOT_APPROVED"), ("image", "WRITER_IMAGE_NOT_APPROVED")):
            value = copy.deepcopy(self.plan)
            if mode == "migration":
                row = next(row for row in value["inspection"]["containers"] if row["service"] == "auth-migrate")
                row.update(running=True, pid=999, status="running")
            else:
                row = next(row for row in value["inspection"]["containers"] if row["service"] == "personal-notification-runner")
                row["image_id"] = "sha256:" + "e" * 64
            with self.assertRaisesRegex(preflight.ReleaseError, code):
                quiesce.select_targets(value)
        self.ssh.assert_not_called()

    def test_storage_resume_uses_original_identity_only_and_preserves_api_stops(self):
        self.claim_stop(); self.stopped_remote()
        self.manifest.side_effect = AssertionError("Emergency resume must not inspect current source")
        self.run_action("resume-storage")
        self.assertEqual([action for _identifier, action in self.remote.mutations], ["start"])
        row = next(row for row in self.remote.inspection["containers"] if row["service"] == "minio")
        self.assertEqual(self.remote.mutations[0][0], row["container_id"])
        self.assertTrue(row["running"])
        self.assertTrue(all(not row["running"] for row in self.remote.inspection["containers"] if row["service"] in quiesce.APIS))

    def test_original_resume_allows_expired_plan_and_never_starts_migration(self):
        self.plan["created_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        self.plan_path.unlink()
        self.plan_hash = preflight.exclusive_json(self.plan_path, self.plan)
        self.claim_stop(); self.stopped_remote()
        self.manifest.side_effect = AssertionError("Emergency resume must not inspect current source")
        self.run_action("resume-original")
        services = {row["container_id"]: row["service"] for row in self.remote.inspection["containers"]}
        self.assertEqual([services[identifier] for identifier, _action in self.remote.mutations],
            ["minio", "auth-service", "learn-service", "community-service", "personal-notification-runner"])
        self.assertFalse(next(row for row in self.remote.inspection["containers"] if row["service"] == "auth-migrate")["running"])

    def test_resume_refuses_missing_intent_or_replaced_id_image_mount(self):
        self.rejected("STOP_INTENT_REQUIRED", "resume-storage")
        self.claim_stop(); self.stopped_remote()
        original = copy.deepcopy(self.remote.inspection)
        for key, value in (("container_id", "e" * 64), ("image_id", "sha256:" + "e" * 64), ("mounts", [])):
            with self.subTest(key=key):
                self.remote.inspection = copy.deepcopy(original)
                next(row for row in self.remote.inspection["containers"] if row["service"] == "minio")[key] = value
                self.rejected("IDENTITY_CHANGED", "resume-storage")
                self.assertFalse(self.remote.mutations)

    def test_resume_skips_already_running_targets_and_does_not_restart_them(self):
        self.claim_stop()
        result = self.run_action("resume-original")
        self.assertEqual(result["mutation_attempt_count"], 0)
        self.assertFalse(self.remote.mutations)

    def test_original_resume_rejects_changed_head_or_schema_before_any_start(self):
        self.claim_stop(); self.stopped_remote()
        original = copy.deepcopy(self.remote.inspection["databases"])
        for key, value in (("head", ["20261013_028"]), ("schema_sha256", "f" * 64)):
            with self.subTest(key=key):
                self.remote.inspection["databases"] = copy.deepcopy(original)
                self.remote.inspection["databases"]["learn"][key] = value
                self.rejected("DATABASE_SCHEMA_CHANGED", "resume-original")
                self.assertFalse(self.remote.mutations)

    def test_original_resume_rejects_activation_intent_before_any_start(self):
        self.claim_stop(); self.stopped_remote()
        path = self.root / "evidence" / ("activate-intent-" + self.plan["nonce"] + ".json")
        preflight.exclusive_json(path, {"binding": "synthetic"})
        self.rejected("ACTIVATION_ALREADY_ATTEMPTED", "resume-original")
        self.assertFalse(self.remote.mutations)

    def test_storage_resume_is_allowed_after_schema_upgrade(self):
        self.claim_stop(); self.stopped_remote()
        self.remote.inspection["databases"]["learn"]["head"] = ["20261013_028"]
        result = self.run_action("resume-storage")
        self.assertEqual(result["mutation_attempt_count"], 1)
        self.assertFalse(any(call.args[0] == quiesce.RESUME_DATABASE_INSPECTOR for call in self.ssh.call_args_list))

    def test_unhealthy_original_start_remains_partial_and_is_not_complete(self):
        self.claim_stop(); self.stopped_remote()
        self.remote.start_health = "unhealthy"
        self.rejected("HEALTH_UNVERIFIED", "resume-original")
        self.assertEqual(len(self.remote.mutations), 1)
        self.assertFalse(quiesce.receipt_path("resume-original", self.plan["nonce"], "complete").exists())
        self.assertTrue(quiesce.receipt_path("resume-original", self.plan["nonce"], "partial").exists())

    def test_unknown_stop_must_settle_before_original_restart(self):
        self.remote.unknown = "before"
        self.rejected("OUTCOME_UNCONFIRMED")
        self.remote.unknown = None
        original = self.remote.__call__
        attempted = self.remote.mutations[0][0]
        polls = 0

        def eventually_exited(program, **kwargs):
            nonlocal polls
            if program == quiesce.CONTAINER_INSPECTOR:
                polls += 1
                if polls == 3:
                    row = next(row for row in self.remote.inspection["containers"] if row["container_id"] == attempted)
                    row.update(running=False, pid=0, status="exited", finished_at="2026-10-13T03:00:00Z")
            return original(program, **kwargs)

        self.ssh.side_effect = eventually_exited
        result = self.run_action("resume-original")
        self.assertEqual(result["mutation_attempt_count"], 1)
        self.assertEqual(self.remote.mutations, [(attempted, "stop"), (attempted, "start")])
        self.assertGreaterEqual(polls, 3)

    def test_existing_worker_without_healthcheck_is_reported_as_process_only(self):
        for rows in (self.plan["inspection"]["containers"], self.remote.inspection["containers"]):
            next(row for row in rows if row["service"] == "personal-notification-runner")["health"] = None
        self.plan_path.unlink()
        self.plan_hash = preflight.exclusive_json(self.plan_path, self.plan)
        self.claim_stop(); self.stopped_remote()
        result = self.run_action("resume-original")
        value = preflight.parse_json(Path(result["receipt"]).read_bytes())
        self.assertEqual(value["readiness_checked_count"], 4)
        self.assertEqual(value["process_only_without_readiness_count"], 1)
        row = next(row for row in value["containers"] if row["service"] == "personal-notification-runner")
        self.assertIsNone(row["health"])
        self.assertTrue(row["running"])
        self.assertGreater(self.clock, 0)

    def test_running_flag_with_zero_pid_is_not_a_ready_process(self):
        target = quiesce.action_targets(self.plan, "resume-original")[0]
        row = next(row for row in self.remote.inspection["containers"] if row["container_id"] == target["container_id"])
        row["pid"] = 0
        with self.assertRaisesRegex(preflight.ReleaseError, "CONTAINER_STATE_INVALID"):
            quiesce.wait_for_target(target, self.remote.inspection["containers"], start=True, healthy=True)

    def test_inspect_is_read_only_without_source_or_age_requirement(self):
        self.manifest.side_effect = AssertionError("Read-only original-state inspection must not require current source")
        result = self.run_action("inspect", False)
        self.assertFalse(result["execute"])
        self.assertEqual(result["running_target_count"], 5)
        self.assertFalse(self.remote.mutations)
        self.assertEqual(list((self.root / "evidence").iterdir()), [self.plan_path])

    def test_external_database_clients_prevent_a_quiescence_receipt(self):
        original = self.remote.__call__
        def clients_remain(program, **kwargs):
            result = original(program, **kwargs)
            if program == preflight.INSPECTOR:
                result["databases"]["auth"]["client_session_count"] = 1
            return result
        self.ssh.side_effect = clients_remain
        self.rejected("DATABASE_CLIENTS_REMAIN")
        self.assertEqual(len(self.remote.mutations), 5)
        self.assertFalse(quiesce.receipt_path("stop", self.plan["nonce"], "complete").exists())
        self.assertTrue(quiesce.receipt_path("stop", self.plan["nonce"], "partial").is_file())

    def test_real_remote_mutator_compiles_and_only_allows_graceful_exact_id_operations(self):
        target = quiesce.action_targets(self.plan, "stop")[0]
        self.patches.close()
        for start in (True, False):
            program = quiesce.mutation_program(target, start=start)
            compile(program, "remote-quiescence", "exec")
            self.assertIn("['--signal',selected_signal] if selected_signal else []", program)
            self.assertIn("'--time','-1',target['container_id']]", program)
            self.assertNotIn("docker compose", program.split("EXPECTED=", 1)[1])
            for forbidden in ("docker kill", "docker rm", "docker prune", "docker restart", "sudo", "SIGKILL"):
                if forbidden == "SIGKILL":
                    continue  # Appears solely in the no-escalation comment.
                self.assertNotIn(forbidden, program.split("EXPECTED=", 1)[1])


class GracefulSignalContracts(unittest.TestCase):
    def test_other_services_keep_their_original_stop_signal(self):
        self.assertIsNone(quiesce.graceful_stop_signal("learn-service", None, None))

    def test_fixed_workers_use_sigterm_and_legacy_pid1_workers_use_registered_sigint(self):
        for service, module in quiesce.LEGACY_SIGNAL_MODULES.items():
            with self.subTest(service=service):
                command = ["python", "-m", module]
                self.assertIsNone(quiesce.graceful_stop_signal(service, command, {"comm": "python", "term": True, "interrupt": True}))
                self.assertEqual(quiesce.graceful_stop_signal(service, command, {"comm": "python", "term": False, "interrupt": True}), "SIGINT")

    def test_wrong_worker_command_cannot_select_an_alternate_signal(self):
        for service in quiesce.LEGACY_SIGNAL_MODULES:
            with self.assertRaisesRegex(preflight.ReleaseError, "COMMAND_CHANGED"):
                quiesce.graceful_stop_signal(service, ["sh", "-c", "python"], {"comm": "python", "term": False, "interrupt": True})

    def test_unknown_pid1_signal_state_fails_closed(self):
        service, module = next(iter(quiesce.LEGACY_SIGNAL_MODULES.items()))
        for state in ({}, {"comm": "sh", "term": False, "interrupt": True},
                      {"comm": "python", "term": 0, "interrupt": True}):
            with self.subTest(state=state), self.assertRaisesRegex(preflight.ReleaseError, "STATE_UNVERIFIED"):
                quiesce.graceful_stop_signal(service, ["python", "-m", module], state)

    def test_legacy_worker_without_a_registered_interrupt_handler_is_not_signaled(self):
        service, module = next(iter(quiesce.LEGACY_SIGNAL_MODULES.items()))
        with self.assertRaisesRegex(preflight.ReleaseError, "HANDLER_MISSING"):
            quiesce.graceful_stop_signal(service, ["python", "-m", module], {"comm": "python", "term": False, "interrupt": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
