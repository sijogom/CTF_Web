"""Explicit, source-bound SSH streaming backup; never restore or prune.

All approval inputs and the operator's same-nonce quiescence proof are mandatory.
Successful receipts certify fenced captures, not a tested restore operation.
Private payloads and partial streams stay beneath the attested task root.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile

import ops
from release_preflight import (DATABASES, HASH, FENCE_INSPECTOR, REMOTE_HELPERS, ReleaseError,
    canonical, exclusive_json, parse_json, remote_program, require, sha256,
    source_migrations, task_path, timestamp, validate_plan, alf_phase, ALF_FIELDS,
    alcf_phase, application_phase_fields)

PRIVATE_ROOT = ops.TASK / "runtime" / "private-backups"
SYSTEM_SID = "S-1-5-18"


def read_acl(root: Path) -> dict:
    require(os.name == "nt", "NTFS_ACL_VERIFICATION_REQUIRES_WINDOWS")
    encoded_path = base64.b64encode(str(root).encode("utf-8")).decode()
    script = r'''
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PATH_BASE64'))
$item = Get-Item -LiteralPath $path -Force
if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'ACL_ROOT_INVALID' }
$acl = Get-Acl -LiteralPath $path
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$entries = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
  [ordered]@{ sid = $_.IdentityReference.Value; rights = [int]$_.FileSystemRights; allow = ([int]$_.AccessControlType -eq 0); inherited = $_.IsInherited; inheritance_flags = [int]$_.InheritanceFlags; propagation_flags = [int]$_.PropagationFlags }
})
[ordered]@{ root = $item.FullName; user_sid = $sid; owner_sid = $owner; inheritance_disabled = $acl.AreAccessRulesProtected; entries = $entries } | ConvertTo-Json -Depth 5 -Compress
'''.replace("PATH_BASE64", encoded_path)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    child_environment = dict(os.environ)
    child_environment["PSModulePath"] = str(system / "System32/WindowsPowerShell/v1.0/Modules")
    result = subprocess.run([str(system / "System32/WindowsPowerShell/v1.0/powershell.exe"),
        "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True, timeout=30, env=child_environment)
    require(result.returncode == 0, "LIVE_ACL_QUERY_FAILED")
    return parse_json(result.stdout)


def validate_acl(attestation, live, *, root=PRIVATE_ROOT, now=None):
    require(isinstance(attestation, dict) and set(attestation) == {
        "schema_version", "root", "user_sid", "allowed_sids", "inheritance_disabled", "verified_at"}, "ACL_ATTESTATION_SCHEMA_INVALID")
    require(attestation["schema_version"] == 1 and Path(attestation["root"]).resolve() == root.resolve(), "ACL_ATTESTATION_ROOT_INVALID")
    age = (now or datetime.now(timezone.utc)) - timestamp(attestation["verified_at"])
    require(timedelta(0) <= age <= timedelta(hours=24), "ACL_ATTESTATION_EXPIRED")
    sid = attestation["user_sid"]
    require(isinstance(sid, str) and re.fullmatch(r"S-\d+(?:-\d+)+", sid) and sid != SYSTEM_SID
            and set(attestation["allowed_sids"]) == {sid, SYSTEM_SID}, "ACL_PRINCIPALS_INVALID")
    require(attestation["inheritance_disabled"] is True and live.get("inheritance_disabled") is True,
            "ACL_INHERITANCE_ENABLED")
    require(Path(live.get("root", "")).resolve() == root.resolve() and live.get("user_sid") == sid and live.get("owner_sid") == sid, "ACL_OWNER_CHANGED")
    entries = live.get("entries", [])
    require(len(entries) == 2 and {row.get("sid") for row in entries} == {sid, SYSTEM_SID}, "ACL_EXTRA_OR_MISSING_ACE")
    require(all(row.get("allow") is True and row.get("inherited") is False and row.get("rights") == 2032127
        and row.get("inheritance_flags") == 3 and row.get("propagation_flags") == 0 for row in entries), "ACL_RIGHTS_INVALID")
    return sha256(canonical(live))


def archive_rows(path: Path, *, private_names=False):
    rows = []
    with tarfile.open(path, "r:*") as archive:
        for item in archive:
            parsed = PurePosixPath(item.name)
            require(not parsed.is_absolute() and ".." not in parsed.parts and (item.isfile() or item.isdir()), "UNSAFE_ARCHIVE_MEMBER")
            if item.isfile():
                name = parsed.as_posix().removeprefix("./")
                with archive.extractfile(item) as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                rows.append({"path": sha256(name.encode()) if private_names else name,
                             "bytes": item.size, "sha256": digest})
                require(len(rows) <= 1000000, "ARCHIVE_FILE_LIMIT")
    require(len({row["path"] for row in rows}) == len(rows), "DUPLICATE_ARCHIVE_MEMBER")
    return sorted(rows, key=lambda row: row["path"])


def validate_archive(path, expected, kind):
    if kind == "objects":
        rows = archive_rows(path, private_names=True)
        content = [[row["path"], row["bytes"], row["sha256"]] for row in rows]
        require(len(rows) == expected["file_count"] and sum(row["bytes"] for row in rows) == expected["bytes"]
                and sha256(json.dumps(content, separators=(",", ":")).encode()) == expected["inventory_sha256"], "OBJECT_INVENTORY_CHANGED")
    else:
        rows = archive_rows(path)
        expected_rows = expected if kind == "code" else [
            {**row, "path": f"protected/{index:06d}"} for index, row in enumerate(expected)]
        require(rows == sorted(expected_rows, key=lambda row: row["path"]), "ARCHIVE_INVENTORY_CHANGED")
    return len(rows)


def archive_command(kind: str) -> str:
    require(kind in {"code", "protected"}, "ARCHIVE_KIND_INVALID")
    source = REMOTE_HELPERS + "\nKIND=" + repr(kind) + r'''
try:
    protected=protected_rows(compose())
    rows=code_rows(protected) if KIND=='code' else protected
    with tarfile.open(fileobj=sys.stdout.buffer,mode='w|',format=tarfile.PAX_FORMAT) as archive:
        for index,row in enumerate(rows):
            path=ROOT/row['path'] if KIND=='code' else pathlib.Path(row['path'])
            name=row['path'] if KIND=='code' else 'protected/'+format(index,'06d')
            try:
                regular(path)
                content=path.open('rb')
                try: info=archive.gettarinfo(str(path),arcname=name)
                except Exception:
                    content.close();raise
            except PermissionError:
                if KIND!='protected': raise
                data=protected_key_bytes(path)
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600
                content=io.BytesIO(data)
            info.uid=info.gid=0;info.uname=info.gname=''
            with content: archive.addfile(info,content)
except Exception:
    sys.exit(1)
'''
    return remote_program(source)


def stream_specs(plan, *, learn_only=False):
    import shlex
    current = plan["inspection"]["containers"]
    postgres = next(row["container_id"] for row in current if row["service"] == "postgres" and row["running"])
    minio = next(row["container_id"] for row in current if row["service"] == "minio" and row["running"])
    specs = []
    shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -U "$POSTGRES_USER" -d "$1" --format=custom --no-owner --no-privileges'
    databases = (DATABASES["auth"], DATABASES["learn"]) if alf_phase(plan) else (DATABASES["learn"],) if learn_only or plan.get("learn_frontend", False) else DATABASES.values()
    for database in databases:
        command = shlex.join(["docker", "exec", postgres, "sh", "-c", shell, "renewal-dump", database])
        specs.append({"name": database + ".dump", "kind": "database", "command": command})
    specs.extend([
        {"name": "minio-objects.tar", "kind": "objects", "command": shlex.join(["docker", "cp", minio + ":/data/.", "-"])},
        {"name": "live-code.tar", "kind": "code", "command": archive_command("code")},
        {"name": "protected-settings.tar", "kind": "protected", "command": archive_command("protected")},
    ])
    return specs


def stable_inspection(value):
    return {key: value[key] for key in ("live_root", "project", "compose_sha256", "containers", "protected_files", "code_files", "objects")} | {
        "databases": {name: {key: row[key] for key in ("database", "head", "schema_sha256", "table_count")} for name, row in value["databases"].items()}}


def static_inspection(value):
    result = stable_inspection(value)
    result.pop("objects")
    result["containers"] = [{key: row[key] for key in ("service", "container_id", "image_id", "mounts")}
                            for row in value["containers"]]
    return result


def validate_quiescence(value, plan, plan_hash, *, now=None, learn_only=False):
    from release_preflight import TASK_ID, validate_inspection
    require(plan.get("learn_only", False) is learn_only, "QUIESCENCE_PLAN_SCOPE_CHANGED")
    alf = alf_phase(plan)
    alcf = alcf_phase(plan)
    lf = plan.get("learn_frontend", False)
    require(all(value.get(key) == plan.get(key) for key in application_phase_fields(plan)), "QUIESCENCE_ALF_SCOPE_CHANGED")
    require(isinstance(value, dict) and set(value) == {"schema_version", "kind", "task", "nonce", "plan_sha256",
        "source_fingerprint", "verified_at", "drain_verified", "external_writers_absent_verified",
        "fence_maintained_until_capture_complete", "inspection"} | ({"learn_only"} if learn_only else set()) | set(application_phase_fields(plan)), "QUIESCENCE_SCHEMA_INVALID")
    require(value.get("learn_only", False) is learn_only, "QUIESCENCE_SCOPE_CHANGED")
    require(value["schema_version"] == 1 and value["kind"] == "renewal-quiescence" and value["task"] == TASK_ID,
            "QUIESCENCE_SCHEMA_INVALID")
    require(value["nonce"] == plan["nonce"] and value["plan_sha256"] == plan_hash and value["source_fingerprint"] == plan["source_fingerprint"], "QUIESCENCE_BINDING_MISMATCH")
    age = (now or datetime.now(timezone.utc)) - timestamp(value["verified_at"])
    require(timedelta(0) <= age <= timedelta(minutes=15), "QUIESCENCE_EXPIRED_OR_FUTURE")
    require(timestamp(value["verified_at"]) >= timestamp(plan["created_at"]), "QUIESCENCE_PREDATES_PLAN")
    require(all(value[name] is True for name in ("drain_verified", "external_writers_absent_verified", "fence_maintained_until_capture_complete")), "QUIESCENCE_OPERATOR_PROOF_MISSING")
    frozen = value["inspection"]
    validate_inspection(frozen, plan["migrations"], quiesced=True)
    require(static_inspection(frozen) == static_inspection(plan["inspection"]), "QUIESCENCE_STATIC_STATE_CHANGED")
    planned = plan["inspection"]["containers"]
    selected_apis = {"auth-service", "learn-service"} if alf else {"learn-service"} if learn_only or lf else {"auth-service", "learn-service", "community-service"}
    api_images = {row["image_id"] for row in planned if row["service"] in selected_apis and row["running"]}
    require(len(api_images) > 0, "QUIESCENCE_WRITER_IMAGES_MISSING")
    writer_ids = {row["container_id"] for row in planned if row["running"] and row["image_id"] in api_images}
    minio_ids = {row["container_id"] for row in planned if row["service"] == "minio" and row["running"]}
    require(len(minio_ids) == 1, "QUIESCENCE_MINIO_AMBIGUOUS")
    stopped_ids = writer_ids | minio_ids
    require(writer_ids and all(any(row["service"] == service and row["container_id"] in writer_ids for row in planned)
        for service in selected_apis), "QUIESCENCE_WRITER_SET_INVALID")
    if learn_only or alf or alcf or lf:
        from quiesce_release import select_targets
        expected = select_targets(plan, learn_only=learn_only)
        require(writer_ids == {row["container_id"] for row in expected["apis"] + expected["workers"]}, "QUIESCENCE_LEARN_WRITER_SET_INVALID")
    prior = {row["container_id"]: row for row in planned}
    for row in frozen["containers"]:
        before = prior[row["container_id"]]
        if row["container_id"] in stopped_ids:
            require(row["running"] is False and row["pid"] == 0 and row["status"] == "exited", "QUIESCENCE_WRITER_OR_MINIO_NOT_STOPPED")
            require(row["started_at"] == before["started_at"] and row["restart_count"] == before["restart_count"], "QUIESCENCE_CONTAINER_RESTARTED")
            require(row["finished_at"] != before["finished_at"] and row["finished_at"] != "0001-01-01T00:00:00Z", "QUIESCENCE_STOP_TIME_UNVERIFIED")
        else:
            require(row == before, "QUIESCENCE_UNRELATED_CONTAINER_CHANGED")
    require(all(row["client_session_count"] == 0 for name, row in frozen["databases"].items() if (name in {"auth", "learn"} if alf else not (learn_only or lf) or name == "learn")), "QUIESCENCE_DATABASE_CLIENTS_REMAIN")
    return frozen


def check_fence(value, frozen, migrations, *, learn_only=False, auth_learn_frontend=False):
    from release_preflight import validate_inspection
    require(isinstance(value, dict) and value.get("kind") == "renewal-fence-inspection" and set(value) == {
        "schema_version", "verified", "kind", "live_root", "project", "containers", "protected_files", "code_files", "databases", "compose_sha256"}, "FENCE_INSPECTION_UNVERIFIED")
    # Objects are validated against the captured archive, not claimed as reread here.
    metadata = {key: item for key, item in value.items() if key != "kind"}
    metadata.update({"objects": frozen["objects"], "disk_available_bytes": frozen["disk_available_bytes"]})
    validate_inspection(metadata, migrations, quiesced=True)
    require(stable_inspection(metadata) == stable_inspection(frozen), "QUIESCENCE_FENCE_CHANGED")
    require(all(row["client_session_count"] == 0 for name, row in value["databases"].items() if (name in {"auth", "learn"} if auth_learn_frontend else not learn_only or name == "learn")), "QUIESCENCE_DATABASE_CLIENTS_REMAIN")


def execute(plan_path, expected_sha256, attestation_path, quiescence_path, *,
            image_receipt_path, image_receipt_sha256, execute_approved, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(execute_approved is True, "EXECUTE_APPROVAL_REQUIRED")
    require(isinstance(expected_sha256, str) and HASH.fullmatch(expected_sha256), "PLAN_SHA256_REQUIRED")
    plan_path = task_path(plan_path, ops.TASK / "evidence")
    attestation_path = task_path(attestation_path, ops.TASK / "evidence")
    quiescence_path = task_path(quiescence_path, ops.TASK / "evidence")
    image_receipt_path = task_path(image_receipt_path, ops.TASK / "evidence")
    payload = plan_path.read_bytes()
    require(sha256(payload) == expected_sha256, "PLAN_SHA256_MISMATCH")
    plan = parse_json(payload)
    require(plan.get("learn_only", False) is learn_only, "BACKUP_PLAN_SCOPE_CHANGED")
    require(alf_phase(plan) is auth_learn_frontend, "BACKUP_PLAN_SCOPE_CHANGED")
    require(alcf_phase(plan) is auth_learn_community_frontend, "BACKUP_PLAN_SCOPE_CHANGED")
    require(plan.get("learn_frontend", False) is learn_frontend, "BACKUP_PLAN_SCOPE_CHANGED")
    current_source = ops.manifest()
    validate_plan(plan, current_source["fingerprint"])
    require(plan["source_file_count"] == len(current_source["files"]), "SOURCE_FILE_COUNT_CHANGED")
    require(plan["migrations"] == source_migrations(), "SOURCE_MIGRATION_GRAPH_CHANGED")
    quiescence_payload = quiescence_path.read_bytes()
    quiescence = parse_json(quiescence_payload)
    frozen = validate_quiescence(quiescence, plan, expected_sha256, learn_only=learn_only)
    require(isinstance(image_receipt_sha256, str) and HASH.fullmatch(image_receipt_sha256), "IMAGE_RECEIPT_SHA256_REQUIRED")
    image_payload = image_receipt_path.read_bytes()
    require(sha256(image_payload) == image_receipt_sha256, "IMAGE_RECEIPT_SHA256_MISMATCH")
    # Import here to keep the image tool's ACL helper reuse acyclic.
    from backup_images import validate_receipt
    image_receipt = parse_json(image_payload)
    image_archive = validate_receipt(image_receipt, plan, expected_sha256)
    require(timestamp(image_receipt["captured_at"]) <= timestamp(quiescence["verified_at"]),
            "IMAGE_BACKUP_MUST_PRECEDE_QUIESCENCE")
    root = task_path(PRIVATE_ROOT / plan["nonce"], PRIVATE_ROOT)
    require(PRIVATE_ROOT.is_dir() and not root.exists(), "BACKUP_ROOT_MISSING_OR_NONCE_REUSED")
    attestation = parse_json(attestation_path.read_bytes())
    acl_sha256 = validate_acl(attestation, read_acl(PRIVATE_ROOT), root=PRIVATE_ROOT)
    require(image_receipt["acl_sha256"] == acl_sha256, "IMAGE_BACKUP_ACL_CHANGED")
    estimate = sum(row["bytes"] for name, row in plan["inspection"]["databases"].items() if (name in {"auth", "learn"} if auth_learn_frontend else not (learn_only or learn_frontend) or name == "learn")) * 2
    estimate += frozen["objects"]["bytes"] + sum(row["bytes"] for row in frozen["code_files"] + frozen["protected_files"])
    require(shutil.disk_usage(PRIVATE_ROOT).free >= estimate + 256 * 1024 * 1024, "LOCAL_BACKUP_SPACE_INSUFFICIENT")
    claim = task_path(ops.TASK / "evidence" / f"backup-attempt-{plan['nonce']}.json", ops.TASK / "evidence")
    exclusive_json(claim, {"schema_version": 1, "nonce": plan["nonce"], "plan_sha256": expected_sha256,
        "source_fingerprint": current_source["fingerprint"], "private_path": str(root), "started_at": datetime.now(timezone.utc).isoformat()})
    root.mkdir()
    require(validate_acl(attestation, read_acl(PRIVATE_ROOT), root=PRIVATE_ROOT) == acl_sha256, "ACL_CHANGED_DURING_START")
    before = ops.remote_python(FENCE_INSPECTOR, timeout=300)
    check_fence(before, frozen, plan["migrations"], learn_only=learn_only or learn_frontend, auth_learn_frontend=auth_learn_frontend)
    files = [image_archive]
    for spec in stream_specs(plan, learn_only=learn_only):
        check_fence(ops.remote_python(FENCE_INSPECTOR, timeout=300), frozen, plan["migrations"], learn_only=learn_only or learn_frontend, auth_learn_frontend=auth_learn_frontend)
        target = root / spec["name"]
        streamed = ops.streaming_ssh(spec["command"], target, timeout=1800)
        require(streamed.get("bytes", 0) > 0 and HASH.fullmatch(streamed.get("sha256", "")), "BACKUP_STREAM_EMPTY_OR_HASH_INVALID")
        if spec["kind"] == "database":
            require(streamed["bytes"] >= 32, "POSTGRES_CUSTOM_DUMP_TOO_SHORT")
            with target.open("rb") as stream:
                require(stream.read(5) == b"PGDMP", "POSTGRES_CUSTOM_DUMP_INVALID")
            count = None
        else:
            key = {"objects": "objects", "code": "code_files", "protected": "protected_files"}[spec["kind"]]
            count = validate_archive(target, frozen[key], spec["kind"])
        files.append({"path": str(target), **streamed, "file_count": count})
    old_images = [{key: row[key] for key in ("service", "container_id", "image_id")} for row in plan["inspection"]["containers"]]
    image_path = root / "old-images.json"
    image_hash = exclusive_json(image_path, old_images)
    files.append({"path": str(image_path), "sha256": image_hash, "bytes": image_path.stat().st_size, "file_count": len(old_images)})
    after = ops.remote_python(FENCE_INSPECTOR, timeout=300)
    check_fence(after, frozen, plan["migrations"], learn_only=learn_only or learn_frontend, auth_learn_frontend=auth_learn_frontend)
    require(ops.manifest()["fingerprint"] == current_source["fingerprint"], "SOURCE_CHANGED_DURING_BACKUP")
    require(validate_acl(attestation, read_acl(PRIVATE_ROOT), root=PRIVATE_ROOT) == acl_sha256, "ACL_CHANGED_DURING_BACKUP")
    require(sha256(image_receipt_path.read_bytes()) == image_receipt_sha256, "IMAGE_RECEIPT_CHANGED_DURING_BACKUP")
    require(validate_receipt(image_receipt, plan, expected_sha256) == image_archive, "IMAGE_ARCHIVE_CHANGED_DURING_BACKUP")
    result = {"schema_version": 1, "kind": "renewal-stream-backup", "nonce": plan["nonce"],
        "plan_sha256": expected_sha256, "source_fingerprint": current_source["fingerprint"], "acl_sha256": acl_sha256,
        "image_backup_receipt_path": str(image_receipt_path), "image_backup_receipt_sha256": image_receipt_sha256,
        "image_backup_verified": True, "selected_image_ids": image_receipt["selected_image_ids"],
        "quiescence_sha256": sha256(quiescence_payload), "quiescence_verified": True, "release_eligible": True,
        "private_path": str(root), "files": files, "database_count": 2 if auth_learn_frontend else 1 if learn_only or learn_frontend else 3, "ctfd_db_included": False,
        "remote_temporary_bytes": 0, "cross_service_consistency_verified": not (learn_only or auth_learn_frontend or learn_frontend), "restore_verified": False,
        "consistency_basis": "same-nonce Auth/Learn writer fence and stopped MinIO; Community remains running and is not captured" if auth_learn_frontend else "same-nonce Learn write fence and stopped MinIO; Learn DB/object consistency only; Auth/Community snapshots not captured" if learn_only else "same-nonce operator write fence, stopped writers and MinIO, zero other database clients, immutable inventories and container process states",
        **({"learn_only": True, "learn_object_consistency_verified": True, "captured_databases": [DATABASES["learn"]]} if learn_only else {}),
        **({**{key: plan[key] for key in ALF_FIELDS}, "auth_learn_object_consistency_verified": True,
            "captured_databases": [DATABASES[name] for name in ("auth", "learn")]} if auth_learn_frontend else {}),
        **({**{key: plan[key] for key in application_phase_fields(plan)}, "captured_databases": list(DATABASES.values())}
           if auth_learn_community_frontend else {}),
        **({**{key: plan[key] for key in application_phase_fields(plan)}, "learn_object_consistency_verified": True,
            "captured_databases": [DATABASES["learn"]], "consistency_basis": "same-nonce Learn writer fence and stopped MinIO; Auth/Community not captured"}
           if learn_frontend else {})}
    receipt = ops.receipt(f"backup-complete-{plan['nonce']}.json", result)
    return {"receipt": str(receipt), "private_path": str(root), "file_count": len(files),
            "source_fingerprint": current_source["fingerprint"], "plan_sha256": expected_sha256,
            "quiescence_verified": True, "release_eligible": True, "cross_service_consistency_verified": not (learn_only or auth_learn_frontend or learn_frontend), "restore_verified": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--acl-attestation", type=Path, required=True)
    parser.add_argument("--quiescence-attestation", type=Path, required=True)
    parser.add_argument("--image-backup-receipt", type=Path, required=True)
    parser.add_argument("--image-backup-receipt-sha256", required=True)
    parser.add_argument("--execute", action="store_true", required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--learn-only", action="store_true")
    scope.add_argument("--auth-learn-frontend", action="store_true")
    scope.add_argument("--auth-learn-community-frontend", action="store_true")
    scope.add_argument("--learn-frontend", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(execute(args.plan, args.plan_sha256, args.acl_attestation, args.quiescence_attestation,
        image_receipt_path=args.image_backup_receipt, image_receipt_sha256=args.image_backup_receipt_sha256,
        execute_approved=args.execute, learn_only=args.learn_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__,
                          "partial_files": "preserved_if_created_no_automatic_cleanup"}))
        sys.exit(1)
