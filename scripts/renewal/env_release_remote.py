"""Remote implementation injected by env_release; never run as a standalone file.

The pre-existing read-only inspection/strict SSH transport is supplied by the
caller. All private files belong to one fresh D2 nonce. Existing release tools,
Compose, environments, keys, Redis configuration and challenge data are not edited.
"""


def private_json(path, value):
    require(path.is_relative_to(Path(REMOTE_ROOT)) and not any(item.is_symlink() for item in (path, *path.parents)), "PRIVATE_PATH_LINK_OR_ESCAPE")
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())


def bounded_command(command, log=None, limit=120):
    if log is None:
        return run(command, limit=limit)
    with log.open("xb") as output:
        os.chmod(log, 0o600)
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=limit)
    require(result.returncode == 0, "COMMAND_FAILED_PRIVATE_LOG_RETAINED")


def source_labels(binding):
    return {"org.opencontainers.image.revision": binding["commit"], "io.ctf-web.source-fingerprint": binding["source_fingerprint"],
            "io.ctf-web.release-nonce": binding["release_nonce"]}


def details(identifier):
    value = json.loads(run(["docker", "inspect", identifier]))
    require(len(value) == 1, "CONTAINER_IDENTITY")
    if value[0]["Id"] != identifier:
        nonce = REQUEST["plan"]["binding"]["release_nonce"]
        require(identifier == "ctf-env-startup-" + nonce and value[0]["Config"].get("Labels", {}).get("io.ctf-web.env-test-nonce") == nonce, "OWNED_CONTAINER_NAME_IDENTITY")
    return value[0]


def normalized_environment(entries):
    require(isinstance(entries, list), "ENVIRONMENT_ENTRY_LIST_REQUIRED")
    values = {}
    for entry in entries:
        require(isinstance(entry, str) and "=" in entry, "ENVIRONMENT_ENTRY_INVALID")
        name, value = entry.split("=", 1)
        require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in values, "ENVIRONMENT_NAME_OR_DUPLICATE")
        values[name] = value
    return [name + "=" + values[name] for name in sorted(values)]


def runtime_hash(value):
    config = value["Config"]
    hosts = value["HostConfig"]
    # Image/source labels are the only intended runtime differences.
    selected = {name: config.get(name) for name in ("User", "Env", "Cmd", "Entrypoint", "WorkingDir", "Healthcheck", "ExposedPorts")}
    selected["Env"] = normalized_environment(config.get("Env"))
    selected["host"] = {name: hosts.get(name) for name in ("NetworkMode", "Binds", "Mounts", "GroupAdd", "SecurityOpt", "CapAdd", "CapDrop", "ReadonlyRootfs", "PortBindings", "Memory", "NanoCpus", "Tmpfs", "ExtraHosts", "RestartPolicy", "Privileged", "PidMode", "IpcMode", "Devices", "LogConfig", "Sysctls", "Ulimits", "Dns")}
    selected["mounts"] = sorted([{name: item.get(name) for name in ("Type", "Source", "Destination", "RW")} for item in value.get("Mounts", [])], key=lambda item: item["Destination"])
    return payload_digest(canonical(selected))


def labs():
    ids = run(["docker", "ps", "-a", "--filter", "label=ctf-challenge=true", "--format", "{{.ID}}"]).decode().splitlines()
    if not ids:
        return []
    rows = []
    for value in json.loads(run(["docker", "inspect", *ids])):
        require(value["Config"].get("Labels", {}).get("ctf-challenge") == "true", "LAB_LABEL_CHANGED")
        rows.append({"container_id": value["Id"], "image_id": value["Image"], "state": {key: value["State"].get(key) for key in ("Running", "Status", "Pid", "StartedAt", "Restarting")},
                     "ports": value["NetworkSettings"].get("Ports"), "mounts": sorted([{key: item.get(key) for key in ("Type", "Source", "Destination", "RW")} for item in value.get("Mounts", [])], key=lambda item: item["Destination"])})
    return sorted(rows, key=lambda item: item["container_id"])


def snapshot():
    model = compose(); protected = protected_rows(model); current = containers()
    envs = [row for row in current if row["service"] == SERVICE]
    require(len(envs) == 1, "ENV_CONTAINER_AMBIGUOUS")
    target = details(envs[0]["container_id"])
    require(target["Config"].get("User") in {"1001", "1001:1001", "appuser"} and target["HostConfig"].get("NetworkMode") == "host", "ENV_RUNTIME_CONTRACT")
    settings = dict(item.split("=", 1) for item in target["Config"].get("Env", []) if "=" in item)
    redis = urllib.parse.urlsplit(settings.get("REDIS_URL", ""))
    require(redis.scheme == "redis" and redis.hostname in {"127.0.0.1", "localhost"} and redis.path == "/2"
            and settings.get("LISTEN_ADDR") == "127.0.0.1:8200", "ENV_LOOPBACK_DB2_CONTRACT")
    require(any(item["Destination"] == "/var/run/docker.sock" for item in target["Mounts"]), "ENV_SOCKET_MOUNT_REQUIRED")
    retained = {}
    for row in current:
        if row["service"] in RETAINED_SERVICES:
            configuration = details(row["container_id"])["Config"]
            labels = configuration.get("Labels", {}) or {}
            retained[row["service"]] = {key: labels.get(key) for key in ("org.opencontainers.image.revision", "io.ctf-web.source-fingerprint", "io.ctf-web.release-nonce")}
            if row["service"] in LEGACY_WORKERS:
                row["command"] = configuration.get("Cmd")
    postgres = singleton(current, "postgres")["container_id"]
    databases = {name: {key: row[key] for key in ("database", "head", "schema_sha256", "table_count")} for name, row in database_rows(postgres).items()}
    return {"containers": current, "code_files": canonical_file_rows(code_rows(protected)), "protected_files": protected, "labs": labs(), "databases": databases,
            "retained_labels": retained, "target_runtime_sha256": runtime_hash(target)}


def verify_retained(current, plan):
    contract = plan["retained_baseline"]["contract"]
    require(current["protected_files"] == contract["protected_files"], "RETAINED_PROTECTED_BASELINE")
    paths = {row["path"] for row in contract["managed_files"]}
    require(canonical_file_rows([row for row in current["code_files"] if row["path"] in paths])
            == canonical_file_rows(contract["managed_files"]), "RETAINED_MANAGED_SOURCE")
    for service, family in RETAINED_SERVICES.items():
        rows = [row for row in current["containers"] if row["service"] == service]
        require(len(rows) == 1 and rows[0]["running"] and rows[0]["image_id"] == contract["images"][family]
                and current["retained_labels"][service] == source_labels(contract["image_bindings"][family]), "RETAINED_IMAGE_PROVENANCE")
        require(rows[0]["health"] is None if service in LEGACY_WORKERS else rows[0]["health"] == "healthy", "RETAINED_READINESS_UNVERIFIED")
        if service in LEGACY_WORKERS:
            require(rows[0].get("command") == ["python", "-m", "app.workers." + service.replace("-", "_")], "RETAINED_LEGACY_WORKER_COMMAND")
    require(all(isinstance(row["head"], list) and len(row["head"]) == 1 for row in current["databases"].values()), "RETAINED_DATABASE_HEAD_SHAPE")
    require({name: row["head"][0] for name, row in current["databases"].items()} == contract["expected_heads"], "RETAINED_DATABASE_HEAD")


def release_directory(create=False):
    nonce = REQUEST["plan"]["binding"]["release_nonce"]
    base = Path(REMOTE_ROOT)
    for path in (base, *base.parents):
        require(not path.is_symlink(), "RELEASE_DIRECTORY_LINK")
    if create:
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = base / nonce
    if create:
        directory.mkdir(mode=0o700, exist_ok=False)
        private_json(directory / "plan.json", {"plan": REQUEST["plan"], "plan_sha256": REQUEST["plan_sha256"]})
    require(directory.is_dir() and not directory.is_symlink(), "RELEASE_DIRECTORY_MISSING")
    recorded = json.loads((directory / "plan.json").read_bytes())
    require(recorded == {"plan": REQUEST["plan"], "plan_sha256": REQUEST["plan_sha256"]}, "REMOTE_PLAN_CHANGED")
    return directory


def intent(directory, action, observation=None):
    path = directory / (action + ".intent.json")
    require(not path.exists() and not path.is_symlink(), "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
    value = {"action": action, "binding": REQUEST["plan"]["binding"], "plan_sha256": REQUEST["plan_sha256"], "created_at": time.time()}
    if observation is not None:
        value["observation"] = observation
    private_json(path, value)


def completed(directory, action):
    path = directory / (action + ".complete.json")
    require(path.is_file() and not path.is_symlink(), "PRIOR_PHASE_INCOMPLETE_" + action.upper())
    value = json.loads(path.read_bytes())
    require(value.get("status") == "passed" and value.get("binding") == REQUEST["plan"]["binding"]
            and value.get("plan_sha256") == REQUEST["plan_sha256"], "PRIOR_PHASE_UNBOUND")
    return value


def image_metadata(identifier):
    rows = json.loads(run(["docker", "image", "inspect", identifier])); require(len(rows) == 1 and rows[0]["Id"] == identifier, "IMAGE_IDENTITY")
    value = rows[0]
    return {"image_id": identifier, "bytes": value["Size"], "labels_sha256": payload_digest(canonical(value.get("Config", {}).get("Labels", {}) or {})),
            "repo_tags_sha256": payload_digest(canonical(sorted(value.get("RepoTags") or []))), "os": value["Os"], "architecture": value["Architecture"]}


def image_source(identifier, binding):
    value = json.loads(run(["docker", "image", "inspect", identifier]))[0]
    labels = value.get("Config", {}).get("Labels", {}) or {}
    require(all(labels.get(key) == item for key, item in source_labels(binding).items()), "IMAGE_SOURCE_LABELS")
    return value


def owned_remove(identifier, nonce):
    value = details(identifier)
    require(value["Config"].get("Labels", {}).get("io.ctf-web.env-test-nonce") == nonce, "OWNED_TEST_IDENTITY")
    if value["State"]["Running"]:
        bounded_command(["docker", "kill", "--signal", "SIGTERM", identifier])
        wait_stopped(identifier, value["Image"], seconds=15)
    bounded_command(["docker", "rm", identifier])


def owned_test_status(nonce):
    ids = run(["docker", "ps", "-a", "--no-trunc", "--filter", "label=io.ctf-web.env-test-nonce=" + nonce, "--format", "{{.ID}}"]).decode().splitlines()
    rows = []
    for identifier in ids:
        value = details(identifier)
        require(value["Config"].get("Labels", {}).get("io.ctf-web.env-test-nonce") == nonce, "OWNED_STATUS_LABEL")
        rows.append({"container_id": value["Id"], "image_id": value["Image"], "name": value["Name"], "running": value["State"]["Running"], "status": value["State"]["Status"]})
    return rows


def go_source_evidence(directory, builder, nonce):
    records = {}; graph = None
    for name, arguments in (("modules", ["-m", "-json", "all"]), ("imports", ["-deps", "-f", "{{.ImportPath}}", "./cmd/main.go"])):
        log = directory / ("go-" + name + ".log")
        command = ["docker", "run", "--rm", "--name", "ctf-env-go-" + name + "-" + nonce, "--label", "io.ctf-web.env-test-nonce=" + nonce,
                   "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "192",
                   "--memory", "1g", "--cpus", "1", "--user", "1001:1001", "--tmpfs", "/tmp:uid=1001,gid=1001,mode=0700,size=512m",
                   "--env", "GOTOOLCHAIN=local", "--env", "GOTELEMETRY=off", "--env", "GOENV=off", "--env", "GOPROXY=off",
                   "--env", "GOCACHE=/tmp/go-cache", "--env", "GOTMPDIR=/tmp", "--env", "HOME=/tmp", "--entrypoint", "go",
                   builder, "list", "-mod=readonly", *arguments]
        bounded_command(command, log, limit=180)
        require(0 < log.stat().st_size <= 16 * 1024 ** 2, "GO_SOURCE_EVIDENCE_SIZE")
        content = log.read_bytes()
        records[name] = {"sha256": payload_digest(content), "bytes": len(content), "private_log": str(log)}
        if name == "modules":
            text = content.decode("utf-8"); decoder = json.JSONDecoder(); modules = []; offset = 0
            while offset < len(text):
                if text[offset].isspace():
                    offset += 1; continue
                row, offset = decoder.raw_decode(text, offset); modules.append(row)
            graph = validate_module_graph(modules)
        else:
            imports = content.decode("utf-8").splitlines()
            require(imports and all(re.fullmatch(r"[A-Za-z0-9_./+-]+", value) for value in imports), "GO_IMPORT_EVIDENCE_INVALID")
            records[name]["package_count"] = len(imports)
    return {"builder_image_id": builder, "compiler": COMPILER, "source_read_only": True, "offline": True, "module_graph": graph, "logs": records}


def linux_tests(directory, builder, redis_image, nonce):
    mem = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    require(mem >= 4 * 1024 ** 3, "LINUX_GO_TEST_RESOURCE_GATE")
    password = secrets.token_urlsafe(32)
    redis_name = "ctf-env-test-redis-" + nonce; test_name = "ctf-env-test-go-" + nonce
    envfile = directory / "test.env"
    with envfile.open("xb") as output:
        os.chmod(envfile, 0o600); output.write(("REDISCLI_AUTH=" + password + "\n").encode())
    identifier = None; test_id = None
    try:
        identifier = run(["docker", "run", "-d", "--name", redis_name, "--label", "io.ctf-web.env-test-nonce=" + nonce, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "64m", "--cpus", "0.25", "--user", "1001:1001",
            "--tmpfs", "/data:uid=1001,gid=1001,mode=0700,size=16m", "--env-file", str(envfile), "--entrypoint", "sh", redis_image,
            "-c", 'exec redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --protected-mode yes --requirepass "$REDISCLI_AUTH"']).decode().strip()
        info = ""
        for _ in range(40):
            try:
                info = run(["docker", "exec", identifier, "redis-cli", "--raw", "INFO", "server"], limit=5).decode(); break
            except Exception:
                time.sleep(0.25)
        values = dict(line.split(":", 1) for line in info.splitlines() if ":" in line)
        require(values.get("process_id", "").isdigit() and re.fullmatch(r"[0-9a-f]{40}", values.get("run_id", "")), "OWNED_REDIS_INFO")
        with envfile.open("ab") as output:
            output.write(("CTF_TEST_REDIS_INTEGRATION=owned\nCTF_TEST_REDIS_ADDR=127.0.0.1:6379\nCTF_TEST_REDIS_DB=15\nCTF_TEST_REDIS_NONCE=" + nonce
                + "\nCTF_TEST_REDIS_PROCESS_ID=" + values["process_id"] + "\nCTF_TEST_REDIS_RUN_ID=" + values["run_id"]
                + "\nCTF_GO_LIFECYCLE_REDIS_PASSWORD=" + password + "\nCGO_ENABLED=0\nGOTOOLCHAIN=local\nGOMEMLIMIT=512MiB\nGOMAXPROCS=1\nGOCACHE=/tmp/go-cache\nGOTMPDIR=/tmp\nHOME=/tmp\nGOPROXY=off\n").encode())
        test_id = run(["docker", "run", "-d", "--name", test_name, "--label", "io.ctf-web.env-test-nonce=" + nonce, "--network", "container:" + identifier,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "192", "--memory", "3g", "--memory-swap", "3g", "--cpus", "1",
            "--user", "1001:1001", "--tmpfs", "/tmp:exec,uid=1001,gid=1001,mode=0700,size=2g", "--env-file", str(envfile),
            "--entrypoint", "go", builder, "test", "-json", "-mod=readonly", "-count=1", "-p=1", "-timeout=180s", "./..."]).decode().strip()
        deadline = time.monotonic() + 300
        while details(test_id)["State"]["Running"] and time.monotonic() < deadline:
            time.sleep(1)
        state = details(test_id)["State"]
        require(not state["Running"], "OWNED_LINUX_TEST_TIMEOUT")
        logs = run(["docker", "logs", test_id], limit=30)
        with (directory / "linux-tests.jsonl").open("xb") as output:
            os.chmod(output.name, 0o600); output.write(logs)
        rows = [json.loads(line) for line in logs.splitlines() if line.startswith(b"{")]
        tests = [row for row in rows if row.get("Test") and row.get("Action") in {"pass", "fail", "skip"}]
        require(state["ExitCode"] == 0 and tests and not any(row["Action"] in {"fail", "skip"} for row in tests)
                and any("Integration" in row["Test"] for row in tests), "LINUX_GO_TEST_ACCEPTANCE")
        return {"compiler": COMPILER, "passed_tests": len(tests), "failed": 0, "skipped": 0, "owned_redis_database": 15,
                "host_or_production_network_used": False, "source_read_only": True, "redis_persistence": False}
    finally:
        if test_id is not None: owned_remove(test_id, nonce)
        if identifier is not None: owned_remove(identifier, nonce)


def binary_probe(directory, image, builder, binding):
    nonce = binding["release_nonce"]
    binary = bounded_command(runtime_probe_command(image, "ctf-env-probe-" + nonce))
    require(20 <= len(binary) <= 64 * 1024 ** 2 and binary[:4] == b"\x7fELF" and binary[5] == 1, "ELF_BINARY_REQUIRED")
    builder_binary = bounded_command(runtime_probe_command(builder, "ctf-env-builder-probe-" + nonce))
    require(builder_binary == binary, "BUILDER_RUNTIME_BINARY_MISMATCH")
    location = directory / "binary-proof"
    with location.open("xb") as output:
        os.chmod(location, 0o444); output.write(binary)
    version = run(["docker", "run", "--rm", "--name", "ctf-env-buildinfo-" + nonce, "--label", "io.ctf-web.env-test-nonce=" + nonce,
                  "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1001:1001",
                  "--pids-limit", "32", "--memory", "128m", "--cpus", "0.5", "--env", "GOTOOLCHAIN=local", "--env", "GOENV=off",
                  "--mount", "type=bind,source=" + str(location) + ",target=/binary,readonly", "--entrypoint", "go", builder, "version", "-m", "/binary"]).decode()
    require(re.search(r"(?m)^/binary: " + re.escape(COMPILER) + r"\s*$", version)
            and re.search(r"(?m)^\s*build\s+CGO_ENABLED=0\s*$", version)
            and re.search(r"(?m)^\s*build\s+GOOS=linux\s*$", version), "BINARY_GO_BUILD_INFO")
    startup_name = "ctf-env-startup-" + nonce
    command = ["docker", "run", "--name", startup_name, "--label", "io.ctf-web.env-test-nonce=" + nonce, "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "32", "--memory", "128m", "--cpus", "0.5", "--user", "1001:1001", "--env", "REDIS_URL=ctf-owned-invalid-redis-url",
               "--env", "INTERNAL_API_KEY=ctf-owned-not-a-production-secret", image]
    try:
        result = subprocess.run(command, capture_output=True, timeout=15)
        text = (result.stdout + result.stderr).decode("utf-8", "replace")
        with (directory / "isolated-startup.log").open("xb") as output:
            os.chmod(output.name, 0o600); output.write(result.stdout + result.stderr)
        require(result.returncode == 1 and re.search(r"(?i)redis[^\n]*(?:url|parse)|(?:url|parse)[^\n]*redis", text), "ISOLATED_REDIS_PARSE_FAILURE_REQUIRED")
        value = details(startup_name)
        require(value["Config"].get("User") == "1001:1001" and value["HostConfig"].get("NetworkMode") == "none" and not value["Mounts"], "ISOLATED_STARTUP_BOUNDARY")
        require(not value["State"]["Running"], "ISOLATED_STARTUP_NOT_EXITED")
        bounded_command(["docker", "rm", value["Id"]])
    except Exception:
        private_json(directory / "isolated-startup-uncertain.json", {"name": startup_name, "image_id": image, "owned": owned_test_status(nonce), "next": "status; no blind cleanup/retry"})
        raise
    proof = {"uid": 1001, "elf_magic": binary[:4].hex(), "elf_machine": struct.unpack_from("<H", binary, 18)[0], "binary_sha256": payload_digest(binary), "builder_binary_sha256": payload_digest(builder_binary),
             "compiler": COMPILER, "cgo_enabled": "0", "build_source": binding,
             "startup": {"exit_code": 1, "redis_failure_observed": True, "production_io": False, "timed_out": False, "health_success_claimed": False}}
    return validate_runtime_probe(proof, binding)


def extract_source(directory, rows, modes):
    payload = base64.b64decode(ARCHIVE, validate=True)
    require(len(payload) <= 512 * 1024 ** 2, "SOURCE_ARCHIVE_LIMIT")
    expected = {row["path"]: row for row in rows}
    source = directory / "source"; source.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        require(len(members) == len(expected) and {row.name for row in members} == set(expected), "SOURCE_ARCHIVE_SET")
        for member in members:
            require(member.isfile() and member.name.startswith(PREFIX) and ".." not in pathlib.PurePosixPath(member.name).parts, "SOURCE_ARCHIVE_PATH")
            content = archive.extractfile(member).read(); row = expected[member.name]
            require(len(content) == row["bytes"] and payload_digest(content) == row["sha256"], "SOURCE_ARCHIVE_HASH")
            path = source / member.name; path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output: output.write(content)
            os.chmod(path, modes[member.name])
    for folder, directories, _files in os.walk(source):
        if Path(folder) != source: os.chmod(folder, 0o755)
        for name in directories: os.chmod(Path(folder) / name, 0o755)
    return source


def build_phase(directory, plan):
    before = snapshot(); preserved_snapshot(plan["inspection"], before); verify_retained(before, plan)
    disk = os.statvfs(ROOT)
    mem = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    require(disk.f_bavail * disk.f_frsize >= 12 * 1024 ** 3 and mem >= 3 * 1024 ** 3, "ENV_BUILD_RESOURCE_GATE")
    for name, image in (("compiler", COMPILER_IMAGE), ("runtime", RUNTIME_IMAGE)):
        bounded_command(["docker", "pull", image], directory / ("pull-" + name + "-base.log"), limit=600)
    source = extract_source(directory, plan["managed_files"], plan["modes"])
    images = {}
    for name, command in build_commands(source, plan["binding"]):
        tag = command[command.index("--tag") + 1]
        require(subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0, "BUILD_TAG_ALREADY_EXISTS")
        bounded_command(command, directory / ("build-" + name + ".log"), limit=1800)
        image = json.loads(run(["docker", "image", "inspect", tag]))[0]["Id"]
        image_source(image, plan["binding"]); images[name] = image
    compiler_base = json.loads(run(["docker", "image", "inspect", COMPILER_IMAGE]))[0]
    runtime_base = json.loads(run(["docker", "image", "inspect", RUNTIME_IMAGE]))[0]
    require(compiler_base.get("RepoDigests") and runtime_base.get("RepoDigests"), "BASE_DIGEST_REQUIRED")
    dependency_evidence = go_source_evidence(directory, images["builder"], plan["binding"]["release_nonce"])
    redis_image = singleton(before["containers"], "redis")["image_id"]
    tests = linux_tests(directory, images["builder"], redis_image, plan["binding"]["release_nonce"])
    proof = binary_probe(directory, images["runtime"], images["builder"], plan["binding"])
    after = snapshot(); preserved_snapshot(before, after)
    return {"image_id": images["runtime"], "builder_image_id": images["builder"], "binary_proof": proof, "linux_tests": tests,
            "dependency_evidence": dependency_evidence, "base_digests": {"compiler": compiler_base["RepoDigests"], "runtime": runtime_base["RepoDigests"]}, "owned_test_containers_closed": True}


def wait_stopped(identifier, image, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = details(identifier)
        require(value["Image"] == image, "ENV_STOP_IMAGE_CHANGED")
        if not value["State"]["Running"]:
            require(value["State"]["Pid"] == 0 and value["State"]["Status"] == "exited", "ENV_STOP_NOT_CONFIRMED")
            return value
        time.sleep(0.5)
    raise EnvReleaseError("ENV_GRACEFUL_STOP_UNCONFIRMED_QUERY_STATUS")


class RedisReader:
    def __init__(self, target):
        settings = dict(item.split("=", 1) for item in target["Config"].get("Env", []) if "=" in item)
        url = urllib.parse.urlsplit(settings["REDIS_URL"])
        require(url.scheme == "redis" and url.hostname in {"localhost", "127.0.0.1"} and url.path == "/2", "REDIS_DB2_SCOPE")
        self.socket = socket.create_connection(("127.0.0.1", url.port or 6379), timeout=5); self.socket.settimeout(5)
        self.file = self.socket.makefile("rb")
        if url.password is not None:
            auth = ["AUTH"] + ([urllib.parse.unquote(url.username)] if url.username else []) + [urllib.parse.unquote(url.password)]
            self.command(*auth)
        self.command("SELECT", "2")
        self.client_id = int(self.command("CLIENT", "ID"))

    def read(self):
        line = self.file.readline(1024 * 1024); require(line.endswith(b"\r\n"), "REDIS_PROTOCOL")
        marker, value = line[:1], line[1:-2]
        if marker == b"+": return value
        if marker == b":": return int(value)
        if marker == b"-": raise EnvReleaseError("REDIS_READ_REJECTED")
        if marker == b"$":
            size = int(value)
            if size == -1: return None
            require(0 <= size <= 32 * 1024 ** 2, "REDIS_VALUE_LIMIT")
            data = self.file.read(size); require(self.file.read(2) == b"\r\n" and len(data) == size, "REDIS_BULK_TRUNCATED"); return data
        if marker == b"*":
            require(0 <= int(value) <= 200000, "REDIS_ARRAY_LIMIT")
            return [self.read() for _ in range(int(value))]
        raise EnvReleaseError("REDIS_PROTOCOL")

    def command(self, *parts):
        values = [item if isinstance(item, bytes) else str(item).encode() for item in parts]
        self.socket.sendall(b"*" + str(len(values)).encode() + b"\r\n" + b"".join(b"$" + str(len(item)).encode() + b"\r\n" + item + b"\r\n" for item in values))
        return self.read()

    def no_other_clients(self):
        clients = self.command("CLIENT", "LIST").decode().splitlines()
        values = [dict(item.split("=", 1) for item in row.split() if "=" in item) for row in clients]
        require(not any(row.get("db") == "2" and row.get("id") != str(self.client_id) for row in values), "UNKNOWN_DB2_WRITER_NO_CONTROL")

    def close(self):
        self.file.close(); self.socket.close()


def redis_state(target):
    reader = RedisReader(target)
    try:
        reader.no_other_clients(); captured = []; seen = set(); total_bytes = 0
        for pattern, expected in (("instance:*", b"hash"), ("user_instances:*", b"set"), ("allocated_ports", b"set"), ("allocated_port_owners", b"hash"), ("allocated_port_cooldowns", b"zset")):
            cursor = b"0"
            while True:
                cursor, keys = reader.command("SCAN", cursor, "MATCH", pattern, "COUNT", "256")
                for key in keys:
                    if key in seen: continue
                    seen.add(key); kind = reader.command("TYPE", key)
                    if kind == b"none": continue
                    require(kind == expected, "DB2_STATE_TYPE_CHANGED")
                    blob = reader.command("DUMP", key); ttl = reader.command("PTTL", key)
                    if blob is None: continue
                    require(ttl >= -1, "DB2_KEY_EXPIRED_DURING_CAPTURE")
                    total_bytes += len(key) + len(blob)
                    require(total_bytes <= 128 * 1024 ** 2, "DB2_CAPTURE_SIZE_LIMIT")
                    captured.append({"key_base64": base64.b64encode(key).decode(), "type": kind.decode(), "dump_base64": base64.b64encode(blob).decode(), "pttl": ttl})
                    require(len(captured) <= 100000, "DB2_KEY_LIMIT")
                if cursor == b"0": break
        reader.no_other_clients()
        return {"database": 2, "captured_at": time.time(), "keys": sorted(captured, key=lambda row: row["key_base64"]), "other_db2_clients": 0}
    finally:
        reader.close()


def tar_rows(path, rows, source):
    with tarfile.open(path, "x", format=tarfile.PAX_FORMAT) as archive:
        os.chmod(path, 0o600)
        for row in rows:
            filename = source / row["path"]
            require(filename.is_file() and not filename.is_symlink() and payload_digest(filename.read_bytes()) == row["sha256"], "OLD_SOURCE_HASH_CHANGED")
            archive.add(filename, arcname=row["path"], recursive=False)


def flag_capture(target, archive_path=None):
    flag_path = dict(item.split("=", 1) for item in target["Config"].get("Env", []) if "=" in item).get("DYNAMIC_FLAG_RUNTIME_DIR")
    require(flag_path and any(item["Type"] == "bind" and item["Source"] == flag_path and item["Destination"] == flag_path for item in target["Mounts"]), "FLAG_BIND_SCOPE")
    data = run(["docker", "cp", target["Id"] + ":" + flag_path.rstrip("/") + "/.", "-"], limit=120)
    require(len(data) <= 64 * 1024 ** 2, "FLAG_ARCHIVE_LIMIT")
    rows = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for item in archive.getmembers():
            require(item.isdir() or item.isfile(), "FLAG_ARCHIVE_LINK")
            require(not pathlib.PurePosixPath(item.name).is_absolute() and ".." not in pathlib.PurePosixPath(item.name).parts, "FLAG_ARCHIVE_PATH")
            if item.isfile(): rows.append({"path": item.name, "bytes": item.size, "mode": item.mode, "sha256": payload_digest(archive.extractfile(item).read())})
    if archive_path is not None:
        with archive_path.open("xb") as output: os.chmod(archive_path, 0o600); output.write(data)
    return {"sha256": payload_digest(canonical(sorted(rows, key=lambda row: row["path"]))), "file_count": len(rows)}


def fence_backup(directory, plan):
    completed(directory, "build"); completed(directory, "image-backup-complete")
    before = snapshot(); preserved_snapshot(plan["inspection"], before); verify_retained(before, plan)
    old = select_consumer(before["containers"])
    original = details(old["container_id"])
    require(original["Image"] == old["image_id"] and original["State"]["Running"] and runtime_hash(original) == before["target_runtime_sha256"], "PRE_STOP_TARGET_CHANGED")
    flags_before = flag_capture(original)
    preserved_snapshot(before, snapshot())
    stop = {"container_id": old["container_id"], "image_id": old["image_id"], "binding": plan["binding"], "plan_sha256": REQUEST["plan_sha256"],
            "source_files": canonical_file_rows([row for row in before["code_files"] if row["path"].startswith(PREFIX)]),
            "runtime_sha256": before["target_runtime_sha256"], "flags": flags_before}
    private_json(directory / "stop.intent.json", stop)
    bounded_command(["docker", "kill", "--signal", "SIGTERM", old["container_id"]])
    target = wait_stopped(old["container_id"], old["image_id"])
    require(runtime_hash(target) == stop["runtime_sha256"], "STOPPED_TARGET_RUNTIME_CHANGED")
    private_json(directory / "stop.confirmed.json", {"container_id": target["Id"], "image_id": target["Image"], "pid": target["State"]["Pid"],
                 "running": False, "binding": plan["binding"], "plan_sha256": REQUEST["plan_sha256"], "stop_intent_sha256": payload_digest(canonical(stop))})
    stopped = snapshot(); preserved_snapshot(before, stopped, env_stopped=True)
    flags = flag_capture(target, directory / "flags.tar")
    require(flags == flags_before, "FLAG_STATE_CHANGED_DURING_STOP")
    private_json(directory / "stop.complete.json", {"snapshot": stopped, "flags": flags})
    state = redis_state(target)
    private_json(directory / "state.json", state)
    private_json(directory / "runtime.json", target)
    rows = [row for row in before["code_files"] if row["path"].startswith(PREFIX)]
    tar_rows(directory / "source.tar", rows, ROOT)
    with tarfile.open(directory / "protected.tar", "x") as archive:
        os.chmod(directory / "protected.tar", 0o600)
        for index, row in enumerate(before["protected_files"]):
            data = protected_key_bytes(Path(row["path"])) if not os.access(row["path"], os.R_OK) else Path(row["path"]).read_bytes()
            require(payload_digest(data) == row["sha256"], "PROTECTED_CAPTURE_CHANGED")
            item = tarfile.TarInfo("protected/" + str(index)); item.size = len(data); item.mode = 0o600; archive.addfile(item, io.BytesIO(data))
    after = snapshot(); preserved_snapshot(before, after, env_stopped=True)
    require(flag_capture(target) == flags, "FLAG_STATE_CHANGED_DURING_FENCE")
    wait_stopped(old["container_id"], old["image_id"], seconds=1)
    reader = RedisReader(target)
    try: reader.no_other_clients()
    finally: reader.close()
    archive_path = directory / "private-backup.tar"
    with tarfile.open(archive_path, "x") as archive:
        os.chmod(archive_path, 0o600)
        for name in ("state.json", "runtime.json", "source.tar", "flags.tar", "protected.tar"):
            archive.add(directory / name, arcname=name, recursive=False)
    private_json(directory / "fence.snapshot.json", after)
    require(archive_path.stat().st_size <= 512 * 1024 ** 2, "PRIVATE_STATE_ARCHIVE_LIMIT")
    with archive_path.open("rb") as data: archive_sha = hashlib.file_digest(data, "sha256").hexdigest()
    return {"private_archive": str(archive_path), "archive_bytes": archive_path.stat().st_size, "archive_sha256": archive_sha,
            "state_key_count": len(state["keys"]), "state_sha256": payload_digest(canonical(state)),
            "env_stopped": True, "other_db2_clients": 0, "redis_database": 2, "other_services_controlled": 0, "automatic_restore": False}


def compose_command(overlay):
    return ["docker", "compose", "-p", PROJECT, "--project-directory", str(ROOT / "infra"), "--env-file", str(ROOT / "infra/.env"),
            "-f", str(ROOT / "infra/docker-compose.yml"), "-f", str(ROOT / "infra/docker-compose.prod.yml"), "-f", str(overlay)]


def activate(directory, plan):
    built = completed(directory, "build"); captured = completed(directory, "fence-backup-complete")
    require(captured["env_stopped"] and captured["other_db2_clients"] == 0, "ACTIVATION_REQUIRES_BACKUP_FENCE")
    before = snapshot(); preserved_snapshot(plan["inspection"], before, env_stopped=True); verify_retained(before, plan)
    old = next(row for row in before["containers"] if row["service"] == SERVICE)
    original_fence = json.loads((directory / "stop.complete.json").read_bytes())
    require(flag_capture(details(old["container_id"])) == original_fence["flags"], "FLAG_FENCE_CHANGED_BEFORE_ACTIVATION")
    state = redis_state(details(old["container_id"]))
    private_json(directory / "pre-activation-state.json", state)
    source = directory / "source"
    current_paths = {row["path"] for row in before["code_files"] if row["path"].startswith(PREFIX)}
    new_paths = {row["path"] for row in plan["managed_files"]}
    require(current_paths <= new_paths, "OLD_ENV_SOURCE_REMOVAL_NOT_AUTHORIZED")
    for row in plan["managed_files"]:
        path = ROOT / row["path"]; staged = source / row["path"]
        require(staged.is_file() and not staged.is_symlink() and payload_digest(staged.read_bytes()) == row["sha256"], "STAGED_SOURCE_CHANGED")
        for parent in (path, *path.parents): require(not parent.is_symlink(), "LIVE_SOURCE_LINK")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".env-release-" + plan["binding"]["release_nonce"])
        with temporary.open("xb") as output: output.write(staged.read_bytes()); output.flush(); os.fsync(output.fileno())
        os.chmod(temporary, plan["modes"][row["path"]]); os.replace(temporary, path)
    overlay = directory / "overlay.json"
    private_json(overlay, {"services": {SERVICE: {"image": built["image_id"], "labels": source_labels(plan["binding"])}}})
    resolved = json.loads(run([*compose_command(overlay), "config", "--format", "json"]))
    require(resolved["services"][SERVICE]["image"] == built["image_id"], "COMPOSE_ENV_IMAGE_OVERRIDE")
    bounded_command([*compose_command(overlay), "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", SERVICE], directory / "activate.log", limit=120)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        observed = snapshot(); rows = [row for row in observed["containers"] if row["service"] == SERVICE and row["running"]]
        if len(rows) == 1 and rows[0]["image_id"] == built["image_id"] and rows[0]["health"] == "healthy":
            preserved_snapshot(plan["inspection"], observed, env_changed=True); verify_retained(observed, plan)
            require(canonical_file_rows([row for row in observed["code_files"] if row["path"].startswith(PREFIX)])
                    == canonical_file_rows(plan["managed_files"]), "ACTIVATED_MANAGED_SOURCE")
            require(details(rows[0]["container_id"])["Config"].get("Labels", {}).items() >= source_labels(plan["binding"]).items(), "ACTIVATED_LABELS")
            return {"image_id": built["image_id"], "container_id": rows[0]["container_id"], "binary_sha256": built["binary_proof"]["binary_sha256"], "automatic_restore": False}
        time.sleep(1)
    raise EnvReleaseError("ENV_ACTIVATION_HEALTH_UNCONFIRMED_FENCE_STATUS_REQUIRED")


def independent_verify(directory, plan):
    built = completed(directory, "build"); active = completed(directory, "activate")
    before = snapshot(); preserved_snapshot(plan["inspection"], before, env_changed=True); verify_retained(before, plan)
    selected = select_consumer(before["containers"])
    require(selected["image_id"] == built["image_id"] and selected["container_id"] == active["container_id"] and selected["health"] == "healthy", "VERIFY_ENV_IMAGE_HEALTH")
    image_source(selected["image_id"], plan["binding"])
    require(canonical_file_rows([row for row in before["code_files"] if row["path"].startswith(PREFIX)])
            == canonical_file_rows(plan["managed_files"]), "VERIFY_ENV_SOURCE")
    binary = run(["docker", "exec", selected["container_id"], "cat", "/usr/local/bin/env-orchestrator"])
    require(payload_digest(binary) == built["binary_proof"]["binary_sha256"], "VERIFY_RUNNING_BINARY")
    after = snapshot(); preserved_snapshot(before, after)
    return {"image_id": selected["image_id"], "binary_sha256": payload_digest(binary), "managed_file_count": len(plan["managed_files"]),
            "retained_families": 4, "lab_count": len(before["labs"]), "independent_read_only": True, "unverified": ["representative_owned_lab_workflow", "separate_lab_origin"]}


def resume_original(directory, plan):
    require(not (directory / "activate.intent.json").exists(), "ACTIVATION_INTENT_FORBIDS_ORIGINAL_RESUME")
    require(not (directory / "resume-original.intent.json").exists(), "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
    stop = json.loads((directory / "stop.intent.json").read_bytes())
    require(stop["binding"] == plan["binding"] and stop["plan_sha256"] == REQUEST["plan_sha256"], "ORIGINAL_RESUME_STOP_INTENT")
    before = snapshot(); preserved_snapshot(plan["inspection"], before, env_stopped=True)
    current = next(row for row in before["containers"] if row["service"] == SERVICE)
    require(current["container_id"] == stop["container_id"] and current["image_id"] == stop["image_id"], "ORIGINAL_RESUME_IDENTITY")
    require(canonical_file_rows([row for row in before["code_files"] if row["path"].startswith(PREFIX)]) == canonical_file_rows(stop["source_files"])
            and before["target_runtime_sha256"] == stop["runtime_sha256"], "ORIGINAL_RESUME_SOURCE_RUNTIME_CHANGED")
    target = wait_stopped(current["container_id"], current["image_id"], seconds=1)
    require(runtime_hash(target) == stop["runtime_sha256"] and flag_capture(target) == stop["flags"], "ORIGINAL_RESUME_FLAG_FENCE_CHANGED")
    reader = RedisReader(target)
    try: reader.no_other_clients()
    finally: reader.close()
    # Read-only failures above do not consume the one permitted control attempt.
    intent(directory, "resume-original", {"container_id": current["container_id"], "image_id": current["image_id"], "stop_intent_sha256": payload_digest(canonical(stop))})
    bounded_command(["docker", "start", current["container_id"]])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        after = snapshot(); row = next(item for item in after["containers"] if item["service"] == SERVICE)
        if row["running"] and row["health"] == "healthy":
            require(row["container_id"] == stop["container_id"] and row["image_id"] == stop["image_id"], "ORIGINAL_RESUME_READY_IDENTITY_CHANGED")
            preserved_snapshot(plan["inspection"], after, env_changed=True)
            require(canonical_file_rows(after["code_files"]) == canonical_file_rows(plan["inspection"]["code_files"]), "ORIGINAL_RESUME_SOURCE_CHANGED")
            return {"original_container_id": row["container_id"], "original_image_id": row["image_id"], "automatic_restore": False}
        time.sleep(1)
    raise EnvReleaseError("ORIGINAL_RESUME_UNCONFIRMED_QUERY_STATUS")


def fence_current(directory, plan):
    require(not (directory / "fence-current.intent.json").exists(), "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
    require((directory / "activate.intent.json").is_file(), "NO_ACTIVATION_INTENT_TO_FENCE")
    attempted = json.loads((directory / "activate.intent.json").read_bytes())
    require(attempted.get("binding") == plan["binding"] and attempted.get("plan_sha256") == REQUEST["plan_sha256"], "CURRENT_FENCE_ACTIVATION_INTENT_UNBOUND")
    built = completed(directory, "build")
    before = snapshot()
    drift = snapshot_drift(plan["inspection"], before)
    require(canonical_file_rows([row for row in before["code_files"] if row["path"].startswith(PREFIX)])
            == canonical_file_rows(plan["managed_files"]), "CURRENT_FENCE_SOURCE_UNBOUND")
    rows = [row for row in before["containers"] if row["service"] == SERVICE]
    require(len(rows) == 1 and rows[0]["image_id"] == built["image_id"], "CURRENT_ENV_NOT_RELEASE_IMAGE")
    target = details(rows[0]["container_id"])
    require(target["Image"] == built["image_id"] and target["Config"].get("Labels", {}).items() >= source_labels(plan["binding"]).items(), "CURRENT_ENV_LABEL_BINDING")
    require(runtime_hash(target) == before["target_runtime_sha256"], "CURRENT_ENV_RUNTIME_CHANGED")
    intent(directory, "fence-current", {"container_id": target["Id"], "image_id": target["Image"], "runtime_sha256": before["target_runtime_sha256"], "original_plan_drift": drift})
    if rows[0]["running"]:
        bounded_command(["docker", "kill", "--signal", "SIGTERM", rows[0]["container_id"]])
    wait_stopped(rows[0]["container_id"], built["image_id"])
    after = snapshot(); preserved_snapshot(before, after, env_stopped=True)
    return {"fenced_container_id": rows[0]["container_id"], "image_id": built["image_id"], "automatic_restore": False, "old_version_resume_forbidden": True,
            "original_plan_drift": drift, "immediate_non_target_preservation": True}


def main():
    global RETAINED_SERVICES
    RETAINED_SERVICES = REQUEST.get("retained_services", {})
    directory = None; lease = None
    try:
        validate_scope(REQUEST["scope"])
        require(ACTION in {"inspect", "build", "image-backup", "image-backup-complete", "fence-backup", "fence-backup-complete", "activate", "verify", "status", "resume-original", "fence-current"}, "REMOTE_ACTION_FORBIDDEN")
        if ACTION == "inspect":
            observed = snapshot(); select_consumer(observed["containers"])
            for service in RETAINED_SERVICES:
                rows = [row for row in observed["containers"] if row["service"] == service]
                require(len(rows) == 1 and rows[0]["running"] and (rows[0]["health"] is None if service in LEGACY_WORKERS else rows[0]["health"] == "healthy"), "INITIAL_RETAINED_READINESS")
            result = {"kind": "env-inspection", "status": "passed", "snapshot": observed, "observed_at": time.time()}
        else:
            plan = REQUEST["plan"]; validate_binding(plan["binding"]); validate_scope(plan["scope"])
            RETAINED_SERVICES = REQUEST["retained_services"]
            directory = release_directory(create=ACTION == "build")
            result = {"kind": "env-" + ACTION, "binding": plan["binding"], "plan_sha256": REQUEST["plan_sha256"], "status": "passed"}
            if ACTION == "status":
                result.update({"snapshot": snapshot(), "journal": {path.name: json.loads(path.read_bytes()) for path in sorted(directory.glob("*.json"))
                    if path.name.endswith((".intent.json", ".confirmed.json", ".complete.json", ".partial.json"))}, "owned_test_containers": owned_test_status(plan["binding"]["release_nonce"]), "replay_or_restore_performed": False})
            else:
                lease = (directory / "lease").open("a+b"); os.chmod(directory / "lease", 0o600); fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if ACTION not in {"resume-original", "fence-current"}:
                    intent(directory, ACTION)
                if ACTION == "build": result.update(build_phase(directory, plan))
                elif ACTION == "image-backup":
                    completed(directory, "build"); current = snapshot(); preserved_snapshot(plan["inspection"], current)
                    selected = select_consumer(current["containers"]); result.update({"image_id": selected["image_id"], "image_metadata": image_metadata(selected["image_id"])})
                elif ACTION == "image-backup-complete":
                    prepared = completed(directory, "image-backup")
                    require(REQUEST["artifact"]["bytes"] > 0 and HASH.fullmatch(REQUEST["artifact"]["sha256"]), "IMAGE_ARCHIVE_ATTESTATION")
                    result.update({"image_id": prepared["image_id"], "artifact": REQUEST["artifact"]})
                    preserved_snapshot(plan["inspection"], snapshot())
                elif ACTION == "fence-backup": result.update(fence_backup(directory, plan))
                elif ACTION == "fence-backup-complete":
                    result.update(completed(directory, "fence-backup")); result["kind"] = "env-fence-backup"
                    result.update({"components": ["image", "source", "config", "flags", "redis_db2"], "artifact": REQUEST["artifact"]})
                    require(REQUEST["artifact"]["bytes"] > 0 and HASH.fullmatch(REQUEST["artifact"]["sha256"]), "STATE_ARCHIVE_ATTESTATION")
                    require(REQUEST["artifact"]["bytes"] == result["archive_bytes"] and REQUEST["artifact"]["sha256"] == result["archive_sha256"], "STATE_ARCHIVE_IDENTITY")
                    preserved_snapshot(plan["inspection"], snapshot(), env_stopped=True)
                elif ACTION == "activate": result.update(activate(directory, plan))
                elif ACTION == "verify": result.update(independent_verify(directory, plan))
                elif ACTION == "resume-original": result.update(resume_original(directory, plan))
                elif ACTION == "fence-current": result.update(fence_current(directory, plan))
                else: raise EnvReleaseError("REMOTE_ACTION_FORBIDDEN")
                private_json(directory / (ACTION + ".complete.json"), result)
        print(json.dumps(result))
    except Exception as error:
        result = {"status": "uncertain", "kind": "env-" + ACTION, "code": str(error) if isinstance(error, EnvReleaseError) else "REMOTE_REVIEW_REQUIRED",
                  "automatic_restore": False, "next": "status; inspect original intent and current identity before any action"}
        if directory is not None and ACTION in {"resume-original", "fence-current"}:
            result["control_intent_recorded"] = (directory / (ACTION + ".intent.json")).exists()
            if not result["control_intent_recorded"]:
                result["next"] = "status; control not attempted; resolve read-only validation failure before explicit recovery"
        if "plan" in REQUEST:
            try: result["owned_test_containers"] = owned_test_status(REQUEST["plan"]["binding"]["release_nonce"])
            except Exception: result["owned_test_status_unverified"] = True
        if directory is not None:
            path = directory / (ACTION + ".partial.json")
            if not path.exists():
                try: private_json(path, result)
                except Exception: result["partial_receipt_write_unverified"] = True
        print(json.dumps(result))
    finally:
        if lease is not None: lease.close()
