"""D2 Env-only release CLI. Mutations require --execute; no automatic restore.

Run inspect first, then plan --inspection RECEIPT --baseline-release ACT BUILD
VERIFY (repeat the baseline triple in historical order). The plan binds the
committed clean copy, complete source fingerprint and a fresh UUID nonce.
Stages: build, image-backup, fence-backup, activate, verify. status is read-only;
resume-original is explicit and only allowed before any activation intent.
Return codes: 0 verified/planned, 1 failed, 2 uncertain (query status, no replay).
Private backups require the existing private-backup ACL attestation.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import tarfile
import uuid

import ops
import backup_images
import backup_release
import release_preflight
import stage_build_release as stage
import verify_release

SERVICE = "env-orchestrator"
PREFIX = "services/env-orchestrator/"
SCOPE = {"contexts": [PREFIX], "services": [SERVICE], "stop_services": [SERVICE],
         "migrations": [], "automatic_restore": False}
COMPILER = "go1.27.2"
COMPILER_IMAGE = "golang:1.27.2-alpine3.24"
RUNTIME_IMAGE = "alpine:3.24.2"
MINIMUM_GO = "1.26.0"
MAIN_MODULE = "github.com/ctf-platform/env-orchestrator"
MODULE_VERSIONS = {"github.com/redis/go-redis/v9": "v9.6.3", "golang.org/x/net": "v0.60.0", "golang.org/x/crypto": "v0.57.0",
                   "golang.org/x/sys": "v0.48.0", "golang.org/x/term": "v0.46.0", "golang.org/x/text": "v0.42.0"}
REMOTE_ROOT = "/home/deploy/.ctf-web-renewal/env-releases"
PHASES = ("build", "image-backup", "fence-backup", "activate", "verify")
COMPONENTS = frozenset({"image", "source", "config", "flags", "redis_db2"})
HASH = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class EnvReleaseError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise EnvReleaseError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def payload_digest(value):
    return hashlib.sha256(value).hexdigest()


def validate_binding(binding):
    require(isinstance(binding, dict) and set(binding) == {"commit", "source_fingerprint", "release_nonce"}, "BINDING_FIELDS")
    require(re.fullmatch(r"[0-9a-f]{40}", binding["commit"] or "") and HASH.fullmatch(binding["source_fingerprint"] or ""), "BINDING_HASH")
    try:
        nonce = uuid.UUID(binding["release_nonce"])
        require(nonce.version == 4 and str(nonce) == binding["release_nonce"], "BINDING_NONCE")
    except (ValueError, TypeError, AttributeError) as error:
        raise EnvReleaseError("BINDING_NONCE") from error
    return binding


def validate_scope(scope):
    require(scope == SCOPE, "ENV_ONLY_SCOPE_REQUIRED")
    return scope


def select_consumer(rows):
    selected = [row for row in rows if row.get("service") == SERVICE]
    require(len(selected) == 1 and selected[0].get("running") is True and selected[0].get("health") == "healthy"
            and IMAGE.fullmatch(selected[0].get("image_id", "")), "ENV_SINGLE_RUNNING_CONSUMER")
    require(len([row for row in rows if row.get("image_id") == selected[0]["image_id"]]) == 1, "ENV_UNKNOWN_IMAGE_CONSUMER")
    return selected[0]


def canonical_file_rows(rows):
    require(isinstance(rows, list), "SOURCE_ROWS_SHAPE")
    names = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}, "SOURCE_ROW_FIELDS")
        name = row["path"]
        require(isinstance(name, str) and name and "\\" not in name and ":" not in name
                and not any(ord(character) < 32 or ord(character) == 127 for character in name), "SOURCE_ROW_PATH")
        parsed = PurePosixPath(name)
        require(not parsed.is_absolute() and ".." not in parsed.parts and parsed.as_posix() == name
                and name not in names, "SOURCE_ROW_PATH_OR_DUPLICATE")
        require(type(row["bytes"]) is int and row["bytes"] >= 0 and isinstance(row["sha256"], str)
                and HASH.fullmatch(row["sha256"]), "SOURCE_ROW_CONTENT")
        names.add(name)
    return sorted(rows, key=lambda row: row["path"])


def context_rows(manifest):
    stage.validate_manifest(manifest, manifest["fingerprint"])
    rows = [row for row in manifest["files"] if row["path"].startswith(PREFIX)]
    require(rows and all(PurePosixPath(row["path"]).as_posix() == row["path"] and ".." not in PurePosixPath(row["path"]).parts for row in rows), "ENV_CONTEXT_REQUIRED")
    require({PREFIX + name for name in ("Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd/main.go")} <= {row["path"] for row in rows}, "ENV_CONTEXT_INCOMPLETE")
    return canonical_file_rows(rows)


def safe_context_name(name):
    require(isinstance(name, str) and name.startswith(PREFIX) and "\\" not in name and ":" not in name
            and not any(ord(character) < 32 or ord(character) == 127 for character in name), "ENV_CONTEXT_PATH")
    path = PurePosixPath(name)
    require(path.as_posix() == name and not path.is_absolute() and ".." not in path.parts
            and not any(part in stage.FORBIDDEN_PARTS for part in path.parts) and path.name not in stage.FORBIDDEN_NAMES
            and not path.name.lower().endswith("cookies.txt") and path.suffix.lower() not in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc", ".log", ".zip"}
            and (not path.name.startswith(".env") or path.name in stage.ENV_TEMPLATES) and path.parts[2] != "data", "ENV_CONTEXT_PATH")
    return path


def validate_context_payloads(payloads):
    require(payloads and all(name.startswith(PREFIX) for name in payloads), "ENV_CONTEXT_SCOPE")
    require(sum(map(len, payloads.values())) <= stage.MAX_STAGE_BYTES and len(payloads) <= stage.MAX_STAGE_FILES, "ENV_CONTEXT_SIZE")
    for name, payload in payloads.items():
        path = safe_context_name(name)
        require(not any(re.search(pattern, payload) for pattern in stage.SECRET_PATTERNS), "SOURCE_SECRET_MATERIAL_FORBIDDEN")
        if path.name in stage.ENV_TEMPLATES:
            for line in payload.decode("utf-8-sig").splitlines():
                match = re.match(r"(?i)^\s*[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|ACCESS_TOKEN|SMTP_PASS)[A-Z0-9_]*\s*=\s*(.*?)\s*$", line)
                if match:
                    value = match[1].strip("'\"")
                    require(not value or "${" in value or value.startswith(("<", "$", "/")) or re.search(r"(?i)change[_-]?me|replace[_-]?me|placeholder|your[_-]", value), "SOURCE_ENV_LITERAL_FORBIDDEN")
    required = {PREFIX + name for name in ("Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd/main.go")}
    require(required <= set(payloads), "ENV_CONTEXT_INCOMPLETE")
    dockerfile = payloads[PREFIX + "Dockerfile"].decode("utf-8-sig")
    require(re.search(r"(?m)^FROM " + re.escape(COMPILER_IMAGE) + r" AS builder\s*$", dockerfile)
            and re.search(r"(?m)^FROM " + re.escape(RUNTIME_IMAGE) + r"\s*$", dockerfile)
            and re.search(r"(?m)^ENV GOTOOLCHAIN=local GOTELEMETRY=off\s*$", dockerfile)
            and "CGO_ENABLED=0 GOOS=linux go build -mod=readonly" in dockerfile, "ENV_COMPILER_CONTRACT")
    modules = payloads[PREFIX + "go.mod"].decode("utf-8-sig")
    term = "golang.org/x/term"
    root_requirements = {name: version for name, version in MODULE_VERSIONS.items() if name != term}
    listed_term = re.findall(r"(?m)^\s*(?:require\s+)?" + re.escape(term) + r"\s+(\S+?)(?:\s*//[^\r\n]*)?\s*$", modules)
    require(re.search(r"(?m)^go " + re.escape(MINIMUM_GO) + r"\s*$", modules)
            and re.search(r"(?m)^module " + re.escape(MAIN_MODULE) + r"\s*$", modules)
            and all(re.search(r"(?m)^\s*" + re.escape(name) + r" " + re.escape(version) + r"(?:\s*// indirect)?\s*$", modules) for name, version in root_requirements.items())
            and (not listed_term or listed_term == [MODULE_VERSIONS[term]]), "ENV_MODULE_CONTRACT")
    patterns = {line.strip() for line in payloads[PREFIX + ".dockerignore"].decode("utf-8-sig").splitlines()}
    require({".git", ".env", ".env.*"} <= patterns, "ENV_IGNORE_CONTRACT")


def build_commands(source, binding):
    validate_binding(binding)
    tag = "ctf-renewal/env:" + binding["commit"] + "-" + binding["release_nonce"]
    common = ["docker", "build", "--pull", "--network", "host", "--progress", "plain", "--file", str(Path(source) / PREFIX / "Dockerfile")]
    labels = [item for key, value in (("org.opencontainers.image.revision", binding["commit"]),
              ("io.ctf-web.source-fingerprint", binding["source_fingerprint"]), ("io.ctf-web.release-nonce", binding["release_nonce"])) for item in ("--label", key + "=" + value)]
    return [("runtime", common + labels + ["--tag", tag, str(Path(source) / PREFIX)]),
            ("builder", common + labels + ["--target", "builder", "--tag", tag + "-tests", str(Path(source) / PREFIX)])]


def validate_module_graph(rows):
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) and isinstance(row.get("Path"), str) for row in rows), "MODULE_GRAPH_SHAPE")
    require(len({row["Path"] for row in rows}) == len(rows) and not any(row.get("Replace") for row in rows), "MODULE_GRAPH_REPLACEMENT_OR_DUPLICATE")
    main = [row for row in rows if row.get("Main") is True]
    require(len(main) == 1 and main[0]["Path"] == MAIN_MODULE and main[0].get("GoVersion") == MINIMUM_GO, "MODULE_MAIN_GO_CONTRACT")
    selected = {row["Path"]: row.get("Version") for row in rows if row["Path"] in MODULE_VERSIONS}
    require(selected == MODULE_VERSIONS, "MODULE_SELECTED_VERSION_CONTRACT")
    return {"main_module": MAIN_MODULE, "minimum_go": MINIMUM_GO, "selected_modules": selected, "module_count": len(rows), "graph_sha256": payload_digest(canonical(rows))}


def runtime_probe_command(image, name):
    match = re.fullmatch(r"ctf-env-(builder-)?probe-([a-f0-9-]{36})", name or "")
    require(IMAGE.fullmatch(image or "") and match is not None, "PROBE_IDENTITY")
    nonce = match[2]
    validate_binding({"commit": "0" * 40, "source_fingerprint": "0" * 64, "release_nonce": nonce})
    binary = "/env-orchestrator" if match[1] else "/usr/local/bin/env-orchestrator"
    return ["docker", "run", "--rm", "--name", name, "--label", "io.ctf-web.env-test-nonce=" + nonce, "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "32", "--memory", "128m", "--cpus", "0.5",
            "--user", "1001:1001", "--entrypoint", "cat", image, binary]


def validate_runtime_probe(value, binding):
    validate_binding(binding)
    require(isinstance(value, dict) and value.get("uid") == 1001 and value.get("elf_magic") == "7f454c46"
            and value.get("elf_machine") in {62, 183} and HASH.fullmatch(value.get("binary_sha256", ""))
            and value.get("builder_binary_sha256") == value.get("binary_sha256")
            and value.get("compiler") == COMPILER and value.get("cgo_enabled") == "0"
            and value.get("build_source") == binding, "BINARY_BUILD_PROOF_REQUIRED")
    startup = value.get("startup", {})
    require(startup.get("exit_code") == 1 and startup.get("redis_failure_observed") is True
            and startup.get("production_io") is False and startup.get("timed_out") is False
            and startup.get("health_success_claimed") is False, "ISOLATED_STARTUP_PROOF_REQUIRED")
    return value


def preserved_snapshot(before, after, *, env_changed=False, env_stopped=False):
    for key in ("protected_files", "labs", "databases"):
        require(before[key] == after[key], "PRESERVATION_" + key.upper())
    require([row for row in before["containers"] if row["service"] != SERVICE] == [row for row in after["containers"] if row["service"] != SERVICE], "NON_TARGET_CONTAINER_CHANGED")
    require(canonical_file_rows([row for row in before["code_files"] if not row["path"].startswith(PREFIX)])
            == canonical_file_rows([row for row in after["code_files"] if not row["path"].startswith(PREFIX)]), "RETAINED_SOURCE_CHANGED")
    require(before["retained_labels"] == after["retained_labels"], "RETAINED_PROVENANCE_CHANGED")
    require(before["target_runtime_sha256"] == after["target_runtime_sha256"], "ENV_RUNTIME_CHANGED")
    if not env_changed:
        require(canonical_file_rows(before["code_files"]) == canonical_file_rows(after["code_files"]), "ENV_SOURCE_CHANGED")
        old = next(row for row in before["containers"] if row["service"] == SERVICE)
        new = next(row for row in after["containers"] if row["service"] == SERVICE)
        if env_stopped:
            require(all(new[key] == old[key] for key in ("container_id", "image_id", "mounts")) and new["running"] is False and new["pid"] == 0, "ENV_STOP_FENCE_CHANGED")
        else:
            require(new == old, "ENV_BASELINE_CHANGED")


def snapshot_drift(before, after):
    observations = {key: (before[key], after[key]) for key in ("protected_files", "labs", "databases", "retained_labels", "target_runtime_sha256")}
    observations["non_target_containers"] = ([row for row in before["containers"] if row["service"] != SERVICE], [row for row in after["containers"] if row["service"] != SERVICE])
    observations["retained_source"] = (canonical_file_rows([row for row in before["code_files"] if not row["path"].startswith(PREFIX)]),
                                       canonical_file_rows([row for row in after["code_files"] if not row["path"].startswith(PREFIX)]))
    return [{"scope": key, "original_sha256": payload_digest(canonical(old)), "current_sha256": payload_digest(canonical(new))}
            for key, (old, new) in observations.items() if old != new]


def validate_intent(existing, action, binding, parent_sha):
    validate_binding(binding)
    require(action in (*PHASES, "resume-original", "fence-current") and HASH.fullmatch(parent_sha or ""), "INTENT_SCOPE")
    require(existing is None, "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
    return {"action": action, "binding": binding, "parent_sha256": parent_sha}


def validate_backup(value, binding, plan_sha):
    require(value.get("kind") == "env-fence-backup" and value.get("binding") == binding and value.get("plan_sha256") == plan_sha, "BACKUP_BINDING")
    require(set(value.get("components", [])) == COMPONENTS and value.get("env_stopped") is True
            and value.get("other_db2_clients") == 0 and value.get("other_services_controlled") == 0
            and value.get("redis_database") == 2 and value.get("automatic_restore") is False, "BACKUP_FENCE_INCOMPLETE")
    require(len(value.get("files", [])) == 2 and all(type(row.get("bytes")) is int and row["bytes"] > 0 and HASH.fullmatch(row.get("sha256", "")) for row in value["files"]), "BACKUP_ARTIFACT_MISSING")
    return value


def phase_dependencies(action):
    require(action in PHASES, "PHASE_INVALID")
    return PHASES[:PHASES.index(action)]


def execute_serial(commands, execute):
    completed = []
    for phase, command in commands:
        result = execute(command)
        require(result == 0, "SERIAL_STEP_FAILED_" + phase.upper())
        completed.append(phase)
    return completed


def tools_binding():
    return {name: digest(path.read_bytes()) for name, path in (("env_release", Path(__file__)), ("env_release_remote", Path(__file__).with_name("env_release_remote.py")),
            ("ops", Path(ops.__file__)), ("preflight", Path(release_preflight.__file__)), ("image_archive", Path(backup_images.__file__)))}


def load_json(path):
    path = release_preflight.task_path(Path(path), ops.TASK)
    payload = path.read_bytes()
    return json.loads(payload), digest(payload)


def receipt(name, value):
    return ops.receipt("env-" + name + ".json", value)


def make_archive(rows, modes):
    for row in rows:
        safe_context_name(row["path"])
        filename = ops.SOURCE / row["path"]
        require(filename.is_file() and not any(ops.linked(parent) for parent in (filename, *filename.parents) if parent.is_relative_to(ops.SOURCE)), "CONTEXT_FILE_LINK")
    payloads = {row["path"]: (ops.SOURCE / row["path"]).read_bytes() for row in rows}
    validate_context_payloads(payloads)
    require(all(len(payloads[row["path"]]) == row["bytes"] and digest(payloads[row["path"]]) == row["sha256"] for row in rows), "CONTEXT_CHANGED")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(payloads):
            info = tarfile.TarInfo(name); info.size = len(payloads[name]); info.mode = modes[name]
            archive.addfile(info, io.BytesIO(payloads[name]))
    return stream.getvalue()


def remote(action, request, archive=None):
    request = dict(request) | {"retained_services": dict(verify_release.SERVICE_IMAGES)}
    code = Path(__file__).with_name("env_release_remote.py").read_text(encoding="utf-8")
    pure = "\n".join(__import__("inspect").getsource(function) for function in (require, canonical, payload_digest, validate_binding, validate_scope, select_consumer, canonical_file_rows,
            preserved_snapshot, snapshot_drift, validate_runtime_probe, validate_module_graph, runtime_probe_command, build_commands))
    prefix = release_preflight.REMOTE_HELPERS + "\nfrom pathlib import Path,PurePosixPath\nimport uuid,base64,struct,socket,urllib.parse,fcntl,secrets,time,signal\n"
    constants = {"SERVICE": SERVICE, "PREFIX": PREFIX, "SCOPE": SCOPE, "COMPILER": COMPILER, "COMPILER_IMAGE": COMPILER_IMAGE, "RUNTIME_IMAGE": RUNTIME_IMAGE,
                 "REMOTE_ROOT": REMOTE_ROOT, "MINIMUM_GO": MINIMUM_GO, "MAIN_MODULE": MAIN_MODULE, "MODULE_VERSIONS": MODULE_VERSIONS,
                 "LEGACY_WORKERS": tuple(verify_release.LEGACY_PROCESS_WORKERS)}
    prefix += "\n".join(name + "=" + repr(value) for name, value in constants.items())
    prefix += "\nHASH=re.compile(r'[0-9a-f]{64}\\Z');IMAGE=re.compile(r'sha256:[0-9a-f]{64}\\Z')\nclass EnvReleaseError(RuntimeError): pass\n"
    prefix += pure
    prefix += "\nREQUEST=" + repr(request) + "\nACTION=" + repr(action) + "\nARCHIVE=" + repr(base64.b64encode(archive or b"").decode()) + "\n"
    program = prefix + code + "\nmain()\n"
    return ops.remote_python(program, timeout=2400)


def validate_plan(plan, *, current=True, age=True):
    require(plan.get("kind") == "env-release-plan" and plan.get("schema_version") == 1, "PLAN_SCHEMA")
    validate_binding(plan["binding"]); validate_scope(plan["scope"])
    require(plan["tool_sources"] == tools_binding(), "RELEASE_TOOLS_CHANGED")
    require(plan["managed_files"] and all(row["path"].startswith(PREFIX) for row in plan["managed_files"]), "PLAN_SOURCE_SCOPE")
    canonical_file_rows(plan["managed_files"])
    for row in plan["managed_files"]: safe_context_name(row["path"])
    require(set(plan["modes"]) == {row["path"] for row in plan["managed_files"]} and all(mode in {0o644, 0o755} for mode in plan["modes"].values()), "PLAN_SOURCE_MODES")
    select_consumer(plan["inspection"]["containers"])
    if age:
        delta = datetime.now(timezone.utc) - datetime.fromisoformat(plan["created_at"])
        require(timedelta(0) <= delta <= timedelta(minutes=30), "PLAN_EXPIRED")
    if current:
        manifest = ops.manifest()
        require(manifest["fingerprint"] == plan["binding"]["source_fingerprint"]
                and context_rows(manifest) == canonical_file_rows(plan["managed_files"]), "PLAN_SOURCE_CHANGED")
    return plan


def private_destination(plan, name, attestation):
    root = backup_release.PRIVATE_ROOT
    value, _ = load_json(attestation)
    backup_release.validate_acl(value, backup_release.read_acl(root))
    target = root / (plan["binding"]["release_nonce"] + "-env")
    require(not ops.linked(target) and not any(ops.linked(parent) for parent in target.parents if parent.is_relative_to(ops.TASK)), "PRIVATE_BACKUP_LINK")
    owner = {"binding": plan["binding"], "owner": "env-release-private-backup"}
    if target.exists():
        require((target / "owner.json").is_file() and json.loads((target / "owner.json").read_bytes()) == owner, "PRIVATE_BACKUP_OWNER")
    else:
        target.mkdir()
        with (target / "owner.json").open("xb") as output:
            output.write(canonical(owner))
    filename = target / name
    require(not filename.exists() and not filename.is_symlink(), "PRIVATE_BACKUP_NO_OVERWRITE")
    return filename


def validate_private_files(files):
    require(files and len({row["path"] for row in files}) == len(files), "BACKUP_FILES_INVALID")
    for artifact in files:
        filename = release_preflight.task_path(Path(artifact["path"]), backup_release.PRIVATE_ROOT)
        require(filename.is_file() and not any(ops.linked(parent) for parent in (filename, *filename.parents) if parent.is_relative_to(backup_release.PRIVATE_ROOT))
                and filename.stat().st_size == artifact["bytes"], "BACKUP_FILE_MISSING")
        with filename.open("rb") as data:
            require(hashlib.file_digest(data, "sha256").hexdigest() == artifact["sha256"], "BACKUP_FILE_CHANGED")


def image_backup_receipt(plan, plan_sha):
    value, _ = load_json(ops.TASK / "evidence" / ("env-image-backup-" + plan["binding"]["release_nonce"] + ".json"))
    require(value.get("status") == "passed" and value.get("binding") == plan["binding"] and value.get("plan_sha256") == plan_sha
            and value.get("image_id") == select_consumer(plan["inspection"]["containers"])["image_id"] and len(value.get("files", [])) == 1, "IMAGE_BACKUP_RECEIPT_UNBOUND")
    validate_private_files(value["files"])
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("inspect", "plan", *PHASES, "status", "resume-original", "fence-current"))
    parser.add_argument("--execute", action="store_true", help="Only root operator executes network/Docker; default is local plan")
    parser.add_argument("--plan", type=Path); parser.add_argument("--inspection", type=Path)
    parser.add_argument("--commit"); parser.add_argument("--nonce")
    parser.add_argument("--baseline-release", nargs=3, action="append", metavar=("ACTIVATION", "BUILD", "VERIFY"))
    parser.add_argument("--acl-attestation", type=Path)
    args = parser.parse_args(argv)
    remote_attempted = False
    def call_remote(action, request, archive=None):
        nonlocal remote_attempted
        remote_attempted = True
        return remote(action, request, archive)
    try:
        if args.action == "inspect":
            if not args.execute:
                print(json.dumps({"planned": "readonly Env/Compose/challenge snapshot", "network": False})); return 0
            result = call_remote("inspect", {"scope": SCOPE})
            require(result.get("status") == "passed", "INSPECTION_FAILED")
            output = receipt("inspection-" + str(uuid.uuid4()), result)
        elif args.action == "plan":
            require(args.inspection and args.commit and args.nonce and args.baseline_release, "PLAN_INPUTS_REQUIRED")
            observed, observed_sha = load_json(args.inspection)
            require(observed.get("kind") == "env-inspection" and observed.get("status") == "passed", "INSPECTION_UNVERIFIED")
            require(type(observed.get("observed_at")) in {int, float} and 0 <= datetime.now(timezone.utc).timestamp() - observed["observed_at"] <= 1800, "INSPECTION_EXPIRED")
            manifest = ops.manifest(); binding = validate_binding({"commit": args.commit, "source_fingerprint": manifest["fingerprint"], "release_nonce": args.nonce})
            baseline = verify_release.load_baseline_chain([[Path(item) for item in triple] for triple in args.baseline_release])
            selected = context_rows(manifest)
            retained = sorted((row for row in manifest["files"] if row["path"].startswith(verify_release.CONTEXTS)), key=lambda row: row["path"])
            require(canonical_file_rows(retained) == canonical_file_rows(baseline["contract"]["managed_files"]), "RETAINED_SOURCE_BASELINE_CHANGED")
            modes = stage.bind_checkout(ops.WORKSPACE / "deploy/CTF_Web", ops.SOURCE, manifest, binding)
            plan = {"schema_version": 1, "kind": "env-release-plan", "created_at": datetime.now(timezone.utc).isoformat(), "binding": binding, "scope": SCOPE,
                    "inspection": observed["snapshot"], "inspection_sha256": observed_sha, "managed_files": selected,
                    "modes": {row["path"]: modes[row["path"]] for row in selected}, "tool_sources": tools_binding(), "retained_baseline": baseline}
            validate_plan(plan)
            output = receipt("plan-" + args.nonce, plan)
        else:
            require(args.plan is not None, "PLAN_INPUT_REQUIRED")
            plan, plan_sha = load_json(args.plan)
            recovery = args.action in {"status", "resume-original", "fence-current"}
            validate_plan(plan, current=not recovery, age=args.action in {"image-backup", "fence-backup", "activate"})
            request = {"plan": plan, "plan_sha256": plan_sha, "scope": SCOPE}
            if not args.execute:
                print(json.dumps({"kind": "env-action-plan", "action": args.action, "binding": plan["binding"], "scope": SCOPE,
                                  "automatic_restore": False, "network": False})); return 0
            if args.action == "image-backup":
                require(args.acl_attestation is not None, "ACL_ATTESTATION_REQUIRED")
                destination = private_destination(plan, "old-env-image.tar", args.acl_attestation)
                prepared = call_remote("image-backup", request)
                require(prepared.get("status") == "passed", "IMAGE_BACKUP_PRECONDITION")
                artifact = ops.streaming_ssh(shlex.join(["docker", "image", "save", prepared["image_id"]]), destination) | {"path": str(destination)}
                backup_images.validate_image_archive(destination, [prepared["image_id"]], [prepared["image_metadata"]])
                result = call_remote("image-backup-complete", request | {"artifact": artifact})
                result["files"] = [artifact]
            elif args.action == "fence-backup":
                require(args.acl_attestation is not None, "ACL_ATTESTATION_REQUIRED")
                old_image = image_backup_receipt(plan, plan_sha)
                destination = private_destination(plan, "env-state.tar", args.acl_attestation)
                prepared = call_remote("fence-backup", request)
                require(prepared.get("status") == "passed", "ENV_FENCE_BACKUP_PENDING_QUERY_STATUS")
                artifact = ops.streaming_ssh(shlex.join(["cat", prepared["private_archive"]]), destination) | {"path": str(destination)}
                require(artifact["bytes"] == prepared["archive_bytes"] and artifact["sha256"] == prepared["archive_sha256"], "STATE_STREAM_HASH_CHANGED")
                with tarfile.open(destination, "r:") as archive:
                    members = archive.getmembers()
                    require(len(members) == 5 and {"state.json", "runtime.json", "source.tar", "flags.tar", "protected.tar"} == {item.name for item in members}
                            and all(item.isfile() for item in members), "STATE_ARCHIVE_COMPONENTS")
                result = call_remote("fence-backup-complete", request | {"artifact": artifact})
                result["files"] = old_image["files"] + [artifact]
                validate_backup(result, plan["binding"], plan_sha)
            else:
                if args.action == "activate":
                    captured, _ = load_json(ops.TASK / "evidence" / ("env-fence-backup-" + plan["binding"]["release_nonce"] + ".json"))
                    validate_backup(captured, plan["binding"], plan_sha)
                    validate_private_files(captured["files"])
                archive = make_archive(plan["managed_files"], plan["modes"]) if args.action == "build" else None
                result = call_remote(args.action, request, archive)
            output = receipt(args.action + "-" + plan["binding"]["release_nonce"] + ("-" + str(uuid.uuid4()) if recovery or args.action == "verify" else ""), result)
            if result.get("status") != "passed":
                print(json.dumps({"status": "uncertain", "receipt": str(output), "next": "status; do not replay or restore"})); return 2
        print(json.dumps({"status": "passed", "receipt": str(output)})); return 0
    except Exception as error:
        print(json.dumps({"status": "uncertain" if remote_attempted else "failed", "code": str(error) if isinstance(error, EnvReleaseError) else "REVIEW_REQUIRED",
                          "automatic_restore": False, "next": "status; do not replay or restore" if remote_attempted else "review inputs before execution"})); return 2 if remote_attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
