"""Pure local contracts only: every SSH/process/ACL query is mocked."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops
import release_preflight as preflight
import backup_release as backup
import backup_images


NOW = datetime.now(timezone.utc)
SID = "S-1-5-21-100-200-300-1001"


def tar_bytes(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def fixtures():
    migrations = {name: {"head": "rev_1", "revisions": ["rev_0", "rev_1"]} for name in preflight.DATABASES}
    code = [{"path": "README.md", "bytes": 4, "sha256": preflight.sha256(b"code")}]
    protected = [{"path": f"{ops.LIVE}/{name}", "bytes": 7, "sha256": preflight.sha256(b"private")}
                 for name in ("infra/.env", "infra/caddy/Caddyfile", "infra/docker-compose.prod.yml", "infra/docker-compose.yml")]
    object_rows = [[preflight.sha256(b"object"), 4, preflight.sha256(b"data")]]
    objects = {"file_count": 1, "bytes": 4, "inventory_sha256": preflight.sha256(json.dumps(object_rows, separators=(",", ":")).encode())}
    services = ["postgres", "minio", "auth-service", "learn-service", "community-service", "frontend", "auth-personal-notifications"]
    containers = []
    for index, service in enumerate(services):
        image_index = 3 if service == "auth-personal-notifications" else index + 1
        containers.append({"service": service, "container_id": format(index + 1, "064x"), "image_id": "sha256:" + format(image_index, "064x"),
            "running": True, "pid": index + 100, "status": "running", "started_at": "2026-10-13T01:00:00Z",
            "finished_at": "0001-01-01T00:00:00Z", "restart_count": 0, "health": "healthy",
            "mounts": [{"type": "volume", "source": "/var/lib/docker/volumes/minio/_data", "destination": "/data", "read_only": False}] if service == "minio" else []})
    inspection = {"schema_version": 1, "verified": True, "live_root": ops.LIVE, "project": preflight.PROJECT,
        "compose_sha256": "c" * 64, "containers": sorted(containers, key=lambda row: (row["service"], row["container_id"])),
        "protected_files": protected, "code_files": code, "objects": objects, "disk_available_bytes": 1000,
        "databases": {name: {"database": database, "head": ["rev_0"], "bytes": 1024, "table_count": 2,
            "client_session_count": 1, "schema_sha256": "d" * 64} for name, database in preflight.DATABASES.items()}}
    source = {"fingerprint": "f" * 64, "files": [{"path": "README.md", "bytes": 4, "sha256": "a" * 64}]}
    plan = preflight.make_plan(source, inspection, migrations, now=NOW, nonce=uuid4())
    frozen = copy.deepcopy(inspection)
    for row in frozen["containers"]:
        if row["service"] in {"minio", "auth-service", "learn-service", "community-service", "auth-personal-notifications"}:
            row.update({"running": False, "pid": 0, "status": "exited", "finished_at": "2026-10-13T02:00:00Z"})
    for row in frozen["databases"].values():
        row["client_session_count"] = 0
    payload = preflight.canonical(plan) + b"\n"
    quiescence = {"schema_version": 1, "kind": "renewal-quiescence", "task": preflight.TASK_ID,
        "nonce": plan["nonce"], "plan_sha256": preflight.sha256(payload), "source_fingerprint": source["fingerprint"],
        "verified_at": NOW.isoformat(), "drain_verified": True, "external_writers_absent_verified": True,
        "fence_maintained_until_capture_complete": True, "inspection": frozen}
    fence = {key: value for key, value in frozen.items() if key not in {"objects", "disk_available_bytes"}}
    fence["kind"] = "renewal-fence-inspection"
    return source, migrations, plan, payload, quiescence, fence


class ReleaseContracts(unittest.TestCase):
    def test_acl_reader_limits_windows_powershell_modules_to_its_child(self):
        original = "unrelated-powershell-7-module-path"
        result = subprocess.CompletedProcess([], 0, stdout=b'{"verified":true}', stderr=b"")
        with patch.dict(backup.os.environ, {"PSModulePath": original}), patch.object(backup.subprocess, "run", return_value=result) as run:
            self.assertEqual(backup.read_acl(backup.PRIVATE_ROOT), {"verified": True})
            child = run.call_args.kwargs["env"]
            system = Path(backup.os.environ.get("SystemRoot", r"C:\Windows"))
            self.assertEqual(child["PSModulePath"], str(system / "System32/WindowsPowerShell/v1.0/Modules"))
            self.assertEqual(backup.os.environ["PSModulePath"], original)

    def setUp(self):
        parent = ops.TASK / "runtime" / "work" / "release-unit-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source, self.migrations, self.plan, self.payload, self.quiescence, self.fence = fixtures()

    def rejected(self, code, function, *args, **kwargs):
        with self.assertRaisesRegex(preflight.ReleaseError, code):
            function(*args, **kwargs)

    def test_plan_accepts_current_source_and_exact_three_databases(self):
        preflight.validate_plan(self.plan, self.source["fingerprint"], now=NOW)
        self.assertEqual(self.plan["target"]["port"], 10022)
        self.assertNotIn("ctfd_db", self.plan["backup_scope"]["databases"])

    def test_stale_foreign_nonce_source_and_unknown_heads_fail_closed(self):
        for field, bad, code in (("task", "2026-09-08-acceptance", "PLAN_SCHEMA"),
            ("nonce", "old-receipt", "NONCE"), ("created_at", (NOW - timedelta(hours=1)).isoformat(), "EXPIRED")):
            value = copy.deepcopy(self.plan); value[field] = bad
            self.rejected(code, preflight.validate_plan, value, self.source["fingerprint"], now=NOW)
        self.rejected("SOURCE_FINGERPRINT", preflight.validate_plan, self.plan, "0" * 64, now=NOW)
        self.plan["inspection"]["databases"]["auth"]["head"] = ["unknown"]
        self.rejected("HEAD_UNKNOWN", preflight.validate_plan, self.plan, self.source["fingerprint"], now=NOW)

    def test_unknown_private_fields_and_ctfd_scope_are_rejected(self):
        self.plan["inspection"]["environment"] = {"SECRET": "not-public"}
        self.rejected("UNKNOWN_FIELDS", preflight.validate_plan, self.plan, self.source["fingerprint"], now=NOW)
        self.plan["inspection"].pop("environment")
        self.plan["backup_scope"]["databases"].append("ctfd_db")
        self.rejected("BACKUP_SCOPE", preflight.validate_plan, self.plan, self.source["fingerprint"], now=NOW)

    def test_json_duplicate_keys_and_exclusive_receipts(self):
        self.rejected("DUPLICATE_JSON", preflight.parse_json, b'{"x":1,"x":2}')
        path = self.root / "receipt.json"
        preflight.exclusive_json(path, {"hash": "a" * 64})
        with self.assertRaises(FileExistsError): preflight.exclusive_json(path, {})
        self.assertEqual(preflight.parse_json(path.read_bytes()), {"hash": "a" * 64})

    def test_preflight_unverified_contract_or_source_race_writes_no_plan(self):
        destination = self.root / "evidence/plan.json"
        with patch.object(ops, "TASK", self.root), patch.object(ops, "manifest", return_value=self.source), \
                patch.object(preflight, "source_migrations", return_value=self.migrations), \
                patch.object(ops, "remote_python", return_value={"schema_version": 1, "verified": False, "unverified_stage": "protected"}):
            self.rejected("UNVERIFIED_PROTECTED", preflight.main, ["--plan", str(destination)])
        self.assertFalse(destination.exists())
        with patch.object(ops, "TASK", self.root), patch.object(ops, "manifest", side_effect=[self.source, {"fingerprint": "0" * 64}]), \
                patch.object(preflight, "source_migrations", return_value=self.migrations), \
                patch.object(ops, "remote_python", return_value=self.plan["inspection"]):
            self.rejected("SOURCE_CHANGED_DURING", preflight.main, ["--plan", str(destination)])
        self.assertFalse(destination.exists())

    def test_source_graph_is_literal_complete_and_single_head(self):
        for service in preflight.DATABASES:
            root = self.root / "services" / service / "migrations" / "versions"
            root.mkdir(parents=True)
            (root / "0.py").write_text("revision='r0'\ndown_revision=None\n")
            (root / "1.py").write_text("revision: str='r1'\ndown_revision='r0'\n")
        graph = preflight.source_migrations(self.root)
        self.assertTrue(all(row["head"] == "r1" for row in graph.values()))
        (self.root / "services/auth/migrations/versions/2.py").write_text("revision='r2'\ndown_revision='r0'\n")
        self.rejected("HEAD_AMBIGUOUS", preflight.source_migrations, self.root)

    def test_remote_programs_compile_and_have_no_mutation_primitives(self):
        for source in (preflight.INSPECTOR, preflight.FENCE_INSPECTOR):
            compile(source, "remote-inspector", "exec")
            for forbidden in ("docker prune", "docker system prune", "ALTER TABLE", "DROP TABLE", "docker stop", "docker start", "shutil.rmtree"):
                self.assertNotIn(forbidden, source)
        commands = backup.stream_specs(self.plan)
        self.assertEqual(len(commands), 6)
        self.assertEqual([row["name"] for row in commands[:3]], [database + ".dump" for database in preflight.DATABASES.values()])
        self.assertTrue(all("ctfd_db" not in row["command"] for row in commands))
        self.assertIn("docker cp", commands[3]["command"])
        self.assertNotIn("docker save", " ".join(row["command"] for row in commands))
        with patch.object(backup, "remote_program", side_effect=lambda source: source):
            for kind in ("code", "protected"):
                compile(backup.archive_command(kind), "remote-archive", "exec")

    def remote_key_reader(self, payload, *, readonly=True):
        namespace = {}
        exec(preflight.REMOTE_HELPERS, namespace)
        source = namespace["ROOT"] / "services/auth/jwt_private.pem"
        namespace["containers"] = lambda: [{"service": "auth-service", "container_id": "a" * 64, "running": False,
            "mounts": [{"type": "bind", "source": str(source), "destination": "/run/secrets/auth_jwt_private_key", "read_only": readonly}]}]
        process = Mock(); process.stdout = io.BytesIO(payload); process.wait.return_value = 0; process.poll.return_value = 0
        return namespace, source, process

    def test_protected_key_fallback_is_bounded_existing_readonly_mount_and_stopped_safe(self):
        data = b"bounded-fixture"
        namespace, source, process = self.remote_key_reader(tar_bytes([("auth_jwt_private_key", data)]))
        with patch.object(subprocess, "Popen", return_value=process) as spawn, patch.object(namespace["threading"], "Timer"):
            self.assertEqual(namespace["protected_key_bytes"](source), data)
            self.assertEqual(spawn.call_args.args[0], ["docker", "cp", "a" * 64 + ":/run/secrets/auth_jwt_private_key", "-"])
            self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)
        namespace, source, process = self.remote_key_reader(tar_bytes([("auth_jwt_private_key", data)]), readonly=False)
        with patch.object(subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(namespace["InspectionError"], "MOUNT_UNVERIFIED"):
                namespace["protected_key_bytes"](source)
            with self.assertRaisesRegex(namespace["InspectionError"], "SCOPE_FORBIDDEN"):
                namespace["protected_key_bytes"](namespace["ROOT"] / "infra/.env")
            spawn.assert_not_called()

    def test_protected_fallback_rejects_large_multiple_or_nonregular_tar(self):
        for payload, code in ((b"x" * (2 * 1024 * 1024 + 1), "TAR_TOO_LARGE"),
            (tar_bytes([("auth_jwt_private_key", b"x" * (1024 * 1024 + 1))]), "MEMBER_INVALID"),
            (tar_bytes([("auth_jwt_private_key", b"a"), ("extra", b"b")]), "SINGLE_FILE_REQUIRED"),
            (tar_bytes([("../auth_jwt_private_key", b"a")]), "MEMBER_INVALID")):
            namespace, source, process = self.remote_key_reader(payload)
            with patch.object(subprocess, "Popen", return_value=process), patch.object(namespace["threading"], "Timer"):
                with self.assertRaisesRegex(namespace["InspectionError"], code): namespace["protected_key_bytes"](source)

    def test_direct_protected_read_precedes_fallback_and_only_permission_denied_uses_it(self):
        namespace = {}; exec(preflight.REMOTE_HELPERS, namespace)
        path = namespace["ROOT"] / "services/auth/jwt_private.pem"
        fallback = Mock(return_value=b"fixture")
        namespace["protected_key_bytes"] = fallback
        namespace["file_row"] = Mock(return_value={"path": str(path), "bytes": 3, "sha256": "a" * 64})
        self.assertEqual(namespace["protected_row"](path)["bytes"], 3); fallback.assert_not_called()
        namespace["file_row"].side_effect = PermissionError()
        self.assertEqual(namespace["protected_row"](path)["sha256"], preflight.sha256(b"fixture")); fallback.assert_called_once_with(path)
        fallback.reset_mock(); namespace["file_row"].side_effect = FileNotFoundError()
        with self.assertRaises(FileNotFoundError): namespace["protected_row"](path)
        fallback.assert_not_called()

    def test_quiescence_requires_same_nonce_and_all_writers_and_minio_stopped(self):
        backup.validate_quiescence(self.quiescence, self.plan, preflight.sha256(self.payload), now=NOW)
        for field in ("nonce", "plan_sha256", "source_fingerprint"):
            value = copy.deepcopy(self.quiescence); value[field] = "changed"
            self.rejected("BINDING", backup.validate_quiescence, value, self.plan, preflight.sha256(self.payload), now=NOW)
        for service in ("minio", "auth-personal-notifications", "learn-service"):
            value = copy.deepcopy(self.quiescence)
            row = next(row for row in value["inspection"]["containers"] if row["service"] == service)
            row.update({"running": True, "pid": 999, "status": "running"})
            self.rejected("NOT_STOPPED", backup.validate_quiescence, value, self.plan, preflight.sha256(self.payload), now=NOW)

    def test_quiescence_allows_pre_stop_object_changes_but_not_schema_or_other_services(self):
        value = copy.deepcopy(self.quiescence)
        value["inspection"]["objects"]["inventory_sha256"] = "e" * 64
        backup.validate_quiescence(value, self.plan, preflight.sha256(self.payload), now=NOW)
        value["inspection"]["databases"]["learn"]["schema_sha256"] = "e" * 64
        self.rejected("STATIC_STATE", backup.validate_quiescence, value, self.plan, preflight.sha256(self.payload), now=NOW)
        value = copy.deepcopy(self.quiescence)
        next(row for row in value["inspection"]["containers"] if row["service"] == "frontend")["pid"] = 999
        self.rejected("UNRELATED_CONTAINER", backup.validate_quiescence, value, self.plan, preflight.sha256(self.payload), now=NOW)

    def test_fence_rejects_clients_restarts_or_minio_start(self):
        backup.check_fence(self.fence, self.quiescence["inspection"], self.migrations)
        changed = copy.deepcopy(self.fence); changed["databases"]["auth"]["client_session_count"] = 1
        self.rejected("CLIENTS_REMAIN", backup.check_fence, changed, self.quiescence["inspection"], self.migrations)
        changed = copy.deepcopy(self.fence)
        next(row for row in changed["containers"] if row["service"] == "minio")["restart_count"] += 1
        self.rejected("FENCE_CHANGED", backup.check_fence, changed, self.quiescence["inspection"], self.migrations)

    def test_archive_hashes_members_without_extracting_and_rejects_traversal_duplicates(self):
        archive = self.root / "objects.tar"
        archive.write_bytes(tar_bytes([("object", b"data")]))
        self.assertEqual(backup.validate_archive(archive, self.plan["inspection"]["objects"], "objects"), 1)
        archive.write_bytes(tar_bytes([("../escape", b"data")]))
        self.rejected("UNSAFE_ARCHIVE", backup.archive_rows, archive)
        archive.write_bytes(tar_bytes([("object", b"data"), ("./object", b"data")]))
        self.rejected("DUPLICATE_ARCHIVE", backup.archive_rows, archive)
        archive.write_bytes(tar_bytes([("object", b"fail")]))
        self.rejected("OBJECT_INVENTORY_CHANGED", backup.validate_archive, archive, self.plan["inspection"]["objects"], "objects")

    def acl(self, root):
        attestation = {"schema_version": 1, "root": str(root), "user_sid": SID, "allowed_sids": [SID, backup.SYSTEM_SID], "inheritance_disabled": True, "verified_at": NOW.isoformat()}
        live = {"root": str(root), "user_sid": SID, "owner_sid": SID, "inheritance_disabled": True,
            "entries": [{"sid": sid, "rights": 2032127, "allow": True, "inherited": False, "inheritance_flags": 3, "propagation_flags": 0} for sid in [SID, backup.SYSTEM_SID]]}
        return attestation, live

    def test_acl_requires_live_two_exact_principals_and_no_inheritance(self):
        attestation, live = self.acl(self.root)
        backup.validate_acl(attestation, live, root=self.root, now=NOW)
        for field, changed in (("inheritance_disabled", False), ("owner_sid", "S-1-1-0")):
            value = copy.deepcopy(live); value[field] = changed
            self.rejected("ACL_", backup.validate_acl, attestation, value, root=self.root, now=NOW)
        live["entries"].append({**live["entries"][0], "sid": "S-1-1-0"})
        self.rejected("EXTRA_OR_MISSING", backup.validate_acl, attestation, live, root=self.root, now=NOW)

    def test_missing_execution_inputs_never_invoke_ssh(self):
        with patch.object(ops, "remote_python") as remote, patch.object(ops, "streaming_ssh") as stream, patch("sys.stderr", io.StringIO()):
            for args in ([], ["--plan", "x"], ["--plan", "x", "--plan-sha256", "a" * 64, "--acl-attestation", "y", "--execute"]):
                with self.assertRaises(SystemExit): backup.main(args)
            remote.assert_not_called(); stream.assert_not_called()

    def test_sha_and_source_mismatch_stop_before_acl_or_ssh(self):
        evidence = self.root / "evidence"; evidence.mkdir()
        path = evidence / "plan.json"; path.write_bytes(self.payload)
        with patch.object(ops, "TASK", self.root), patch.object(ops, "remote_python") as remote, \
                patch.object(ops, "streaming_ssh") as stream, patch.object(backup, "read_acl") as acl:
            self.rejected("SHA256_MISMATCH", backup.execute, path, "0" * 64, evidence / "acl", evidence / "q",
                image_receipt_path=evidence / "images", image_receipt_sha256="a" * 64, execute_approved=True)
            with patch.object(ops, "manifest", return_value={"fingerprint": "0" * 64}):
                self.rejected("SOURCE_FINGERPRINT", backup.execute, path, preflight.sha256(self.payload), evidence / "acl", evidence / "q",
                    image_receipt_path=evidence / "images", image_receipt_sha256="a" * 64, execute_approved=True)
            remote.assert_not_called(); stream.assert_not_called(); acl.assert_not_called()

    def test_image_receipt_hash_and_pre_quiescence_order_are_required_before_ssh(self):
        task = self.root / "image-gate-task"; evidence = task / "evidence"; evidence.mkdir(parents=True)
        plan = evidence / "plan.json"; plan.write_bytes(self.payload)
        q = evidence / "quiescence.json"; q.write_bytes(preflight.canonical(self.quiescence))
        image = evidence / "image.json"
        image.write_bytes(preflight.canonical({"captured_at": (NOW + timedelta(seconds=1)).isoformat()}))
        with patch.object(ops, "TASK", task), patch.object(ops, "manifest", return_value=self.source), \
                patch.object(backup, "source_migrations", return_value=self.migrations), \
                patch.object(backup_images, "validate_receipt", return_value={}), \
                patch.object(ops, "remote_python") as remote, patch.object(backup, "read_acl") as acl:
            kwargs = {"image_receipt_path": image, "execute_approved": True}
            self.rejected("IMAGE_RECEIPT_SHA256_MISMATCH", backup.execute, plan, preflight.sha256(self.payload),
                evidence / "acl", q, image_receipt_sha256="0" * 64, **kwargs)
            self.rejected("MUST_PRECEDE_QUIESCENCE", backup.execute, plan, preflight.sha256(self.payload),
                evidence / "acl", q, image_receipt_sha256=preflight.sha256(image.read_bytes()), **kwargs)
            remote.assert_not_called(); acl.assert_not_called()

    def run_execute(self, failing_stream=False):
        task = self.root / "task"; private = task / "runtime/private-backups"
        private.mkdir(parents=True)
        evidence = task / "evidence"; evidence.mkdir()
        plan_path = evidence / "plan.json"; plan_path.write_bytes(self.payload)
        q_path = evidence / "quiescence.json"; q_path.write_bytes(preflight.canonical(self.quiescence))
        attestation, live = self.acl(private)
        acl_path = evidence / "acl.json"; acl_path.write_bytes(preflight.canonical(attestation))
        image_directory = private / (self.plan["nonce"] + "-images"); image_directory.mkdir()
        image_path = image_directory / "old-release-images.tar"; image_path.write_bytes(b"image-capture-fixture")
        image_row = {"path": str(image_path), "bytes": image_path.stat().st_size,
                     "sha256": preflight.sha256(image_path.read_bytes()), "file_count": 1}
        image_receipt = {"captured_at": NOW.isoformat(), "acl_sha256": preflight.sha256(preflight.canonical(live)),
                         "selected_image_ids": backup_images.selection(self.plan)[0]}
        image_receipt_path = evidence / "images.json"; image_receipt_path.write_bytes(preflight.canonical(image_receipt))
        image_kwargs = {"image_receipt_path": image_receipt_path,
                        "image_receipt_sha256": preflight.sha256(image_receipt_path.read_bytes())}
        def stream(command, destination, **kwargs):
            if failing_stream:
                destination.write_bytes(b"partial")
                raise RuntimeError("BACKUP_STREAM_FAILED_PARTIAL_RETAINED")
            if destination.suffix == ".dump": data = b"PGDMP" + b"fixture-dump" * 4
            elif destination.name == "minio-objects.tar": data = tar_bytes([("object", b"data")])
            elif destination.name == "live-code.tar": data = tar_bytes([("README.md", b"code")])
            else: data = tar_bytes([(f"protected/{index:06d}", b"private") for index in range(4)])
            with destination.open("xb") as output: output.write(data)
            return {"bytes": len(data), "sha256": preflight.sha256(data)}
        with patch.object(ops, "TASK", task), patch.object(backup, "PRIVATE_ROOT", private), patch.object(ops, "manifest", return_value=self.source), \
                patch.object(backup, "source_migrations", return_value=self.migrations), patch.object(backup, "read_acl", return_value=live), \
                patch.object(ops, "remote_python", return_value=self.fence) as remote, patch.object(ops, "streaming_ssh", side_effect=stream), \
                patch.object(backup_images, "validate_receipt", return_value=image_row) as validate_images:
            if failing_stream:
                with self.assertRaisesRegex(RuntimeError, "BACKUP_STREAM_FAILED_PARTIAL_RETAINED"):
                    backup.execute(plan_path, preflight.sha256(self.payload), acl_path, q_path, **image_kwargs, execute_approved=True)
                self.assertFalse(list(evidence.glob("backup-complete-*")))
                self.assertEqual((private / self.plan["nonce"] / "platform_db.dump").read_bytes(), b"partial")
            else:
                result = backup.execute(plan_path, preflight.sha256(self.payload), acl_path, q_path, **image_kwargs, execute_approved=True)
                receipt = json.loads(Path(result["receipt"]).read_bytes())
                self.assertTrue(receipt["quiescence_verified"] and receipt["release_eligible"])
                self.assertFalse(receipt["restore_verified"] or receipt["ctfd_db_included"])
                self.assertEqual(receipt["remote_temporary_bytes"], 0)
                self.assertTrue(receipt["image_backup_verified"])
                self.assertIn(image_row, receipt["files"])
                self.assertEqual(receipt["image_backup_receipt_sha256"], image_kwargs["image_receipt_sha256"])
                self.assertEqual(validate_images.call_count, 2)
                self.assertEqual(remote.call_count, 8)
                self.assertNotIn('"private"', json.dumps(receipt))
                with self.assertRaises(preflight.ReleaseError):
                    backup.execute(plan_path, preflight.sha256(self.payload), acl_path, q_path, **image_kwargs, execute_approved=True)

    def test_mocked_capture_is_nonce_exclusive_and_public_receipt_has_no_payload(self):
        self.run_execute()

    def test_partial_stream_is_preserved_without_success_receipt(self):
        self.run_execute(failing_stream=True)

    def test_strict_ssh_argv_has_no_agent_or_unknown_host_fallback(self):
        key, known = self.root / "fixture-key", self.root / "known-hosts"
        key.write_bytes(b"fixture"); known.write_bytes(b"fixture")
        with patch.object(ops, "HOST", "ctf.example.com"), patch.object(ops, "KEY", key), patch.object(ops, "KNOWN_HOSTS", known):
            argv = ops.ssh_invocation("true")
        for option in ("BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none", "AddKeysToAgent=no", "StrictHostKeyChecking=yes"):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index("-p") + 1], "10022")
        self.assertEqual(argv[-2], "deploy@ctf.example.com")

    def test_stream_escape_no_overwrite_timeout_and_stderr_discard(self):
        task = self.root / "stream-task"; private = task / "runtime/private-backups"
        private.mkdir(parents=True)
        with patch.object(ops, "TASK", task), patch.object(ops, "ssh_invocation", return_value=["mock-ssh"]) as argv, patch.object(ops.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "PATH_ESCAPE"):
                ops.streaming_ssh("command", self.root / "escape")
            present = private / "existing"; present.write_bytes(b"preserve")
            with self.assertRaises(FileExistsError): ops.streaming_ssh("command", present)
            popen.assert_not_called()
            process = Mock(); process.wait.side_effect = [subprocess.TimeoutExpired("mock", 1), 0]; popen.return_value = process
            partial = private / "partial"
            with self.assertRaisesRegex(RuntimeError, "TIMED_OUT_PARTIAL"):
                ops.streaming_ssh("command", partial, timeout=1)
            process.kill.assert_called_once()
            self.assertTrue(partial.exists())
            self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(present.read_bytes(), b"preserve")

    def test_stream_link_is_rejected_before_process_creation(self):
        task = self.root / "link-task"; private = task / "runtime/private-backups"
        private.mkdir(parents=True)
        destination = private / "linked"
        with patch.object(ops, "TASK", task), patch.object(ops, "linked", side_effect=lambda path: path == destination), patch.object(ops.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "LINK_FORBIDDEN"):
                ops.streaming_ssh("command", destination)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
