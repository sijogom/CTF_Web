"""Exact-container graceful write fence and original-identity emergency resume.

Without --execute stop/resume only describe the plan. Explicit inspect is
read-only. No Compose up/down, migration, force kill, restore or cleanup exists.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import inspect
import os
from pathlib import Path
import re
import sys
import time

import ops
import backup_release as backup
from release_preflight import (HASH, INSPECTOR, PROJECT, REMOTE_HELPERS, TASK_ID,
    ReleaseError, canonical, exclusive_json, parse_json, require, sha256,
    source_migrations, task_path, timestamp, validate_inspection, validate_plan, alf_phase, ALF_FIELDS,
    alcf_phase, application_phase_fields)


APIS = ("auth-service", "learn-service", "community-service")
WORKERS = frozenset({
    "personal-notification-runner", "knowledge-candidate-runner",
    "education-asset-cleanup-runner", "challenge-image-cleanup-runner",
    "lesson-image-reconcile-runner", "community-inquiry-runner",
    "marketplace-settlement-runner", "comment-notification-runner",
})
_ROW_KEYS = {"service", "container_id", "image_id", "running", "pid", "status",
    "started_at", "finished_at", "restart_count", "health", "mounts"}
_IDENTITY_KEYS = ("service", "container_id", "image_id", "mounts")
STATE_WAIT_SECONDS = 90
STATE_POLL_SECONDS = 2
LEGACY_SIGNAL_MODULES = {
    "personal-notification-runner": "app.workers.personal_notification_runner",
    "comment-notification-runner": "app.workers.notification_event_runner",
}


def graceful_stop_signal(service, command, pid1):
    if service not in LEGACY_SIGNAL_MODULES:
        return None
    require(command == ["python", "-m", LEGACY_SIGNAL_MODULES[service]], "WORKER_SIGNAL_COMMAND_CHANGED")
    require(isinstance(pid1, dict) and set(pid1) == {"comm", "term", "interrupt"}
            and pid1["comm"] == "python" and type(pid1["term"]) is bool and type(pid1["interrupt"]) is bool,
            "WORKER_SIGNAL_STATE_UNVERIFIED")
    if pid1["term"]:
        return None
    require(pid1["interrupt"], "WORKER_GRACEFUL_HANDLER_MISSING")
    return "SIGINT"

CONTAINER_INSPECTOR = REMOTE_HELPERS + r'''
try:
    print(json.dumps({'schema_version':1,'verified':True,'live_root':str(ROOT),
      'project':PROJECT,'containers':containers()}))
except Exception:
    print(json.dumps({'schema_version':1,'verified':False}))
'''

RESUME_DATABASE_INSPECTOR = REMOTE_HELPERS + r'''
try:
    postgres=singleton(containers(),'postgres')
    print(json.dumps({'schema_version':1,'verified':True,'live_root':str(ROOT),
      'project':PROJECT,'databases':database_rows(postgres['container_id'])}))
except Exception:
    print(json.dumps({'schema_version':1,'verified':False}))
'''


def identity(row):
    return {key: row[key] for key in _IDENTITY_KEYS}


def select_targets(plan, *, learn_only=False):
    rows = plan["inspection"]["containers"]
    require(len({row["container_id"] for row in rows}) == len(rows), "DUPLICATE_CONTAINER_ID")
    apis = []
    alf = alf_phase(plan)
    lf = plan.get("learn_frontend", False)
    alcf_phase(plan)
    selected_apis = ("auth-service", "learn-service") if alf else ("learn-service",) if learn_only or lf else APIS
    for service in selected_apis:
        selected = [row for row in rows if row["service"] == service and row["running"]]
        require(len(selected) == 1, "ORIGINAL_API_AMBIGUOUS")
        apis.extend(selected)
    images = {row["image_id"] for row in apis}
    require(not alf or all(row["image_id"] not in images for row in rows if row["service"] in {
        "community-service", "community-inquiry-runner", "marketplace-settlement-runner", "comment-notification-runner"}), "ALF_RETAINED_IMAGE_SHARED")
    workers = []
    for row in rows:
        if not row["running"] or row["service"] in APIS:
            continue
        if row["service"] in WORKERS and (not (learn_only or alf or lf) or row["image_id"] in images):
            require(row["image_id"] in images, "WRITER_IMAGE_NOT_APPROVED")
            workers.append(row)
        elif row["image_id"] in images:
            # A running migration/shell or unknown same-image service is not an
            # approved writer worker. Do not infer permission to stop it.
            raise ReleaseError("SAME_IMAGE_SERVICE_NOT_APPROVED")
    storage = [row for row in rows if row["service"] == "minio" and row["running"]]
    require(len(storage) == 1, "ORIGINAL_MINIO_AMBIGUOUS")
    workers.sort(key=lambda row: (row["service"], row["container_id"]))
    by_service = {row["service"]: row for row in apis}
    if learn_only or alf or lf:
        expected = {"knowledge-candidate-runner", "education-asset-cleanup-runner", "challenge-image-cleanup-runner", "lesson-image-reconcile-runner"}
        if alf:
            expected.add("personal-notification-runner")
        require({row["service"] for row in workers} == expected, "LEARN_WRITER_SET_INCOMPLETE")
    return {"workers": workers, "apis": [by_service[service] for service in selected_apis], "storage": storage}


def action_targets(plan, action, *, learn_only=False):
    selected = select_targets(plan, learn_only=learn_only)
    if action == "stop":
        return selected["workers"] + list(reversed(selected["apis"])) + selected["storage"]
    if action == "resume-storage":
        return selected["storage"]
    if action == "resume-original":
        return selected["storage"] + selected["apis"] + selected["workers"]
    return selected["workers"] + selected["apis"] + selected["storage"]


def validate_container_inventory(value):
    require(isinstance(value, dict) and set(value) == {
        "schema_version", "verified", "live_root", "project", "containers"}, "CONTAINER_INSPECTION_INVALID")
    require(value["schema_version"] == 1 and value["verified"] is True
        and value["live_root"] == ops.LIVE and value["project"] == PROJECT, "CONTAINER_INSPECTION_UNVERIFIED")
    rows = value["containers"]
    require(isinstance(rows, list) and rows, "CONTAINER_INVENTORY_MISSING")
    ids = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == _ROW_KEYS, "CONTAINER_FIELDS_INVALID")
        require(isinstance(row["container_id"], str) and HASH.fullmatch(row["container_id"])
            and row["container_id"] not in ids, "CONTAINER_ID_INVALID")
        ids.add(row["container_id"])
        require(isinstance(row["image_id"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"])
            and isinstance(row["service"], str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,100}", row["service"]), "CONTAINER_IDENTITY_INVALID")
        require(type(row["running"]) is bool and type(row["pid"]) is int and row["pid"] >= 0
            and (row["pid"] > 0) == row["running"] and row["status"] in {"running", "exited", "created"}
            and (row["status"] == "running") == row["running"], "CONTAINER_STATE_INVALID")
        require(type(row["restart_count"]) is int and row["restart_count"] >= 0
            and row["health"] in {None, "healthy", "unhealthy", "starting"}, "CONTAINER_PROCESS_INVALID")
        require(all(isinstance(row[key], str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", row[key])
            for key in ("started_at", "finished_at")), "CONTAINER_TIMESTAMP_INVALID")
        require(isinstance(row["mounts"], list), "CONTAINER_MOUNTS_INVALID")
        for mount in row["mounts"]:
            require(isinstance(mount, dict) and set(mount) == {"type", "source", "destination", "read_only"}
                and mount["type"] in {"volume", "bind", "tmpfs"} and isinstance(mount["source"], str)
                and isinstance(mount["destination"], str) and type(mount["read_only"]) is bool, "CONTAINER_MOUNT_INVALID")
    return rows


def assert_original_identities(current, targets):
    by_id = {row["container_id"]: row for row in current}
    for target in targets:
        actual = by_id.get(target["container_id"])
        require(actual is not None and identity(actual) == identity(target), "ORIGINAL_CONTAINER_IDENTITY_CHANGED")
        require(not any(row["service"] == target["service"] and row["running"]
            and row["container_id"] != target["container_id"] for row in current), "REPLACEMENT_SERVICE_ALREADY_RUNNING")
    return by_id


def validate_before_stop(plan, inspection, *, learn_only=False):
    validate_inspection(inspection, plan["migrations"])
    require(backup.static_inspection(inspection) == backup.static_inspection(plan["inspection"]), "STOP_STATIC_FENCE_CHANGED")
    require(inspection["containers"] == plan["inspection"]["containers"], "STOP_PROCESS_FENCE_CHANGED")
    select_targets(plan, learn_only=learn_only)


def validate_resume_databases(value, plan):
    require(isinstance(value, dict) and set(value) == {"schema_version", "verified", "live_root", "project", "databases"}
        and value["schema_version"] == 1 and value["verified"] is True and value["live_root"] == ops.LIVE
        and value["project"] == PROJECT, "RESUME_DATABASE_INSPECTION_UNVERIFIED")
    expected = plan["inspection"]["databases"]
    require(isinstance(value["databases"], dict) and set(value["databases"]) == set(expected), "RESUME_DATABASE_SET_CHANGED")
    keys = {"head", "bytes", "table_count", "client_session_count", "database", "schema_sha256"}
    for service, row in value["databases"].items():
        require(isinstance(row, dict) and set(row) == keys and isinstance(row["head"], list)
            and isinstance(row["schema_sha256"], str) and HASH.fullmatch(row["schema_sha256"])
            and all(type(row[key]) is int and row[key] >= 0 for key in ("bytes", "table_count", "client_session_count")),
            "RESUME_DATABASE_FIELDS_INVALID")
        require(all(row[key] == expected[service][key] for key in ("database", "head", "schema_sha256", "table_count")),
            "ORIGINAL_RESUME_DATABASE_SCHEMA_CHANGED")


def assert_resume_databases(plan):
    validate_resume_databases(ops.remote_python(RESUME_DATABASE_INSPECTOR, timeout=180), plan)


def assert_no_activation_attempt(plan):
    for name in ("activate-intent-", "activate-complete-"):
        path = task_path(ops.TASK / "evidence" / (name + plan["nonce"] + ".json"), ops.TASK / "evidence")
        require(not path.exists(), "ORIGINAL_RESUME_ACTIVATION_ALREADY_ATTEMPTED")


def assert_unrelated_unchanged(before, after, target_id):
    prior = {row["container_id"]: row for row in before}
    require(set(prior) == {row["container_id"] for row in after}, "CONTAINER_SET_CHANGED_DURING_MUTATION")
    # Health can finish transitioning while a dependent service starts; all
    # process, mount and image identities remain an exact fence.
    require(all({key: value for key, value in row.items() if key != "health"}
        == {key: value for key, value in prior[row["container_id"]].items() if key != "health"}
        for row in after if row["container_id"] != target_id), "UNRELATED_CONTAINER_CHANGED_DURING_MUTATION")


def wait_for_target(target, current, *, start, healthy=False):
    deadline = time.monotonic() + STATE_WAIT_SECONDS
    original = {row["container_id"]: row for row in current}[target["container_id"]]
    observed_running = None
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "ORIGINAL_HEALTH_UNVERIFIED" if healthy else "MUTATION_OUTCOME_UNCONFIRMED")
        try:
            value = ops.remote_python(CONTAINER_INSPECTOR, timeout=min(30, max(1, remaining)))
        except Exception:
            # Only read-only observation is retried, never the start/stop command.
            time.sleep(min(STATE_POLL_SECONDS, max(0, deadline - time.monotonic())))
            continue
        after = validate_container_inventory(value)
        actual = assert_original_identities(after, [target])[target["container_id"]]
        assert_unrelated_unchanged(current, after, target["container_id"])
        if not start:
            require(actual["started_at"] == original["started_at"] and actual["restart_count"] == original["restart_count"],
                "TARGET_RESTARTED_DURING_STOP")
        desired = actual["running"] is start and (
            actual["pid"] > 0 and actual["status"] == "running" if start
            else actual["pid"] == 0 and actual["status"] == "exited")
        if desired and (not healthy or actual["health"] == "healthy"):
            if not start or healthy:
                return after
            process = {key: actual[key] for key in ("pid", "started_at", "restart_count")}
            if process == observed_running:
                return after
            observed_running = process
        else:
            observed_running = None
        time.sleep(min(STATE_POLL_SECONDS, max(0, deadline - time.monotonic())))


def mutation_program(target, *, start):
    require(type(start) is bool and set(target) == _ROW_KEYS, "MUTATION_TARGET_INVALID")
    require(HASH.fullmatch(target["container_id"]) and target["service"] in {*APIS, *WORKERS, "minio"}, "MUTATION_TARGET_FORBIDDEN")
    wanted = identity(target)
    return (REMOTE_HELPERS + "\ndef require(condition, code):\n    if not condition: fail(code)\n"
        + inspect.getsource(graceful_stop_signal)
        + "\nLEGACY_SIGNAL_MODULES=" + repr(LEGACY_SIGNAL_MODULES)
        + "\nEXPECTED=" + repr(wanted) + "\nSTART=" + repr(start) + r'''
try:
    rows=containers()
    matches=[row for row in rows if row['container_id']==EXPECTED['container_id']]
    if len(matches)!=1 or {key:matches[0][key] for key in EXPECTED}!=EXPECTED: fail('IDENTITY_CHANGED')
    if any(row['service']==EXPECTED['service'] and row['running'] and row['container_id']!=EXPECTED['container_id'] for row in rows): fail('REPLACEMENT_RUNNING')
    target=matches[0]
    if target['running']==START:
        result='already_desired'
    else:
        selected_signal=None
        if not START and target['service'] in LEGACY_SIGNAL_MODULES:
            cmd=json.loads(subprocess.check_output(['docker','inspect','--format','{{json .Config.Cmd}}',target['container_id']],text=True,timeout=15))
            probe="import json,pathlib; p=pathlib.Path('/proc/1'); s=dict(x.split(':',1) for x in (p/'status').read_text().splitlines() if ':' in x); print(json.dumps({'comm':(p/'comm').read_text().strip(),'term':bool(int(s['SigCgt'].strip(),16)&(1<<14)),'interrupt':bool(int(s['SigCgt'].strip(),16)&(1<<1))}))"
            pid1=json.loads(subprocess.check_output(['docker','exec',target['container_id'],'python','-I','-B','-c',probe],text=True,timeout=15))
            selected_signal=graceful_stop_signal(target['service'],cmd,pid1)
            if selected_signal:
                help_text=subprocess.check_output(['docker','stop','--help'],text=True,timeout=15)
                require('--signal' in help_text,'DOCKER_STOP_SIGNAL_UNSUPPORTED')
        command=['docker','start',target['container_id']] if START else ['docker','stop',*(['--signal',selected_signal] if selected_signal else []),'--time','-1',target['container_id']]
        # --time -1 never escalates to SIGKILL. Timeout is an uncertain daemon
        # outcome; the caller must inspect, never issue a blind second command.
        process=subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90)
        result='acknowledged' if process.returncode==0 else 'unknown'
    print(json.dumps({'schema_version':1,'kind':'renewal-container-mutation','result':result}))
except Exception:
    print(json.dumps({'schema_version':1,'kind':'renewal-container-mutation','result':'unknown'}))
''')


def mutate_and_inspect(target, current, *, start):
    try:
        ops.remote_python(mutation_program(target, start=start), timeout=240)
    except Exception:
        # SSH loss may follow a successful stop/start. No command retry here.
        pass
    return wait_for_target(target, current, start=start, healthy=start and target["health"] is not None)


def receipt_path(action, nonce, suffix):
    return task_path(ops.TASK / "evidence" / f"quiesce-{action}-{suffix}-{nonce}.json", ops.TASK / "evidence")


def target_stop_path(plan, target, suffix):
    return receipt_path("stop", plan["nonce"], suffix + "-" + target["container_id"])


def target_stop_claim(plan, plan_hash, target, suffix):
    return {"schema_version": 1, "kind": "renewal-stop-" + suffix, "task": TASK_ID, "nonce": plan["nonce"],
        "plan_sha256": plan_hash, "source_fingerprint": plan["source_fingerprint"], "target": identity(target)}


def validate_target_stop_claim(path, expected):
    require(parse_json(path.read_bytes()) == expected, "ORIGINAL_STOP_TARGET_RECEIPT_CHANGED")


def settle_previous_stop(plan, plan_hash, targets, current, stop_claim):
    protocol = stop_claim.get("stop_attempt_protocol")
    complete = receipt_path("stop", plan["nonce"], "complete")
    if protocol != 1:
        # Older/unknown attempts cannot distinguish a pending SIGTERM from an
        # untouched running process. Do not claim recovery from that ambiguity.
        require(complete.is_file(), "ORIGINAL_STOP_PROTOCOL_UNVERIFIED")
        previous = parse_json(complete.read_bytes())
        require(previous.get("kind") == "renewal-quiescence" and previous.get("nonce") == plan["nonce"]
            and previous.get("plan_sha256") == plan_hash and previous.get("source_fingerprint") == plan["source_fingerprint"],
            "ORIGINAL_STOP_COMPLETION_CHANGED")
        stopped = assert_original_identities(previous.get("inspection", {}).get("containers", []), targets)
        require(all(not stopped[row["container_id"]]["running"] and stopped[row["container_id"]]["pid"] == 0 for row in targets),
            "ORIGINAL_STOP_COMPLETION_UNVERIFIED")
        return current
    for target in targets:
        attempt = target_stop_path(plan, target, "attempt")
        settled = target_stop_path(plan, target, "settled")
        if not attempt.exists():
            require(not settled.exists(), "ORIGINAL_STOP_SETTLEMENT_WITHOUT_ATTEMPT")
            continue
        validate_target_stop_claim(attempt, target_stop_claim(plan, plan_hash, target, "attempt"))
        expected = target_stop_claim(plan, plan_hash, target, "settled")
        if settled.exists():
            validate_target_stop_claim(settled, expected)
            continue
        try:
            current = wait_for_target(target, current, start=False)
        except ReleaseError as error:
            if str(error) == "MUTATION_OUTCOME_UNCONFIRMED":
                raise ReleaseError("ORIGINAL_STOP_STILL_IN_FLIGHT") from error
            raise
        exclusive_json(settled, expected)
    return current


@contextmanager
def action_lease(plan, plan_hash):
    """A process-bound nonce lease prevents concurrent stop/resume actions."""
    path = task_path(ops.TASK / "evidence" / f"quiesce-active-{plan['nonce']}.lock", ops.TASK / "evidence")
    payload = canonical({"task": TASK_ID, "nonce": plan["nonce"], "plan_sha256": plan_hash}) + b"\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as created:
            created.write(payload)
            created.flush()
            os.fsync(created.fileno())
    except FileExistsError:
        pass
    with path.open("r+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ReleaseError("QUIESCENCE_ACTION_IN_PROGRESS") from error
        try:
            require(stream.read() == payload, "QUIESCENCE_LEASE_BINDING_CHANGED")
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_plan(path, expected_hash, action):
    require(isinstance(expected_hash, str) and HASH.fullmatch(expected_hash), "PLAN_SHA256_REQUIRED")
    path = task_path(path, ops.TASK / "evidence")
    payload = path.read_bytes()
    require(sha256(payload) == expected_hash, "PLAN_SHA256_MISMATCH")
    plan = parse_json(payload)
    if action == "stop":
        source = ops.manifest()
        validate_plan(plan, source["fingerprint"])
        require(plan["source_file_count"] == len(source["files"]), "SOURCE_FILE_COUNT_CHANGED")
        require(plan["migrations"] == source_migrations(), "SOURCE_MIGRATIONS_CHANGED")
    else:
        # Emergency resume must not depend on today's source or an expired
        # planning clock. All structural/target/nonce contracts still apply.
        require(isinstance(plan, dict), "PLAN_SCHEMA_INVALID")
        require(isinstance(plan.get("source_fingerprint"), str) and HASH.fullmatch(plan["source_fingerprint"]), "SOURCE_FINGERPRINT_INVALID")
        validate_plan(plan, plan.get("source_fingerprint"), now=timestamp(plan.get("created_at")))
    select_targets(plan)
    return plan


def execute(plan_path, plan_hash, action, *, execute_approved=False, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(action in {"stop", "resume-storage", "resume-original", "inspect"}, "ACTION_INVALID")
    plan = load_plan(plan_path, plan_hash, action)
    require(plan.get("learn_frontend", False) is learn_frontend, "QUIESCENCE_PLAN_SCOPE_CHANGED")
    require(plan.get("learn_only", False) is learn_only, "QUIESCENCE_PLAN_SCOPE_CHANGED")
    require(alf_phase(plan) is auth_learn_frontend, "QUIESCENCE_PLAN_SCOPE_CHANGED")
    require(alcf_phase(plan) is auth_learn_community_frontend, "QUIESCENCE_PLAN_SCOPE_CHANGED")
    targets = action_targets(plan, action, learn_only=learn_only)
    if action != "inspect" and not execute_approved:
        return {"execute": False, "action": action, "plan_sha256": plan_hash,
                "target_count": len(targets), "target_identity_sha256": sha256(canonical([identity(row) for row in targets]))}
    if action == "inspect":
        inspection = ops.remote_python(INSPECTOR, timeout=900)
        validate_inspection(inspection, plan["migrations"], quiesced=True)
        by_id = assert_original_identities(inspection["containers"], targets)
        return {"execute": False, "action": action, "inspection_sha256": sha256(canonical(inspection)),
                "target_count": len(targets), "running_target_count": sum(by_id[row["container_id"]]["running"] for row in targets)}
    require(execute_approved is True, "EXECUTE_APPROVAL_REQUIRED")
    with action_lease(plan, plan_hash):
        return _execute_mutating_action(plan, plan_hash, action, targets, learn_only=learn_only)


def _execute_mutating_action(plan, plan_hash, action, targets, *, learn_only=False):
    nonce = plan["nonce"]
    intent, completion = receipt_path(action, nonce, "intent"), receipt_path(action, nonce, "complete")
    require(not intent.exists() and not completion.exists()
        and not receipt_path(action, nonce, "partial").exists(), "QUIESCENCE_ACTION_ALREADY_ATTEMPTED")
    if action != "stop":
        previous = receipt_path("stop", nonce, "intent")
        require(previous.is_file(), "ORIGINAL_STOP_INTENT_REQUIRED")
        claim = parse_json(previous.read_bytes())
        require(claim.get("kind") == "renewal-quiescence-intent" and claim.get("task") == TASK_ID
            and claim.get("action") == "stop" and claim.get("nonce") == nonce
            and claim.get("plan_sha256") == plan_hash and claim.get("source_fingerprint") == plan["source_fingerprint"]
            and claim.get("learn_only", False) is learn_only
            and all(claim.get(key) == plan.get(key) for key in application_phase_fields(plan))
            and claim.get("targets") == [identity(row) for row in action_targets(plan, "stop", learn_only=learn_only)], "ORIGINAL_STOP_INTENT_MISMATCH")
    if action == "stop":
        inspection = ops.remote_python(INSPECTOR, timeout=900)
        validate_before_stop(plan, inspection, learn_only=learn_only)
        current = inspection["containers"]
        validate_plan(plan, plan["source_fingerprint"])
        require(ops.manifest()["fingerprint"] == plan["source_fingerprint"], "SOURCE_CHANGED_BEFORE_STOP")
    else:
        if action == "resume-original":
            assert_no_activation_attempt(plan)
            assert_resume_databases(plan)
        current = validate_container_inventory(ops.remote_python(CONTAINER_INSPECTOR, timeout=180))
        assert_original_identities(current, targets)
        current = settle_previous_stop(plan, plan_hash, targets, current, claim)
    exclusive_json(intent, {"schema_version": 1, "kind": "renewal-quiescence-intent", "task": TASK_ID,
        "nonce": nonce, "action": action, "plan_sha256": plan_hash,
        "source_fingerprint": plan["source_fingerprint"], "created_at": datetime.now(timezone.utc).isoformat(),
        "targets": [identity(row) for row in targets], **({"stop_attempt_protocol": 1} if action == "stop" else {}),
        **({"learn_only": True} if learn_only else {}), **{key: plan[key] for key in application_phase_fields(plan)}})
    try:
        mutation_count = 0
        for target in targets:
            start = action != "stop"
            by_id = assert_original_identities(current, [target])
            if action == "resume-original" and target["service"] != "minio":
                assert_no_activation_attempt(plan)
                assert_resume_databases(plan)
            if by_id[target["container_id"]]["running"] is start:
                if start:
                    current = wait_for_target(target, current, start=True, healthy=target["health"] is not None)
                continue
            if action == "stop":
                validate_plan(plan, plan["source_fingerprint"])
                require(ops.manifest()["fingerprint"] == plan["source_fingerprint"], "SOURCE_CHANGED_DURING_STOP")
                exclusive_json(target_stop_path(plan, target, "attempt"), target_stop_claim(plan, plan_hash, target, "attempt"))
            current = mutate_and_inspect(target, current, start=start)
            if action == "stop":
                exclusive_json(target_stop_path(plan, target, "settled"), target_stop_claim(plan, plan_hash, target, "settled"))
            mutation_count += 1
        if action == "stop":
            frozen = ops.remote_python(INSPECTOR, timeout=900)
            result = {"schema_version": 1, "kind": "renewal-quiescence", "task": TASK_ID,
                "nonce": nonce, "plan_sha256": plan_hash, "source_fingerprint": plan["source_fingerprint"],
                "verified_at": datetime.now(timezone.utc).isoformat(), "drain_verified": True,
                "external_writers_absent_verified": True, "fence_maintained_until_capture_complete": True,
                "inspection": frozen, **({"learn_only": True} if learn_only else {}),
                **{key: plan[key] for key in application_phase_fields(plan)}}
            # This exact DTO is the backup consumer contract. Its subsequent
            # per-stream fence checks enforce continued quiescence until capture.
            backup.validate_quiescence(result, plan, plan_hash, learn_only=learn_only)
            require(ops.manifest()["fingerprint"] == plan["source_fingerprint"], "SOURCE_CHANGED_DURING_STOP")
        else:
            if action == "resume-original":
                assert_no_activation_attempt(plan)
                assert_resume_databases(plan)
            final = validate_container_inventory(ops.remote_python(CONTAINER_INSPECTOR, timeout=180))
            by_id = assert_original_identities(final, targets)
            require(all(by_id[row["container_id"]]["running"] and (
                by_id[row["container_id"]]["health"] == "healthy" if row["health"] is not None
                else by_id[row["container_id"]]["health"] is None) for row in targets), "ORIGINAL_RESUME_UNVERIFIED")
            require(final == current, "CONTAINER_STATE_CHANGED_AFTER_RESUME")
            result = {"schema_version": 1, "kind": "renewal-quiescence-resume", "task": TASK_ID,
                "nonce": nonce, "action": action, "plan_sha256": plan_hash,
                "source_fingerprint": plan["source_fingerprint"], "verified_at": datetime.now(timezone.utc).isoformat(),
                "original_identity_verified": True, "health_verified": True,
                "readiness_checked_count": sum(row["health"] is not None for row in targets),
                "process_only_without_readiness_count": sum(row["health"] is None for row in targets),
                "database_schema_original_verified": action == "resume-original", "containers": final}
        completed_hash = exclusive_json(completion, result)
        return {"execute": True, "action": action, "receipt": str(completion), "receipt_sha256": completed_hash,
                "plan_sha256": plan_hash, "target_count": len(targets), "mutation_attempt_count": mutation_count}
    except Exception as error:
        code = str(error) if isinstance(error, ReleaseError) else type(error).__name__
        exclusive_json(receipt_path(action, nonce, "partial"), {"schema_version": 1,
            "kind": "renewal-quiescence-partial", "task": TASK_ID, "nonce": nonce, "action": action,
            "plan_sha256": plan_hash, "error": code, "last_verified_containers": current,
            "automatic_retry_or_restore": False})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--action", choices=("stop", "resume-storage", "resume-original", "inspect"), default="stop")
    parser.add_argument("--execute", action="store_true")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--learn-only", action="store_true")
    scope.add_argument("--auth-learn-frontend", action="store_true")
    scope.add_argument("--auth-learn-community-frontend", action="store_true")
    scope.add_argument("--learn-frontend", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(execute(args.plan, args.plan_sha256, args.action, execute_approved=args.execute, learn_only=args.learn_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__,
                          "partial_receipts": "preserved_no_automatic_retry_or_restore"}))
        sys.exit(1)
