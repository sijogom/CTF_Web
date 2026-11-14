"""Content-addressed OCI export fixtures; no credentials or Docker process."""
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_images as backup
import ops


INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
ATTEST = "application/vnd.in-toto+json"


def fixture(fault=None):
    files = {}
    def blob(payload, media):
        digest = "sha256:" + backup.sha256(payload)
        files["blobs/sha256/" + digest[7:]] = payload
        return {"digest": digest, "size": len(payload), "mediaType": media}
    def document(value, media):
        return blob(backup.canonical(value), media)
    labels = {"fixture": "not-a-credential"}
    config = document({"os": "linux", "architecture": "amd64", "config": {"Labels": labels}}, CONFIG)
    layer = blob(b"opaque-fixture-layer", LAYER)
    if fault == "size":
        layer["size"] += 1
    runtime = document({"schemaVersion": 2, "mediaType": MANIFEST, "config": config, "layers": [layer]}, MANIFEST)
    runtime["platform"] = {"os": "linux", "architecture": "amd64"}
    attestation_config = document({"os": "unknown", "architecture": "unknown", "config": {}}, CONFIG)
    attestation_layer = blob(b'{"predicateType":"fixture"}', ATTEST)
    attestation = document({"schemaVersion": 2, "mediaType": MANIFEST, "config": attestation_config, "layers": [attestation_layer]}, MANIFEST)
    attestation["platform"] = {"os": "unknown", "architecture": "unknown"}
    attestation["annotations"] = {"vnd.docker.reference.type": "attestation-manifest",
                                  "vnd.docker.reference.digest": "sha256:" + "0" * 64 if fault == "target" else runtime["digest"]}
    root = document({"schemaVersion": 2, "mediaType": INDEX, "manifests": [runtime, attestation]}, INDEX)
    files["index.json"] = backup.canonical({"schemaVersion": 2, "mediaType": INDEX, "manifests": [root]})
    files["oci-layout"] = backup.canonical({"imageLayoutVersion": "1.0.0"})
    files["manifest.json"] = backup.canonical([{"Config": "blobs/sha256/" + config["digest"][7:], "RepoTags": [],
                                              "Layers": ["blobs/sha256/" + layer["digest"][7:]]}])
    if fault == "hash":
        files["blobs/sha256/" + layer["digest"][7:]] = b"X" + b"opaque-fixture-layer"[1:]
    if fault == "missing":
        del files["blobs/sha256/" + layer["digest"][7:]]
    if fault == "unreferenced":
        blob(b"unreferenced", LAYER)
    if fault == "docker-layer":
        entry = json.loads(files["manifest.json"])
        entry[0]["Layers"] = ["blobs/sha256/" + attestation_layer["digest"][7:]]
        files["manifest.json"] = backup.canonical(entry)
    ids = [root["digest"]]
    images = [{"image_id": root["digest"], "os": "linux", "architecture": "amd64", "labels_sha256": backup.sha256(backup.canonical(labels))}]
    return files, ids, images


class OCIArchiveContracts(unittest.TestCase):
    def setUp(self):
        parent = ops.TASK / "runtime/work/oci-image-backup-unit-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def archive(self, files):
        path = self.root / "fixture.tar"
        with tarfile.open(path, "w") as archive:
            for name, payload in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        return path

    def test_selected_index_digest_reaches_runtime_and_attestation_bytes(self):
        files, ids, images = fixture()
        result = backup.validate_image_archive(self.archive(files), ids, images)
        self.assertTrue(result["digest_chain_verified"])
        self.assertEqual(result["image_identity_mode"], "oci-digest-chain")
        self.assertEqual(result["selected_image_ids"], ids)
        self.assertEqual(result["runnable_config_count"], 1)
        self.assertEqual(result["attestation_manifest_count"], 1)

    def test_content_size_missing_and_unreferenced_blobs_fail_closed(self):
        for fault, code in (("hash", "BLOB_HASH"), ("size", "BLOB_MISSING_OR_SIZE"),
                            ("missing", "LAYER_MISSING"), ("unreferenced", "UNREFERENCED_FILES")):
            with self.subTest(fault=fault):
                files, ids, images = fixture(fault)
                with self.assertRaisesRegex(backup.ReleaseError, code):
                    backup.validate_image_archive(self.archive(files), ids, images)

    def test_attestation_must_reference_runtime_in_same_selected_root(self):
        files, ids, images = fixture("target")
        with self.assertRaisesRegex(backup.ReleaseError, "ATTESTATION_TARGET"):
            backup.validate_image_archive(self.archive(files), ids, images)

    def test_docker_runtime_layer_list_cannot_point_to_other_verified_blob(self):
        files, ids, images = fixture("docker-layer")
        with self.assertRaisesRegex(backup.ReleaseError, "DOCKER_LAYER"):
            backup.validate_image_archive(self.archive(files), ids, images)

    def test_selected_image_set_and_inspected_labels_are_independent_fences(self):
        files, ids, images = fixture()
        path = self.archive(files)
        with self.assertRaisesRegex(backup.ReleaseError, "SELECTED_SET"):
            backup.validate_image_archive(path, ["sha256:" + "f" * 64], images)
        changed = copy.deepcopy(images)
        changed[0]["labels_sha256"] = "0" * 64
        with self.assertRaisesRegex(backup.ReleaseError, "METADATA_CHANGED"):
            backup.validate_image_archive(path, ids, changed)


if __name__ == "__main__":
    unittest.main()
