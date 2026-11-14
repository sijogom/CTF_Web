"""Source-bound Learn/Env/frontend release; no migrations, prune or automatic restore.

Only the operator may supply --execute. Inspect, create a fresh plan, then run
build, image-backup, fence-backup, activate and verify in order. Query status
after an uncertain result; mutation intents are never replayed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile
import uuid

import backup_images
import backup_release
import env_release
import ops
import release_preflight
import stage_build_release as stage

PHASE = "deleted-problem-resources-v1"
INSPECTION_KIND = "deleted-problem-inspection"
DEPENDENT_HEALTH_SERVICE = "personal-notification-runner"
CONTEXTS = (("learn", "services/learn"), ("env", "services/env-orchestrator"), ("frontend", "frontend"))
WORKERS = ("challenge-image-cleanup-runner", "education-asset-cleanup-runner", "knowledge-candidate-runner", "lesson-image-reconcile-runner")
FAMILIES = {"env-orchestrator": "env", "learn-service": "learn", **dict.fromkeys(WORKERS, "learn"), "frontend": "frontend"}
TARGETS = tuple(FAMILIES)
ACTIVATION_GROUPS = (("env-orchestrator",), ("learn-service", *WORKERS), ("frontend",))
HEADS = {"auth": "20261018_006", "learn": "20261020_032", "community": "20261013_005"}
SCOPE = {"phase": PHASE, "contexts": [context + "/" for _, context in CONTEXTS], "services": list(TARGETS),
         "activation_groups": [list(group) for group in ACTIVATION_GROUPS], "migrations": [],
         "backup_only_services": ["minio"], "automatic_restore": False}
REMOTE_ROOT = "/home/deploy/.ctf-web-renewal/deleted-problem-releases"
PHASES = ("build", "image-backup", "fence-backup", "activate", "verify")
HASH = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMPONENTS = frozenset({"images", "learn_database", "objects", "source", "protected", "runtime"})


class DeletedProblemReleaseError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise DeletedProblemReleaseError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def payload_digest(value):
    return hashlib.sha256(value).hexdigest()


def validate_binding(value):
    env_release.validate_binding(value)
    return value


def validate_scope(value):
    require(value == SCOPE, "EXACT_DELETED_PROBLEM_SCOPE_REQUIRED")
    return value


def file_rows(rows):
    return env_release.canonical_file_rows(rows)


def managed_source_contract(original_rows, current_rows, managed_rows, *, require_managed=True):
    original = file_rows(original_rows)
    current = file_rows(current_rows)
    managed = file_rows(managed_rows)
    require(managed and all(any(row["path"].startswith(context + "/") for _, context in CONTEXTS) for row in managed), "MANAGED_SOURCE_SCOPE_REQUIRED")
    names = {row["path"] for row in managed}
    old_extras = [row for row in original if row["path"] not in names]
    current_extras = [row for row in current if row["path"] not in names]
    require(old_extras == current_extras, "UNMANAGED_SOURCE_CHANGED")
    if require_managed:
        require([row for row in current if row["path"] in names] == managed, "MANAGED_SOURCE_MISSING_OR_CHANGED")
    return {"managed_file_count": len(managed), "preserved_extra_file_count": len(old_extras), "preserved_extras_sha256": payload_digest(canonical(old_extras))}


def context_name(name):
    path = env_release.safe_context_name(name) if isinstance(name, str) and name.startswith("services/env-orchestrator/") else stage.safe_source_path(name)
    require(any(name.startswith(context + "/") for _, context in CONTEXTS), "SOURCE_OUTSIDE_THREE_CONTEXTS")
    return path


def selected_rows(manifest):
    stage.validate_manifest(manifest, manifest["fingerprint"])
    rows = file_rows([row for row in manifest["files"] if any(row["path"].startswith(context + "/") for _, context in CONTEXTS)])
    require(all(any(row["path"].startswith(context + "/") for row in rows) for _, context in CONTEXTS), "THREE_CONTEXTS_REQUIRED")
    for row in rows:
        context_name(row["path"])
    return rows


def validate_payloads(payloads):
    require(payloads and len(payloads) <= stage.MAX_STAGE_FILES and sum(map(len, payloads.values())) <= stage.MAX_STAGE_BYTES, "SOURCE_SIZE_LIMIT")
    for name, data in payloads.items():
        context_name(name)
        if not name.startswith("services/env-orchestrator/"):
            stage.reject_secret_material(name, data)
    env_payloads = {name: data for name, data in payloads.items() if name.startswith("services/env-orchestrator/")}
    env_release.validate_context_payloads(env_payloads)
    stage.validate_context_payloads({name: data for name, data in payloads.items() if name not in env_payloads}, contexts=(CONTEXTS[0], CONTEXTS[2]))


def make_archive(rows, modes):
    payloads = {}
    for row in rows:
        context_name(row["path"])
        path = ops.SOURCE / row["path"]
        require(path.is_file() and not any(ops.linked(parent) for parent in (path, *path.parents) if parent.is_relative_to(ops.SOURCE)), "SOURCE_LINK_FORBIDDEN")
        data = path.read_bytes()
        require(len(data) == row["bytes"] and payload_digest(data) == row["sha256"], "SOURCE_CHANGED")
        payloads[row["path"]] = data
    validate_payloads(payloads)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(payloads.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), modes[name]
            require(item.mode in {0o644, 0o755}, "SOURCE_MODE_INVALID")
            archive.addfile(item, io.BytesIO(data))
    require(len(output.getvalue()) <= stage.MAX_STAGE_BYTES, "ARCHIVE_SIZE_LIMIT")
    return output.getvalue()


def selected_consumers(rows, *, running=True):
    result = {}
    for service in TARGETS:
        selected = [row for row in rows if row.get("project") == "ctf_web" and row.get("service") == service]
        require(len(selected) == 1 and IMAGE.fullmatch(selected[0].get("image_id", "")), "TARGET_SINGLE_IDENTITY_" + service)
        value = selected[0]
        if running:
            require(value.get("running") is True and (value.get("health") is None if service in WORKERS else value.get("health") == "healthy"), "TARGET_READINESS_" + service)
        result[service] = value
    for family in set(FAMILIES.values()):
        family_rows = [result[service] for service in TARGETS if FAMILIES[service] == family]
        require(len({row["image_id"] for row in family_rows}) == 1, "FAMILY_IMAGE_MISMATCH")
        image = family_rows[0]["image_id"]
        require({row["container_id"] for row in rows if row.get("image_id") == image} == {row["container_id"] for row in family_rows}, "UNKNOWN_SHARED_IMAGE_CONSUMER")
    return result


def preserved_snapshot(before, after, *, changed=(), stopped=False, minio_resumed=False, learn_maintenance=False):
    require(set(changed) <= set(TARGETS), "UNAUTHORIZED_CHANGED_SERVICE")
    require(type(learn_maintenance) is bool, "MAINTENANCE_PHASE_FLAG_REQUIRED")
    health_changes = []
    for key in ("protected_files", "databases"):
        require(before[key] == after[key], "PRESERVATION_" + key.upper())
    prefixes = tuple(context + "/" for _, context in CONTEXTS)
    require(file_rows([row for row in before["code_files"] if not row["path"].startswith(prefixes)])
            == file_rows([row for row in after["code_files"] if not row["path"].startswith(prefixes)]), "RETAINED_SOURCE_CHANGED")
    old_rows = {row["container_id"]: row for row in before["containers"]}
    new_rows = {row["container_id"]: row for row in after["containers"]}
    old_selected = selected_consumers(before["containers"], running=False)
    new_selected = selected_consumers(after["containers"], running=False)
    changed_ids = {old_selected[name]["container_id"] for name in changed} | {new_selected[name]["container_id"] for name in changed}
    require(set(old_rows) - changed_ids == set(new_rows) - changed_ids, "NON_TARGET_CONTAINER_SET_CHANGED")
    for identifier in set(old_rows) - changed_ids:
        old, new = old_rows[identifier], new_rows[identifier]
        service = old.get("service") if old.get("project") == "ctf_web" else None
        mutable = set()
        if stopped and service in TARGETS:
            mutable = {"running", "pid", "status", "started_at", "finished_at", "health", "ports"}
            require(new["running"] is False and new["pid"] == 0, "TARGET_NOT_FENCED")
        elif minio_resumed and service == "minio":
            mutable = {"pid", "started_at", "finished_at", "health"}
            require(new["running"] is True and new["health"] == "healthy", "MINIO_NOT_RESUMED")
        elif learn_maintenance and service == DEPENDENT_HEALTH_SERVICE:
            require(old["running"] is True and new["running"] is True
                    and old["health"] in {"healthy", "unhealthy", "starting"} and new["health"] in {"healthy", "unhealthy", "starting"}, "DEPENDENT_HEALTH_STATE_INVALID")
            mutable = {"health"}
            if old["health"] != new["health"]:
                health_changes.append({"service": service, "container_id": identifier, "from": old["health"], "to": new["health"], "phase": "learn-maintenance", "controlled": False})
        require({key: value for key, value in old.items() if key not in mutable}
                == {key: value for key, value in new.items() if key not in mutable}, "NON_TARGET_CONTAINER_CHANGED")
    for service in changed:
        require(new_selected[service]["runtime_sha256"] == old_selected[service]["runtime_sha256"], "TARGET_RUNTIME_CHANGED")
    if not changed:
        require(file_rows(before["code_files"]) == file_rows(after["code_files"]), "SOURCE_CHANGED_WITHOUT_ACTIVATION")
    return health_changes


def build_commands(source, binding, frontend_args):
    validate_binding(binding)
    stage.validate_frontend_args(frontend_args)
    source = PurePosixPath(str(source).replace("\\", "/"))
    require(source.is_absolute() and ".." not in source.parts, "REMOTE_BUILD_SOURCE_PATH")
    result = []
    for family, context in CONTEXTS:
        tag = "ctf-renewal/" + family + ":" + binding["commit"] + "-" + binding["release_nonce"]
        command = ["docker", "build", "--pull", "--network", "host", "--progress", "plain", "--file", str(source / context / "Dockerfile"), "--tag", tag]
        for key, value in source_labels(binding).items():
            command.extend(["--label", key + "=" + value])
        if family == "frontend":
            command.extend(["--target", "production", "--label", stage.FRONTEND_ARGS_LABEL + "=" + payload_digest(canonical(frontend_args))])
            for key in stage.FRONTEND_KEYS:
                command.extend(["--build-arg", key + "=" + frontend_args[key]])
        command.append(str(source / context))
        result.append((family, command))
    return result


def source_labels(binding):
    return {"org.opencontainers.image.revision": binding["commit"], "io.ctf-web.source-fingerprint": binding["source_fingerprint"], "io.ctf-web.release-nonce": binding["release_nonce"]}


def tools_binding():
    names = ("deleted_problem_release", "deleted_problem_release_remote", "env_release", "env_release_remote", "ops", "release_preflight", "stage_build_release", "backup_images", "backup_release")
    return {name: payload_digest(Path(__file__).with_name(name + ".py").read_bytes()) for name in names}


def load_json(path):
    filename = release_preflight.task_path(Path(path), ops.TASK)
    data = filename.read_bytes()
    return json.loads(data), payload_digest(data)


def receipt(name, value):
    return ops.receipt("deleted-problem-" + name + ".json", value)


def validate_plan(plan, *, current=True, age=True):
    require(plan.get("schema_version") == 1 and plan.get("kind") == "deleted-problem-release-plan", "PLAN_SCHEMA")
    validate_binding(plan["binding"])
    validate_scope(plan["scope"])
    require(plan["tool_sources"] == tools_binding(), "RELEASE_TOOLS_CHANGED")
    require(plan["managed_files"] == file_rows(plan["managed_files"]), "PLAN_SOURCE_ORDER")
    for row in plan["managed_files"]:
        context_name(row["path"])
    require(set(plan["modes"]) == {row["path"] for row in plan["managed_files"]} and all(mode in {0o644, 0o755} for mode in plan["modes"].values()), "PLAN_SOURCE_MODES")
    selected_consumers(plan["inspection"]["containers"])
    require({name: value["head"] for name, value in plan["inspection"]["databases"].items()} == {name: [head] for name, head in HEADS.items()}, "DATABASE_HEADS_CHANGED")
    stage.validate_frontend_args(plan["inspection"]["frontend_args"])
    if age:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(plan["created_at"])
        require(timedelta(0) <= delta <= timedelta(minutes=30), "PLAN_EXPIRED")
    if current:
        manifest = ops.manifest()
        require(manifest["fingerprint"] == plan["binding"]["source_fingerprint"] and selected_rows(manifest) == plan["managed_files"], "PLAN_SOURCE_CHANGED")
        stage.bind_checkout(ops.WORKSPACE / "deploy/CTF_Web", ops.SOURCE, manifest, plan["binding"])
    return plan


def private_destination(plan, name, attestation):
    value, _ = load_json(attestation)
    backup_release.validate_acl(value, backup_release.read_acl(backup_release.PRIVATE_ROOT))
    root = backup_release.PRIVATE_ROOT / (plan["binding"]["release_nonce"] + "-deleted-problem")
    require(not any(ops.linked(path) for path in (root, *root.parents) if path.is_relative_to(ops.TASK)), "PRIVATE_BACKUP_LINK")
    owner = {"binding": plan["binding"], "owner": PHASE}
    if root.exists():
        require((root / "owner.json").is_file() and json.loads((root / "owner.json").read_bytes()) == owner, "PRIVATE_BACKUP_OWNER")
    else:
        root.mkdir()
        with (root / "owner.json").open("xb") as output:
            output.write(canonical(owner))
    target = root / name
    require(not target.exists() and not target.is_symlink(), "PRIVATE_BACKUP_NO_OVERWRITE")
    require(__import__("shutil").disk_usage(root).free >= 2 * 1024 ** 3, "LOCAL_BACKUP_SPACE_GATE")
    return target


def validate_backup(value, plan, plan_sha):
    require(value.get("status") == "passed" and value.get("binding") == plan["binding"] and value.get("plan_sha256") == plan_sha
            and value.get("components") == sorted(COMPONENTS) and value.get("fenced_services") == list(TARGETS)
            and value.get("automatic_restore") is False and value.get("minio_resumed_same_identity") is True, "BACKUP_FENCE_INCOMPLETE")
    files = value.get("files", [])
    require(len(files) == 2, "BACKUP_TWO_ARCHIVES_REQUIRED")
    env_release.validate_private_files(files)
    return value


def remote_program(action, request, archive=None):
    # Retain helper implementations and their contracts without modifying old scopes.
    stage_functions = (stage.require, stage.canonical, stage.digest, stage.safe_source_path, stage.reject_secret_material, stage.validate_frontend_args,
                       stage.frontend_runtime_args, stage.frontend_args_from_inspection, stage.runtime_probe_command, stage.validate_runtime_probe)
    stage_code = "from pathlib import Path,PurePosixPath\nimport re,json,hashlib\nfrom urllib.parse import urlsplit\nclass StageError(RuntimeError): pass\n"
    for name in ("CONTEXTS", "FRONTEND_KEYS", "FRONTEND_ARGS_LABEL", "FORBIDDEN_PARTS", "FORBIDDEN_NAMES", "ENV_TEMPLATES", "SECRET_PATTERNS", "MAX_STAGE_BYTES"):
        stage_code += name + "=" + repr(CONTEXTS if name == "CONTEXTS" else getattr(stage, name)) + "\n"
    stage_code += "\n".join(inspect.getsource(function) for function in stage_functions)
    # Env helpers are pure serialization/runtime normalization; no old release main is injected.
    env_remote = Path(__file__).with_name("env_release_remote.py").read_text(encoding="utf-8")
    module = __import__("ast").parse(env_remote)
    env_functions = [__import__("ast").get_source_segment(env_remote, node) for node in module.body if isinstance(node, __import__("ast").FunctionDef) and node.name in {"normalized_environment", "runtime_hash"}]
    prefix = release_preflight.REMOTE_HELPERS + "\nfrom pathlib import Path,PurePosixPath\nimport base64,fcntl,time,uuid,shlex\n"
    constants = {"PHASE": PHASE, "INSPECTION_KIND": INSPECTION_KIND, "DEPENDENT_HEALTH_SERVICE": DEPENDENT_HEALTH_SERVICE, "CONTEXTS": CONTEXTS, "WORKERS": WORKERS, "FAMILIES": FAMILIES, "TARGETS": TARGETS, "ACTIVATION_GROUPS": ACTIVATION_GROUPS, "HEADS": HEADS,
                 "SCOPE": SCOPE, "REMOTE_ROOT": REMOTE_ROOT, "PHASES": PHASES, "COMPONENTS": COMPONENTS, "MAX_STAGE_BYTES": stage.MAX_STAGE_BYTES, "MAX_STAGE_FILES": stage.MAX_STAGE_FILES}
    prefix += "\n".join(name + "=" + repr(value) for name, value in constants.items())
    prefix += "\nHASH=re.compile(r'[0-9a-f]{64}\\Z');IMAGE=re.compile(r'sha256:[0-9a-f]{64}\\Z')\nclass DeletedProblemReleaseError(RuntimeError): pass\n"
    prefix += "\n".join(inspect.getsource(function) for function in (require, canonical, payload_digest, source_labels, selected_consumers, preserved_snapshot))
    prefix += "\n".join(env_functions)
    prefix += "\n_STAGE={};exec(" + repr(stage_code) + ",_STAGE)\nimport types\nstage=types.SimpleNamespace(**_STAGE)\n"
    prefix += inspect.getsource(env_release.validate_binding).replace("EnvReleaseError", "DeletedProblemReleaseError")
    prefix += inspect.getsource(build_commands)
    prefix += "\n" + inspect.getsource(env_release.canonical_file_rows).replace("canonical_file_rows", "file_rows")
    prefix += inspect.getsource(managed_source_contract)
    prefix += "\ndef validate_scope(value):\n    require(value==SCOPE,'EXACT_DELETED_PROBLEM_SCOPE_REQUIRED')\n"
    prefix += "\nREQUEST=" + repr(request) + "\nACTION=" + repr(action) + "\nARCHIVE=" + repr(base64.b64encode(archive or b"").decode()) + "\n"
    return prefix + Path(__file__).with_name("deleted_problem_release_remote.py").read_text(encoding="utf-8") + "\nmain()\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "plan", *PHASES, "status"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--inspection", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--nonce")
    parser.add_argument("--acl-attestation", type=Path)
    args = parser.parse_args(argv)
    remote_attempted = False

    def call(action, request, archive=None):
        nonlocal remote_attempted
        remote_attempted = True
        return ops.remote_python(remote_program(action, request, archive), timeout=2400)

    try:
        if args.action == "inspect":
            if not args.execute:
                print(json.dumps({"planned": "read-only full daemon / source / schema inspection", "network": False, "scope": SCOPE})); return 0
            value = call("inspect", {"scope": SCOPE})
            require(value.get("status") == "passed", "INSPECTION_UNVERIFIED")
            output = receipt("inspection-" + str(uuid.uuid4()), value)
        elif args.action == "plan":
            require(args.inspection and args.commit and args.nonce, "PLAN_INPUTS_REQUIRED")
            observed, observed_sha = load_json(args.inspection)
            require(observed.get("kind") == INSPECTION_KIND and observed.get("status") == "passed"
                    and isinstance(observed.get("observed_at"), (int, float)) and 0 <= datetime.now(timezone.utc).timestamp() - observed["observed_at"] <= 1800, "INSPECTION_UNVERIFIED_OR_EXPIRED")
            manifest = ops.manifest()
            binding = validate_binding({"commit": args.commit, "source_fingerprint": manifest["fingerprint"], "release_nonce": args.nonce})
            modes = stage.bind_checkout(ops.WORKSPACE / "deploy/CTF_Web", ops.SOURCE, manifest, binding)
            rows = selected_rows(manifest)
            make_archive(rows, modes)
            plan = {"schema_version": 1, "kind": "deleted-problem-release-plan", "created_at": datetime.now(timezone.utc).isoformat(),
                    "binding": binding, "scope": SCOPE, "inspection": observed["snapshot"], "inspection_sha256": observed_sha,
                    "managed_files": rows, "modes": {row["path"]: modes[row["path"]] for row in rows}, "tool_sources": tools_binding()}
            validate_plan(plan)
            output = receipt("plan-" + args.nonce, plan)
        else:
            require(args.plan, "PLAN_REQUIRED")
            plan, plan_sha = load_json(args.plan)
            validate_plan(plan, current=args.action != "status", age=args.action in {"build", "image-backup", "fence-backup", "activate"})
            request = {"plan": plan, "plan_sha256": plan_sha, "scope": SCOPE}
            if not args.execute:
                print(json.dumps({"planned": args.action, "binding": plan["binding"], "scope": SCOPE, "network": False})); return 0
            if args.action == "image-backup":
                require(args.acl_attestation, "ACL_ATTESTATION_REQUIRED")
                destination = private_destination(plan, "old-images.tar", args.acl_attestation)
                prepared = call(args.action, request)
                require(prepared.get("status") == "passed", "IMAGE_BACKUP_PRECONDITION_UNCERTAIN")
                artifact = ops.streaming_ssh(shlex.join(["docker", "image", "save", *prepared["image_ids"]]), destination) | {"path": str(destination)}
                backup_images.validate_image_archive(destination, prepared["image_ids"], prepared["image_metadata"])
                value = call("image-backup-complete", request | {"artifact": artifact})
                value["files"] = [artifact]
            elif args.action == "fence-backup":
                require(args.acl_attestation, "ACL_ATTESTATION_REQUIRED")
                prior, _ = load_json(ops.TASK / "evidence" / ("deleted-problem-image-backup-" + plan["binding"]["release_nonce"] + ".json"))
                require(prior.get("status") == "passed" and prior.get("binding") == plan["binding"] and prior.get("plan_sha256") == plan_sha, "IMAGE_BACKUP_NOT_BOUND")
                env_release.validate_private_files(prior["files"])
                destination = private_destination(plan, "fenced-state.tar", args.acl_attestation)
                prepared = call(args.action, request)
                require(prepared.get("status") == "passed", "FENCE_BACKUP_UNCERTAIN_QUERY_STATUS")
                artifact = ops.streaming_ssh(shlex.join(["cat", prepared["private_archive"]]), destination) | {"path": str(destination)}
                require(artifact["bytes"] == prepared["archive_bytes"] and artifact["sha256"] == prepared["archive_sha256"], "BACKUP_STREAM_CHANGED")
                with tarfile.open(destination, "r:") as archive:
                    members = archive.getmembers()
                    require({item.name for item in members} == {"learn.dump", "objects.tar", "source.tar", "protected.tar", "runtime.json", "state.json"}
                            and len(members) == 6 and all(item.isfile() for item in members), "BACKUP_ARCHIVE_COMPONENTS")
                value = call("fence-backup-complete", request | {"artifact": artifact})
                value["files"] = prior["files"] + [artifact]
                validate_backup(value, plan, plan_sha)
            else:
                if args.action == "activate":
                    captured, _ = load_json(ops.TASK / "evidence" / ("deleted-problem-fence-backup-" + plan["binding"]["release_nonce"] + ".json"))
                    validate_backup(captured, plan, plan_sha)
                if args.action == "build":
                    request["build_commands"] = build_commands(PurePosixPath(REMOTE_ROOT) / plan["binding"]["release_nonce"] / "source", plan["binding"], plan["inspection"]["frontend_args"])
                value = call(args.action, request, make_archive(plan["managed_files"], plan["modes"]) if args.action == "build" else None)
            suffix = "-" + str(uuid.uuid4()) if args.action in {"status", "verify"} else ""
            output = receipt(args.action + "-" + plan["binding"]["release_nonce"] + suffix, value)
            if value.get("status") != "passed":
                print(json.dumps({"status": "uncertain", "receipt": str(output), "next": "status; no replay or automatic restore"})); return 2
        print(json.dumps({"status": "passed", "receipt": str(output)})); return 0
    except Exception as error:
        print(json.dumps({"status": "uncertain" if remote_attempted else "failed", "code": str(error) if isinstance(error, (DeletedProblemReleaseError, env_release.EnvReleaseError, stage.StageError, release_preflight.ReleaseError)) else "REVIEW_REQUIRED",
                          "automatic_restore": False, "next": "status; no replay or automatic restore" if remote_attempted else "review inputs"})); return 2 if remote_attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
