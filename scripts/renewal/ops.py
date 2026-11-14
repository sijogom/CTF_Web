"""Source-bound renewal inspection and deployment primitives.

Remote inspection never prints environment values or command stderr. Mutating
release actions are implemented separately after their inputs are verified.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from uuid import uuid4
import zlib


SOURCE = Path(__file__).resolve().parents[2]
WORKSPACE = SOURCE.parent
TASK = SOURCE / ".verification" / "2026-10-13-renewal"
HOST = os.environ.get("CTF_DEPLOY_HOST", "").strip()
LIVE = "/home/deploy/CTF_Web"
KEY = Path(os.environ.get("CTF_DEPLOY_KEY", ""))
KNOWN_HOSTS = Path(os.environ.get("CTF_KNOWN_HOSTS", ""))
SKIP_PARTS = frozenset({
    ".git", ".verification", ".venv", "__pycache__", "node_modules", ".next",
    ".playwright-mcp", ".playwright-cli", "vendor", ".idea", ".vscode",
    "dist", "build", "out", ".turbo", "volumes", "logs",
})
SKIP_NAMES = frozenset({
    "AGENTS.md", "CLAUDE.md", "MEMORY.md", "Thumbs.db", ".DS_Store", "tsconfig.tsbuildinfo", "next-env.d.ts",
    "local-session-cookies.txt", "C\uf03aProgram",
})
ROOTS = ("frontend", "services", "infra", "ctfd", "scripts")
ROOT_FILES = ("README.md", ".gitignore")


def linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def allowed(path: Path) -> bool:
    relative = path.relative_to(SOURCE).as_posix()
    if relative.startswith(("docs/", "mockups/")):
        return False
    if path.name in SKIP_NAMES or path.suffix.lower() in {".pem", ".key", ".pyc", ".log", ".zip"}:
        return False
    if path.name.startswith(".env"):
        return path.name in {".env.example", ".env.sample", ".env.template", ".env.schema"}
    return not any(part in SKIP_PARTS for part in path.relative_to(SOURCE).parts)


def source_paths():
    for name in ROOT_FILES:
        path = SOURCE / name
        if path.is_file() and not linked(path):
            yield path
    for name in ROOTS:
        root = SOURCE / name
        if not root.is_dir() or linked(root):
            continue
        for directory, children, files in os.walk(root, followlinks=False):
            base = Path(directory)
            children[:] = sorted(child for child in children if child not in SKIP_PARTS
                and not linked(base / child)
                and not (child == "data" and (base.relative_to(SOURCE).as_posix() in {"infra", "ctfd"}
                    or base.parent == SOURCE / "services")))
            for name in sorted(files):
                path = base / name
                if not linked(path) and allowed(path):
                    yield path


def manifest() -> dict:
    rows = []
    for path in sorted(source_paths()):
        payload = path.read_bytes()
        rows.append({"path": path.relative_to(SOURCE).as_posix(), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"created_at": datetime.now(timezone.utc).isoformat(), "source": str(SOURCE), "fingerprint": digest, "files": rows}


def receipt(name: str, value: dict) -> Path:
    directory = TASK / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def ssh_invocation(command: str) -> list[str]:
    if not HOST or not KEY.is_file() or not KNOWN_HOSTS.is_file():
        raise RuntimeError("SSH_IDENTITY_OR_KNOWN_HOSTS_MISSING")
    return [
        "ssh.exe", "-F", "NUL", "-T", "-p", "10022", "-i", str(KEY),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", "AddKeysToAgent=no", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        f"deploy@{HOST}", command,
    ]


def _native_ssh(command: str, *, timeout: int = 60, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(ssh_invocation(command), input=stdin, capture_output=True, timeout=timeout)
    if result.returncode:
        # Stderr may contain unrelated login diagnostics. Keep the public error bounded.
        raise RuntimeError(f"SSH_COMMAND_FAILED_{result.returncode}")
    return result.stdout


MAX_STDIN_BYTES = 8 * 1024
MAX_INLINE_COMMAND_BYTES = 20000
TRANSPORT_ROOT = "/home/deploy/.ctf-web-renewal/transport"


def _inline_python(source: str) -> str:
    encoded = base64.b64encode(zlib.compress(source.encode("utf-8"), level=9)).decode("ascii")
    program = f"import base64,zlib;exec(compile(zlib.decompress(base64.b64decode({encoded!r})),'<renewal-remote>','exec'))"
    command = "python3 -c " + shlex.quote(program)
    if len(command.encode("utf-8")) > MAX_INLINE_COMMAND_BYTES:
        raise RuntimeError("REMOTE_INLINE_COMMAND_TOO_LARGE")
    return command


_TRANSPORT_HELPER = r'''
import hashlib,json,os,pwd,stat,sys
ROOT='/home/deploy/.ctf-web-renewal'
PARENT=ROOT+'/transport'
DIRECTORY=PARENT+'/'+SPEC['nonce']
PAYLOAD=DIRECTORY+'/payload'
UID=pwd.getpwnam('deploy').pw_uid
if UID<=0 or os.getuid()!=UID: raise RuntimeError('TRANSPORT_USER_INVALID')
def directory(path,private=False):
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode): raise RuntimeError('TRANSPORT_DIRECTORY_INVALID')
    if (private or path=='/home/deploy') and info.st_uid!=UID: raise RuntimeError('TRANSPORT_OWNER_INVALID')
    if private and stat.S_IMODE(info.st_mode)!=0o700: raise RuntimeError('TRANSPORT_MODE_INVALID')
    return info
def parents(create=False):
    for path in ('/','/home','/home/deploy'): directory(path)
    for path in (ROOT,PARENT):
        if create:
            try: os.mkdir(path,0o700)
            except FileExistsError: pass
        directory(path,True)
def identity():
    parents()
    fd=os.open(DIRECTORY,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    info=os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=UID or stat.S_IMODE(info.st_mode)!=0o700 or info.st_dev!=SPEC['device'] or info.st_ino!=SPEC['inode']:
        os.close(fd);raise RuntimeError('TRANSPORT_DIRECTORY_CHANGED')
    return fd
def payload_fd(directory_fd):
    before=os.stat('payload',dir_fd=directory_fd,follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_uid!=UID or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1 or before.st_size!=SPEC['bytes']:
        raise RuntimeError('TRANSPORT_PAYLOAD_INVALID')
    fd=os.open('payload',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory_fd)
    try:
        opened=os.fstat(fd)
        if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino): raise RuntimeError('TRANSPORT_PAYLOAD_CHANGED')
        digest=hashlib.sha256();count=0
        while True:
            block=os.read(fd,1024*1024)
            if not block: break
            count+=len(block)
            if count>SPEC['bytes']: raise RuntimeError('TRANSPORT_PAYLOAD_GREW')
            digest.update(block)
        after=os.fstat(fd)
        if count!=SPEC['bytes'] or digest.hexdigest()!=SPEC['sha256'] or (after.st_size,after.st_mtime_ns,after.st_nlink)!=(before.st_size,before.st_mtime_ns,1):
            raise RuntimeError('TRANSPORT_PAYLOAD_HASH_CHANGED')
        os.lseek(fd,0,os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd);raise
os.umask(0o077)
if ACTION=='create':
    parents(True)
    os.mkdir(DIRECTORY,0o700)
    info=directory(DIRECTORY,True)
    print(json.dumps({'created':True,'uid':UID,'device':info.st_dev,'inode':info.st_ino}))
elif ACTION=='execute':
    directory_fd=identity()
    try: fd=payload_fd(directory_fd)
    finally: os.close(directory_fd)
    os.dup2(fd,0)
    if fd!=0: os.close(fd)
    os.set_inheritable(0,True)
    os.execv('/bin/sh',['sh','-c',SPEC['command']])
elif ACTION=='cleanup':
    directory_fd=identity()
    parent_fd=os.open(PARENT,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        fd=payload_fd(directory_fd);os.close(fd)
        if os.listdir(directory_fd)!=['payload']: raise RuntimeError('TRANSPORT_DIRECTORY_NOT_EXCLUSIVE')
        info=os.stat(SPEC['nonce'],dir_fd=parent_fd,follow_symlinks=False)
        if (info.st_dev,info.st_ino)!=(SPEC['device'],SPEC['inode']): raise RuntimeError('TRANSPORT_DIRECTORY_CHANGED')
        os.unlink('payload',dir_fd=directory_fd)
        os.rmdir(SPEC['nonce'],dir_fd=parent_fd)
    finally:
        os.close(directory_fd);os.close(parent_fd)
    print(json.dumps({'removed':True}))
else: raise RuntimeError('TRANSPORT_ACTION_INVALID')
'''


def _transport_command(action: str, spec: dict) -> str:
    if action not in {"create", "execute", "cleanup"}:
        raise RuntimeError("TRANSPORT_ACTION_INVALID")
    return _inline_python("SPEC=" + repr(spec) + "\nACTION=" + repr(action) + "\n" + _TRANSPORT_HELPER)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("TRANSPORT_TIMEOUT")
    return remaining


def _sftp_payload(path: str, payload: bytes, uid: int, *, deadline: float) -> None:
    # Import lazily: read-only compressed commands and native downloads do not
    # need a Paramiko connection or a remote temporary file.
    import paramiko
    if not HOST or not KEY.is_file() or not KNOWN_HOSTS.is_file():
        raise RuntimeError("SSH_IDENTITY_OR_KNOWN_HOSTS_MISSING")
    client = paramiko.SSHClient()
    timer = threading.Timer(_remaining(deadline), client.close)
    timer.daemon = True
    timer.start()
    try:
        client.load_host_keys(str(KNOWN_HOSTS))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        connect_timeout = min(15, _remaining(deadline))
        client.connect(HOST, port=10022, username="deploy", key_filename=str(KEY),
            allow_agent=False, look_for_keys=False, timeout=connect_timeout,
            banner_timeout=connect_timeout, auth_timeout=connect_timeout)
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(_remaining(deadline))
            with sftp.open(path, "wx") as stream:
                stream.set_pipelined(False)
                sftp.chmod(path, 0o600)
                for offset in range(0, len(payload), 4096):
                    sftp.get_channel().settimeout(_remaining(deadline))
                    stream.write(payload[offset:offset + 4096])
                    stream.flush()
            info = sftp.lstat(path)
            import stat
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(payload)):
                raise RuntimeError("SFTP_PAYLOAD_IDENTITY_INVALID")
    finally:
        timer.cancel()
        client.close()


def _payload_ssh(command: str, payload: bytes, *, timeout: int) -> bytes:
    if not isinstance(payload, bytes) or not isinstance(command, str) or not command or timeout <= 0:
        raise RuntimeError("TRANSPORT_INPUT_INVALID")
    # Validate the strict local identity before creating an intent or remote path.
    ssh_invocation("true")
    nonce = str(uuid4())
    remote_path = TRANSPORT_ROOT + "/" + nonce + "/payload"
    digest = hashlib.sha256(payload).hexdigest()
    spec = {"nonce": nonce, "bytes": len(payload), "sha256": digest, "command": command}
    _transport_command("execute", spec | {"device": 0, "inode": 0})
    intent = {"schema_version": 1, "kind": "renewal-ssh-payload-transport-intent", "nonce": nonce,
        "created_at": datetime.now(timezone.utc).isoformat(), "target": {"host": HOST, "port": 10022, "user": "deploy"},
        "remote_path": remote_path, "payload_bytes": len(payload), "payload_sha256": digest,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(), "automatic_retry_performed": False}
    receipt(f"transport-intent-{nonce}.json", intent)
    deadline = time.monotonic() + timeout
    phase = "create_pending"
    try:
        created = json.loads(_native_ssh(_transport_command("create", spec), timeout=_remaining(deadline)))
        if (not isinstance(created, dict) or set(created) != {"created", "uid", "device", "inode"}
                or created["created"] is not True or any(type(created[key]) is not int or created[key] < 0
                    for key in ("uid", "device", "inode")) or created["uid"] == 0):
            raise RuntimeError("TRANSPORT_DIRECTORY_UNVERIFIED")
        spec.update({"device": created["device"], "inode": created["inode"]})
        phase = "upload_pending"
        _sftp_payload(remote_path, payload, created["uid"], deadline=deadline)
        phase = "execute_pending"
        output = _native_ssh(_transport_command("execute", spec), timeout=_remaining(deadline))
        phase = "cleanup_pending"
        cleaned = json.loads(_native_ssh(_transport_command("cleanup", spec), timeout=_remaining(deadline)))
        if cleaned != {"removed": True}:
            raise RuntimeError("TRANSPORT_CLEANUP_UNVERIFIED")
        receipt(f"transport-complete-{nonce}.json", {**intent, "kind": "renewal-ssh-payload-transport-complete",
            "completed_at": datetime.now(timezone.utc).isoformat(), "command_succeeded": True, "cleanup_verified": True})
        return output
    except Exception:
        receipt(f"transport-unresolved-{nonce}.json", {**intent, "kind": "renewal-ssh-payload-transport-unresolved",
            "recorded_at": datetime.now(timezone.utc).isoformat(), "phase": phase,
            "remote_payload_state": "may_exist_preserved_no_blind_retry", "cleanup_verified": False})
        raise RuntimeError("SSH_PAYLOAD_TRANSPORT_UNRESOLVED_" + nonce) from None


def ssh(command: str, *, timeout: int = 60, stdin: bytes | None = None) -> bytes:
    if stdin is not None and len(stdin) > MAX_STDIN_BYTES:
        return _payload_ssh(command, stdin, timeout=timeout)
    return _native_ssh(command, timeout=timeout, stdin=stdin)


def streaming_ssh(command: str, stdout_file: Path, *, timeout: int = 1800) -> dict:
    """Stream only into a new file beneath the ACL-protected task backup root."""
    root = TASK / "runtime" / "private-backups"
    destination = Path(stdout_file)
    if not destination.resolve().is_relative_to(root.resolve()) or destination == root:
        raise RuntimeError("BACKUP_STREAM_PATH_ESCAPE")
    if any(linked(parent) for parent in (destination, *destination.parents) if parent.is_relative_to(root)):
        raise RuntimeError("BACKUP_STREAM_LINK_FORBIDDEN")
    with destination.open("xb") as output:
        process = subprocess.Popen(ssh_invocation(command), stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.DEVNULL)
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("BACKUP_STREAM_TIMED_OUT_PARTIAL_RETAINED") from None
        output.flush()
        os.fsync(output.fileno())
        if status:
            raise RuntimeError("BACKUP_STREAM_FAILED_PARTIAL_RETAINED")
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {"bytes": destination.stat().st_size, "sha256": digest.hexdigest()}


def remote_python(source: str, *, timeout: int = 60) -> dict:
    try:
        command = _inline_python(source)
    except RuntimeError as error:
        if str(error) != "REMOTE_INLINE_COMMAND_TOO_LARGE":
            raise
    else:
        return json.loads(ssh(command, timeout=timeout))
    program = "import sys;exec(compile(sys.stdin.buffer.read(),'<renewal-remote>','exec'))"
    return json.loads(ssh("python3 -c " + shlex.quote(program), timeout=timeout,
                         stdin=source.encode("utf-8")))


PREFLIGHT = '''
import getpass, hashlib, json, os, pathlib, socket, subprocess
root = pathlib.Path('/home/deploy/CTF_Web')
def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError('INSPECTION_FAILED_' + str(result.returncode))
    return result.stdout
names = run(['docker', 'ps', '-a', '--format', '{{.Names}}']).splitlines()
selected = [name for name in names if name.startswith('ctf-') or name.startswith('ctf_web-')]
containers = []
if selected:
    for item in json.loads(run(['docker', 'inspect', *selected])):
        labels = item.get('Config', {}).get('Labels', {}) or {}
        state = item.get('State', {})
        containers.append({'name': item['Name'].lstrip('/'), 'id': item['Id'], 'image': item['Image'],
          'project': labels.get('com.docker.compose.project'), 'service': labels.get('com.docker.compose.service'),
          'state': state.get('Status'), 'health': state.get('Health', {}).get('Status'),
          'restart_count': item.get('RestartCount'), 'mounts': [{'type': m['Type'], 'name': m.get('Name'),
          'source': m.get('Source'), 'destination': m.get('Destination')} for m in item.get('Mounts', [])]})
stat = os.statvfs(root)
memory = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines())
files = {}
for relative in ['infra/docker-compose.yml', 'infra/docker-compose.prod.yml', 'infra/caddy/Caddyfile', 'infra/.env']:
    path = root / relative
    files[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
print(json.dumps({'user': getpass.getuser(), 'host': socket.gethostname(), 'live_root': str(root),
 'disk_available_bytes': stat.f_bavail * stat.f_frsize, 'memory_available_bytes': int(memory['MemAvailable'].strip().split()[0]) * 1024,
 'containers': containers, 'protected_sha256': files}))
'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("baseline", "preflight", "manifest"))
    args = parser.parse_args()
    if args.action == "baseline":
        value = manifest()
        path = receipt("source-before.json", value)
        print(json.dumps({"receipt": str(path), "files": len(value["files"]), "fingerprint": value["fingerprint"]}))
    elif args.action == "manifest":
        value = manifest()
        print(json.dumps({"files": len(value["files"]), "fingerprint": value["fingerprint"]}))
    else:
        value = remote_python(PREFLIGHT)
        path = receipt("server-preflight.json", value)
        print(json.dumps({"receipt": str(path), "user": value["user"], "disk_available_bytes": value["disk_available_bytes"],
            "memory_available_bytes": value["memory_available_bytes"], "containers": [{k: item[k] for k in ("name", "project", "service", "state", "health")} for item in value["containers"]]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}))
        sys.exit(1)
