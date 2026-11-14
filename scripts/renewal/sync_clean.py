"""Synchronize the task-owned clean checkout without touching other copies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from ops import SOURCE, TASK, WORKSPACE, allowed, linked, manifest

TARGET = WORKSPACE / "deploy" / "CTF_Web"
STATE = TASK / "evidence" / "clean-copy-state.json"
BRANCH = os.environ.get("CTF_SOURCE_BRANCH", "main")


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(TARGET), *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8").strip()


def tracked_paths() -> list[str]:
    return git("ls-files", "-z").rstrip("\0").split("\0")


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main() -> None:
    if TARGET.resolve() != TARGET or linked(TARGET) or not (TARGET / ".git").is_dir():
        raise RuntimeError("CLEAN_CHECKOUT_IDENTITY_INVALID")
    if git("branch", "--show-current") != BRANCH:
        raise RuntimeError("CLEAN_CHECKOUT_BRANCH_CHANGED")
    if git("remote", "get-url", "origin") != "https://github.com/sijogom/CTF_Web.git":
        raise RuntimeError("CLEAN_CHECKOUT_REMOTE_CHANGED")
    previous = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    if previous is None:
        if git("status", "--porcelain"):
            raise RuntimeError("INITIAL_CLEAN_CHECKOUT_HAS_USER_CHANGES")
        owned = {name: digest(TARGET / name) for name in tracked_paths()}
    else:
        owned = previous["owned_hashes"]
        for name, expected in previous.get("pending_hashes", {}).items():
            if digest(TARGET / name) == expected:
                owned[name] = expected
    source = manifest()
    files = {row["path"]: row for row in source["files"]}
    removed = []
    for name in sorted(set(tracked_paths()) | set(owned)):
        origin = SOURCE / name
        if not allowed(origin) or (not origin.is_file() and any(
                name.startswith(root + "/") for root in ("frontend", "services", "infra", "ctfd", "scripts"))):
            removed.append(name)
    normalized = {}
    if git("config", "--get", "core.autocrlf") != "false":
        for name in set(tracked_paths()) - set(files) - set(removed):
            destination = TARGET / name
            if digest(destination) != owned.get(name):
                raise RuntimeError("CLEAN_COPY_USER_CONFLICT: " + name)
            payload = subprocess.run(["git", "-C", str(TARGET), "show", "HEAD:" + name],
                                     capture_output=True, check=True).stdout
            current = destination.read_bytes()
            if current != payload:
                if current.replace(b"\r\n", b"\n") != payload:
                    raise RuntimeError("UNEXPECTED_CHECKOUT_TRANSFORMATION: " + name)
                normalized[name] = payload
    # Resolve and validate every operation before modifying the checkout.
    for name in set(files) | set(removed) | set(normalized):
        destination = TARGET / name
        if not destination.resolve().is_relative_to(TARGET):
            raise RuntimeError("CLEAN_COPY_PATH_ESCAPE")
        if any(linked(parent) for parent in (destination, *destination.parents) if parent.is_relative_to(TARGET)):
            raise RuntimeError("CLEAN_COPY_LINK_FORBIDDEN")
        if digest(destination) != owned.get(name):
            raise RuntimeError("CLEAN_COPY_USER_CONFLICT: " + name)
    pending = {name: row["sha256"] for name, row in files.items()}
    pending.update({name: None for name in removed})
    pending.update({name: hashlib.sha256(payload).hexdigest() for name, payload in normalized.items()})
    STATE.write_text(json.dumps({"owned_hashes": owned, "pending_hashes": pending},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name, payload in normalized.items():
        (TARGET / name).write_bytes(payload)
        owned[name] = pending[name]
    git("config", "--local", "core.autocrlf", "false")
    for name, row in files.items():
        origin, destination = SOURCE / name, TARGET / name
        if digest(origin) != row["sha256"]:
            raise RuntimeError("SOURCE_CHANGED_DURING_SYNC")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
        owned[name] = row["sha256"]
    for name in removed:
        destination = TARGET / name
        if destination.exists():
            destination.unlink()
        owned[name] = None
    for name, row in files.items():
        if digest(TARGET / name) != row["sha256"]:
            raise RuntimeError("CLEAN_COPY_HASH_MISMATCH")
    value = {"source_fingerprint": source["fingerprint"], "source_file_count": len(files),
             "target": str(TARGET), "branch": BRANCH, "removed_excluded_paths": removed,
             "owned_hashes": owned}
    STATE.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value[key] for key in value if key != "owned_hashes"}))


if __name__ == "__main__":
    main()
