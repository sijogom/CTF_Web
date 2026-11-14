"""Source/commit/nonce-bound staging and serial image builds, never activation.

Default execution is local planning only. --execute creates one fresh remote
release directory and four uniquely tagged images. It never replaces live
files, restarts services, modifies databases, removes images, or prunes caches.
Remote diagnostics stay in mode-0600 files inside that new release directory.
No September receipt is an input. Backup/quiescence are activation contracts,
not claims made by this stage/build tool.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fnmatch
import hashlib
import inspect
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit

import ops
import release_preflight
import verify_release


EXECUTOR_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
OPS_SOURCE_SHA256 = hashlib.sha256(Path(ops.__file__).read_bytes()).hexdigest()
PREFLIGHT_SOURCE_SHA256 = hashlib.sha256(Path(release_preflight.__file__).read_bytes()).hexdigest()
VERIFY_SOURCE_SHA256 = hashlib.sha256(Path(verify_release.__file__).read_bytes()).hexdigest()
validate_alf_contract = release_preflight.validate_alf_contract
validate_alcf_contract = release_preflight.validate_alcf_contract
CONTEXTS = (("auth", "services/auth"), ("learn", "services/learn"),
            ("community", "services/community"), ("frontend", "frontend"))
PROJECT = "ctf_web"
RELEASE_ROOT = "/home/deploy/.ctf-web-renewal/releases"
MAX_STAGE_BYTES = 512 * 1024 * 1024
MAX_STAGE_FILES = 20000
MIN_DISK_BYTES = 12 * 1024 ** 3
MIN_MEMORY_BYTES = 3 * 1024 ** 3
FRONTEND_KEYS = ("NEXT_PUBLIC_SITE_URL", "NEXT_PUBLIC_ENV_PROXY_BASE", "NEXT_PUBLIC_ENV_ACCESS_MODE")
SOURCE_LABELS = ("org.opencontainers.image.revision", "io.ctf-web.source-fingerprint", "io.ctf-web.release-nonce")
FRONTEND_ARGS_LABEL = "io.ctf-web.frontend-build-args-sha256"
FORBIDDEN_PARTS = frozenset(ops.SKIP_PARTS | {".pytest_cache", "venv", ".ssh", ".aws", ".azure", ".gcloud"})
FORBIDDEN_NAMES = frozenset(ops.SKIP_NAMES | {".netrc", "_netrc", ".pypirc", ".npmrc", "id_rsa", "id_dsa",
                                           "id_ecdsa", "id_ed25519", "credentials.json", "service-account.json"})
ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template", ".env.schema"})
SECRET_PATTERNS = (
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b",
    rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b",
    rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
)


class StageError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise StageError(code)


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def parse_mem_available(text: str) -> int:
    rows = [line.partition(":")[2].split() for line in text.splitlines() if line.partition(":")[0] == "MemAvailable"]
    require(len(rows) == 1 and len(rows[0]) == 2 and rows[0][0].isdigit() and rows[0][1] == "kB", "MEMORY_AVAILABLE_UNVERIFIED")
    return int(rows[0][0]) * 1024


def resource_gate(snapshot: dict):
    require(isinstance(snapshot,dict) and set(snapshot) == {"disk_available_bytes","memory_available_bytes"}
            and all(type(value) is int and value >= 0 for value in snapshot.values()), "BUILD_RESOURCE_UNVERIFIED")
    require(snapshot["disk_available_bytes"] >= MIN_DISK_BYTES and snapshot["memory_available_bytes"] >= MIN_MEMORY_BYTES,
            "BUILD_RESOURCE_GATE_FAILED")


def validate_binding(fingerprint: str, commit: str, nonce: str) -> dict:
    require(isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint), "SOURCE_FINGERPRINT_INVALID")
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "COMMIT_INVALID")
    require(isinstance(nonce, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", nonce), "RELEASE_NONCE_INVALID")
    return {"source_fingerprint": fingerprint, "commit": commit, "release_nonce": nonce}


def safe_source_path(name: str) -> PurePosixPath:
    require(isinstance(name, str) and bool(name) and "\\" not in name and ":" not in name
            and not any(ord(character) < 32 or ord(character) == 127 for character in name), "SOURCE_PATH_INVALID")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and path.as_posix() == name
            and path.parts and path.parts[0] != ".", "SOURCE_PATH_INVALID")
    require(any(name.startswith(context + "/") for _, context in CONTEXTS), "SOURCE_CONTEXT_FORBIDDEN")
    require(not any(part in FORBIDDEN_PARTS for part in path.parts), "SOURCE_ARTIFACT_FORBIDDEN")
    require(path.name not in FORBIDDEN_NAMES and not path.name.lower().endswith("cookies.txt")
            and path.suffix.lower() not in {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc", ".log", ".zip"}, "SOURCE_ARTIFACT_FORBIDDEN")
    require(not path.name.startswith(".env") or path.name in ENV_TEMPLATES, "SOURCE_ENV_FORBIDDEN")
    require(not (path.parts[0] == "services" and len(path.parts) > 2 and path.parts[2] == "data"), "SOURCE_DATA_FORBIDDEN")
    return path


def reject_secret_material(name: str, payload: bytes):
    safe_source_path(name)
    require(not any(re.search(pattern, payload) for pattern in SECRET_PATTERNS), "SOURCE_SECRET_MATERIAL_FORBIDDEN")
    if PurePosixPath(name).name in ENV_TEMPLATES:
        for line in payload.decode("utf-8-sig").splitlines():
            match = re.match(r"(?i)^\s*[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|ACCESS_TOKEN|SMTP_PASS)[A-Z0-9_]*\s*=\s*(.*?)\s*$", line)
            if match:
                value = match.group(1).strip("'\"")
                require(not value or "${" in value or value.startswith(("<", "$", "/"))
                        or re.search(r"(?i)change[_-]?me|replace[_-]?me|placeholder|your[_-]", value), "SOURCE_ENV_LITERAL_FORBIDDEN")


def manifest_fingerprint(rows: list[dict]) -> str:
    # Match ops.manifest's existing canonical byte contract, including ASCII escaping.
    return digest(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode())


def validate_manifest(value: dict, expected: str) -> dict[str, dict]:
    require(isinstance(value, dict) and value.get("fingerprint") == expected, "SOURCE_FINGERPRINT_CHANGED")
    rows = value.get("files")
    require(isinstance(rows, list) and rows and len(rows) <= 100000, "SOURCE_MANIFEST_INVALID")
    names = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}, "SOURCE_MANIFEST_INVALID")
        name = row["path"]
        require(isinstance(name, str) and name and "\\" not in name and ":" not in name
                and PurePosixPath(name).as_posix() == name and not PurePosixPath(name).is_absolute()
                and ".." not in PurePosixPath(name).parts and not any(ord(character) < 32 for character in name)
                and name not in names, "SOURCE_MANIFEST_PATH_INVALID")
        require(isinstance(row["bytes"], int) and not isinstance(row["bytes"], bool) and row["bytes"] >= 0
                and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "SOURCE_MANIFEST_HASH_INVALID")
        names.add(name)
    require(manifest_fingerprint(rows) == expected, "SOURCE_MANIFEST_FINGERPRINT_INVALID")
    require(sum(row["bytes"] for row in rows) <= MAX_STAGE_BYTES * 2, "SOURCE_MANIFEST_SIZE_LIMIT")
    return {row["path"]: row for row in rows}


def parse_git_tree(payload: bytes) -> dict[str, tuple[str, str]]:
    result = {}
    try:
        for record in payload.split(b"\0"):
            if not record:
                continue
            header, encoded_name = record.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split()
            name = encoded_name.decode("utf-8")
            require(name not in result and kind in {"blob", "commit"} and re.fullmatch(r"[0-9a-f]{40}", oid), "GIT_TREE_INVALID")
            result[name] = (mode, oid)
    except (ValueError, UnicodeError) as exc:
        raise StageError("GIT_TREE_INVALID") from exc
    return result


def parse_git_blobs(payload: bytes, object_ids: list[str]) -> dict[str, bytes]:
    stream, result = io.BytesIO(payload), {}
    for expected in object_ids:
        header = stream.readline(200)
        try:
            oid, kind, size = header.decode("ascii").strip().split()
            length = int(size)
        except (UnicodeError, ValueError) as exc:
            raise StageError("GIT_BLOB_RESPONSE_INVALID") from exc
        require(oid == expected and kind == "blob" and 0 <= length <= MAX_STAGE_BYTES, "GIT_BLOB_RESPONSE_INVALID")
        content = stream.read(length)
        require(len(content) == length and stream.read(1) == b"\n", "GIT_BLOB_RESPONSE_INVALID")
        result[expected] = content
    require(not stream.read(1), "GIT_BLOB_RESPONSE_INVALID")
    return result


def git_bytes(checkout: Path, *args: str, payload: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(checkout), *args], input=payload, capture_output=True)
    require(result.returncode == 0, "GIT_READ_FAILED")
    return result.stdout


def bind_checkout(checkout: Path, source: Path, manifest: dict, binding: dict, *, git_reader=git_bytes) -> dict[str, int]:
    files = validate_manifest(manifest, binding["source_fingerprint"])
    require(checkout.is_dir() and (checkout / ".git").is_dir() and not ops.linked(checkout / ".git")
            and not any(ops.linked(parent) for parent in (checkout, *checkout.parents))
            and source.is_dir() and not any(ops.linked(parent) for parent in (source, *source.parents)), "CLEAN_CHECKOUT_INVALID")
    require(git_reader(checkout, "rev-parse", "HEAD").decode().strip() == binding["commit"], "CLEAN_CHECKOUT_COMMIT_CHANGED")
    require(git_reader(checkout, "config", "--local", "--get", "core.autocrlf").decode().strip() == "false", "CLEAN_CHECKOUT_BYTE_POLICY_CHANGED")
    require(not git_reader(checkout, "status", "--porcelain", "--untracked-files=all").strip(), "CLEAN_CHECKOUT_NOT_COMMITTED")
    tree = parse_git_tree(git_reader(checkout, "ls-tree", "-r", "-z", binding["commit"]))
    require(all(name in tree and tree[name][0] in {"100644", "100755"} for name in files), "SOURCE_COMMIT_FILE_MISSING_OR_LINKED")
    ids = sorted({tree[name][1] for name in files})
    blobs = parse_git_blobs(git_reader(checkout, "cat-file", "--batch", payload=("\n".join(ids) + "\n").encode()), ids)
    modes = {}
    for name, row in files.items():
        for root in (source, checkout):
            path = root / name
            require(path.resolve().is_relative_to(root.resolve()) and path.is_file()
                    and not any(ops.linked(parent) for parent in (path, *path.parents) if parent.is_relative_to(root)), "SOURCE_OR_COPY_FILE_INVALID")
            payload = path.read_bytes()
            require(len(payload) == row["bytes"] and digest(payload) == row["sha256"], "SOURCE_OR_COPY_HASH_CHANGED")
        blob = blobs[tree[name][1]]
        require(len(blob) == row["bytes"] and digest(blob) == row["sha256"], "SOURCE_COMMIT_BLOB_MISMATCH")
        modes[name] = 0o755 if tree[name][0] == "100755" else 0o644
    return modes


def validate_context_payloads(payloads: dict[str, bytes], *, contexts=CONTEXTS):
    require(payloads and len(payloads) <= MAX_STAGE_FILES and sum(map(len, payloads.values())) <= MAX_STAGE_BYTES, "STAGE_SIZE_LIMIT")
    for name, payload in payloads.items():
        reject_secret_material(name, payload)
        require(any(name.startswith(context + "/") for _,context in contexts), "STAGE_CONTEXT_MODE_MISMATCH")
    for service, context in contexts:
        required = {"Dockerfile", ".dockerignore"}
        required.update({"package.json", "package-lock.json", "tsconfig.json", "next.config.ts", "src/app/layout.tsx"}
                        if service == "frontend" else {"requirements.txt", "app/main.py", "alembic.ini", "migrations/env.py"})
        if service == "auth":
            required.add("docker-entrypoint.sh")
        require(all(context + "/" + name in payloads for name in required), "DOCKER_CONTEXT_INCOMPLETE")
        patterns = {line.strip() for line in payloads[context + "/.dockerignore"].decode("utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")}
        expected = {"node_modules", ".next", ".git"} if service == "frontend" else {"__pycache__", "*.pyc", ".env", ".env.*", ".git", ".venv", "*.pem", "*.key"}
        require(expected <= patterns, "DOCKERIGNORE_REQUIRED_EXCLUSIONS_MISSING")
        dockerfile = payloads[context + "/Dockerfile"].decode("utf-8-sig")
        if service == "frontend":
            require(re.search(r"(?im)^FROM\s+.+\s+AS\s+production\s*$", dockerfile), "FRONTEND_PRODUCTION_TARGET_MISSING")
        relative = [name.removeprefix(context + "/") for name in payloads if name.startswith(context + "/")]
        for line in dockerfile.splitlines():
            if not re.match(r"(?i)^COPY\s", line):
                continue
            parts = shlex.split(line)[1:]
            if any(part.startswith("--from=") for part in parts):
                continue
            parts = [part for part in parts if not part.startswith("--")]
            require(len(parts) >= 2, "DOCKER_COPY_SYNTAX_UNSUPPORTED")
            for origin in parts[:-1]:
                require(not origin.startswith("/") and ".." not in PurePosixPath(origin).parts and "$" not in origin, "DOCKER_COPY_SOURCE_UNSAFE")
                present = origin in {".", "./"} or any(
                    item == origin.rstrip("/") or item.startswith(origin.rstrip("/") + "/")
                    or ("/" not in item and fnmatch.fnmatchcase(item, origin)) for item in relative)
                require(present, "DOCKER_COPY_SOURCE_MISSING")
        if service == "auth":
            entrypoint = payloads[context + "/docker-entrypoint.sh"]
            require(entrypoint.startswith(b"#!/bin/sh\n") and b"\r" not in entrypoint, "DOCKER_ENTRYPOINT_REQUIRES_LF")


def make_tar(payloads: dict[str, bytes], modes: dict[str, int], *, contexts=CONTEXTS) -> tuple[bytes, list[dict]]:
    validate_context_payloads(payloads, contexts=contexts)
    stream, rows = io.BytesIO(), []
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, payload in sorted(payloads.items()):
            mode = modes[name]
            require(mode in {0o644, 0o755}, "SOURCE_MODE_INVALID")
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(payload), mode, 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(payload))
            rows.append({"path": name, "bytes": len(payload), "sha256": digest(payload), "mode": mode})
    payload = stream.getvalue()
    require(len(payload) <= MAX_STAGE_BYTES, "STAGE_SIZE_LIMIT")
    return payload, rows


def validate_stage_rows(rows: list[dict]) -> dict[str, dict]:
    require(isinstance(rows, list) and 0 < len(rows) <= MAX_STAGE_FILES, "STAGE_MANIFEST_INVALID")
    result = {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256", "mode"}, "STAGE_MANIFEST_INVALID")
        safe_source_path(row["path"])
        require(row["path"] not in result and isinstance(row["bytes"], int) and not isinstance(row["bytes"], bool)
                and 0 <= row["bytes"] <= MAX_STAGE_BYTES and row["mode"] in {0o644, 0o755}
                and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "STAGE_MANIFEST_INVALID")
        result[row["path"]] = row
    require(sum(row["bytes"] for row in rows) <= MAX_STAGE_BYTES, "STAGE_SIZE_LIMIT")
    return result


def extract_verified_tar(payload: bytes, destination: Path, rows: list[dict], *, contexts=CONTEXTS) -> dict:
    expected, received, payloads = validate_stage_rows(rows), set(), {}
    require(destination.is_dir() and not destination.is_symlink(), "STAGE_DESTINATION_INVALID")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive:
            safe_source_path(member.name)
            require(member.isfile() and not member.issym() and not member.islnk()
                    and not any(key.startswith("GNU.sparse") for key in member.pax_headers), "TAR_MEMBER_TYPE_FORBIDDEN")
            require(member.name in expected and member.name not in received, "TAR_MEMBER_UNEXPECTED_OR_DUPLICATE")
            row = expected[member.name]
            require(member.size == row["bytes"] and member.mode == row["mode"], "TAR_MEMBER_METADATA_MISMATCH")
            stream = archive.extractfile(member)
            data = stream.read(member.size + 1)
            require(len(data) == row["bytes"] and digest(data) == row["sha256"], "TAR_MEMBER_HASH_MISMATCH")
            reject_secret_material(member.name, data)
            path = destination / member.name
            require(path.resolve().is_relative_to(destination.resolve()), "STAGE_PATH_ESCAPE")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            require(not any(parent.is_symlink() for parent in (path, *path.parents)
                            if parent.is_relative_to(destination)), "STAGE_LINK_FORBIDDEN")
            # The private SSH transport keeps umask 077; image source directories
            # need explicit traversal permissions without opening its ancestors.
            for parent in path.parents:
                if parent == destination:
                    break
                parent.chmod(0o755)
            with path.open("xb") as output:
                output.write(data)
            path.chmod(row["mode"])
            require(digest(path.read_bytes()) == row["sha256"], "STAGE_WRITE_HASH_MISMATCH")
            received.add(member.name)
            payloads[member.name] = data
        require(not payload[archive.offset:].strip(b"\0"), "TAR_TRAILING_DATA_FORBIDDEN")
    require(received == set(expected), "TAR_MEMBER_MISSING")
    validate_context_payloads(payloads, contexts=contexts)
    return {"file_count": len(received), "bytes": sum(row["bytes"] for row in rows), "files_sha256": digest(canonical(rows))}


def validate_frontend_args(values: dict) -> dict:
    require(isinstance(values, dict) and set(values) == set(FRONTEND_KEYS)
            and all(isinstance(value, str) and len(value) <= 2048 and not any(ord(character) < 32 for character in value)
                    for value in values.values()), "FRONTEND_BUILD_ARGS_INVALID")
    for key in FRONTEND_KEYS[:2]:
        value = values[key]
        if key == "NEXT_PUBLIC_ENV_PROXY_BASE" and value == "":
            continue
        parsed = urlsplit(value)
        require(parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                and parsed.password is None and not parsed.query and not parsed.fragment, "FRONTEND_BUILD_URL_INVALID")
        require(parsed.port is None or 1 <= parsed.port <= 65535, "FRONTEND_BUILD_URL_INVALID")
    require(values["NEXT_PUBLIC_ENV_ACCESS_MODE"] in {"proxy", "direct"}, "FRONTEND_ACCESS_MODE_INVALID")
    return dict(values)


def frontend_runtime_args(environment_rows: list[str]) -> dict:
    require(isinstance(environment_rows, list), "FRONTEND_INSPECTION_INVALID")
    environment = {}
    for line in environment_rows:
        require(isinstance(line, str), "FRONTEND_INSPECTION_INVALID")
        key, separator, value = line.partition("=")
        if key in FRONTEND_KEYS:
            require(separator and key not in environment, "LIVE_FRONTEND_ENV_AMBIGUOUS")
            environment[key] = value
    return environment


def frontend_args_from_inspection(environment_rows: list[str], compose_args: dict) -> dict:
    require(isinstance(compose_args,dict),"FRONTEND_INSPECTION_INVALID")
    environment = frontend_runtime_args(environment_rows)
    arguments = {key:compose_args.get(key) for key in FRONTEND_KEYS}
    validate_frontend_args(arguments)
    require(all(arguments[key] == value for key,value in environment.items()), "LIVE_FRONTEND_BUILD_ARGS_MISMATCH")
    return arguments


def frontend_args_observation(environment_rows: list[str]) -> dict:
    present = frontend_runtime_args(environment_rows)
    return {"authoritative_source":"protected-compose-build-args","runtime_env_present":sorted(present),
            "runtime_env_missing":sorted(set(FRONTEND_KEYS)-set(present)),"present_runtime_values_match":True,
            "prior_compiled_args_verified":False}


def safe_image_source_labels(labels: dict) -> dict:
    require(isinstance(labels,dict), "LIVE_SOURCE_LABEL_INVALID")
    patterns = (r"[0-9a-f]{40}", r"[0-9a-f]{64}", r"[a-z0-9][a-z0-9-]{7,63}")
    result = {}
    for key,pattern in zip(SOURCE_LABELS,patterns):
        value = labels.get(key)
        require(value is None or isinstance(value,str) and re.fullmatch(pattern,value), "LIVE_SOURCE_LABEL_INVALID")
        result[key] = value
    return result


def selected_contexts(*, frontend_only=False, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(all(type(item) is bool for item in (frontend_only, learn_only, auth_learn_frontend, auth_learn_community_frontend, learn_frontend))
            and sum((frontend_only, learn_only, auth_learn_frontend, auth_learn_community_frontend, learn_frontend)) <= 1, "BUILD_SCOPE_INVALID")
    return (CONTEXTS[1], CONTEXTS[3]) if learn_frontend else (CONTEXTS[0], CONTEXTS[1], CONTEXTS[3]) if auth_learn_frontend else CONTEXTS[-1:] if frontend_only else (CONTEXTS[1],) if learn_only else CONTEXTS


def build_commands(source_root: Path, binding: dict, frontend_args: dict, *, frontend_only=False, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False) -> list[tuple[str, list[str]]]:
    validate_binding(binding["source_fingerprint"], binding["commit"], binding["release_nonce"])
    validate_frontend_args(frontend_args)
    result = []
    for service, context in selected_contexts(frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend):
        tag = "ctf-renewal/" + service + ":" + binding["commit"] + "-" + binding["release_nonce"]
        command = ["docker", "build", "--pull", "--progress", "plain", "--network", "host",
                   "--file", str(source_root / context / "Dockerfile"), "--tag", tag]
        command.extend(["--label", "org.opencontainers.image.revision=" + binding["commit"],
                        "--label", "io.ctf-web.source-fingerprint=" + binding["source_fingerprint"],
                        "--label", "io.ctf-web.release-nonce=" + binding["release_nonce"]])
        if service == "frontend":
            command.extend(["--target", "production"])
            command.extend(["--label",FRONTEND_ARGS_LABEL + "=" + digest(canonical(frontend_args))])
            for key in FRONTEND_KEYS:
                command.extend(["--build-arg", key + "=" + frontend_args[key]])
        command.append(str(source_root / context))
        result.append((service, command))
    return result


def runtime_probe_command(service: str, image: str, rows: list[dict]) -> tuple[list[str], int]:
    require(service in {name for name, _ in CONTEXTS} and re.fullmatch(r"sha256:[0-9a-f]{64}", image), "RUNTIME_PROBE_IMAGE_INVALID")
    context = dict(CONTEXTS)[service] + "/"
    paths = {"/app"}
    for row in rows:
        name = row["path"]
        if not name.startswith(context):
            continue
        relative = name.removeprefix(context)
        included = relative.startswith("public/") if service == "frontend" else (
            relative.startswith(("app/", "migrations/")) or relative in {"alembic.ini", "requirements.txt"})
        if included:
            paths.add("/app/" + relative)
    paths.update({"/app/server.js", "/app/.next/server", "/app/.next/static"} if service == "frontend" else {"/app/app/main.py", "/app/migrations/env.py", "/app/alembic.ini"})
    if service in {"auth", "learn"}:
        paths.add("/app/app/scripts/" + service + "_migrate.py")
    for path in tuple(paths):
        paths.update(str(parent) for parent in PurePosixPath(path).parents if str(parent).startswith("/app"))
    encoded = json.dumps(sorted(paths), separators=(",", ":"))
    python = "import json,os,sys;paths=json.loads(sys.argv[1]);bad=sum(not os.access(p,os.R_OK|(os.X_OK if os.path.isdir(p) else 0)) for p in paths);print(json.dumps({'uid':os.getuid(),'checked':len(paths),'inaccessible':bad}))"
    node = "const fs=require('fs');const paths=JSON.parse(process.argv[1]);let bad=0;for(const p of paths){try{fs.accessSync(p,fs.constants.R_OK|(fs.statSync(p).isDirectory()?fs.constants.X_OK:0))}catch{bad++}}console.log(JSON.stringify({uid:process.getuid(),checked:paths.length,inaccessible:bad}))"
    command = ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--cpus", "0.5",
               "--user", "1001:1001", "--entrypoint", "node" if service == "frontend" else "python", image]
    command.extend(["-e", node, encoded] if service == "frontend" else ["-S", "-c", python, encoded])
    return command, len(paths)


def validate_runtime_probe(value: dict, count: int):
    require(isinstance(value, dict) and set(value) == {"uid", "checked", "inaccessible"}
            and type(value["uid"]) is int and value["uid"] == 1001
            and type(value["checked"]) is int and value["checked"] == count and count > 0
            and type(value["inaccessible"]) is int and value["inaccessible"] == 0, "IMAGE_RUNTIME_READABILITY_FAILED")


def execute_serial_builds(commands: list[tuple[str, list[str]]], run_build, inspect_image, update_progress) -> list[dict]:
    completed = []
    for service, command in commands:
        update_progress(service, completed)
        require(run_build(service, command) == 0, "IMAGE_BUILD_FAILED")
        image = inspect_image(service, command[command.index("--tag") + 1])
        require(isinstance(image, dict) and re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("image_id", ""))
                and image.get("service") == service and image.get("tag") == command[command.index("--tag") + 1], "BUILT_IMAGE_UNVERIFIED")
        completed.append(image)
        update_progress(service, completed)
    return completed


def load_backend_baseline(activation_path, build_path, verify_path, manifest_rows) -> dict:
    import verify_release
    activation, activation_sha = verify_release.load_receipt(activation_path)
    built, build_sha = verify_release.load_receipt(build_path)
    verified, verify_sha = verify_release.load_receipt(verify_path)
    prior = activation.get("binding", {})
    binding = verify_release.binding_for(prior.get("commit"), prior.get("source_fingerprint"), prior.get("release_nonce"))
    verify_release.baseline_verify_contract(verified, binding, build_sha, activation_sha)
    baseline = verify_release.activation_contract(activation, binding, activation.get("managed_files"), verify_release.build_contract(built, binding))
    backends = [row for row in baseline["managed_files"] if row["path"].startswith("services/")]
    current = sorted((row for row in manifest_rows if row["path"].startswith(tuple(context + "/" for _,context in CONTEXTS[:-1]))), key=lambda row:row["path"])
    require(current == backends, "FRONTEND_ONLY_BACKEND_SOURCE_CHANGED")
    return {"binding":binding, "images":{name:baseline["images"][name] for name in ("auth","learn","community")},
            "managed_files":backends, "protected_files":baseline["protected_files"],
            "service_images":{name:family for name,family in verify_release.SERVICE_IMAGES.items() if family != "frontend"},
            "receipt_hashes":{"activation":activation_sha,"build":build_sha,"verify":verify_sha}}


def check_backend_baseline(snapshot: dict, baseline: dict | None, live_root: Path):
    if baseline is None:
        return
    require(snapshot["protected_files"] == baseline["protected_files"], "FRONTEND_ONLY_PROTECTED_BASELINE_CHANGED")
    for service,family in baseline["service_images"].items():
        binding = baseline.get("image_bindings", {}).get(family, baseline["binding"])
        expected_labels = dict(zip(SOURCE_LABELS, (binding["commit"], binding["source_fingerprint"], binding["release_nonce"])))
        selected = [row for row in snapshot["containers"] if row["service"] == service and row["running"]]
        require(len(selected) == 1 and selected[0]["image_id"] == baseline["images"][family]
                and selected[0].get("source_labels") == expected_labels, "FRONTEND_ONLY_BACKEND_IMAGE_BASELINE_CHANGED")
    for row in baseline["managed_files"]:
        safe_source_path(row["path"])
        path = live_root / row["path"]
        require(path.resolve().is_relative_to(live_root.resolve()) and path.is_file()
                and not any(parent.is_symlink() for parent in (path,*path.parents) if parent.is_relative_to(live_root)), "FRONTEND_ONLY_BACKEND_PATH_CHANGED")
        payload = path.read_bytes()
        require(len(payload) == row["bytes"] and digest(payload) == row["sha256"], "FRONTEND_ONLY_BACKEND_FILE_CHANGED")


def load_retained_baseline(paths, manifest_rows, *, frontend_only=False, learn_only=False, learn_code_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    import verify_release
    chain = verify_release.load_baseline_chain(paths)
    contract = chain["contract"]
    require(not learn_code_only or learn_only and not frontend_only, "LEARN_CODE_SCOPE_INVALID")
    expected_head = "20261013_029" if learn_code_only else "20261013_028"
    require(not learn_only or contract["expected_heads"]["learn"] == expected_head, "LEARN_COMPATIBILITY_BASELINE_HEAD_INVALID")
    families = verify_release.release_families(frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend)
    retained_rows = verify_release.retained_source_contract(contract, manifest_rows, frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend)
    if learn_code_only:
        migration_rows = release_preflight.learn_migration_rows(manifest_rows)
        require(migration_rows == release_preflight.learn_migration_rows(contract["managed_files"]), "LEARN_CODE_MIGRATION_BYTES_CHANGED")
        retained_rows = sorted(retained_rows + migration_rows, key=lambda row: row["path"])
    return {**verify_release.baseline_summary(contract, families), "managed_files": retained_rows,
            "protected_files": contract["protected_files"],
            "service_images": {name: family for name, family in verify_release.SERVICE_IMAGES.items() if family not in families},
            "receipt_hashes": chain["hashes"]}


def load_preflight(path: Path | None, expected_sha: str | None, binding: dict, source_count: int, *, learn_only=False, learn_code_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False) -> dict | None:
    require((path is None) == (expected_sha is None), "PREFLIGHT_INPUT_PAIR_REQUIRED")
    if path is None:
        require(not learn_code_only, "LEARN_CODE_PREFLIGHT_REQUIRED")
        require(not auth_learn_frontend, "ALF_PREFLIGHT_REQUIRED")
        require(not auth_learn_community_frontend, "ALCF_PREFLIGHT_REQUIRED")
        require(not learn_frontend, "LF_PREFLIGHT_REQUIRED")
        return None
    require(isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-f]{64}", expected_sha), "PREFLIGHT_HASH_INVALID")
    path = Path(path).absolute()
    require(path.resolve().is_relative_to((ops.TASK / "evidence").resolve()) and path.is_file()
            and not any(ops.linked(parent) for parent in (path, *path.parents) if parent.is_relative_to(ops.TASK)), "PREFLIGHT_PATH_INVALID")
    payload = path.read_bytes()
    require(digest(payload) == expected_sha, "PREFLIGHT_HASH_CHANGED")
    value = json.loads(payload)
    require(value.get("learn_only", False) is learn_only, "PREFLIGHT_BUILD_SCOPE_CHANGED")
    require(release_preflight.code_only_phase(value) is learn_code_only, "PREFLIGHT_BUILD_PHASE_CHANGED")
    require(release_preflight.alf_phase(value) is auth_learn_frontend, "PREFLIGHT_BUILD_PHASE_CHANGED")
    require(release_preflight.alcf_phase(value) is auth_learn_community_frontend, "PREFLIGHT_BUILD_PHASE_CHANGED")
    require(release_preflight.lf_phase(value) is learn_frontend, "PREFLIGHT_BUILD_PHASE_CHANGED")
    try:
        release_preflight.validate_plan(value, binding["source_fingerprint"])
    except release_preflight.ReleaseError as exc:
        raise StageError("PREFLIGHT_PLAN_INVALID") from exc
    require(value.get("schema_version") == 1 and value.get("kind") == "renewal-release-plan"
            and value.get("task") == "2026-10-13-renewal", "PREFLIGHT_SCHEMA_INVALID")
    require(value.get("source_fingerprint") == binding["source_fingerprint"] and value.get("source_file_count") == source_count
            and value.get("nonce") == binding["release_nonce"], "PREFLIGHT_SOURCE_BINDING_CHANGED")
    require(value.get("target") == {"host": ops.HOST, "port": 10022, "live_root": ops.LIVE, "project": PROJECT}, "PREFLIGHT_TARGET_CHANGED")
    inspection = value.get("inspection", {})
    require(inspection.get("verified") is True and inspection.get("protected_files") and inspection.get("containers"), "PREFLIGHT_INSPECTION_UNVERIFIED")
    protected, containers = [], []
    for row in inspection["protected_files"]:
        require(isinstance(row, dict) and isinstance(row.get("path"), str) and PurePosixPath(row["path"]).is_absolute()
                and ".." not in PurePosixPath(row["path"]).parts and isinstance(row.get("bytes"), int)
                and row["bytes"] >= 0 and re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")), "PREFLIGHT_PROTECTED_FILE_INVALID")
        protected.append({key:row[key] for key in ("path", "bytes", "sha256")})
    for row in inspection["containers"]:
        require(isinstance(row, dict) and isinstance(row.get("service"), str) and re.fullmatch(r"[0-9a-f]{64}", row.get("container_id", ""))
                and re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("image_id", "")) and isinstance(row.get("running"), bool), "PREFLIGHT_CONTAINER_INVALID")
        containers.append({key:row[key] for key in ("service", "container_id", "image_id", "running")})
    require(len({row["path"] for row in protected}) == len(protected)
            and len({row["container_id"] for row in containers}) == len(containers), "PREFLIGHT_DUPLICATE_INVENTORY")
    return {"plan_sha256": expected_sha, "protected_files": sorted(protected, key=lambda row:row["path"]), "containers": containers,
            **({"release_phase": release_preflight.LEARN_CODE_PHASE, "learn_code_contract": value["learn_code_contract"]} if learn_code_only else {}),
            **{key: value[key] for key in release_preflight.application_phase_fields(value)}}


REMOTE_MAIN = r'''
import os, stat
LIVE = pathlib.Path('/home/deploy/CTF_Web')
def command_output(args, timeout=90):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    require(result.returncode == 0, 'REMOTE_READ_FAILED')
    return result.stdout
def live_snapshot():
    model = json.loads(command_output(['docker','compose','-p',PROJECT,'--project-directory',str(LIVE/'infra'),'--env-file',str(LIVE/'infra/.env'),'-f',str(LIVE/'infra/docker-compose.yml'),'-f',str(LIVE/'infra/docker-compose.prod.yml'),'config','--format','json']))
    ids = command_output(['docker','ps','-a','--filter','label=com.docker.compose.project='+PROJECT,'--format','{{.ID}}']).decode().splitlines()
    require(ids, 'LIVE_CONTAINERS_MISSING')
    inspected = json.loads(command_output(['docker','inspect',*ids]))
    containers, frontend = [], []
    for item in inspected:
        labels = item.get('Config',{}).get('Labels',{}) or {}
        require(labels.get('com.docker.compose.project') == PROJECT, 'LIVE_PROJECT_CHANGED')
        service = labels.get('com.docker.compose.service')
        containers.append({'service':service,'container_id':item['Id'],'image_id':item['Image'],'running':item.get('State',{}).get('Running') is True,'pid':item.get('State',{}).get('Pid'),'source_labels':safe_image_source_labels(labels)})
        if service == 'frontend' and item.get('State',{}).get('Running') is True:
            frontend.append(item)
    require(len(frontend) == 1, 'LIVE_FRONTEND_AMBIGUOUS')
    for service in ['auth-service','learn-service','community-service']:
        require(len([row for row in containers if row['service'] == service and row['running']]) == 1, 'LIVE_SERVICE_AMBIGUOUS')
    environment = frontend_args_from_inspection(frontend[0].get('Config',{}).get('Env',[]),model.get('services',{}).get('frontend',{}).get('build',{}).get('args',{}))
    protected = _PROTECTED['protected_rows'](model)
    key_paths = {}
    for key in ['auth_jwt_private_key','platform_jwt_public_key']:
        path = pathlib.Path(model['secrets'][key]['file'])
        if not path.is_absolute(): path = LIVE/'infra'/path
        key_paths[key] = str(path)
    return {'project':PROJECT,'protected_key_paths':key_paths,'containers':sorted(containers,key=lambda row:(row['service'] or '',row['container_id'])), 'protected_files':protected,
            'frontend_args':environment,'frontend_args_sha256':digest(canonical(environment)),
            'frontend_args_observation':frontend_args_observation(frontend[0].get('Config',{}).get('Env',[]))}
def snapshot_public(snapshot):
    return {key:value for key,value in snapshot.items() if key != 'frontend_args'}
def expected_snapshot(snapshot, expected):
    if expected is None: return
    require(snapshot['protected_files'] == expected['protected_files'], 'PREFLIGHT_PROTECTED_FILES_CHANGED')
    prior = sorted([{key:row[key] for key in ['service','container_id','image_id','running']} for row in expected['containers']],key=lambda row:(row['service'] or '',row['container_id']))
    current = [{key:row[key] for key in ['service','container_id','image_id','running']} for row in snapshot['containers']]
    require(current == prior, 'PREFLIGHT_LIVE_IMAGES_CHANGED')
def private_json(path, value):
    descriptor = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream: stream.write(canonical(value)+b'\n')
def new_release_directory(nonce):
    root = pathlib.Path(RELEASE_ROOT)
    for path in [root.parent,root]:
        require(not any(parent.is_symlink() for parent in (path,*path.parents)), 'RELEASE_PARENT_LINK')
        if not path.exists(): path.mkdir(mode=0o700)
        require(path.is_dir() and not any(parent.is_symlink() for parent in (path,*path.parents))
                and path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o022 == 0, 'RELEASE_PARENT_INVALID')
    destination = root/nonce
    destination.mkdir(mode=0o700)
    (destination/'source').mkdir(mode=0o700)
    (destination/'private').mkdir(mode=0o700)
    return destination
def resource_snapshot():
    available = os.statvfs(LIVE)
    return {'disk_available_bytes':available.f_bavail*available.f_frsize,
            'memory_available_bytes':parse_mem_available(pathlib.Path('/proc/meminfo').read_text(encoding='ascii'))}
def remote_main():
    release = None
    receipt = {'schema_version':1,'kind':'renewal-stage-build','status':'unverified','binding':REQUEST.get('binding'),
               'activation_performed':False,'database_mutation_performed':False,'images_pruned':False,'live_files_replaced':False,'completed_images':[]}
    if REQUEST.get('release_phase') and not REQUEST.get('auth_learn_frontend') and not REQUEST.get('auth_learn_community_frontend') and not REQUEST.get('learn_frontend'):
        receipt.update({key:REQUEST[key] for key in ('release_phase','learn_code_contract')})
    try:
        binding = validate_binding(**{ 'fingerprint':REQUEST['binding']['source_fingerprint'], 'commit':REQUEST['binding']['commit'], 'nonce':REQUEST['binding']['release_nonce'] })
        validate_stage_rows(REQUEST['files'])
        require(isinstance(REQUEST.get('source_file_count'),int) and len(REQUEST['files']) <= REQUEST['source_file_count'] <= 100000, 'SOURCE_COUNT_INVALID')
        require(isinstance(REQUEST.get('tar_bytes'),int) and 0 < REQUEST['tar_bytes'] <= MAX_STAGE_BYTES
                and re.fullmatch('[0-9a-f]{64}',REQUEST.get('tar_sha256','')), 'TAR_BINDING_INVALID')
        data = sys.stdin.buffer.read(REQUEST['tar_bytes']+1)
        require(len(data) == REQUEST['tar_bytes'] and digest(data) == REQUEST['tar_sha256'], 'TAR_STREAM_HASH_MISMATCH')
        before = live_snapshot(); expected_snapshot(before,REQUEST.get('preflight'))
        frontend_only = REQUEST.get('frontend_only') is True
        learn_only = REQUEST.get('learn_only') is True
        alf = REQUEST.get('auth_learn_frontend') is True
        alcf = REQUEST.get('auth_learn_community_frontend') is True
        lf = REQUEST.get('learn_frontend') is True
        contexts = selected_contexts(frontend_only=frontend_only,learn_only=learn_only,auth_learn_frontend=alf,auth_learn_community_frontend=alcf,learn_frontend=lf)
        require((frontend_only or learn_only or alf or alcf or lf) == (REQUEST.get('backend_baseline') is not None), 'BACKEND_BASELINE_MODE_MISMATCH')
        check_backend_baseline(before,REQUEST.get('backend_baseline'),LIVE)
        if alf:
            validate_alf_contract(REQUEST.get('alf_contract'))
            require(REQUEST.get('release_phase')=='auth-learn-frontend-v1' and REQUEST.get('preflight',{}).get('alf_contract')==REQUEST.get('alf_contract'),'ALF_PREFLIGHT_CHANGED')
            require(REQUEST['backend_baseline']['expected_heads']==REQUEST['alf_contract']['previous_heads'],'ALF_BASELINE_HEADS_CHANGED')
            receipt.update({key:REQUEST[key] for key in ('auth_learn_frontend','release_phase','alf_contract')})
        elif alcf:
            validate_alcf_contract(REQUEST.get('alcf_contract'))
            require(REQUEST.get('release_phase')=='auth-learn-community-frontend-v1' and REQUEST.get('preflight',{}).get('alcf_contract')==REQUEST.get('alcf_contract'),'ALCF_PREFLIGHT_CHANGED')
            require(REQUEST['backend_baseline']['expected_heads']==REQUEST['alcf_contract']['previous_heads'],'ALCF_BASELINE_HEADS_CHANGED')
            receipt.update({key:REQUEST[key] for key in ('auth_learn_community_frontend','release_phase','alcf_contract')})
        elif lf:
            validate_lf_contract(REQUEST.get('lf_contract'))
            require(REQUEST.get('release_phase')=='learn-frontend-032-v1' and REQUEST.get('preflight',{}).get('lf_contract')==REQUEST.get('lf_contract'),'LF_PREFLIGHT_CHANGED')
            require(REQUEST['backend_baseline']['expected_heads']==REQUEST['lf_contract']['previous_heads'],'LF_BASELINE_HEADS_CHANGED')
            receipt.update({key:REQUEST[key] for key in ('learn_frontend','release_phase','lf_contract')})
        elif REQUEST.get('release_phase'):
            require(REQUEST['release_phase']=='learn-code-only-029' and learn_only and not frontend_only,'LEARN_CODE_SCOPE_INVALID')
            require(REQUEST.get('preflight',{}).get('learn_code_contract')==REQUEST['learn_code_contract'],'LEARN_CODE_PREFLIGHT_CHANGED')
            live=[row for row in before['containers'] if row['service']=='learn-service' and row['running']]
            require(len(live)==1 and live[0]['image_id']==REQUEST['learn_code_contract']['previous_learn_image'],'LEARN_CODE_PREIMAGE_CHANGED')
            receipt.update({key:REQUEST[key] for key in ('release_phase','learn_code_contract')})
        commands = build_commands(pathlib.Path(RELEASE_ROOT)/binding['release_nonce']/'source',binding,before['frontend_args'],frontend_only=frontend_only,learn_only=learn_only,auth_learn_frontend=alf,auth_learn_community_frontend=alcf,learn_frontend=lf)
        receipt['frontend_only'] = frontend_only
        if REQUEST.get('baseline_chain'):
            receipt['learn_only'] = learn_only
            receipt['retained_baseline'] = {key:REQUEST['backend_baseline'][key] for key in ['binding','images','image_bindings','expected_heads']}
        elif frontend_only:
            receipt['backend_baseline'] = {key:REQUEST['backend_baseline'][key] for key in ['binding','images']}
        for service,command in commands:
            tag = command[command.index('--tag')+1]
            require(subprocess.run(['docker','image','inspect',tag],capture_output=True).returncode != 0, 'RELEASE_IMAGE_TAG_ALREADY_EXISTS')
        release = new_release_directory(binding['release_nonce'])
        receipt['live_before'] = snapshot_public(before)
        private_json(release/'build-request.json',{**receipt,'status':'staging'})
        receipt['stage'] = extract_verified_tar(data,release/'source',REQUEST['files'],contexts=contexts)
        receipt['tar_sha256'] = REQUEST['tar_sha256']
        receipt['source_file_count'] = REQUEST['source_file_count']
        receipt['preflight_plan_sha256'] = REQUEST.get('preflight',{}).get('plan_sha256') if REQUEST.get('preflight') else None
        checkpointed = set()
        def progress(service,completed):
            receipt['current_service'] = service
            receipt['completed_images'] = list(completed)
            if completed and len(completed) not in checkpointed:
                private_json(release/'private'/('completed-'+str(len(completed))+'.json'),{**receipt,'status':'building-not-activated'})
                checkpointed.add(len(completed))
        def build(service,command):
            resources = resource_snapshot()
            private_json(release/'private'/('resources-'+service+'.json'),resources)
            resource_gate(resources)
            descriptor = os.open(release/'private'/('build-'+service+'.log'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as stream:
                result = subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=3600)
            return result.returncode
        def image(service,tag):
            value = json.loads(command_output(['docker','image','inspect',tag]))
            require(len(value) == 1, 'BUILT_IMAGE_AMBIGUOUS')
            labels = value[0].get('Config',{}).get('Labels',{}) or {}
            require(labels.get('org.opencontainers.image.revision') == binding['commit']
                    and labels.get('io.ctf-web.source-fingerprint') == binding['source_fingerprint']
                    and labels.get('io.ctf-web.release-nonce') == binding['release_nonce'], 'BUILT_IMAGE_SOURCE_LABEL_MISMATCH')
            probe,count=runtime_probe_command(service,value[0]['Id'],REQUEST['files'])
            validate_runtime_probe(json.loads(command_output(probe)),count)
            result={'service':service,'tag':tag,'image_id':value[0]['Id']}
            if service=='frontend':
                require(labels.get(FRONTEND_ARGS_LABEL)==before['frontend_args_sha256'],'BUILT_FRONTEND_ARGS_LABEL_MISMATCH')
                result['frontend_args_sha256']=before['frontend_args_sha256']
            return result
        receipt['completed_images'] = execute_serial_builds(commands,build,image,progress)
        receipt['runtime_readability_verified'] = True
        after = live_snapshot()
        check_backend_baseline(after,REQUEST.get('backend_baseline'),LIVE)
        require(snapshot_public(before) == snapshot_public(after), 'LIVE_STATE_CHANGED_DURING_BUILD')
        receipt['live_after'] = snapshot_public(after)
        receipt['status'] = 'built-not-activated'
        receipt['live_state_unchanged'] = True
    except Exception as exc:
        receipt['status'] = 'partial-unverified'
        receipt['error'] = str(exc) if isinstance(exc,StageError) else 'REMOTE_STAGE_BUILD_FAILED'
        if release is not None:
            try:
                receipt['live_state_unchanged'] = snapshot_public(live_snapshot()) == receipt.get('live_before')
            except Exception:
                receipt['live_state_unchanged'] = False
    if release is not None:
        private_json(release/'build-receipt.json',receipt)
        receipt['remote_receipt'] = str(release/'build-receipt.json')
    print(json.dumps(receipt))
remote_main()
'''


def remote_program() -> str:
    functions = (StageError, require, canonical, digest, parse_mem_available, resource_gate, validate_binding, safe_source_path,
                 reject_secret_material, validate_context_payloads, validate_stage_rows,
                 extract_verified_tar, validate_frontend_args, frontend_runtime_args, frontend_args_from_inspection, frontend_args_observation, safe_image_source_labels,
                 selected_contexts, build_commands, runtime_probe_command, validate_runtime_probe, execute_serial_builds, check_backend_baseline, validate_alf_contract, validate_alcf_contract, release_preflight.validate_lf_contract)
    preamble = "from __future__ import annotations\nimport fnmatch,hashlib,io,json,pathlib,re,shlex,subprocess,sys,tarfile\nfrom pathlib import Path,PurePosixPath\nfrom urllib.parse import urlsplit\n"
    constants = {"CONTEXTS": CONTEXTS, "PROJECT": PROJECT, "RELEASE_ROOT": RELEASE_ROOT,
                 "MAX_STAGE_BYTES": MAX_STAGE_BYTES, "MAX_STAGE_FILES": MAX_STAGE_FILES,
                 "MIN_DISK_BYTES":MIN_DISK_BYTES, "MIN_MEMORY_BYTES":MIN_MEMORY_BYTES,
                 "FRONTEND_KEYS": FRONTEND_KEYS, "SOURCE_LABELS": SOURCE_LABELS, "FRONTEND_ARGS_LABEL":FRONTEND_ARGS_LABEL, "FORBIDDEN_PARTS": sorted(FORBIDDEN_PARTS),
                 "FORBIDDEN_NAMES": sorted(FORBIDDEN_NAMES), "ENV_TEMPLATES": sorted(ENV_TEMPLATES),
                 "SECRET_PATTERNS": SECRET_PATTERNS, "DATABASES": release_preflight.DATABASES,
                 "REVISION": release_preflight.REVISION, "HASH": release_preflight.HASH}
    shared = "\n_PROTECTED={}\nexec(" + repr(release_preflight.REMOTE_HELPERS) + ",_PROTECTED)\n"
    return preamble + "\n".join(name + "=" + repr(value) for name, value in constants.items()) + "\n" + "\n".join(inspect.getsource(function) for function in functions) + shared + REMOTE_MAIN


def execution_payload(request: dict, archive: bytes) -> tuple[str, bytes]:
    request = {**request, "program": base64.b64encode(remote_program().encode()).decode("ascii")}
    header = canonical(request) + b"\n"
    require(len(header) <= 8 * 1024 * 1024, "REMOTE_HEADER_SIZE_LIMIT")
    bootstrap = "import base64,json,sys;request=json.loads(sys.stdin.buffer.readline(8388609));program=base64.b64decode(request.pop('program'));exec(compile(program,'<renewal-stage-build>','exec'),{'REQUEST':request})"
    return "python3 -c " + shlex.quote(bootstrap), header + archive


def validate_remote_receipt(value: dict, request: dict) -> dict:
    if request.get("auth_learn_frontend"):
        validate_stage_rows(request["files"])
        require(all(row["path"].startswith(("services/auth/", "services/learn/", "frontend/")) for row in request["files"]), "ALF_STAGE_CONTEXT_CHANGED")
    allowed = {"schema_version", "kind", "status", "binding", "activation_performed", "database_mutation_performed",
               "images_pruned", "live_files_replaced", "completed_images", "live_before", "live_after", "stage",
               "tar_sha256", "source_file_count", "preflight_plan_sha256", "current_service", "error",
                "live_state_unchanged", "remote_receipt", "frontend_only", "backend_baseline", "runtime_readability_verified", "learn_only", "retained_baseline",
                "release_phase", "learn_code_contract", "auth_learn_frontend", "alf_contract", "auth_learn_community_frontend", "alcf_contract", "learn_frontend", "lf_contract"}
    require(isinstance(value, dict) and set(value) <= allowed and value.get("schema_version") == 1
            and value.get("kind") == "renewal-stage-build" and value.get("binding") == request["binding"], "REMOTE_RECEIPT_BINDING_MISMATCH")
    require(value.get("status") in {"built-not-activated", "partial-unverified"}
            and all(value.get(key) is False for key in ("activation_performed", "database_mutation_performed", "images_pruned", "live_files_replaced")), "REMOTE_RECEIPT_BOUNDARY_INVALID")
    validate_stage_rows(request["files"])
    require("runtime_readability_verified" not in value or type(value["runtime_readability_verified"]) is bool,
            "REMOTE_RUNTIME_PROOF_INVALID")
    expected_stage = {"file_count":len(request["files"]), "bytes":sum(row["bytes"] for row in request["files"]), "files_sha256":digest(canonical(request["files"]))}
    if "stage" in value:
        require(value["stage"] == expected_stage, "REMOTE_STAGE_FIELDS_INVALID")
    if "current_service" in value:
        require(value["current_service"] in {service for service,_ in CONTEXTS}, "REMOTE_CURRENT_SERVICE_INVALID")
        require(not request.get("auth_learn_frontend") or value["current_service"] in {"auth", "learn", "frontend"}, "REMOTE_CURRENT_SERVICE_INVALID")
    if "live_state_unchanged" in value:
        require(type(value["live_state_unchanged"]) is bool, "REMOTE_LIVE_STATE_INVALID")
    if "source_file_count" in value:
        require(type(value["source_file_count"]) is int and value["source_file_count"] == request["source_file_count"], "REMOTE_SOURCE_COUNT_INVALID")
    if "tar_sha256" in value:
        require(value["tar_sha256"] == request["tar_sha256"], "REMOTE_TAR_HASH_INVALID")
    if "preflight_plan_sha256" in value:
        expected_preflight = (request.get("preflight") or {}).get("plan_sha256")
        require(value["preflight_plan_sha256"] == expected_preflight, "REMOTE_PREFLIGHT_HASH_INVALID")
    if "error" in value:
        require(isinstance(value["error"], str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", value["error"]), "REMOTE_ERROR_UNSAFE")
    completed = value.get("completed_images")
    require(isinstance(completed, list) and len(completed) <= len(CONTEXTS), "REMOTE_IMAGE_INVENTORY_INVALID")
    contexts = selected_contexts(frontend_only=bool(request.get("frontend_only")), learn_only=bool(request.get("learn_only")), auth_learn_frontend=bool(request.get("auth_learn_frontend")), auth_learn_community_frontend=bool(request.get("auth_learn_community_frontend")), learn_frontend=bool(request.get("learn_frontend")))
    require(value.get("frontend_only", False) is bool(request.get("frontend_only")), "REMOTE_BUILD_MODE_CHANGED")
    require(value.get("learn_only", False) is bool(request.get("learn_only")), "REMOTE_BUILD_MODE_CHANGED")
    require(release_preflight.alf_phase(value) is bool(request.get("auth_learn_frontend"))
            and value.get("alf_contract") == request.get("alf_contract"), "REMOTE_BUILD_MODE_CHANGED")
    if request.get("auth_learn_frontend"):
        validate_alf_contract(value["alf_contract"])
    require(release_preflight.alcf_phase(value) is bool(request.get("auth_learn_community_frontend"))
            and value.get("alcf_contract") == request.get("alcf_contract"), "REMOTE_BUILD_MODE_CHANGED")
    if request.get("auth_learn_community_frontend"):
        validate_alcf_contract(value["alcf_contract"])
    require(release_preflight.lf_phase(value) is bool(request.get("learn_frontend"))
            and value.get("lf_contract") == request.get("lf_contract"), "REMOTE_BUILD_MODE_CHANGED")
    if request.get("learn_frontend"):
        release_preflight.validate_lf_contract(value["lf_contract"])
        require(all(row["path"].startswith(("services/learn/", "frontend/")) for row in request["files"]), "LF_STAGE_CONTEXT_CHANGED")
    require(all(value.get(key) == request.get(key) for key in ("release_phase", "learn_code_contract")), "REMOTE_BUILD_PHASE_CHANGED")
    if request.get("baseline_chain"):
        require(value.get("retained_baseline") == {key:request["backend_baseline"][key] for key in ("binding","images","image_bindings","expected_heads")}, "REMOTE_RETAINED_BASELINE_CHANGED")
        require("backend_baseline" not in value, "REMOTE_BACKEND_BASELINE_UNEXPECTED")
    elif request.get("frontend_only"):
        require(value.get("backend_baseline") == {key:request["backend_baseline"][key] for key in ("binding","images")}, "REMOTE_BACKEND_BASELINE_CHANGED")
    else:
        require("backend_baseline" not in value, "REMOTE_BACKEND_BASELINE_UNEXPECTED")
    require(len(completed) <= len(contexts), "REMOTE_IMAGE_INVENTORY_INVALID")
    for row, (service, _) in zip(completed, contexts):
        tag = "ctf-renewal/" + service + ":" + request["binding"]["commit"] + "-" + request["binding"]["release_nonce"]
        fields = {"service","tag","image_id"} | ({"frontend_args_sha256"} if service == "frontend" else set())
        require(isinstance(row, dict) and set(row) == fields and row["service"] == service and row["tag"] == tag
                and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"]), "REMOTE_IMAGE_INVENTORY_INVALID")
        if service == "frontend":
            require(isinstance(row["frontend_args_sha256"],str) and re.fullmatch(r"[0-9a-f]{64}",row["frontend_args_sha256"])
                    and row["frontend_args_sha256"] == value.get("live_before",{}).get("frontend_args_sha256"),"REMOTE_FRONTEND_ARGS_HASH_INVALID")
    for key in ("live_before", "live_after"):
        if key not in value:
            continue
        snapshot = value[key]
        require(isinstance(snapshot, dict) and set(snapshot) == {"project", "protected_key_paths", "containers", "protected_files", "frontend_args_sha256","frontend_args_observation"}
                and snapshot["project"] == PROJECT and isinstance(snapshot["containers"],list) and 4 <= len(snapshot["containers"]) <= 1000
                and isinstance(snapshot["protected_files"],list) and 6 <= len(snapshot["protected_files"]) <= 1000
                and isinstance(snapshot["frontend_args_sha256"],str) and re.fullmatch(r"[0-9a-f]{64}", snapshot["frontend_args_sha256"])
                and all(isinstance(row,dict) for row in [*snapshot["containers"],*snapshot["protected_files"]]), "REMOTE_LIVE_SNAPSHOT_INVALID")
        observation = snapshot["frontend_args_observation"]
        require(isinstance(observation,dict) and set(observation)=={"authoritative_source","runtime_env_present","runtime_env_missing","present_runtime_values_match","prior_compiled_args_verified"}
                and observation["authoritative_source"] == "protected-compose-build-args" and observation["present_runtime_values_match"] is True
                and observation["prior_compiled_args_verified"] is False and isinstance(observation["runtime_env_present"],list)
                and isinstance(observation["runtime_env_missing"],list) and all(isinstance(key,str) for key in observation["runtime_env_present"]+observation["runtime_env_missing"])
                and sorted(observation["runtime_env_present"]+observation["runtime_env_missing"]) == sorted(FRONTEND_KEYS),"REMOTE_FRONTEND_ARGS_OBSERVATION_INVALID")
        require(len({row.get("container_id") for row in snapshot["containers"]}) == len(snapshot["containers"]), "REMOTE_DUPLICATE_CONTAINER")
        require(len({row.get("path") for row in snapshot["protected_files"]}) == len(snapshot["protected_files"]), "REMOTE_DUPLICATE_PROTECTED_FILE")
        for row in snapshot["containers"]:
            require(isinstance(row, dict) and set(row) == {"service", "container_id", "image_id", "running", "pid", "source_labels"}
                    and re.fullmatch(r"[a-z][a-z0-9_-]{0,100}", row["service"])
                    and re.fullmatch(r"[0-9a-f]{64}", row["container_id"]) and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"])
                    and isinstance(row["running"], bool) and type(row["pid"]) is int and row["pid"] >= 0, "REMOTE_CONTAINER_INVALID")
            if "source_labels" in row:
                require(safe_image_source_labels(row["source_labels"]) == row["source_labels"], "REMOTE_SOURCE_LABELS_INVALID")
        for row in snapshot["protected_files"]:
            require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}
                    and row["path"].startswith(("/home/deploy/", "/var/lib/ctf-web/")) and ".." not in PurePosixPath(row["path"]).parts
                    and type(row["bytes"]) is int and row["bytes"] >= 0 and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "REMOTE_PROTECTED_FILE_INVALID")
        for service in ("auth-service","learn-service","community-service","frontend"):
            require(len([row for row in snapshot["containers"] if row["service"] == service and row["running"]]) == 1, "REMOTE_REQUIRED_SERVICE_MISSING")
        protected = {row["path"]:row for row in snapshot["protected_files"]}
        required = {ops.LIVE + "/" + name for name in ("infra/.env","infra/docker-compose.yml","infra/docker-compose.prod.yml","infra/caddy/Caddyfile")}
        require(required <= set(protected), "REMOTE_REQUIRED_PROTECTED_FILE_MISSING")
        keys = snapshot["protected_key_paths"]
        require(isinstance(keys,dict) and set(keys) == {"auth_jwt_private_key","platform_jwt_public_key"}
                and all(isinstance(path,str) and path in protected and protected[path]["bytes"] > 0 for path in keys.values()) and len(set(keys.values())) == 2
                and keys["auth_jwt_private_key"] == ops.LIVE + "/services/auth/jwt_private.pem", "REMOTE_PROTECTED_KEYS_UNVERIFIED")
    if "remote_receipt" in value:
        require(value["remote_receipt"] == RELEASE_ROOT + "/" + request["binding"]["release_nonce"] + "/build-receipt.json", "REMOTE_RECEIPT_PATH_INVALID")
    if value["status"] == "built-not-activated":
        require(len(completed) == len(contexts) and value.get("live_state_unchanged") is True and value.get("runtime_readability_verified") is True
                and value.get("live_before") == value.get("live_after") and "live_before" in value
                and value.get("tar_sha256") == request["tar_sha256"] and value.get("source_file_count") == request["source_file_count"]
                and value.get("stage") == {"file_count":len(request["files"]), "bytes":sum(row["bytes"] for row in request["files"]), "files_sha256":digest(canonical(request["files"]))}, "REMOTE_BUILD_SUCCESS_UNVERIFIED")
    return value


def exclusive_receipt(nonce: str, value: dict):
    return ops.receipt("stage-build-" + nonce + ".json", value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-fingerprint", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--release-nonce", required=True)
    parser.add_argument("--preflight-plan", type=Path)
    parser.add_argument("--preflight-plan-sha256")
    parser.add_argument("--execute", action="store_true")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--frontend-only", action="store_true")
    scope.add_argument("--learn-only", action="store_true")
    scope.add_argument("--auth-learn-frontend", action="store_true")
    scope.add_argument("--auth-learn-community-frontend", action="store_true")
    scope.add_argument("--learn-frontend", action="store_true")
    parser.add_argument("--learn-code-only", action="store_true")
    parser.add_argument("--baseline-activation-receipt", type=Path)
    parser.add_argument("--baseline-build-receipt", type=Path)
    parser.add_argument("--baseline-verify-receipt", type=Path)
    parser.add_argument("--baseline-release", nargs=3, action="append", type=Path, metavar=("ACTIVATION", "BUILD", "VERIFY"))
    args = parser.parse_args(argv)
    require(not args.learn_code_only or args.learn_only, "LEARN_CODE_SCOPE_INVALID")
    binding = validate_binding(args.source_fingerprint, args.commit, args.release_nonce)
    receipt_path = ops.TASK / "evidence" / ("stage-build-" + binding["release_nonce"] + ".json")
    if args.execute:
        require(args.preflight_plan is not None and args.preflight_plan_sha256 is not None, "EXECUTE_FRESH_PREFLIGHT_REQUIRED")
        require(not receipt_path.exists(), "EXECUTION_RECEIPT_ALREADY_EXISTS")
        require(not (ops.TASK / "evidence" / ("stage-build-request-" + binding["release_nonce"] + ".json")).exists(), "EXECUTION_NONCE_ALREADY_CONSUMED")
    manifest = ops.manifest()
    rows = validate_manifest(manifest, binding["source_fingerprint"])
    require(rows.get("scripts/renewal/stage_build_release.py", {}).get("sha256") == EXECUTOR_SOURCE_SHA256
            and rows.get("scripts/renewal/ops.py", {}).get("sha256") == OPS_SOURCE_SHA256
            and rows.get("scripts/renewal/release_preflight.py", {}).get("sha256") == PREFLIGHT_SOURCE_SHA256
            and rows.get("scripts/renewal/verify_release.py", {}).get("sha256") == VERIFY_SOURCE_SHA256, "EXECUTOR_SOURCE_CHANGED_SINCE_IMPORT")
    modes = bind_checkout(ops.WORKSPACE / "deploy" / "CTF_Web", ops.SOURCE, manifest, binding)
    import verify_release
    inputs = verify_release.baseline_inputs(args)
    chain_mode = bool(args.baseline_release) or args.learn_only or args.auth_learn_frontend or args.auth_learn_community_frontend or args.learn_frontend
    backend = load_retained_baseline(inputs, manifest["files"], frontend_only=args.frontend_only, learn_only=args.learn_only, learn_code_only=args.learn_code_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend) if chain_mode else load_backend_baseline(*inputs[0], manifest["files"]) if inputs else None
    contexts = selected_contexts(frontend_only=args.frontend_only, learn_only=args.learn_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)
    payloads = {name:(ops.SOURCE / name).read_bytes() for name in rows
                if any(name.startswith(context + "/") for _, context in contexts)}
    require(all(len(payload) == rows[name]["bytes"] and digest(payload) == rows[name]["sha256"]
                for name,payload in payloads.items()), "SOURCE_CHANGED_DURING_CONTEXT_READ")
    validate_context_payloads(payloads,contexts=contexts)
    preflight = load_preflight(args.preflight_plan, args.preflight_plan_sha256, binding, len(rows), learn_only=args.learn_only, learn_code_only=args.learn_code_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)
    if args.learn_code_only:
        require(preflight["learn_code_contract"]["baseline_receipts"] == backend["receipt_hashes"], "LEARN_CODE_BASELINE_CHAIN_CHANGED")
    if args.auth_learn_frontend:
        require(preflight["alf_contract"]["baseline_receipts"] == backend["receipt_hashes"], "ALF_BASELINE_CHAIN_CHANGED")
    if args.auth_learn_community_frontend:
        require(preflight["alcf_contract"]["baseline_receipts"] == backend["receipt_hashes"], "ALCF_BASELINE_CHAIN_CHANGED")
    if args.learn_frontend:
        require(preflight["lf_contract"]["baseline_receipts"] == backend["receipt_hashes"], "LF_BASELINE_CHAIN_CHANGED")
    value = {"schema_version": 1, "kind": "renewal-stage-build-plan", "created_at": datetime.now(timezone.utc).isoformat(),
             "binding": binding, "source_file_count": len(rows), "stage_file_count": len(payloads),
             "stage_bytes": sum(map(len, payloads.values())), "contexts_serial": [service for service,_ in contexts], "frontend_only":args.frontend_only,
             "remote_release": RELEASE_ROOT + "/" + binding["release_nonce"], "source_commit_blobs_verified": True,
             "execute": args.execute, "activation_performed": False, "preflight_bound": preflight is not None,
             "frontend_build_args": "read and cross-check live container/compose only during execute; values not published"}
    if chain_mode:
        value.update({"learn_only": args.learn_only, "baseline_receipts": backend["receipt_hashes"]})
    if args.learn_code_only:
        value.update({key: preflight[key] for key in ("release_phase", "learn_code_contract")})
    if args.auth_learn_frontend:
        value.update({key: preflight[key] for key in release_preflight.ALF_FIELDS})
    if args.auth_learn_community_frontend:
        value.update({key: preflight[key] for key in release_preflight.ALCF_FIELDS})
    if args.learn_frontend:
        value.update({key: preflight[key] for key in release_preflight.LF_FIELDS})
    require(ops.manifest()["fingerprint"] == binding["source_fingerprint"], "SOURCE_CHANGED_DURING_PLAN")
    if not args.execute:
        print(json.dumps(value))
        return 0
    archive, selected = make_tar(payloads, modes,contexts=contexts)
    request = {"binding": binding, "source_file_count": len(rows), "files": selected,
               "tar_sha256": digest(archive), "tar_bytes": len(archive), "preflight": preflight,
               "frontend_only":args.frontend_only,"learn_only":args.learn_only,"baseline_chain":chain_mode,"backend_baseline":backend}
    if args.learn_code_only:
        request.update({key: preflight[key] for key in ("release_phase", "learn_code_contract")})
    if args.auth_learn_frontend:
        request.update({key: preflight[key] for key in release_preflight.ALF_FIELDS})
    if args.auth_learn_community_frontend:
        request.update({key: preflight[key] for key in release_preflight.ALCF_FIELDS})
    if args.learn_frontend:
        request.update({key: preflight[key] for key in release_preflight.LF_FIELDS})
    command, payload = execution_payload(request, archive)
    require(ops.manifest()["fingerprint"] == binding["source_fingerprint"], "SOURCE_CHANGED_BEFORE_EXECUTION")
    require(git_bytes(ops.WORKSPACE / "deploy" / "CTF_Web", "rev-parse", "HEAD").decode().strip() == binding["commit"], "COMMIT_CHANGED_BEFORE_EXECUTION")
    value["kind"] = "renewal-stage-build"
    value["status"] = "remote-outcome-unconfirmed"
    ops.receipt("stage-build-request-" + binding["release_nonce"] + ".json", value)
    try:
        result = validate_remote_receipt(json.loads(ops.ssh(command, timeout=15000, stdin=payload)), request)
        value["remote"] = result
        value["status"] = result.get("status", "partial-unverified")
    except Exception:
        value["error"] = "REMOTE_OUTCOME_UNCONFIRMED_INSPECT_BEFORE_RETRY"
    receipt = exclusive_receipt(binding["release_nonce"], value)
    print(json.dumps({"receipt": str(receipt), "status": value["status"], "binding": binding,
                      "activation_performed": False, "secret_values_printed": False}))
    return 0 if value["status"] == "built-not-activated" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, StageError) else type(exc).__name__,
                          "secret_values_printed": False}))
        raise SystemExit(1)
