"""Explicit, source-bound activation. Default is local planning without SSH.

Only selected managed source files and selected service images may change.
Protected Compose/env/Caddy/key files and unknown extra files are preserved.
Post-migration backend failures keep writers fenced, never restart old writers
or migration jobs and never restore databases. A confirmed frontend-only
failure may restore only its prior image. Ambiguous outcomes require inspection.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import re
import shlex
import sys

import ops
import release_preflight as preflight
import backup_release
import backup_images
import stage_build_release as stage
import verify_release as verify
import quiesce_release as quiesce


SERVICE_IMAGES = dict(verify.SERVICE_IMAGES)
MIGRATION_IMAGES = dict(verify.MIGRATION_IMAGES)
EXPECTED_HEADS = dict(verify.EXPECTED_HEADS)
TRIGGER_TABLES = tuple(verify.TRIGGER_TABLES)
LEGACY_PROCESS_WORKERS = dict(verify.LEGACY_PROCESS_WORKERS)
API_ORDER = ("auth-service", "learn-service", "community-service")
NEW_WORKERS = {
    "personal-notification-runner": ("auth-service", "app.workers.personal_notification_runner", 8040, "PERSONAL_NOTIFICATION_HEALTH_PORT"),
    "marketplace-settlement-runner": ("community-service", "app.workers.marketplace_settlement_runner", 8320, "MARKETPLACE_SETTLEMENT_HEALTH_PORT"),
    "comment-notification-runner": ("community-service", "app.workers.notification_event_runner", 8330, "COMMENT_NOTIFICATION_HEALTH_PORT"),
}
PGOPTIONS = "-c lock_timeout=10000 -c statement_timeout=120000"
SCHEMA_TABLES = {"auth": ("marketplace_point_transfers", "personal_notifications", "notification_policies", "personal_notification_events", "competition_notification_states", "badge_observations"),
                 "learn": ("knowledge_graph_revision", "knowledge_relations", "challenge_knowledge_tags", "course_knowledge_tags", "lesson_knowledge_tags"),
                 "community": ("marketplace_purchase_operations", "marketplace_settlement_outbox", "comment_notification_events")}
SCHEMA_COLUMNS = {"auth": ("users.competition_notifications", "users.comment_notifications", "users.badge_notifications"),
                  "learn": ("challenge_knowledge_tags.source_relation_id", "course_knowledge_tags.source_relation_id", "lesson_knowledge_tags.source_relation_id"),
                  "community": ()}
EXECUTOR_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
COMPONENT_SOURCE_SHA256 = {name:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                          for name,module in (("ops",ops),("release_preflight",preflight),("backup_release",backup_release),("backup_images",backup_images),
                                              ("stage_build_release",stage),("verify_release",verify),("quiesce_release",quiesce))}
RELEASE_ROOT = "/home/deploy/.ctf-web-renewal/releases"
learn_migration_rows = preflight.learn_migration_rows
validate_code_state = preflight.validate_code_state
validate_code_contract = preflight.validate_code_contract
validate_code_preservation = preflight.validate_code_preservation
code_runtime = preflight.code_runtime
validate_alf_contract = preflight.validate_alf_contract
validate_alcf_contract = preflight.validate_alcf_contract
validate_lf_contract = preflight.validate_lf_contract
ALCF_TEAM_SCHEMA_SQL = verify.ALCF_TEAM_SCHEMA_SQL
ALCF_ATTACHMENT_SCHEMA_SQL = verify.ALCF_ATTACHMENT_SCHEMA_SQL

# Loaded by Python itself in migration children, including Learn's Alembic
# subprocess. PGOPTIONS is also set, but asyncpg needs explicit server_settings.
MIGRATION_SITE_CUSTOMIZE = '''import os
try:
    if os.environ.get("PGOPTIONS") != "-c lock_timeout=10000 -c statement_timeout=120000":
        raise RuntimeError("MIGRATION_TIMEOUT_ENV_INVALID")
    import asyncpg
    _renewal_connect = asyncpg.connect
    async def _renewal_bounded_connect(*args, **kwargs):
        settings = dict(kwargs.get("server_settings") or {})
        settings.update({"lock_timeout":"10000", "statement_timeout":"120000"})
        kwargs["server_settings"] = settings
        return await _renewal_connect(*args, **kwargs)
    asyncpg.connect = _renewal_bounded_connect
except Exception:
    os._exit(88)
'''


class ActivateError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ActivateError(code)


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def backup_contract(value, binding, plan_sha, quiescence_sha, old_containers, *, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(isinstance(value,dict) and value.get("schema_version") == 1 and value.get("kind") == "renewal-stream-backup"
            and value.get("nonce") == binding["release_nonce"] and value.get("source_fingerprint") == binding["source_fingerprint"]
            and value.get("plan_sha256") == plan_sha and value.get("quiescence_sha256") == quiescence_sha, "BACKUP_BINDING_MISMATCH")
    require(all(value.get(key) is True for key in ("quiescence_verified", "release_eligible", "image_backup_verified"))
            and value.get("learn_only", False) is learn_only and value.get("ctfd_db_included") is False, "BACKUP_NOT_RELEASE_ELIGIBLE")
    require(preflight.alf_phase(value) is auth_learn_frontend, "BACKUP_SCOPE_CHANGED")
    require(preflight.alcf_phase(value) is auth_learn_community_frontend, "BACKUP_SCOPE_CHANGED")
    require(preflight.lf_phase(value) is learn_frontend, "BACKUP_SCOPE_CHANGED")
    if auth_learn_community_frontend:
        validate_alcf_contract(value["alcf_contract"])
        require(value.get("captured_databases") == list(preflight.DATABASES.values()), "ALCF_BACKUP_SCOPE_UNVERIFIED")
    if auth_learn_frontend:
        preflight.validate_alf_contract(value["alf_contract"])
        require(value.get("database_count") == 2 and value.get("cross_service_consistency_verified") is False
                and value.get("auth_learn_object_consistency_verified") is True
                and value.get("captured_databases") == ["platform_db", "learn_db"], "ALF_BACKUP_SCOPE_UNVERIFIED")
    elif learn_only or learn_frontend:
        require(value.get("database_count") == 1 and value.get("cross_service_consistency_verified") is False
                and value.get("learn_object_consistency_verified") is True and value.get("captured_databases") == ["learn_db"], "LEARN_BACKUP_SCOPE_UNVERIFIED")
    else:
        require(value.get("database_count") == 3 and value.get("cross_service_consistency_verified") is True, "BACKUP_NOT_RELEASE_ELIGIBLE")
    selected = []
    for service in (("learn-service", "frontend") if learn_frontend else ("auth-service", "learn-service", "frontend") if auth_learn_frontend else (*API_ORDER,"frontend")):
        rows = [row for row in old_containers if row["service"] == service and row["running"]]
        require(len(rows) == 1,"OLD_IMAGE_BASELINE_AMBIGUOUS")
        selected.append(rows[0]["image_id"])
    require(value.get("selected_image_ids") == sorted(set(selected)),"IMAGE_BACKUP_BASELINE_MISMATCH")
    require(isinstance(value.get("image_backup_receipt_sha256"),str)
            and re.fullmatch(r"[0-9a-f]{64}",value["image_backup_receipt_sha256"]),"IMAGE_BACKUP_RECEIPT_UNVERIFIED")
    require(isinstance(value.get("files"),list) and len(value["files"]) >= (7 if auth_learn_frontend else 6 if learn_only or learn_frontend else 8), "BACKUP_FILES_INCOMPLETE")
    return value


def verify_backup_files(value, private_root=None):
    original_root = Path(private_root or backup_release.PRIVATE_ROOT).absolute()
    require(not any(ops.linked(parent) for parent in (original_root,*original_root.parents)
                    if parent == original_root or parent.is_relative_to(ops.TASK)),"BACKUP_PRIVATE_ROOT_LINKED")
    root = original_root.resolve()
    seen = set()
    for row in value["files"]:
        require(isinstance(row,dict) and isinstance(row.get("path"),str) and row["path"] not in seen
                and type(row.get("bytes")) is int and row["bytes"] > 0 and re.fullmatch(r"[0-9a-f]{64}",row.get("sha256","")), "BACKUP_FILE_INVALID")
        path = Path(row["path"])
        require(path.is_absolute() and path.resolve().is_relative_to(root) and path.is_file()
                and not any(ops.linked(parent) for parent in (path,*path.parents) if parent.is_relative_to(root)), "BACKUP_FILE_PATH_INVALID")
        require(path.stat().st_size == row["bytes"], "BACKUP_FILE_SIZE_CHANGED")
        with path.open("rb") as stream:
            require(hashlib.file_digest(stream,"sha256").hexdigest() == row["sha256"], "BACKUP_FILE_HASH_CHANGED")
        seen.add(row["path"])
    return len(seen)


def compatibility_image_backup(path, expected_sha, plan, plan_sha, baseline, *, required=False):
    require((path is None) == (expected_sha is None), "COMPATIBILITY_IMAGE_BACKUP_INPUT_PAIR_REQUIRED")
    require(not required or path is not None, "COMPATIBILITY_IMAGE_BACKUP_REQUIRED")
    if path is None:
        return None
    require(required and isinstance(expected_sha,str) and re.fullmatch(r"[0-9a-f]{64}",expected_sha), "COMPATIBILITY_IMAGE_BACKUP_SCOPE_INVALID")
    value, actual_sha = verify.load_receipt(path)
    require(actual_sha == expected_sha, "COMPATIBILITY_IMAGE_BACKUP_HASH_CHANGED")
    backup_images.validate_receipt(value, plan, plan_sha)
    require(baseline["expected_heads"]["learn"] == verify.LEARN_CATEGORY_HEAD and baseline["images"]["learn"] in value["selected_image_ids"],
            "COMPATIBILITY_LEARN_IMAGE_NOT_CAPTURED")
    return actual_sha


def selected_rows(manifest, frontend_only=False, *, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    verify.release_families(frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend,
                            auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend)
    prefixes = ("services/learn/", "frontend/") if learn_frontend else ("services/auth/", "services/learn/", "frontend/") if auth_learn_frontend else ("frontend/",) if frontend_only else ("services/learn/",) if learn_only else verify.CONTEXTS
    return verify.file_rows([row for row in manifest["files"] if row["path"].startswith(prefixes)])


def load_baseline(paths, manifest, *, frontend_only=True, learn_only=False, chain_mode=False, learn_code_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    if chain_mode:
        chain = verify.load_baseline_chain(paths)
        contract = chain["contract"]
        require(not learn_code_only or learn_only and not frontend_only, "LEARN_CODE_SCOPE_INVALID")
        expected_head = "20261013_029" if learn_code_only else "20261013_028"
        require(not learn_only or contract["expected_heads"]["learn"] == expected_head, "LEARN_COMPATIBILITY_BASELINE_HEAD_INVALID")
        if learn_code_only:
            require(learn_migration_rows(manifest["files"]) == learn_migration_rows(contract["managed_files"]), "LEARN_CODE_MIGRATION_BYTES_CHANGED")
        verify.retained_source_contract(contract, manifest["files"], frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend,
                                        auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend)
        anchor = chain["backup_anchor"]
        return {"contract": contract, "activation": anchor["activation"], "old_containers": anchor["build"]["remote"]["live_before"]["containers"],
                "hashes": chain["hashes"], "backup_binding": anchor["contract"]["binding"]}
    activation, activation_sha = verify.load_receipt(paths[0])
    built, build_sha = verify.load_receipt(paths[1])
    checked, checked_sha = verify.load_receipt(paths[2])
    prior = activation.get("binding",{})
    binding = verify.binding_for(prior.get("commit"),prior.get("source_fingerprint"),prior.get("release_nonce"))
    verify.baseline_verify_contract(checked,binding,build_sha,activation_sha)
    contract = verify.activation_contract(activation,binding,activation.get("managed_files"),verify.build_contract(built,binding))
    require([row for row in selected_rows(manifest) if not row["path"].startswith("frontend/")]
            == [row for row in contract["managed_files"] if not row["path"].startswith("frontend/")], "FRONTEND_ONLY_BACKEND_SOURCE_CHANGED")
    return {"contract":contract,"activation":activation,"old_containers":built["remote"]["live_before"]["containers"],
            "hashes":{"activation":activation_sha,"build":build_sha,"verify":checked_sha}}


def activation_steps(worker_names=(), *, frontend_only=False, learn_only=False, learn_code_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, migration_services=(), learn_frontend=False):
    require(sum((frontend_only, learn_only, auth_learn_frontend, auth_learn_community_frontend, learn_frontend)) <= 1, "ACTIVATION_SCOPE_INVALID")
    require(not learn_code_only or learn_only and not frontend_only, "LEARN_CODE_SCOPE_INVALID")
    if frontend_only:
        return [("source","frontend"),("up","frontend"),("health","frontend"),("verify","all")]
    if learn_frontend:
        require(not learn_code_only and list(migration_services) == ["learn"] and set(worker_names) == set(LEGACY_PROCESS_WORKERS), "LF_TARGET_SET_INVALID")
        return ([("source", "managed"), ("migration", "learn"), ("up", "learn-service"), ("health", "learn-service"), ("schema", "all")]
                + [(action, name) for name in worker_names for action in ("up", "health")]
                + [("up", "frontend"), ("health", "frontend"), ("verify", "all")])
    if auth_learn_frontend:
        require(not learn_code_only and list(migration_services) == [name for name in ("auth", "learn") if name in migration_services], "ALF_MIGRATION_SCOPE_CHANGED")
        require(set(worker_names) == {"personal-notification-runner", *LEGACY_PROCESS_WORKERS}, "ALF_WORKER_SET_INCOMPLETE")
        return ([("source", "managed")] + [("migration", name) for name in migration_services]
                + [(action, name) for name in ("auth-service", "learn-service") for action in ("up", "health")]
                + [("schema", "all")] + [(action, name) for name in worker_names for action in ("up", "health")]
                + [("up", "frontend"), ("health", "frontend"), ("verify", "all")])
    if learn_only:
        require(all(SERVICE_IMAGES.get(name) == "learn" and name != "learn-service" for name in worker_names), "LEARN_WORKER_SCOPE_INVALID")
        return ([("source", "learn")] + ([] if learn_code_only else [("migration", "learn")])
                + [("up", "learn-service"), ("health", "learn-service"), ("schema", "all")]
                + [(action, name) for name in worker_names for action in ("up", "health")] + [("verify", "all")])
    if auth_learn_community_frontend:
        require(not learn_code_only and list(migration_services) == [name for name in ("auth", "learn", "community") if name in migration_services], "ALCF_MIGRATION_SCOPE_CHANGED")
    migrations = [("migration", name) for name in migration_services] if auth_learn_community_frontend else [("migration", name) for name in ("auth", "learn", "community")]
    return ([("source","managed"),*migrations]
            + [(action,service) for service in API_ORDER for action in ("up","health")]
            + [("schema","all"),("baseline","auth")]
            + [(action,service) for service in worker_names for action in ("up","health")]
            + [("up","frontend"),("health","frontend"),("verify","all")])


def derive_workers(current, services, *, auth_learn_frontend=False, learn_frontend=False):
    images = {}
    for service in (("learn-service",) if learn_frontend else ("auth-service", "learn-service") if auth_learn_frontend else API_ORDER):
        rows = [row for row in current if row["service"] == service]
        require(len(rows) == 1, "API_CONTAINER_AMBIGUOUS")
        images[rows[0]["image_id"]] = SERVICE_IMAGES[service]
    workers = []
    for row in current:
        name = row["service"]
        if learn_frontend and name in MIGRATION_IMAGES:
            require(not row["running"], "LF_RUNNING_MIGRATION_FORBIDDEN")
            continue
        if learn_frontend and SERVICE_IMAGES.get(name) not in {"learn", "frontend"}:
            require(row["image_id"] not in images, "LF_RETAINED_IMAGE_SHARED")
            continue
        if auth_learn_frontend and SERVICE_IMAGES.get(name) == "community":
            require(row["image_id"] not in images, "ALF_RETAINED_IMAGE_SHARED")
            continue
        if name in SERVICE_IMAGES and name not in (*API_ORDER,"frontend"):
            require(row["image_id"] in images and images[row["image_id"]] == SERVICE_IMAGES[name],"WORKER_API_IMAGE_MISMATCH")
        if row["image_id"] in images and name not in API_ORDER and name not in MIGRATION_IMAGES:
            require(name in SERVICE_IMAGES and SERVICE_IMAGES[name] == images[row["image_id"]]
                    and (name in services or name in NEW_WORKERS), "UNKNOWN_SAME_IMAGE_CONSUMER")
            workers.append(name)
    for name in NEW_WORKERS:
        if learn_frontend:
            continue
        if auth_learn_frontend and SERVICE_IMAGES[name] != "auth":
            continue
        if name not in workers:
            workers.append(name)
    require(not auth_learn_frontend or set(workers) == {"personal-notification-runner", *LEGACY_PROCESS_WORKERS}, "ALF_WORKER_SET_INCOMPLETE")
    require(not learn_frontend or set(workers) == set(LEGACY_PROCESS_WORKERS), "LF_WORKER_SET_INCOMPLETE")
    return sorted(workers)


PERSONAL_WORKER = "personal-notification-runner"
PERSONAL_WORKER_COMMAND = ["python", "-m", "app.workers.personal_notification_runner"]
LEARN_WRITERS = ("learn-service", *sorted(LEGACY_PROCESS_WORKERS))


def personal_identity(row):
    fields = ("container_id", "image_id", "pid", "started_at", "restart_count")
    require(isinstance(row, dict) and row.get("service") == PERSONAL_WORKER and row.get("running") is True
            and type(row.get("pid")) is int and row["pid"] > 0
            and type(row.get("restart_count")) is int and row["restart_count"] >= 0
            and isinstance(row.get("container_id"), str) and re.fullmatch(r"[0-9a-f]{64}", row["container_id"])
            and isinstance(row.get("image_id"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"])
            and isinstance(row.get("started_at"), str)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", row["started_at"])
            and isinstance(row.get("mounts"), list), "LEARN_DEPENDENCY_ORIGINAL_IDENTITY_INVALID")
    return {**{key: row[key] for key in fields}, "mounts_sha256": sha(canonical(row["mounts"]))}


def learn_fence_rows(snapshot, baseline, frozen):
    require(isinstance(frozen, list), "LEARN_DEPENDENCY_FROZEN_ROWS_REQUIRED")
    result = []
    for name in LEARN_WRITERS:
        current = [row for row in snapshot["containers"] if row["service"] == name]
        original = [row for row in frozen if row["service"] == name]
        require(len(current) == len(original) == 1, "LEARN_DEPENDENCY_FENCE_INCOMPLETE")
        first, prior = current[0], original[0]
        require(all(row.get("running") is False and type(row.get("pid")) is int and row["pid"] == 0 for row in (first, prior))
                and first["container_id"] == prior["container_id"]
                and first["image_id"] == prior["image_id"] == baseline["images"]["learn"],
                "LEARN_DEPENDENCY_FENCE_CHANGED")
        result.append({key: first[key] for key in ("service", "container_id", "image_id", "running", "pid")})
    require(not any(row["running"] and row["service"] in MIGRATION_IMAGES for row in snapshot["containers"]),
            "LEARN_DEPENDENCY_RUNNING_MIGRATION")
    require(not any(row["image_id"] == baseline["images"]["learn"] and row["service"] not in (*LEARN_WRITERS, *MIGRATION_IMAGES)
                    for row in snapshot["containers"]), "LEARN_DEPENDENCY_UNKNOWN_CONSUMER")
    return sorted(result, key=lambda row: row["service"])


def original_personal_worker(snapshot, baseline, frozen):
    current = [row for row in snapshot["containers"] if row["service"] == PERSONAL_WORKER]
    original = [row for row in frozen if row["service"] == PERSONAL_WORKER]
    require(len(current) == len(original) == 1, "LEARN_DEPENDENCY_WORKER_AMBIGUOUS")
    row = current[0]
    identity = personal_identity(row)
    require(identity == personal_identity(original[0]) and row["image_id"] == baseline["images"]["auth"]
            and row.get("command") == PERSONAL_WORKER_COMMAND
            and snapshot["source_labels"].get(row["container_id"]) == source_labels(baseline.get("image_bindings", {}).get("auth", baseline["binding"])),
            "LEARN_DEPENDENCY_WORKER_CHANGED")
    return row, identity


def learn_dependency_guard(snapshot, baseline, frozen, probe):
    fence = learn_fence_rows(snapshot, baseline, frozen)
    row, identity = original_personal_worker(snapshot, baseline, frozen)
    status = probe(row["container_id"])
    require(isinstance(status, dict) and set(status) == {"health_status", "ready_status", "baseline_complete"}
            and type(status["health_status"]) is int and status["health_status"] == 200
            and type(status["ready_status"]) is int and status["ready_status"] in (200, 503)
            and status["baseline_complete"] is True, "LEARN_DEPENDENCY_PROBE_UNVERIFIED")
    require(row["health"] == "healthy" or row["health"] == "unhealthy" and status["ready_status"] == 503,
            "LEARN_DEPENDENCY_HEALTH_STATE_INVALID")
    return {"service": PERSONAL_WORKER, "dependency": "learn", "scope": "learn-only-stopped-writers",
            "fence": fence, "original_identity": identity, "command": list(PERSONAL_WORKER_COMMAND),
            "source_labels": snapshot["source_labels"][row["container_id"]],
            "before": {"docker_health": row["health"], **status, "readiness_deferred": status["ready_status"] == 503}}


def check_backend_baseline(snapshot, baseline, *, learn_only=False, frozen=None, probe=None, auth_learn_frontend=False, learn_frontend=False):
    require((frozen is None) == (probe is None) and (frozen is None or learn_only is True or learn_frontend is True), "LEARN_DEPENDENCY_CONTEXT_INVALID")
    dependency = learn_dependency_guard(snapshot, baseline, frozen, probe) if frozen is not None else None
    require(snapshot["protected_files"] == baseline["protected_files"],"FRONTEND_ONLY_PROTECTED_BASELINE_CHANGED")
    current = {row["path"]:row for row in snapshot["code_files"]}
    changed_prefix = ("services/learn/", "frontend/") if learn_frontend else ("services/auth/", "services/learn/", "frontend/") if auth_learn_frontend else "services/learn/" if learn_only else "frontend/"
    require(all(current.get(row["path"]) == row for row in baseline["managed_files"]
                if not row["path"].startswith(changed_prefix)),"FRONTEND_ONLY_BACKEND_FILES_CHANGED")
    for service,family in SERVICE_IMAGES.items():
        if family in {"learn", "frontend"} if learn_frontend else family in {"auth", "learn", "frontend"} if auth_learn_frontend else family == ("learn" if learn_only else "frontend"):
            continue
        matches = [row for row in snapshot["containers"] if row["service"] == service and row["running"]]
        require(len(matches) == 1 and matches[0]["image_id"] == baseline["images"][family]
                and (dependency is not None and service == PERSONAL_WORKER or
                     (matches[0].get("health") == "healthy" if service not in LEGACY_PROCESS_WORKERS else
                     matches[0].get("health") is None and type(matches[0].get("pid")) is int and matches[0]["pid"] > 0
                     and matches[0].get("command") == ["python","-m",LEGACY_PROCESS_WORKERS[service]]))
                and snapshot["source_labels"].get(matches[0]["container_id"]) == source_labels(baseline.get("image_bindings", {}).get(family, baseline["binding"])),
                "FRONTEND_ONLY_BACKEND_RUNTIME_CHANGED")
    return dependency


def source_labels(binding):
    return {"org.opencontainers.image.revision":binding["commit"], "io.ctf-web.source-fingerprint":binding["source_fingerprint"],
            "io.ctf-web.release-nonce":binding["release_nonce"]}


def frontend_rollback_binding(snapshot):
    current = [row for row in snapshot["containers"] if row["service"] == "frontend" and row["running"]]
    require(len(current) == 1, "ROLLBACK_FRONTEND_BASELINE_AMBIGUOUS")
    labels = snapshot["source_labels"].get(current[0]["container_id"], {})
    keys = {"commit": "org.opencontainers.image.revision", "source_fingerprint": "io.ctf-web.source-fingerprint",
            "release_nonce": "io.ctf-web.release-nonce"}
    patterns = {"commit": r"[0-9a-f]{40}", "source_fingerprint": r"[0-9a-f]{64}",
                "release_nonce": r"[a-z0-9][a-z0-9-]{7,63}"}
    require(isinstance(labels, dict) and set(labels) == set(keys.values()), "ROLLBACK_FRONTEND_SOURCE_LABELS_UNVERIFIED")
    binding = {name: labels[key] for name, key in keys.items()}
    require(all(isinstance(value, str) and re.fullmatch(patterns[name], value) for name, value in binding.items()),
            "ROLLBACK_FRONTEND_SOURCE_LABELS_UNVERIFIED")
    return binding


def clone_runtime_service(api):
    require(isinstance(api,dict) and isinstance(api.get("environment"),dict) and api.get("networks"), "API_RUNTIME_CONTRACT_UNKNOWN")
    allowed = ("environment","secrets","volumes","read_only","tmpfs","user","group_add","cap_drop","cap_add",
               "security_opt","ulimits","logging","init","stop_grace_period","restart")
    result = {key:copy.deepcopy(api[key]) for key in allowed if key in api}
    require(not api.get("privileged",False) and not api.get("network_mode"), "API_WORKER_SECURITY_UNSUPPORTED")
    networks = api["networks"]
    result["networks"] = {name:{} for name in networks} if isinstance(networks,dict) else list(networks)
    if isinstance(api.get("deploy"),dict) and "resources" in api["deploy"]:
        result["deploy"] = {"resources":copy.deepcopy(api["deploy"]["resources"])}
    return result


def worker_templates(source=None):
    import yaml
    class ComposeLoader(yaml.SafeLoader):
        pass
    def reset(loader,node):
        return loader.construct_sequence(node) if isinstance(node,yaml.SequenceNode) else loader.construct_scalar(node)
    ComposeLoader.add_constructor("!reset",reset)
    root = Path(source or ops.SOURCE)
    base = yaml.load((root/"infra/docker-compose.yml").read_text(encoding="utf-8-sig"),Loader=ComposeLoader)
    production = yaml.load((root/"infra/docker-compose.prod.yml").read_text(encoding="utf-8-sig"),Loader=ComposeLoader)
    allowed = {"environment","networks","secrets","stop_grace_period","healthcheck","command","restart",
               "security_opt","cap_drop","cap_add","read_only","tmpfs","logging","deploy","user","group_add","init"}
    result = {}
    for name in NEW_WORKERS:
        first,second = base["services"][name],production["services"][name]
        definition = {key:copy.deepcopy(value) for key,value in first.items() if key in allowed}
        definition.update({key:copy.deepcopy(value) for key,value in second.items() if key in allowed and key != "environment"})
        definition["environment"] = {**first.get("environment",{}),**second.get("environment",{})}
        require(not set(definition) & {"volumes","ports","build","depends_on"},"WORKER_TEMPLATE_UNSAFE")
        result[name] = definition
    return result


def minimal_worker(api, template):
    require(isinstance(api.get("environment"),dict) and isinstance(template,dict),"WORKER_RUNTIME_CONTRACT_UNKNOWN")
    result = copy.deepcopy(template)
    environment = {}
    for key,value in template["environment"].items():
        if key in api["environment"]:
            environment[key] = api["environment"][key]
        elif isinstance(value,str) and "${" not in value:
            environment[key] = value
        else:
            match = re.fullmatch(r"\$\{"+re.escape(key)+r":-([0-9]+)\}",str(value))
            require(match is not None and key.startswith(("PERSONAL_NOTIFICATION_","MARKETPLACE_SETTLEMENT_","COMMENT_NOTIFICATION_")),"WORKER_ENV_VALUE_MISSING")
            environment[key] = match.group(1)
    require(environment.get("ENVIRONMENT") == "production","WORKER_PRODUCTION_ENV_REQUIRED")
    result["environment"] = environment
    secrets = {item if isinstance(item,str) else item["source"]:item for item in api.get("secrets",[])}
    requested = [item if isinstance(item,str) else item["source"] for item in template.get("secrets",[])]
    require(all(name in secrets for name in requested),"WORKER_REQUIRED_SECRET_MISSING")
    result["secrets"] = [copy.deepcopy(secrets[name]) for name in requested]
    networks = template["networks"]
    require(all(name in api.get("networks",{}) for name in networks),"WORKER_INTERNAL_NETWORK_MISSING")
    result["networks"] = {name:{} for name in networks}
    require(not set(result) & {"volumes","ports","container_name","depends_on","privileged"},"WORKER_RUNTIME_EXCESS_PRIVILEGE")
    require(not result.get("cap_add") or set(result["cap_add"]) == {"CHOWN","SETUID","SETGID"}
            and "auth_jwt_private_key" in requested,"WORKER_RUNTIME_EXCESS_CAPABILITY")
    return result


def compose_literal(value):
    if isinstance(value,str):
        return value.replace("$","$$")
    if isinstance(value,list):
        return [compose_literal(item) for item in value]
    if isinstance(value,dict):
        return {key:compose_literal(item) for key,item in value.items()}
    return value


def make_overlay(model, images, binding, workers, runtime_dir, *, frontend_only=False, learn_only=False, templates=None, learn_code_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, migration_services=(), learn_frontend=False):
    require(not learn_code_only or learn_only and not frontend_only, "LEARN_CODE_SCOPE_INVALID")
    services = model.get("services",{})
    require(all(name in services for name in (*API_ORDER,"frontend")), "LIVE_SERVICE_MODEL_INCOMPLETE")
    definitions = {}
    if auth_learn_frontend or auth_learn_community_frontend or learn_frontend:
        activation_steps(workers, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, migration_services=migration_services, learn_frontend=learn_frontend)
    selected = ("learn-service", *workers, "frontend") if learn_frontend else ("auth-service", "learn-service", *workers, "frontend") if auth_learn_frontend else ("frontend",) if frontend_only else ("learn-service", *workers) if learn_only else (*API_ORDER,*workers,"frontend")
    for name in selected:
        require(name in SERVICE_IMAGES, "OVERLAY_SERVICE_FORBIDDEN")
        if name in services:
            definition = {}
        else:
            require(name in NEW_WORKERS, "RUNTIME_WORKER_MODEL_UNKNOWN")
            api, module, port, key = NEW_WORKERS[name]
            require(templates is not None and name in templates,"WORKER_TEMPLATE_REQUIRED")
            definition = minimal_worker(services[api],templates[name])
            require(definition["command"] == ["python","-m",module] and definition["environment"][key] == str(port),"WORKER_TEMPLATE_COMMAND_CHANGED")
        definition.update({"image":images[SERVICE_IMAGES[name]],"labels":source_labels(binding)})
        definitions[name] = definition
    if not frontend_only and not learn_code_only:
        for job,family in MIGRATION_IMAGES.items():
            if (auth_learn_frontend or auth_learn_community_frontend or learn_frontend) and family not in migration_services:
                continue
            if learn_only and family != "learn":
                continue
            api = services[family + "-service"]
            definition = {} if job in services else clone_runtime_service(api)
            prior_environment = (services.get(job) or api).get("environment",{})
            environment = {} if job in services else copy.deepcopy(prior_environment)
            environment.update({"PGOPTIONS":PGOPTIONS,"PYTHONPATH":"/renewal-runtime" + (":" + prior_environment["PYTHONPATH"] if prior_environment.get("PYTHONPATH") else "")})
            definition.update({"image":images[family],"labels":source_labels(binding),"environment":environment,
                               "volumes":[*copy.deepcopy((services.get(job) or api).get("volumes",[])),
                                          {"type":"bind","source":str(runtime_dir),"target":"/renewal-runtime","read_only":True}],
                               "restart":"no"})
            definitions[job] = definition
    return compose_literal({"services":definitions})


def copy_managed(staged, live, rows, modes, before, nonce):
    expected = {row["path"]:row for row in before}
    prepared = []
    for row in rows:
        name = row["path"]
        require(name.startswith(("frontend/","services/auth/","services/learn/","services/community/")), "MANAGED_CONTEXT_INVALID")
        origin, destination = staged/name, live/name
        for path,root in ((origin,staged),(destination,live)):
            require(path.resolve().is_relative_to(root.resolve()) and not any(parent.is_symlink() for parent in (path,*path.parents)
                    if parent.is_relative_to(root)), "MANAGED_PATH_LINK_OR_ESCAPE")
        payload = origin.read_bytes()
        require(len(payload) == row["bytes"] and sha(payload) == row["sha256"], "STAGED_SOURCE_CHANGED")
        old = expected.get(name)
        require(destination.is_file() if old else not destination.exists(), "LIVE_MANAGED_FILE_CHANGED")
        if old:
            current = destination.read_bytes()
            require(len(current) == old["bytes"] and sha(current) == old["sha256"], "LIVE_MANAGED_FILE_CHANGED")
        require(modes[name] in {0o644,0o755}, "MANAGED_FILE_MODE_INVALID")
        prepared.append((destination,payload,modes[name]))
    import os
    for destination,payload,mode in prepared:
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary = destination.with_name(".renewal-" + nonce + "-" + sha(str(destination).encode())[:12] + ".tmp")
        descriptor = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,"wb") as output:
            output.write(payload); output.flush(); os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary,destination)
        require(sha(destination.read_bytes()) == sha(payload), "LIVE_MANAGED_COPY_UNVERIFIED")


REMOTE = r'''
import copy,time
from pathlib import Path
def private_json(path,value):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as output: output.write(canonical(value)+b'\n')
def private_command(command,log,timeout=600):
    descriptor=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as output:
        result=subprocess.run(command,stdout=output,stderr=subprocess.STDOUT,timeout=timeout)
    require(result.returncode==0,'ACTIVATION_COMMAND_FAILED')
def compose_command(overlay):
    return ['docker','compose','-p',PROJECT,'--project-directory',str(ROOT/'infra'),'--env-file',str(ROOT/'infra/.env'),'-f',str(ROOT/'infra/docker-compose.yml'),'-f',str(ROOT/'infra/docker-compose.prod.yml'),'-f',str(overlay)]
def sql(database,query):
    postgres=singleton(containers(),'postgres')['container_id']
    shell='export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=1000"; exec psql -U "$POSTGRES_USER" -d "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"'
    return json.loads(run(['docker','exec',postgres,'sh','-c',shell,'renewal-activation-read',database,query],limit=30))
def probe_personal_worker(container_id):
    require(isinstance(container_id,str) and re.fullmatch('[0-9a-f]{64}',container_id),'LEARN_DEPENDENCY_PROBE_TARGET_INVALID')
    code="""import json,urllib.error,urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect)
out={}
for path in ('health','ready'):
 try: response=opener.open('http://127.0.0.1:8040/'+path,timeout=2)
 except urllib.error.HTTPError as error: response=error
 with response:
  body=response.read(4097)
  if len(body)>4096: raise SystemExit(2)
  out[path]={'http_status':response.code,'body':json.loads(body.decode('utf-8'))}
print(json.dumps(out))"""
    value=json.loads(run(['docker','exec',container_id,'python','-B','-c',code],limit=8))
    require(isinstance(value,dict) and set(value)=={'health','ready'}
            and all(isinstance(row,dict) and set(row)=={'http_status','body'} for row in value.values()),'LEARN_DEPENDENCY_RESPONSE_INVALID')
    health,ready=value['health'],value['ready']
    require(type(health['http_status']) is int and health['http_status']==200 and health['body']=={'status':'ok'},'LEARN_DEPENDENCY_LIVENESS_INVALID')
    require(type(ready['http_status']) is int and ready['http_status'] in (200,503)
            and ready['body']=={'status':'ready' if ready['http_status']==200 else 'not_ready','baseline_complete':True}
            and ready['body'].get('baseline_complete') is True,'LEARN_DEPENDENCY_READY_BODY_INVALID')
    return {'health_status':200,'ready_status':ready['http_status'],'baseline_complete':True}
def schema_contract():
    heads={name:sql(database,"SELECT coalesce(json_agg(version_num ORDER BY version_num),'[]'::json) FROM public.alembic_version") for name,database in DATABASES.items()}
    expected_heads=REQUEST.get('expected_heads',EXPECTED_HEADS)
    require(heads=={name:[head] for name,head in expected_heads.items()},'MIGRATION_HEAD_UNVERIFIED')
    if REQUEST.get('auth_learn_frontend'):
        postgres=singleton(containers(),'postgres')['container_id']
        require(database_rows(postgres)['community']['schema_sha256']==REQUEST['alf_contract']['community_schema_sha256'],'ALF_COMMUNITY_SCHEMA_CHANGED')
    alcf=REQUEST.get('auth_learn_community_frontend') is True
    if alcf and 'community' not in REQUEST['alcf_contract']['migration_services']:
        postgres=singleton(containers(),'postgres')['container_id']
        require(database_rows(postgres)['community']['schema_sha256']==REQUEST['alcf_contract']['community_schema_sha256'],'ALCF_COMMUNITY_SCHEMA_CHANGED')
    for name,database in DATABASES.items():
        found=sql(database,"SELECT coalesce(json_agg(table_name),'[]'::json) FROM information_schema.tables WHERE table_schema='public'")
        require(set(SCHEMA_TABLES[name])<=set(found),'REQUIRED_SCHEMA_TABLE_MISSING')
        columns=sql(database,"SELECT coalesce(json_agg(table_name||'.'||column_name),'[]'::json) FROM information_schema.columns WHERE table_schema='public'")
        require(set(SCHEMA_COLUMNS[name])<=set(columns),'REQUIRED_SCHEMA_COLUMN_MISSING')
        alf=REQUEST.get('auth_learn_frontend') is True
        graph=REQUEST['alf_contract']['migration_graph'] if alf else REQUEST['alcf_contract']['migration_graph'] if alcf else {}
        if name=='learn' and (expected_heads['learn']==LEARN_CATEGORY_HEAD or (alf or alcf) and '20261013_029' in graph['learn']['revisions']):
            require({'platform_event_categories','platform_event_category_audit_events'}<=set(found),'EVENT_CATEGORY_SCHEMA_MISSING')
        if name=='learn' and (alf or alcf) and '20261018_030' in graph['learn']['revisions']:
            reason=sql(database,"SELECT coalesce(json_agg(json_build_object('type',data_type,'nullable',is_nullable)),'[]'::json) FROM information_schema.columns WHERE table_schema='public' AND table_name='submission_flag_reveal_audits' AND column_name='reason'")
            require(reason==[{'type':'text','nullable':'YES'}],'REVEAL_REASON_SCHEMA_MISSING')
    if alcf:
        require(sql('platform_db',ALCF_TEAM_SCHEMA_SQL) is True,'TEAM_SCHEMA_CONTRACT_UNVERIFIED')
        require(sql('learn_db',ALCF_ATTACHMENT_SCHEMA_SQL) is True,'ATTACHMENT_SCHEMA_CONTRACT_UNVERIFIED')
    if REQUEST.get('learn_frontend') is True:
        schemas=database_rows(singleton(containers(),'postgres')['container_id'])
        require(all(schemas[name]['schema_sha256']==REQUEST['lf_contract'][name+'_schema_sha256'] for name in ('auth','community')),'LF_RETAINED_SCHEMA_CHANGED')
        tag_schema=sql('learn_db',LF_TAG_SCHEMA_SQL)
        require(tag_schema==LF_TAG_SCHEMA_EXPECTED and all(value is True for value in tag_schema.values()),'LF_TAG_SCHEMA_UNVERIFIED')
    triggers=sql('learn_db',"SELECT coalesce(json_agg(json_build_object('table',c.relname,'enabled',t.tgenabled,'function',p.proname,'type',t.tgtype)),'[]'::json) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid WHERE NOT t.tgisinternal AND n.nspname='public' AND t.tgname='knowledge_graph_revision_write'")
    require(len(triggers)==18 and {row['table'] for row in triggers}==set(TRIGGER_TABLES) and all(row['enabled'] in ['O','A'] and row['function']=='bump_knowledge_graph_revision' and row['type']==28 for row in triggers),'LEARN_TRIGGER_CONTRACT_UNVERIFIED')
    return {'heads':heads,'trigger_count':len(triggers),'required_schema_verified':True}
def category_compatibility():
    row=sql('learn_db',"SELECT json_build_object('custom_category_count',(SELECT count(*) FROM platform_event_categories WHERE key NOT IN ('workshop','seminar','study','networking','notice','other')),'custom_event_count',(SELECT count(*) FROM platform_events WHERE category NOT IN ('workshop','seminar','study','networking','notice','other')))")
    require(isinstance(row,dict) and set(row)=={'custom_category_count','custom_event_count'} and all(type(value) is int and value>=0 for value in row.values()),'CATEGORY_ROLLBACK_GUARD_UNVERIFIED')
    return {**row,'legacy_enum_image_rollback_forbidden':any(row.values()),'automatic_backend_rollback_performed':False}
def category_before_migration():
    head=sql('learn_db',"SELECT coalesce(json_agg(version_num ORDER BY version_num),'[]'::json) FROM public.alembic_version")
    require(head==['20261013_028'],'LEARN_COMPATIBILITY_PREVIOUS_HEAD_CHANGED')
    row=sql('learn_db',"SELECT json_build_object('registry_absent',to_regclass('public.platform_event_categories') IS NULL,'legacy_constraint_valid',EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='public.platform_events'::regclass AND conname='ck_platform_events_category' AND contype='c' AND convalidated),'custom_event_count',(SELECT count(*) FROM platform_events WHERE category NOT IN ('workshop','seminar','study','networking','notice','other')))")
    require(isinstance(row,dict) and set(row)=={'registry_absent','legacy_constraint_valid','custom_event_count'} and row['registry_absent'] is True
            and row['legacy_constraint_valid'] is True and type(row['custom_event_count']) is int and row['custom_event_count']==0,'LEARN_COMPATIBILITY_PRE_MIGRATION_STATE_UNVERIFIED')
    return {'previous_head':head[0],**row}
def learn_code_snapshot():
    database=database_rows(singleton(containers(),'postgres')['container_id'])['learn']
    state=sql('learn_db',LEARN_CODE_STATE_SQL)
    require(state['head']==database['head'],'LEARN_CODE_SNAPSHOT_HEAD_CHANGED')
    state['schema_sha256']=database['schema_sha256']
    return validate_code_state(state)
def notification_state():
    return sql('platform_db',"SELECT json_build_object('ready',(SELECT baseline_completed_at IS NOT NULL FROM notification_policies WHERE id='personal-v1'),'notifications',(SELECT count(*) FROM personal_notifications),'events',(SELECT count(*) FROM personal_notification_events))")
def wait_service(service,image,binding):
    deadline=time.monotonic()+180; prior=None
    while time.monotonic()<deadline:
        matches=[row for row in containers() if row['service']==service and row['running']]
        if len(matches)==1 and matches[0]['image_id']==image:
            item=json.loads(run(['docker','inspect',matches[0]['container_id']]))[0]
            labels=item.get('Config',{}).get('Labels',{}) or {}
            require(all(labels.get(key)==value for key,value in source_labels(binding).items()),'CONTAINER_SOURCE_LABEL_UNVERIFIED')
            if service in LEGACY_PROCESS_WORKERS:
                row=matches[0]
                require(item.get('Config',{}).get('Cmd')==['python','-m',LEGACY_PROCESS_WORKERS[service]],'LEGACY_WORKER_COMMAND_CHANGED')
                require(row.get('health') is None,'LEGACY_WORKER_READINESS_CONTRACT_CHANGED')
                signature=(row['container_id'],row['image_id'],row['pid'],row['started_at'],row['restart_count'])
                if type(row['pid']) is int and row['pid']>0 and row['started_at'] and type(row['restart_count']) is int:
                    if prior==signature: return {**row,'readiness_available':False,'process_stable':True}
                    prior=signature
            elif matches[0].get('health')=='healthy': return matches[0]
        time.sleep(2)
    raise ActivateError('TARGET_HEALTH_UNVERIFIED')
def safe_snapshot():
    model=compose(); protected=protected_rows(model)
    current=containers(); labels={}
    for row in current:
        if row['service'] not in SERVICE_IMAGES and REQUEST.get('release_phase') not in ('learn-code-only-029','auth-learn-community-frontend-v1','learn-frontend-032-v1'): continue
        config=json.loads(run(['docker','inspect',row['container_id']]))[0].get('Config',{})
        raw=config.get('Labels',{}) or {}
        labels[row['container_id']]={key:raw.get(key) for key in source_labels(REQUEST['binding'])}
        if row['service'] in LEGACY_PROCESS_WORKERS or row['service']==PERSONAL_WORKER: row['command']=config.get('Cmd')
    return {'containers':current,'protected_files':protected,'code_files':code_rows(protected),'source_labels':labels}
def wait_personal_dependency_recovery(baseline,frozen):
    started=time.monotonic(); observations=0
    while time.monotonic()-started<=180:
        snapshot=safe_snapshot()
        row,identity=original_personal_worker(snapshot,baseline,frozen)
        require(row.get('health') in ('healthy','unhealthy'),'LEARN_DEPENDENCY_RECOVERY_HEALTH_INVALID')
        status=probe_personal_worker(row['container_id']);observations+=1
        elapsed=time.monotonic()-started
        if row['health']=='healthy' and status['ready_status']==200:
            require(elapsed<=180,'LEARN_DEPENDENCY_RECOVERY_TIMEOUT')
            return snapshot,{'identity':identity,'command':list(PERSONAL_WORKER_COMMAND),
                'source_labels':snapshot['source_labels'][row['container_id']],
                'docker_health':'healthy',**status,'observations':observations,
                'elapsed_seconds':round(elapsed,3),'maximum_wait_seconds':180}
        require(elapsed<180,'LEARN_DEPENDENCY_RECOVERY_TIMEOUT')
        time.sleep(min(2,180-elapsed))
    raise ActivateError('LEARN_DEPENDENCY_RECOVERY_TIMEOUT')
def check_images():
    for family,image in REQUEST['images'].items():
        selected=json.loads(run(['docker','image','inspect',image]))
        require(len(selected)==1 and selected[0]['Id']==image,'STAGED_IMAGE_ID_CHANGED')
        labels=selected[0].get('Config',{}).get('Labels',{}) or {}
        require(all(labels.get(key)==value for key,value in source_labels(REQUEST['image_bindings'][family]).items()),'STAGED_IMAGE_SOURCE_LABEL_CHANGED')
        if family=='frontend': require(labels.get('io.ctf-web.frontend-build-args-sha256')==REQUEST['frontend_args_sha256'],'STAGED_FRONTEND_ARGS_LABEL_CHANGED')
def identity(rows):
    return sorted([{key:row[key] for key in ['service','container_id','image_id','running']} for row in rows],key=lambda row:(row['service'],row['container_id']))
def assert_preserved(before,after,managed,targets):
    require(before['protected_files']==after['protected_files'],'PROTECTED_FILES_CHANGED')
    paths={row['path'] for row in managed}
    require([row for row in before['code_files'] if row['path'] not in paths]==[row for row in after['code_files'] if row['path'] not in paths],'UNKNOWN_EXTRA_SOURCE_CHANGED')
    require(identity([row for row in before['containers'] if row['service'] not in targets])==identity([row for row in after['containers'] if row['service'] not in targets]),'UNTARGETED_CONTAINER_CHANGED')
    for name in LEGACY_PROCESS_WORKERS:
        if name in targets: continue
        fields=('container_id','pid','started_at','restart_count','command','health')
        require([[row.get(key) for key in fields] for row in before['containers'] if row['service']==name]
                ==[[row.get(key) for key in fields] for row in after['containers'] if row['service']==name],'LEGACY_PROCESS_STABILITY_CHANGED')
def activation_main():
    directory=None; started=[]; frontend_attempted=False; backend_migrated=False; backend_attempted=False; prior_frontend_binding=None
    code_only=REQUEST.get('release_phase')=='learn-code-only-029'
    alf=REQUEST.get('auth_learn_frontend') is True
    alcf=REQUEST.get('auth_learn_community_frontend') is True
    lf=REQUEST.get('learn_frontend') is True
    receipt={'schema_version':1,'kind':'renewal-activate','binding':REQUEST['binding'],'frontend_only':REQUEST['frontend_only'],'status':'partial-unverified',
             'plan_sha256':REQUEST['plan_sha256'],'backup_receipt_sha256':REQUEST['backup_receipt_sha256'],'quiescence_sha256':REQUEST['quiescence_sha256'],
             'database_restore_performed':False,'old_migration_jobs_executed':False,'protected_files_replaced':False,'images_pruned':False}
    if 'baseline_receipts' in REQUEST: receipt.update({'learn_only':REQUEST.get('learn_only',False),'baseline_receipts':REQUEST['baseline_receipts']})
    if REQUEST.get('compatibility_image_backup_receipt_sha256'): receipt['compatibility_image_backup_receipt_sha256']=REQUEST['compatibility_image_backup_receipt_sha256']
    if code_only:
        receipt.update({key:REQUEST[key] for key in ('release_phase','learn_code_contract')})
        receipt['migration_executed']=False
    if alf:
        validate_alf_contract(REQUEST['alf_contract'])
        require(REQUEST.get('release_phase')=='auth-learn-frontend-v1','ALF_PHASE_INVALID')
        receipt.update({key:REQUEST[key] for key in ('auth_learn_frontend','release_phase','alf_contract')})
        receipt['executed_migrations']=[]
    if alcf:
        validate_alcf_contract(REQUEST['alcf_contract'])
        require(REQUEST.get('release_phase')=='auth-learn-community-frontend-v1','ALCF_PHASE_INVALID')
        receipt.update({key:REQUEST[key] for key in ('auth_learn_community_frontend','release_phase','alcf_contract')})
        receipt['executed_migrations']=[]
    if lf:
        validate_lf_contract(REQUEST['lf_contract'])
        require(REQUEST.get('release_phase')=='learn-frontend-032-v1','LF_PHASE_INVALID')
        receipt.update({key:REQUEST[key] for key in ('learn_frontend','release_phase','lf_contract')})
        receipt['executed_migrations']=[]
    try:
        before=safe_snapshot(); model=compose()
        check_images()
        require(before['protected_files']==REQUEST['protected_files'],'PROTECTED_BASELINE_CHANGED')
        require(identity(before['containers'])==identity(REQUEST['current_container_baseline']),'CURRENT_CONTAINER_BASELINE_CHANGED')
        singleton(before['containers'],'minio')
        frontend_only=REQUEST['frontend_only']; learn_only=REQUEST.get('learn_only',False)
        require(REQUEST.get('release_phase') in (None,'learn-code-only-029','auth-learn-frontend-v1','auth-learn-community-frontend-v1','learn-frontend-032-v1') and (not code_only or learn_only and not frontend_only)
                and sum((frontend_only,learn_only,alf,alcf,lf))<=1,'RELEASE_PHASE_INVALID')
        workers=[] if frontend_only else derive_workers(before['containers'],model['services'],learn_frontend=True) if lf else derive_workers(before['containers'],model['services'],auth_learn_frontend=True) if alf else derive_workers(before['containers'],model['services'])
        if learn_only: workers=[name for name in workers if SERVICE_IMAGES[name]=='learn']
        targets=['learn-service',*workers,'frontend'] if lf else ['auth-service','learn-service',*workers,'frontend'] if alf else ['frontend'] if frontend_only else ['learn-service',*workers] if learn_only else [*API_ORDER,*workers,'frontend']
        if lf:
            contract=REQUEST['lf_contract']
            require(all(row['path'].startswith(('services/learn/','frontend/')) for row in REQUEST['managed_files']),'LF_MANAGED_SCOPE_CHANGED')
            require(REQUEST.get('expected_heads')==contract['target_heads'] and REQUEST['backend_baseline']['expected_heads']==contract['previous_heads']
                and REQUEST['backend_baseline']['images']==contract['previous_images'],'LF_BASELINE_BINDING_CHANGED')
            require(code_runtime(before,[*targets,'minio'],include_labels=False)
                ==code_runtime({'containers':REQUEST['current_container_baseline']},[*targets,'minio'],include_labels=False),'LF_UNTARGETED_PROCESS_CHANGED')
            check_backend_baseline(before,REQUEST['backend_baseline'],learn_frontend=True,
                frozen=REQUEST['current_container_baseline'],probe=probe_personal_worker)
            prior_heads={name:sql(database,"SELECT coalesce(json_agg(version_num ORDER BY version_num),'[]'::json) FROM public.alembic_version") for name,database in DATABASES.items()}
            require(prior_heads=={name:[head] for name,head in contract['previous_heads'].items()},'LF_LIVE_HEADS_CHANGED')
            schemas=database_rows(singleton(before['containers'],'postgres')['container_id'])
            require(all(schemas[name]['schema_sha256']==contract[name+'_schema_sha256'] for name in ('auth','community')),'LF_RETAINED_SCHEMA_CHANGED')
            selected_before=sorted((row for row in before['code_files'] if row['path'].startswith(('services/auth/','services/learn/','services/community/','frontend/'))),key=lambda row:row['path'])
            require(selected_before==sorted(contract['previous_source_files']+contract['unmanaged_in_target_families'],key=lambda row:row['path']),'LF_LIVE_BASELINE_SOURCE_CHANGED')
            receipt['untargeted_runtime_before']=code_runtime(before,targets)
        if alf:
            require(all(row['path'].startswith(('services/auth/','services/learn/','frontend/')) for row in REQUEST['managed_files']),'ALF_MANAGED_SCOPE_CHANGED')
            require(REQUEST.get('expected_heads')==REQUEST['alf_contract']['target_heads'] and REQUEST['backend_baseline']['expected_heads']==REQUEST['alf_contract']['previous_heads'],'ALF_HEAD_BINDING_CHANGED')
            require(code_runtime(before,[*targets,'minio'],include_labels=False)
                ==code_runtime({'containers':REQUEST['current_container_baseline']},[*targets,'minio'],include_labels=False),'ALF_UNTARGETED_PROCESS_CHANGED')
            check_backend_baseline(before,REQUEST['backend_baseline'],auth_learn_frontend=True)
            prior_heads={name:sql(database,"SELECT coalesce(json_agg(version_num ORDER BY version_num),'[]'::json) FROM public.alembic_version") for name,database in DATABASES.items()}
            require(prior_heads=={name:[head] for name,head in REQUEST['alf_contract']['previous_heads'].items()},'ALF_LIVE_HEADS_CHANGED')
            postgres=singleton(before['containers'],'postgres')['container_id']
            require(database_rows(postgres)['community']['schema_sha256']==REQUEST['alf_contract']['community_schema_sha256'],'ALF_COMMUNITY_SCHEMA_CHANGED')
            receipt['untargeted_runtime_before']=code_runtime(before,targets)
        if alcf:
            contract=REQUEST['alcf_contract']
            require(REQUEST.get('expected_heads')==contract['target_heads']
                    and REQUEST['backend_baseline']['expected_heads']==contract['previous_heads']
                    and REQUEST['backend_baseline']['images']==contract['previous_images']
                    and sorted((row for row in REQUEST['backend_baseline']['unmanaged_files'] if row['path'].startswith(('services/auth/','services/learn/','services/community/','frontend/'))),key=lambda row:row['path'])
                    ==contract['unmanaged_in_target_families'],'ALCF_BASELINE_BINDING_CHANGED')
            require(code_runtime(before,[*targets,'minio'],include_labels=False)
                ==code_runtime({'containers':REQUEST['current_container_baseline']},[*targets,'minio'],include_labels=False),'ALCF_UNTARGETED_PROCESS_CHANGED')
            prior_heads={name:sql(database,"SELECT coalesce(json_agg(version_num ORDER BY version_num),'[]'::json) FROM public.alembic_version") for name,database in DATABASES.items()}
            require(prior_heads=={name:[head] for name,head in contract['previous_heads'].items()},'ALCF_LIVE_HEADS_CHANGED')
            require(database_rows(singleton(before['containers'],'postgres')['container_id'])['community']['schema_sha256']==contract['community_schema_sha256'],'ALCF_COMMUNITY_SCHEMA_CHANGED')
            selected_before=sorted((row for row in before['code_files'] if row['path'].startswith(('services/auth/','services/learn/','services/community/','frontend/'))),key=lambda row:row['path'])
            require(selected_before==sorted(contract['previous_source_files']+contract['unmanaged_in_target_families'],key=lambda row:row['path']),'ALCF_LIVE_BASELINE_SOURCE_CHANGED')
            receipt['untargeted_runtime_before']=code_runtime(before,targets)
        if learn_only:
            receipt['learn_dependency_readiness']=check_backend_baseline(before,REQUEST['backend_baseline'],learn_only=True,
                frozen=REQUEST['current_container_baseline'],probe=probe_personal_worker)
            if not code_only: receipt['category_before_migration']=category_before_migration()
        if not frontend_only:
            require(not any(row['running'] and row['service'] in MIGRATION_IMAGES for row in before['containers']),'RUNNING_MIGRATION_JOB_FORBIDDEN')
            for row in before['containers']:
                if row['service'] in targets and row['service']!='frontend': require(not row['running'] and row['pid']==0,'WRITER_FENCE_NOT_MAINTAINED')
            for name,database in DATABASES.items():
                if alf and name=='community': continue
                if (learn_only or lf) and name!='learn': continue
                clients=sql(database,"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND backend_type='client backend' AND pid<>pg_backend_pid()")
                require(clients==0,'OTHER_DATABASE_CLIENTS_REMAIN')
        else:
            check_backend_baseline(before,REQUEST['backend_baseline'])
            schema_contract()
            prior_frontend_binding=frontend_rollback_binding(before)
        if code_only:
            validate_code_contract(REQUEST['learn_code_contract'])
            require(code_runtime(before,[*targets,'minio'],include_labels=False)
                ==code_runtime({'containers':REQUEST['current_container_baseline']},[*targets,'minio'],include_labels=False),
                'LEARN_CODE_UNTARGETED_PROCESS_CHANGED')
            require(learn_migration_rows(before['code_files'])==REQUEST['learn_code_contract']['migration_files'],'LEARN_CODE_MIGRATION_BYTES_CHANGED')
            prior=[row for row in before['containers'] if row['service']=='learn-service']
            require(len(prior)==1 and prior[0]['image_id']==REQUEST['learn_code_contract']['previous_learn_image'],'LEARN_CODE_PREIMAGE_CHANGED')
            receipt['learn_code_before']=learn_code_snapshot()
            require(receipt['learn_code_before']['schema_sha256']==REQUEST['learn_code_contract']['schema_sha256'],'LEARN_CODE_SCHEMA_BASELINE_CHANGED')
            receipt['untargeted_runtime_before']=code_runtime(before,targets)
        release=Path(RELEASE_ROOT)/REQUEST['binding']['release_nonce']
        require(release.is_dir() and not any(parent.is_symlink() for parent in (release,*release.parents)),'STAGED_RELEASE_PATH_INVALID')
        directory=release/'activation'; directory.mkdir(mode=0o700)
        private_json(directory/'intent.json',{'binding':REQUEST['binding'],'targets':targets,'frontend_only':frontend_only,'learn_only':learn_only,
            **({'release_phase':REQUEST['release_phase'],'learn_code_contract':REQUEST['learn_code_contract']} if code_only else {}),
            **({key:REQUEST[key] for key in ('auth_learn_frontend','release_phase','alf_contract')} if alf else {}),
            **({key:REQUEST[key] for key in ('auth_learn_community_frontend','release_phase','alcf_contract')} if alcf else {}),
            **({key:REQUEST[key] for key in ('learn_frontend','release_phase','lf_contract')} if lf else {})})
        runtime=directory/'runtime'
        if not frontend_only and not code_only:
            runtime.mkdir(mode=0o755); runtime.chmod(0o755)
            wrapper=MIGRATION_SITE_CUSTOMIZE.encode()
            descriptor=os.open(runtime/'sitecustomize.py',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o444)
            with os.fdopen(descriptor,'wb') as output: output.write(wrapper)
            (runtime/'sitecustomize.py').chmod(0o444)
            require(sha((runtime/'sitecustomize.py').read_bytes())==REQUEST['migration_wrapper_sha256'],'MIGRATION_WRAPPER_HASH_MISMATCH')
        overlay=directory/'runtime-overlay.json'
        private_json(overlay,make_overlay(model,REQUEST['images'],REQUEST['binding'],workers,runtime,frontend_only=frontend_only,learn_only=learn_only,templates=REQUEST['worker_templates'],learn_code_only=code_only,
            auth_learn_frontend=alf,auth_learn_community_frontend=alcf,learn_frontend=lf,
            migration_services=REQUEST['lf_contract']['migration_services'] if lf else REQUEST['alf_contract']['migration_services'] if alf else REQUEST['alcf_contract']['migration_services'] if alcf else ()))
        base=compose_command(overlay)
        # Inspect resolved overlay privately before any source or service mutation.
        resolved=json.loads(run([*base,'config','--format','json']))
        for name in targets: require(resolved['services'][name]['image']==REQUEST['images'][SERVICE_IMAGES[name]],'RESOLVED_IMAGE_OVERRIDE_MISMATCH')
        receipt['containers_before']=identity(before['containers'])
        receipt['protected_files']=before['protected_files']
        selected={row['path'] for row in REQUEST['managed_files']}
        receipt['unmanaged_files']=[row for row in before['code_files'] if row['path'] not in selected]
        for action,subject in activation_steps(workers,frontend_only=frontend_only,learn_only=learn_only,learn_code_only=code_only,
                auth_learn_frontend=alf,auth_learn_community_frontend=alcf,learn_frontend=lf,
                migration_services=REQUEST['lf_contract']['migration_services'] if lf else REQUEST['alf_contract']['migration_services'] if alf else REQUEST['alcf_contract']['migration_services'] if alcf else ()):
            receipt['current_step']=[action,subject]
            if action=='source':
                copy_managed(release/'source',ROOT,REQUEST['managed_files'],REQUEST['modes'],before['code_files'],REQUEST['binding']['release_nonce'])
                receipt['managed_files']=REQUEST['managed_files']
            elif action=='migration':
                backend_migrated=True
                job=subject+'-migrate'
                command=['python','-m','app.scripts.'+subject+'_migrate'] if subject!='community' else ['alembic','-c','/app/alembic.ini','upgrade','head']
                private_command([*base,'run','--rm','--no-deps','--pull','never','--name','ctf-renewal-'+job+'-'+REQUEST['binding']['release_nonce'],job,*command],directory/('migration-'+subject+'.log'),timeout=600)
                if alf or alcf or lf: receipt['executed_migrations'].append(subject)
            elif action=='up':
                if subject=='frontend': frontend_attempted=True
                else: backend_attempted=True
                private_command([*base,'up','-d','--no-deps','--no-build','--pull','never','--force-recreate',subject],directory/('up-'+subject+'.log'))
                started.append(subject)
            elif action=='health':
                wait_service(subject,REQUEST['images'][SERVICE_IMAGES[subject]],REQUEST['binding'])
                if subject in LEGACY_PROCESS_WORKERS:
                    receipt.setdefault('legacy_worker_process_stability',{})[subject]={'readiness_available':False,'process_stable':True,'observations':2}
            elif action=='schema': receipt['schema']=schema_contract()
            elif action=='baseline':
                for row in containers():
                    if row['service'] in NEW_WORKERS: require(not row['running'],'NOTIFICATION_WORKER_STARTED_BEFORE_BASELINE')
                prior=notification_state()
                if not prior['ready']:
                    private_command([*base,'run','--rm','--no-deps','--pull','never','--name','ctf-renewal-badge-baseline-'+REQUEST['binding']['release_nonce'],
                                     'auth-service','python','-m','app.workers.personal_notification_runner','--baseline-only'],directory/'badge-baseline.log',timeout=1800)
                after_baseline=notification_state()
                require(after_baseline['ready'] is True and prior['notifications']==after_baseline['notifications'] and prior['events']==after_baseline['events'],'BADGE_BASELINE_OR_HISTORICAL_EVENT_UNVERIFIED')
                receipt['notification_baseline_verified']=True
            elif action=='verify':
                after=safe_snapshot(); assert_preserved(before,after,REQUEST['managed_files'],targets)
                require([row for row in after['code_files'] if row['path'] in selected]==REQUEST['managed_files'],'MANAGED_SOURCE_FINAL_HASH_MISMATCH')
                receipt['schema']=schema_contract()
                graph=REQUEST['alf_contract']['migration_graph'] if alf else REQUEST['alcf_contract']['migration_graph'] if alcf else {}
                if lf or REQUEST.get('expected_heads',EXPECTED_HEADS)['learn']==LEARN_CATEGORY_HEAD or (alf or alcf) and '20261013_029' in graph['learn']['revisions']:
                    receipt['category_compatibility']=category_compatibility()
                    if learn_only and not code_only: require(not receipt['category_compatibility']['legacy_enum_image_rollback_forbidden'],'LEARN_COMPAT_PHASE_CUSTOM_CATEGORY_ALREADY_WRITTEN')
                for service in targets: wait_service(service,REQUEST['images'][SERVICE_IMAGES[service]],REQUEST['binding'])
                if learn_only:
                    recovered,evidence=wait_personal_dependency_recovery(REQUEST['backend_baseline'],REQUEST['current_container_baseline'])
                    assert_preserved(before,recovered,REQUEST['managed_files'],targets)
                    require([row for row in recovered['code_files'] if row['path'] in selected]==REQUEST['managed_files'],'MANAGED_SOURCE_FINAL_HASH_MISMATCH')
                    check_backend_baseline(recovered,REQUEST['backend_baseline'],learn_only=True)
                    receipt['learn_dependency_readiness']['recovery']=evidence
                if code_only:
                    final=safe_snapshot()
                    receipt['learn_code_after']=learn_code_snapshot()
                    receipt['untargeted_runtime_after']=code_runtime(final,targets)
                    validate_code_preservation(receipt,REQUEST['learn_code_contract'])
                if alf:
                    receipt['untargeted_runtime_after']=code_runtime(after,targets)
                    require(receipt['untargeted_runtime_before']==receipt['untargeted_runtime_after'],'ALF_UNTARGETED_RUNTIME_CHANGED')
                    require(receipt['executed_migrations']==REQUEST['alf_contract']['migration_services'],'ALF_MIGRATIONS_INCOMPLETE')
                if alcf:
                    receipt['untargeted_runtime_after']=code_runtime(after,targets)
                    require(receipt['untargeted_runtime_before']==receipt['untargeted_runtime_after'],'ALCF_UNTARGETED_RUNTIME_CHANGED')
                    require(receipt['executed_migrations']==REQUEST['alcf_contract']['migration_services'],'ALCF_MIGRATIONS_INCOMPLETE')
                if lf:
                    recovered,_=wait_personal_dependency_recovery(REQUEST['backend_baseline'],REQUEST['current_container_baseline'])
                    check_backend_baseline(recovered,REQUEST['backend_baseline'],learn_frontend=True)
                    receipt['untargeted_runtime_after']=code_runtime(recovered,targets)
                    require(receipt['untargeted_runtime_before']==receipt['untargeted_runtime_after'],'LF_UNTARGETED_RUNTIME_CHANGED')
                    require(receipt['executed_migrations']==['learn'],'LF_MIGRATIONS_INCOMPLETE')
                receipt['status']='activated'
        receipt['runtime_overlay']=str(overlay)
    except Exception as error:
        receipt['error']=str(error) if isinstance(error,ActivateError) else 'ACTIVATION_FAILED'
        # A failed write may already have happened. Inspect exact project IDs;
        # never repeat the failed operation or execute old migration jobs.
        try:
            if frontend_attempted and REQUEST['frontend_only'] and isinstance(error,ActivateError) and str(error)=='TARGET_HEALTH_UNVERIFIED':
                failed=[row for row in containers() if row['service']=='frontend' and row['running'] and row['image_id']==REQUEST['images']['frontend']]
                require(len(failed)==1 and failed[0].get('health')!='healthy','FRONTEND_FAILURE_OUTCOME_UNCONFIRMED')
                prior=[row for row in before['containers'] if row['service']=='frontend' and row['running']]
                require(len(prior)==1,'ROLLBACK_FRONTEND_BASELINE_AMBIGUOUS')
                rollback=directory/'rollback-frontend.json'
                labels=source_labels(prior_frontend_binding)
                private_json(rollback,{'services':{'frontend':{'image':prior[0]['image_id'],'labels':labels}}})
                private_command([*compose_command(rollback),'up','-d','--no-deps','--no-build','--pull','never','--force-recreate','frontend'],directory/'rollback-frontend.log')
                wait_service('frontend',prior[0]['image_id'],prior_frontend_binding)
                receipt['frontend_image_rolled_back']=True
            elif backend_migrated or (code_only or alf or alcf or lf) and backend_attempted:
                ids=[row['container_id'] for row in containers() if row['service'] in targets
                     and row['service']!='frontend' and row['running'] and row['image_id'] in set(REQUEST['images'].values())]
                if code_only or alf or alcf or lf:
                    # Bound the CLI wait, never escalate the container to SIGKILL.
                    # An uncertain daemon result is inspected below, not retried.
                    try:
                        if ids: run(['docker','stop','--time','-1',*ids],limit=180)
                    except Exception:
                        receipt['recovery_outcome_unverified']=True
                elif ids:
                    run(['docker','stop','--time','30',*ids],limit=180)
                receipt['writer_fence_maintained']=not any(row['running'] and row['service'] in targets and row['service']!='frontend' for row in containers())
        except Exception:
            receipt['recovery_outcome_unverified']=True
    if directory is not None: private_json(directory/'activation-receipt.json',receipt)
    print(json.dumps(receipt))
activation_main()
'''


def remote_program():
    functions = (ActivateError,require,sha,canonical,activation_steps,derive_workers,source_labels,frontend_rollback_binding,
                 personal_identity,learn_fence_rows,original_personal_worker,learn_dependency_guard,check_backend_baseline,
                 clone_runtime_service,minimal_worker,compose_literal,make_overlay,copy_managed,
                 learn_migration_rows,validate_code_contract,validate_code_state,validate_code_preservation,code_runtime,validate_alf_contract,validate_alcf_contract,validate_lf_contract)
    constants = {"SERVICE_IMAGES":SERVICE_IMAGES,"MIGRATION_IMAGES":MIGRATION_IMAGES,"EXPECTED_HEADS":EXPECTED_HEADS,
                 "TRIGGER_TABLES":TRIGGER_TABLES,"API_ORDER":API_ORDER,"NEW_WORKERS":NEW_WORKERS,"PGOPTIONS":PGOPTIONS,"LEGACY_PROCESS_WORKERS":LEGACY_PROCESS_WORKERS,
                 "SCHEMA_TABLES":SCHEMA_TABLES,"SCHEMA_COLUMNS":SCHEMA_COLUMNS,"MIGRATION_SITE_CUSTOMIZE":MIGRATION_SITE_CUSTOMIZE,"RELEASE_ROOT":RELEASE_ROOT,
                  "LEARN_CATEGORY_HEAD":verify.LEARN_CATEGORY_HEAD,"LEARN_CODE_STATE_SQL":preflight.LEARN_CODE_STATE_SQL,
                  "ALCF_TEAM_SCHEMA_SQL":ALCF_TEAM_SCHEMA_SQL,"ALCF_ATTACHMENT_SCHEMA_SQL":ALCF_ATTACHMENT_SCHEMA_SQL,
                  "LF_TAG_SCHEMA_SQL":verify.LF_TAG_SCHEMA_SQL,"LF_TAG_SCHEMA_EXPECTED":verify.LF_TAG_SCHEMA_EXPECTED,
                  "REVISION":preflight.REVISION,"HASH":preflight.HASH}
    constants.update({'PERSONAL_WORKER':PERSONAL_WORKER,'PERSONAL_WORKER_COMMAND':PERSONAL_WORKER_COMMAND,'LEARN_WRITERS':LEARN_WRITERS})
    return (preflight.REMOTE_HELPERS + "\nimport copy,hashlib\n" + "\n".join(key+"="+repr(value) for key,value in constants.items()) + "\n"
            + "\n".join(inspect.getsource(function) for function in functions) + REMOTE)


def execution_payload(request):
    payload = canonical({**request,"program":base64.b64encode(remote_program().encode()).decode()}) + b"\n"
    require(len(payload) <= 16*1024*1024,"ACTIVATION_REQUEST_TOO_LARGE")
    bootstrap="import base64,json,sys;request=json.loads(sys.stdin.buffer.readline(16777217));program=base64.b64decode(request.pop('program'));exec(compile(program,'<renewal-activate>','exec'),{'REQUEST':request})"
    return "python3 -c " + shlex.quote(bootstrap),payload


def dependency_receipt_contract(value, request, *, recovered):
    require(request.get("learn_only") is True and request.get("frontend_only") is False,
            "REMOTE_DEPENDENCY_SCOPE_INVALID")
    keys = {"service", "dependency", "scope", "fence", "original_identity", "command", "source_labels", "before"}
    require(isinstance(value, dict) and set(value) in (keys, keys | {"recovery"})
            and value.get("service") == PERSONAL_WORKER and value.get("dependency") == "learn"
            and value.get("scope") == "learn-only-stopped-writers", "REMOTE_DEPENDENCY_EVIDENCE_INVALID")
    baseline, frozen = request["backend_baseline"], request["current_container_baseline"]
    expected_fence = learn_fence_rows({"containers": frozen}, baseline, frozen)
    original = [row for row in frozen if row["service"] == PERSONAL_WORKER]
    require(len(original) == 1, "REMOTE_DEPENDENCY_ORIGINAL_WORKER_MISSING")
    require(original[0].get("image_id") == baseline["images"]["auth"],
            "REMOTE_DEPENDENCY_ORIGINAL_WORKER_CHANGED")
    expected_identity = personal_identity(original[0])
    expected_labels = source_labels(baseline.get("image_bindings", {}).get("auth", baseline["binding"]))
    identity = value.get("original_identity")
    require(isinstance(identity, dict) and identity == expected_identity
            and type(identity.get("pid")) is int and type(identity.get("restart_count")) is int
            and value.get("command") == PERSONAL_WORKER_COMMAND and value.get("source_labels") == expected_labels,
            "REMOTE_DEPENDENCY_IDENTITY_UNVERIFIED")
    require(isinstance(value["fence"], list) and value["fence"] == expected_fence
            and all(type(row.get("pid")) is int and row.get("running") is False for row in value["fence"]),
            "REMOTE_DEPENDENCY_FENCE_UNVERIFIED")
    before = value["before"]
    require(isinstance(before, dict) and set(before) == {"docker_health", "health_status", "ready_status", "baseline_complete", "readiness_deferred"}
            and type(before.get("health_status")) is int and before["health_status"] == 200
            and type(before.get("ready_status")) is int and before["ready_status"] in (200, 503)
            and before.get("baseline_complete") is True and type(before.get("readiness_deferred")) is bool
            and before["readiness_deferred"] is (before["ready_status"] == 503)
            and (before.get("docker_health") == "healthy" or before.get("docker_health") == "unhealthy" and before["ready_status"] == 503),
            "REMOTE_DEPENDENCY_DEFERRED_STATE_INVALID")
    require(not recovered or "recovery" in value, "REMOTE_DEPENDENCY_RECOVERY_MISSING")
    if "recovery" not in value:
        return
    final = value["recovery"]
    require(isinstance(final, dict) and set(final) == {"identity", "command", "source_labels", "docker_health",
                "health_status", "ready_status", "baseline_complete", "observations", "elapsed_seconds", "maximum_wait_seconds"}
            and isinstance(final.get("identity"), dict) and final["identity"] == expected_identity
            and type(final["identity"].get("pid")) is int and type(final["identity"].get("restart_count")) is int
            and final.get("command") == PERSONAL_WORKER_COMMAND and final.get("source_labels") == expected_labels
            and final.get("docker_health") == "healthy"
            and type(final.get("health_status")) is int and final["health_status"] == 200
            and type(final.get("ready_status")) is int and final["ready_status"] == 200 and final.get("baseline_complete") is True
            and type(final.get("observations")) is int and 1 <= final["observations"] <= 91
            and type(final.get("elapsed_seconds")) in (int, float) and 0 <= final["elapsed_seconds"] <= 180
            and type(final.get("maximum_wait_seconds")) is int and final["maximum_wait_seconds"] == 180,
            "REMOTE_DEPENDENCY_RECOVERY_UNVERIFIED")


def validate_remote_receipt(value, request):
    allowed = {"schema_version","kind","binding","frontend_only","status","plan_sha256","backup_receipt_sha256","quiescence_sha256",
               "database_restore_performed","old_migration_jobs_executed","protected_files_replaced","images_pruned","current_step",
               "containers_before","protected_files","unmanaged_files","managed_files","schema","notification_baseline_verified",
               "runtime_overlay","error","frontend_image_rolled_back","writer_fence_maintained","recovery_outcome_unverified","legacy_worker_process_stability",
                "learn_only","baseline_receipts","category_compatibility","compatibility_image_backup_receipt_sha256","category_before_migration","learn_dependency_readiness",
                "release_phase","learn_code_contract","learn_code_before","learn_code_after","migration_executed","untargeted_runtime_before","untargeted_runtime_after",
                "auth_learn_frontend","alf_contract","auth_learn_community_frontend","alcf_contract","executed_migrations","learn_frontend","lf_contract"}
    require(isinstance(value,dict) and set(value) <= allowed and value.get("schema_version") == 1
            and value.get("kind") == "renewal-activate" and value.get("binding") == request["binding"]
            and value.get("frontend_only") is request["frontend_only"],"REMOTE_ACTIVATION_BINDING_MISMATCH")
    require(value.get("learn_only",False) is request.get("learn_only",False),"REMOTE_ACTIVATION_SCOPE_CHANGED")
    code_only = preflight.code_only_phase(request)
    alf = preflight.alf_phase(request)
    alcf = preflight.alcf_phase(request)
    lf = preflight.lf_phase(request)
    require(preflight.alf_phase(value) is alf and value.get("alf_contract") == request.get("alf_contract"), "REMOTE_ACTIVATION_PHASE_CHANGED")
    require(preflight.alcf_phase(value) is alcf and value.get("alcf_contract") == request.get("alcf_contract"), "REMOTE_ACTIVATION_PHASE_CHANGED")
    require(preflight.lf_phase(value) is lf and value.get("lf_contract") == request.get("lf_contract"), "REMOTE_ACTIVATION_PHASE_CHANGED")
    if alf or alcf or lf:
        scoped_contract = value["lf_contract"] if lf else value["alf_contract"] if alf else value["alcf_contract"]
        (validate_lf_contract if lf else validate_alf_contract if alf else validate_alcf_contract)(scoped_contract)
        executed = value.get("executed_migrations")
        expected_migrations = scoped_contract["migration_services"]
        require(isinstance(executed, list) and executed == expected_migrations[:len(executed)], "ALF_MIGRATIONS_CHANGED")
        require(not any(name in value for name in ("learn_code_before", "learn_code_after", "migration_executed", "category_before_migration")),
                "ALF_UNEXPECTED_CODE_ONLY_EVIDENCE")
    else:
        require("executed_migrations" not in value, "REMOTE_UNEXPECTED_ALF_EVIDENCE")
    require(all(value.get(key) == request.get(key) for key in ("release_phase", "learn_code_contract")), "REMOTE_ACTIVATION_PHASE_CHANGED")
    if code_only:
        validate_code_contract(request["learn_code_contract"])
        require(value.get("migration_executed") is False and "category_before_migration" not in value, "LEARN_CODE_MIGRATION_FORBIDDEN")
        for name in ("learn_code_before", "learn_code_after"):
            if name in value: validate_code_state(value[name])
    elif not alf and not alcf and not lf:
        require(not any(name in value for name in ("learn_code_before", "learn_code_after", "migration_executed", "untargeted_runtime_before", "untargeted_runtime_after")),
                "REMOTE_UNEXPECTED_CODE_ONLY_EVIDENCE")
    if "baseline_receipts" in request:
        require(value.get("baseline_receipts")==request["baseline_receipts"],"REMOTE_ACTIVATION_BASELINE_CHANGED")
    if request.get("compatibility_image_backup_receipt_sha256"):
        require(value.get("compatibility_image_backup_receipt_sha256")==request["compatibility_image_backup_receipt_sha256"],"REMOTE_COMPATIBILITY_IMAGE_BACKUP_CHANGED")
    require(value.get("status") in {"activated","partial-unverified"}
            and all(value.get(name) is False for name in ("database_restore_performed","old_migration_jobs_executed","protected_files_replaced","images_pruned")),
            "REMOTE_ACTIVATION_BOUNDARY_INVALID")
    require(all(value.get(name) == request[name] for name in ("plan_sha256","backup_receipt_sha256","quiescence_sha256")),"REMOTE_ACTIVATION_HASH_CHANGED")
    for name in ("notification_baseline_verified","frontend_image_rolled_back","writer_fence_maintained","recovery_outcome_unverified"):
        require(name not in value or type(value[name]) is bool,"REMOTE_ACTIVATION_FLAG_INVALID")
    if "error" in value:
        require(isinstance(value["error"],str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}",value["error"]),"REMOTE_ACTIVATION_ERROR_UNSAFE")
    if "current_step" in value:
        workers=sorted(name for name,family in SERVICE_IMAGES.items() if name not in (*API_ORDER,"frontend") and (family=="learn" if lf else family in {"auth", "learn"} if alf else not request.get("learn_only") or family=="learn"))
        allowed_steps = activation_steps(workers,frontend_only=request["frontend_only"],learn_only=request.get("learn_only",False),learn_code_only=code_only,
            auth_learn_frontend=alf,auth_learn_community_frontend=alcf,learn_frontend=lf,
            migration_services=request["lf_contract"]["migration_services"] if lf else request["alf_contract"]["migration_services"] if alf else request["alcf_contract"]["migration_services"] if alcf else ())
        require(isinstance(value["current_step"],list) and tuple(value["current_step"]) in allowed_steps,"REMOTE_ACTIVATION_STEP_INVALID")
    for name in ("managed_files","unmanaged_files","protected_files"):
        if name not in value:
            continue
        rows = verify.file_rows(value[name],absolute=name == "protected_files")
        if name in {"managed_files","protected_files"}:
            require(rows == verify.file_rows(request[name],absolute=name == "protected_files"),"REMOTE_ACTIVATION_FILES_CHANGED")
        else:
            require(not {row["path"] for row in rows} & {row["path"] for row in request["managed_files"]},"REMOTE_ACTIVATION_UNMANAGED_INVALID")
    if "containers_before" in value:
        require(all(isinstance(row,dict) and set(row) == {"service","container_id","image_id","running"} for row in value["containers_before"]),
                "REMOTE_ACTIVATION_CONTAINER_FIELDS_INVALID")
        require(verify.container_rows(value["containers_before"]) == verify.container_rows(request["current_container_baseline"]),"REMOTE_ACTIVATION_CONTAINERS_CHANGED")
    if "schema" in value:
        require(value["schema"] == {"heads":{name:[head] for name,head in request.get("expected_heads",EXPECTED_HEADS).items()},"trigger_count":18,"required_schema_verified":True},
                "REMOTE_ACTIVATION_SCHEMA_INVALID")
    if "legacy_worker_process_stability" in value:
        evidence=value["legacy_worker_process_stability"]
        require(isinstance(evidence,dict) and set(evidence)<=set(LEGACY_PROCESS_WORKERS)
                and all(row=={"readiness_available":False,"process_stable":True,"observations":2} for row in evidence.values()),"REMOTE_LEGACY_WORKER_EVIDENCE_INVALID")
    if "runtime_overlay" in value:
        require(value["runtime_overlay"] == RELEASE_ROOT + "/" + request["binding"]["release_nonce"] + "/activation/runtime-overlay.json",
                "REMOTE_ACTIVATION_OVERLAY_PATH_INVALID")
    if "category_compatibility" in value:
        guard=value["category_compatibility"]
        require(isinstance(guard,dict) and set(guard)=={"custom_category_count","custom_event_count","legacy_enum_image_rollback_forbidden","automatic_backend_rollback_performed"}
                and all(type(guard[name]) is int and guard[name]>=0 for name in ("custom_category_count","custom_event_count"))
                and guard["legacy_enum_image_rollback_forbidden"] is bool(guard["custom_category_count"] or guard["custom_event_count"])
                and guard["automatic_backend_rollback_performed"] is False,"REMOTE_CATEGORY_ROLLBACK_GUARD_INVALID")
    if "category_before_migration" in value:
        prior=value["category_before_migration"]
        require(request.get("learn_only") is True and isinstance(prior,dict) and set(prior)=={"previous_head","registry_absent","legacy_constraint_valid","custom_event_count"}
                and prior["previous_head"]=="20261013_028" and prior["registry_absent"] is True and prior["legacy_constraint_valid"] is True
                and type(prior["custom_event_count"]) is int and prior["custom_event_count"]==0,"REMOTE_CATEGORY_PRE_MIGRATION_STATE_INVALID")
    if "learn_dependency_readiness" in value:
        dependency_receipt_contract(value["learn_dependency_readiness"], request, recovered=value["status"] == "activated")
    if value["status"] == "activated":
        require(all(name in value for name in ("managed_files","unmanaged_files","protected_files","containers_before","schema","runtime_overlay"))
                and (request["frontend_only"] or request.get("learn_only") or alf or lf or value.get("notification_baseline_verified") is True)
                and (request["frontend_only"] or set(value.get("legacy_worker_process_stability",{})) == set(LEGACY_PROCESS_WORKERS))
                and "error" not in value,"REMOTE_ACTIVATION_SUCCESS_UNVERIFIED")
        graph = request["alf_contract"]["migration_graph"] if alf else request["alcf_contract"]["migration_graph"] if alcf else {}
        if lf or request.get("expected_heads",EXPECTED_HEADS)["learn"]==verify.LEARN_CATEGORY_HEAD or (alf or alcf) and '20261013_029' in graph['learn']['revisions']:
            require("category_compatibility" in value and (not request.get("learn_only") or code_only or value["category_compatibility"]["legacy_enum_image_rollback_forbidden"] is False),"REMOTE_CATEGORY_COMPATIBILITY_UNVERIFIED")
        require(not request.get("learn_only") or code_only or "category_before_migration" in value,"REMOTE_CATEGORY_PRE_MIGRATION_STATE_MISSING")
        if code_only:
            validate_code_preservation(value, request["learn_code_contract"])
        if alf or alcf or lf:
            require(value["executed_migrations"] == expected_migrations and isinstance(value.get("untargeted_runtime_before"), list)
                    and value["untargeted_runtime_before"] and value["untargeted_runtime_before"] == value.get("untargeted_runtime_after"), "ALF_PRESERVATION_UNVERIFIED")
            require(all(value.get(name, False) is False for name in ("recovery_outcome_unverified", "writer_fence_maintained", "frontend_image_rolled_back")), "ALF_SUCCESS_FAILURE_FLAG")
        require(not request.get("learn_only") or "learn_dependency_readiness" in value,"REMOTE_DEPENDENCY_EVIDENCE_MISSING")
        if request.get("learn_only"):
            require(all(value.get(name, False) is False for name in
                        ("recovery_outcome_unverified", "writer_fence_maintained", "frontend_image_rolled_back")),
                    "REMOTE_DEPENDENCY_SUCCESS_FAILURE_FLAG")
    return value


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("plan","build-receipt","backup-receipt","quiescence-attestation"):
        parser.add_argument("--"+name,type=Path,required=True)
    for name in ("plan-sha256","commit","source-fingerprint","release-nonce"):
        parser.add_argument("--"+name,required=True)
    scope=parser.add_mutually_exclusive_group()
    scope.add_argument("--frontend-only",action="store_true")
    scope.add_argument("--learn-only",action="store_true")
    scope.add_argument("--auth-learn-frontend",action="store_true")
    scope.add_argument("--auth-learn-community-frontend",action="store_true")
    scope.add_argument("--learn-frontend",action="store_true")
    parser.add_argument("--learn-code-only",action="store_true")
    for name in ("baseline-activation-receipt","baseline-build-receipt","baseline-verify-receipt"):
        parser.add_argument("--"+name,type=Path)
    parser.add_argument("--baseline-release",nargs=3,action="append",type=Path,metavar=("ACTIVATION","BUILD","VERIFY"))
    parser.add_argument("--compatibility-image-backup-receipt",type=Path)
    parser.add_argument("--compatibility-image-backup-receipt-sha256")
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args(argv)
    require(not args.learn_code_only or args.learn_only, "LEARN_CODE_SCOPE_INVALID")
    binding=verify.binding_for(args.commit,args.source_fingerprint,args.release_nonce)
    manifest=ops.manifest(); rows=stage.validate_manifest(manifest,binding['source_fingerprint'])
    require(rows.get("scripts/renewal/activate_release.py",{}).get("sha256")==EXECUTOR_SOURCE_SHA256,"EXECUTOR_SOURCE_CHANGED_SINCE_IMPORT")
    require(all(rows.get("scripts/renewal/"+name+".py",{}).get("sha256") == expected
                for name,expected in COMPONENT_SOURCE_SHA256.items()),"COMPONENT_SOURCE_CHANGED_SINCE_IMPORT")
    modes=stage.bind_checkout(ops.WORKSPACE/'deploy'/'CTF_Web',ops.SOURCE,manifest,binding)
    plan,plan_sha=verify.load_receipt(args.plan)
    require(plan_sha==args.plan_sha256,"PLAN_HASH_CHANGED")
    preflight.validate_plan(plan,binding['source_fingerprint'])
    require(plan.get('learn_only',False) is args.learn_only,'ACTIVATION_PLAN_SCOPE_CHANGED')
    require(preflight.code_only_phase(plan) is args.learn_code_only, 'ACTIVATION_PLAN_PHASE_CHANGED')
    require(preflight.alf_phase(plan) is args.auth_learn_frontend, 'ACTIVATION_PLAN_PHASE_CHANGED')
    require(preflight.alcf_phase(plan) is args.auth_learn_community_frontend, 'ACTIVATION_PLAN_PHASE_CHANGED')
    require(preflight.lf_phase(plan) is args.learn_frontend, 'ACTIVATION_PLAN_PHASE_CHANGED')
    require(plan['nonce']==binding['release_nonce'] and plan['source_file_count']==len(rows),"PLAN_SOURCE_BINDING_CHANGED")
    baseline_paths=verify.baseline_inputs(args)
    chain_mode=bool(args.baseline_release) or args.learn_only or args.auth_learn_frontend or args.auth_learn_community_frontend or args.learn_frontend
    baseline=load_baseline(baseline_paths if chain_mode else baseline_paths[0],manifest,frontend_only=args.frontend_only,
                           learn_only=args.learn_only,chain_mode=chain_mode,learn_code_only=args.learn_code_only,auth_learn_frontend=args.auth_learn_frontend,
                           auth_learn_community_frontend=args.auth_learn_community_frontend,learn_frontend=args.learn_frontend) if baseline_paths else None
    if args.learn_code_only:
        expected=preflight.make_code_contract(manifest['files'],plan['inspection'],preflight.source_migrations(),baseline['contract'],baseline['hashes'])
        require(plan['learn_code_contract']==expected,'LEARN_CODE_BASELINE_CHAIN_CHANGED')
    if args.auth_learn_frontend:
        expected=preflight.make_alf_contract(manifest['files'],plan['inspection'],preflight.source_migrations(),baseline['contract'],baseline['hashes'])
        require(plan['alf_contract']==expected,'ALF_BASELINE_CHAIN_CHANGED')
    if args.auth_learn_community_frontend:
        expected=preflight.make_alcf_contract(manifest['files'],plan['inspection'],preflight.source_migrations(),baseline['contract'],baseline['hashes'])
        require(plan['alcf_contract']==expected,'ALCF_BASELINE_CHAIN_CHANGED')
    if args.learn_frontend:
        expected=preflight.make_lf_contract(manifest['files'],plan['inspection'],preflight.source_migrations(),baseline['contract'],baseline['hashes'])
        require(plan['lf_contract']==expected,'LF_BASELINE_CHAIN_CHANGED')
    built,build_sha=verify.load_receipt(args.build_receipt)
    contract=verify.build_contract(built,binding,frontend_only=args.frontend_only,learn_only=args.learn_only,baseline=baseline['contract'] if baseline else None,auth_learn_frontend=args.auth_learn_frontend,
                                   auth_learn_community_frontend=args.auth_learn_community_frontend,learn_frontend=args.learn_frontend)
    require(preflight.code_only_phase(built) is args.learn_code_only
            and built.get('learn_code_contract')==plan.get('learn_code_contract'), 'ACTIVATION_BUILD_PHASE_CHANGED')
    require(preflight.alf_phase(built) is args.auth_learn_frontend and built.get('alf_contract')==plan.get('alf_contract'),'ACTIVATION_BUILD_PHASE_CHANGED')
    require(preflight.alcf_phase(built) is args.auth_learn_community_frontend and built.get('alcf_contract')==plan.get('alcf_contract'),'ACTIVATION_BUILD_PHASE_CHANGED')
    compatibility_backup_sha=compatibility_image_backup(args.compatibility_image_backup_receipt,args.compatibility_image_backup_receipt_sha256,
        plan,plan_sha,baseline['contract'] if baseline else None,
        required=args.learn_code_only or args.frontend_only and baseline['contract'].get('expected_heads',EXPECTED_HEADS)['learn']==verify.LEARN_CATEGORY_HEAD)
    backup,backup_sha=verify.load_receipt(args.backup_receipt)
    quiescence,quiescence_sha=verify.load_receipt(args.quiescence_attestation)
    if baseline and not args.learn_only and not args.auth_learn_frontend and not args.auth_learn_community_frontend and not args.learn_frontend:
        prior=baseline['activation']
        require(prior.get('backup_receipt_sha256')==backup_sha and prior.get('quiescence_sha256')==quiescence_sha,"BASELINE_BACKUP_CHANGED")
        backup_binding=baseline.get('backup_binding',baseline['contract']['binding'])
        backup_contract(backup,backup_binding,prior['plan_sha256'],quiescence_sha,baseline['old_containers'],learn_only=prior.get('learn_only',False),auth_learn_frontend=preflight.alf_phase(prior),
                        auth_learn_community_frontend=preflight.alcf_phase(prior),learn_frontend=preflight.lf_phase(prior))
        require(quiescence.get('nonce')==backup_binding['release_nonce'] and quiescence.get('source_fingerprint')==backup_binding['source_fingerprint'],"BASELINE_QUIESCENCE_CHANGED")
        current_baseline=plan['inspection']['containers']
    else:
        backup_contract(backup,binding,plan_sha,quiescence_sha,plan['inspection']['containers'],learn_only=args.learn_only,auth_learn_frontend=args.auth_learn_frontend,
                        auth_learn_community_frontend=args.auth_learn_community_frontend,learn_frontend=args.learn_frontend)
        require(not args.auth_learn_frontend or backup.get('alf_contract')==plan['alf_contract'],'ALF_BACKUP_CONTRACT_CHANGED')
        require(not args.auth_learn_community_frontend or backup.get('alcf_contract')==plan['alcf_contract'],'ALCF_BACKUP_CONTRACT_CHANGED')
        require(not args.learn_frontend or backup.get('lf_contract')==plan['lf_contract'],'LF_BACKUP_CONTRACT_CHANGED')
        frozen=backup_release.validate_quiescence(quiescence,plan,plan_sha,learn_only=args.learn_only)
        current_baseline=copy.deepcopy(frozen['containers'])
        # MinIO must already be restarted by the authorized backup operator.
        # Match its original ID/image/running state; activation never restarts it.
        minio=next(row for row in plan['inspection']['containers'] if row['service']=='minio')
        for index,row in enumerate(current_baseline):
            if row['service']=='minio': current_baseline[index]=minio
    captured=verify_backup_files(backup)
    if args.learn_code_only:
        require(compatibility_backup_sha==backup['image_backup_receipt_sha256'], 'LEARN_CODE_PREIMAGE_BACKUP_CHANGED')
    managed=selected_rows(manifest,args.frontend_only,learn_only=args.learn_only,auth_learn_frontend=args.auth_learn_frontend,
                          auth_learn_community_frontend=args.auth_learn_community_frontend,learn_frontend=args.learn_frontend)
    require(contract['protected_files']==verify.file_rows(plan['inspection']['protected_files'],absolute=True),"PLAN_BUILD_PROTECTED_CHANGED")
    request={'binding':binding,'frontend_only':args.frontend_only,'learn_only':args.learn_only,'managed_files':managed,'modes':{row['path']:modes[row['path']] for row in managed},
             'images':contract['images'],'protected_files':contract['protected_files'],'current_container_baseline':current_baseline,
             'image_bindings':contract['image_bindings'],'frontend_args_sha256':built['remote']['live_before']['frontend_args_sha256'],
             'plan_sha256':plan_sha,'backup_receipt_sha256':backup_sha,'quiescence_sha256':quiescence_sha,
             'migration_wrapper_sha256':sha(MIGRATION_SITE_CUSTOMIZE.encode()),
             'backend_baseline':baseline['contract'] if baseline else None,
             'worker_templates':None if args.frontend_only or args.learn_only or args.learn_frontend else worker_templates()}
    if args.learn_code_only:
        request.update({key:plan[key] for key in ('release_phase','learn_code_contract')})
    if args.auth_learn_frontend:
        request.update({key:plan[key] for key in preflight.ALF_FIELDS})
    if args.auth_learn_community_frontend:
        request.update({key:plan[key] for key in preflight.ALCF_FIELDS})
    if args.learn_frontend:
        request.update({key:plan[key] for key in preflight.LF_FIELDS})
    if chain_mode:
        request['baseline_receipts']=baseline['hashes']
        request['expected_heads']=dict(baseline['contract']['expected_heads'])
        if args.learn_only: request['expected_heads']['learn']=verify.LEARN_CATEGORY_HEAD
        if args.auth_learn_frontend: request['expected_heads']=dict(plan['alf_contract']['target_heads'])
        if args.auth_learn_community_frontend: request['expected_heads']=dict(plan['alcf_contract']['target_heads'])
        if args.learn_frontend: request['expected_heads']=dict(plan['lf_contract']['target_heads'])
    elif not args.frontend_only:
        request['expected_heads']={name:row['head'] for name,row in plan['migrations'].items()}
    if compatibility_backup_sha:
        request['compatibility_image_backup_receipt_sha256']=compatibility_backup_sha
    local={'schema_version':1,'kind':'renewal-activate-plan','binding':binding,'frontend_only':args.frontend_only,'execute':args.execute,
           'managed_file_count':len(managed),'backup_files_verified':captured,'migration_wrapper_sha256':request['migration_wrapper_sha256'],
           'plan_sha256':plan_sha,'build_receipt_sha256':build_sha,'backup_receipt_sha256':backup_sha,'quiescence_sha256':quiescence_sha,
           'activation_performed':False,'database_restore_performed':False,'secret_values_recorded':False}
    if chain_mode:
        local.update({'learn_only':args.learn_only,'baseline_receipts':baseline['hashes']})
    if args.learn_code_only:
        local.update({key:plan[key] for key in ('release_phase','learn_code_contract')})
    if args.auth_learn_frontend:
        local.update({key:plan[key] for key in preflight.ALF_FIELDS})
    if args.auth_learn_community_frontend:
        local.update({key:plan[key] for key in preflight.ALCF_FIELDS})
    if args.learn_frontend:
        local.update({key:plan[key] for key in preflight.LF_FIELDS})
    if compatibility_backup_sha:
        local['compatibility_image_backup_receipt_sha256']=compatibility_backup_sha
    require(ops.manifest()['fingerprint']==binding['source_fingerprint'],'SOURCE_CHANGED_BEFORE_ACTIVATION')
    if not args.execute:
        print(json.dumps(local)); return 0
    with quiesce.action_lease(plan,plan_sha):
        require(ops.manifest()['fingerprint']==binding['source_fingerprint'],'SOURCE_CHANGED_BEFORE_ACTIVATION')
        require(stage.git_bytes(ops.WORKSPACE/'deploy'/'CTF_Web','rev-parse','HEAD').decode().strip()==binding['commit'],'COMMIT_CHANGED_BEFORE_ACTIVATION')
        ops.receipt('activate-intent-'+binding['release_nonce']+'.json',local)
        local.pop('activation_performed')
        command,payload=execution_payload(request)
        try:
            result=validate_remote_receipt(preflight.parse_json(ops.ssh(command,timeout=7200,stdin=payload)),request)
            if result.get('status')=='activated':
                verify.activation_contract(result,binding,managed,contract,baseline=baseline['contract'] if baseline else None,learn_only=args.learn_only,auth_learn_frontend=args.auth_learn_frontend,
                                           auth_learn_community_frontend=args.auth_learn_community_frontend,learn_frontend=args.learn_frontend)
            local.update(result)
        except Exception:
            local.update({'kind':'renewal-activate','status':'remote-outcome-unconfirmed','error':'INSPECT_BEFORE_RETRY'})
        local.update({'build_receipt_sha256':build_sha,'recorded_at':datetime.now(timezone.utc).isoformat(),'secret_values_recorded':False})
        receipt=ops.receipt('activate-complete-'+binding['release_nonce']+'.json',local)
    print(json.dumps({'receipt':str(receipt),'status':local.get('status'),'binding':binding,'secret_values_printed':False}))
    return 0 if local.get('status')=='activated' else 1


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'ok':False,'error':str(error) if isinstance(error,ActivateError) else 'LOCAL_ACTIVATION_FAILED','secret_values_printed':False}))
        raise SystemExit(1)
