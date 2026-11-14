"""Pure D2 Env-only release contracts. No SSH, Docker, Go, or credentials."""
from __future__ import annotations

import copy
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from pathlib import PurePosixPath
import sys
import tarfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import env_release as ENV


BINDING = {
    "commit": "a" * 40,
    "source_fingerprint": "b" * 64,
    "release_nonce": "11111111-2222-4333-8444-555555555555",
}
PLAN_SHA = "c" * 64
OLD_IMAGE = "sha256:" + "d" * 64
NEW_IMAGE = "sha256:" + "e" * 64


def source_row(name, payload=b"fixture"):
    return {"path": name, "bytes": len(payload), "sha256": ENV.digest(payload)}


def context_payloads():
    files = {
        "Dockerfile": (
            b"FROM golang:1.27.2-alpine3.24 AS builder\n"
            b"ENV GOTOOLCHAIN=local GOTELEMETRY=off\n"
            b"WORKDIR /build\nCOPY go.mod go.sum ./\nCOPY . .\n"
            b"RUN CGO_ENABLED=0 GOOS=linux go build -mod=readonly -o /out/env-orchestrator ./cmd\n"
            b"FROM alpine:3.24.2\n"
            b"COPY --from=builder /out/env-orchestrator /usr/local/bin/env-orchestrator\n"
            b"USER 1001:1001\nENTRYPOINT [\"/usr/local/bin/env-orchestrator\"]\n"
        ),
        ".dockerignore": b".git\n.env\n.env.*\n__pycache__\n.venv\n",
        "go.mod": (b"module github.com/ctf-platform/env-orchestrator\n\ngo 1.26.0\n\nrequire (\n"
                   b" github.com/redis/go-redis/v9 v9.6.3\n golang.org/x/net v0.60.0\n"
                   b" golang.org/x/crypto v0.57.0\n golang.org/x/sys v0.48.0\n"
                   b" golang.org/x/text v0.42.0\n)\n"),
        "go.sum": b"",
        "cmd/main.go": b"package main\nfunc main() {}\n",
        "internal/lifecycle/fixture_test.go": b"package lifecycle\n",
    }
    return {ENV.PREFIX + name: payload for name, payload in files.items()}


def manifest_for(payloads):
    rows = [source_row(name, payload) for name, payload in sorted(payloads.items())]
    return {"files": rows, "fingerprint": ENV.stage.manifest_fingerprint(rows)}


def target_row():
    return {"service": ENV.SERVICE, "container_id": "1" * 64,
            "image_id": OLD_IMAGE, "running": True, "pid": 1001,
            "mounts": [{"source": "/runtime/flags", "destination": "/flags", "read_only": False}],
            "health": "healthy", "started_at": "2026-10-15T00:00:00Z", "restart_count": 0}


def snapshot():
    return {
        "containers": [target_row(), {"service": "learn-service", "container_id": "2" * 64,
            "image_id": "sha256:" + "f" * 64, "running": True, "pid": 2001,
            "mounts": [], "health": "healthy", "restart_count": 0}],
        "code_files": [source_row(ENV.PREFIX + "cmd/main.go"), source_row("frontend/src/app/page.tsx")],
        "protected_files": [source_row("infra/.env", b"opaque-hash-fixture")],
        "labs": [{"container_id": "3" * 64, "image_id": "sha256:" + "9" * 64,
                  "running": True, "ports": [{"container": 8000, "host": 45000}], "mounts": []}],
        "databases": {"auth": ["005"], "learn": ["029"], "community": ["005"]},
        "retained_labels": {"frontend": {"org.opencontainers.image.revision": "7" * 40}},
        "target_runtime_sha256": "8" * 64,
    }


def runtime_proof():
    return {"uid": 1001, "elf_magic": "7f454c46", "elf_machine": 62,
            "binary_sha256": "1" * 64, "builder_binary_sha256": "1" * 64,
            "compiler": "go1.27.2", "cgo_enabled": "0",
            "build_source": copy.deepcopy(BINDING),
            "startup": {"exit_code": 1, "redis_failure_observed": True,
                        "production_io": False, "timed_out": False, "health_success_claimed": False}}


def module_graph_fixture():
    return [{"Path": "github.com/ctf-platform/env-orchestrator", "Main": True, "GoVersion": "1.26.0"},
            {"Path": "github.com/redis/go-redis/v9", "Version": "v9.6.3"},
            {"Path": "golang.org/x/net", "Version": "v0.60.0"},
            {"Path": "golang.org/x/crypto", "Version": "v0.57.0"},
            {"Path": "golang.org/x/sys", "Version": "v0.48.0"},
            {"Path": "golang.org/x/term", "Version": "v0.46.0"},
            {"Path": "golang.org/x/text", "Version": "v0.42.0"}]


GO_BUILD_INFO = b"/binary: go1.27.2\n\tbuild\tCGO_ENABLED=0\n\tbuild\tGOOS=linux\n"


def backup_proof():
    return {"kind": "env-fence-backup", "binding": copy.deepcopy(BINDING),
            "plan_sha256": PLAN_SHA, "components": sorted(ENV.COMPONENTS),
            "env_stopped": True, "other_db2_clients": 0, "other_services_controlled": 0,
            "redis_database": 2, "automatic_restore": False,
            "files": [{"path": "private/old-env-image.tar", "bytes": 128, "sha256": "2" * 64},
                      {"path": "private/env-state.tar", "bytes": 256, "sha256": "3" * 64}]}


def plan():
    return {"schema_version": 1, "kind": "env-release-plan",
            "created_at": datetime.now(timezone.utc).isoformat(), "binding": copy.deepcopy(BINDING),
            "scope": copy.deepcopy(ENV.SCOPE), "managed_files": [source_row(ENV.PREFIX + "cmd/main.go")],
            "modes": {ENV.PREFIX + "cmd/main.go": 0o644},
            "inspection": snapshot(), "tool_sources": {"fixture": "4" * 64}}


class MemoryStream(io.BytesIO):
    def __init__(self, path, initial=b"", *, write=False):
        super().__init__(initial)
        self.path = path
        self.name = str(path)
        self.write_back = write

    def fileno(self):
        return 123

    def close(self):
        if not self.closed and self.write_back:
            self.path.files[str(self.path)] = self.getvalue()
            self.path.events.append(("write", str(self.path)))
        super().close()


class MemoryPath:
    """Remote filesystem double. It never reads or writes host paths."""

    def __init__(self, value, files, events):
        self.value = PurePosixPath(str(value))
        self.files, self.events = files, events

    def __str__(self):
        return str(self.value)

    def __truediv__(self, name):
        return MemoryPath(self.value / str(name), self.files, self.events)

    def __lt__(self, other):
        return str(self) < str(other)

    @property
    def name(self):
        return self.value.name

    @property
    def parent(self):
        return MemoryPath(self.value.parent, self.files, self.events)

    @property
    def parents(self):
        return tuple(MemoryPath(value, self.files, self.events) for value in self.value.parents)

    def with_name(self, name):
        return MemoryPath(self.value.with_name(name), self.files, self.events)

    def is_symlink(self):
        return False

    def is_relative_to(self, other):
        return self.value.is_relative_to(PurePosixPath(str(other)))

    def exists(self):
        return str(self) in self.files

    def is_file(self):
        return self.exists()

    def read_bytes(self):
        return self.files[str(self)]

    def read_text(self):
        return self.read_bytes().decode()

    def stat(self, **_kwargs):
        return SimpleNamespace(st_size=len(self.read_bytes()), st_mode=0o100644)

    def mkdir(self, **_kwargs):
        self.events.append(("mkdir", str(self)))

    def open(self, mode):
        if "x" in mode and self.exists():
            raise FileExistsError(str(self))
        if "r" in mode and not self.exists():
            raise FileNotFoundError(str(self))
        stream = MemoryStream(self, self.files.get(str(self), b""), write=any(token in mode for token in "xwa+"))
        if "a" in mode:
            stream.seek(0, io.SEEK_END)
        return stream

    def glob(self, pattern):
        return [MemoryPath(name, self.files, self.events) for name in self.files
                if PurePosixPath(name).parent == self.value and PurePosixPath(name).match(pattern)]


def remote_namespace(action="inspect", request=None):
    """Compile the exact transport payload, stopping before its main call."""
    request = {"scope": copy.deepcopy(ENV.SCOPE)} if request is None else request
    with patch.object(ENV.ops, "remote_python", return_value={}) as transport:
        ENV.remote(action, request)
    transport.assert_called_once()
    program = transport.call_args.args[0]
    suffix = "\nmain()\n"
    if not program.endswith(suffix):
        raise AssertionError("Remote payload main entry point changed; review its harness")
    fcntl = ModuleType("fcntl")
    fcntl.LOCK_EX, fcntl.LOCK_NB = 2, 4
    fcntl.flock = Mock()
    namespace = {}
    with patch.dict(sys.modules, {"fcntl": fcntl}):
        exec(compile(program[:-len(suffix)], "<env-transport-payload>", "exec"), namespace)
    namespace["run"] = Mock(side_effect=AssertionError("real command forbidden"))
    namespace["subprocess"] = SimpleNamespace(run=Mock(side_effect=AssertionError("real process forbidden")))
    namespace["socket"] = SimpleNamespace(create_connection=Mock(side_effect=AssertionError("real socket forbidden")))
    namespace["time"] = SimpleNamespace(time=Mock(return_value=1.0), monotonic=Mock(return_value=0.0), sleep=Mock())
    return namespace


def memory_remote(action="inspect"):
    value = plan()
    request = {"plan": value, "plan_sha256": PLAN_SHA, "scope": copy.deepcopy(ENV.SCOPE)}
    namespace = remote_namespace(action, request)
    files, events = {}, []
    factory = lambda name: MemoryPath(name, files, events)
    namespace["Path"] = factory
    namespace["ROOT"] = factory("/owned-live")
    namespace["REMOTE_ROOT"] = "/owned-release"
    namespace["os"] = SimpleNamespace(chmod=Mock(), fsync=Mock(), access=Mock(return_value=False), R_OK=4,
                                      replace=Mock(side_effect=lambda old, new: files.__setitem__(str(new), files.pop(str(old)))))
    return namespace, factory("/owned-release"), value, files, events


def source_evidence_processes(namespace):
    namespace["_go_logs"] = {
        "modules": b"\n \n".join(json.dumps(row, indent=2).encode() for row in module_graph_fixture()),
        "imports": b"fmt\nnet/http\ngithub.com/ctf-platform/env-orchestrator/internal/container\n",
    }
    def child(command, *, stdout=None, **_kwargs):
        if command[:2] == ["docker", "pull"]:
            stdout.write(b"owned-official-base-pull-log\n")
            return SimpleNamespace(returncode=1 if command[2] == namespace.get("_pull_failure") else 0)
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=1)
        name = command[command.index("--name") + 1]
        kind = "modules" if name.startswith("ctf-env-go-modules-") else "imports"
        stdout.write(namespace["_go_logs"][kind])
        return SimpleNamespace(returncode=0)
    namespace["subprocess"] = SimpleNamespace(run=Mock(side_effect=child), STDOUT=-2)


def retained_fixture():
    current = snapshot()
    families = {family: "sha256:" + str(index) * 64 for index, family in enumerate(("auth", "learn", "community", "frontend"), 1)}
    current["containers"] = [target_row()]
    current["retained_labels"] = {}
    for index, (service, family) in enumerate(ENV.verify_release.SERVICE_IMAGES.items(), 10):
        item = {"service": service, "container_id": format(index, "064x"), "image_id": families[family],
                "running": True, "health": "healthy"}
        if service in ENV.verify_release.LEGACY_PROCESS_WORKERS:
            item.update(health=None, command=["python", "-m", ENV.verify_release.LEGACY_PROCESS_WORKERS[service]])
        current["containers"].append(item)
        current["retained_labels"][service] = {"org.opencontainers.image.revision": BINDING["commit"],
            "io.ctf-web.source-fingerprint": BINDING["source_fingerprint"], "io.ctf-web.release-nonce": BINDING["release_nonce"]}
    heads = {**ENV.verify_release.EXPECTED_HEADS, "learn": ENV.verify_release.LEARN_CATEGORY_HEAD}
    current["databases"] = {name: {"head": [head], "database": ENV.release_preflight.DATABASES[name],
        "schema_sha256": "9" * 64, "table_count": 10} for name, head in heads.items()}
    contract = {"protected_files": copy.deepcopy(current["protected_files"]),
        "managed_files": [copy.deepcopy(current["code_files"][1])], "images": families,
        "image_bindings": {family: copy.deepcopy(BINDING) for family in families}, "expected_heads": heads}
    value = plan()
    value["retained_baseline"] = {"contract": contract}
    return current, value


@contextmanager
def private_artifacts_fixture():
    files, events, artifacts, paths = {}, [], [], {}
    root = MemoryPath("/owned-private", files, events)
    for name, payload in (("old-env-image.tar", b"owned-image-fixture"), ("env-state.tar", b"owned-state-fixture")):
        path = root / name
        files[str(path)] = payload
        artifacts.append({"path": str(path), "bytes": len(payload), "sha256": ENV.digest(payload)})
        paths[str(Path(str(path)))] = path
    with patch.object(ENV.backup_release, "PRIVATE_ROOT", root), \
         patch.object(ENV.release_preflight, "task_path", side_effect=lambda path, _root: paths[str(path)]):
        yield artifacts, files, paths


class EnvBindingAndSourceTests(unittest.TestCase):
    def test_binding_preserves_exact_commit_fingerprint_and_fresh_uuid(self):
        original = copy.deepcopy(BINDING)
        self.assertIs(ENV.validate_binding(original), original)
        self.assertEqual(original, BINDING)

    def test_binding_rejects_missing_extra_or_non_object_fields(self):
        bad = [None, [], "binding", {}, {**BINDING, "extra": "forbidden"}]
        bad.extend({key: value for key, value in BINDING.items() if key != missing} for missing in BINDING)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ENV.EnvReleaseError):
                ENV.validate_binding(value)

    def test_binding_rejects_hash_aliases_wrong_lengths_and_uppercase(self):
        for field, values in (("commit", (None, "main", "a" * 39, "A" * 40, "a" * 41)),
                              ("source_fingerprint", (None, "b" * 63, "B" * 64, "b" * 65))):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ENV.EnvReleaseError):
                    ENV.validate_binding({**BINDING, field: value})

    def test_binding_rejects_traversal_reused_shape_and_noncanonical_nonce(self):
        for nonce in (None, "../../live", "not-a-uuid", "11111111-2222-1333-8444-555555555555",
                      "11111111222243338444555555555555", "{11111111-2222-4333-8444-555555555555}",
                      "AAAAAAAA-2222-4333-8444-555555555555"):
            with self.subTest(nonce=nonce), self.assertRaises(ENV.EnvReleaseError):
                ENV.validate_binding({**BINDING, "release_nonce": nonce})

    def test_only_env_scope_is_accepted_without_mutating_caller(self):
        candidate = copy.deepcopy(ENV.SCOPE)
        self.assertIs(ENV.validate_scope(candidate), candidate)
        self.assertEqual(candidate, {"contexts": [ENV.PREFIX], "services": [ENV.SERVICE],
                                     "stop_services": [ENV.SERVICE], "migrations": [], "automatic_restore": False})
        for field, value in (("contexts", ["services/learn/"]), ("services", [ENV.SERVICE, "frontend"]),
                             ("stop_services", [ENV.SERVICE, "ctf-minio"]), ("migrations", ["learn"]),
                             ("automatic_restore", True), ("extra", "forbidden")):
            with self.subTest(field=field), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_ONLY_SCOPE_REQUIRED"):
                ENV.validate_scope({**candidate, field: value})

    def test_context_manifest_selects_only_env_and_retains_full_fingerprint_contract(self):
        payloads = context_payloads()
        payloads["frontend/src/app/page.tsx"] = b"retained"
        value = manifest_for(payloads)
        frozen = copy.deepcopy(value)
        rows = ENV.context_rows(value)
        self.assertEqual([row["path"] for row in rows], sorted(context_payloads()))
        self.assertEqual(value, frozen)

    def test_context_manifest_rejects_fingerprint_drift_missing_required_source_and_duplicates(self):
        wrong = manifest_for(context_payloads())
        wrong["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(ENV.stage.StageError):
            ENV.context_rows(wrong)
        for missing in ("Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd/main.go"):
            payloads = context_payloads()
            del payloads[ENV.PREFIX + missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_CONTEXT_INCOMPLETE"):
                ENV.context_rows(manifest_for(payloads))
        value = manifest_for(context_payloads())
        value["files"].append(copy.deepcopy(value["files"][0]))
        value["fingerprint"] = ENV.stage.manifest_fingerprint(value["files"])
        with self.assertRaisesRegex(ENV.stage.StageError, "SOURCE_MANIFEST_PATH_INVALID"):
            ENV.context_rows(value)

    def test_context_manifest_rejects_empty_env_selection_and_unsafe_paths(self):
        with self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_CONTEXT_REQUIRED"):
            ENV.context_rows(manifest_for({"frontend/src/app/page.tsx": b"retained"}))
        for path in (ENV.PREFIX + "../learn/main.go", ENV.PREFIX + "cmd//main.go", "/" + ENV.PREFIX + "cmd/main.go"):
            payloads = context_payloads()
            payloads[path] = b"unsafe"
            with self.subTest(path=path), self.assertRaises((ENV.EnvReleaseError, ENV.stage.StageError)):
                ENV.context_rows(manifest_for(payloads))

    def test_pinned_go_context_is_accepted_without_source_rewrite(self):
        payloads = context_payloads()
        before = copy.deepcopy(payloads)
        ENV.validate_context_payloads(payloads)
        self.assertEqual(payloads, before)

    def test_context_payload_rejects_foreign_source_missing_file_and_size_overflow(self):
        payloads = context_payloads()
        for foreign in ("frontend/Dockerfile", "services/learn/app/main.py", "infra/docker-compose.yml"):
            with self.subTest(foreign=foreign), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_CONTEXT_SCOPE"):
                ENV.validate_context_payloads({**payloads, foreign: b"outside-context"})
        for missing in ("Dockerfile", ".dockerignore", "go.mod", "go.sum", "cmd/main.go"):
            candidate = {name: content for name, content in payloads.items() if name != ENV.PREFIX + missing}
            with self.subTest(missing=missing), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_CONTEXT_INCOMPLETE"):
                ENV.validate_context_payloads(candidate)
        with patch.object(ENV.stage, "MAX_STAGE_BYTES", 1), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_CONTEXT_SIZE"):
            ENV.validate_context_payloads(payloads)
        with patch.object(ENV.stage, "MAX_STAGE_FILES", 1), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_CONTEXT_SIZE"):
            ENV.validate_context_payloads(payloads)

    def test_context_rejects_traversal_links_cache_and_credential_paths(self):
        for suffix in ("../learn/main.go", "cmd//unsafe.go", "cmd/./unsafe.go", "cmd\\unsafe.go",
                       ".git/config", "node_modules/cache.js", ".verification/cache.bin", ".ssh/id_ed25519",
                       ".env", ".env.production", "private.pem", "session-cookies.txt", ".npmrc"):
            with self.subTest(suffix=suffix), self.assertRaises((ENV.EnvReleaseError, ENV.stage.StageError)):
                ENV.validate_context_payloads({**context_payloads(), ENV.PREFIX + suffix: b"fixture"})

    def test_context_secret_material_is_rejected_without_echoing_bytes(self):
        marker = b"-----BEGIN " + b"PRIVATE KEY-----"
        with self.assertRaises((ENV.EnvReleaseError, ENV.stage.StageError)) as caught:
            ENV.validate_context_payloads({**context_payloads(), ENV.PREFIX + "internal/fixture.go": marker})
        self.assertNotIn(marker.decode(), str(caught.exception))

    def test_context_allows_placeholder_template_but_rejects_literal_template_secret(self):
        payloads = {**context_payloads(), ENV.PREFIX + ".env.example": b"API_KEY=${API_KEY}\n"}
        ENV.validate_context_payloads(payloads)
        payloads[ENV.PREFIX + ".env.example"] = b"API_KEY=synthetic-fixture-private-value-123\n"
        with self.assertRaises((ENV.EnvReleaseError, ENV.stage.StageError)):
            ENV.validate_context_payloads(payloads)

    def test_archive_preserves_source_bytes_modes_and_only_env_entries_in_memory(self):
        payloads = context_payloads()
        payloads[ENV.PREFIX + "cmd/main.go"] += b"// raw CRLF preserved\r\n"
        value = manifest_for(payloads)
        modes = {name: 0o644 for name in payloads}
        modes[ENV.PREFIX + "cmd/main.go"] = 0o755
        files = {"/owned-source/" + name: payload for name, payload in payloads.items()}
        fake_source = MemoryPath("/owned-source", files, [])
        with patch.object(ENV.ops, "SOURCE", fake_source):
            encoded = ENV.make_archive(value["files"], modes)
        with tarfile.open(fileobj=io.BytesIO(encoded), mode="r:") as archive:
            self.assertEqual(archive.getnames(), sorted(payloads))
            self.assertTrue(all(member.isfile() for member in archive.getmembers()))
            for member in archive.getmembers():
                self.assertEqual(archive.extractfile(member).read(), payloads[member.name])
                self.assertEqual(member.mode, modes[member.name])
        self.assertEqual(payloads[ENV.PREFIX + "cmd/main.go"][-23:], b"// raw CRLF preserved\r\n")

    def test_archive_rejects_source_changed_after_manifest_before_transfer(self):
        payloads = context_payloads()
        value = manifest_for(payloads)
        modes = {name: 0o644 for name in payloads}
        payloads[ENV.PREFIX + "cmd/main.go"] += b"changed-after-plan"
        files = {"/owned-source/" + name: payload for name, payload in payloads.items()}
        fake_source = MemoryPath("/owned-source", files, [])
        with patch.object(ENV.ops, "SOURCE", fake_source), self.assertRaisesRegex(ENV.EnvReleaseError, "CONTEXT_CHANGED"):
            ENV.make_archive(value["files"], modes)

    def test_context_rejects_unpinned_compiler_runtime_cgo_and_missing_secret_exclusions(self):
        for old, new in ((b"golang:1.27.2-alpine3.24", b"golang:latest"),
                         (b"alpine:3.24.2", b"alpine:latest"),
                         (b"CGO_ENABLED=0 GOOS=linux go build", b"CGO_ENABLED=1 GOOS=linux go build")):
            payloads = context_payloads()
            payloads[ENV.PREFIX + "Dockerfile"] = payloads[ENV.PREFIX + "Dockerfile"].replace(old, new)
            with self.subTest(old=old), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_COMPILER_CONTRACT"):
                ENV.validate_context_payloads(payloads)
        for exclusion in (b".git\n", b".env\n", b".env.*\n"):
            payloads = context_payloads()
            payloads[ENV.PREFIX + ".dockerignore"] = payloads[ENV.PREFIX + ".dockerignore"].replace(exclusion, b"")
            with self.subTest(exclusion=exclusion), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_IGNORE_CONTRACT"):
                ENV.validate_context_payloads(payloads)

    def test_context_rejects_stale_module_go_minimum_selected_versions_and_toolchain_policy(self):
        for old, new in ((b"go 1.26.0", b"go 1.25.0"),
                         (b"module github.com/ctf-platform/env-orchestrator", b"module fixture.invalid/other"),
                         (b"github.com/redis/go-redis/v9 v9.6.3", b"github.com/redis/go-redis/v9 v9.6.0"),
                         (b"golang.org/x/net v0.60.0", b"golang.org/x/net v0.59.0"),
                         (b"golang.org/x/crypto v0.57.0", b"golang.org/x/crypto v0.56.0"),
                         (b"golang.org/x/sys v0.48.0", b"golang.org/x/sys v0.47.0"),
                         (b"golang.org/x/text v0.42.0", b"golang.org/x/text v0.41.0")):
            payloads = context_payloads()
            payloads[ENV.PREFIX + "go.mod"] = payloads[ENV.PREFIX + "go.mod"].replace(old, new)
            with self.subTest(old=old), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_MODULE_CONTRACT"):
                ENV.validate_context_payloads(payloads)
        for old, new in ((b"ENV GOTOOLCHAIN=local GOTELEMETRY=off", b"ENV GOTOOLCHAIN=auto GOTELEMETRY=off"),
                         (b"go build -mod=readonly", b"go build -mod=mod")):
            payloads = context_payloads()
            payloads[ENV.PREFIX + "Dockerfile"] = payloads[ENV.PREFIX + "Dockerfile"].replace(old, new)
            with self.subTest(old=old), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_COMPILER_CONTRACT"):
                ENV.validate_context_payloads(payloads)

    def test_root_term_may_be_absent_while_exact_transitive_selected_version_remains_required(self):
        payloads = context_payloads()
        self.assertNotIn(b"golang.org/x/term", payloads[ENV.PREFIX + "go.mod"])
        ENV.validate_context_payloads(payloads)
        graph = ENV.validate_module_graph(module_graph_fixture())
        self.assertEqual(graph["selected_modules"]["golang.org/x/term"], "v0.46.0")

    def test_explicit_root_term_allows_exact_version_once_in_block_or_single_line_require(self):
        for style in ("block", "single-line"):
            payloads = context_payloads()
            modules = payloads[ENV.PREFIX + "go.mod"]
            if style == "block":
                modules = modules.replace(b")\n", b" golang.org/x/term v0.46.0 // indirect\n)\n")
            else:
                modules += b"\nrequire golang.org/x/term v0.46.0 // indirect\n"
            payloads[ENV.PREFIX + "go.mod"] = modules
            with self.subTest(style=style):
                ENV.validate_context_payloads(payloads)

    def test_explicit_root_term_rejects_wrong_version_and_duplicates_in_both_declaration_forms(self):
        for style, version, duplicate in (("block", b"v0.45.0", False), ("single-line", b"v0.45.0", False),
                                          ("block", b"v0.46.0", True), ("single-line", b"v0.46.0", True)):
            payloads = context_payloads()
            modules = payloads[ENV.PREFIX + "go.mod"]
            declaration = b"golang.org/x/term " + version + b"\n"
            if style == "block":
                modules = modules.replace(b")\n", b" " + declaration + b")\n")
            else:
                modules += b"\nrequire " + declaration
            if duplicate:
                modules += b"\nrequire golang.org/x/term v0.46.0\n"
            payloads[ENV.PREFIX + "go.mod"] = modules
            with self.subTest(style=style, version=version, duplicate=duplicate), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_MODULE_CONTRACT"):
                ENV.validate_context_payloads(payloads)

    def test_other_five_root_module_declarations_remain_required(self):
        for declaration in (b" github.com/redis/go-redis/v9 v9.6.3\n", b" golang.org/x/net v0.60.0\n",
                            b" golang.org/x/crypto v0.57.0\n", b" golang.org/x/sys v0.48.0\n",
                            b" golang.org/x/text v0.42.0\n"):
            payloads = context_payloads()
            payloads[ENV.PREFIX + "go.mod"] = payloads[ENV.PREFIX + "go.mod"].replace(declaration, b"")
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_MODULE_CONTRACT"):
                ENV.validate_context_payloads(payloads)


class EnvModuleGraphTests(unittest.TestCase):
    def test_exact_approved_module_graph_retains_structural_digest_and_input_rows(self):
        rows = module_graph_fixture()
        frozen = copy.deepcopy(rows)
        proof = ENV.validate_module_graph(rows)
        self.assertEqual(proof["main_module"], "github.com/ctf-platform/env-orchestrator")
        self.assertEqual(proof["minimum_go"], "1.26.0")
        self.assertEqual(proof["selected_modules"], {row["Path"]: row["Version"] for row in frozen[1:]})
        self.assertEqual(proof["graph_sha256"], ENV.digest(ENV.canonical(frozen)))
        self.assertEqual(rows, frozen)

    def test_module_graph_rejects_shape_duplicate_replacement_wrong_main_and_minimum(self):
        for rows in (None, [], {}, "rows", [{}], [None], [{"Path": 123}]):
            with self.subTest(rows=rows), self.assertRaises(ENV.EnvReleaseError):
                ENV.validate_module_graph(rows)
        for change in ("duplicate", "replace", "module", "minimum", "two-main", "no-main"):
            rows = module_graph_fixture()
            if change == "duplicate":
                rows.append(copy.deepcopy(rows[1]))
            elif change == "replace":
                rows[1]["Replace"] = {"Path": "fixture.invalid/replaced"}
            elif change == "module":
                rows[0]["Path"] = "fixture.invalid/other"
            elif change == "minimum":
                rows[0]["GoVersion"] = "1.25.0"
            elif change == "two-main":
                rows[1]["Main"] = True
            else:
                rows[0]["Main"] = False
            with self.subTest(change=change), self.assertRaises(ENV.EnvReleaseError):
                ENV.validate_module_graph(rows)

    def test_module_graph_rejects_each_missing_or_differently_selected_pinned_module(self):
        for index in range(1, len(module_graph_fixture())):
            for change in ("missing", "version"):
                rows = module_graph_fixture()
                if change == "missing":
                    rows.pop(index)
                else:
                    rows[index]["Version"] = "v0.0.0"
                with self.subTest(index=index, change=change), self.assertRaisesRegex(ENV.EnvReleaseError, "MODULE_SELECTED_VERSION_CONTRACT"):
                    ENV.validate_module_graph(rows)

    def test_transitive_term_missing_or_wrong_selected_version_is_rejected_even_without_root_declaration(self):
        ENV.validate_context_payloads(context_payloads())
        for change in ("missing", "version"):
            rows = module_graph_fixture()
            if change == "missing":
                rows = [row for row in rows if row["Path"] != "golang.org/x/term"]
            else:
                next(row for row in rows if row["Path"] == "golang.org/x/term")["Version"] = "v0.45.0"
            with self.subTest(change=change), self.assertRaisesRegex(ENV.EnvReleaseError, "MODULE_SELECTED_VERSION_CONTRACT"):
                ENV.validate_module_graph(rows)
class EnvConsumerAndPreservationTests(unittest.TestCase):
    def test_only_exact_running_env_consumer_is_selected(self):
        rows = snapshot()["containers"]
        before = copy.deepcopy(rows)
        self.assertIs(ENV.select_consumer(rows), rows[0])
        self.assertEqual(rows, before)

    def test_missing_duplicate_stopped_or_unverified_env_consumer_is_rejected(self):
        cases = [[], [target_row(), target_row()]]
        cases.extend([{**target_row(), key: value}] for key, value in (
            ("service", "frontend"), ("running", False), ("running", 1),
            ("image_id", "env:latest"), ("image_id", "sha256:" + "D" * 64)))
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_SINGLE_RUNNING_CONSUMER"):
                ENV.select_consumer(rows)

    def test_initial_env_consumer_requires_healthy_readiness(self):
        for health in (None, "starting", "unhealthy", "unknown"):
            with self.subTest(health=health), self.assertRaises(ENV.EnvReleaseError):
                ENV.select_consumer([{**target_row(), "health": health}])

    def test_unknown_same_image_consumer_is_rejected_even_if_service_is_defined(self):
        for name in ("unknown-env-worker", "frontend", "ctf-challenge-instance"):
            rows = [target_row(), {**target_row(), "service": name, "container_id": "9" * 64}]
            with self.subTest(name=name), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_UNKNOWN_IMAGE_CONSUMER"):
                ENV.select_consumer(rows)

    def test_unchanged_snapshot_is_read_only_and_does_not_mutate_inputs(self):
        before, after = snapshot(), snapshot()
        original = copy.deepcopy((before, after))
        ENV.preserved_snapshot(before, after)
        self.assertEqual((before, after), original)

    def test_non_target_identity_image_state_mount_health_and_restart_changes_are_rejected(self):
        for key, value in (("container_id", "4" * 64), ("image_id", NEW_IMAGE), ("running", False),
                           ("pid", 0), ("mounts", [{"source": "/other"}]), ("health", "unhealthy"), ("restart_count", 1)):
            after = snapshot()
            after["containers"][1][key] = value
            for mode in ({}, {"env_stopped": True}, {"env_changed": True}):
                with self.subTest(key=key, mode=mode), self.assertRaisesRegex(ENV.EnvReleaseError, "NON_TARGET_CONTAINER_CHANGED"):
                    ENV.preserved_snapshot(snapshot(), after, **mode)

    def test_non_target_addition_and_removal_are_rejected(self):
        for addition in (True, False):
            after = snapshot()
            if addition:
                after["containers"].append({"service": "new-unrequested-service", "container_id": "8" * 64})
            else:
                after["containers"].pop()
            with self.subTest(addition=addition), self.assertRaisesRegex(ENV.EnvReleaseError, "NON_TARGET_CONTAINER_CHANGED"):
                ENV.preserved_snapshot(snapshot(), after, env_changed=True)

    def test_protected_database_labs_source_provenance_and_runtime_are_preserved_after_env_change(self):
        changes = (
            ("protected_files", lambda value: value[0].update(sha256="0" * 64), "PRESERVATION_PROTECTED_FILES"),
            ("databases", lambda value: value.update(learn=["030"]), "PRESERVATION_DATABASES"),
            ("labs", lambda value: value[0].update(running=False), "PRESERVATION_LABS"),
            ("labs", lambda value: value[0].update(image_id=NEW_IMAGE), "PRESERVATION_LABS"),
            ("labs", lambda value: value[0].update(ports=[]), "PRESERVATION_LABS"),
            ("labs", lambda value: value[0].update(mounts=[{"source": "/other"}]), "PRESERVATION_LABS"),
            ("code_files", lambda value: value[1].update(sha256="0" * 64), "RETAINED_SOURCE_CHANGED"),
            ("retained_labels", lambda value: value["frontend"].update(extra="changed"), "RETAINED_PROVENANCE_CHANGED"),
        )
        for field, change, error in changes:
            after = snapshot()
            change(after[field])
            with self.subTest(field=field, error=error), self.assertRaisesRegex(ENV.EnvReleaseError, error):
                ENV.preserved_snapshot(snapshot(), after, env_changed=True)
        after = snapshot()
        after["target_runtime_sha256"] = "0" * 64
        with self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_RUNTIME_CHANGED"):
            ENV.preserved_snapshot(snapshot(), after, env_changed=True)

    def test_env_source_image_and_identity_can_change_only_in_activation_mode(self):
        after = snapshot()
        after["code_files"][0]["sha256"] = "0" * 64
        after["containers"][0].update(container_id="4" * 64, image_id=NEW_IMAGE, pid=4001)
        with self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_SOURCE_CHANGED"):
            ENV.preserved_snapshot(snapshot(), after)
        ENV.preserved_snapshot(snapshot(), after, env_changed=True)
        after = snapshot()
        after["containers"][0]["pid"] = 4001
        with self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_BASELINE_CHANGED"):
            ENV.preserved_snapshot(snapshot(), after)

    def test_stopped_fence_allows_only_original_env_identity_image_mounts_with_pid_zero(self):
        after = snapshot()
        after["containers"][0].update(running=False, pid=0, health=None)
        ENV.preserved_snapshot(snapshot(), after, env_stopped=True)
        for field, value in (("container_id", "9" * 64), ("image_id", NEW_IMAGE),
                             ("mounts", []), ("running", True), ("pid", 1)):
            changed = copy.deepcopy(after)
            changed["containers"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ENV.EnvReleaseError, "ENV_STOP_FENCE_CHANGED"):
                ENV.preserved_snapshot(snapshot(), changed, env_stopped=True)


class EnvCommandAndRuntimeTests(unittest.TestCase):
    def test_build_is_serial_runtime_then_builder_with_unique_source_bound_tags(self):
        commands = ENV.build_commands(Path("/owned/source"), BINDING)
        self.assertEqual([name for name, _ in commands], ["runtime", "builder"])
        tags = [command[command.index("--tag") + 1] for _, command in commands]
        self.assertEqual(len(set(tags)), 2)
        self.assertTrue(all(BINDING["commit"] + "-" + BINDING["release_nonce"] in tag for tag in tags))
        for _, command in commands:
            self.assertEqual(command[:2], ["docker", "build"])
            self.assertEqual(command[command.index("--network") + 1], "host")
            self.assertIn("--pull", command)
            self.assertEqual(command[-1], str(Path("/owned/source") / ENV.PREFIX))
            labels = [command[index + 1] for index, token in enumerate(command) if token == "--label"]
            self.assertEqual(labels, ["org.opencontainers.image.revision=" + BINDING["commit"],
                                     "io.ctf-web.source-fingerprint=" + BINDING["source_fingerprint"],
                                     "io.ctf-web.release-nonce=" + BINDING["release_nonce"]])
            self.assertFalse(set(command) & {"compose", "up", "restart", "prune", "rmi", "system", "rm", "exec", "--build-arg"})
        self.assertNotIn("--target", commands[0][1])
        self.assertEqual(commands[1][1][commands[1][1].index("--target") + 1], "builder")

    def test_runtime_probe_is_nonroot_readonly_networkless_and_has_no_production_bindings(self):
        name = "ctf-env-probe-" + BINDING["release_nonce"]
        command = ENV.runtime_probe_command(NEW_IMAGE, name)
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertEqual(command[command.index("--name") + 1], name)
        for flag, expected in (("--network", "none"), ("--user", "1001:1001"), ("--cap-drop", "ALL"),
                               ("--security-opt", "no-new-privileges"), ("--pids-limit", "32"),
                               ("--memory", "128m"), ("--cpus", "0.5"), ("--entrypoint", "cat")):
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertIn("--read-only", command)
        self.assertEqual(command[-2:], [NEW_IMAGE, "/usr/local/bin/env-orchestrator"])
        self.assertFalse(set(command) & {"-v", "--volume", "--mount", "-e", "--env", "--env-file", "--privileged", "--publish", "-p", "--pid"})
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("redis", " ".join(command).lower())
        self.assertNotIn("python", " ".join(command).lower())

    def test_runtime_probe_requires_digest_and_owned_fixed_name(self):
        for image, name in (("env:latest", "ctf-env-probe-" + BINDING["release_nonce"]),
                            (NEW_IMAGE, "existing-user-container"), (NEW_IMAGE, "ctf-env-probe-../../outside"),
                            (NEW_IMAGE, "ctf-env-builder-probe-11111111-2222-1333-8444-555555555555")):
            with self.subTest(image=image, name=name), self.assertRaises(ENV.EnvReleaseError):
                ENV.runtime_probe_command(image, name)

    def test_builder_binary_probe_has_same_owned_nonce_isolation_limits_and_no_production_mount(self):
        nonce = BINDING["release_nonce"]
        runtime = ENV.runtime_probe_command(NEW_IMAGE, "ctf-env-probe-" + nonce)
        builder = ENV.runtime_probe_command(OLD_IMAGE, "ctf-env-builder-probe-" + nonce)
        self.assertEqual(builder[-2:], [OLD_IMAGE, "/env-orchestrator"])
        self.assertEqual(builder[builder.index("--name") + 1], "ctf-env-builder-probe-" + nonce)
        for command in (runtime, builder):
            self.assertEqual(command[command.index("--label") + 1], "io.ctf-web.env-test-nonce=" + nonce)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertEqual(command[command.index("--user") + 1], "1001:1001")
            self.assertEqual(command[command.index("--memory") + 1], "128m")
            self.assertEqual(command[command.index("--cpus") + 1], "0.5")
            self.assertEqual(command[command.index("--pids-limit") + 1], "32")
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertIn("--read-only", command)
            self.assertFalse(set(command) & {"-v", "--volume", "--mount", "--env", "--env-file", "--privileged", "--publish", "-p"})

    def test_runtime_proof_accepts_only_source_bound_linux_elf_with_isolated_expected_failure(self):
        value = runtime_proof()
        original = copy.deepcopy(value)
        self.assertIs(ENV.validate_runtime_probe(value, BINDING), value)
        value["elf_machine"] = 183
        ENV.validate_runtime_probe(value, BINDING)
        value["elf_machine"] = original["elf_machine"]
        self.assertEqual(value, original)

    def test_runtime_proof_rejects_wrong_user_binary_arch_compiler_cgo_and_source(self):
        for field, value in (("uid", 0), ("uid", "1001"), ("elf_magic", "4d5a"), ("elf_machine", 3),
                             ("binary_sha256", "0" * 63), ("compiler", "go1.25.14"), ("cgo_enabled", "1"),
                             ("builder_binary_sha256", "0" * 64), ("builder_binary_sha256", None),
                             ("build_source", {**BINDING, "release_nonce": "66666666-7777-4888-8999-000000000000"})):
            candidate = {**runtime_proof(), field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ENV.EnvReleaseError, "BINARY_BUILD_PROOF_REQUIRED"):
                ENV.validate_runtime_probe(candidate, BINDING)

    def test_runtime_proof_requires_complete_nonproduction_nontimedout_startup_evidence(self):
        for field, values in (("exit_code", (0, 2, None)), ("redis_failure_observed", (False, 1, None)),
                              ("production_io", (True, 0, None)), ("timed_out", (True, 0, None)),
                              ("health_success_claimed", (True, 0, None))):
            for value in values:
                candidate = runtime_proof()
                candidate["startup"][field] = value
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ENV.EnvReleaseError, "ISOLATED_STARTUP_PROOF_REQUIRED"):
                    ENV.validate_runtime_probe(candidate, BINDING)
        candidate = runtime_proof()
        del candidate["startup"]
        with self.assertRaisesRegex(ENV.EnvReleaseError, "ISOLATED_STARTUP_PROOF_REQUIRED"):
            ENV.validate_runtime_probe(candidate, BINDING)

    def test_serial_executor_runs_once_in_exact_order_and_returns_completed_phases(self):
        commands = [(phase, ["owned-fake", phase]) for phase in ENV.PHASES]
        observed = []
        completed = ENV.execute_serial(commands, lambda command: observed.append(command) or 0)
        self.assertEqual(completed, list(ENV.PHASES))
        self.assertEqual(observed, [command for _, command in commands])

    def test_serial_executor_stops_on_each_first_failure_without_replay_cleanup_or_restore(self):
        commands = [(phase, ["owned-fake", phase]) for phase in ENV.PHASES]
        for failed_index, failed_phase in enumerate(ENV.PHASES):
            observed = []
            def execute(command):
                observed.append(command)
                return 2 if command[-1] == failed_phase else 0
            with self.subTest(phase=failed_phase), self.assertRaisesRegex(ENV.EnvReleaseError, "SERIAL_STEP_FAILED_" + failed_phase.upper()):
                ENV.execute_serial(commands, execute)
            self.assertEqual(observed, [command for _, command in commands[:failed_index + 1]])


class EnvIntentBackupAndPlanTests(unittest.TestCase):
    def test_intent_binds_action_source_commit_nonce_and_parent_before_mutation(self):
        for action in (*ENV.PHASES, "resume-original"):
            value = ENV.validate_intent(None, action, BINDING, PLAN_SHA)
            self.assertEqual(value, {"action": action, "binding": BINDING, "parent_sha256": PLAN_SHA})

    def test_existing_or_uncertain_intent_requires_status_and_never_replay(self):
        for existing in ({}, {"status": "pending"}, {"status": "passed"}, {"status": "uncertain"}, False):
            for action in ENV.PHASES:
                with self.subTest(existing=existing, action=action), self.assertRaisesRegex(ENV.EnvReleaseError, "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY"):
                    ENV.validate_intent(existing, action, BINDING, PLAN_SHA)

    def test_intent_rejects_unsupported_action_and_missing_parent_hash(self):
        for action, parent in (("down", PLAN_SHA), ("migration", PLAN_SHA), ("restore", PLAN_SHA),
                               ("activate", None), ("activate", "c" * 63), ("activate", "C" * 64)):
            with self.subTest(action=action, parent=parent), self.assertRaisesRegex(ENV.EnvReleaseError, "INTENT_SCOPE"):
                ENV.validate_intent(None, action, BINDING, parent)

    def test_each_phase_requires_all_prior_receipts_in_exact_serial_order(self):
        self.assertEqual(ENV.PHASES, ("build", "image-backup", "fence-backup", "activate", "verify"))
        for index, action in enumerate(ENV.PHASES):
            self.assertEqual(ENV.phase_dependencies(action), ENV.PHASES[:index])
        for action in ("down", "restore", "inspect", "status", "resume-original"):
            with self.subTest(action=action), self.assertRaisesRegex(ENV.EnvReleaseError, "PHASE_INVALID"):
                ENV.phase_dependencies(action)

    def test_backup_binds_two_artifacts_all_components_and_exact_stopped_env_only_fence(self):
        value = backup_proof()
        original = copy.deepcopy(value)
        self.assertIs(ENV.validate_backup(value, BINDING, PLAN_SHA), value)
        self.assertEqual(value, original)

    def test_backup_rejects_wrong_kind_plan_commit_fingerprint_or_nonce(self):
        changes = [("kind", "renewal-backup"), ("plan_sha256", "9" * 64)]
        changes.extend(("binding", {**BINDING, field: bad}) for field, bad in (
            ("commit", "9" * 40), ("source_fingerprint", "9" * 64),
            ("release_nonce", "66666666-7777-4888-8999-000000000000")))
        for field, value in changes:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ENV.EnvReleaseError, "BACKUP_BINDING"):
                ENV.validate_backup({**backup_proof(), field: value}, BINDING, PLAN_SHA)

    def test_backup_rejects_each_missing_component_unknown_component_live_env_and_other_control(self):
        for missing in ENV.COMPONENTS:
            value = backup_proof()
            value["components"].remove(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(ENV.EnvReleaseError, "BACKUP_FENCE_INCOMPLETE"):
                ENV.validate_backup(value, BINDING, PLAN_SHA)
        for field, value in (("components", sorted(ENV.COMPONENTS) + ["postgres"]), ("env_stopped", False),
                             ("env_stopped", 1), ("other_db2_clients", 1), ("other_services_controlled", 1),
                             ("redis_database", 0), ("automatic_restore", True), ("automatic_restore", 0)):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ENV.EnvReleaseError, "BACKUP_FENCE_INCOMPLETE"):
                ENV.validate_backup({**backup_proof(), field: value}, BINDING, PLAN_SHA)

    def test_backup_requires_actual_positive_size_digest_for_both_image_and_state_artifacts(self):
        for count in (0, 1, 3):
            value = backup_proof()
            value["files"] = [copy.deepcopy(value["files"][0]) for _ in range(count)]
            with self.subTest(count=count), self.assertRaisesRegex(ENV.EnvReleaseError, "BACKUP_ARTIFACT_MISSING"):
                ENV.validate_backup(value, BINDING, PLAN_SHA)
        for index in (0, 1):
            for field, bad in (("bytes", 0), ("bytes", -1), ("bytes", True), ("bytes", "128"),
                               ("sha256", ""), ("sha256", "A" * 64), ("sha256", "0" * 63)):
                value = backup_proof()
                value["files"][index][field] = bad
                with self.subTest(index=index, field=field, bad=bad), self.assertRaisesRegex(ENV.EnvReleaseError, "BACKUP_ARTIFACT_MISSING"):
                    ENV.validate_backup(value, BINDING, PLAN_SHA)

    def test_plan_checks_current_source_tool_hash_scope_and_single_consumer_without_network(self):
        value = plan()
        manifest = manifest_for(context_payloads())
        value["binding"]["source_fingerprint"] = manifest["fingerprint"]
        value["managed_files"] = manifest["files"]
        value["modes"] = {row["path"]: 0o644 for row in manifest["files"]}
        with patch.object(ENV, "tools_binding", return_value=value["tool_sources"]), \
             patch.object(ENV.ops, "manifest", return_value=manifest), \
             patch.object(ENV, "remote", side_effect=AssertionError("network forbidden")):
            self.assertIs(ENV.validate_plan(value), value)
            for field, bad, error in (("kind", "renewal-plan", "PLAN_SCHEMA"),
                                       ("schema_version", 2, "PLAN_SCHEMA"),
                                       ("scope", {**ENV.SCOPE, "services": ["frontend"]}, "ENV_ONLY_SCOPE_REQUIRED"),
                                       ("tool_sources", {"fixture": "9" * 64}, "RELEASE_TOOLS_CHANGED"),
                                       ("managed_files", [source_row("services/learn/app/main.py")], "PLAN_SOURCE_SCOPE"),
                                       ("managed_files", [], "PLAN_SOURCE_SCOPE")):
                with self.subTest(field=field, error=error), self.assertRaisesRegex(ENV.EnvReleaseError, error):
                    ENV.validate_plan({**value, field: bad})

    def test_plan_rejects_expired_future_and_current_source_drift_but_recovery_is_read_only(self):
        value = plan()
        with patch.object(ENV, "tools_binding", return_value=value["tool_sources"]), \
             patch.object(ENV.ops, "manifest", return_value={"fingerprint": BINDING["source_fingerprint"]}) as manifest:
            for offset in (timedelta(minutes=-31), timedelta(minutes=1)):
                altered = {**value, "created_at": (datetime.now(timezone.utc) + offset).isoformat()}
                with self.subTest(offset=offset), self.assertRaisesRegex(ENV.EnvReleaseError, "PLAN_EXPIRED"):
                    ENV.validate_plan(altered)
            manifest.return_value = {"fingerprint": "0" * 64}
            with self.assertRaisesRegex(ENV.EnvReleaseError, "PLAN_SOURCE_CHANGED"):
                ENV.validate_plan(value)
            manifest.reset_mock()
            self.assertIs(ENV.validate_plan(value, current=False, age=False), value)
            manifest.assert_not_called()

    def test_inspect_without_execute_never_reaches_network_or_writes_receipt(self):
        output = io.StringIO()
        with patch.object(ENV, "remote", side_effect=AssertionError("network forbidden")), \
             patch.object(ENV, "receipt", side_effect=AssertionError("write forbidden")), redirect_stdout(output):
            self.assertEqual(ENV.main(["inspect"]), 0)
        self.assertIs(json.loads(output.getvalue())["network"], False)

    def test_mutating_actions_without_execute_are_source_bound_local_plans_only(self):
        value = plan()
        for action in (*ENV.PHASES, "status", "resume-original", "fence-current"):
            output = io.StringIO()
            with self.subTest(action=action), \
                 patch.object(ENV, "load_json", return_value=(value, PLAN_SHA)), \
                 patch.object(ENV, "validate_plan", return_value=value), \
                 patch.object(ENV, "remote", side_effect=AssertionError("network forbidden")), \
                 patch.object(ENV, "receipt", side_effect=AssertionError("write forbidden")), \
                 patch.object(ENV.ops, "streaming_ssh", side_effect=AssertionError("stream forbidden")), redirect_stdout(output):
                self.assertEqual(ENV.main([action, "--plan", "owned-fixture-plan.json"]), 0)
            observed = json.loads(output.getvalue())
            self.assertEqual(observed["binding"], BINDING)
            self.assertEqual(observed["scope"], ENV.SCOPE)
            self.assertIs(observed["network"], False)
            self.assertIs(observed["automatic_restore"], False)


class EnvPrivateArtifactTests(unittest.TestCase):
    def test_actual_private_artifact_validator_rehashes_both_owned_files_without_modifying_them(self):
        with private_artifacts_fixture() as (artifacts, files, _paths):
            original = copy.deepcopy((artifacts, files))
            ENV.validate_private_files(artifacts)
            self.assertEqual((artifacts, files), original)

    def test_private_artifact_missing_size_hash_duplicate_and_link_fail_closed(self):
        for failure in ("missing", "size", "hash", "duplicate", "link"):
            with self.subTest(failure=failure), private_artifacts_fixture() as (artifacts, files, _paths):
                if failure == "missing":
                    del files[artifacts[0]["path"]]
                elif failure == "size":
                    artifacts[0]["bytes"] += 1
                elif failure == "hash":
                    artifacts[0]["sha256"] = "0" * 64
                elif failure == "duplicate":
                    artifacts[1]["path"] = artifacts[0]["path"]
                with patch.object(ENV.ops, "linked", return_value=failure == "link"), self.assertRaises(ENV.EnvReleaseError):
                    ENV.validate_private_files(artifacts)

    def test_image_backup_receipt_requires_current_plan_source_nonce_and_original_running_image(self):
        value = plan()
        with private_artifacts_fixture() as (artifacts, _files, _paths):
            captured = {"status": "passed", "binding": copy.deepcopy(BINDING), "plan_sha256": PLAN_SHA,
                        "image_id": OLD_IMAGE, "files": artifacts[:1]}
            with patch.object(ENV, "load_json", return_value=(captured, "8" * 64)):
                self.assertIs(ENV.image_backup_receipt(value, PLAN_SHA), captured)
            for field, bad in (("status", "uncertain"), ("binding", {**BINDING, "source_fingerprint": "0" * 64}),
                               ("binding", {**BINDING, "release_nonce": "66666666-7777-4888-8999-000000000000"}),
                               ("plan_sha256", "0" * 64), ("image_id", NEW_IMAGE), ("files", artifacts)):
                altered = {**captured, field: bad}
                with self.subTest(field=field, bad=bad), patch.object(ENV, "load_json", return_value=(altered, "8" * 64)), \
                     self.assertRaisesRegex(ENV.EnvReleaseError, "IMAGE_BACKUP_RECEIPT_UNBOUND"):
                    ENV.image_backup_receipt(value, PLAN_SHA)

    def test_external_backup_stream_failure_returns_uncertain_without_complete_retry_resume_or_restore(self):
        value = plan()
        for action in ("image-backup", "fence-backup"):
            output = io.StringIO()
            with self.subTest(action=action), \
                 patch.object(ENV, "load_json", return_value=(value, PLAN_SHA)), \
                 patch.object(ENV, "validate_plan", return_value=value), \
                 patch.object(ENV, "image_backup_receipt", return_value={"files": []}), \
                 patch.object(ENV, "private_destination", return_value=Path("owned-private-artifact.tar")), \
                 patch.object(ENV, "remote", return_value={"status": "passed", "image_id": OLD_IMAGE,
                    "image_metadata": {"fixture": True}, "private_archive": "/owned-release/private-backup.tar"}) as remote, \
                 patch.object(ENV.ops, "streaming_ssh", side_effect=TimeoutError("owned stream lost")) as stream, \
                 patch.object(ENV, "receipt", side_effect=AssertionError("no false complete receipt")), redirect_stdout(output):
                self.assertEqual(ENV.main([action, "--plan", "owned-fixture-plan.json", "--acl-attestation", "owned-acl.json", "--execute"]), 2, output.getvalue())
                remote.assert_called_once()
                self.assertEqual(remote.call_args.args[0], action)
                stream.assert_called_once()
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(result["next"], "status; do not replay or restore")
            self.assertIs(result["automatic_restore"], False)


class EnvLinuxTestExecutionTests(unittest.TestCase):
    def fixture(self):
        namespace, directory, _value, files, _events = memory_remote()
        files["/proc/meminfo"] = b"MemAvailable: 4194304 kB\n"
        namespace["secrets"] = SimpleNamespace(token_urlsafe=Mock(return_value="owned-synthetic-memory-fixture"))
        nonce = BINDING["release_nonce"]
        redis_id, test_id = "7" * 64, "6" * 64
        namespace["_redis_info"] = b"process_id:321\r\nrun_id:" + b"a" * 40 + b"\r\n"
        namespace["_test_logs"] = b"\n".join(ENV.canonical(row) for row in (
            {"Action": "pass", "Test": "TestOwnedIntegration", "Package": "fixture/container"},
            {"Action": "pass", "Test": "TestUnitContract", "Package": "fixture/container"}))
        test = {"Id": test_id, "Image": OLD_IMAGE, "Config": {"Labels": {"io.ctf-web.env-test-nonce": nonce}},
                "State": {"Running": False, "ExitCode": 0, "Pid": 0, "Status": "exited"}}
        redis = {"Id": redis_id, "Image": NEW_IMAGE, "Config": {"Labels": {"io.ctf-web.env-test-nonce": nonce}},
                 "State": {"Running": True, "Pid": 321, "Status": "running"}}
        def run(command, **_kwargs):
            if command[:3] == ["docker", "run", "-d"]:
                name = command[command.index("--name") + 1]
                return (redis_id if name == "ctf-env-test-redis-" + nonce else test_id).encode()
            if command[:3] == ["docker", "exec", redis_id]:
                self.assertEqual(command[3:], ["redis-cli", "--raw", "INFO", "server"])
                return namespace["_redis_info"]
            self.assertEqual(command, ["docker", "logs", test_id])
            return namespace["_test_logs"]
        namespace["run"] = Mock(side_effect=run)
        namespace["details"] = Mock(side_effect=lambda identifier: test if identifier == test_id else redis)
        namespace["bounded_command"] = Mock()
        namespace["wait_stopped"] = Mock(side_effect=lambda identifier, image, **_kwargs:
            {"Id": identifier, "Image": image, "State": {"Running": False, "Pid": 0, "Status": "exited"}})
        return namespace, directory, files, test, redis

    def test_actual_linux_tests_accept_exact_four_gib_gate_and_keep_owned_execution_budgets(self):
        namespace, directory, files, test, redis = self.fixture()
        nonce = BINDING["release_nonce"]
        proof = namespace["linux_tests"](directory, OLD_IMAGE, NEW_IMAGE, nonce)
        starts = [call.args[0] for call in namespace["run"].call_args_list if call.args[0][:3] == ["docker", "run", "-d"]]
        self.assertEqual(len(starts), 2)
        redis_command, go_command = starts
        for command in starts:
            self.assertEqual(command[command.index("--label") + 1], "io.ctf-web.env-test-nonce=" + nonce)
            self.assertEqual(command[command.index("--user") + 1], "1001:1001")
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertEqual(command[command.index("--security-opt") + 1], "no-new-privileges")
            self.assertEqual(command[command.index("--env-file") + 1], str(directory / "test.env"))
            self.assertIn("--read-only", command)
            self.assertFalse(set(command) & {"--mount", "--volume", "-v", "--privileged", "--publish", "-p"})
            self.assertNotIn("docker.sock", " ".join(command))
        self.assertEqual(redis_command[redis_command.index("--network") + 1], "none")
        self.assertEqual(redis_command[redis_command.index("--memory") + 1], "64m")
        self.assertEqual(redis_command[redis_command.index("--cpus") + 1], "0.25")
        self.assertEqual(redis_command[redis_command.index("--pids-limit") + 1], "64")
        self.assertEqual(redis_command[redis_command.index("--tmpfs") + 1], "/data:uid=1001,gid=1001,mode=0700,size=16m")
        self.assertEqual(go_command[go_command.index("--network") + 1], "container:" + redis["Id"])
        self.assertEqual(go_command[go_command.index("--memory") + 1], "3g")
        self.assertEqual(go_command[go_command.index("--memory-swap") + 1], "3g")
        self.assertEqual(go_command[go_command.index("--cpus") + 1], "1")
        self.assertEqual(go_command[go_command.index("--pids-limit") + 1], "192")
        self.assertEqual(go_command[go_command.index("--tmpfs") + 1], "/tmp:exec,uid=1001,gid=1001,mode=0700,size=2g")
        self.assertEqual(go_command[-8:], [OLD_IMAGE, "test", "-json", "-mod=readonly", "-count=1", "-p=1", "-timeout=180s", "./..."])
        settings = dict(line.split("=", 1) for line in files[str(directory / "test.env")].decode().splitlines())
        self.assertEqual(settings["GOMEMLIMIT"], "512MiB")
        self.assertEqual(settings["GOMAXPROCS"], "1")
        self.assertEqual(settings["CTF_TEST_REDIS_INTEGRATION"], "owned")
        self.assertEqual(settings["CTF_TEST_REDIS_DB"], "15")
        self.assertEqual(settings["CTF_TEST_REDIS_NONCE"], nonce)
        self.assertEqual(settings["CTF_TEST_REDIS_PROCESS_ID"], "321")
        self.assertEqual(settings["CTF_TEST_REDIS_RUN_ID"], "a" * 40)
        self.assertEqual(settings["GOPROXY"], "off")
        namespace["secrets"].token_urlsafe.assert_called_once_with(32)
        self.assertEqual(proof["passed_tests"], 2)
        self.assertEqual(proof["skipped"], 0)
        self.assertIs(proof["host_or_production_network_used"], False)
        self.assertIs(proof["source_read_only"], True)
        self.assertIs(proof["redis_persistence"], False)
        self.assertEqual([call.args[0] for call in namespace["bounded_command"].call_args_list], [
            ["docker", "rm", test["Id"]], ["docker", "kill", "--signal", "SIGTERM", redis["Id"]], ["docker", "rm", redis["Id"]]])
        namespace["wait_stopped"].assert_called_once_with(redis["Id"], NEW_IMAGE, seconds=15)
        namespace["socket"].create_connection.assert_not_called()

    def test_linux_test_resource_shortage_precedes_secret_envfile_container_or_command_creation(self):
        namespace, directory, files, _test, _redis = self.fixture()
        files["/proc/meminfo"] = b"MemAvailable: 4194303 kB\n"
        original = dict(files)
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "LINUX_GO_TEST_RESOURCE_GATE"):
            namespace["linux_tests"](directory, OLD_IMAGE, NEW_IMAGE, BINDING["release_nonce"])
        self.assertEqual(files, original)
        namespace["secrets"].token_urlsafe.assert_not_called()
        namespace["run"].assert_not_called()
        namespace["bounded_command"].assert_not_called()
        namespace["details"].assert_not_called()

    def test_linux_tests_reject_skips_failures_missing_integration_zero_terminals_or_failed_exit_and_cleanup_owned_only(self):
        for scenario in ("skip", "fail", "missing-integration", "zero-terminal", "failed-exit", "partial-linker-no-space"):
            namespace, directory, files, test, redis = self.fixture()
            if scenario in {"skip", "fail"}:
                namespace["_test_logs"] = ENV.canonical({"Action": scenario, "Test": "TestOwnedIntegration"})
            elif scenario == "missing-integration":
                namespace["_test_logs"] = ENV.canonical({"Action": "pass", "Test": "TestUnitContract"})
            elif scenario == "zero-terminal":
                namespace["_test_logs"] = b"compiler: signal: killed\n"
            elif scenario == "partial-linker-no-space":
                rows = [{"Action": "pass", "Test": "TestUnit" + str(index), "Package": "fixture/auth"} for index in range(5)]
                rows.extend(({"Action": "output", "Package": "fixture/container", "Output": "linker: no space left on device\n"},
                             {"Action": "fail", "Package": "fixture/container"}))
                namespace["_test_logs"] = b"\n".join(ENV.canonical(row) for row in rows)
                test["State"]["ExitCode"] = 1
                terminals = [row for row in rows if row.get("Test") and row["Action"] in {"pass", "fail", "skip"}]
                self.assertEqual({action: sum(row["Action"] == action for row in terminals) for action in ("pass", "fail", "skip")},
                                 {"pass": 5, "fail": 0, "skip": 0})
                self.assertFalse(any("Integration" in row["Test"] for row in terminals))
            else:
                test["State"]["ExitCode"] = 1
            with self.subTest(scenario=scenario), self.assertRaisesRegex(namespace["EnvReleaseError"], "LINUX_GO_TEST_ACCEPTANCE"):
                namespace["linux_tests"](directory, OLD_IMAGE, NEW_IMAGE, BINDING["release_nonce"])
            self.assertEqual(files[str(directory / "linux-tests.jsonl")], namespace["_test_logs"])
            self.assertEqual([call.args[0] for call in namespace["bounded_command"].call_args_list], [
                ["docker", "rm", test["Id"]], ["docker", "kill", "--signal", "SIGTERM", redis["Id"]], ["docker", "rm", redis["Id"]]])

    def test_linux_tests_invalid_redis_process_or_run_identity_blocks_go_creation_and_cleans_only_owned_redis(self):
        for info in (b"process_id:invalid\nrun_id:" + b"a" * 40, b"process_id:321\nrun_id:short", b"process_id:321\nrun_id:" + b"A" * 40):
            namespace, directory, files, _test, redis = self.fixture()
            namespace["_redis_info"] = info
            with self.subTest(info=info[:40]), self.assertRaisesRegex(namespace["EnvReleaseError"], "OWNED_REDIS_INFO"):
                namespace["linux_tests"](directory, OLD_IMAGE, NEW_IMAGE, BINDING["release_nonce"])
            starts = [call.args[0] for call in namespace["run"].call_args_list if call.args[0][:3] == ["docker", "run", "-d"]]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0][starts[0].index("--name") + 1], "ctf-env-test-redis-" + BINDING["release_nonce"])
            self.assertNotIn(str(directory / "linux-tests.jsonl"), files)
            self.assertEqual([call.args[0] for call in namespace["bounded_command"].call_args_list], [
                ["docker", "kill", "--signal", "SIGTERM", redis["Id"]], ["docker", "rm", redis["Id"]]])

    def test_linux_tests_overall_timeout_remains_three_hundred_seconds_and_uses_no_force_kill(self):
        namespace, directory, _files, test, redis = self.fixture()
        test["State"].update(Running=True, Pid=1001, Status="running")
        namespace["time"].monotonic.side_effect = [1000, 1299, 1301]
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "OWNED_LINUX_TEST_TIMEOUT"):
            namespace["linux_tests"](directory, OLD_IMAGE, NEW_IMAGE, BINDING["release_nonce"])
        namespace["time"].sleep.assert_called_once_with(1)
        commands = [call.args[0] for call in namespace["bounded_command"].call_args_list]
        self.assertEqual(commands, [["docker", "kill", "--signal", "SIGTERM", test["Id"]], ["docker", "rm", test["Id"]],
                                    ["docker", "kill", "--signal", "SIGTERM", redis["Id"]], ["docker", "rm", redis["Id"]]])
        self.assertTrue(all("SIGKILL" not in command for command in commands))


class EnvRemoteExecutionTests(unittest.TestCase):
    def test_actual_go_source_evidence_runs_two_serial_offline_readonly_lists_with_private_digest_logs(self):
        namespace, directory, _value, files, _events = memory_remote()
        source_evidence_processes(namespace)
        result = namespace["go_source_evidence"](directory, OLD_IMAGE, BINDING["release_nonce"])
        calls = namespace["subprocess"].run.call_args_list
        self.assertEqual(len(calls), 2)
        for kind, call in zip(("modules", "imports"), calls):
            command = call.args[0]
            self.assertEqual(command[:3], ["docker", "run", "--rm"])
            self.assertEqual(command[command.index("--name") + 1], "ctf-env-go-" + kind + "-" + BINDING["release_nonce"])
            self.assertEqual(command[command.index("--label") + 1], "io.ctf-web.env-test-nonce=" + BINDING["release_nonce"])
            for flag, expected in (("--network", "none"), ("--user", "1001:1001"), ("--memory", "1g"),
                                   ("--cpus", "1"), ("--pids-limit", "192"), ("--cap-drop", "ALL")):
                self.assertEqual(command[command.index(flag) + 1], expected)
            self.assertIn("--read-only", command)
            self.assertEqual(command[command.index("--tmpfs") + 1], "/tmp:uid=1001,gid=1001,mode=0700,size=512m")
            settings = [command[index + 1] for index, token in enumerate(command) if token == "--env"]
            self.assertTrue({"GOTOOLCHAIN=local", "GOTELEMETRY=off", "GOENV=off", "GOPROXY=off", "HOME=/tmp"} <= set(settings))
            self.assertFalse(set(command) & {"--mount", "--volume", "-v", "--env-file", "--privileged", "--publish", "-p"})
            self.assertEqual(call.kwargs["timeout"], 180)
            self.assertEqual(call.kwargs["stderr"], namespace["subprocess"].STDOUT)
            self.assertEqual(call.kwargs["stdout"].name, str(directory / ("go-" + kind + ".log")))
            record = result["logs"][kind]
            self.assertEqual(record["sha256"], ENV.digest(namespace["_go_logs"][kind]))
            self.assertEqual(record["bytes"], len(namespace["_go_logs"][kind]))
            self.assertEqual(files[record["private_log"]], namespace["_go_logs"][kind])
            self.assertIn((str(directory / ("go-" + kind + ".log")), 0o600),
                          [(str(call.args[0]), call.args[1]) for call in namespace["os"].chmod.call_args_list])
        self.assertEqual(calls[0].args[0][-6:], [OLD_IMAGE, "list", "-mod=readonly", "-m", "-json", "all"])
        self.assertEqual(calls[1].args[0][-7:], [OLD_IMAGE, "list", "-mod=readonly", "-deps", "-f", "{{.ImportPath}}", "./cmd/main.go"])
        self.assertEqual(result["builder_image_id"], OLD_IMAGE)
        self.assertIs(result["offline"], True)
        self.assertIs(result["source_read_only"], True)
        self.assertEqual(result["logs"]["imports"]["package_count"], 3)
        self.assertEqual([call.args[1] for call in namespace["os"].chmod.call_args_list], [0o600, 0o600])
        namespace["run"].assert_not_called()
        namespace["socket"].create_connection.assert_not_called()

    def test_actual_go_source_evidence_rejects_malformed_wrong_mvs_or_empty_modules_before_imports(self):
        changed = module_graph_fixture()
        changed[1]["Version"] = "v9.6.0"
        for payload in (b"", b"{not-json}", b"[{}]", b"\n".join(json.dumps(row).encode() for row in changed)):
            namespace, directory, _value, files, _events = memory_remote()
            source_evidence_processes(namespace)
            namespace["_go_logs"]["modules"] = payload
            with self.subTest(payload=payload[:40]), self.assertRaises((namespace["EnvReleaseError"], json.JSONDecodeError)):
                namespace["go_source_evidence"](directory, OLD_IMAGE, BINDING["release_nonce"])
            namespace["subprocess"].run.assert_called_once()
            self.assertNotIn(str(directory / "go-imports.log"), files)

    def test_actual_go_source_evidence_rejects_invalid_imports_without_success_proof(self):
        for payload in (b"", b"package name with spaces\n", b"fmt\n\n", b"fmt\n<script>\n"):
            namespace, directory, _value, _files, _events = memory_remote()
            source_evidence_processes(namespace)
            namespace["_go_logs"]["imports"] = payload
            with self.subTest(payload=payload), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["go_source_evidence"](directory, OLD_IMAGE, BINDING["release_nonce"])
            self.assertEqual(namespace["subprocess"].run.call_count, 2)

    def build_fixture(self):
        namespace, directory, value, files, events = memory_remote()
        files["/proc/meminfo"] = b"MemAvailable: 3145728 kB\n"
        current = snapshot()
        current["containers"].append({"service": "redis", "container_id": "5" * 64,
            "image_id": "sha256:" + "7" * 64, "running": True, "health": "healthy"})
        value["inspection"] = copy.deepcopy(current)
        namespace["snapshot"] = Mock(return_value=current)
        namespace["verify_retained"] = Mock()
        namespace["os"].statvfs = Mock(return_value=SimpleNamespace(f_bavail=12 * 1024 ** 3 // 4096, f_frsize=4096))
        namespace["extract_source"] = Mock(side_effect=lambda *_args: events.append(("extract",)) or directory / "source")
        source_evidence_processes(namespace)
        actual_bounded = namespace["bounded_command"]
        def bounded(command, *args, **kwargs):
            if command[:2] == ["docker", "build"]:
                events.append(("build", command))
                return None
            events.append(("base-pull", command[2]) if command[:2] == ["docker", "pull"] else ("dependency", args[0].name))
            return actual_bounded(command, *args, **kwargs)
        namespace["bounded_command"] = Mock(side_effect=bounded)
        def run(command):
            self.assertEqual(command[:3], ["docker", "image", "inspect"])
            subject = command[3]
            if subject in (ENV.COMPILER_IMAGE, ENV.RUNTIME_IMAGE):
                return json.dumps([{"RepoDigests": [subject.split(":")[0] + "@sha256:" + "9" * 64]}]).encode()
            return json.dumps([{"Id": "sha256:" + "6" * 64 if subject.endswith("-tests") else NEW_IMAGE}]).encode()
        namespace["run"] = Mock(side_effect=run)
        namespace["image_source"] = Mock()
        namespace["linux_tests"] = Mock(side_effect=lambda *_args: events.append(("linux-tests",)) or {"passed_tests": 4})
        namespace["binary_probe"] = Mock(side_effect=lambda *_args: events.append(("binary-probe",)) or runtime_proof())
        return namespace, directory, value, files, events

    def test_real_build_phase_runs_runtime_then_builder_then_linux_tests_then_binary_probe_without_activation(self):
        namespace, directory, value, _, events = self.build_fixture()
        result = namespace["build_phase"](directory, value)
        self.assertEqual(result["image_id"], NEW_IMAGE)
        self.assertEqual(result["builder_image_id"], "sha256:" + "6" * 64)
        self.assertIs(result["owned_test_containers_closed"], True)
        semantic = [event[0] for event in events if event[0] != "write"]
        self.assertEqual(semantic, ["base-pull", "base-pull", "extract", "build", "build", "dependency", "dependency", "linux-tests", "binary-probe"])
        builds = [event[1] for event in events if event[0] == "build"]
        self.assertNotIn("--target", builds[0])
        self.assertEqual(builds[1][builds[1].index("--target") + 1], "builder")
        self.assertFalse(set(token for command in builds for token in command) & {"compose", "up", "kill", "stop", "restart", "prune", "SIGKILL"})
        namespace["linux_tests"].assert_called_once_with(directory, "sha256:" + "6" * 64, "sha256:" + "7" * 64, BINDING["release_nonce"])
        namespace["binary_probe"].assert_called_once_with(directory, NEW_IMAGE, "sha256:" + "6" * 64, BINDING)
        self.assertEqual(result["dependency_evidence"]["module_graph"]["selected_modules"], {row["Path"]: row["Version"] for row in module_graph_fixture()[1:]})

    def test_real_build_phase_existing_tag_or_first_failure_blocks_later_build_test_and_probe(self):
        for failure in ("existing-tag", "runtime-build"):
            namespace, directory, value, _, _ = self.build_fixture()
            if failure == "existing-tag":
                actual_child = namespace["subprocess"].run.side_effect
                namespace["subprocess"].run.side_effect = lambda command, **kwargs: SimpleNamespace(returncode=0) if command[:3] == ["docker", "image", "inspect"] else actual_child(command, **kwargs)
            else:
                actual_bounded = namespace["bounded_command"].side_effect
                def fail_app_build(command, *args, **kwargs):
                    if command[:2] == ["docker", "build"]:
                        raise namespace["EnvReleaseError"]("COMMAND_FAILED_PRIVATE_LOG_RETAINED")
                    return actual_bounded(command, *args, **kwargs)
                namespace["bounded_command"].side_effect = fail_app_build
            with self.subTest(failure=failure), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["build_phase"](directory, value)
            app_builds = [call for call in namespace["bounded_command"].call_args_list if call.args[0][:2] == ["docker", "build"]]
            self.assertLessEqual(len(app_builds), 1)
            namespace["linux_tests"].assert_not_called()
            namespace["binary_probe"].assert_not_called()

    def test_real_build_phase_resource_gate_precedes_extract_build_or_owned_test_creation(self):
        for failure in ("disk", "memory"):
            namespace, directory, value, files, _ = self.build_fixture()
            if failure == "disk":
                namespace["os"].statvfs.return_value.f_bavail -= 1
            else:
                files["/proc/meminfo"] = b"MemAvailable: 3145727 kB\n"
            with self.subTest(failure=failure), self.assertRaisesRegex(namespace["EnvReleaseError"], "ENV_BUILD_RESOURCE_GATE"):
                namespace["build_phase"](directory, value)
            namespace["extract_source"].assert_not_called()
            namespace["bounded_command"].assert_not_called()
            namespace["subprocess"].run.assert_not_called()
            namespace["linux_tests"].assert_not_called()
            namespace["binary_probe"].assert_not_called()

    def test_real_build_phase_pulls_only_two_official_bases_serially_before_extract_with_private_logs(self):
        namespace, directory, value, files, events = self.build_fixture()
        namespace["build_phase"](directory, value)
        pulls = [call for call in namespace["subprocess"].run.call_args_list if call.args[0][:2] == ["docker", "pull"]]
        self.assertEqual([call.args[0] for call in pulls], [
            ["docker", "pull", "golang:1.27.2-alpine3.24"],
            ["docker", "pull", "alpine:3.24.2"],
        ])
        for name, call in zip(("compiler", "runtime"), pulls):
            path = directory / ("pull-" + name + "-base.log")
            self.assertEqual(call.kwargs["stdout"].name, str(path))
            self.assertEqual(call.kwargs["timeout"], 600)
            self.assertEqual(call.kwargs["stderr"], namespace["subprocess"].STDOUT)
            self.assertEqual(files[str(path)], b"owned-official-base-pull-log\n")
            self.assertIn((str(path), 0o600), [(str(row.args[0]), row.args[1]) for row in namespace["os"].chmod.call_args_list])
            self.assertLess(events.index(("write", str(path))), events.index(("extract",)))
        semantic = [event for event in events if event[0] != "write"]
        self.assertEqual(semantic[:3], [("base-pull", "golang:1.27.2-alpine3.24"),
                                        ("base-pull", "alpine:3.24.2"), ("extract",)])

    def test_each_base_pull_failure_preserves_private_log_and_blocks_extract_all_app_builds_and_probes(self):
        for failed_name, failed_image, expected_pulls in (("compiler", "golang:1.27.2-alpine3.24", 1),
                                                         ("runtime", "alpine:3.24.2", 2)):
            namespace, directory, value, files, events = self.build_fixture()
            namespace["_pull_failure"] = failed_image
            with self.subTest(failed_name=failed_name), self.assertRaisesRegex(namespace["EnvReleaseError"], "COMMAND_FAILED_PRIVATE_LOG_RETAINED"):
                namespace["build_phase"](directory, value)
            self.assertEqual(namespace["subprocess"].run.call_count, expected_pulls)
            self.assertEqual(namespace["subprocess"].run.call_args.args[0], ["docker", "pull", failed_image])
            failed_log = directory / ("pull-" + failed_name + "-base.log")
            self.assertEqual(files[str(failed_log)], b"owned-official-base-pull-log\n")
            self.assertTrue(all(event[0] in {"base-pull", "write"} for event in events))
            namespace["extract_source"].assert_not_called()
            namespace["run"].assert_not_called()
            namespace["linux_tests"].assert_not_called()
            namespace["binary_probe"].assert_not_called()
            namespace["socket"].create_connection.assert_not_called()
            self.assertFalse(any(path.endswith(("go-modules.log", "go-imports.log")) for path in files))

    def test_snapshot_or_retained_gate_failure_prevents_every_base_pull_and_extract(self):
        for failure in ("snapshot", "retained"):
            namespace, directory, value, files, _events = self.build_fixture()
            if failure == "snapshot":
                namespace["snapshot"].return_value["protected_files"][0]["sha256"] = "0" * 64
            else:
                namespace["verify_retained"].side_effect = namespace["EnvReleaseError"]("RETAINED_IMAGE_PROVENANCE")
            with self.subTest(failure=failure), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["build_phase"](directory, value)
            namespace["bounded_command"].assert_not_called()
            namespace["subprocess"].run.assert_not_called()
            namespace["extract_source"].assert_not_called()
            self.assertFalse(any("pull-" in path for path in files))

    def test_real_build_phase_invalid_selected_graph_blocks_linux_tests_and_binary_probe(self):
        namespace, directory, value, _, _ = self.build_fixture()
        rows = module_graph_fixture()
        rows[1]["Version"] = "v9.6.0"
        namespace["_go_logs"]["modules"] = b"\n".join(json.dumps(row).encode() for row in rows)
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "MODULE_SELECTED_VERSION_CONTRACT"):
            namespace["build_phase"](directory, value)
        namespace["linux_tests"].assert_not_called()
        namespace["binary_probe"].assert_not_called()

    def binary_fixture(self):
        namespace, directory, value, files, events = memory_remote()
        binary = bytearray(32)
        binary[:4], binary[5], binary[18] = b"\x7fELF", 1, 62
        binary = bytes(binary)
        namespace["bounded_command"] = Mock(side_effect=lambda command: binary if command[-1] in {"/env-orchestrator", "/usr/local/bin/env-orchestrator"} else b"")
        namespace["run"].side_effect = None
        namespace["run"].return_value = GO_BUILD_INFO
        namespace["subprocess"].run.side_effect = None
        namespace["subprocess"].run.return_value = SimpleNamespace(returncode=1, stdout=b"redis URL parse failure", stderr=b"")
        namespace["details"] = Mock(return_value={"Id": "6" * 64, "Config": {"User": "1001:1001"},
            "HostConfig": {"NetworkMode": "none"}, "Mounts": [], "State": {"Running": False}})
        return namespace, directory, value, files, events, binary

    def test_binary_proof_requires_identical_builder_runtime_bytes_and_does_not_claim_health(self):
        namespace, directory, _value, _files, _events, binary = self.binary_fixture()
        result = namespace["binary_probe"](directory, NEW_IMAGE, OLD_IMAGE, BINDING)
        self.assertEqual(result["binary_sha256"], ENV.digest(binary))
        self.assertEqual(result["builder_binary_sha256"], ENV.digest(binary))
        self.assertIs(result["startup"]["health_success_claimed"], False)
        commands = [call.args[0] for call in namespace["bounded_command"].call_args_list]
        self.assertEqual(commands[0], ENV.runtime_probe_command(NEW_IMAGE, "ctf-env-probe-" + BINDING["release_nonce"]))
        self.assertEqual(commands[1], ENV.runtime_probe_command(OLD_IMAGE, "ctf-env-builder-probe-" + BINDING["release_nonce"]))
        self.assertEqual(commands[2], ["docker", "rm", "6" * 64])
        namespace["subprocess"].run.assert_called_once()

    def test_binary_mismatch_or_short_elf_blocks_buildinfo_and_startup(self):
        namespace, directory, _value, files, _events, binary = self.binary_fixture()
        namespace["bounded_command"].side_effect = [binary, binary + b"mismatch"]
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "BUILDER_RUNTIME_BINARY_MISMATCH"):
            namespace["binary_probe"](directory, NEW_IMAGE, OLD_IMAGE, BINDING)
        self.assertEqual(namespace["bounded_command"].call_count, 2)
        namespace["run"].assert_not_called()
        namespace["subprocess"].run.assert_not_called()
        self.assertFalse(files)
        for short in (b"", b"\x7fELF", binary[:19]):
            namespace, directory, _value, _files, _events, _binary = self.binary_fixture()
            namespace["bounded_command"].return_value = short
            namespace["bounded_command"].side_effect = None
            with self.subTest(length=len(short)), self.assertRaisesRegex(namespace["EnvReleaseError"], "ELF_BINARY_REQUIRED"):
                namespace["binary_probe"](directory, NEW_IMAGE, OLD_IMAGE, BINDING)
            namespace["bounded_command"].assert_called_once()
            namespace["run"].assert_not_called()
            namespace["subprocess"].run.assert_not_called()

    def test_binary_buildinfo_rejects_similar_compiler_cgo_or_target_prefix_before_startup(self):
        for old, new in ((b"go1.27.2", b"go1.27.20"), (b"CGO_ENABLED=0", b"CGO_ENABLED=00"),
                         (b"GOOS=linux", b"GOOS=linux2")):
            namespace, directory, _value, _files, _events, _binary = self.binary_fixture()
            namespace["run"].return_value = GO_BUILD_INFO.replace(old, new)
            with self.subTest(new=new), self.assertRaisesRegex(namespace["EnvReleaseError"], "BINARY_GO_BUILD_INFO"):
                namespace["binary_probe"](directory, NEW_IMAGE, OLD_IMAGE, BINDING)
            namespace["subprocess"].run.assert_not_called()

    def test_retained_verification_matches_existing_string_heads_to_observed_singleton_lists(self):
        namespace = remote_namespace()
        namespace["RETAINED_SERVICES"] = dict(ENV.verify_release.SERVICE_IMAGES)
        current, value = retained_fixture()
        original = copy.deepcopy((current, value))
        namespace["verify_retained"](current, value)
        self.assertEqual((current, value), original)
        namespace["run"].assert_not_called()

    def test_retained_verification_rejects_legacy_command_readiness_provenance_source_and_head_drift(self):
        namespace = remote_namespace()
        namespace["RETAINED_SERVICES"] = dict(ENV.verify_release.SERVICE_IMAGES)
        changes = (
            lambda current: current["code_files"][1].update(sha256="0" * 64),
            lambda current: current["protected_files"][0].update(sha256="0" * 64),
            lambda current: current["retained_labels"]["frontend"].update({"io.ctf-web.release-nonce": "wrong"}),
            lambda current: current["databases"]["learn"].update(head=["20261013_999"]),
            lambda current: current["databases"]["learn"].update(head=[]),
            lambda current: current["databases"]["learn"].update(head=["20261013_029", "20261013_030"]),
            lambda current: next(row for row in current["containers"] if row["service"] == "frontend").update(health="unhealthy"),
            lambda current: next(row for row in current["containers"] if row["service"] == "knowledge-candidate-runner").update(health="healthy"),
            lambda current: next(row for row in current["containers"] if row["service"] == "knowledge-candidate-runner").update(command=["python", "-m", "other.module"]),
            lambda current: next(row for row in current["containers"] if row["service"] == "learn-service").update(image_id=NEW_IMAGE),
        )
        for index, change in enumerate(changes):
            current, value = retained_fixture()
            change(current)
            with self.subTest(change=index), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["verify_retained"](current, value)
        namespace["run"].assert_not_called()

    def test_exact_generated_remote_payload_passes_retained_service_mapping_to_main(self):
        namespace = remote_namespace()
        observed = {}
        def inspect_snapshot():
            observed.update(namespace["RETAINED_SERVICES"])
            value = snapshot()
            existing = {row["service"] for row in value["containers"]}
            for index, service in enumerate(ENV.verify_release.SERVICE_IMAGES):
                if service not in existing:
                    value["containers"].append({"service": service, "container_id": format(index + 10, "064x"),
                        "image_id": "sha256:" + "f" * 64, "running": True,
                        "health": None if service in ENV.verify_release.LEGACY_PROCESS_WORKERS else "healthy"})
            return value
        namespace["snapshot"] = Mock(side_effect=inspect_snapshot)
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["main"]()
        self.assertEqual(observed, ENV.verify_release.SERVICE_IMAGES)
        self.assertEqual(namespace["REQUEST"]["retained_services"], ENV.verify_release.SERVICE_IMAGES)
        self.assertEqual(json.loads(output.getvalue())["status"], "passed")
        namespace["run"].assert_not_called()
        namespace["subprocess"].run.assert_not_called()
        namespace["socket"].create_connection.assert_not_called()

    def test_generated_payload_keeps_file_hash_and_payload_hash_contracts_distinct(self):
        namespace = remote_namespace()
        payload = b"pure-file-row-fixture"
        path = Mock(spec=Path)
        path.is_symlink.return_value = False
        path.stat.return_value = SimpleNamespace(st_mode=0o100644, st_size=len(payload))
        path.open.side_effect = lambda _mode: io.BytesIO(payload)
        self.assertEqual(namespace["file_row"](path, "fixture.bin"), source_row("fixture.bin", payload))
        self.assertEqual(namespace["payload_digest"](payload), ENV.digest(payload))
        namespace["run"].assert_not_called()

    def test_remote_scope_failure_precedes_snapshot_journal_and_every_command(self):
        namespace = remote_namespace(request={"scope": {**ENV.SCOPE, "stop_services": ["ctf-minio"]}})
        namespace["snapshot"] = Mock(side_effect=AssertionError("snapshot must not run"))
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["main"]()
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "uncertain")
        self.assertEqual(value["code"], "ENV_ONLY_SCOPE_REQUIRED")
        namespace["snapshot"].assert_not_called()
        namespace["run"].assert_not_called()

    def test_existing_remote_intent_records_partial_and_never_calls_phase_or_repeats(self):
        namespace, directory, value, files, _ = memory_remote("activate")
        files[str(directory / "activate.intent.json")] = b"{\"existing\":true}"
        namespace["release_directory"] = Mock(return_value=directory)
        namespace["activate"] = Mock(side_effect=AssertionError("activation must not run"))
        namespace["owned_test_status"] = Mock(return_value=[])
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["main"]()
        result = json.loads(output.getvalue())
        self.assertEqual(result["code"], "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(files[str(directory / "activate.intent.json")], b"{\"existing\":true}")
        self.assertIn(str(directory / "activate.partial.json"), files)
        self.assertNotIn(str(directory / "activate.complete.json"), files)
        namespace["activate"].assert_not_called()
        namespace["run"].assert_not_called()

    def test_remote_status_reads_owned_journal_without_intent_control_cleanup_or_restore(self):
        namespace, directory, value, files, _ = memory_remote("status")
        pending = {"action": "activate", "binding": value["binding"], "plan_sha256": PLAN_SHA}
        files[str(directory / "activate.intent.json")] = ENV.canonical(pending)
        namespace["release_directory"] = Mock(return_value=directory)
        namespace["snapshot"] = Mock(return_value=snapshot())
        namespace["owned_test_status"] = Mock(return_value=[{"container_id": "6" * 64, "running": True}])
        namespace["intent"] = Mock(side_effect=AssertionError("status must not write intent"))
        namespace["owned_remove"] = Mock(side_effect=AssertionError("status must not remove"))
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["main"]()
        result = json.loads(output.getvalue())
        self.assertEqual(result["journal"], {"activate.intent.json": pending})
        self.assertIs(result["replay_or_restore_performed"], False)
        namespace["intent"].assert_not_called()
        namespace["owned_remove"].assert_not_called()
        namespace["run"].assert_not_called()

    def fence_fixture(self):
        namespace, directory, value, files, events = memory_remote()
        stopped = snapshot()
        stopped["containers"][0].update(running=False, pid=0, health=None)
        namespace["completed"] = Mock(side_effect=lambda _directory, action: events.append(("completed", action)))
        namespace["snapshot"] = Mock(side_effect=[snapshot(), snapshot(), stopped, stopped])
        namespace["verify_retained"] = Mock()
        original = {"Id": "1" * 64, "Image": OLD_IMAGE, "State": {"Running": True, "Pid": 1001}}
        target = {"Id": "1" * 64, "Image": OLD_IMAGE, "State": {"Running": False, "Pid": 0, "Status": "exited"}}
        namespace["details"] = Mock(return_value=original)
        namespace["runtime_hash"] = Mock(return_value="8" * 64)
        namespace["bounded_command"] = Mock(side_effect=lambda command, *_args, **_kwargs: events.append(("command", command)))
        namespace["wait_stopped"] = Mock(side_effect=lambda *_args, **_kwargs: events.append(("wait-stopped",)) or target)
        namespace["flag_capture"] = Mock(return_value={"sha256": "5" * 64, "file_count": 0})
        namespace["redis_state"] = Mock(return_value={"keys": [], "database": 2})
        namespace["tar_rows"] = Mock()
        namespace["protected_key_bytes"] = Mock(return_value=b"opaque-hash-fixture")
        namespace["RedisReader"] = Mock(return_value=Mock())
        def fake_tar(path, _mode):
            if path.name == "private-backup.tar":
                files[str(path)] = b"owned-fake-tar-bytes"
            return MagicMock()
        namespace["tarfile"] = SimpleNamespace(open=Mock(side_effect=fake_tar), TarInfo=tarfile.TarInfo)
        return namespace, directory, value, files, events, stopped

    def test_fence_unknown_db2_writer_stops_only_env_once_then_blocks_source_backup_and_up(self):
        namespace, directory, value, files, events, _ = self.fence_fixture()
        namespace["redis_state"] = Mock(side_effect=namespace["EnvReleaseError"]("UNKNOWN_DB2_WRITER_NO_CONTROL"))
        namespace["tar_rows"] = Mock(side_effect=AssertionError("source backup must not proceed"))
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "UNKNOWN_DB2_WRITER_NO_CONTROL"):
            namespace["fence_backup"](directory, value)
        self.assertEqual(events[:2], [("completed", "build"), ("completed", "image-backup-complete")])
        namespace["bounded_command"].assert_called_once_with(["docker", "kill", "--signal", "SIGTERM", "1" * 64])
        namespace["tar_rows"].assert_not_called()
        namespace["run"].assert_not_called()
        self.assertIn(str(directory / "stop.intent.json"), files)
        self.assertIn(str(directory / "stop.confirmed.json"), files)
        self.assertIn(str(directory / "stop.complete.json"), files)
        self.assertNotIn(str(directory / "state.json"), files)
        self.assertFalse(any("/owned-live/" in path for path in files))

    def test_fence_success_rechecks_original_stopped_identity_flags_and_clients_without_other_service_control(self):
        namespace, directory, value, files, _, _ = self.fence_fixture()
        reader = namespace["RedisReader"].return_value
        result = namespace["fence_backup"](directory, value)
        self.assertEqual(result["redis_database"], 2)
        self.assertIs(result["env_stopped"], True)
        self.assertEqual(result["other_services_controlled"], 0)
        self.assertIs(result["automatic_restore"], False)
        namespace["bounded_command"].assert_called_once_with(["docker", "kill", "--signal", "SIGTERM", "1" * 64])
        self.assertEqual(namespace["wait_stopped"].call_args_list[-1].kwargs, {"seconds": 1})
        self.assertEqual(namespace["flag_capture"].call_count, 3)
        reader.no_other_clients.assert_called_once()
        reader.close.assert_called_once()
        self.assertIn(str(directory / "fence.snapshot.json"), files)
        self.assertFalse(any("/owned-live/" in path for path in files))

    def test_fence_confirmed_record_precedes_post_stop_snapshot_or_flag_failure(self):
        for failure in ("snapshot", "flags"):
            namespace, directory, value, files, events, stopped = self.fence_fixture()
            flags = namespace["flag_capture"].return_value
            if failure == "snapshot":
                namespace["snapshot"].side_effect = [snapshot(), snapshot(), OSError("owned post-stop snapshot unavailable")]
            else:
                namespace["flag_capture"].side_effect = [flags, OSError("owned post-stop flag read unavailable")]
            with self.subTest(failure=failure), self.assertRaises(OSError):
                namespace["fence_backup"](directory, value)
            stop = json.loads(files[str(directory / "stop.intent.json")])
            confirmed = json.loads(files[str(directory / "stop.confirmed.json")])
            self.assertEqual(stop["source_files"], [snapshot()["code_files"][0]])
            self.assertEqual(stop["runtime_sha256"], snapshot()["target_runtime_sha256"])
            self.assertEqual(stop["flags"], flags)
            self.assertEqual(confirmed["stop_intent_sha256"], ENV.digest(ENV.canonical(stop)))
            self.assertEqual(confirmed["binding"], BINDING)
            self.assertEqual(confirmed["plan_sha256"], PLAN_SHA)
            self.assertEqual(confirmed["pid"], 0)
            self.assertIs(confirmed["running"], False)
            self.assertLess(events.index(("write", str(directory / "stop.intent.json"))), events.index(("command", ["docker", "kill", "--signal", "SIGTERM", "1" * 64])))
            self.assertLess(events.index(("wait-stopped",)), events.index(("write", str(directory / "stop.confirmed.json"))))
            self.assertNotIn(str(directory / "stop.complete.json"), files)
            namespace["bounded_command"].assert_called_once()
            namespace["redis_state"].assert_not_called()

    def test_pre_stop_flag_failure_prevents_control_intent_and_sigterm(self):
        namespace, directory, value, files, _events, _stopped = self.fence_fixture()
        namespace["flag_capture"].side_effect = OSError("owned pre-stop flag read unavailable")
        with self.assertRaises(OSError):
            namespace["fence_backup"](directory, value)
        self.assertNotIn(str(directory / "stop.intent.json"), files)
        self.assertNotIn(str(directory / "stop.confirmed.json"), files)
        namespace["bounded_command"].assert_not_called()

    def test_post_stop_flag_failure_can_resume_same_original_from_pre_stop_proof_without_complete_receipt(self):
        namespace, directory, value, files, _events, stopped = self.fence_fixture()
        flags = namespace["flag_capture"].return_value
        namespace["flag_capture"].side_effect = [flags, OSError("owned post-stop flag read unavailable")]
        with self.assertRaises(OSError):
            namespace["fence_backup"](directory, value)
        retained = dict(files)
        self.assertNotIn(str(directory / "stop.complete.json"), files)
        namespace["flag_capture"].side_effect = None
        namespace["flag_capture"].return_value = flags
        namespace["snapshot"].side_effect = [stopped, snapshot()]
        namespace["bounded_command"].reset_mock()
        namespace["time"].monotonic.side_effect = [0, 1]
        result = namespace["resume_original"](directory, value)
        self.assertEqual(result["original_container_id"], "1" * 64)
        namespace["bounded_command"].assert_called_once_with(["docker", "start", "1" * 64])
        self.assertEqual({path: files[path] for path in retained}, retained)
        self.assertNotIn(str(directory / "stop.complete.json"), files)
        self.assertIn(str(directory / "resume-original.intent.json"), files)

    def test_injected_resume_main_failed_read_preflight_does_not_consume_control_attempt(self):
        namespace, directory, value, files, _events = self.resume_fixture()
        stopped = json.loads(files[str(directory / "stop.complete.json")])["snapshot"]
        del files[str(directory / "stop.complete.json")]
        namespace["ACTION"] = "resume-original"
        namespace["REQUEST"]["plan"] = value
        namespace["release_directory"] = Mock(return_value=directory)
        namespace["owned_test_status"] = Mock(return_value=[])
        flags = namespace["flag_capture"].return_value
        namespace["flag_capture"].side_effect = [OSError("owned transient flag read unavailable"), flags]
        namespace["snapshot"].side_effect = [stopped, stopped, snapshot()]
        first = io.StringIO()
        with redirect_stdout(first):
            namespace["main"]()
        self.assertEqual(json.loads(first.getvalue())["status"], "uncertain")
        self.assertNotIn(str(directory / "resume-original.intent.json"), files)
        namespace["bounded_command"].assert_not_called()
        second = io.StringIO()
        with redirect_stdout(second):
            namespace["main"]()
        self.assertEqual(json.loads(second.getvalue())["status"], "passed")
        namespace["bounded_command"].assert_called_once_with(["docker", "start", "1" * 64])
        third = io.StringIO()
        with redirect_stdout(third):
            namespace["main"]()
        self.assertEqual(json.loads(third.getvalue())["code"], "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
        self.assertEqual(namespace["bounded_command"].call_count, 1)

    def test_injected_resume_main_start_response_loss_consumes_attempt_and_never_resends(self):
        namespace, directory, value, files, _events = self.resume_fixture()
        namespace["ACTION"] = "resume-original"
        namespace["REQUEST"]["plan"] = value
        namespace["release_directory"] = Mock(return_value=directory)
        namespace["owned_test_status"] = Mock(return_value=[])
        namespace["bounded_command"].side_effect = TimeoutError("owned start response lost")
        for _attempt in range(2):
            output = io.StringIO()
            with redirect_stdout(output):
                namespace["main"]()
            self.assertEqual(json.loads(output.getvalue())["status"], "uncertain")
        self.assertIn(str(directory / "resume-original.intent.json"), files)
        namespace["bounded_command"].assert_called_once_with(["docker", "start", "1" * 64])
        namespace["os"].replace.assert_not_called()

    def test_db2_client_guard_allows_only_self_and_other_database_clients(self):
        namespace = remote_namespace()
        reader = object.__new__(namespace["RedisReader"])
        reader.client_id = 123
        reader.command = Mock(return_value=b"id=123 db=2 addr=127.0.0.1:1\nid=456 db=0 addr=127.0.0.1:2\n")
        reader.no_other_clients()
        reader.command.assert_called_once_with("CLIENT", "LIST")
        reader.command.return_value += b"id=789 db=2 addr=127.0.0.1:3\n"
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "UNKNOWN_DB2_WRITER_NO_CONTROL"):
            reader.no_other_clients()
        namespace["socket"].create_connection.assert_not_called()

    def test_db2_reader_rejects_nonloopback_other_database_and_nonredis_url_before_connect(self):
        for url in ("redis://192.0.2.1:6379/2", "redis://127.0.0.1:6379/0", "file:///2", "rediss://localhost/2"):
            namespace = remote_namespace()
            target = {"Config": {"Env": ["REDIS_URL=" + url]}}
            with self.subTest(url=url), self.assertRaisesRegex(namespace["EnvReleaseError"], "REDIS_DB2_SCOPE"):
                namespace["RedisReader"](target)
            namespace["socket"].create_connection.assert_not_called()

    def test_db2_capture_reads_only_scoped_instance_owner_port_cooldown_keys_and_closes_reader(self):
        namespace = remote_namespace()
        kinds = {b"instance:fixture": b"hash", b"user_instances:fixture": b"set", b"allocated_ports": b"set",
                 b"allocated_port_owners": b"hash", b"allocated_port_cooldowns": b"zset"}
        patterns = {"instance:*": b"instance:fixture", "user_instances:*": b"user_instances:fixture",
                    "allocated_ports": b"allocated_ports", "allocated_port_owners": b"allocated_port_owners",
                    "allocated_port_cooldowns": b"allocated_port_cooldowns"}
        reader = Mock()
        def command(*parts):
            if parts[0] == "SCAN":
                self.assertEqual(parts[1], b"0")
                self.assertEqual(parts[2], "MATCH")
                self.assertEqual(parts[4:], ("COUNT", "256"))
                return b"0", [patterns[parts[3]]]
            self.assertIn(parts[0], {"TYPE", "DUMP", "PTTL"})
            self.assertIn(parts[1], kinds)
            return {"TYPE": kinds[parts[1]], "DUMP": b"synthetic-serialized-value", "PTTL": 30000}[parts[0]]
        reader.command.side_effect = command
        namespace["RedisReader"] = Mock(return_value=reader)
        result = namespace["redis_state"]({"Id": "1" * 64})
        self.assertEqual(result["database"], 2)
        self.assertEqual(len(result["keys"]), 5)
        self.assertEqual(reader.no_other_clients.call_count, 2)
        reader.close.assert_called_once()
        self.assertFalse({call.args[0] for call in reader.command.call_args_list} & {"DEL", "RESTORE", "FLUSHDB", "FLUSHALL", "CONFIG", "SET", "SELECT"})
        namespace["socket"].create_connection.assert_not_called()

    def test_fence_missing_build_or_image_backup_receipt_prevents_any_stop(self):
        for missing in ("build", "image-backup-complete"):
            namespace, directory, value, _, _ = memory_remote()
            def completed(_directory, action):
                if action == missing:
                    raise namespace["EnvReleaseError"]("PRIOR_PHASE_INCOMPLETE_" + action.upper())
            namespace["completed"] = Mock(side_effect=completed)
            namespace["bounded_command"] = Mock()
            namespace["snapshot"] = Mock()
            with self.subTest(missing=missing), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["fence_backup"](directory, value)
            namespace["snapshot"].assert_not_called()
            namespace["bounded_command"].assert_not_called()

    def test_wait_stopped_requires_exact_image_pid_zero_exited_without_force_kill(self):
        namespace = remote_namespace()
        stopped = {"Image": OLD_IMAGE, "State": {"Running": False, "Pid": 0, "Status": "exited"}}
        namespace["details"] = Mock(return_value=stopped)
        namespace["time"].monotonic.side_effect = [0, 1]
        self.assertIs(namespace["wait_stopped"]("1" * 64, OLD_IMAGE), stopped)
        for candidate in ({**stopped, "Image": NEW_IMAGE},
                          {**stopped, "State": {"Running": False, "Pid": 1, "Status": "exited"}},
                          {**stopped, "State": {"Running": False, "Pid": 0, "Status": "dead"}}):
            namespace["details"].return_value = candidate
            namespace["time"].monotonic.side_effect = [0, 1]
            with self.subTest(candidate=candidate), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["wait_stopped"]("1" * 64, OLD_IMAGE)
        namespace["run"].assert_not_called()
        namespace["subprocess"].run.assert_not_called()

    def test_wait_stopped_timeout_queries_identity_without_second_signal_or_cleanup(self):
        namespace = remote_namespace()
        namespace["details"] = Mock(return_value={"Image": OLD_IMAGE, "State": {"Running": True, "Pid": 1001, "Status": "running"}})
        namespace["time"].monotonic.side_effect = [0, 1, 91]
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "ENV_GRACEFUL_STOP_UNCONFIRMED_QUERY_STATUS"):
            namespace["wait_stopped"]("1" * 64, OLD_IMAGE)
        namespace["details"].assert_called_once_with("1" * 64)
        namespace["run"].assert_not_called()
        namespace["subprocess"].run.assert_not_called()

    def activation_fixture(self):
        namespace, directory, value, files, events = memory_remote()
        payload = b"source-bound-new-go-fixture"
        value["managed_files"] = [source_row(ENV.PREFIX + "cmd/main.go", payload)]
        value["modes"] = {ENV.PREFIX + "cmd/main.go": 0o644}
        staged = directory / "source" / (ENV.PREFIX + "cmd/main.go")
        live = namespace["ROOT"] / (ENV.PREFIX + "cmd/main.go")
        files[str(staged)], files[str(live)] = payload, b"fixture"
        stopped, after = snapshot(), snapshot()
        stopped["containers"][0].update(running=False, pid=0, health=None)
        after["containers"][0].update(container_id="4" * 64, image_id=NEW_IMAGE, pid=4001)
        after["code_files"][0] = value["managed_files"][0]
        built = {"image_id": NEW_IMAGE, "binary_proof": {"binary_sha256": "5" * 64}}
        captured = {"env_stopped": True, "other_db2_clients": 0}
        flags = {"sha256": "7" * 64, "file_count": 0}
        files[str(directory / "stop.complete.json")] = ENV.canonical({"snapshot": stopped, "flags": flags})
        namespace["completed"] = Mock(side_effect=lambda _dir, action: built if action == "build" else captured)
        namespace["snapshot"] = Mock(side_effect=[stopped, after])
        namespace["verify_retained"] = Mock()
        namespace["details"] = Mock(side_effect=[{"Id": "1" * 64}, {"Id": "1" * 64}, {"Config": {"Labels": namespace["source_labels"](BINDING)}}])
        namespace["flag_capture"] = Mock(return_value=flags)
        namespace["redis_state"] = Mock(side_effect=lambda _target: events.append(("redis-state",)) or {"keys": []})
        namespace["run"] = Mock(side_effect=lambda command: events.append(("command", command)) or json.dumps({"services": {ENV.SERVICE: {"image": NEW_IMAGE}}}).encode())
        namespace["bounded_command"] = Mock(side_effect=lambda command, *_args, **_kwargs: events.append(("command", command)))
        namespace["time"].monotonic.side_effect = [0, 1]
        return namespace, directory, value, files, events, live, captured

    def test_activate_captures_db2_state_before_source_then_exact_env_only_compose_up_and_health(self):
        namespace, directory, value, files, events, live, _ = self.activation_fixture()
        result = namespace["activate"](directory, value)
        self.assertEqual(result["image_id"], NEW_IMAGE)
        self.assertEqual(result["container_id"], "4" * 64)
        self.assertEqual(files[str(live)], b"source-bound-new-go-fixture")
        self.assertLess(events.index(("redis-state",)), next(index for index, event in enumerate(events) if event[0] == "write" and "env-release-" in event[1]))
        command = namespace["bounded_command"].call_args.args[0]
        self.assertEqual(command[-8:], ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", ENV.SERVICE])
        self.assertEqual(command.count("up"), 1)
        self.assertFalse(set(command) & {"down", "stop", "restart", "prune", "rmi", "migration", "SIGKILL"})
        overlay = json.loads(files[str(directory / "overlay.json")])
        self.assertEqual(set(overlay["services"]), {ENV.SERVICE})
        self.assertEqual(overlay["services"][ENV.SERVICE]["image"], NEW_IMAGE)
        self.assertEqual(namespace["snapshot"].call_count, 2)

    def test_activate_missing_or_unconfirmed_backup_prevents_redis_source_and_up(self):
        for patch_values in ({"env_stopped": False}, {"other_db2_clients": 1}):
            namespace, directory, value, files, _, live, captured = self.activation_fixture()
            captured.update(patch_values)
            before = dict(files)
            with self.subTest(patch_values=patch_values), self.assertRaisesRegex(namespace["EnvReleaseError"], "ACTIVATION_REQUIRES_BACKUP_FENCE"):
                namespace["activate"](directory, value)
            self.assertEqual(files, before)
            namespace["redis_state"].assert_not_called()
            namespace["run"].assert_not_called()
            namespace["bounded_command"].assert_not_called()

    def test_activate_unknown_db2_clients_prevents_live_source_replacement_and_compose_up(self):
        namespace, directory, value, files, _, live, _ = self.activation_fixture()
        namespace["redis_state"] = Mock(side_effect=namespace["EnvReleaseError"]("UNKNOWN_DB2_WRITER_NO_CONTROL"))
        original = files[str(live)]
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "UNKNOWN_DB2_WRITER_NO_CONTROL"):
            namespace["activate"](directory, value)
        self.assertEqual(files[str(live)], original)
        namespace["os"].replace.assert_not_called()
        namespace["run"].assert_not_called()
        namespace["bounded_command"].assert_not_called()

    def resume_fixture(self):
        namespace, directory, value, files, events = memory_remote()
        stopped = snapshot()
        stopped["containers"][0].update(running=False, pid=0, health=None)
        flags = {"sha256": "5" * 64, "file_count": 0}
        files[str(directory / "stop.intent.json")] = ENV.canonical({"binding": BINDING, "plan_sha256": PLAN_SHA,
            "container_id": "1" * 64, "image_id": OLD_IMAGE, "source_files": [stopped["code_files"][0]],
            "runtime_sha256": stopped["target_runtime_sha256"], "flags": flags})
        files[str(directory / "stop.complete.json")] = ENV.canonical({"snapshot": stopped, "flags": flags})
        namespace["snapshot"] = Mock(side_effect=[stopped, snapshot()])
        namespace["details"] = Mock(return_value={"Id": "1" * 64})
        namespace["wait_stopped"] = Mock(return_value={"Id": "1" * 64, "Image": OLD_IMAGE,
            "State": {"Running": False, "Pid": 0, "Status": "exited"}})
        namespace["runtime_hash"] = Mock(return_value=stopped["target_runtime_sha256"])
        namespace["flag_capture"] = Mock(return_value=flags)
        namespace["RedisReader"] = Mock(return_value=Mock())
        namespace["bounded_command"] = Mock(side_effect=lambda command: events.append(("command", command)))
        namespace["time"].monotonic.side_effect = [0, 1]
        return namespace, directory, value, files, events

    def test_resume_original_starts_only_exact_previous_container_without_source_or_data_restore(self):
        namespace, directory, value, files, events = self.resume_fixture()
        before = dict(files)
        result = namespace["resume_original"](directory, value)
        self.assertEqual(result["original_container_id"], "1" * 64)
        self.assertEqual(result["original_image_id"], OLD_IMAGE)
        self.assertIs(result["automatic_restore"], False)
        namespace["bounded_command"].assert_called_once_with(["docker", "start", "1" * 64])
        self.assertEqual({path: files[path] for path in before}, before)
        self.assertEqual(set(files) - set(before), {str(directory / "resume-original.intent.json")})
        self.assertLess(events.index(("write", str(directory / "resume-original.intent.json"))), events.index(("command", ["docker", "start", "1" * 64])))
        namespace["os"].replace.assert_not_called()

    def test_resume_original_is_forbidden_after_any_activation_intent_even_without_completion(self):
        namespace, directory, value, files, _ = self.resume_fixture()
        files[str(directory / "activate.intent.json")] = b"{}"
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "ACTIVATION_INTENT_FORBIDS_ORIGINAL_RESUME"):
            namespace["resume_original"](directory, value)
        namespace["snapshot"].assert_not_called()
        namespace["bounded_command"].assert_not_called()

    def test_resume_original_rejects_changed_ready_container_or_image_after_start(self):
        for key, bad in (("container_id", "4" * 64), ("image_id", NEW_IMAGE)):
            namespace, directory, value, files, _ = self.resume_fixture()
            stopped = json.loads(files[str(directory / "stop.complete.json")])["snapshot"]
            after = snapshot()
            after["containers"][0][key] = bad
            namespace["snapshot"].side_effect = [stopped, after]
            with self.subTest(key=key), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["resume_original"](directory, value)
            namespace["bounded_command"].assert_called_once_with(["docker", "start", "1" * 64])

    def fence_current_fixture(self):
        namespace, directory, value, files, events, _live, _captured = self.activation_fixture()
        before, after = snapshot(), snapshot()
        for observed in (before, after):
            observed["containers"][0].update(container_id="4" * 64, image_id=NEW_IMAGE, pid=4001)
            observed["code_files"][0] = copy.deepcopy(value["managed_files"][0])
        after["containers"][0].update(running=False, pid=0, health=None)
        files[str(directory / "activate.intent.json")] = ENV.canonical({"action": "activate", "binding": BINDING, "plan_sha256": PLAN_SHA})
        namespace["snapshot"] = Mock(side_effect=[before, after])
        namespace["details"] = Mock(return_value={"Id": "4" * 64, "Image": NEW_IMAGE,
            "Config": {"Labels": namespace["source_labels"](BINDING)}})
        namespace["runtime_hash"] = Mock(return_value=before["target_runtime_sha256"])
        namespace["wait_stopped"] = Mock(return_value={"Image": NEW_IMAGE, "State": {"Running": False, "Pid": 0, "Status": "exited"}})
        return namespace, directory, value, files, before, after

    def test_explicit_fence_current_stops_only_source_bound_new_env_once_and_never_restores_old_version(self):
        namespace, directory, value, _, _, _ = self.fence_current_fixture()
        result = namespace["fence_current"](directory, value)
        self.assertEqual(result["fenced_container_id"], "4" * 64)
        self.assertEqual(result["image_id"], NEW_IMAGE)
        self.assertIs(result["automatic_restore"], False)
        self.assertIs(result["old_version_resume_forbidden"], True)
        namespace["bounded_command"].assert_called_once()
        self.assertEqual(namespace["bounded_command"].call_args.args[0], ["docker", "kill", "--signal", "SIGTERM", "4" * 64])
        namespace["wait_stopped"].assert_called_once_with("4" * 64, NEW_IMAGE)
        namespace["run"].assert_not_called()
        namespace["os"].replace.assert_not_called()

    def test_snapshot_drift_reports_hashes_without_raw_values_and_ignores_intended_env_source_identity_change(self):
        before, after = snapshot(), snapshot()
        after["containers"][0].update(image_id=NEW_IMAGE, container_id="4" * 64)
        after["code_files"][0]["sha256"] = "0" * 64
        self.assertEqual(ENV.snapshot_drift(before, after), [])
        after["containers"][1]["pid"] += 1
        after["labs"][0]["running"] = False
        after["databases"]["learn"] = ["030"]
        after["protected_files"][0]["sha256"] = "0" * 64
        after["retained_labels"]["frontend"]["org.opencontainers.image.revision"] = "0" * 40
        after["target_runtime_sha256"] = "0" * 64
        after["code_files"][1]["sha256"] = "0" * 64
        rows = ENV.snapshot_drift(before, after)
        self.assertEqual({row["scope"] for row in rows}, {"protected_files", "labs", "databases", "retained_labels",
            "target_runtime_sha256", "non_target_containers", "retained_source"})
        for row in rows:
            self.assertEqual(set(row), {"scope", "original_sha256", "current_sha256"})
            self.assertNotEqual(row["original_sha256"], row["current_sha256"])
            self.assertRegex(row["original_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["current_sha256"], r"^[0-9a-f]{64}$")

    def test_fence_current_reports_original_non_target_lab_or_db_drift_without_blocking_exact_release_target(self):
        for scope in ("containers", "labs", "databases", "protected_files"):
            namespace, directory, value, files, before, after = self.fence_current_fixture()
            for observed in (before, after):
                if scope == "containers":
                    observed[scope][1]["pid"] += 1
                elif scope == "labs":
                    observed[scope][0]["running"] = False
                elif scope == "databases":
                    observed[scope]["learn"] = ["030"]
                else:
                    observed[scope][0]["sha256"] = "0" * 64
            with self.subTest(scope=scope):
                result = namespace["fence_current"](directory, value)
                self.assertTrue(result["original_plan_drift"])
                self.assertIs(result["immediate_non_target_preservation"], True)
                namespace["bounded_command"].assert_called_once()
                self.assertIn(str(directory / "fence-current.intent.json"), files)
                namespace["os"].replace.assert_not_called()

    def test_fence_current_rejects_immediate_non_target_or_target_identity_change_after_single_signal(self):
        for scope in ("containers", "labs", "databases", "source", "target"):
            namespace, directory, value, files, _before, after = self.fence_current_fixture()
            if scope == "containers":
                after[scope][1]["pid"] += 1
            elif scope == "labs":
                after[scope][0]["running"] = False
            elif scope == "databases":
                after[scope]["learn"] = ["030"]
            elif scope == "source":
                after["code_files"][1]["sha256"] = "0" * 64
            else:
                after["containers"][0]["container_id"] = "9" * 64
            with self.subTest(scope=scope), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["fence_current"](directory, value)
            namespace["bounded_command"].assert_called_once()
            self.assertIn(str(directory / "fence-current.intent.json"), files)
            namespace["os"].replace.assert_not_called()

    def test_injected_fence_current_main_failed_guard_does_not_consume_one_control_attempt(self):
        namespace, directory, value, files, before, after = self.fence_current_fixture()
        namespace["ACTION"] = "fence-current"
        namespace["REQUEST"]["plan"] = value
        namespace["release_directory"] = Mock(return_value=directory)
        namespace["owned_test_status"] = Mock(return_value=[])
        good_labels = namespace["details"].return_value["Config"]["Labels"]
        namespace["details"].return_value["Config"]["Labels"] = {}
        first = io.StringIO()
        with redirect_stdout(first):
            namespace["main"]()
        self.assertEqual(json.loads(first.getvalue())["status"], "uncertain")
        self.assertNotIn(str(directory / "fence-current.intent.json"), files)
        namespace["bounded_command"].assert_not_called()
        namespace["snapshot"].side_effect = [before, after]
        namespace["details"].return_value["Config"]["Labels"] = good_labels
        second = io.StringIO()
        with redirect_stdout(second):
            namespace["main"]()
        self.assertEqual(json.loads(second.getvalue())["status"], "passed")
        namespace["bounded_command"].assert_called_once()
        self.assertIn(str(directory / "fence-current.intent.json"), files)

    def test_explicit_fence_current_rejects_missing_or_unbound_activation_intent_before_signal(self):
        for altered in (None, {}, {"action": "activate", "binding": {**BINDING, "commit": "0" * 40}, "plan_sha256": PLAN_SHA},
                        {"action": "activate", "binding": BINDING, "plan_sha256": "0" * 64}):
            namespace, directory, value, files, _, _ = self.fence_current_fixture()
            path = str(directory / "activate.intent.json")
            if altered is None:
                del files[path]
            else:
                files[path] = ENV.canonical(altered)
            with self.subTest(altered=altered), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["fence_current"](directory, value)
            namespace["bounded_command"].assert_not_called()

    def test_explicit_fence_current_rejects_different_live_image_source_or_labels_before_signal(self):
        for altered in ("image", "source", "labels"):
            namespace, directory, value, _, before, _ = self.fence_current_fixture()
            if altered == "image":
                before["containers"][0]["image_id"] = OLD_IMAGE
            elif altered == "source":
                before["code_files"][0]["sha256"] = "0" * 64
            else:
                namespace["details"].return_value["Config"]["Labels"] = {}
            with self.subTest(altered=altered), self.assertRaises(namespace["EnvReleaseError"]):
                namespace["fence_current"](directory, value)
            namespace["bounded_command"].assert_not_called()

    def test_owned_cleanup_requires_nonce_label_and_does_not_remove_on_stop_timeout(self):
        namespace = remote_namespace(request={"scope": ENV.SCOPE, "plan": plan()})
        identifier = "6" * 64
        nonce = BINDING["release_nonce"]
        namespace["details"] = Mock(return_value={"Config": {"Labels": {}}, "State": {"Running": True}, "Image": NEW_IMAGE})
        namespace["bounded_command"] = Mock()
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "OWNED_TEST_IDENTITY"):
            namespace["owned_remove"](identifier, nonce)
        namespace["bounded_command"].assert_not_called()
        namespace["details"].return_value["Config"]["Labels"]["io.ctf-web.env-test-nonce"] = nonce
        namespace["wait_stopped"] = Mock(side_effect=namespace["EnvReleaseError"]("ENV_GRACEFUL_STOP_UNCONFIRMED_QUERY_STATUS"))
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "ENV_GRACEFUL_STOP_UNCONFIRMED_QUERY_STATUS"):
            namespace["owned_remove"](identifier, nonce)
        namespace["bounded_command"].assert_called_once_with(["docker", "kill", "--signal", "SIGTERM", identifier])

    def test_owned_status_uses_full_ids_and_exact_nonce_filter_without_controlling_containers(self):
        namespace = remote_namespace()
        nonce, identifier = BINDING["release_nonce"], "6" * 64
        namespace["run"].side_effect = None
        namespace["run"].return_value = (identifier + "\n").encode()
        namespace["details"] = Mock(return_value={"Id": identifier, "Image": NEW_IMAGE,
            "Name": "/ctf-env-startup-" + nonce, "State": {"Running": True, "Status": "running"},
            "Config": {"Labels": {"io.ctf-web.env-test-nonce": nonce}}})
        namespace["bounded_command"] = Mock()
        rows = namespace["owned_test_status"](nonce)
        self.assertEqual(rows[0]["container_id"], identifier)
        command = namespace["run"].call_args.args[0]
        self.assertIn("--no-trunc", command)
        self.assertEqual(command[command.index("--filter") + 1], "label=io.ctf-web.env-test-nonce=" + nonce)
        namespace["details"].assert_called_once_with(identifier)
        namespace["bounded_command"].assert_not_called()

    def test_owned_startup_name_requires_nonce_label_before_using_resolved_container_id(self):
        namespace = remote_namespace(request={"scope": ENV.SCOPE, "plan": plan()})
        name = "ctf-env-startup-" + BINDING["release_nonce"]
        resolved = {"Id": "6" * 64, "Config": {"Labels": {"io.ctf-web.env-test-nonce": BINDING["release_nonce"]}}}
        namespace["run"].return_value = json.dumps([resolved]).encode()
        namespace["run"].side_effect = None
        self.assertEqual(namespace["details"](name), resolved)
        resolved["Config"]["Labels"] = {}
        namespace["run"].return_value = json.dumps([resolved]).encode()
        with self.assertRaisesRegex(namespace["EnvReleaseError"], "OWNED_CONTAINER_NAME_IDENTITY"):
            namespace["details"](name)
        namespace["subprocess"].run.assert_not_called()

    def test_startup_timeout_records_owned_status_without_blind_remove_or_retry(self):
        namespace, directory, value, files, _ = memory_remote()
        binary = bytearray(32)
        binary[:4], binary[5], binary[18] = b"\x7fELF", 1, 62
        namespace["bounded_command"] = Mock(return_value=bytes(binary))
        namespace["run"].side_effect = None
        namespace["run"].return_value = GO_BUILD_INFO
        namespace["subprocess"].run.side_effect = TimeoutError("owned startup deadline")
        namespace["owned_test_status"] = Mock(return_value=[{"container_id": "6" * 64, "running": True}])
        namespace["owned_remove"] = Mock(side_effect=AssertionError("no blind removal"))
        with self.assertRaises(TimeoutError):
            namespace["binary_probe"](directory, NEW_IMAGE, OLD_IMAGE, BINDING)
        recorded = json.loads(files[str(directory / "isolated-startup-uncertain.json")])
        self.assertEqual(recorded["name"], "ctf-env-startup-" + BINDING["release_nonce"])
        self.assertEqual(recorded["image_id"], NEW_IMAGE)
        self.assertEqual(recorded["next"], "status; no blind cleanup/retry")
        self.assertEqual(recorded["owned"], [{"container_id": "6" * 64, "running": True}])
        namespace["subprocess"].run.assert_called_once()
        self.assertEqual(namespace["bounded_command"].call_count, 2)
        namespace["owned_remove"].assert_not_called()

    def test_uncertain_remote_result_is_recorded_once_and_requires_status_without_replay(self):
        value = plan()
        captured = backup_proof()
        artifact_files = {}
        for artifact, payload in zip(captured["files"], (b"owned-image-fixture", b"owned-state-fixture")):
            artifact.update(bytes=len(payload), sha256=ENV.digest(payload))
            filename = Mock(spec=Path)
            filename.is_file.return_value = True
            filename.parents = ()
            filename.is_relative_to.return_value = True
            filename.stat.return_value = SimpleNamespace(st_size=len(payload))
            filename.open.side_effect = lambda _mode, data=payload: io.BytesIO(data)
            artifact_files[str(Path(artifact["path"]))] = filename
        for action in ("build", "activate", "verify", "resume-original"):
            output = io.StringIO()
            with self.subTest(action=action), \
                 patch.object(ENV, "load_json", side_effect=lambda path: (value if str(path) == "owned-fixture-plan.json" else captured, PLAN_SHA)), \
                 patch.object(ENV, "validate_plan", return_value=value), \
                 patch.object(ENV.release_preflight, "task_path", side_effect=lambda path, _root: artifact_files[str(path)]), \
                 patch.object(ENV.ops, "linked", return_value=False), \
                 patch.object(ENV, "make_archive", return_value=b"owned-fake-archive"), \
                 patch.object(ENV, "remote", return_value={"status": "uncertain"}) as remote, \
                 patch.object(ENV, "receipt", return_value=Path("owned-fixture-receipt.json")) as receipt, \
                 patch.object(ENV.ops, "streaming_ssh", side_effect=AssertionError("stream forbidden")), redirect_stdout(output):
                self.assertEqual(ENV.main([action, "--plan", "owned-fixture-plan.json", "--execute"]), 2, output.getvalue())
                remote.assert_called_once()
                self.assertEqual(remote.call_args.args[0], action)
                receipt.assert_called_once()
            observed = json.loads(output.getvalue())
            self.assertEqual(observed["status"], "uncertain")
            self.assertEqual(observed["next"], "status; do not replay or restore")

    def test_backup_actions_without_acl_fail_before_any_remote_or_stream_call(self):
        value = plan()
        for action in ("image-backup", "fence-backup"):
            output = io.StringIO()
            with self.subTest(action=action), \
                 patch.object(ENV, "load_json", return_value=(value, PLAN_SHA)), \
                 patch.object(ENV, "validate_plan", return_value=value), \
                 patch.object(ENV, "remote", side_effect=AssertionError("network forbidden")) as remote, \
                 patch.object(ENV, "receipt", side_effect=AssertionError("write forbidden")), \
                 patch.object(ENV.ops, "streaming_ssh", side_effect=AssertionError("stream forbidden")), redirect_stdout(output):
                self.assertEqual(ENV.main([action, "--plan", "owned-fixture-plan.json", "--execute"]), 1)
                remote.assert_not_called()
            observed = json.loads(output.getvalue())
            self.assertEqual(observed["code"], "ACL_ATTESTATION_REQUIRED")
            self.assertIs(observed["automatic_restore"], False)


class EnvCanonicalSemanticContracts(unittest.TestCase):
    def runtime(self):
        return {"Config": {"Env": ["B=synthetic=value", "A=other"], "User": "appuser"}, "HostConfig": {}, "Mounts": []}

    def test_runtime_hash_normalizes_generic_environment_order_and_preserves_full_values(self):
        namespace = remote_namespace()
        for count in (0, 1, 17, 29):
            first = self.runtime()
            first["Config"]["Env"] = ["KEY_" + str(index) + "=synthetic=" + str(index) for index in range(count)]
            second = copy.deepcopy(first); second["Config"]["Env"].reverse()
            frozen = copy.deepcopy((first, second))
            self.assertEqual(namespace["runtime_hash"](first), namespace["runtime_hash"](second))
            self.assertEqual((first, second), frozen)
        first, second = self.runtime(), self.runtime()
        second["Config"]["Env"][0] += "-changed"
        self.assertNotEqual(namespace["runtime_hash"](first), namespace["runtime_hash"](second))

    def test_runtime_hash_rejects_duplicate_or_malformed_names_without_secret_output(self):
        namespace = remote_namespace()
        for entries in (None, "KEY=value", [None], ["missing-separator"], ["=synthetic"], ["9KEY=synthetic"],
                        ["A B=synthetic"], ["A\nB=synthetic"], ["A=synthetic", "A=synthetic"], ["A=synthetic", "A=different"]):
            value = self.runtime(); value["Config"]["Env"] = entries
            with self.subTest(entries=entries), self.assertRaises(namespace["EnvReleaseError"]) as error:
                namespace["runtime_hash"](value)
            self.assertNotIn("synthetic", str(error.exception))

    def test_runtime_hash_still_covers_every_other_config_host_and_effective_mount_field(self):
        namespace = remote_namespace()
        original = self.runtime(); before = namespace["runtime_hash"](original)
        fields = {"Config": ("User", "Cmd", "Entrypoint", "WorkingDir", "Healthcheck", "ExposedPorts"),
                  "HostConfig": ("NetworkMode", "Binds", "Mounts", "GroupAdd", "SecurityOpt", "CapAdd", "CapDrop", "ReadonlyRootfs", "PortBindings", "Memory", "NanoCpus", "Tmpfs", "ExtraHosts", "RestartPolicy", "Privileged", "PidMode", "IpcMode", "Devices", "LogConfig", "Sysctls", "Ulimits", "Dns")}
        for section, names in fields.items():
            for name in names:
                changed = copy.deepcopy(original); changed[section][name] = "changed"
                with self.subTest(section=section, name=name): self.assertNotEqual(before, namespace["runtime_hash"](changed))
        changed = copy.deepcopy(original)
        changed["Mounts"] = [{"Type": "bind", "Source": "/changed", "Destination": "/changed", "RW": False}]
        self.assertNotEqual(before, namespace["runtime_hash"](changed))

    def test_context_rows_sort_exact_paths_independently_from_manifest_array_order(self):
        payloads = context_payloads()
        payloads.update({ENV.PREFIX + "internal/fixture_" + str(index) + ".go": b"fixture" for index in range(23)})
        expected = manifest_for(payloads)["files"]
        self.assertEqual(len(expected), 29)
        for rows in (list(reversed(expected)), sorted(expected, key=lambda row: row["path"].lower())):
            manifest = {"files": rows, "fingerprint": ENV.stage.manifest_fingerprint(rows)}
            self.assertEqual(ENV.context_rows(manifest), expected)

    def test_plan_current_context_comparison_accepts_semantic_row_order_without_mutating_plan(self):
        manifest = manifest_for(context_payloads())
        value = plan()
        value["binding"]["source_fingerprint"] = manifest["fingerprint"]
        value["managed_files"] = sorted(manifest["files"], key=lambda row: row["path"].lower())
        value["modes"] = {row["path"]: 0o644 for row in value["managed_files"]}
        value["tool_sources"] = ENV.tools_binding()
        frozen = copy.deepcopy(value)
        with patch.object(ENV.ops, "manifest", return_value=manifest):
            self.assertIs(ENV.validate_plan(value), value)
        self.assertEqual(value, frozen)

    def test_canonical_rows_reject_duplicate_paths_extra_fields_or_unsafe_content(self):
        row = source_row(ENV.PREFIX + "Dockerfile")
        cases = [[row, copy.deepcopy(row)], [{**row, "extra": "ignored"}], [{**row, "bytes": True}], [{**row, "bytes": -1}],
                 [{**row, "sha256": "bad"}], [{**row, "path": "../escape"}], [{**row, "path": "/absolute"}],
                 [{**row, "path": "services\\env-orchestrator\\Dockerfile"}], [{**row, "path": "services//env-orchestrator/Dockerfile"}]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ENV.EnvReleaseError): ENV.canonical_file_rows(rows)

    def test_preservation_ignores_only_source_array_order_and_never_source_content_or_duplicates(self):
        before = snapshot(); after = copy.deepcopy(before); after["code_files"].reverse()
        ENV.preserved_snapshot(before, after)
        self.assertEqual(ENV.snapshot_drift(before, after), [])
        for change in ("hash", "bytes", "path", "duplicate"):
            changed = copy.deepcopy(after)
            if change == "duplicate": changed["code_files"].append(copy.deepcopy(changed["code_files"][0]))
            elif change == "hash": changed["code_files"][0]["sha256"] = "0" * 64
            elif change == "bytes": changed["code_files"][0]["bytes"] += 1
            else: changed["code_files"][0]["path"] += ".different"
            with self.subTest(change=change), self.assertRaises(ENV.EnvReleaseError): ENV.preserved_snapshot(before, changed)

    def test_actual_independent_verify_accepts_29_exact_rows_and_preserves_baseline_lab(self):
        namespace, directory, value, _files, _events = memory_remote()
        rows = [source_row(ENV.PREFIX + "internal/fixture_" + str(index) + ".go") for index in range(28)] + [source_row(ENV.PREFIX + "Dockerfile")]
        value["managed_files"] = ENV.canonical_file_rows(rows)
        value["inspection"]["code_files"] = rows + [source_row("frontend/src/app/page.tsx")]
        current = copy.deepcopy(value["inspection"])
        current["code_files"] = [source_row("frontend/src/app/page.tsx")] + list(reversed(rows))
        current["containers"][0].update(container_id="4" * 64, image_id=NEW_IMAGE)
        binary = b"synthetic-running-binary"
        built = {"image_id": NEW_IMAGE, "binary_proof": {"binary_sha256": ENV.digest(binary)}}
        namespace["completed"] = Mock(side_effect=lambda _directory, action: built if action == "build" else {"container_id": "4" * 64})
        namespace["snapshot"] = Mock(return_value=current); namespace["verify_retained"] = Mock(); namespace["image_source"] = Mock()
        namespace["run"] = Mock(return_value=binary)
        proof = namespace["independent_verify"](directory, value)
        self.assertEqual(proof["managed_file_count"], 29)
        self.assertEqual(proof["lab_count"], 1)
        namespace["run"].assert_called_once_with(["docker", "exec", "4" * 64, "cat", "/usr/local/bin/env-orchestrator"])
        for change in ("duplicate", "hash", "bytes", "extra"):
            damaged = copy.deepcopy(current)
            row = next(item for item in damaged["code_files"] if item["path"].startswith(ENV.PREFIX))
            if change == "duplicate": damaged["code_files"].append(copy.deepcopy(row))
            elif change == "hash": row["sha256"] = "0" * 64
            elif change == "bytes": row["bytes"] += 1
            else: row["path"] += ".extra"
            namespace["snapshot"].return_value = damaged
            with self.subTest(change=change), self.assertRaises(namespace["EnvReleaseError"]): namespace["independent_verify"](directory, value)

    def test_inherited_compose_lab_is_excluded_from_core_but_separately_fully_tracked(self):
        namespace = remote_namespace()
        core = {"Id": "2" * 64, "Image": "sha256:" + "f" * 64,
                "Config": {"Labels": {"com.docker.compose.project": "ctf_web", "com.docker.compose.service": "learn-service"}},
                "State": {"Running": True, "Pid": 2001, "Status": "running", "StartedAt": "2026-10-13T00:00:00Z",
                          "FinishedAt": "0001-01-01T00:00:00Z", "Health": {"Status": "healthy"}}, "RestartCount": 0, "Mounts": []}
        lab = copy.deepcopy(core)
        lab.update(Id="3" * 64, Image="sha256:" + "9" * 64)
        lab["Config"]["Labels"]["ctf-challenge"] = "true"
        lab["NetworkSettings"] = {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "45001"}]}}
        lab["Mounts"] = [{"Type": "bind", "Source": "/owned-lab", "Destination": "/owned-lab", "RW": False}]
        def run(command):
            if command[1] == "ps":
                return (lab["Id"] if "label=ctf-challenge=true" in command else core["Id"] + "\n" + lab["Id"]).encode()
            self.assertEqual(command[:2], ["docker", "inspect"])
            return json.dumps([item for item in (core, lab) if item["Id"] in command[2:]]).encode()
        namespace["run"] = Mock(side_effect=run)
        self.assertEqual([row["container_id"] for row in namespace["containers"]()], [core["Id"]])
        rows = namespace["labs"]()
        self.assertEqual(rows, namespace["labs"]())
        self.assertEqual(rows[0]["container_id"], lab["Id"])
        self.assertEqual(rows[0]["image_id"], lab["Image"])
        self.assertEqual(rows[0]["ports"], lab["NetworkSettings"]["Ports"])
        self.assertEqual(rows[0]["mounts"][0]["Source"], "/owned-lab")
        self.assertTrue(all(call.args[0][1] in {"ps", "inspect"} for call in namespace["run"].call_args_list))


if __name__ == "__main__":
    unittest.main()
