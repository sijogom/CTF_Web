"""Pure Docker-save contracts; no SSH, service, credential, or ACL mutation."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops
import backup_images as images
import release_preflight as preflight
from test_release_backup import NOW, SID, fixtures


def archive_bytes(ids=None, *, extra=(), modern=False, duplicate=False, malicious=None):
    configs = [preflight.canonical({"config": {"Labels": {"release": str(index)}}, "os": "linux", "architecture": "amd64"})
               for index in range(4)]
    identities = ["sha256:" + preflight.sha256(data) for data in configs]
    chosen = ids or identities
    entries, manifest = [], []
    for identity in chosen:
        index = identities.index(identity)
        name = ("blobs/sha256/" + identity[7:]) if modern else (identity[7:] + ".json")
        layer = f"layers/{index}/layer.tar"
        entries.extend([(name, configs[index]), (layer, b"fixture-layer")])
        manifest.append({"Config": name, "RepoTags": [f"fixture:{index}"], "Layers": [layer]})
    entries.append(("manifest.json", preflight.canonical(manifest)))
    entries.extend(extra)
    if duplicate:
        entries.append(("./manifest.json", preflight.canonical(manifest)))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data in entries:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        if malicious:
            item = tarfile.TarInfo(malicious)
            item.type, item.linkname = tarfile.SYMTYPE, "../outside"
            archive.addfile(item)
    rows = [{"image_id": identity, "bytes": 1024, "labels_sha256": preflight.sha256(preflight.canonical({"release": str(identities.index(identity))})),
             "repo_tags_sha256": "b" * 64, "os": "linux", "architecture": "amd64"} for identity in sorted(chosen)]
    return output.getvalue(), sorted(chosen), rows


class ImageBackupContracts(unittest.TestCase):
    def setUp(self):
        parent = ops.TASK / "runtime/work/image-backup-unit-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source, self.migrations, self.plan, _, _, _ = fixtures()
        self.tar, self.ids, self.image_rows = archive_bytes()
        replacements = {}
        for service, identity in zip(sorted(images.CONTEXTS), self.ids):
            row = next(row for row in self.plan["inspection"]["containers"] if row["service"] == service)
            replacements[row["image_id"]] = identity
        for row in self.plan["inspection"]["containers"]:
            row["image_id"] = replacements.get(row["image_id"], row["image_id"])
        self.payload = preflight.canonical(self.plan) + b"\n"
        self.hash = preflight.sha256(self.payload)
        self.snapshot = {key: copy.deepcopy(value) for key, value in self.plan["inspection"].items()
                         if key not in {"objects", "databases"}}
        self.snapshot.update({"kind": "renewal-image-inspection", "images": self.image_rows,
                              "memory_available_bytes": 1024 * 1024 * 1024})

    def rejected(self, code, function, *args, **kwargs):
        with self.assertRaisesRegex(preflight.ReleaseError, code):
            function(*args, **kwargs)

    def acl(self, root):
        attestation = {"schema_version": 1, "root": str(root), "user_sid": SID, "allowed_sids": [SID, "S-1-5-18"],
                       "inheritance_disabled": True, "verified_at": NOW.isoformat()}
        live = {"root": str(root), "user_sid": SID, "owner_sid": SID, "inheritance_disabled": True,
            "entries": [{"sid": sid, "rights": 2032127, "allow": True, "inherited": False,
                         "inheritance_flags": 3, "propagation_flags": 0} for sid in (SID, "S-1-5-18")]}
        return attestation, live

    def test_selection_includes_four_contexts_and_same_image_workers_once(self):
        selected, binding = images.selection(self.plan)
        self.assertEqual(selected, self.ids)
        self.assertRegex(binding, r"^[0-9a-f]{64}$")
        row = next(row for row in self.plan["inspection"]["containers"] if row["service"] == "frontend")
        row["running"] = False
        self.rejected("RUNNING_AMBIGUOUS", images.selection, self.plan)

    def test_snapshot_requires_same_running_containers_labels_and_protected_code(self):
        images.validate_snapshot(self.snapshot, self.plan, self.ids)
        for field in ("compose_sha256", "protected_files", "containers", "code_files"):
            changed = copy.deepcopy(self.snapshot)
            changed[field] = "changed"
            self.rejected("LIVE_STATE", images.validate_snapshot, changed, self.plan, self.ids)
        changed = copy.deepcopy(self.snapshot)
        changed["images"][0]["labels_sha256"] = "invalid"
        self.rejected("METADATA_INVALID", images.validate_snapshot, changed, self.plan, self.ids)

    def test_export_is_rejected_when_minio_is_stopped_or_resources_unverified(self):
        value, plan = copy.deepcopy(self.snapshot), copy.deepcopy(self.plan)
        for data in (value, plan["inspection"]):
            next(row for row in data["containers"] if row["service"] == "minio").update(running=False, pid=0)
        self.rejected("RUNNING_MINIO", images.validate_snapshot, value, plan, self.ids)
        value = copy.deepcopy(self.snapshot); value["memory_available_bytes"] = 0
        self.rejected("RESOURCES_UNVERIFIED", images.validate_snapshot, value, self.plan, self.ids)

    def test_legacy_and_blob_config_paths_are_hash_verified_without_extracting(self):
        for modern in (False, True):
            payload, ids, rows = archive_bytes(modern=modern)
            path = self.root / ("modern.tar" if modern else "legacy.tar")
            path.write_bytes(payload)
            with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("must not extract")):
                checked = images.validate_image_archive(path, ids, rows)
            self.assertTrue(checked["config_matching_verified"])
            self.assertEqual(checked["selected_image_ids"], ids)
            self.assertEqual(checked["file_count"], 9)

    def test_missing_extra_or_tampered_config_fail_closed(self):
        path = self.root / "images.tar"; path.write_bytes(self.tar)
        self.rejected("MANIFEST_SET_MISMATCH", images.validate_image_archive, path, self.ids[:-1])
        changed = self.ids.copy(); changed[0] = "sha256:" + "f" * 64; changed.sort()
        self.rejected("CONFIG_ID_MISMATCH", images.validate_image_archive, path, changed)
        rows = copy.deepcopy(self.image_rows); rows[0]["labels_sha256"] = "0" * 64
        self.rejected("CONFIG_METADATA_CHANGED", images.validate_image_archive, path, self.ids, rows)

    def test_unsafe_duplicate_link_and_hidden_trailing_tar_are_rejected(self):
        path = self.root / "unsafe.tar"
        for name in ("../escape", "/absolute", "C:/drive", "back\\slash"):
            payload, _, _ = archive_bytes(extra=[(name, b"data")]); path.write_bytes(payload)
            self.rejected("PATH_INVALID", images.validate_image_archive, path, self.ids)
        for kwargs, code in (({"duplicate": True}, "DUPLICATE_MEMBER"), ({"malicious": "link"}, "LINK_OR_SPECIAL")):
            payload, _, _ = archive_bytes(**kwargs); path.write_bytes(payload)
            self.rejected(code, images.validate_image_archive, path, self.ids)
        path.write_bytes(self.tar + self.tar)
        self.rejected("TRAILING_PAYLOAD", images.validate_image_archive, path, self.ids)

    def test_remote_program_only_reads_and_public_metadata_excludes_values(self):
        source = images.image_inspector(self.ids)
        compile(source, "image-inspection", "exec")
        for forbidden in ("docker stop", "docker start", "docker build", "docker tag", "docker prune", "image rm", "sudo", "restore"):
            self.assertNotIn(forbidden, source)
        self.assertIn("'labels_sha256'", source)
        self.assertIn("'repo_tags_sha256'", source)
        self.assertNotIn("'environment':", source)

    def test_missing_required_flags_never_reach_ssh(self):
        with patch.object(ops, "remote_python") as remote, patch.object(ops, "streaming_ssh") as stream, patch("sys.stderr", io.StringIO()):
            for argv in ([], ["--plan", "x", "--plan-sha256", "a" * 64, "--acl-attestation", "y"]):
                with self.assertRaises(SystemExit): images.main(argv)
            remote.assert_not_called(); stream.assert_not_called()

    def test_sha_or_source_mismatch_is_rejected_before_acl_or_ssh(self):
        task = self.root / "task"; evidence = task / "evidence"; evidence.mkdir(parents=True)
        path = evidence / "plan.json"; path.write_bytes(self.payload)
        with patch.object(ops, "TASK", task), patch.object(ops, "remote_python") as remote, patch.object(images, "read_acl") as acl:
            self.rejected("SHA256_MISMATCH", images.execute, path, "0" * 64, evidence / "acl", execute_approved=True)
            with patch.object(ops, "manifest", return_value={"fingerprint": "0" * 64}):
                self.rejected("SOURCE_FINGERPRINT", images.execute, path, self.hash, evidence / "acl", execute_approved=True)
            remote.assert_not_called(); acl.assert_not_called()

    def test_receipt_rejects_foreign_binding_unverified_capture_and_private_path_escape(self):
        private = self.root / "runtime/private-backups"; private.mkdir(parents=True)
        archive = private / (self.plan["nonce"] + "-images") / "old-release-images.tar"
        archive.parent.mkdir(); archive.write_bytes(self.tar)
        receipt = {"schema_version": 1, "kind": "renewal-image-backup", "task": preflight.TASK_ID,
            "nonce": self.plan["nonce"], "plan_sha256": self.hash, "source_fingerprint": self.source["fingerprint"],
            "source_file_count": len(self.source["files"]), "target": self.plan["target"],
            "started_at": NOW.isoformat(), "captured_at": NOW.isoformat(), "selected_image_ids": self.ids,
            "container_binding_sha256": images.selection(self.plan)[1], "images_metadata_sha256": "a" * 64,
            "acl_sha256": "b" * 64, "archive": {"path": str(archive), "bytes": len(self.tar),
                "sha256": preflight.sha256(self.tar), "file_count": 9}, "capture_verified": True,
            "config_matching_verified": True, "restore_verified": False, "remote_temporary_bytes": 0,
            "image_identity_mode": "legacy-config-digest", "digest_chain_verified": False,
            "service_mutations_performed": False, "tag_mutations_performed": False}
        with patch.object(images, "PRIVATE_ROOT", private):
            images.validate_receipt(receipt, self.plan, self.hash)
            for key, bad in (("nonce", "foreign"), ("source_fingerprint", "0" * 64),
                             ("plan_sha256", "0" * 64), ("selected_image_ids", self.ids[:-1])):
                value = copy.deepcopy(receipt); value[key] = bad
                self.rejected("BINDING", images.validate_receipt, value, self.plan, self.hash)
            for key, bad in (("restore_verified", True), ("capture_verified", False), ("remote_temporary_bytes", 1)):
                value = copy.deepcopy(receipt); value[key] = bad
                self.rejected("CAPTURE_UNVERIFIED", images.validate_receipt, value, self.plan, self.hash)
            value = copy.deepcopy(receipt); value["archive"]["path"] = str(self.root / "outside.tar")
            self.rejected("PATH_ESCAPE", images.validate_receipt, value, self.plan, self.hash)
            value = copy.deepcopy(receipt); value["captured_at"] = (NOW + timedelta(days=1)).isoformat()
            self.rejected("TIME_INVALID", images.validate_receipt, value, self.plan, self.hash)

    def run_execute(self, *, fail=False, changed=False, low_space=False):
        task = self.root / "task"; evidence = task / "evidence"; evidence.mkdir(parents=True)
        private = task / "runtime/private-backups"; private.mkdir(parents=True)
        path = evidence / "plan.json"; path.write_bytes(self.payload)
        attestation, live = self.acl(private)
        acl = evidence / "acl.json"; acl.write_bytes(preflight.canonical(attestation))
        after = copy.deepcopy(self.snapshot)
        if changed:
            after["images"][0]["repo_tags_sha256"] = "f" * 64
        def stream(command, target, **kwargs):
            self.assertEqual(shlex_split(command)[:3], ["docker", "image", "save"])
            self.assertEqual(shlex_split(command)[3:], self.ids)
            with target.open("xb") as output:
                output.write(b"partial" if fail else self.tar)
            if fail:
                raise RuntimeError("SSH_STREAM_TIMEOUT_PARTIAL_RETAINED")
            return {"bytes": len(self.tar), "sha256": preflight.sha256(self.tar)}
        with patch.object(ops, "TASK", task), patch.object(images, "PRIVATE_ROOT", private), \
                patch.object(ops, "manifest", return_value=self.source), patch.object(images, "source_migrations", return_value=self.migrations), \
                patch.object(images, "read_acl", return_value=live), patch.object(ops, "remote_python", side_effect=[self.snapshot, after]), \
                patch.object(ops, "streaming_ssh", side_effect=stream) as streaming, \
                patch.object(images.shutil, "disk_usage", return_value=type("Space", (), {"free": 0 if low_space else 10**12})()):
            if low_space:
                self.rejected("SPACE_INSUFFICIENT", images.execute, path, self.hash, acl, execute_approved=True)
                streaming.assert_not_called()
            elif fail:
                with self.assertRaisesRegex(RuntimeError, "TIMEOUT_PARTIAL"):
                    images.execute(path, self.hash, acl, execute_approved=True)
                self.assertEqual((private / (self.plan["nonce"] + "-images") / "old-release-images.tar").read_bytes(), b"partial")
            elif changed:
                self.rejected("METADATA_CHANGED", images.execute, path, self.hash, acl, execute_approved=True)
            else:
                result = images.execute(path, self.hash, acl, execute_approved=True)
                receipt = preflight.parse_json(Path(result["receipt"]).read_bytes())
                images.validate_receipt(receipt, self.plan, self.hash)
                self.assertTrue(receipt["capture_verified"] and receipt["config_matching_verified"])
                self.assertFalse(receipt["restore_verified"] or receipt["service_mutations_performed"] or receipt["tag_mutations_performed"])
                self.assertNotIn('"Labels"', json.dumps(receipt))
                self.rejected("NONCE_REUSED", images.execute, path, self.hash, acl, execute_approved=True)
                Path(receipt["archive"]["path"]).write_bytes(b"tampered")
                self.rejected("SIZE_CHANGED", images.validate_receipt, receipt, self.plan, self.hash)
            if fail or changed or low_space:
                self.assertFalse(list(evidence.glob("image-backup-complete-*")))

    def test_mocked_capture_is_exclusive_and_archive_is_rechecked(self):
        self.run_execute()

    def test_partial_failure_is_preserved_without_success_receipt(self):
        self.run_execute(fail=True)

    def test_metadata_tag_drift_prevents_success_receipt(self):
        self.run_execute(changed=True)

    def test_insufficient_local_space_never_starts_stream(self):
        self.run_execute(low_space=True)


def shlex_split(command):
    import shlex
    return shlex.split(command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
