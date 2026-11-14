"""Pure source/path/archive/build-order tests. No SSH, Docker, or Git writes."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from types import SimpleNamespace


RENEWAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RENEWAL))
SPEC = importlib.util.spec_from_file_location("stage_build_release_tested", RENEWAL / "stage_build_release.py")
BUILD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILD
SPEC.loader.exec_module(BUILD)
TEMP_ROOT = BUILD.ops.TASK / "runtime" / "stage-build-unit"
FP = "a" * 64
COMMIT = "b" * 40
NONCE = "11111111-2222-4333-8444-555555555555"
BINDING = BUILD.validate_binding(FP, COMMIT, NONCE)
ARGS = {"NEXT_PUBLIC_SITE_URL": "https://example.invalid/ctf", "NEXT_PUBLIC_ENV_PROXY_BASE": "", "NEXT_PUBLIC_ENV_ACCESS_MODE": "proxy"}


def context_payloads():
    result = {}
    for service, context in BUILD.CONTEXTS:
        if service == "frontend":
            files = {"Dockerfile": b"FROM node:20 AS production\nCOPY package*.json ./\nCOPY . .\n",
                     ".dockerignore": b"node_modules\n.next\n.git\n*.md\n",
                     "package.json": b"{}\n", "package-lock.json": b"{}\n", "tsconfig.json": b"{}\n",
                     "next.config.ts": b"export default {};\n", "src/app/layout.tsx": b"export default function Layout() {}\n",
                     "messages/ko.json": b"{}\n", "public/fixture.bin": b"\x00\x01\x02"}
        else:
            docker = b"FROM python:3.12-slim\nCOPY requirements.txt .\nCOPY app/ ./app/\nCOPY alembic.ini ./\nCOPY migrations/ ./migrations/\n"
            files = {"Dockerfile": docker, ".dockerignore": b"__pycache__\n*.pyc\n.env\n.env.*\n.git\n.venv\n*.pem\n*.key\n",
                     "requirements.txt": b"", "app/main.py": b"app = None\n", "alembic.ini": b"[alembic]\n",
                     "migrations/env.py": b"pass\n"}
            if service == "auth":
                files["Dockerfile"] += b"COPY docker-entrypoint.sh /usr/local/bin/entrypoint\n"
                files["docker-entrypoint.sh"] = b"#!/bin/sh\nexec python -m app.main\n"
        result.update({context + "/" + name: payload for name, payload in files.items()})
    return result


def archive_members(items):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data, kind in items:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o644
            entry.type = kind
            if kind == tarfile.REGTYPE:
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            else:
                entry.linkname = "../../outside"
                archive.addfile(entry)
    return stream.getvalue()


class StageBuildTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_binding_rejects_missing_hash_and_traversal_nonce(self):
        for values in (("a" * 63, COMMIT, NONCE), (FP, "main", NONCE), (FP, COMMIT, "../../live"), (FP, COMMIT, "UPPERCASE")):
            with self.assertRaises(BUILD.StageError):
                BUILD.validate_binding(*values)

    def test_resource_gate_accepts_exact_threshold_and_rejects_each_lower_bound(self):
        exact = {"disk_available_bytes":BUILD.MIN_DISK_BYTES,"memory_available_bytes":BUILD.MIN_MEMORY_BYTES}
        BUILD.resource_gate(exact)
        for key in exact:
            with self.assertRaisesRegex(BUILD.StageError,"BUILD_RESOURCE_GATE_FAILED"):
                BUILD.resource_gate({**exact,key:exact[key]-1})

    def test_memavailable_parses_only_exact_field_and_kib_units(self):
        self.assertTrue(BUILD.parse_mem_available("MemTotal: 999 kB\nMemAvailable: 3145728 kB\n") == BUILD.MIN_MEMORY_BYTES)
        for text in ("MemTotal: 999 kB", "MemAvailable: 3 GB", "MemAvailable: -1 kB", "MemAvailable: 1 kB\nMemAvailable: 2 kB"):
            with self.assertRaisesRegex(BUILD.StageError,"MEMORY_AVAILABLE_UNVERIFIED"):
                BUILD.parse_mem_available(text)

    def test_source_paths_reject_absolute_traversal_links_and_secrets(self):
        names = ("/frontend/a", "frontend/../infra/.env", "frontend//a", "frontend/./a", "C:\\frontend\\a",
                 "frontend/.env.production", "services/auth/private.pem", "services/learn/data/db.bin",
                 "frontend/node_modules/a.js", "frontend/.git/config", "docs/handoff/a.md", "frontend/session-cookies.txt",
                 "frontend/.npmrc", "services/auth/.aws/credentials", "frontend/private.pfx")
        for name in names:
            with self.assertRaises(BUILD.StageError):
                BUILD.safe_source_path(name)
        self.assertTrue(BUILD.safe_source_path("frontend/.dockerignore").name == ".dockerignore")

    def test_literal_private_material_and_nonplaceholder_template_are_rejected(self):
        with self.assertRaisesRegex(BUILD.StageError, "SOURCE_SECRET_MATERIAL_FORBIDDEN"):
            BUILD.reject_secret_material("services/auth/app/key.py", b"-----BEGIN " + b"PRIVATE KEY-----")
        with self.assertRaisesRegex(BUILD.StageError, "SOURCE_ENV_LITERAL_FORBIDDEN"):
            BUILD.reject_secret_material("frontend/.env.example", b"API_KEY=synthetic-unit-fixture-123\n")
        BUILD.reject_secret_material("frontend/.env.example", b"API_KEY=${API_KEY}\n")

    def test_context_requires_dockerignore_and_copy_sources(self):
        for missing in ("frontend/.dockerignore", "services/auth/docker-entrypoint.sh", "services/community/migrations/env.py", "frontend/package-lock.json"):
            data = context_payloads()
            del data[missing]
            with self.assertRaisesRegex(BUILD.StageError, "DOCKER_CONTEXT_INCOMPLETE"):
                BUILD.validate_context_payloads(data)
        data = context_payloads()
        data["services/auth/Dockerfile"] += b"COPY absent.py ./\n"
        with self.assertRaisesRegex(BUILD.StageError, "DOCKER_COPY_SOURCE_MISSING"):
            BUILD.validate_context_payloads(data)

    def test_shell_entrypoint_crlf_is_rejected_without_normalizing_source(self):
        data = context_payloads()
        original = data["services/auth/docker-entrypoint.sh"].replace(b"\n", b"\r\n")
        data["services/auth/docker-entrypoint.sh"] = original
        with self.assertRaisesRegex(BUILD.StageError, "DOCKER_ENTRYPOINT_REQUIRES_LF"):
            BUILD.validate_context_payloads(data)
        self.assertTrue(data["services/auth/docker-entrypoint.sh"] == original)

    def test_dockerignore_missing_secret_exclusion_is_rejected(self):
        data = context_payloads()
        data["services/learn/.dockerignore"] = data["services/learn/.dockerignore"].replace(b"*.pem\n", b"")
        with self.assertRaisesRegex(BUILD.StageError, "DOCKERIGNORE_REQUIRED_EXCLUSIONS_MISSING"):
            BUILD.validate_context_payloads(data)

    def test_tar_roundtrip_preserves_raw_bytes_modes_and_dockerignore(self):
        data = context_payloads()
        data["frontend/src/app/layout.tsx"] += b"// raw CRLF kept\r\n"
        modes = {name:0o644 for name in data}
        modes["services/auth/docker-entrypoint.sh"] = 0o755
        archive, rows = BUILD.make_tar(data, modes)
        destination = self.root / "source"
        destination.mkdir()
        value = BUILD.extract_verified_tar(archive, destination, rows)
        self.assertTrue(value["file_count"] == len(data))
        self.assertTrue(all((destination / name).read_bytes() == payload for name,payload in data.items()))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
            member = stream.getmember("services/auth/docker-entrypoint.sh")
            self.assertTrue(member.mode == 0o755 and member.mtime == 0 and member.uid == 0)

    def test_tar_symlink_hardlink_device_and_traversal_never_escape(self):
        row = {"path":"frontend/file.txt", "bytes":1, "sha256":BUILD.digest(b"x"), "mode":0o644}
        cases = (("frontend/file.txt", tarfile.SYMTYPE), ("frontend/file.txt", tarfile.LNKTYPE),
                 ("frontend/file.txt", tarfile.CHRTYPE), ("../../outside", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE))
        for index, (name, kind) in enumerate(cases):
            destination = self.root / str(index)
            destination.mkdir()
            with self.assertRaises(BUILD.StageError):
                BUILD.extract_verified_tar(archive_members([(name,b"x",kind)]), destination, [row])
        self.assertTrue(not (self.root / "outside").exists())

    def test_tar_duplicate_and_hash_mismatch_are_rejected(self):
        row = {"path":"frontend/file.txt", "bytes":1, "sha256":BUILD.digest(b"x"), "mode":0o644}
        for index, entries in enumerate(([("frontend/file.txt",b"x",tarfile.REGTYPE)] * 2,
                                        [("frontend/file.txt",b"y",tarfile.REGTYPE)])):
            destination = self.root / str(index)
            destination.mkdir()
            with self.assertRaises(BUILD.StageError):
                BUILD.extract_verified_tar(archive_members(entries), destination, [row])

    def test_archive_missing_and_trailing_members_are_rejected(self):
        data = context_payloads()
        archive, rows = BUILD.make_tar(data, {name:0o644 for name in data})
        for index, (payload, expected) in enumerate(((archive, rows + [{"path":"frontend/missing.txt","bytes":0,"sha256":BUILD.digest(b""),"mode":0o644}]),
                                                    (archive + b"unexpected trailer", rows))):
            destination = self.root / str(index)
            destination.mkdir()
            with self.assertRaises(BUILD.StageError):
                BUILD.extract_verified_tar(payload, destination, expected)

    def test_frontend_args_reject_credentials_queries_and_unknown_mode(self):
        for key, value in (("NEXT_PUBLIC_SITE_URL", "https://user:synthetic@example.invalid/ctf"),
                           ("NEXT_PUBLIC_SITE_URL", "https://example.invalid/?credential=fixture"),
                           ("NEXT_PUBLIC_ENV_PROXY_BASE", "file:///tmp/proxy"),
                           ("NEXT_PUBLIC_ENV_ACCESS_MODE", "unknown")):
            with self.assertRaises(BUILD.StageError):
                BUILD.validate_frontend_args({**ARGS,key:value})
        self.assertTrue(BUILD.validate_frontend_args(ARGS) == ARGS)

    def test_live_frontend_arguments_require_exact_compose_match(self):
        environment = [key + "=" + value for key,value in ARGS.items()]
        self.assertTrue(BUILD.frontend_args_from_inspection(environment, ARGS) == ARGS)
        with self.assertRaisesRegex(BUILD.StageError, "LIVE_FRONTEND_BUILD_ARGS_MISMATCH"):
            BUILD.frontend_args_from_inspection(environment, {**ARGS,"NEXT_PUBLIC_SITE_URL":"https://other.invalid/ctf"})
        with self.assertRaisesRegex(BUILD.StageError, "LIVE_FRONTEND_ENV_AMBIGUOUS"):
            BUILD.frontend_args_from_inspection(environment + [environment[0]], ARGS)
        self.assertEqual(BUILD.frontend_args_from_inspection(environment[:-1],ARGS),ARGS)

    def test_production_env_may_omit_build_args_but_partial_mismatch_is_rejected(self):
        self.assertEqual(BUILD.frontend_args_from_inspection(["NODE_ENV=production"],ARGS),ARGS)
        observation = BUILD.frontend_args_observation(["NODE_ENV=production"])
        self.assertEqual(observation["runtime_env_present"],[])
        self.assertEqual(observation["runtime_env_missing"],sorted(BUILD.FRONTEND_KEYS))
        self.assertFalse(observation["prior_compiled_args_verified"])
        with self.assertRaisesRegex(BUILD.StageError,"LIVE_FRONTEND_BUILD_ARGS_MISMATCH"):
            BUILD.frontend_args_from_inspection(["NEXT_PUBLIC_ENV_ACCESS_MODE=direct"],{**ARGS,"NEXT_PUBLIC_ENV_ACCESS_MODE":"proxy"})

    def test_unknown_sensitive_source_label_value_is_not_returned(self):
        with self.assertRaisesRegex(BUILD.StageError,"LIVE_SOURCE_LABEL_INVALID"):
            BUILD.safe_image_source_labels({BUILD.SOURCE_LABELS[0]:"synthetic-sensitive-value"})
        self.assertTrue(set(BUILD.safe_image_source_labels({})) == set(BUILD.SOURCE_LABELS))

    def test_build_commands_have_only_serial_contexts_unique_tags_and_no_activation(self):
        commands = BUILD.build_commands(Path("/new/source"), BINDING, ARGS)
        self.assertTrue(all(command.count("--pull") == 1 for _, command in commands))
        self.assertTrue(all(command[command.index("--network") + 1] == "host" for _,command in commands))
        self.assertTrue([service for service,_ in commands] == ["auth","learn","community","frontend"])
        forbidden = {"compose", "up", "restart", "prune", "rmi", "system", "rm", "exec"}
        self.assertTrue(all(not (set(command) & forbidden) for _,command in commands))
        tags = [command[command.index("--tag")+1] for _,command in commands]
        self.assertTrue(len(set(tags)) == 4 and all(COMMIT + "-" + NONCE in tag for tag in tags))
        self.assertTrue("--target" in commands[-1][1] and "--build-arg" not in commands[0][1])

    def test_serial_build_executor_stops_on_first_failure_and_keeps_partial_images(self):
        events, progress = [], []
        commands = BUILD.build_commands(Path("/new/source"), BINDING, ARGS)
        def run(service,command):
            events.append(service)
            return 1 if service == "learn" else 0
        def image(service,tag):
            return {"service":service,"tag":tag,"image_id":"sha256:" + "c" * 64}
        def update(service,completed):
            progress.append((service,len(completed)))
        with self.assertRaisesRegex(BUILD.StageError, "IMAGE_BUILD_FAILED"):
            BUILD.execute_serial_builds(commands,run,image,update)
        self.assertTrue(events == ["auth","learn"] and progress[-1] == ("learn",1))

    def test_remote_program_is_compileable_and_tar_uses_stdin_not_shell_extraction(self):
        program = BUILD.remote_program()
        compile(program, "<unit-remote-program>", "exec")
        command, payload = BUILD.execution_payload({"binding":BINDING}, b"tar-unit-fixture")
        header, archive = payload.split(b"\n",1)
        request = json.loads(header)
        self.assertTrue(base64.b64decode(request["program"]).decode() == program)
        self.assertTrue(archive == b"tar-unit-fixture" and "python3 -c" in command and len(command) < 2000)
        self.assertTrue("extractall" not in program and "docker-compose up" not in program)

    def _remote_execution_fixture(self, fail_service=None, frontend_only=False, learn_only=False, unreadable=False, alf_contract=None):
        data = context_payloads()
        if frontend_only:
            data = {name:value for name,value in data.items() if name.startswith("frontend/")}
        if learn_only:
            data = {name:value for name,value in data.items() if name.startswith("services/learn/")}
        if alf_contract is not None:
            data = {name:value for name,value in data.items() if not name.startswith("services/community/")}
        archive, rows = BUILD.make_tar(data,{name:0o644 for name in data},contexts=BUILD.selected_contexts(frontend_only=frontend_only,learn_only=learn_only,auth_learn_frontend=alf_contract is not None))
        namespace = {}
        program = BUILD.remote_program().rsplit("remote_main()",1)[0]
        exec(compile(program,"<pure-remote-fixture>","exec"),namespace)
        release = self.root / "release"
        release.mkdir(); (release / "source").mkdir(); (release / "private").mkdir()
        request = {"binding":BINDING,"files":rows,"tar_bytes":len(archive),"tar_sha256":BUILD.digest(archive),"source_file_count":len(rows),"preflight":None,"frontend_only":frontend_only,"backend_baseline":None}
        protected_names = ("infra/.env","infra/docker-compose.yml","infra/docker-compose.prod.yml","infra/caddy/Caddyfile","services/auth/jwt_private.pem","services/auth/jwt_public.pem")
        before = {"project":"ctf_web","protected_key_paths":{"auth_jwt_private_key":"/home/deploy/CTF_Web/services/auth/jwt_private.pem","platform_jwt_public_key":"/home/deploy/CTF_Web/services/auth/jwt_public.pem"},
                  "containers":[{"service":"frontend","container_id":"d"*64,"image_id":"sha256:"+"e"*64,"running":True,"pid":123,"source_labels":dict.fromkeys(BUILD.SOURCE_LABELS)}],
                  "protected_files":[{"path":"/home/deploy/CTF_Web/"+name,"bytes":1,"sha256":BUILD.digest(b"x")} for name in protected_names],
                  "frontend_args":ARGS,"frontend_args_sha256":BUILD.digest(BUILD.canonical(ARGS)),"frontend_args_observation":BUILD.frontend_args_observation([])}
        images = {name:"sha256:"+str(index)*64 for index,name in enumerate(("auth","learn","community"),1)}
        services = {name+"-service":name for name in images}
        backend_labels = dict.fromkeys(BUILD.SOURCE_LABELS)
        if frontend_only or learn_only or alf_contract is not None:
            old_binding = BUILD.validate_binding("2"*64,"1"*40,"baseline-nonce-123")
            labels = dict(zip(BUILD.SOURCE_LABELS,(old_binding["commit"],old_binding["source_fingerprint"],old_binding["release_nonce"])))
            backend_labels = labels
            request["backend_baseline"] = {"binding":old_binding,"images":images,"managed_files":[],"protected_files":before["protected_files"],"service_images":services}
        if learn_only:
            frontend_binding = BUILD.validate_binding("4"*64,"3"*40,"frontend-nonce-123")
            before["containers"][0]["source_labels"] = dict(zip(BUILD.SOURCE_LABELS,(frontend_binding["commit"],frontend_binding["source_fingerprint"],frontend_binding["release_nonce"])))
            retained = {name:images[name] for name in ("auth","community")}
            retained["frontend"] = before["containers"][0]["image_id"]
            request.update({"learn_only":True,"baseline_chain":True,"backend_baseline":{
                "binding":frontend_binding,"images":retained,"managed_files":[],"protected_files":before["protected_files"],
                "service_images":{"auth-service":"auth","community-service":"community","frontend":"frontend"},
                "image_bindings":{"auth":old_binding,"community":old_binding,"frontend":frontend_binding},
                "expected_heads":{"auth":"20261013_005","learn":"20261013_028","community":"20261013_005"}}})
        if alf_contract is not None:
            request.update({"auth_learn_frontend": True, "release_phase": "auth-learn-frontend-v1", "alf_contract": alf_contract,
                            "baseline_chain": True, "preflight": {"alf_contract": alf_contract}, "backend_baseline": {
                "binding": old_binding, "images": {"community": images["community"]}, "managed_files": [],
                "protected_files": before["protected_files"], "service_images": {"community-service": "community"},
                "image_bindings": {"community": old_binding}, "expected_heads": alf_contract["previous_heads"]}})
        before["containers"].extend({"service":service,"container_id":str(index)*64,"image_id":images[family],"running":True,"pid":index,"source_labels":backend_labels} for index,(service,family) in enumerate(services.items(),1))
        before["containers"].sort(key=lambda row: (row["service"], row["container_id"]))
        namespace.update({"REQUEST":request,"live_snapshot":lambda:before,"new_release_directory":lambda nonce:release})
        if alf_contract is not None:
            request["preflight"].update(protected_files=before["protected_files"], containers=before["containers"])
        self.remote_request = request
        namespace["resource_snapshot"] = lambda:{"disk_available_bytes":BUILD.MIN_DISK_BYTES,"memory_available_bytes":BUILD.MIN_MEMORY_BYTES}
        built, modes = [], []
        def run(command,**kwargs):
            if command[:3] == ["docker","image","inspect"]:
                return SimpleNamespace(returncode=1)
            if command[:2] == ["docker","build"]:
                tag = command[command.index("--tag")+1]
                service = tag.split("/",1)[1].split(":",1)[0]
                built.append(service)
                kwargs["stdout"].write(b"synthetic build log\n")
                return SimpleNamespace(returncode=1 if service == fail_service else 0)
            raise AssertionError("REAL_COMMAND_FORBIDDEN")
        def output(command):
            if command[:2] == ["docker", "run"]:
                self.assertTrue("--read-only" in command and command[command.index("--network") + 1] == "none")
                self.assertEqual(command[command.index("--user") + 1], "1001:1001")
                return json.dumps({"uid": 1001, "checked": len(json.loads(command[-1])), "inaccessible": 1 if unreadable else 0}).encode()
            self.assertTrue(command[:3] == ["docker","image","inspect"])
            return json.dumps([{"Id":"sha256:"+"f"*64,"Config":{"Labels":{
                "org.opencontainers.image.revision":COMMIT,"io.ctf-web.source-fingerprint":FP,"io.ctf-web.release-nonce":NONCE,
                BUILD.FRONTEND_ARGS_LABEL:BUILD.digest(BUILD.canonical(ARGS))}}}]).encode()
        namespace["command_output"] = output
        original_open = namespace["os"].open
        def private_open(path,flags,mode):
            modes.append(mode)
            return original_open(path,flags,mode)
        with patch.object(namespace["subprocess"],"run",side_effect=run), \
                patch.object(namespace["os"],"open",side_effect=private_open), \
                patch.object(namespace["sys"],"stdin",SimpleNamespace(buffer=io.BytesIO(archive))), \
                patch.object(namespace["sys"],"stdout",new_callable=io.StringIO) as stdout:
            namespace["remote_main"]()
        return json.loads(stdout.getvalue()),built,modes,release

    def test_frontend_only_remote_simulation_builds_exactly_one_context(self):
        result,built,modes,release = self._remote_execution_fixture(frontend_only=True)
        self.assertTrue(result["status"] == "built-not-activated" and result["frontend_only"] is True)
        self.assertTrue(built == ["frontend"] and len(result["completed_images"]) == 1 and len(modes) == 5)
        self.assertTrue(not (release/"source/services").exists() and set(result["backend_baseline"]["images"]) == {"auth","learn","community"})

    def test_learn_only_remote_simulation_keeps_mixed_auth_community_frontend_bindings(self):
        result,built,modes,release = self._remote_execution_fixture(learn_only=True)
        self.assertEqual(result["status"],"built-not-activated")
        self.assertEqual(built,["learn"])
        self.assertTrue(result["learn_only"])
        self.assertEqual(set(result["retained_baseline"]["images"]),{"auth","community","frontend"})
        self.assertNotEqual(result["retained_baseline"]["image_bindings"]["auth"],result["retained_baseline"]["image_bindings"]["frontend"])
        self.assertFalse((release/"source/frontend").exists())
        self.assertFalse((release/"source/services/auth").exists())
        self.assertEqual(result["live_before"],result["live_after"])
        result.pop("remote_receipt")
        BUILD.validate_remote_receipt(result,self.remote_request)

    def test_frontend_only_checks_backend_labels_and_live_file_hash(self):
        root = self.root / "live"
        path = root / "services/auth/app/main.py"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unchanged\n")
        labels = dict(zip(BUILD.SOURCE_LABELS,(COMMIT,FP,NONCE)))
        row = {"service":"auth-service","running":True,"image_id":"sha256:"+"c"*64,"source_labels":labels}
        snapshot = {"protected_files":[],"containers":[row]}
        baseline = {"protected_files":[],"binding":BINDING,"images":{"auth":row["image_id"]},"service_images":{"auth-service":"auth"},
                    "managed_files":[{"path":"services/auth/app/main.py","bytes":10,"sha256":BUILD.digest(b"unchanged\n")}]}
        BUILD.check_backend_baseline(snapshot,baseline,root)
        path.write_bytes(b"changed\n")
        with self.assertRaisesRegex(BUILD.StageError,"FRONTEND_ONLY_BACKEND_FILE_CHANGED"):
            BUILD.check_backend_baseline(snapshot,baseline,root)
        row["source_labels"] = {}
        with self.assertRaisesRegex(BUILD.StageError,"FRONTEND_ONLY_BACKEND_IMAGE_BASELINE_CHANGED"):
            BUILD.check_backend_baseline(snapshot,baseline,root)

    def test_remote_protected_permission_error_uses_shared_bounded_fallback(self):
        namespace = {}
        exec(compile(BUILD.remote_program().rsplit("remote_main()",1)[0],"<pure-protected-fixture>","exec"),namespace)
        helpers = namespace["_PROTECTED"]
        def denied(path,name):
            raise PermissionError()
        helpers["file_row"] = denied
        helpers["protected_key_bytes"] = lambda path:b"synthetic-key-fixture"
        result = helpers["protected_row"](Path("/home/deploy/CTF_Web/services/auth/jwt_private.pem"))
        self.assertTrue(result["sha256"] == BUILD.digest(b"synthetic-key-fixture") and set(result) == {"path","bytes","sha256"})

    def test_complete_remote_program_simulation_builds_serially_with_private_logs(self):
        result,built,modes,release = self._remote_execution_fixture()
        self.assertTrue(result["status"] == "built-not-activated" and built == ["auth","learn","community","frontend"])
        self.assertTrue(len(modes) == 14 and all(mode == 0o600 for mode in modes))
        self.assertTrue((release/"build-receipt.json").is_file() and len(result["completed_images"]) == 4)
        self.assertTrue(all(set(result[key]) == {"project","protected_key_paths","containers","protected_files","frontend_args_sha256","frontend_args_observation"} for key in ("live_before","live_after")))
        self.assertTrue(ARGS["NEXT_PUBLIC_SITE_URL"] not in json.dumps(result))
        self.assertTrue(result["runtime_readability_verified"])

    def test_unreadable_runtime_stops_before_next_image_or_activation(self):
        result,built,_,_ = self._remote_execution_fixture(unreadable=True)
        self.assertEqual(result["error"], "IMAGE_RUNTIME_READABILITY_FAILED")
        self.assertEqual(result["status"], "partial-unverified")
        self.assertEqual(built, ["auth"])
        self.assertEqual(result["completed_images"], [])

    def test_runtime_probe_cannot_accept_root_or_partial_path_checks(self):
        for change in ({"uid": 0}, {"checked": 1}, {"checked": True}, {"inaccessible": 1}):
            with self.assertRaisesRegex(BUILD.StageError, "RUNTIME_READABILITY"):
                BUILD.validate_runtime_probe({"uid":1001,"checked":10,"inaccessible":0, **change}, 10)
        BUILD.validate_runtime_probe({"uid":1001,"checked":10,"inaccessible":0}, 10)

    def test_production_dockerfiles_normalize_cached_directory_modes(self):
        for service, context in BUILD.CONTEXTS:
            content = (BUILD.ops.SOURCE / context / "Dockerfile").read_text(encoding="utf-8")
            command = "RUN find /app -type d -exec chmod 0755 {} +" if service == "frontend" else (
                "RUN find /app/app /app/migrations -type d -exec chmod 0755 {} +")
            self.assertIn(command, content)
            self.assertGreater(content.index(command), content.rindex("COPY "))
            if "USER appuser" in content:
                self.assertLess(content.index(command), content.index("USER appuser"))

    def test_staged_context_directories_are_searchable_by_nonroot_image_users(self):
        data = context_payloads()
        archive, rows = BUILD.make_tar(data, {name:0o644 for name in data})
        destination = self.root / "source"; destination.mkdir()
        original = Path.chmod
        calls = []
        def chmod(path, mode, *args, **kwargs):
            calls.append((path, mode))
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, "chmod", chmod):
            BUILD.extract_verified_tar(archive, destination, rows)
        directories = {parent for name in data for parent in (destination / name).parents
                       if parent.is_relative_to(destination) and parent != destination}
        self.assertEqual({path for path, mode in calls if path in directories and mode == 0o755}, directories)
        self.assertNotIn(destination, {path for path, _ in calls})

    def test_complete_remote_program_simulation_keeps_partial_receipt_on_failure(self):
        result,built,modes,release = self._remote_execution_fixture(fail_service="learn")
        self.assertTrue(result["status"] == "partial-unverified" and result["error"] == "IMAGE_BUILD_FAILED")
        self.assertTrue(built == ["auth","learn"] and len(result["completed_images"]) == 1 and len(modes) == 7)
        self.assertTrue((release/"private/build-learn.log").is_file() and not (release/"private/build-community.log").exists())
        self.assertTrue((release/"build-request.json").is_file() and (release/"private/completed-1.json").is_file())
        self.assertTrue(result["activation_performed"] is False and result["database_mutation_performed"] is False)

    def test_remote_receipt_rejects_unknown_secret_fields(self):
        request = {"binding":BINDING,"files":[],"tar_sha256":FP,"source_file_count":1}
        result = {"schema_version":1,"kind":"renewal-stage-build","binding":BINDING,"status":"partial-unverified",
                  "activation_performed":False,"database_mutation_performed":False,"images_pruned":False,
                  "live_files_replaced":False,"completed_images":[],"password":"synthetic-unit-fixture"}
        with self.assertRaisesRegex(BUILD.StageError,"REMOTE_RECEIPT_BINDING_MISMATCH"):
            BUILD.validate_remote_receipt(result,request)

    def test_success_requires_nonempty_complete_unique_live_evidence(self):
        result,_,_,_ = self._remote_execution_fixture()
        for field in ("containers","protected_files"):
            with self.subTest(field=field):
                bad = json.loads(json.dumps(result))
                bad["live_before"][field] = []
                bad["live_after"][field] = []
                with self.assertRaises(BUILD.StageError):
                    BUILD.validate_remote_receipt(bad,self.remote_request)
        for field in ("containers","protected_files"):
            bad = json.loads(json.dumps(result))
            bad["live_before"][field].append(bad["live_before"][field][0])
            bad["live_after"] = bad["live_before"]
            with self.assertRaises(BUILD.StageError):
                BUILD.validate_remote_receipt(bad,self.remote_request)

    def test_partial_receipt_rejects_unexpected_nested_data_and_service(self):
        result,_,_,_ = self._remote_execution_fixture(fail_service="learn")
        for field,bad_value in (("stage",{"Env":["synthetic"]}),
                                ("backend_baseline",{"Env":["synthetic"]}),
                                ("current_service","synthetic unexpected service")):
            with self.subTest(field=field):
                bad = {**result,field:bad_value}
                with self.assertRaises(BUILD.StageError):
                    BUILD.validate_remote_receipt(bad,self.remote_request)

    def test_execute_requires_preflight_pair_before_source_or_ssh(self):
        with patch.object(BUILD.ops,"manifest",side_effect=AssertionError("SOURCE_FORBIDDEN")), \
                patch.object(BUILD.ops,"ssh",side_effect=AssertionError("SSH_FORBIDDEN")):
            with self.assertRaisesRegex(BUILD.StageError,"EXECUTE_FRESH_PREFLIGHT_REQUIRED"):
                BUILD.main(["--source-fingerprint",FP,"--commit",COMMIT,"--release-nonce",NONCE,"--execute"])

    def test_optional_preflight_binds_nonce_fingerprint_count_target_and_hash(self):
        evidence = self.root / "evidence"
        evidence.mkdir()
        path = evidence / "preflight.json"
        from test_release_backup import fixtures
        plan = fixtures()[2]
        plan.update({"nonce":NONCE,"source_fingerprint":FP,"source_file_count":100,"created_at":datetime.now(timezone.utc).isoformat()})
        payload = BUILD.canonical(plan)
        path.write_bytes(payload)
        with patch.object(BUILD.ops,"TASK",self.root):
            result = BUILD.load_preflight(path,BUILD.digest(payload),BINDING,100)
            self.assertTrue(result["plan_sha256"] == BUILD.digest(payload))
            with self.assertRaisesRegex(BUILD.StageError,"PREFLIGHT_SOURCE_BINDING_CHANGED"):
                BUILD.load_preflight(path,BUILD.digest(payload),BINDING,101)
            with self.assertRaisesRegex(BUILD.StageError,"PREFLIGHT_HASH_CHANGED"):
                BUILD.load_preflight(path,"0"*64,BINDING,100)
            for created_at in (None,"2000-01-01T00:00:00+00:00"):
                stale = dict(plan)
                if created_at is None:
                    stale.pop("created_at")
                else:
                    stale["created_at"] = created_at
                payload = BUILD.canonical(stale)
                path.write_bytes(payload)
                with self.assertRaisesRegex(BUILD.StageError,"PREFLIGHT_PLAN_INVALID"):
                    BUILD.load_preflight(path,BUILD.digest(payload),BINDING,100)

    def _checkout_fixture(self, *, blob_payload=None, mode="100644", status=b""):
        source, checkout = self.root / "source", self.root / "checkout"
        source.mkdir(); checkout.mkdir(); (checkout / ".git").mkdir()
        data = {"README.md":b"project overview\n", "frontend/package.json":b"{}\n"}
        rows, blobs, tree = [], {}, []
        for name,payload in data.items():
            rows.append({"path":name,"bytes":len(payload),"sha256":BUILD.digest(payload)})
            for root in (source,checkout):
                (root/name).parent.mkdir(parents=True,exist_ok=True)
                (root/name).write_bytes(payload)
            blob = blob_payload if blob_payload is not None and name == "frontend/package.json" else payload
            oid = hashlib.sha1(blob).hexdigest()
            blobs[oid] = blob
            tree.append(f"{mode} blob {oid}\t{name}".encode() + b"\0")
        fp = BUILD.manifest_fingerprint(rows)
        manifest, binding = {"files":rows,"fingerprint":fp}, BUILD.validate_binding(fp,COMMIT,NONCE)
        def reader(path,*args,payload=None):
            if args == ("rev-parse","HEAD"): return COMMIT.encode() + b"\n"
            if args[0] == "config": return b"false\n"
            if args[0] == "status": return status
            if args[0] == "ls-tree": return b"".join(tree)
            if args[0] == "cat-file":
                return b"".join(oid + b" blob " + str(len(blobs[oid.decode()])).encode() + b"\n" + blobs[oid.decode()] + b"\n" for oid in payload.splitlines())
            raise AssertionError("UNEXPECTED_GIT_READ")
        return source,checkout,manifest,binding,reader

    def test_every_manifest_file_is_bound_to_source_copy_and_committed_blob(self):
        source,checkout,manifest,binding,reader = self._checkout_fixture()
        modes = BUILD.bind_checkout(checkout,source,manifest,binding,git_reader=reader)
        self.assertTrue(set(modes) == {"README.md","frontend/package.json"})

    def test_committed_blob_eol_difference_is_not_silently_normalized(self):
        source,checkout,manifest,binding,reader = self._checkout_fixture(blob_payload=b"{}\r\n")
        with self.assertRaisesRegex(BUILD.StageError, "SOURCE_COMMIT_BLOB_MISMATCH"):
            BUILD.bind_checkout(checkout,source,manifest,binding,git_reader=reader)

    def test_committed_symlink_and_dirty_checkout_are_rejected(self):
        source,checkout,manifest,binding,reader = self._checkout_fixture(mode="120000")
        with self.assertRaisesRegex(BUILD.StageError, "SOURCE_COMMIT_FILE_MISSING_OR_LINKED"):
            BUILD.bind_checkout(checkout,source,manifest,binding,git_reader=reader)
        with patch.object(BUILD, "git_bytes", side_effect=AssertionError("REAL_GIT_FORBIDDEN")):
            dirty = lambda path,*args,**kwargs: b" M source\n" if args[0] == "status" else reader(path,*args,**kwargs)
            with self.assertRaisesRegex(BUILD.StageError, "CLEAN_CHECKOUT_NOT_COMMITTED"):
                BUILD.bind_checkout(checkout,source,manifest,binding,git_reader=dirty)

    def test_default_main_never_calls_ssh_or_builds_archive(self):
        data = context_payloads()
        data["scripts/renewal/stage_build_release.py"] = (RENEWAL/"stage_build_release.py").read_bytes()
        data["scripts/renewal/ops.py"] = (RENEWAL/"ops.py").read_bytes()
        data["scripts/renewal/release_preflight.py"] = (RENEWAL/"release_preflight.py").read_bytes()
        data["scripts/renewal/verify_release.py"] = (RENEWAL/"verify_release.py").read_bytes()
        source = self.root / "source"
        source.mkdir()
        for name,payload in data.items():
            (source/name).parent.mkdir(parents=True,exist_ok=True)
            (source/name).write_bytes(payload)
        rows = [{"path":name,"bytes":len(payload),"sha256":BUILD.digest(payload)} for name,payload in sorted(data.items())]
        fp = BUILD.manifest_fingerprint(rows)
        manifest = {"fingerprint":fp,"files":rows}
        with patch.object(BUILD.ops,"SOURCE",source), patch.object(BUILD.ops,"manifest",return_value=manifest), \
                patch.object(BUILD,"bind_checkout",return_value={name:0o644 for name in data}), \
                patch.object(BUILD.ops,"ssh",side_effect=AssertionError("SSH_FORBIDDEN")), \
                patch.object(BUILD,"make_tar",side_effect=AssertionError("ARCHIVE_FORBIDDEN")), \
                patch("sys.stdout",new_callable=io.StringIO) as output:
            status = BUILD.main(["--source-fingerprint",fp,"--commit",COMMIT,"--release-nonce",NONCE])
        value = json.loads(output.getvalue())
        self.assertTrue(status == 0 and value["execute"] is False and value["activation_performed"] is False)


if __name__ == "__main__":
    unittest.main()
