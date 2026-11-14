"""Source-bound, read-only post-activation verification.

The default prints a local plan without SSH. --execute only reads the fixed
ctf_web project and emits aggregate evidence; public product HTTP is separate.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
import uuid

import ops
import release_preflight

code_runtime = release_preflight.code_runtime


CONTEXTS = ("frontend/", "services/auth/", "services/learn/", "services/community/")
SERVICE_IMAGES = {
    "auth-service": "auth", "personal-notification-runner": "auth",
    "learn-service": "learn", "knowledge-candidate-runner": "learn",
    "education-asset-cleanup-runner": "learn", "challenge-image-cleanup-runner": "learn",
    "lesson-image-reconcile-runner": "learn", "community-service": "community",
    "community-inquiry-runner": "community", "marketplace-settlement-runner": "community",
    "comment-notification-runner": "community", "frontend": "frontend",
}
MIGRATION_IMAGES = {"auth-migrate": "auth", "learn-migrate": "learn", "community-migrate": "community"}
EXPECTED_HEADS = {"auth": "20261013_005", "learn": "20261013_028", "community": "20261013_005"}
LEARN_CATEGORY_HEAD = "20261013_029"
TRIGGER_TABLES = (
    "challenges", "courses", "lessons", "hints", "knowledge_tags",
    "challenge_knowledge_tags", "course_knowledge_tags", "lesson_knowledge_tags",
    "challenge_prerequisites", "challenge_learning_prerequisites", "course_prerequisites",
    "knowledge_content_nodes", "knowledge_relations", "platform_events", "teaching_classes",
    "education_assignments", "teaching_class_enrollments", "education_role_grants",
)
LEGACY_PROCESS_WORKERS = {name:"app.workers."+name.replace("-","_") for name in (
    "knowledge-candidate-runner","education-asset-cleanup-runner","challenge-image-cleanup-runner","lesson-image-reconcile-runner")}
UNVERIFIED = ("public_product_http_and_browser", "external_delivery_and_model_behavior", "legacy_learn_workers_readiness_not_available")
FRONTEND_ARGS_LABEL = "io.ctf-web.frontend-build-args-sha256"
LF_TAG_SCHEMA_SQL = """SELECT json_build_object(
 'archive_table', to_regclass('public.knowledge_tag_deletion_archives') IS NOT NULL,
 'archive_columns', (SELECT count(*)=7 FROM information_schema.columns WHERE table_schema='public'
 AND table_name='knowledge_tag_deletion_archives' AND column_name IN
 ('id','tag_id','actor_id','tag_snapshot','impact_snapshot','graph_revision','deleted_at')),
 'archive_indexes', (SELECT count(*)=2 FROM pg_indexes WHERE schemaname='public'
 AND tablename='knowledge_tag_deletion_archives' AND indexname IN
 ('ix_knowledge_tag_deletion_archives_tag_id','ix_knowledge_tag_deletion_archives_actor_id')),
 'audit_fk_absent', NOT EXISTS(SELECT 1 FROM pg_constraint WHERE contype='f'
 AND conrelid='public.knowledge_tag_mapping_audit_events'::regclass
 AND confrelid='public.knowledge_tags'::regclass),
 'outbox_aggregate', EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='public.knowledge_projection_outbox'::regclass
 AND conname='ck_knowledge_projection_outbox_aggregate' AND contype='c' AND convalidated
 AND pg_get_constraintdef(oid) LIKE '%knowledge_tag%'),
 'outbox_event', EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='public.knowledge_projection_outbox'::regclass
 AND conname='ck_knowledge_projection_outbox_event' AND contype='c' AND convalidated
 AND pg_get_constraintdef(oid) LIKE '%tag_deleted%'))"""
LF_TAG_SCHEMA_EXPECTED = dict.fromkeys(("archive_table", "archive_columns", "archive_indexes", "audit_fk_absent", "outbox_aggregate", "outbox_event"), True)
ALCF_TEAM_SCHEMA_SQL = """SELECT
 (SELECT count(*)=4 FROM information_schema.tables WHERE table_schema='public' AND table_name IN
 ('competition_team_registrations','competition_team_members','competition_team_assignments','competition_team_solves'))
 AND (SELECT count(*)=1 FROM information_schema.columns WHERE table_schema='public' AND table_name='competitions'
 AND column_name='team_contract' AND data_type='character varying' AND character_maximum_length=30 AND is_nullable='YES')
 AND EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='public.competitions'::regclass
 AND conname='ck_competitions_team_contract' AND contype='c' AND convalidated)
 AND (SELECT count(*)=2 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_proc p ON p.oid=t.tgfoid WHERE n.nspname='public' AND NOT t.tgisinternal AND t.tgenabled IN ('O','A')
 AND t.tgtype=23 AND p.proname='guard_fixed_roster_legacy_write' AND NOT p.prosecdef
 AND ((c.relname='competition_participants' AND t.tgname='competition_participants_fixed_roster_guard')
 OR (c.relname='competition_assignments' AND t.tgname='competition_assignments_fixed_roster_guard')))"""
ALCF_ATTACHMENT_SCHEMA_SQL = """SELECT
 (SELECT count(*)=2 FROM information_schema.tables WHERE table_schema='public'
 AND table_name IN ('challenge_attachment_drafts','challenge_attachments'))
 AND (SELECT count(*)=1 FROM information_schema.columns WHERE table_schema='public' AND table_name='challenges'
 AND column_name='attachment_revision' AND data_type='integer' AND is_nullable='NO')
 AND (SELECT count(*)=4 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
 WHERE n.nspname='public' AND c.contype='c' AND c.convalidated AND
 ((t.relname='challenge_attachment_drafts' AND c.conname IN ('ck_challenge_attachment_draft_state','ck_challenge_attachment_draft_revision'))
 OR (t.relname='challenge_attachments' AND c.conname IN ('ck_challenge_attachment_size','ck_challenge_attachment_state'))))"""


class VerifyError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise VerifyError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def binding_for(commit, fingerprint, nonce):
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit), "COMMIT_INVALID")
    require(isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint), "SOURCE_FINGERPRINT_INVALID")
    require(isinstance(nonce, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", nonce), "RELEASE_NONCE_INVALID")
    return {"commit": commit, "source_fingerprint": fingerprint, "release_nonce": nonce}


def file_rows(rows, *, absolute=False):
    require(isinstance(rows, list) and len(rows) <= 100000, "FILE_ROWS_INVALID")
    result = []
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}, "FILE_ROW_FIELDS_INVALID")
        name = row["path"]
        require(isinstance(name, str) and name and "\\" not in name and not any(ord(c) < 32 for c in name), "FILE_PATH_INVALID")
        path = PurePosixPath(name)
        require(path.parts and path.as_posix() == name and path.is_absolute() is absolute and ".." not in path.parts, "FILE_PATH_INVALID")
        if absolute:
            require(path.is_relative_to("/home/deploy") or path.is_relative_to("/var/lib/ctf-web"), "PROTECTED_PATH_SCOPE_INVALID")
        else:
            require(not any(part in {".git", ".verification", "node_modules", ".venv", "__pycache__", ".next"} for part in path.parts), "MANAGED_PATH_FORBIDDEN")
            require(not (path.name.startswith(".env") and path.name not in {".env.example", ".env.sample", ".env.template", ".env.schema"})
                    and path.suffix.lower() not in {".key", ".pem"}, "MANAGED_SECRET_PATH_FORBIDDEN")
        require(type(row["bytes"]) is int and 0 <= row["bytes"] <= 512 * 1024 * 1024
                and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "FILE_HASH_INVALID")
        result.append(dict(row))
    require(len({row["path"] for row in result}) == len(result), "DUPLICATE_FILE_PATH")
    return sorted(result, key=lambda row: row["path"])


def container_rows(rows):
    require(isinstance(rows, list) and 0 < len(rows) <= 1000, "CONTAINER_ROWS_INVALID")
    result = []
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get("service"), str)
                and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", row["service"])
                and isinstance(row.get("container_id"), str) and re.fullmatch(r"[0-9a-f]{64}", row["container_id"])
                and isinstance(row.get("image_id"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"])
                and type(row.get("running")) is bool, "CONTAINER_IDENTITY_INVALID")
        result.append({key: row[key] for key in ("service", "container_id", "image_id", "running")})
    require(len({row["container_id"] for row in result}) == len(result), "DUPLICATE_CONTAINER_ID")
    return sorted(result, key=lambda row: (row["service"], row["container_id"]))


def release_families(*, frontend_only=False, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(all(type(item) is bool for item in (frontend_only, learn_only, auth_learn_frontend, auth_learn_community_frontend, learn_frontend))
            and sum((frontend_only, learn_only, auth_learn_frontend, auth_learn_community_frontend, learn_frontend)) <= 1, "RELEASE_SCOPE_INVALID")
    return ("learn", "frontend") if learn_frontend else ("auth", "learn", "frontend") if auth_learn_frontend else ("frontend",) if frontend_only else ("learn",) if learn_only else ("auth", "learn", "community", "frontend")


def baseline_summary(baseline, families):
    retained = [name for name in baseline["images"] if name not in families]
    return {"binding": baseline["binding"], "images": {name: baseline["images"][name] for name in retained},
            "image_bindings": {name: baseline["image_bindings"][name] for name in retained},
            "expected_heads": baseline.get("expected_heads", EXPECTED_HEADS)}


def build_contract(value, binding, *, frontend_only=False, learn_only=False, baseline=None, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(isinstance(value, dict) and value.get("schema_version") == 1 and value.get("kind") == "renewal-stage-build"
            and value.get("binding") == binding and value.get("status") == "built-not-activated", "BUILD_RECEIPT_UNVERIFIED")
    remote = value.get("remote", {})
    require(remote.get("binding") == binding and remote.get("kind") == "renewal-stage-build"
            and remote.get("status") == "built-not-activated" and remote.get("live_state_unchanged") is True
            and remote.get("runtime_readability_verified") is True,
            "BUILD_REMOTE_UNVERIFIED")
    require(remote.get("frontend_only", False) is frontend_only, "BUILD_MODE_CHANGED")
    require(remote.get("learn_only", False) is learn_only, "BUILD_MODE_CHANGED")
    require(release_preflight.alf_phase(value) is auth_learn_frontend and release_preflight.alf_phase(remote) is auth_learn_frontend
            and all(value.get(key) == remote.get(key) for key in release_preflight.ALF_FIELDS), "BUILD_ALF_SCOPE_CHANGED")
    if auth_learn_frontend:
        contract = release_preflight.validate_alf_contract(value["alf_contract"])
        require(baseline is not None and baseline["expected_heads"] == contract["previous_heads"], "ALF_BASELINE_HEADS_CHANGED")
    require(release_preflight.alcf_phase(value) is auth_learn_community_frontend and release_preflight.alcf_phase(remote) is auth_learn_community_frontend
            and all(value.get(key) == remote.get(key) for key in release_preflight.ALCF_FIELDS), "BUILD_ALCF_SCOPE_CHANGED")
    if auth_learn_community_frontend:
        contract = release_preflight.validate_alcf_contract(value["alcf_contract"])
        require(baseline is not None and baseline["expected_heads"] == contract["previous_heads"]
                and baseline["images"] == contract["previous_images"]
                and file_rows(baseline["managed_files"]) == file_rows(contract["previous_source_files"])
                and file_rows([row for row in baseline["unmanaged_files"] if row["path"].startswith(CONTEXTS)])
                == contract["unmanaged_in_target_families"], "ALCF_BASELINE_CHANGED")
    require(release_preflight.lf_phase(value) is learn_frontend and release_preflight.lf_phase(remote) is learn_frontend
            and all(value.get(key) == remote.get(key) for key in release_preflight.LF_FIELDS), "BUILD_LF_SCOPE_CHANGED")
    if learn_frontend:
        contract = release_preflight.validate_lf_contract(value["lf_contract"])
        require(baseline is not None and baseline["expected_heads"] == contract["previous_heads"]
                and baseline["images"] == contract["previous_images"], "LF_BASELINE_CHANGED")
    code_only = release_preflight.code_only_phase(value)
    require(release_preflight.code_only_phase(remote) is code_only
            and value.get("learn_code_contract") == remote.get("learn_code_contract"), "BUILD_PHASE_CHANGED")
    if code_only:
        release_preflight.validate_code_contract(value["learn_code_contract"])
        require(baseline is not None and baseline.get("expected_heads", {}).get("learn") == LEARN_CATEGORY_HEAD,
                "LEARN_CODE_BASELINE_HEAD_INVALID")
    images = {}
    rows = remote.get("completed_images")
    require((baseline is not None) is (frontend_only or learn_only or auth_learn_frontend or auth_learn_community_frontend or learn_frontend), "BUILD_BASELINE_REQUIRED")
    required_images = set(release_families(frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend))
    require(isinstance(rows, list) and len(rows) == len(required_images), "BUILD_IMAGES_INCOMPLETE")
    for row in rows:
        service = row.get("service") if isinstance(row, dict) else None
        require(service in required_images and service not in images, "BUILD_IMAGE_SERVICE_INVALID")
        require(row.get("tag") == f"ctf-renewal/{service}:{binding['commit']}-{binding['release_nonce']}"
                and isinstance(row.get("image_id"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"]), "BUILD_IMAGE_IDENTITY_INVALID")
        images[service] = row["image_id"]
    image_bindings = {name: binding for name in images}
    if baseline is not None:
        retained = baseline_summary(baseline, required_images)
        if "retained_baseline" in remote:
            require(remote["retained_baseline"] == retained, "BUILD_RETAINED_BASELINE_CHANGED")
        else:
            require(frontend_only and remote.get("backend_baseline") == {"binding": baseline["binding"], "images": retained["images"]},
                    "BUILD_BACKEND_BASELINE_CHANGED")
        images.update(retained["images"])
        image_bindings.update(retained["image_bindings"])
    before, after = remote.get("live_before", {}), remote.get("live_after", {})
    frontend_hash = before.get("frontend_args_sha256")
    require(isinstance(frontend_hash,str) and re.fullmatch(r"[0-9a-f]{64}",frontend_hash)
            and after.get("frontend_args_sha256") == frontend_hash
            and ("frontend" not in required_images or next(row for row in rows if row["service"] == "frontend").get("frontend_args_sha256") == frontend_hash)
            and ("frontend" in required_images or baseline["frontend_args_sha256"] == frontend_hash),
            "BUILD_FRONTEND_ARGS_HASH_UNVERIFIED")
    protected = file_rows(after.get("protected_files"), absolute=True)
    require(protected and protected == file_rows(before.get("protected_files"), absolute=True), "BUILD_PROTECTED_FILES_CHANGED")
    require(container_rows(before.get("containers")) == container_rows(after.get("containers")), "BUILD_LIVE_CONTAINERS_CHANGED")
    return {"images": images, "image_bindings": image_bindings, "protected_files": protected,"frontend_args_sha256":frontend_hash,
            **({key: value[key] for key in ("release_phase", "learn_code_contract")} if code_only else {}),
            **({key: value[key] for key in release_preflight.ALF_FIELDS} if auth_learn_frontend else {}),
            **({key: value[key] for key in release_preflight.ALCF_FIELDS} if auth_learn_community_frontend else {}),
            **({key: value[key] for key in release_preflight.LF_FIELDS} if learn_frontend else {})}


def activation_contract(value, binding, managed, built, *, baseline=None, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(isinstance(value, dict) and value.get("schema_version") == 1 and value.get("kind") == "renewal-activate"
            and value.get("binding") == binding and value.get("status") == "activated", "ACTIVATION_RECEIPT_UNVERIFIED")
    expected = file_rows(managed)
    require(expected and all(row["path"].startswith(CONTEXTS) for row in expected), "MANAGED_CONTEXT_INVALID")
    frontend_only = baseline is not None and not learn_only and not auth_learn_frontend and not auth_learn_community_frontend and not learn_frontend
    require(value.get("frontend_only", False) is frontend_only, "ACTIVATION_MODE_CHANGED")
    require(value.get("learn_only", False) is learn_only, "ACTIVATION_MODE_CHANGED")
    require(release_preflight.alf_phase(value) is auth_learn_frontend
            and all(value.get(key) == built.get(key) for key in release_preflight.ALF_FIELDS), "ACTIVATION_ALF_SCOPE_CHANGED")
    if auth_learn_frontend:
        scope = release_preflight.validate_alf_contract(value["alf_contract"])
        require(baseline is not None and baseline["expected_heads"] == scope["previous_heads"], "ALF_BASELINE_HEADS_CHANGED")
        require(value.get("executed_migrations") == scope["migration_services"], "ALF_MIGRATIONS_INCOMPLETE")
        require(file_rows(scope["community_source_files"])
                == file_rows([row for row in baseline["managed_files"] if row["path"].startswith("services/community/")]), "ALF_COMMUNITY_SOURCE_CHANGED")
    require(release_preflight.alcf_phase(value) is auth_learn_community_frontend
            and all(value.get(key) == built.get(key) for key in release_preflight.ALCF_FIELDS), "ACTIVATION_ALCF_SCOPE_CHANGED")
    if auth_learn_community_frontend:
        scope = release_preflight.validate_alcf_contract(value["alcf_contract"])
        require(baseline is not None and baseline["expected_heads"] == scope["previous_heads"]
                and baseline["images"] == scope["previous_images"]
                and file_rows(baseline["managed_files"]) == file_rows(scope["previous_source_files"])
                and file_rows([row for row in baseline["unmanaged_files"] if row["path"].startswith(CONTEXTS)])
                == scope["unmanaged_in_target_families"], "ALCF_BASELINE_CHANGED")
        require(value.get("executed_migrations") == scope["migration_services"], "ALCF_MIGRATIONS_INCOMPLETE")
    code_only = release_preflight.code_only_phase(value)
    require(release_preflight.lf_phase(value) is learn_frontend
            and all(value.get(key) == built.get(key) for key in release_preflight.LF_FIELDS), "ACTIVATION_LF_SCOPE_CHANGED")
    if learn_frontend:
        scope = release_preflight.validate_lf_contract(value["lf_contract"])
        require(baseline is not None and baseline["expected_heads"] == scope["previous_heads"]
                and baseline["images"] == scope["previous_images"]
                and file_rows(baseline["managed_files"]) == file_rows(scope["previous_source_files"])
                and value.get("executed_migrations") == ["learn"], "LF_BASELINE_CHANGED")
        require(all(row["path"].startswith(("services/learn/", "frontend/")) for row in expected), "LF_MANAGED_SCOPE_CHANGED")
    require(all(value.get(key) == built.get(key) for key in ("release_phase", "learn_code_contract")), "ACTIVATION_PHASE_CHANGED")
    if code_only:
        require(learn_only and baseline is not None and baseline.get("expected_heads", {}).get("learn") == LEARN_CATEGORY_HEAD,
                "LEARN_CODE_BASELINE_HEAD_INVALID")
        require(value["learn_code_contract"]["previous_learn_image"] == baseline["images"]["learn"]
                and value["learn_code_contract"]["baseline_receipts"] == value.get("baseline_receipts"),
                "LEARN_CODE_BASELINE_CHAIN_CHANGED")
        release_preflight.validate_code_preservation(value, value["learn_code_contract"])
        require(release_preflight.learn_migration_rows(managed)
                == release_preflight.learn_migration_rows(baseline["managed_files"])
                == value["learn_code_contract"]["migration_files"], "LEARN_CODE_MIGRATION_BYTES_CHANGED")
    families = release_families(frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend)
    require(not auth_learn_frontend or all(row["path"].startswith(("services/auth/", "services/learn/", "frontend/")) for row in expected), "ALF_MANAGED_SCOPE_CHANGED")
    require(not frontend_only or all(row["path"].startswith("frontend/") for row in expected), "FRONTEND_ONLY_MANAGED_CONTEXT_INVALID")
    require(not learn_only or baseline is not None and all(row["path"].startswith("services/learn/") for row in expected), "LEARN_ONLY_MANAGED_CONTEXT_INVALID")
    require(file_rows(value.get("managed_files")) == expected, "ACTIVATION_MANAGED_FILES_CHANGED")
    protected = file_rows(value.get("protected_files"), absolute=True)
    require(protected == built["protected_files"], "ACTIVATION_PROTECTED_FILES_CHANGED")
    unmanaged = file_rows(value.get("unmanaged_files"))
    selected_paths = {row["path"] for row in expected}
    require(all(row["path"] not in selected_paths for row in unmanaged), "UNMANAGED_CONTEXT_INVALID")
    if auth_learn_community_frontend:
        require(unmanaged == file_rows(baseline["unmanaged_files"]), "ALCF_UNMANAGED_FILES_CHANGED")
    if baseline is not None:
        prefixes = tuple("frontend/" if name == "frontend" else "services/" + name + "/" for name in families)
        backend_rows = [row for row in baseline["managed_files"] if not row["path"].startswith(prefixes)]
        require((backend_rows or auth_learn_community_frontend) and file_rows([row for row in unmanaged if row["path"] in {item["path"] for item in backend_rows}]) == backend_rows,
                "ACTIVATION_BACKEND_MANAGED_FILES_CHANGED")
        expected = file_rows(expected + backend_rows)
        unmanaged = [row for row in unmanaged if row["path"] not in {item["path"] for item in backend_rows}]
    prior_containers = container_rows(value.get("containers_before"))
    changed_services = sorted(name for name, family in {**SERVICE_IMAGES, **MIGRATION_IMAGES}.items() if family in families)
    if code_only:
        changed_services = sorted(name for name, family in SERVICE_IMAGES.items() if family == "learn")
    if auth_learn_frontend or auth_learn_community_frontend or learn_frontend:
        changed_services = sorted([name for name, family in SERVICE_IMAGES.items() if family in families]
                                 + [name for name, family in MIGRATION_IMAGES.items() if family in scope["migration_services"]])
    result = {"binding": binding, "images": built["images"], "image_bindings": built["image_bindings"],"frontend_args_sha256":built["frontend_args_sha256"],
            "managed_files": expected, "unmanaged_files": unmanaged, "protected_files": protected,
            "containers_before": prior_containers, "changed_services": changed_services}
    if learn_only or auth_learn_frontend or auth_learn_community_frontend or learn_frontend or baseline is not None and "expected_heads" in baseline:
        heads = dict(scope["target_heads"] if auth_learn_frontend or auth_learn_community_frontend or learn_frontend else baseline.get("expected_heads", EXPECTED_HEADS))
        if learn_only:
            heads["learn"] = LEARN_CATEGORY_HEAD
        if frontend_only and heads["learn"] == LEARN_CATEGORY_HEAD:
            require(isinstance(value.get("compatibility_image_backup_receipt_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", value["compatibility_image_backup_receipt_sha256"]), "ACTIVATION_COMPATIBILITY_IMAGE_BACKUP_MISSING")
        require(value.get("schema", {}).get("heads") == {name: [head] for name, head in heads.items()}, "ACTIVATION_SCHEMA_BINDING_CHANGED")
        result["expected_heads"] = heads
        result["category_defaults_only"] = learn_only and not code_only
    if auth_learn_frontend:
        result.update({key: value[key] for key in release_preflight.ALF_FIELDS})
        require(isinstance(value.get("untargeted_runtime_before"), list) and value["untargeted_runtime_before"]
                == value.get("untargeted_runtime_after"), "ALF_UNTARGETED_RUNTIME_CHANGED")
        result["untargeted_runtime"] = value["untargeted_runtime_before"]
    if auth_learn_community_frontend:
        result.update({key: value[key] for key in release_preflight.ALCF_FIELDS})
        require(isinstance(value.get("untargeted_runtime_before"), list) and value["untargeted_runtime_before"]
                == value.get("untargeted_runtime_after"), "ALCF_UNTARGETED_RUNTIME_CHANGED")
        result["untargeted_runtime"] = value["untargeted_runtime_before"]
    if code_only:
        result.update({key: value[key] for key in ("release_phase", "learn_code_contract")})
        result["learn_code_state"] = value["learn_code_before"]
        result["untargeted_runtime"] = value["untargeted_runtime_before"]
    if learn_frontend:
        result.update({key: value[key] for key in release_preflight.LF_FIELDS})
        require(isinstance(value.get("untargeted_runtime_before"), list) and value["untargeted_runtime_before"]
                == value.get("untargeted_runtime_after"), "LF_UNTARGETED_RUNTIME_CHANGED")
        result["untargeted_runtime"] = value["untargeted_runtime_before"]
    return result


def evaluate_snapshot(request, before, after, images, databases, triggers, categories=None, code_states=None, alf_states=None, alcf_states=None, lf_states=None):
    """Compare identities and hashes without returning config, rows or secrets."""
    checks = {}
    def record(name, passed, count):
        checks[name] = {"status": "passed" if passed else "failed", "count": count}
    managed_paths = {row["path"] for row in request["managed_files"]}
    current_code = file_rows(before["code_files"])
    record("managed_files", [row for row in current_code if row["path"] in managed_paths] == request["managed_files"], len(managed_paths))
    record("unmanaged_files", [row for row in current_code if row["path"] not in managed_paths] == request["unmanaged_files"], len(request["unmanaged_files"]))
    record("protected_files", file_rows(before["protected_files"], absolute=True) == request["protected_files"], len(request["protected_files"]))
    before_ids, after_ids = container_rows(before["containers"]), container_rows(after["containers"])
    record("readonly_container_identity", before_ids == after_ids, len(before_ids))
    record("readonly_file_identity", before["code_files"] == after["code_files"] and before["protected_files"] == after["protected_files"], len(current_code))
    changed_services = set(request["changed_services"])
    record("untargeted_containers", [row for row in before_ids if row["service"] not in changed_services]
           == [row for row in request["containers_before"] if row["service"] not in changed_services], len([row for row in before_ids if row["service"] not in changed_services]))
    running = {name: [row for row in before["containers"] if row["service"] == name and row["running"]] for name in SERVICE_IMAGES}
    service_ok = all(len(rows) == 1 and rows[0]["image_id"] == request["images"][SERVICE_IMAGES[name]] for name, rows in running.items())
    record("api_frontend_worker_images", service_ok, len(SERVICE_IMAGES))
    def labels_for(binding):
        return {"org.opencontainers.image.revision": binding["commit"], "io.ctf-web.source-fingerprint": binding["source_fingerprint"],
                "io.ctf-web.release-nonce": binding["release_nonce"]}
    expected_image_labels = {image: labels_for(request["image_bindings"][name]) for name, image in request["images"].items()}
    expected_image_labels[request["images"]["frontend"]][FRONTEND_ARGS_LABEL] = request["frontend_args_sha256"]
    record("image_source_labels", set(images) == set(request["images"].values())
           and all(images[image] == expected_image_labels[image] for image in images), len(images))
    record("container_source_labels", service_ok and all(row.get("source_labels") == labels_for(request["image_bindings"][SERVICE_IMAGES[name]]) for name, rows in running.items() for row in rows), len(SERVICE_IMAGES))
    ready_services = set(SERVICE_IMAGES)-set(LEGACY_PROCESS_WORKERS)
    health_ok = service_ok and all(row.get("health") == "healthy" for name,rows in running.items() if name in ready_services for row in rows)
    record("target_health", health_ok, len(ready_services))
    later = {row["container_id"]:row for row in after["containers"]}
    stable = service_ok
    for name,module in LEGACY_PROCESS_WORKERS.items():
        if len(running[name]) != 1:
            stable = False
            continue
        row = running[name][0]
        other = later.get(row["container_id"],{})
        fields = ("running","pid","started_at","restart_count","command","health")
        stable = stable and row.get("health") is None and type(row.get("pid")) is int and row["pid"] > 0
        stable = stable and isinstance(row.get("started_at"),str) and bool(row["started_at"]) and type(row.get("restart_count")) is int
        stable = stable and row.get("command") == ["python","-m",module] and all(row.get(key) == other.get(key) for key in fields)
    record("legacy_worker_process_stability",stable,len(LEGACY_PROCESS_WORKERS))
    record("readonly_health", [(row["container_id"], row.get("health"), row.get("restart_count")) for row in before["containers"]]
           == [(row["container_id"], row.get("health"), row.get("restart_count")) for row in after["containers"]], len(before_ids))
    expected_heads = request.get("expected_heads", EXPECTED_HEADS)
    record("database_heads", databases == {name: [head] for name, head in expected_heads.items()}, len(expected_heads))
    trigger_ok = isinstance(triggers, list) and len(triggers) == len(TRIGGER_TABLES) and {row.get("table") for row in triggers} == set(TRIGGER_TABLES)
    trigger_ok = trigger_ok and all(row.get("enabled") in {"O", "A"} and row.get("function") == "bump_knowledge_graph_revision" and row.get("type") == 28 for row in triggers)
    record("learn_graph_revision_triggers", trigger_ok, len(triggers) if isinstance(triggers, list) else 0)
    alf = request.get("auth_learn_frontend") is True
    alcf = request.get("auth_learn_community_frontend") is True
    lf = request.get("learn_frontend") is True
    if lf or expected_heads["learn"] == LEARN_CATEGORY_HEAD or alf and "20261013_029" in request["alf_contract"]["migration_graph"]["learn"]["revisions"] or alcf and "20261013_029" in request["alcf_contract"]["migration_graph"]["learn"]["revisions"]:
        category_ok = isinstance(categories, dict) and set(categories) == {"registry_present", "audit_present", "registry_fk_present", "legacy_check_absent", "custom_category_count", "custom_event_count"}
        category_ok = category_ok and all(categories[name] is True for name in ("registry_present", "audit_present", "registry_fk_present", "legacy_check_absent"))
        category_ok = category_ok and all(type(categories[name]) is int and categories[name] >= 0 for name in ("custom_category_count", "custom_event_count"))
        if category_ok and request.get("category_defaults_only"):
            category_ok = categories["custom_category_count"] == 0 and categories["custom_event_count"] == 0
        record("event_category_registry", category_ok, categories.get("custom_category_count", 0) if isinstance(categories, dict) else 0)
    if request.get("release_phase") == "learn-code-only-029":
        valid = isinstance(code_states, list) and len(code_states) == 2 and all(row == request["learn_code_state"] for row in code_states)
        record("learn_code_preservation", valid, 2)
        record("learn_code_untargeted_runtime", code_runtime(before, changed_services) == code_runtime(after, changed_services)
               == request["untargeted_runtime"], len(request["untargeted_runtime"]))
    if alf:
        record("alf_untargeted_runtime", code_runtime(before, changed_services) == code_runtime(after, changed_services)
               == request["untargeted_runtime"], len(request["untargeted_runtime"]))
        expected_schema = {"community_schema_sha256": request["alf_contract"]["community_schema_sha256"],
                           "reason_nullable": True if "20261018_030" in request["alf_contract"]["migration_graph"]["learn"]["revisions"] else None}
        record("alf_schema_preservation", isinstance(alf_states, list) and alf_states == [expected_schema, expected_schema], 2)
    if alcf:
        record("alcf_untargeted_runtime", code_runtime(before, changed_services) == code_runtime(after, changed_services)
               == request["untargeted_runtime"], len(request["untargeted_runtime"]))
        expected_schema = {"community_schema_sha256": request["alcf_contract"]["community_schema_sha256"]
                           if request["alcf_contract"]["previous_heads"]["community"] == expected_heads["community"] else None,
                           "reason_nullable": True, "team_schema": True, "attachment_schema": True}
        record("alcf_schema_preservation", isinstance(alcf_states, list) and alcf_states == [expected_schema, expected_schema], 2)
    if lf:
        retained_targets = changed_services - set(MIGRATION_IMAGES)
        record("lf_untargeted_runtime", code_runtime(before, retained_targets) == code_runtime(after, retained_targets)
               == request["untargeted_runtime"], len(request["untargeted_runtime"]))
        expected_schema = {"auth_schema_sha256": request["lf_contract"]["auth_schema_sha256"],
                           "community_schema_sha256": request["lf_contract"]["community_schema_sha256"], "tag_schema": LF_TAG_SCHEMA_EXPECTED}
        schema_ok = isinstance(lf_states, list) and lf_states == [expected_schema, expected_schema]
        schema_ok = schema_ok and all(value is True for state in lf_states for value in state["tag_schema"].values())
        record("lf_schema_preservation", schema_ok, 2)
    return {"schema_version": 1, "kind": "renewal-verify", "binding": request["binding"],
            "status": "passed" if all(row["status"] == "passed" for row in checks.values()) else "failed",
            "checks": checks, "unverified_fields": list(UNVERIFIED), "read_only": True, "remote_write_performed": False}


REMOTE = r'''
def verify_snapshot():
    model = compose()
    protected = protected_rows(model)
    current = containers()
    details = {row['Id']:row.get('Config',{}) for row in json.loads(run(['docker','inspect',*[row['container_id'] for row in current]]))}
    labels = {identifier:(config.get('Labels',{}) or {}) for identifier,config in details.items()}
    for row in current:
        row['source_labels'] = {key:labels[row['container_id']].get(key) for key in ['org.opencontainers.image.revision','io.ctf-web.source-fingerprint','io.ctf-web.release-nonce']}
        if row['service'] in LEGACY_PROCESS_WORKERS: row['command']=details[row['container_id']].get('Cmd')
    return {'containers':current, 'protected_files':protected, 'code_files':code_rows(protected)}

def verify_main():
    try:
        before = verify_snapshot()
        postgres = singleton(before['containers'], 'postgres')['container_id']
        shell = 'export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=1000"; exec psql -U "$POSTGRES_USER" -d "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"'
        def sql(database, query):
            return json.loads(run(['docker','exec',postgres,'sh','-c',shell,'renewal-verify',database,query], limit=30))
        def heads():
            return {name:sql(database, 'SELECT coalesce(json_agg(version_num ORDER BY version_num),\'[]\'::json) FROM public.alembic_version') for name,database in DATABASES.items()}
        def graph_triggers():
            return sql('learn_db', "SELECT coalesce(json_agg(json_build_object('table',c.relname,'enabled',t.tgenabled,'function',p.proname,'type',t.tgtype) ORDER BY c.relname),'[]'::json) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid WHERE NOT t.tgisinternal AND n.nspname='public' AND t.tgname='knowledge_graph_revision_write'")
        databases, triggers = heads(), graph_triggers()
        alf=REQUEST.get('auth_learn_frontend') is True
        alcf=REQUEST.get('auth_learn_community_frontend') is True
        lf=REQUEST.get('learn_frontend') is True
        def lf_state():
            schemas=database_rows(postgres)
            return {'auth_schema_sha256':schemas['auth']['schema_sha256'],'community_schema_sha256':schemas['community']['schema_sha256'],
                    'tag_schema':sql('learn_db',LF_TAG_SCHEMA_SQL)}
        lf_states=[lf_state()] if lf else None
        def alf_state():
            reason=None
            if '20261018_030' in REQUEST['alf_contract']['migration_graph']['learn']['revisions']:
                reason=sql('learn_db',"SELECT count(*)=1 FROM information_schema.columns WHERE table_schema='public' AND table_name='submission_flag_reveal_audits' AND column_name='reason' AND data_type='text' AND is_nullable='YES'")
            return {'community_schema_sha256':database_rows(postgres)['community']['schema_sha256'],'reason_nullable':reason}
        alf_states=[alf_state()] if alf else None
        def alcf_state():
            contract=REQUEST['alcf_contract']
            return {'community_schema_sha256':database_rows(postgres)['community']['schema_sha256'] if contract['previous_heads']['community']==contract['target_heads']['community'] else None,
                'reason_nullable':sql('learn_db',"SELECT count(*)=1 FROM information_schema.columns WHERE table_schema='public' AND table_name='submission_flag_reveal_audits' AND column_name='reason' AND data_type='text' AND is_nullable='YES'"),
                'team_schema':sql('platform_db',ALCF_TEAM_SCHEMA_SQL),'attachment_schema':sql('learn_db',ALCF_ATTACHMENT_SCHEMA_SQL)}
        alcf_states=[alcf_state()] if alcf else None
        code_states = None
        if REQUEST.get('release_phase')=='learn-code-only-029':
            state=sql('learn_db',LEARN_CODE_STATE_SQL)
            state['schema_sha256']=database_rows(postgres)['learn']['schema_sha256']
            code_states=[validate_code_state(state)]
        categories = None
        if lf or REQUEST.get('expected_heads',EXPECTED_HEADS)['learn']==LEARN_CATEGORY_HEAD or alf and '20261013_029' in REQUEST['alf_contract']['migration_graph']['learn']['revisions'] or alcf and '20261013_029' in REQUEST['alcf_contract']['migration_graph']['learn']['revisions']:
            categories=sql('learn_db', "SELECT json_build_object('registry_present',to_regclass('public.platform_event_categories') IS NOT NULL,'audit_present',to_regclass('public.platform_event_category_audit_events') IS NOT NULL,'registry_fk_present',EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='public.platform_events'::regclass AND conname='fk_platform_events_category_registry' AND contype='f' AND convalidated),'legacy_check_absent',NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='public.platform_events'::regclass AND conname='ck_platform_events_category'),'custom_category_count',(SELECT count(*) FROM platform_event_categories WHERE key NOT IN ('workshop','seminar','study','networking','notice','other')),'custom_event_count',(SELECT count(*) FROM platform_events WHERE category NOT IN ('workshop','seminar','study','networking','notice','other')))")
        images = {}
        for image in sorted(set(REQUEST['images'].values())):
            rows = json.loads(run(['docker','image','inspect',image]))
            require(len(rows) == 1 and rows[0]['Id'] == image, 'IMAGE_INSPECTION_IDENTITY_CHANGED')
            labels = rows[0].get('Config',{}).get('Labels',{}) or {}
            images[image] = {key:labels.get(key) for key in ['org.opencontainers.image.revision','io.ctf-web.source-fingerprint','io.ctf-web.release-nonce']}
            if image==REQUEST['images']['frontend']: images[image][FRONTEND_ARGS_LABEL]=labels.get(FRONTEND_ARGS_LABEL)
        import time
        time.sleep(2)
        after = verify_snapshot()
        require(databases == heads() and triggers == graph_triggers(), 'DATABASE_SCHEMA_CHANGED_DURING_VERIFY')
        if code_states is not None:
            state=sql('learn_db',LEARN_CODE_STATE_SQL)
            state['schema_sha256']=database_rows(postgres)['learn']['schema_sha256']
            code_states.append(validate_code_state(state))
        if alf_states is not None: alf_states.append(alf_state())
        if alcf_states is not None: alcf_states.append(alcf_state())
        if lf_states is not None: lf_states.append(lf_state())
        print(json.dumps(evaluate_snapshot(REQUEST,before,after,images,databases,triggers,categories,code_states,alf_states,alcf_states,lf_states)))
    except Exception:
        print(json.dumps({'schema_version':1,'kind':'renewal-verify','binding':REQUEST['binding'],'status':'unverified','checks':{},'unverified_fields':['remote_read_failed',*UNVERIFIED],'read_only':True,'remote_write_performed':False}))
verify_main()
'''


def remote_program():
    functions = (require, canonical, sha, file_rows, container_rows, release_preflight.validate_code_state,
                 release_preflight.code_runtime, evaluate_snapshot)
    constants = {"CONTEXTS": CONTEXTS, "SERVICE_IMAGES": SERVICE_IMAGES, "MIGRATION_IMAGES": MIGRATION_IMAGES,
                 "EXPECTED_HEADS": EXPECTED_HEADS, "LEARN_CATEGORY_HEAD":LEARN_CATEGORY_HEAD, "TRIGGER_TABLES": TRIGGER_TABLES, "UNVERIFIED": UNVERIFIED,"FRONTEND_ARGS_LABEL":FRONTEND_ARGS_LABEL,
                  "LEGACY_PROCESS_WORKERS":LEGACY_PROCESS_WORKERS, "LEARN_CODE_STATE_SQL":release_preflight.LEARN_CODE_STATE_SQL,
                  "ALCF_TEAM_SCHEMA_SQL": ALCF_TEAM_SCHEMA_SQL, "ALCF_ATTACHMENT_SCHEMA_SQL": ALCF_ATTACHMENT_SCHEMA_SQL,
                  "LF_TAG_SCHEMA_SQL": LF_TAG_SCHEMA_SQL, "LF_TAG_SCHEMA_EXPECTED": LF_TAG_SCHEMA_EXPECTED}
    return (release_preflight.REMOTE_HELPERS + "\nfrom pathlib import PurePosixPath\nclass VerifyError(RuntimeError): pass\n"
            + "\n".join(name + "=" + repr(value) for name, value in constants.items()) + "\n"
            + "\n".join(inspect.getsource(function) for function in functions) + REMOTE)


def execution_payload(request):
    payload = canonical({**request, "program": base64.b64encode(remote_program().encode()).decode()}) + b"\n"
    require(len(payload) <= 16 * 1024 * 1024, "VERIFICATION_REQUEST_TOO_LARGE")
    bootstrap = "import base64,json,sys;request=json.loads(sys.stdin.buffer.readline(16777217));program=base64.b64decode(request.pop('program'));exec(compile(program,'<renewal-verify>','exec'),{'REQUEST':request})"
    return "python3 -c " + shlex.quote(bootstrap), payload


def load_receipt(path):
    path = Path(path).absolute()
    require(path.resolve().is_relative_to((ops.TASK / "evidence").resolve()) and path.is_file()
            and not any(ops.linked(parent) for parent in (path, *path.parents) if parent.is_relative_to(ops.TASK)), "INPUT_RECEIPT_PATH_INVALID")
    require(path.stat().st_size <= 16 * 1024 * 1024, "INPUT_RECEIPT_TOO_LARGE")
    payload = path.read_bytes()
    return json.loads(payload), sha(payload)


def validate_result(value, binding, *, expected_heads=None, learn_code_only=False, auth_learn_frontend=False, alf_contract=None, auth_learn_community_frontend=False, alcf_contract=None, learn_frontend=False, lf_contract=None):
    require(isinstance(value, dict) and set(value) == {"schema_version", "kind", "binding", "status", "checks", "unverified_fields", "read_only", "remote_write_performed"}
            and value["schema_version"] == 1 and value["kind"] == "renewal-verify" and value["binding"] == binding
            and value["read_only"] is True and value["remote_write_performed"] is False
            and value["status"] in {"passed", "failed", "unverified"}, "REMOTE_VERIFICATION_FIELDS_INVALID")
    names = {"managed_files", "unmanaged_files", "protected_files", "readonly_container_identity", "readonly_file_identity", "untargeted_containers",
             "api_frontend_worker_images", "image_source_labels", "container_source_labels", "target_health", "readonly_health", "database_heads", "learn_graph_revision_triggers","legacy_worker_process_stability"}
    if learn_code_only:
        names |= {"learn_code_preservation", "learn_code_untargeted_runtime"}
    if auth_learn_frontend:
        release_preflight.validate_alf_contract(alf_contract)
        names |= {"alf_untargeted_runtime", "alf_schema_preservation"}
    if auth_learn_community_frontend:
        release_preflight.validate_alcf_contract(alcf_contract)
        names |= {"alcf_untargeted_runtime", "alcf_schema_preservation"}
    if learn_frontend:
        release_preflight.validate_lf_contract(lf_contract)
        names |= {"lf_untargeted_runtime", "lf_schema_preservation"}
    checks = value["checks"]
    require(isinstance(checks, dict) and set(checks) <= names | {"event_category_registry"}
            and (value["status"] == "unverified" or set(checks) in (names, names | {"event_category_registry"})), "REMOTE_CHECKS_INCOMPLETE")
    if (learn_frontend or expected_heads and expected_heads.get("learn") == LEARN_CATEGORY_HEAD or auth_learn_frontend and "20261013_029" in alf_contract["migration_graph"]["learn"]["revisions"] or auth_learn_community_frontend and "20261013_029" in alcf_contract["migration_graph"]["learn"]["revisions"]) and value["status"] != "unverified":
        require("event_category_registry" in checks, "REMOTE_EVENT_CATEGORY_CHECK_MISSING")
    for row in checks.values():
        require(isinstance(row, dict) and set(row) == {"status", "count"} and row["status"] in {"passed", "failed"}
                and type(row["count"]) is int and 0 <= row["count"] <= 100000, "REMOTE_CHECK_RESULT_INVALID")
    require(isinstance(value["unverified_fields"], list) and set(value["unverified_fields"]) in (set(UNVERIFIED), {"remote_read_failed", *UNVERIFIED}), "REMOTE_UNVERIFIED_FIELDS_INVALID")
    require(value["status"] != "passed" or all(row["status"] == "passed" for row in checks.values()), "REMOTE_FALSE_PASS")
    return value


def baseline_verify_contract(value, binding, build_sha, activation_sha, *, frontend_only=False, learn_only=False, parent_hashes=None, learn_code_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    fields = {"schema_version", "kind", "binding", "status", "checks", "unverified_fields", "read_only", "remote_write_performed"}
    metadata = {"local_source_unchanged", "recorded_at", "build_receipt_sha256", "activation_receipt_sha256", "frontend_only", "secret_values_recorded"}
    extra = {"baseline_activation_receipt_sha256", "baseline_build_receipt_sha256", "baseline_verify_receipt_sha256"} if frontend_only and parent_hashes is None else set()
    if parent_hashes is not None:
        extra = {"baseline_receipts", "learn_only"}
    if learn_code_only:
        extra |= {"release_phase"}
    if auth_learn_frontend:
        extra |= set(release_preflight.ALF_FIELDS)
    if auth_learn_community_frontend:
        extra |= set(release_preflight.ALCF_FIELDS)
    if learn_frontend:
        extra |= set(release_preflight.LF_FIELDS)
    require(isinstance(value, dict) and set(value) == fields | metadata | extra, "BASELINE_VERIFY_FIELDS_INVALID")
    require(value.get("release_phase") == (release_preflight.LF_PHASE if learn_frontend else release_preflight.ALCF_PHASE if auth_learn_community_frontend else release_preflight.ALF_PHASE if auth_learn_frontend else release_preflight.LEARN_CODE_PHASE if learn_code_only else None), "BASELINE_VERIFY_PHASE_CHANGED")
    if auth_learn_frontend:
        require(release_preflight.alf_phase(value), "ALF_VERIFY_SCOPE_CHANGED")
        release_preflight.validate_alf_contract(value["alf_contract"])
    if auth_learn_community_frontend:
        require(release_preflight.alcf_phase(value), "ALCF_VERIFY_SCOPE_CHANGED")
    if learn_frontend:
        require(release_preflight.lf_phase(value), "LF_VERIFY_SCOPE_CHANGED")
    validate_result({key: value[key] for key in fields}, binding, learn_code_only=learn_code_only, auth_learn_frontend=auth_learn_frontend, alf_contract=value.get("alf_contract"), auth_learn_community_frontend=auth_learn_community_frontend, alcf_contract=value.get("alcf_contract"), learn_frontend=learn_frontend, lf_contract=value.get("lf_contract"))
    require(value["status"] == "passed" and value["frontend_only"] is frontend_only and value.get("learn_only", False) is learn_only and value["local_source_unchanged"] is True
            and value["secret_values_recorded"] is False and value["build_receipt_sha256"] == build_sha
            and value["activation_receipt_sha256"] == activation_sha, "BASELINE_VERIFY_NOT_PASSED_OR_UNBOUND")
    require(isinstance(value["recorded_at"], str) and datetime.fromisoformat(value["recorded_at"]).tzinfo is not None,
            "BASELINE_VERIFY_TIMESTAMP_INVALID")
    if parent_hashes is not None:
        require(value["baseline_receipts"] == parent_hashes, "BASELINE_CHAIN_PARENT_HASH_CHANGED")


def baseline_inputs(args):
    legacy = (args.baseline_activation_receipt, args.baseline_build_receipt, args.baseline_verify_receipt)
    chain = getattr(args, "baseline_release", None) or []
    scoped = args.frontend_only or getattr(args, "learn_only", False) or getattr(args, "auth_learn_frontend", False) or getattr(args, "auth_learn_community_frontend", False) or getattr(args, "learn_frontend", False)
    require(not chain or all(path is None for path in legacy), "BASELINE_INPUT_STYLES_CONFLICT")
    require((bool(chain) or all(path is not None for path in legacy)) if scoped else not chain and all(path is None for path in legacy),
            "BASELINE_INPUTS_INVALID")
    return chain or ([legacy] if scoped else [])


def load_baseline_chain(paths):
    require(isinstance(paths, (list, tuple)) and 0 < len(paths) <= 20, "BASELINE_CHAIN_INVALID")
    baseline = None
    hashes = []
    first = None
    backup_anchor = None
    trusted_heads = None
    for index, triple in enumerate(paths):
        require(len(triple) == 3, "BASELINE_CHAIN_INPUT_INVALID")
        activation, activation_sha = load_receipt(triple[0])
        built, build_sha = load_receipt(triple[1])
        checked, checked_sha = load_receipt(triple[2])
        old = activation.get("binding", {})
        binding = binding_for(old.get("commit"), old.get("source_fingerprint"), old.get("release_nonce"))
        frontend_only = activation.get("frontend_only", False)
        learn_only = activation.get("learn_only", False)
        alf = release_preflight.alf_phase(activation)
        alcf = release_preflight.alcf_phase(activation)
        lf = release_preflight.lf_phase(activation)
        code_only = release_preflight.code_only_phase(activation)
        require(not code_only or index > 0 and activation["learn_code_contract"]["baseline_receipts"] == hashes,
                "LEARN_CODE_BASELINE_CHAIN_CHANGED")
        require(not alf or index > 0 and activation["alf_contract"]["baseline_receipts"] == hashes, "ALF_BASELINE_CHAIN_CHANGED")
        require(not alcf or index > 0 and activation["alcf_contract"]["baseline_receipts"] == hashes, "ALCF_BASELINE_CHAIN_CHANGED")
        require(not lf or index > 0 and activation["lf_contract"]["baseline_receipts"] == hashes, "LF_BASELINE_CHAIN_CHANGED")
        require(index > 0 or not (frontend_only or learn_only or alf or alcf or lf), "BASELINE_CHAIN_MUST_START_FULL")
        require(index == 0 or frontend_only or learn_only or alf or alcf or lf, "BASELINE_CHAIN_FULL_RESTART_FORBIDDEN")
        if index > 0 and "baseline_receipts" not in checked:
            require(index == 1 and frontend_only and not learn_only, "LEGACY_BASELINE_CHAIN_UNBOUND")
            require({name: checked.get("baseline_" + name + "_receipt_sha256") for name in ("activation", "build", "verify")}
                    == hashes[-1], "BASELINE_CHAIN_PARENT_HASH_CHANGED")
            baseline_verify_contract(checked, binding, build_sha, activation_sha, frontend_only=True)
        else:
            baseline_verify_contract(checked, binding, build_sha, activation_sha, frontend_only=frontend_only,
                                     learn_only=learn_only, parent_hashes=hashes if index else None, learn_code_only=code_only, auth_learn_frontend=alf, auth_learn_community_frontend=alcf, learn_frontend=lf)
        require(not alf or checked["alf_contract"] == activation["alf_contract"], "ALF_VERIFY_CONTRACT_CHANGED")
        require(not alcf or checked["alcf_contract"] == activation["alcf_contract"], "ALCF_VERIFY_CONTRACT_CHANGED")
        require(not lf or checked["lf_contract"] == activation["lf_contract"], "LF_VERIFY_CONTRACT_CHANGED")
        contract = build_contract(built, binding, frontend_only=frontend_only, learn_only=learn_only, baseline=baseline, auth_learn_frontend=alf, auth_learn_community_frontend=alcf, learn_frontend=lf)
        baseline = activation_contract(activation, binding, activation.get("managed_files"), contract, baseline=baseline, learn_only=learn_only, auth_learn_frontend=alf, auth_learn_community_frontend=alcf, learn_frontend=lf)
        schema_heads = activation.get("schema", {}).get("heads")
        require(isinstance(schema_heads, dict) and set(schema_heads) == set(EXPECTED_HEADS)
                and all(isinstance(row, list) and len(row) == 1 for row in schema_heads.values()), "BASELINE_SCHEMA_UNBOUND")
        heads = {name: row[0] for name, row in schema_heads.items()}
        require(heads == activation["lf_contract"]["target_heads"] if lf else heads == activation["alcf_contract"]["target_heads"] if alcf else heads == activation["alf_contract"]["target_heads"] if alf else heads == trusted_heads or heads in (EXPECTED_HEADS, {**EXPECTED_HEADS, "learn": LEARN_CATEGORY_HEAD}), "BASELINE_SCHEMA_UNSUPPORTED")
        require(heads["learn"] != LEARN_CATEGORY_HEAD or checked["checks"].get("event_category_registry", {}).get("status") == "passed",
                "BASELINE_EVENT_CATEGORY_CHECK_MISSING")
        baseline["expected_heads"] = heads
        trusted_heads = heads
        hashes.append({"activation": activation_sha, "build": build_sha, "verify": checked_sha})
        entry = {"activation": activation, "build": built, "contract": baseline}
        if first is None:
            first = entry
        if index == 0 or learn_only or alf or alcf or lf:
            backup_anchor = entry
    return {"contract": baseline, "activation": activation, "build": built, "first": first, "backup_anchor": backup_anchor, "hashes": hashes}


def retained_source_contract(baseline, manifest_rows, *, frontend_only=False, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    families = release_families(frontend_only=frontend_only, learn_only=learn_only, auth_learn_frontend=auth_learn_frontend, auth_learn_community_frontend=auth_learn_community_frontend, learn_frontend=learn_frontend)
    require(frontend_only or learn_only or auth_learn_frontend or auth_learn_community_frontend or learn_frontend, "RETAINED_SOURCE_SCOPE_REQUIRED")
    # A Learn compatibility release deliberately leaves the pending frontend
    # source unapplied. Auth/Community must still match the verified baseline.
    unchanged = set() if auth_learn_community_frontend else {"community"} if auth_learn_frontend else {"auth", "community"} if learn_only or learn_frontend else {"auth", "learn", "community"}
    prefixes = tuple("services/" + name + "/" for name in unchanged)
    require(file_rows([row for row in manifest_rows if row["path"].startswith(prefixes)])
            == file_rows([row for row in baseline["managed_files"] if row["path"].startswith(prefixes)]), "RETAINED_BACKEND_SOURCE_CHANGED")
    excluded = tuple("frontend/" if name == "frontend" else "services/" + name + "/" for name in families)
    return [row for row in baseline["managed_files"] if not row["path"].startswith(excluded)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--source-fingerprint", required=True)
    parser.add_argument("--release-nonce", required=True)
    parser.add_argument("--build-receipt", type=Path, required=True)
    parser.add_argument("--activation-receipt", type=Path, required=True)
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
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    require(not args.learn_code_only or args.learn_only, "LEARN_CODE_SCOPE_INVALID")
    binding = binding_for(args.commit, args.source_fingerprint, args.release_nonce)
    inputs = baseline_inputs(args)
    manifest = ops.manifest()
    require(manifest["fingerprint"] == binding["source_fingerprint"], "CURRENT_SOURCE_CHANGED")
    all_managed = file_rows([row for row in manifest["files"] if row["path"].startswith(CONTEXTS)])
    prefix = "frontend/" if args.frontend_only else "services/learn/" if args.learn_only else None
    managed = [row for row in all_managed if row["path"].startswith(("frontend/", "services/learn/"))] if args.learn_frontend else [row for row in all_managed if row["path"].startswith(("frontend/", "services/auth/", "services/learn/"))] if args.auth_learn_frontend else [row for row in all_managed if row["path"].startswith(prefix)] if prefix else all_managed
    build, build_sha = load_receipt(args.build_receipt)
    activation, activation_sha = load_receipt(args.activation_receipt)
    require(release_preflight.code_only_phase(activation) is args.learn_code_only, "VERIFY_PHASE_CHANGED")
    require(release_preflight.alf_phase(activation) is args.auth_learn_frontend, "VERIFY_PHASE_CHANGED")
    require(release_preflight.alcf_phase(activation) is args.auth_learn_community_frontend, "VERIFY_PHASE_CHANGED")
    require(release_preflight.lf_phase(activation) is args.learn_frontend, "VERIFY_PHASE_CHANGED")
    baseline = None
    baseline_hashes = {}
    if inputs:
        chain = load_baseline_chain(inputs)
        baseline = chain["contract"]
        retained_source_contract(baseline, all_managed, frontend_only=args.frontend_only, learn_only=args.learn_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)
        if args.baseline_release or args.learn_only or args.auth_learn_frontend or args.auth_learn_community_frontend or args.learn_frontend:
            baseline_hashes = {"baseline_receipts": chain["hashes"], "learn_only": args.learn_only}
            require(activation.get("baseline_receipts") == chain["hashes"], "ACTIVATION_BASELINE_CHAIN_CHANGED")
        else:
            require(baseline["expected_heads"] == EXPECTED_HEADS, "CATEGORY_029_EXPLICIT_BASELINE_CHAIN_REQUIRED")
            baseline = {key: value for key, value in baseline.items() if key not in {"expected_heads", "category_defaults_only"}}
            baseline_hashes = {"baseline_" + name + "_receipt_sha256": value for name, value in chain["hashes"][0].items()}
    request = activation_contract(activation, binding, managed, build_contract(build, binding, frontend_only=args.frontend_only,
                                  learn_only=args.learn_only, baseline=baseline, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend), baseline=baseline, learn_only=args.learn_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)
    if not inputs and "schema" in activation:
        heads = {name: row[0] for name, row in activation["schema"]["heads"].items() if isinstance(row, list) and len(row) == 1}
        require(heads in (EXPECTED_HEADS, {**EXPECTED_HEADS, "learn": LEARN_CATEGORY_HEAD}), "ACTIVATION_SCHEMA_UNSUPPORTED")
        request["expected_heads"] = heads
    plan = {"schema_version": 1, "kind": "renewal-verify-plan", "binding": binding, "execute": args.execute,
            "managed_file_count": len(managed), "unmanaged_file_count": len(request["unmanaged_files"]),
            "build_receipt_sha256": build_sha, "activation_receipt_sha256": activation_sha, "frontend_only": args.frontend_only, **baseline_hashes,
            "read_only": True, "remote_write_performed": False, "unverified_fields": ["remote_state_not_read", *UNVERIFIED]}
    if args.learn_code_only:
        plan["release_phase"] = release_preflight.LEARN_CODE_PHASE
    if args.auth_learn_frontend:
        plan.update({key: activation[key] for key in release_preflight.ALF_FIELDS})
    if args.auth_learn_community_frontend:
        plan.update({key: activation[key] for key in release_preflight.ALCF_FIELDS})
    if args.learn_frontend:
        plan.update({key: activation[key] for key in release_preflight.LF_FIELDS})
    if not args.execute:
        print(json.dumps(plan))
        return 0
    require(ops.manifest()["fingerprint"] == binding["source_fingerprint"], "SOURCE_CHANGED_BEFORE_VERIFICATION")
    command, payload = execution_payload(request)
    try:
        result = validate_result(json.loads(ops.ssh(command, timeout=600, stdin=payload)), binding, expected_heads=request.get("expected_heads"), learn_code_only=args.learn_code_only, auth_learn_frontend=args.auth_learn_frontend, alf_contract=request.get("alf_contract"), auth_learn_community_frontend=args.auth_learn_community_frontend, alcf_contract=request.get("alcf_contract"), learn_frontend=args.learn_frontend, lf_contract=request.get("lf_contract"))
    except Exception:
        result = {"schema_version": 1, "kind": "renewal-verify", "binding": binding, "status": "unverified", "checks": {},
                  "unverified_fields": ["remote_read_failed", *UNVERIFIED], "read_only": True, "remote_write_performed": False}
    result["local_source_unchanged"] = ops.manifest()["fingerprint"] == binding["source_fingerprint"]
    if not result["local_source_unchanged"]:
        result["status"] = "unverified"
        result["unverified_fields"].append("source_changed_during_verification")
    result.update({"recorded_at": datetime.now(timezone.utc).isoformat(), "build_receipt_sha256": build_sha,
                   "activation_receipt_sha256": activation_sha, "frontend_only": args.frontend_only, **baseline_hashes, "secret_values_recorded": False})
    if args.learn_code_only:
        result["release_phase"] = release_preflight.LEARN_CODE_PHASE
    if args.auth_learn_frontend:
        result.update({key: activation[key] for key in release_preflight.ALF_FIELDS})
    if args.auth_learn_community_frontend:
        result.update({key: activation[key] for key in release_preflight.ALCF_FIELDS})
    if args.learn_frontend:
        result.update({key: activation[key] for key in release_preflight.LF_FIELDS})
    receipt = ops.receipt(f"verify-{binding['release_nonce']}-{uuid.uuid4().hex[:12]}.json", result)
    print(json.dumps({"receipt": str(receipt), "status": result["status"], "checks": result["checks"],
                      "unverified_fields": result["unverified_fields"], "secret_values_printed": False}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"status": "unverified", "error": str(error) if isinstance(error, VerifyError) else "LOCAL_VERIFICATION_FAILED", "secret_values_printed": False}))
        sys.exit(1)
