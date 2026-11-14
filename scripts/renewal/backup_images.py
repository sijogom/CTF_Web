"""Capture immutable running release images before service/MinIO quiescence.

This tool never builds, tags, stops, starts, restores, or prunes anything. Docker
save stdout streams directly to a new ACL-attested private file; no remote tar
or image configuration/label/environment payload is printed in a receipt.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sys
import tarfile

import ops
from backup_release import PRIVATE_ROOT, read_acl, validate_acl
from release_preflight import (HASH, REMOTE_HELPERS, TASK_ID, ReleaseError, canonical,
    exclusive_json, parse_json, require, sha256, source_migrations, task_path, timestamp, validate_plan, alf_phase, ALF_FIELDS,
    alcf_phase, application_phase_fields)

CONTEXTS = frozenset({"auth-service", "learn-service", "community-service", "frontend"})
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_METADATA_BYTES = 16 * 1024 * 1024
OCI_INDEX_TYPES = frozenset({"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"})
OCI_MANIFEST_TYPES = frozenset({"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"})
OCI_CONFIG_TYPES = frozenset({"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"})
OCI_LAYER_TYPES = frozenset({"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd", "application/vnd.oci.image.layer.nondistributable.v1.tar",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip", "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
    "application/vnd.docker.image.rootfs.diff.tar", "application/vnd.docker.image.rootfs.diff.tar.gzip",
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip", "application/vnd.in-toto+json"})


def selection(plan):
    alcf_phase(plan)
    rows = plan["inspection"]["containers"]
    selected = []
    contexts = {"learn-service", "frontend"} if plan.get("learn_frontend", False) else CONTEXTS - {"community-service"} if alf_phase(plan) else CONTEXTS
    for service in sorted(contexts):
        current = [row for row in rows if row["service"] == service and row["running"]]
        require(len(current) == 1, "IMAGE_CONTEXT_RUNNING_AMBIGUOUS")
        selected.append(current[0]["image_id"])
    require(all(IMAGE_ID.fullmatch(item) for item in selected), "IMAGE_ID_INVALID")
    # Same-image writer workers share an immutable archive entry with their API.
    ids = sorted(set(selected))
    bindings = [{key: row[key] for key in ("service", "container_id", "image_id")}
                for row in rows if row["image_id"] in ids]
    return ids, sha256(canonical(bindings))


def image_inspector(ids):
    require(ids and ids == sorted(set(ids)) and all(IMAGE_ID.fullmatch(item) for item in ids), "IMAGE_ID_INVALID")
    return REMOTE_HELPERS + "\nSELECTED=" + repr(ids) + r'''
try:
    config=compose();protected=protected_rows(config);current=containers()
    raw=json.loads(run(['docker','image','inspect',*SELECTED]))
    images=[]
    for item in raw:
        labels=item.get('Config',{}).get('Labels',{}) or {}
        tags=item.get('RepoTags') or []
        images.append({'image_id':item['Id'],'bytes':item['Size'],
            'labels_sha256':hashlib.sha256(json.dumps(labels,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),
            'repo_tags_sha256':hashlib.sha256(json.dumps(sorted(tags),separators=(',',':'),ensure_ascii=False).encode()).hexdigest(),
            'os':item['Os'],'architecture':item['Architecture']})
    memory={line.split(':',1)[0]:line.split(':',1)[1].strip().split()[0]
            for line in pathlib.Path('/proc/meminfo').read_text().splitlines()}
    disk=os.statvfs(ROOT)
    print(json.dumps({'schema_version':1,'kind':'renewal-image-inspection','verified':True,
        'live_root':str(ROOT),'project':PROJECT,'containers':current,'protected_files':protected,
        'code_files':code_rows(protected),'compose_sha256':hashlib.sha256(json.dumps(config,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'images':sorted(images,key=lambda row:row['image_id']),
        'disk_available_bytes':disk.f_bavail*disk.f_frsize,'memory_available_bytes':int(memory['MemAvailable'])*1024}))
except Exception:
    print(json.dumps({'schema_version':1,'verified':False,'unverified_stage':'image_metadata'}))
'''


def validate_snapshot(value, plan, ids):
    require(isinstance(value, dict) and set(value) == {"schema_version", "kind", "verified", "live_root", "project",
        "containers", "protected_files", "code_files", "compose_sha256", "images", "disk_available_bytes", "memory_available_bytes"}, "IMAGE_INSPECTION_UNVERIFIED")
    require(value["schema_version"] == 1 and value["kind"] == "renewal-image-inspection" and value["verified"] is True,
            "IMAGE_INSPECTION_UNVERIFIED")
    for key in ("live_root", "project", "containers", "protected_files", "code_files", "compose_sha256"):
        require(value[key] == plan["inspection"][key], "IMAGE_LIVE_STATE_CHANGED")
    minio = [row for row in value["containers"] if row["service"] == "minio"]
    require(len(minio) == 1 and minio[0]["running"] is True and minio[0]["pid"] > 0, "IMAGE_EXPORT_REQUIRES_RUNNING_MINIO")
    require(type(value["disk_available_bytes"]) is int and value["disk_available_bytes"] > 0
        and type(value["memory_available_bytes"]) is int and value["memory_available_bytes"] >= 256 * 1024 * 1024,
        "IMAGE_REMOTE_RESOURCES_UNVERIFIED")
    images = value["images"]
    require(isinstance(images, list) and [row.get("image_id") for row in images] == ids, "IMAGE_METADATA_SET_MISMATCH")
    for row in images:
        require(set(row) == {"image_id", "bytes", "labels_sha256", "repo_tags_sha256", "os", "architecture"}
            and type(row["bytes"]) is int and row["bytes"] >= 0
            and all(isinstance(row[key], str) and HASH.fullmatch(row[key]) for key in ("labels_sha256", "repo_tags_sha256"))
            and all(isinstance(row[key], str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", row[key]) for key in ("os", "architecture")),
            "IMAGE_METADATA_INVALID")
    return images


def safe_member_name(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name and "\x00" not in name,
            "IMAGE_ARCHIVE_PATH_INVALID")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts, "IMAGE_ARCHIVE_PATH_INVALID")
    normalized = path.as_posix().removeprefix("./").rstrip("/")
    require(normalized not in {"", "."}, "IMAGE_ARCHIVE_PATH_INVALID")
    return normalized


def validate_oci_chain(archive, members, metadata, ids, docker_entries, expected):
    require(parse_json(metadata("oci-layout")) == {"imageLayoutVersion": "1.0.0"}, "IMAGE_OCI_LAYOUT_INVALID")
    index = parse_json(metadata("index.json"))
    require(isinstance(index, dict) and index.get("schemaVersion") == 2 and index.get("mediaType") in OCI_INDEX_TYPES
        and isinstance(index.get("manifests"), list), "IMAGE_OCI_INDEX_INVALID")
    roots = index["manifests"]
    require(len(roots) == len(ids) and all(isinstance(row, dict) for row in roots)
        and sorted(row.get("digest", "") for row in roots) == ids, "IMAGE_OCI_SELECTED_SET_MISMATCH")
    verified, leaves, config_documents = {}, {}, {}
    root_manifests, root_attestations = {identity: set() for identity in ids}, {identity: [] for identity in ids}
    def descriptor(row, allowed):
        require(isinstance(row, dict) and {"digest", "size", "mediaType"} <= set(row)
            and set(row) <= {"digest", "size", "mediaType", "annotations", "platform", "artifactType"}
            and isinstance(row["digest"], str) and IMAGE_ID.fullmatch(row["digest"])
            and type(row["size"]) is int and row["size"] >= 0 and row["mediaType"] in allowed,
            "IMAGE_OCI_DESCRIPTOR_INVALID")
        identity = row["digest"]
        name = "blobs/sha256/" + identity[7:]
        require(name in members and members[name].isfile() and members[name].size == row["size"], "IMAGE_OCI_BLOB_MISSING_OR_SIZE_CHANGED")
        signature = (row["size"], row["mediaType"])
        if identity in verified:
            require(verified[identity] == signature, "IMAGE_OCI_DESCRIPTOR_CONFLICT")
        else:
            with archive.extractfile(members[name]) as content:
                require(hashlib.file_digest(content, "sha256").hexdigest() == identity[7:], "IMAGE_OCI_BLOB_HASH_CHANGED")
            verified[identity] = signature
        return identity, name
    def walk(row, root, ancestors=()):
        identity, name = descriptor(row, OCI_INDEX_TYPES | OCI_MANIFEST_TYPES)
        require(identity not in ancestors and len(ancestors) < 8, "IMAGE_OCI_CHAIN_CYCLE_OR_DEPTH")
        document = parse_json(metadata(name))
        require(isinstance(document, dict) and document.get("schemaVersion") == 2
            and document.get("mediaType") == row["mediaType"], "IMAGE_OCI_DOCUMENT_INVALID")
        if row["mediaType"] in OCI_INDEX_TYPES:
            children = document.get("manifests")
            require(isinstance(children, list) and 0 < len(children) <= 10000, "IMAGE_OCI_CHILDREN_INVALID")
            for child in children: walk(child, root, (*ancestors, identity))
            return
        config_id, config_name = descriptor(document.get("config"), OCI_CONFIG_TYPES)
        config = parse_json(metadata(config_name))
        require(isinstance(config, dict) and isinstance(config.get("config", {}), dict), "IMAGE_CONFIG_INVALID")
        layers = document.get("layers")
        require(isinstance(layers, list) and all(isinstance(layer, dict) for layer in layers), "IMAGE_OCI_LAYERS_INVALID")
        layer_paths = [descriptor(layer, OCI_LAYER_TYPES)[1] for layer in layers]
        require(len(layer_paths) == len(set(layer_paths)), "IMAGE_OCI_LAYERS_INVALID")
        annotations = row.get("annotations", {})
        require(isinstance(annotations, dict), "IMAGE_OCI_ANNOTATIONS_INVALID")
        attestation = annotations.get("vnd.docker.reference.type") == "attestation-manifest"
        if attestation:
            target = annotations.get("vnd.docker.reference.digest")
            require(isinstance(target, str) and IMAGE_ID.fullmatch(target) and config.get("os") == "unknown"
                and config.get("architecture") == "unknown" and layers
                and all(layer["mediaType"] == "application/vnd.in-toto+json" for layer in layers), "IMAGE_OCI_ATTESTATION_INVALID")
            root_attestations[root].append((identity, target))
        else:
            require(all(isinstance(config.get(key), str) and config[key] != "unknown" for key in ("os", "architecture")),
                    "IMAGE_OCI_RUNTIME_CONFIG_INVALID")
            root_manifests[root].add(identity)
        platform = row.get("platform")
        if platform is not None:
            require(isinstance(platform, dict) and all(platform.get(key) == config.get(key) for key in ("os", "architecture")),
                    "IMAGE_OCI_PLATFORM_CONFIG_MISMATCH")
        leaf = {"config_id": config_id, "config_name": config_name, "layers": layer_paths, "attestation": attestation}
        require(identity not in leaves or leaves[identity] == leaf, "IMAGE_OCI_MANIFEST_CONFLICT")
        leaves[identity] = leaf
        config_documents[config_id] = config
    for root in roots: walk(root, root["digest"])
    runtime_configs = {leaf["config_id"] for leaf in leaves.values() if not leaf["attestation"]}
    require(set(docker_entries) == runtime_configs, "IMAGE_OCI_DOCKER_CONFIG_SET_MISMATCH")
    for leaf in leaves.values():
        if not leaf["attestation"]:
            require(docker_entries[leaf["config_id"]]["config_name"] == leaf["config_name"]
                and docker_entries[leaf["config_id"]]["layers"] == leaf["layers"], "IMAGE_OCI_DOCKER_LAYER_MISMATCH")
    for identity in ids:
        require(root_manifests[identity], "IMAGE_OCI_SELECTED_RUNTIME_MISSING")
        require(all(target in root_manifests[identity] for _, target in root_attestations[identity]), "IMAGE_OCI_ATTESTATION_TARGET_MISMATCH")
        if expected:
            row = expected[identity]
            candidates = [config_documents[leaves[item]["config_id"]] for item in root_manifests[identity]
                if all(config_documents[leaves[item]["config_id"]].get(key) == row[key] for key in ("os", "architecture"))]
            require(len(candidates) == 1 and sha256(canonical(candidates[0].get("config", {}).get("Labels") or {})) == row["labels_sha256"],
                    "IMAGE_CONFIG_METADATA_CHANGED")
    reachable = {"blobs/sha256/" + identity[7:] for identity in verified}
    require({name for name, member in members.items() if member.isfile()} == reachable | {"index.json", "manifest.json", "oci-layout"},
            "IMAGE_OCI_UNREFERENCED_FILES")
    return {"descriptor_blob_count": len(verified), "runnable_config_count": len(runtime_configs),
            "attestation_manifest_count": len({item for rows in root_attestations.values() for item, _ in rows})}


def validate_image_archive(path, ids, images=None):
    require(ids and ids == sorted(set(ids)) and all(IMAGE_ID.fullmatch(item) for item in ids), "IMAGE_ID_INVALID")
    members, end = {}, 0
    # docker image save is an uncompressed tar. Never extract even to a private path.
    with tarfile.open(path, "r:") as archive:
        for item in archive:
            name = safe_member_name(item.name)
            require(item.isfile() or item.isdir(), "IMAGE_ARCHIVE_LINK_OR_SPECIAL_FORBIDDEN")
            require(name not in members, "IMAGE_ARCHIVE_DUPLICATE_MEMBER")
            require(item.size >= 0, "IMAGE_ARCHIVE_MEMBER_SIZE_INVALID")
            members[name] = item
            end = max(end, item.offset_data + ((item.size + 511) // 512) * 512)
            require(len(members) <= 1000000, "IMAGE_ARCHIVE_MEMBER_LIMIT")
        def metadata(name):
            require(name in members and members[name].isfile() and members[name].size <= MAX_METADATA_BYTES,
                    "IMAGE_ARCHIVE_METADATA_MISSING_OR_LARGE")
            with archive.extractfile(members[name]) as content:
                data = content.read(MAX_METADATA_BYTES + 1)
            require(len(data) == members[name].size, "IMAGE_ARCHIVE_TRUNCATED")
            return data
        manifest = parse_json(metadata("manifest.json"))
        oci = "index.json" in members or "oci-layout" in members
        require(isinstance(manifest, list) and manifest and (oci or len(manifest) == len(ids)), "IMAGE_MANIFEST_SET_MISMATCH")
        included = set()
        expected = {row["image_id"]: row for row in images} if images is not None else {}
        docker_entries = {}
        for entry in manifest:
            require(isinstance(entry, dict) and set(entry) <= {"Config", "RepoTags", "Layers", "LayerSources"}
                and {"Config", "Layers"} <= set(entry), "IMAGE_MANIFEST_ENTRY_INVALID")
            config_name = safe_member_name(entry["Config"])
            config_bytes = metadata(config_name)
            identity = "sha256:" + sha256(config_bytes)
            require((oci or identity in ids) and identity not in included, "IMAGE_CONFIG_ID_MISMATCH_OR_DUPLICATE")
            included.add(identity)
            config = parse_json(config_bytes)
            require(isinstance(config, dict) and isinstance(config.get("config", {}), dict), "IMAGE_CONFIG_INVALID")
            if expected and not oci:
                labels = config.get("config", {}).get("Labels") or {}
                row = expected[identity]
                require(sha256(canonical(labels)) == row["labels_sha256"] and config.get("os") == row["os"]
                    and config.get("architecture") == row["architecture"], "IMAGE_CONFIG_METADATA_CHANGED")
            layers = entry["Layers"]
            require(isinstance(layers, list) and all(isinstance(layer, str) for layer in layers)
                and len(layers) == len(set(layers)), "IMAGE_LAYERS_INVALID")
            for layer in layers:
                name = safe_member_name(layer)
                require(name in members and members[name].isfile(), "IMAGE_LAYER_MISSING")
            docker_entries[identity] = {"config_name": config_name, "layers": [safe_member_name(layer) for layer in layers]}
        proof = validate_oci_chain(archive, members, metadata, ids, docker_entries, expected) if oci else {}
        if not oci: require(included == set(ids), "IMAGE_MANIFEST_SET_MISMATCH")
    # Reject a hidden appended archive after tar's first end marker.
    with Path(path).open("rb") as source:
        source.seek(end)
        while block := source.read(1024 * 1024):
            require(not block.strip(b"\0"), "IMAGE_ARCHIVE_TRAILING_PAYLOAD")
    return {"file_count": sum(item.isfile() for item in members.values()), "selected_image_ids": ids,
            "config_matching_verified": True, "image_identity_mode": "oci-digest-chain" if oci else "legacy-config-digest",
            "digest_chain_verified": oci, **proof}


def validate_receipt(value, plan, plan_hash, *, verify_archive=True):
    require(isinstance(value, dict) and set(value) == {"schema_version", "kind", "task", "nonce", "plan_sha256",
        "source_fingerprint", "source_file_count", "target", "started_at", "captured_at", "selected_image_ids",
        "container_binding_sha256", "images_metadata_sha256", "acl_sha256", "archive", "capture_verified",
        "config_matching_verified", "image_identity_mode", "digest_chain_verified", "restore_verified", "remote_temporary_bytes", "service_mutations_performed", "tag_mutations_performed"}
        | set(application_phase_fields(plan)),
        "IMAGE_RECEIPT_SCHEMA_INVALID")
    require(value["schema_version"] == 1 and value["kind"] == "renewal-image-backup" and value["task"] == TASK_ID,
            "IMAGE_RECEIPT_SCHEMA_INVALID")
    for key in ("nonce", "source_fingerprint", "source_file_count", "target"):
        require(value[key] == plan[key], "IMAGE_RECEIPT_BINDING_MISMATCH")
    require(all(value.get(key) == plan[key] for key in application_phase_fields(plan)), "IMAGE_RECEIPT_SCOPE_CHANGED")
    ids, binding = selection(plan)
    require(value["plan_sha256"] == plan_hash and value["selected_image_ids"] == ids
        and value["container_binding_sha256"] == binding, "IMAGE_RECEIPT_BINDING_MISMATCH")
    require(timestamp(plan["created_at"]) <= timestamp(value["started_at"]) <= timestamp(value["captured_at"])
        <= datetime.now(timezone.utc), "IMAGE_RECEIPT_TIME_INVALID")
    require(value["capture_verified"] is True and value["config_matching_verified"] is True and value["restore_verified"] is False
        and value["remote_temporary_bytes"] == 0 and value["service_mutations_performed"] is False
        and value["tag_mutations_performed"] is False, "IMAGE_RECEIPT_CAPTURE_UNVERIFIED")
    require(value["image_identity_mode"] in {"legacy-config-digest", "oci-digest-chain"}
        and value["digest_chain_verified"] is (value["image_identity_mode"] == "oci-digest-chain"), "IMAGE_RECEIPT_IDENTITY_PROOF_INVALID")
    require(all(isinstance(value[key], str) and HASH.fullmatch(value[key]) for key in ("images_metadata_sha256", "acl_sha256")),
            "IMAGE_RECEIPT_HASH_INVALID")
    row = value["archive"]
    require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256", "file_count"}
        and type(row["bytes"]) is int and row["bytes"] > 0 and type(row["file_count"]) is int and row["file_count"] > 0
        and isinstance(row["sha256"], str) and HASH.fullmatch(row["sha256"]), "IMAGE_RECEIPT_ARCHIVE_INVALID")
    path = task_path(Path(row["path"]), PRIVATE_ROOT)
    require(path == PRIVATE_ROOT / (plan["nonce"] + "-images") / "old-release-images.tar", "IMAGE_RECEIPT_PATH_INVALID")
    if verify_archive:
        require(path.is_file() and path.stat().st_size == row["bytes"], "IMAGE_ARCHIVE_SIZE_CHANGED")
        with path.open("rb") as source:
            require(hashlib.file_digest(source, "sha256").hexdigest() == row["sha256"], "IMAGE_ARCHIVE_HASH_CHANGED")
        checked = validate_image_archive(path, ids)
        require(checked["file_count"] == row["file_count"], "IMAGE_ARCHIVE_FILE_COUNT_CHANGED")
        require(all(checked[key] == value[key] for key in ("image_identity_mode", "digest_chain_verified")), "IMAGE_ARCHIVE_IDENTITY_PROOF_CHANGED")
    return row


def execute(plan_path, expected_sha256, attestation_path, *, execute_approved, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(execute_approved is True, "EXECUTE_APPROVAL_REQUIRED")
    require(isinstance(expected_sha256, str) and HASH.fullmatch(expected_sha256), "PLAN_SHA256_REQUIRED")
    plan_path = task_path(plan_path, ops.TASK / "evidence")
    attestation_path = task_path(attestation_path, ops.TASK / "evidence")
    payload = plan_path.read_bytes()
    require(sha256(payload) == expected_sha256, "PLAN_SHA256_MISMATCH")
    plan = parse_json(payload)
    require(alf_phase(plan) is auth_learn_frontend, "IMAGE_BACKUP_SCOPE_CHANGED")
    require(alcf_phase(plan) is auth_learn_community_frontend, "IMAGE_BACKUP_SCOPE_CHANGED")
    require(plan.get("learn_frontend", False) is learn_frontend, "IMAGE_BACKUP_SCOPE_CHANGED")
    current = ops.manifest()
    validate_plan(plan, current["fingerprint"])
    require(plan["source_file_count"] == len(current["files"]), "SOURCE_FILE_COUNT_CHANGED")
    require(plan["migrations"] == source_migrations(), "SOURCE_MIGRATION_GRAPH_CHANGED")
    ids, binding = selection(plan)
    root = task_path(PRIVATE_ROOT / (plan["nonce"] + "-images"), PRIVATE_ROOT)
    require(PRIVATE_ROOT.is_dir() and not root.exists(), "IMAGE_BACKUP_ROOT_MISSING_OR_NONCE_REUSED")
    attestation = parse_json(attestation_path.read_bytes())
    acl = validate_acl(attestation, read_acl(PRIVATE_ROOT), root=PRIVATE_ROOT)
    inspector = image_inspector(ids)
    before = ops.remote_python(inspector, timeout=300)
    images = validate_snapshot(before, plan, ids)
    require(shutil.disk_usage(PRIVATE_ROOT).free >= sum(row["bytes"] for row in images) * 2 + 256 * 1024 * 1024,
            "LOCAL_IMAGE_BACKUP_SPACE_INSUFFICIENT")
    started_at = datetime.now(timezone.utc).isoformat()
    exclusive_json(task_path(ops.TASK / "evidence" / f"image-backup-attempt-{plan['nonce']}.json", ops.TASK / "evidence"),
        {"schema_version": 1, "nonce": plan["nonce"], "plan_sha256": expected_sha256,
         "source_fingerprint": current["fingerprint"], "private_path": str(root), "started_at": started_at})
    root.mkdir()
    require(validate_acl(attestation, read_acl(PRIVATE_ROOT), root=PRIVATE_ROOT) == acl, "ACL_CHANGED_DURING_START")
    archive = root / "old-release-images.tar"
    streamed = ops.streaming_ssh(shlex.join(["docker", "image", "save", *ids]), archive, timeout=1800)
    require(streamed.get("bytes", 0) == archive.stat().st_size and streamed["bytes"] > 0
        and HASH.fullmatch(streamed.get("sha256", "")), "IMAGE_STREAM_EMPTY_OR_INVALID")
    checked = validate_image_archive(archive, ids, images)
    after = ops.remote_python(inspector, timeout=300)
    require(validate_snapshot(after, plan, ids) == images, "IMAGE_METADATA_CHANGED_DURING_CAPTURE")
    require(ops.manifest()["fingerprint"] == current["fingerprint"], "SOURCE_CHANGED_DURING_IMAGE_BACKUP")
    require(validate_acl(attestation, read_acl(PRIVATE_ROOT), root=PRIVATE_ROOT) == acl, "ACL_CHANGED_DURING_IMAGE_BACKUP")
    result = {"schema_version": 1, "kind": "renewal-image-backup", "task": TASK_ID, "nonce": plan["nonce"],
        "plan_sha256": expected_sha256, "source_fingerprint": current["fingerprint"], "source_file_count": len(current["files"]),
        "target": plan["target"], "started_at": started_at, "captured_at": datetime.now(timezone.utc).isoformat(),
        "selected_image_ids": ids, "container_binding_sha256": binding, "images_metadata_sha256": sha256(canonical(images)),
        "acl_sha256": acl, "archive": {"path": str(archive), **streamed, "file_count": checked["file_count"]},
        "capture_verified": True, "config_matching_verified": True, "restore_verified": False,
        "image_identity_mode": checked["image_identity_mode"], "digest_chain_verified": checked["digest_chain_verified"],
        "remote_temporary_bytes": 0, "service_mutations_performed": False, "tag_mutations_performed": False,
        **{key: plan[key] for key in application_phase_fields(plan)}}
    validate_receipt(result, plan, expected_sha256, verify_archive=False)
    receipt = ops.receipt(f"image-backup-complete-{plan['nonce']}.json", result)
    return {"receipt": str(receipt), "archive": result["archive"], "selected_image_ids": ids,
            "capture_verified": True, "config_matching_verified": True, "image_identity_mode": checked["image_identity_mode"],
            "digest_chain_verified": checked["digest_chain_verified"], "restore_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--acl-attestation", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--auth-learn-frontend", action="store_true")
    scope.add_argument("--auth-learn-community-frontend", action="store_true")
    scope.add_argument("--learn-frontend", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(execute(args.plan, args.plan_sha256, args.acl_attestation, execute_approved=args.execute, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__,
                          "partial_files": "preserved_if_created_no_automatic_cleanup"}))
        sys.exit(1)
