"""Pure same-nonce reinspection before any backup or mutation attempt."""
from __future__ import annotations

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
import release_preflight as preflight
from test_release_backup import fixtures


class PreflightRenewalTests(unittest.TestCase):
    def setUp(self):
        parent = ops.TASK / "runtime/work/preflight-renewal-unit-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.directory.cleanup)
        self.task = Path(self.directory.name)
        self.evidence = self.task / "evidence"; self.evidence.mkdir()
        self.source, self.migrations, self.plan, _, _, _ = fixtures()
        self.plan["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.payload = preflight.canonical(self.plan) + b"\n"
        self.prior = self.evidence / "plan-a.json"; self.prior.write_bytes(self.payload)
        self.plan_hash = preflight.sha256(self.payload)

    def rejected(self, code, function, *args, **kwargs):
        with self.assertRaisesRegex(preflight.ReleaseError, code): function(*args, **kwargs)

    def load(self):
        return preflight.load_renewal_plan(self.prior, self.plan_hash, self.source, self.migrations)

    def run_main(self, snapshot=None, before_return=None):
        destination = self.evidence / "plan-b.json"
        def remote(*args, **kwargs):
            if before_return: before_return()
            return snapshot if snapshot is not None else copy.deepcopy(self.plan["inspection"])
        with patch.object(ops, "TASK", self.task), patch.object(ops, "manifest", return_value=self.source), \
                patch.object(preflight, "source_migrations", return_value=self.migrations), \
                patch.object(ops, "remote_python", side_effect=remote) as inspect, patch("sys.stdout", io.StringIO()) as output:
            preflight.main(["--plan", str(destination), "--renew-plan", str(self.prior), "--renew-plan-sha256", self.plan_hash])
        return destination, inspect, json.loads(output.getvalue())

    def test_expired_prior_requires_actual_fresh_inspection_and_new_exclusive_plan(self):
        self.rejected("EXPIRED", preflight.validate_plan, self.plan, self.source["fingerprint"])
        destination, inspected, public = self.run_main()
        renewed = preflight.parse_json(destination.read_bytes())
        preflight.validate_plan(renewed, self.source["fingerprint"])
        self.assertEqual(renewed["nonce"], self.plan["nonce"])
        self.assertNotEqual(renewed["created_at"], self.plan["created_at"])
        self.assertEqual(self.prior.read_bytes(), self.payload)
        self.assertEqual(public["plan_sha256"], preflight.sha256(destination.read_bytes()))
        self.assertIn(public["plan_sha256"][:12], Path(public["receipt"]).name)
        receipt = preflight.parse_json(Path(public["receipt"]).read_bytes())
        self.assertTrue(receipt["fresh_inspection_verified"])
        self.assertFalse(receipt["prior_receipts_modified"])
        self.assertEqual(receipt["renewed_from_plan_sha256"], self.plan_hash)
        inspected.assert_called_once_with(preflight.INSPECTOR, timeout=900)

    def test_mount_order_does_not_change_identity_but_actual_mount_change_does(self):
        mounts = [{"type": "bind", "source": "/tmp/a", "destination": "/a", "read_only": True},
                  {"type": "volume", "source": "/tmp/b", "destination": "/b", "read_only": False}]
        self.plan["inspection"]["containers"][0]["mounts"] = mounts
        snapshot = copy.deepcopy(self.plan["inspection"])
        snapshot["containers"][0]["mounts"].reverse()
        with patch.object(ops, "TASK", self.task):
            preflight.validate_renewal_inspection(self.plan, snapshot, self.migrations)
            snapshot["containers"][0]["mounts"][0]["read_only"] = True
            self.rejected("IMMUTABLE_LIVE_STATE", preflight.validate_renewal_inspection, self.plan, snapshot, self.migrations)

    def test_remote_container_inventory_canonicalizes_docker_mount_order(self):
        namespace = {}
        exec(compile(preflight.REMOTE_HELPERS, "<remote-helper-fixture>", "exec"), namespace)
        item = {"Id": "a" * 64, "Image": "sha256:" + "b" * 64,
                "Config": {"Labels": {"com.docker.compose.project": "ctf_web", "com.docker.compose.service": "caddy"}},
                "State": {"Running": True, "Pid": 12, "Status": "running", "StartedAt": "2026-10-13T00:00:00Z",
                          "FinishedAt": "0001-01-01T00:00:00Z"}, "RestartCount": 0,
                "Mounts": [{"Type": "bind", "Source": "/tmp/b", "Destination": "/b", "RW": False},
                           {"Type": "volume", "Source": "/tmp/a", "Destination": "/a", "RW": True}]}
        namespace["run"] = lambda command: (b"a\n" if command[1] == "ps" else json.dumps([item]).encode())
        first = namespace["containers"]()
        item["Mounts"].reverse()
        self.assertEqual(first, namespace["containers"]())
        self.assertEqual([mount["destination"] for mount in first[0]["mounts"]], ["/a", "/b"])

    def test_missing_pair_or_wrong_hash_stops_before_remote_inspection(self):
        with patch.object(ops, "TASK", self.task), patch.object(ops, "manifest", return_value=self.source), \
                patch.object(preflight, "source_migrations", return_value=self.migrations), patch.object(ops, "remote_python") as inspect:
            for extra in (["--renew-plan", str(self.prior)], ["--renew-plan-sha256", self.plan_hash]):
                self.rejected("INPUT_PAIR", preflight.main, ["--plan", str(self.evidence / "new.json"), *extra])
            self.rejected("SHA256_MISMATCH", preflight.main, ["--plan", str(self.evidence / "new.json"),
                "--renew-plan", str(self.prior), "--renew-plan-sha256", "0" * 64])
            inspect.assert_not_called()

    def test_foreign_source_count_migration_target_or_schema_are_rejected(self):
        with patch.object(ops, "TASK", self.task):
            for mutation, code in ((lambda value: value.update(source_fingerprint="0" * 64), "SOURCE_FINGERPRINT"),
                (lambda value: value.update(source_file_count=99), "FILE_COUNT"),
                (lambda value: value["migrations"]["auth"].update(head="rev_0"), "MIGRATION_GRAPH_CHANGED"),
                (lambda value: value["target"].update(port=22), "TARGET_INVALID"),
                (lambda value: value.update(unexpected="field"), "UNKNOWN_FIELDS")):
                value = copy.deepcopy(self.plan); mutation(value)
                payload = preflight.canonical(value); self.prior.write_bytes(payload)
                self.rejected(code, preflight.load_renewal_plan, self.prior, preflight.sha256(payload), self.source, self.migrations)

    def test_any_image_backup_stop_activation_intent_or_partial_blocks_renewal(self):
        nonce = self.plan["nonce"]
        names = [f"image-backup-attempt-{nonce}.json", f"image-backup-complete-{nonce}.json",
            f"backup-attempt-{nonce}.json", f"backup-complete-{nonce}.json",
            f"quiesce-stop-intent-{nonce}.json", f"quiesce-stop-partial-{nonce}.json",
            f"quiesce-stop-attempt-{'1' * 64}-{nonce}.json", f"quiesce-active-{nonce}.lock",
            f"activate-intent-{nonce}.json", f"activate-complete-{nonce}.json"]
        with patch.object(ops, "TASK", self.task):
            for name in names:
                marker = self.evidence / name; marker.write_bytes(b"preserve-marker")
                with self.subTest(marker=name): self.rejected("AFTER_MUTATION", self.load)
                self.assertEqual(marker.read_bytes(), b"preserve-marker")
                marker.unlink()

    def test_existing_private_nonce_backup_blocks_renewal_even_without_receipt(self):
        private = self.task / "runtime/private-backups"; private.mkdir(parents=True)
        with patch.object(ops, "TASK", self.task):
            for suffix in ("", "-images"):
                path = private / (self.plan["nonce"] + suffix); path.mkdir()
                self.rejected("AFTER_BACKUP", self.load)
                path.rmdir()

    def test_all_immutable_live_state_changes_are_rejected(self):
        mutations = [lambda value: value.update(compose_sha256="0" * 64),
            lambda value: value["protected_files"][0].update(sha256="0" * 64),
            lambda value: value["code_files"][0].update(sha256="0" * 64),
            lambda value: value["containers"][0].update(pid=value["containers"][0]["pid"] + 1),
            lambda value: value["containers"][0].update(image_id="sha256:" + "f" * 64),
            lambda value: value["containers"][0].update(restart_count=1),
            lambda value: value["containers"][0].update(health="unhealthy"),
            lambda value: value["containers"][0].update(health="starting"),
            lambda value: value["containers"][0].update(started_at="2026-10-13T03:00:00Z"),
            lambda value: value["containers"][0]["mounts"].append({"type": "bind", "source": "/fixture", "destination": "/fixture", "read_only": True}),
            lambda value: value["databases"]["auth"].update(head=["rev_1"]),
            lambda value: value["databases"]["auth"].update(schema_sha256="0" * 64),
            lambda value: value["databases"]["auth"].update(table_count=3)]
        with patch.object(ops, "TASK", self.task):
            for mutate in mutations:
                value = copy.deepcopy(self.plan["inspection"]); mutate(value)
                with self.subTest(mutation=mutations.index(mutate)):
                    self.rejected("IMMUTABLE_LIVE_STATE", preflight.validate_renewal_inspection, self.plan, value, self.migrations)

    def test_online_data_sizes_sessions_objects_and_disk_activity_are_fresh_not_copied(self):
        snapshot = copy.deepcopy(self.plan["inspection"])
        snapshot["disk_available_bytes"] += 100
        snapshot["objects"].update(bytes=9, file_count=2, inventory_sha256="a" * 64)
        for row in snapshot["databases"].values(): row.update(bytes=4096, client_session_count=7)
        destination, _, _ = self.run_main(snapshot)
        renewed = preflight.parse_json(destination.read_bytes())
        self.assertEqual(renewed["inspection"], snapshot)
        self.assertNotEqual(renewed["inspection"], self.plan["inspection"])

    def test_failed_unverified_inspection_never_produces_a_new_plan(self):
        with self.assertRaisesRegex(preflight.ReleaseError, "REMOTE_INSPECTION_UNVERIFIED_PROTECTED"):
            self.run_main({"schema_version": 1, "verified": False, "unverified_stage": "protected"})
        self.assertFalse((self.evidence / "plan-b.json").exists())
        self.assertEqual(self.prior.read_bytes(), self.payload)

    def test_attempt_appearing_during_inspection_blocks_new_plan(self):
        marker = self.evidence / ("image-backup-attempt-" + self.plan["nonce"] + ".json")
        with self.assertRaisesRegex(preflight.ReleaseError, "AFTER_MUTATION"):
            self.run_main(before_return=lambda: marker.write_bytes(b"preserve-marker"))
        self.assertFalse((self.evidence / "plan-b.json").exists())

    def test_stage_receipt_is_preserved_and_does_not_block_refresh(self):
        stage = self.evidence / ("stage-build-" + self.plan["nonce"] + ".json")
        stage.write_bytes(b"preserve-stage-a-receipt")
        self.run_main()
        self.assertEqual(stage.read_bytes(), b"preserve-stage-a-receipt")


class ComposeChallengeClassificationTests(unittest.TestCase):
    def namespace(self, labels=None):
        namespace = {}
        exec(compile(preflight.REMOTE_HELPERS, "<compose-classification-fixture>", "exec"), namespace)
        core = {"Id": "a" * 64, "Image": "sha256:" + "b" * 64,
                "Config": {"Labels": {"com.docker.compose.project": "ctf_web", "com.docker.compose.service": "learn-service"}},
                "State": {"Running": True, "Pid": 12, "Status": "running", "StartedAt": "2026-10-13T00:00:00Z",
                          "FinishedAt": "0001-01-01T00:00:00Z", "Health": {"Status": "healthy"}}, "RestartCount": 0, "Mounts": []}
        lab = copy.deepcopy(core)
        lab.update(Id="c" * 64, Name="/ctf-challenge-prefixed-instance")
        lab["Config"]["Labels"].update({"ctf-challenge": "true"} if labels is None else labels)
        values = [core, lab]
        namespace["run"] = lambda command: ("\n".join(item["Id"] for item in values).encode() if command[1] == "ps" else json.dumps(values).encode())
        return namespace, values

    def test_inherited_compose_project_and_service_labels_do_not_make_challenge_a_core_consumer(self):
        namespace, values = self.namespace()
        before = copy.deepcopy(values)
        rows = namespace["containers"]()
        self.assertEqual(len(rows), 1)
        self.assertEqual(namespace["singleton"](rows, "learn-service")["container_id"], values[0]["Id"])
        self.assertEqual(values, before)

    def test_name_prefix_or_nonexact_challenge_value_is_not_silently_excluded(self):
        for labels in ({}, {"ctf-challenge": "false"}, {"ctf-challenge": "true-extra"}):
            namespace, _values = self.namespace(labels)
            rows = namespace["containers"]()
            self.assertEqual(len(rows), 2)
            with self.subTest(labels=labels), self.assertRaisesRegex(namespace["InspectionError"], "RUNNING_SERVICE_AMBIGUOUS"):
                namespace["singleton"](rows, "learn-service")

    def test_a_challenge_with_inherited_labels_cannot_hide_missing_real_core(self):
        namespace, values = self.namespace()
        values.pop(0)
        with self.assertRaisesRegex(namespace["InspectionError"], "COMPOSE_CONTAINERS_MISSING"):
            namespace["containers"]()


if __name__ == "__main__":
    unittest.main(verbosity=2)
