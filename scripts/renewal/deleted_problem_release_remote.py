"""Injected dedicated release scope. Never execute this file independently."""

LEARN_PUBLIC_TABLES_SQL = "SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]'::json) FROM pg_catalog.pg_tables WHERE schemaname='public'"


def learn_public_table_names(postgres):
    shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=3000"; exec psql -U "$POSTGRES_USER" -d learn_db -X -A -t -v ON_ERROR_STOP=1 -c "$1"'
    data = run(["docker", "exec", postgres, "sh", "-c", shell, "deleted-problem-backup-catalog", LEARN_PUBLIC_TABLES_SQL], limit=30)
    return json.loads(data)


def validate_learn_dump_catalog(return_code, payload, expected_tables):
    require(return_code == 0 and isinstance(payload, bytes) and 0 < len(payload) <= 16 * 1024 ** 2, "LEARN_DUMP_RESTORE_CATALOG_INVALID")
    require(isinstance(expected_tables, list) and expected_tables and all(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in expected_tables)
            and len(expected_tables) == len(set(expected_tables)) and {"challenges", "challenge_image_cleanup_jobs"} <= set(expected_tables), "LEARN_PUBLIC_TABLE_CATALOG_INVALID")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DeletedProblemReleaseError("LEARN_DUMP_RESTORE_CATALOG_INVALID") from error
    entries = {"TABLE": [], "TABLE DATA": []}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        match = re.fullmatch(r"\s*\d+;\s+\d+\s+\d+\s+(TABLE(?: DATA)?)\s+(\S+)\s+(\S+)\s+.+", line)
        if match and match[2] == "public":
            entries[match[1]].append(match[3])
    expected = set(expected_tables)
    for names in entries.values():
        require(len(names) == len(set(names)) and set(names) == expected, "LEARN_DUMP_PUBLIC_TABLE_OR_DATA_MISMATCH")
    return {"table_count": len(entries["TABLE"]), "table_data_count": len(entries["TABLE DATA"]),
            "public_tables_sha256": payload_digest(canonical(sorted(expected))), "native_list_sha256": payload_digest(payload)}


def private_json(path, value):
    require(path.is_relative_to(Path(REMOTE_ROOT)) and not any(item.is_symlink() for item in (path, *path.parents)), "PRIVATE_PATH_LINK_OR_ESCAPE")
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        output.write(canonical(value)); output.flush(); os.fsync(output.fileno())


def details(identifier):
    require(re.fullmatch(r"[0-9a-f]{64}", identifier or ""), "FULL_CONTAINER_ID_REQUIRED")
    values = json.loads(run(["docker", "inspect", identifier]))
    require(len(values) == 1 and values[0]["Id"] == identifier, "CONTAINER_IDENTITY_CHANGED")
    return values[0]


def daemon_rows():
    ids = run(["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"]).decode().splitlines()
    require(ids and len(ids) == len(set(ids)) and all(re.fullmatch(r"[0-9a-f]{64}", item) for item in ids), "DAEMON_INVENTORY_INVALID")
    rows = []
    for value in json.loads(run(["docker", "inspect", *ids])):
        config, state = value["Config"], value["State"]
        labels = config.get("Labels") or {}
        rows.append({"container_id": value["Id"], "image_id": value["Image"], "project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service"),
                     "running": state.get("Running") is True, "pid": state.get("Pid"), "status": state.get("Status"), "health": state.get("Health", {}).get("Status"),
                     "started_at": state.get("StartedAt"), "finished_at": state.get("FinishedAt"), "restart_count": value.get("RestartCount"),
                     "runtime_sha256": runtime_hash(value), "labels_sha256": payload_digest(canonical(labels)), "ports": value.get("NetworkSettings", {}).get("Ports"),
                     "mounts": sorted([{key: item.get(key) for key in ("Type", "Source", "Destination", "RW")} for item in value.get("Mounts", [])], key=lambda item: item["Destination"])})
    require({row["container_id"] for row in rows} == set(ids), "DAEMON_INSPECTION_INCOMPLETE")
    return sorted(rows, key=lambda row: row["container_id"])


def database_snapshot(postgres):
    result = {}
    shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=3000"; exec psql -U "$POSTGRES_USER" -d "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"'
    sql = "SELECT json_build_object('head',(SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version),'table_count',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'))"
    for name, database in DATABASES.items():
        value = json.loads(run(["docker", "exec", postgres, "sh", "-c", shell, "deleted-problem-readonly", database, sql], limit=30))
        schema_shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=3000"; exec pg_dump -U "$POSTGRES_USER" -d "$1" --schema-only --no-owner --no-privileges --no-comments --lock-wait-timeout=3000'
        schema = run(["docker", "exec", postgres, "sh", "-c", schema_shell, "deleted-problem-schema", database], limit=60)
        schema = b"\n".join(line for line in schema.splitlines() if not line.startswith((b"\\restrict ", b"\\unrestrict ")))
        value.update({"database": database, "schema_sha256": payload_digest(schema)})
        result[name] = value
    require({name: value["head"] for name, value in result.items()} == {name: [head] for name, head in HEADS.items()}, "SCHEMA_HEAD_MISMATCH_NO_MIGRATION_ALLOWED")
    return result


def snapshot():
    model = compose()
    protected = protected_rows(model)
    require(len(protected) == 6, "SIX_PROTECTED_FILES_REQUIRED")
    current = daemon_rows()
    # Recovery inspection must remain available during a partial family rollout.
    # Read the frontend contract independently; uniform target families are an
    # execution/readiness precondition, not a prerequisite for viewing status.
    frontends = [row for row in current if row["project"] == PROJECT and row["service"] == "frontend"]
    require(len(frontends) == 1, "FRONTEND_IDENTITY_AMBIGUOUS")
    postgres = [row for row in current if row["project"] == PROJECT and row["service"] == "postgres" and row["running"]]
    require(len(postgres) == 1, "POSTGRES_IDENTITY_AMBIGUOUS")
    frontend = details(frontends[0]["container_id"])
    arguments = _STAGE["frontend_args_from_inspection"](frontend["Config"].get("Env", []), model["services"]["frontend"]["build"]["args"])
    return {"containers": current, "code_files": code_rows(protected), "protected_files": protected, "databases": database_snapshot(postgres[0]["container_id"]), "frontend_args": arguments}


def release_directory(create=False):
    binding = REQUEST["plan"]["binding"]
    require(re.fullmatch(r"[0-9a-f]{40}", binding.get("commit", "")) and HASH.fullmatch(binding.get("source_fingerprint", "")), "BINDING_HASH")
    nonce = uuid.UUID(binding["release_nonce"])
    require(nonce.version == 4 and str(nonce) == binding["release_nonce"], "BINDING_NONCE")
    root = Path(REMOTE_ROOT)
    require(not any(item.is_symlink() for item in (root, *root.parents)), "RELEASE_DIRECTORY_LINK")
    if create:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = root / str(nonce)
    if create:
        directory.mkdir(mode=0o700, exist_ok=False)
        private_json(directory / "plan.json", {"plan": REQUEST["plan"], "plan_sha256": REQUEST["plan_sha256"]})
    require(directory.is_dir() and not directory.is_symlink(), "RELEASE_DIRECTORY_MISSING")
    require(json.loads((directory / "plan.json").read_bytes()) == {"plan": REQUEST["plan"], "plan_sha256": REQUEST["plan_sha256"]}, "REMOTE_PLAN_CHANGED")
    return directory


def intent(directory, action, observation=None):
    require(action in {*PHASES, "image-backup-complete", "fence-backup-complete", "stop", "minio-stop", "minio-start", "activate-env", "activate-learn", "activate-frontend"}, "INTENT_SCOPE")
    path = directory / (action + ".intent.json")
    require(not path.exists() and not path.is_symlink(), "INTENT_EXISTS_QUERY_STATUS_NO_REPLAY")
    private_json(path, {"action": action, "binding": REQUEST["plan"]["binding"], "plan_sha256": REQUEST["plan_sha256"], "observed_at": time.time(), "observation": observation})


def completed(directory, action):
    path = directory / (action + ".complete.json")
    require(path.is_file() and not path.is_symlink(), "PRIOR_PHASE_INCOMPLETE_" + action.upper())
    value = json.loads(path.read_bytes())
    require(value.get("status") == "passed" and value.get("binding") == REQUEST["plan"]["binding"] and value.get("plan_sha256") == REQUEST["plan_sha256"], "PRIOR_PHASE_UNBOUND")
    return value


def bounded(command, log=None, limit=120):
    if log is None:
        return run(command, limit=limit)
    require(log.is_relative_to(Path(REMOTE_ROOT)) and not log.exists(), "PRIVATE_LOG_SCOPE")
    with log.open("xb") as output:
        os.chmod(log, 0o600)
        result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=limit)
    require(result.returncode == 0, "COMMAND_FAILED_PRIVATE_LOG_RETAINED")


def inspect_image(identifier, binding=None):
    values = json.loads(run(["docker", "image", "inspect", identifier]))
    require(len(values) == 1 and IMAGE.fullmatch(values[0].get("Id", "")), "IMAGE_IDENTITY")
    value = values[0]
    if IMAGE.fullmatch(identifier):
        require(value["Id"] == identifier, "IMAGE_IDENTITY_CHANGED")
    if binding is not None:
        require((value.get("Config", {}).get("Labels") or {}).items() >= source_labels(binding).items(), "IMAGE_SOURCE_LABELS")
    return value


def image_metadata(identifier):
    value = inspect_image(identifier)
    return {"image_id": identifier, "bytes": value["Size"], "labels_sha256": payload_digest(canonical(value.get("Config", {}).get("Labels", {}) or {})),
            "repo_tags_sha256": payload_digest(canonical(sorted(value.get("RepoTags") or []))), "os": value["Os"], "architecture": value["Architecture"]}


def extract_source(directory, plan):
    payload = base64.b64decode(ARCHIVE, validate=True)
    require(0 < len(payload) <= MAX_STAGE_BYTES, "SOURCE_ARCHIVE_SIZE")
    expected = {row["path"]: row for row in plan["managed_files"]}
    require(len(expected) == len(plan["managed_files"]) <= MAX_STAGE_FILES and set(plan["modes"]) == set(expected), "SOURCE_MANIFEST_INVALID")
    source = directory / "source"
    source.mkdir(mode=0o755)
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for item in archive:
            _STAGE["safe_source_path"](item.name)
            require(any(item.name.startswith(context + "/") for _, context in CONTEXTS) and item.name in expected and item.name not in seen
                    and item.isfile() and not any(key.startswith("GNU.sparse") for key in item.pax_headers), "SOURCE_ARCHIVE_MEMBER")
            row = expected[item.name]
            require(item.size == row["bytes"] and item.mode == plan["modes"][item.name] and item.mode in {0o644, 0o755}, "SOURCE_MEMBER_METADATA")
            data = archive.extractfile(item).read(item.size + 1)
            require(len(data) == row["bytes"] and payload_digest(data) == row["sha256"], "SOURCE_MEMBER_HASH")
            _STAGE["reject_secret_material"](item.name, data)
            path = source / item.name
            path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            require(path.resolve().is_relative_to(source.resolve()) and not any(parent.is_symlink() for parent in (path, *path.parents)), "SOURCE_LINK_OR_ESCAPE")
            for parent in path.parents:
                if parent == source:
                    break
                parent.chmod(0o755)
            with path.open("xb") as output:
                output.write(data)
            path.chmod(item.mode)
            seen.add(item.name)
        require(not payload[archive.offset:].strip(b"\0"), "SOURCE_ARCHIVE_TRAILING_DATA")
    require(seen == set(expected), "SOURCE_ARCHIVE_INCOMPLETE")
    return source


def build_phase(directory, plan):
    before = snapshot()
    preserved_snapshot(plan["inspection"], before)
    selected_consumers(before["containers"])
    require(before["frontend_args"] == plan["inspection"]["frontend_args"], "FRONTEND_ARGS_CHANGED")
    disk = os.statvfs(directory)
    mem = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))
    require(disk.f_bavail * disk.f_frsize >= 12 * 1024 ** 3 and mem >= 3 * 1024 ** 3, "BUILD_RESOURCE_GATE")
    source = extract_source(directory, plan)
    commands = REQUEST["build_commands"]
    require(commands == build_commands(source, plan["binding"], plan["inspection"]["frontend_args"]), "EXACT_SERIAL_BUILD_COMMANDS_REQUIRED")
    images = {}
    for family, command in commands:
        context = dict(CONTEXTS)[family]
        tag = "ctf-renewal/" + family + ":" + plan["binding"]["commit"] + "-" + plan["binding"]["release_nonce"]
        require(command[:9] == ["docker", "build", "--pull", "--network", "host", "--progress", "plain", "--file", str(source / context / "Dockerfile")]
                and command[9:11] == ["--tag", tag] and command[-1] == str(source / context), "BUILD_COMMAND_IDENTITY")
        allowed_flags = {"--tag", "--label", "--target", "--build-arg"}
        tail = command[9:-1]
        require(len(tail) % 2 == 0 and all(tail[index] in allowed_flags for index in range(0, len(tail), 2)), "BUILD_COMMAND_FLAGS")
        bounded(command, directory / ("build-" + family + ".log"), limit=1800)
        image = inspect_image(tag, plan["binding"])
        require(image.get("Config", {}).get("User") in {"1001", "1001:1001", "appuser", "nextjs"}, "BUILT_NON_ROOT_REQUIRED")
        identifier = image["Id"]
        name = "ctf-delete-probe-" + family + "-" + plan["binding"]["release_nonce"]
        if family == "env":
            command = ["docker", "run", "--rm", "--name", name, "--label", "io.ctf-web.deleted-problem-test-nonce=" + plan["binding"]["release_nonce"],
                       "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "32", "--memory", "128m", "--cpus", "0.5",
                       "--user", "1001:1001", "--entrypoint", "cat", identifier, "/usr/local/bin/env-orchestrator"]
            binary = bounded(command)
            require(binary[:4] == b"\x7fELF" and 20 <= len(binary) <= 64 * 1024 ** 2, "ENV_BINARY_REQUIRED")
            proof = {"non_root_uid": 1001, "binary_sha256": payload_digest(binary), "isolated": True}
        else:
            command, count = _STAGE["runtime_probe_command"](family, identifier, plan["managed_files"])
            command[3:3] = ["--name", name, "--label", "io.ctf-web.deleted-problem-test-nonce=" + plan["binding"]["release_nonce"]]
            proof = json.loads(bounded(command))
            _STAGE["validate_runtime_probe"](proof, count)
        if family == "frontend":
            require(image["Config"].get("Labels", {}).get("io.ctf-web.frontend-build-args-sha256") == payload_digest(canonical(before["frontend_args"])), "FRONTEND_COMPILED_ARGUMENT_BINDING")
        images[family] = {"image_id": identifier, "tag": tag, "proof": proof}
        private_json(directory / ("build-" + family + ".complete.json"), {"status": "passed", "binding": plan["binding"], "plan_sha256": REQUEST["plan_sha256"], **images[family]})
    preserved_snapshot(before, snapshot())
    return {"images": images, "serial_order": [family for family, _ in CONTEXTS], "production_io_from_probes": False}


def wait_state(identifier, image, running, seconds=90):
    deadline = time.monotonic() + seconds
    while True:
        value = details(identifier)
        require(value["Image"] == image, "CONTROL_IMAGE_CHANGED")
        state = value["State"]
        ready = state["Running"] is running and (state.get("Health", {}).get("Status") == "healthy" if running and state.get("Health") else True)
        if ready and (running or state.get("Pid") == 0):
            return value
        require(time.monotonic() < deadline, "CONTROL_STATE_UNCONFIRMED_QUERY_STATUS")
        time.sleep(0.5)


def control_container(identifier, row, action):
    require(action in {"stop", "start"} and row["project"] == PROJECT and (row["service"] in TARGETS or row["service"] == "minio"), "CONTROL_SCOPE_FORBIDDEN")
    value = details(identifier)
    require(value["Id"] == row["container_id"] and value["Image"] == row["image_id"] and runtime_hash(value) == row["runtime_sha256"], "CONTROL_IDENTITY_CHANGED")
    labels = value["Config"].get("Labels") or {}
    require(labels.get("com.docker.compose.project") == PROJECT and labels.get("com.docker.compose.service") == row["service"], "CONTROL_LABEL_CHANGED")
    require(value["State"]["Running"] is (action == "stop"), "CONTROL_STATE_CHANGED_NO_REPLAY")
    command = ["docker", "stop", "--time", "30", identifier] if action == "stop" else ["docker", "start", identifier]
    bounded(command, limit=60)
    return wait_state(identifier, row["image_id"], action == "start")


def capture_command(command, path, limit=600):
    require(path.is_relative_to(Path(REMOTE_ROOT)) and not path.exists(), "BACKUP_FILE_SCOPE")
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        result = subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL, timeout=limit)
    require(result.returncode == 0 and path.stat().st_size > 0, "PRIVATE_CAPTURE_FAILED")


def archive_rows(path, rows, root):
    with tarfile.open(path, "x") as archive:
        os.chmod(path, 0o600)
        for row in rows:
            filename = root / row["path"]
            require(filename.is_file() and not any(item.is_symlink() for item in (filename, *filename.parents)) and filename.stat().st_size == row["bytes"] and digest(filename) == row["sha256"], "BACKUP_SOURCE_CHANGED")
            archive.add(filename, arcname=row["path"], recursive=False)


def resume_backup_minio(directory, original):
    # A failed stop reply is not proof that stop failed. Observe the exact
    # backup-paused container before issuing its one permitted resume action.
    current = details(original["container_id"])
    labels = current.get("Config", {}).get("Labels") or {}
    require(current.get("Id") == original["container_id"] and current.get("Image") == original["image_id"]
            and runtime_hash(current) == original["runtime_sha256"]
            and labels.get("com.docker.compose.project") == PROJECT and labels.get("com.docker.compose.service") == "minio",
            "MINIO_RESUME_IDENTITY_UNCONFIRMED_QUERY_STATUS")
    state = current.get("State", {})
    if state.get("Running") is True:
        return {"action": "none", "reason": "same_container_already_running"}
    require(state.get("Running") is False and state.get("Pid") == 0 and state.get("Status") == "exited", "MINIO_RESUME_STOP_UNCONFIRMED_QUERY_STATUS")
    intent(directory, "minio-start", {"container_id": original["container_id"], "image_id": original["image_id"]})
    control_container(original["container_id"], original, "start")
    return {"action": "start", "container_id": original["container_id"]}


def fence_backup(directory, plan):
    completed(directory, "image-backup-complete")
    before = snapshot()
    preserved_snapshot(plan["inspection"], before)
    managed_source_contract(plan["inspection"]["code_files"], before["code_files"], plan["managed_files"], require_managed=False)
    chosen = selected_consumers(before["containers"])
    minios = [row for row in before["containers"] if row["project"] == PROJECT and row["service"] == "minio" and row["running"] and row["health"] == "healthy"]
    require(len(minios) == 1, "MINIO_SINGLE_HEALTHY_IDENTITY")
    minio = minios[0]
    object_usage = run(["docker", "exec", minio["container_id"], "du", "-sk", "/data"], limit=120).decode().split()
    require(object_usage and object_usage[0].isdigit(), "OBJECT_SIZE_UNVERIFIED")
    postgres = next(row for row in before["containers"] if row["project"] == PROJECT and row["service"] == "postgres" and row["running"])
    size_shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=15000 -c lock_timeout=3000"; exec psql -U "$POSTGRES_USER" -d learn_db -X -A -t -v ON_ERROR_STOP=1 -c "SELECT pg_database_size(current_database())"'
    database_bytes = run(["docker", "exec", postgres["container_id"], "sh", "-c", size_shell], limit=30).strip()
    require(database_bytes.isdigit(), "LEARN_SIZE_UNVERIFIED")
    needed = 2 * (int(object_usage[0]) * 1024 + int(database_bytes) + sum(row["bytes"] for row in before["code_files"])) + 1024 ** 3
    disk = os.statvfs(directory)
    require(disk.f_bavail * disk.f_frsize >= needed, "SERVER_BACKUP_SPACE_GATE")
    original_runtime = {service: details(row["container_id"]) for service, row in chosen.items()}
    private_json(directory / "runtime.json", original_runtime)
    intent(directory, "stop", {service: {"container_id": row["container_id"], "image_id": row["image_id"]} for service, row in chosen.items()})
    for service in reversed(TARGETS):
        control_container(chosen[service]["container_id"], chosen[service], "stop")
    stopped = snapshot()
    health_changes = preserved_snapshot(before, stopped, stopped=True, learn_maintenance=True)
    minio_stop_intended = False
    try:
        intent(directory, "minio-stop", {"container_id": minio["container_id"], "image_id": minio["image_id"]})
        minio_stop_intended = True
        control_container(minio["container_id"], minio, "stop")
        shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=600000 -c lock_timeout=3000"; exec pg_dump -U "$POSTGRES_USER" -d learn_db --format=custom --no-owner --no-privileges --lock-wait-timeout=3000'
        capture_command(["docker", "exec", postgres["container_id"], "sh", "-c", shell], directory / "learn.dump")
        with (directory / "learn.dump").open("rb") as data:
            checked = subprocess.run(["docker", "exec", "-i", postgres["container_id"], "pg_restore", "--list"], stdin=data, capture_output=True, timeout=60)
        learn_catalog = validate_learn_dump_catalog(checked.returncode, checked.stdout, learn_public_table_names(postgres["container_id"]))
        capture_command(["docker", "cp", minio["container_id"] + ":/data/.", "-"], directory / "objects.tar")
        with (directory / "objects.tar").open("rb") as stream:
            object_inventory = tar_inventory(stream)
        old_rows = [row for row in before["code_files"] if any(row["path"].startswith(context + "/") for _, context in CONTEXTS)]
        archive_rows(directory / "source.tar", old_rows, ROOT)
        with tarfile.open(directory / "protected.tar", "x") as archive:
            os.chmod(directory / "protected.tar", 0o600)
            for index, row in enumerate(before["protected_files"]):
                filename = Path(row["path"])
                data = filename.read_bytes() if os.access(filename, os.R_OK) else protected_key_bytes(filename)
                require(payload_digest(data) == row["sha256"], "PROTECTED_CAPTURE_CHANGED")
                item = tarfile.TarInfo("protected/" + str(index)); item.size, item.mode = len(data), 0o600
                archive.addfile(item, io.BytesIO(data))
        private_json(directory / "state.json", {"snapshot": before, "object_inventory": object_inventory, "learn_dump_catalog": learn_catalog, "captured_at": time.time(), "automatic_restore": False})
    finally:
        if minio_stop_intended:
            resume_backup_minio(directory, minio)
    after = snapshot()
    health_changes.extend(preserved_snapshot(before, after, stopped=True, minio_resumed=True, learn_maintenance=True))
    private_json(directory / "fence.snapshot.json", after)
    path = directory / "private-backup.tar"
    with tarfile.open(path, "x") as archive:
        os.chmod(path, 0o600)
        for name in ("learn.dump", "objects.tar", "source.tar", "protected.tar", "runtime.json", "state.json"):
            archive.add(directory / name, arcname=name, recursive=False)
    return {"private_archive": str(path), "archive_bytes": path.stat().st_size, "archive_sha256": digest(path), "object_inventory": object_inventory, "learn_dump_catalog": learn_catalog,
            "fenced_services": list(TARGETS), "minio_resumed_same_identity": True, "dependent_health_observations": health_changes, "automatic_restore": False}


def compose_command(overlay):
    return ["docker", "compose", "-p", PROJECT, "--project-directory", str(ROOT / "infra"), "--env-file", str(ROOT / "infra/.env"),
            "-f", str(ROOT / "infra/docker-compose.yml"), "-f", str(ROOT / "infra/docker-compose.prod.yml"), "-f", str(overlay)]


def await_original_dependent_health(plan, seconds=120):
    original = plan["inspection"]
    baseline = [row for row in original["containers"] if row.get("project") == PROJECT and row.get("service") == DEPENDENT_HEALTH_SERVICE]
    require(len(baseline) == 1 and baseline[0]["running"] is True and baseline[0]["health"] == "healthy", "ORIGINAL_DEPENDENT_HEALTHY_BASELINE_REQUIRED")
    deadline = time.monotonic() + seconds
    observations = []
    last_health = "healthy"
    while True:
        observed = snapshot()
        preserved_snapshot(original, observed, changed=TARGETS, minio_resumed=True, learn_maintenance=True)
        current = next(row for row in observed["containers"] if row["container_id"] == baseline[0]["container_id"])
        if current["health"] != last_health:
            observations.append({"service": DEPENDENT_HEALTH_SERVICE, "container_id": current["container_id"], "from": last_health, "to": current["health"], "phase": "readonly-recovery-wait", "controlled": False})
            last_health = current["health"]
        if current["health"] == "healthy":
            # The final comparison is against the original healthy inspection,
            # never an unhealthy intermediate backup snapshot.
            preserved_snapshot(original, observed, changed=TARGETS, minio_resumed=True)
            return observed, observations
        require(time.monotonic() < deadline, "DEPENDENT_HEALTH_RECOVERY_UNCONFIRMED_QUERY_STATUS")
        time.sleep(1)


def activate(directory, plan):
    built = completed(directory, "build")
    captured = completed(directory, "fence-backup-complete")
    require(captured.get("fenced_services") == list(TARGETS) and captured.get("minio_resumed_same_identity") is True, "BACKUP_FENCE_REQUIRED")
    fenced = json.loads((directory / "fence.snapshot.json").read_bytes())
    before = snapshot()
    health_changes = preserved_snapshot(fenced, before, learn_maintenance=True)
    require(all(not row["running"] for row in selected_consumers(before["containers"], running=False).values()), "TARGETS_NOT_FENCED")
    managed_source_contract(plan["inspection"]["code_files"], before["code_files"], plan["managed_files"], require_managed=False)
    # Validate the entire staged managed subset before the first live write.
    # Existing non-managed files are never copied, renamed or removed.
    staged_rows = []
    for row in plan["managed_files"]:
        source = directory / "source" / row["path"]
        target = ROOT / row["path"]
        require(source.is_file() and not any(item.is_symlink() for item in (source, *source.parents, target, *target.parents)), "ACTIVATION_SOURCE_MISSING_OR_LINK")
        staged_rows.append(file_row(source, row["path"]))
    managed_source_contract([], staged_rows, plan["managed_files"])
    for row in plan["managed_files"]:
        target, source = ROOT / row["path"], directory / "source" / row["path"]
        require(source.is_file() and source.stat().st_size == row["bytes"] and digest(source) == row["sha256"] and not any(item.is_symlink() for item in (source, *source.parents, target, *target.parents)), "ACTIVATION_SOURCE_CHANGED_OR_LINK")
        target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
        temporary = target.with_name(target.name + ".delete-release-" + plan["binding"]["release_nonce"])
        with temporary.open("xb") as output:
            output.write(source.read_bytes()); output.flush(); os.fsync(output.fileno())
        temporary.chmod(plan["modes"][row["path"]]); os.replace(temporary, target)
    overlay = directory / "overlay.json"
    private_json(overlay, {"services": {service: {"image": built["images"][family]["image_id"], "labels": source_labels(plan["binding"])} for service, family in FAMILIES.items()}})
    resolved = json.loads(run([*compose_command(overlay), "config", "--format", "json"]))
    require(all(resolved["services"][service]["image"] == built["images"][family]["image_id"] for service, family in FAMILIES.items()), "COMPOSE_EXACT_IMAGES_REQUIRED")
    managed_source_contract(plan["inspection"]["code_files"], snapshot()["code_files"], plan["managed_files"])
    changed = []
    for family, services in zip(("env", "learn", "frontend"), ACTIVATION_GROUPS):
        intent(directory, "activate-" + family, {"services": list(services), "image_id": built["images"][family]["image_id"]})
        bounded([*compose_command(overlay), "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", *services], directory / ("activate-" + family + ".log"), limit=180)
        deadline = time.monotonic() + 120
        while True:
            now = snapshot()
            selected = selected_consumers(now["containers"], running=False)
            ready = all(selected[service]["running"] and selected[service]["image_id"] == built["images"][FAMILIES[service]]["image_id"]
                        and (selected[service]["health"] is None if service in WORKERS else selected[service]["health"] == "healthy") for service in services)
            if ready:
                break
            require(time.monotonic() < deadline, "ACTIVATION_HEALTH_UNCONFIRMED_QUERY_STATUS")
            time.sleep(1)
        changed.extend(services)
        phase_health = preserved_snapshot(fenced, now, changed=changed, learn_maintenance=True)
        health_changes.extend(phase_health)
        require(all(not selected[service]["running"] for service in TARGETS if service not in changed), "UNEXPECTED_EARLY_TARGET_START")
        private_json(directory / ("activate-" + family + ".complete.json"), {"status": "passed", "binding": plan["binding"], "plan_sha256": REQUEST["plan_sha256"], "services": list(services), "image_id": built["images"][family]["image_id"], "dependent_health_observations": phase_health})
    final, recovery_health = await_original_dependent_health(plan)
    source_contract = managed_source_contract(plan["inspection"]["code_files"], final["code_files"], plan["managed_files"])
    selected = selected_consumers(final["containers"])
    health_changes.extend(recovery_health)
    return {"images": {family: row["image_id"] for family, row in built["images"].items()}, "consumers": {service: row["container_id"] for service, row in selected.items()}, "activation_groups": [list(group) for group in ACTIVATION_GROUPS],
            "source_contract": source_contract, "dependent_healthy_against_original_inspection": True, "dependent_health_observations": health_changes, "automatic_restore": False}


def verify(directory, plan):
    built, active = completed(directory, "build"), completed(directory, "activate")
    before, health_changes = await_original_dependent_health(plan)
    selected = selected_consumers(before["containers"])
    source_contract = managed_source_contract(plan["inspection"]["code_files"], before["code_files"], plan["managed_files"])
    for service, row in selected.items():
        family = FAMILIES[service]
        require(row["container_id"] == active["consumers"][service] and row["image_id"] == built["images"][family]["image_id"], "ACTIVE_CONSUMER_CHANGED")
        image = inspect_image(row["image_id"], plan["binding"])
        container = details(row["container_id"])
        require((container["Config"].get("Labels") or {}).items() >= source_labels(plan["binding"]).items(), "ACTIVE_CONTAINER_LABELS")
        if service in WORKERS:
            require(container["Config"].get("Cmd") == ["python", "-m", "app.workers." + service.replace("-", "_")], "WORKER_ENTRYPOINT_CHANGED")
        if family == "frontend":
            require(image["Config"]["Labels"].get("io.ctf-web.frontend-build-args-sha256") == payload_digest(canonical(before["frontend_args"])), "ACTIVE_FRONTEND_ARGUMENTS")
        if family == "env":
            binary = run(["docker", "exec", row["container_id"], "cat", "/usr/local/bin/env-orchestrator"])
            require(payload_digest(binary) == built["images"][family]["proof"]["binary_sha256"], "ACTIVE_ENV_BINARY_MISMATCH")
    after, later_health = await_original_dependent_health(plan)
    preserved_snapshot(before, after)
    managed_source_contract(plan["inspection"]["code_files"], after["code_files"], plan["managed_files"])
    return {"independent_read_only": True, "images": active["images"], "consumers": active["consumers"], "managed_file_count": len(plan["managed_files"]),
            "non_target_containers_preserved": len(before["containers"]) - len(TARGETS), "protected_files_preserved": 6, "database_heads_preserved": HEADS,
            "source_contract": source_contract, "dependent_healthy_against_original_inspection": True, "dependent_health_observations": health_changes + later_health,
            "automatic_restore": False, "behavior_acceptance_is_separate": True}


def owned_test_status(nonce):
    ids = run(["docker", "ps", "-a", "--no-trunc", "--filter", "label=io.ctf-web.deleted-problem-test-nonce=" + nonce, "--format", "{{.ID}}"]).decode().splitlines()
    return [{"container_id": value["Id"], "image_id": value["Image"], "running": value["State"]["Running"]} for value in (details(identifier) for identifier in ids)]


def status(directory, plan):
    journal = {path.name: json.loads(path.read_bytes()) for path in sorted(directory.glob("*.json")) if path.name.endswith((".intent.json", ".complete.json", ".partial.json"))}
    original = selected_consumers(plan["inspection"]["containers"], running=False)
    try:
        observed = snapshot()
        complete = True
        snapshot_error = None
    except Exception as error:
        observed = {"containers": daemon_rows()}
        complete = False
        snapshot_error = str(error) if isinstance(error, (DeletedProblemReleaseError, InspectionError)) else "FULL_SNAPSHOT_UNAVAILABLE"
    return {"snapshot": observed, "inspection_complete": complete, "snapshot_error": snapshot_error, "journal": journal, "owned_test_containers": owned_test_status(plan["binding"]["release_nonce"]), "replay_or_restore_performed": False,
            "recovery": {"automatic": False, "database_or_object_restore": False, "only_after_confirmed_failure": True,
                         "permitted_services": list(TARGETS), "old_images": {service: value["image_id"] for service, value in original.items()},
                         "source_backup": str(directory / "source.tar"), "runtime_backup": str(directory / "runtime.json"),
                         "procedure": "Inspect exact failed services and source/runtime/image evidence; operator explicitly selects only affected old images and matching source. Never restore databases/objects or control non-target containers."}}


def main():
    directory = None
    lease = None
    try:
        validate_scope(REQUEST["scope"])
        require(ACTION in {"inspect", *PHASES, "image-backup-complete", "fence-backup-complete", "status"}, "REMOTE_ACTION_FORBIDDEN")
        if ACTION == "inspect":
            observed = snapshot()
            selected_consumers(observed["containers"])
            result = {"kind": INSPECTION_KIND, "status": "passed", "snapshot": observed, "observed_at": time.time()}
        else:
            plan = REQUEST["plan"]
            validate_scope(plan["scope"])
            directory = release_directory(create=ACTION == "build")
            result = {"kind": "deleted-problem-" + ACTION, "status": "passed", "binding": plan["binding"], "plan_sha256": REQUEST["plan_sha256"]}
            if ACTION == "status":
                result.update(status(directory, plan))
            else:
                lease = (directory / "lease").open("a+b")
                os.chmod(directory / "lease", 0o600)
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if ACTION != "verify":
                    intent(directory, ACTION)
                if ACTION == "build":
                    result.update(build_phase(directory, plan))
                elif ACTION == "image-backup":
                    completed(directory, "build")
                    preserved_snapshot(plan["inspection"], snapshot())
                    selected = selected_consumers(plan["inspection"]["containers"])
                    ids = sorted({row["image_id"] for row in selected.values()})
                    require(len(ids) == 3, "THREE_OLD_IMAGES_REQUIRED")
                    result.update({"image_ids": ids, "image_metadata": [image_metadata(identifier) for identifier in ids]})
                elif ACTION == "image-backup-complete":
                    prepared = completed(directory, "image-backup")
                    require(REQUEST["artifact"]["bytes"] > 0 and HASH.fullmatch(REQUEST["artifact"]["sha256"]), "IMAGE_ARCHIVE_ATTESTATION")
                    preserved_snapshot(plan["inspection"], snapshot())
                    result.update({"image_ids": prepared["image_ids"], "artifact": REQUEST["artifact"]})
                elif ACTION == "fence-backup":
                    result.update(fence_backup(directory, plan))
                elif ACTION == "fence-backup-complete":
                    prepared = completed(directory, "fence-backup")
                    require(REQUEST["artifact"]["bytes"] == prepared["archive_bytes"] and REQUEST["artifact"]["sha256"] == prepared["archive_sha256"], "STATE_ARCHIVE_ATTESTATION")
                    phase_health = preserved_snapshot(json.loads((directory / "fence.snapshot.json").read_bytes()), snapshot(), learn_maintenance=True)
                    result.update(prepared)
                    result.update({"kind": "deleted-problem-fence-backup", "components": sorted(COMPONENTS), "artifact": REQUEST["artifact"],
                                   "dependent_health_observations": prepared.get("dependent_health_observations", []) + phase_health})
                elif ACTION == "activate":
                    result.update(activate(directory, plan))
                elif ACTION == "verify":
                    result.update(verify(directory, plan))
                filename = ACTION + ".complete.json" if ACTION != "verify" else "verify-" + str(uuid.uuid4()) + ".complete.json"
                private_json(directory / filename, result)
        print(json.dumps(result))
    except Exception as error:
        result = {"kind": INSPECTION_KIND if ACTION == "inspect" else "deleted-problem-" + ACTION, "status": "uncertain", "code": str(error) if isinstance(error, DeletedProblemReleaseError) else "REMOTE_REVIEW_REQUIRED",
                  "automatic_restore": False, "next": "status; inspect action intent and current identities; never replay an uncertain mutation"}
        if directory is not None:
            path = directory / (ACTION + ".partial.json")
            if not path.exists():
                try:
                    private_json(path, result)
                except Exception:
                    result["partial_receipt_write_unverified"] = True
        print(json.dumps(result))
    finally:
        if lease is not None:
            lease.close()
