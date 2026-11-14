"""Read-only, source-bound release planning. Never print remote diagnostics.

An accepted plan proves the enumerated read-only contracts, not a production
upgrade or a cross-database/MinIO point-in-time snapshot. No September receipt
is an input. All newly written plans/receipts are exclusive and task scoped.
"""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from uuid import UUID, uuid4

import ops

TASK_ID = "2026-10-13-renewal"
DATABASES = {"auth": "platform_db", "learn": "learn_db", "community": "community_db"}
PROJECT = "ctf_web"
SSH_PORT = 10022
HASH = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[A-Za-z0-9_]{1,100}$")
MAX_PLAN_AGE = timedelta(minutes=30)
LEARN_CODE_PHASE = "learn-code-only-029"


class ReleaseError(RuntimeError):
    pass


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require(condition, code):
    if not condition:
        raise ReleaseError(code)


def parse_json(payload: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        return json.loads(payload, object_pairs_hook=unique)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("INVALID_JSON") from exc


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "TIMESTAMP_TIMEZONE_REQUIRED")
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReleaseError("INVALID_TIMESTAMP") from exc


def task_path(path: Path, base: Path) -> Path:
    path, base = Path(path).absolute(), Path(base).absolute()
    require(path.resolve().is_relative_to(base.resolve()) and path != base, "TASK_PATH_ESCAPE")
    for parent in (path, *path.parents):
        if parent.is_relative_to(ops.SOURCE):
            require(not ops.linked(parent), "TASK_PATH_LINK_FORBIDDEN")
    return path


def exclusive_json(path: Path, value) -> str:
    payload = canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
    return sha256(payload)


def source_migrations(source: Path = ops.SOURCE):
    result = {}
    for service in DATABASES:
        versions = source / "services" / service / "migrations" / "versions"
        require(all(not ops.linked(path) for path in (versions, *versions.parents) if path.is_relative_to(source)), "MIGRATION_DIRECTORY_LINK_FORBIDDEN")
        revisions = {}
        for file in sorted(versions.glob("*.py")):
            require(not ops.linked(file), "MIGRATION_LINK_FORBIDDEN")
            values = {}
            for node in ast.parse(file.read_text(encoding="utf-8-sig")).body:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                        try:
                            values[target.id] = ast.literal_eval(node.value)
                        except (ValueError, TypeError) as exc:
                            raise ReleaseError("MIGRATION_REVISION_NOT_LITERAL") from exc
            if "revision" not in values:
                continue
            revision = values["revision"]
            require(isinstance(revision, str) and REVISION.fullmatch(revision), "MIGRATION_REVISION_INVALID")
            require(revision not in revisions, "MIGRATION_REVISION_DUPLICATE")
            parent = values.get("down_revision")
            require(parent is None or isinstance(parent, str), "MIGRATION_BRANCH_UNSUPPORTED")
            revisions[revision] = parent
        require(bool(revisions), "MIGRATION_GRAPH_MISSING")
        require(all(parent is None or parent in revisions for parent in revisions.values()), "MIGRATION_PARENT_MISSING")
        heads = set(revisions) - {parent for parent in revisions.values() if parent}
        require(len(heads) == 1, "MIGRATION_HEAD_AMBIGUOUS")
        head = next(iter(heads))
        visited, cursor = set(), head
        while cursor is not None:
            require(cursor not in visited, "MIGRATION_GRAPH_CYCLE")
            visited.add(cursor)
            cursor = revisions[cursor]
        require(visited == set(revisions), "MIGRATION_GRAPH_DISCONNECTED")
        result[service] = {"head": head, "revisions": sorted(revisions)}
    return result


def learn_migration_rows(rows):
    exact = {"services/learn/alembic.ini", "services/learn/app/scripts/learn_migrate.py",
             "services/learn/app/config.py", "services/learn/app/database.py",
             "services/learn/app/utils/learn_schema_bootstrap_contract.py"}
    selected = sorted((row for row in rows if row["path"].startswith("services/learn/migrations/")
                       or row["path"] in exact), key=lambda row: row["path"])
    require(exact <= {row["path"] for row in selected}
            and any(row["path"].startswith("services/learn/migrations/versions/") for row in selected),
            "LEARN_CODE_MIGRATION_FILES_MISSING")
    return selected


ALF_PHASE = "auth-learn-frontend-v1"
ALF_FIELDS = ("auth_learn_frontend", "release_phase", "alf_contract")
ALCF_PHASE = "auth-learn-community-frontend-v1"
ALCF_FIELDS = ("auth_learn_community_frontend", "release_phase", "alcf_contract")
LF_PHASE = "learn-frontend-032-v1"
LF_FIELDS = ("learn_frontend", "release_phase", "lf_contract")


def alf_phase(value):
    selected = value.get("auth_learn_frontend", False)
    require(type(selected) is bool, "ALF_SCOPE_INVALID")
    if selected:
        require(value.get("release_phase") == ALF_PHASE and not value.get("learn_only", False)
                and not value.get("frontend_only", False) and "alf_contract" in value
                and "learn_code_contract" not in value and not value.get("auth_learn_community_frontend", False), "ALF_PHASE_INVALID")
    else:
        require(value.get("release_phase") != ALF_PHASE and "alf_contract" not in value, "ALF_SCOPE_REQUIRED")
    return selected


def alcf_phase(value):
    selected = value.get("auth_learn_community_frontend", False)
    require(type(selected) is bool, "ALCF_SCOPE_INVALID")
    if selected:
        require(value.get("release_phase") == ALCF_PHASE and not any(value.get(key, False) for key in
                ("learn_only", "frontend_only", "auth_learn_frontend")) and "alcf_contract" in value
                and "alf_contract" not in value and "learn_code_contract" not in value, "ALCF_PHASE_INVALID")
    else:
        require(value.get("release_phase") != ALCF_PHASE and "alcf_contract" not in value, "ALCF_SCOPE_REQUIRED")
    return selected


def application_phase_fields(value):
    alf, alcf, lf = alf_phase(value), alcf_phase(value), lf_phase(value)
    return LF_FIELDS if lf else ALF_FIELDS if alf else ALCF_FIELDS if alcf else ()


def lf_phase(value):
    selected = value.get("learn_frontend", False)
    require(type(selected) is bool, "LF_SCOPE_INVALID")
    if selected:
        require(value.get("release_phase") == LF_PHASE and not any(value.get(key, False) for key in
                ("learn_only", "frontend_only", "auth_learn_frontend", "auth_learn_community_frontend"))
                and "lf_contract" in value and not any(key in value for key in
                ("learn_code_contract", "alf_contract", "alcf_contract")), "LF_PHASE_INVALID")
    else:
        require(value.get("release_phase") != LF_PHASE and "lf_contract" not in value, "LF_SCOPE_REQUIRED")
    return selected


def validate_lf_contract(value, inspection=None, migrations=None):
    require(isinstance(value, dict) and "auth_schema_sha256" in value, "LF_CONTRACT_INVALID")
    projected = {key: item for key, item in value.items() if key != "auth_schema_sha256"}
    validate_alcf_contract(projected, inspection, migrations)
    require(value["previous_heads"] == {"auth": "20261018_006", "learn": "20261018_031", "community": "20261013_005"}
            and value["target_heads"] == {**value["previous_heads"], "learn": "20261020_032"}
            and value["migration_services"] == ["learn"], "LF_MIGRATION_SCOPE_CHANGED")
    require(isinstance(value["auth_schema_sha256"], str) and HASH.fullmatch(value["auth_schema_sha256"]), "LF_AUTH_SCHEMA_INVALID")
    if inspection is not None:
        require(inspection["databases"]["auth"]["schema_sha256"] == value["auth_schema_sha256"], "LF_AUTH_SCHEMA_CHANGED")
    return value


def make_lf_contract(source_rows, inspection, migrations, baseline, hashes):
    value = make_alcf_contract(source_rows, inspection, migrations, baseline, hashes)
    value["auth_schema_sha256"] = inspection["databases"]["auth"]["schema_sha256"]
    prefixes = ("services/auth/", "services/community/")
    current = sorted((row for row in source_rows if row["path"].startswith(prefixes)), key=lambda row: row["path"])
    retained = sorted((row for row in baseline["managed_files"] if row["path"].startswith(prefixes)), key=lambda row: row["path"])
    require(current == retained, "LF_RETAINED_SOURCE_CHANGED")
    return validate_lf_contract(value, inspection, migrations)


def validate_alcf_contract(value, inspection=None, migrations=None):
    require(isinstance(value, dict) and set(value) == {"previous_heads", "target_heads", "migration_graph", "migration_services",
            "baseline_receipts", "previous_source_files", "unmanaged_in_target_families", "previous_images",
            "retained_migration_files", "community_schema_sha256"}, "ALCF_CONTRACT_INVALID")
    previous, target, graph = value["previous_heads"], value["target_heads"], value["migration_graph"]
    require(all(isinstance(heads, dict) and set(heads) == set(DATABASES) and all(isinstance(h, str) and REVISION.fullmatch(h)
            for h in heads.values()) for heads in (previous, target)), "ALCF_HEADS_INVALID")
    require(isinstance(graph, dict) and set(graph) == set(DATABASES) and all(isinstance(row, dict)
            and set(row) == {"head", "revisions"} and row["head"] == target[name]
            and isinstance(row["revisions"], list) and previous[name] in row["revisions"] and row["head"] in row["revisions"]
            and len(row["revisions"]) == len(set(row["revisions"])) and all(isinstance(h, str) and REVISION.fullmatch(h) for h in row["revisions"])
            for name, row in graph.items()), "ALCF_MIGRATION_GRAPH_CHANGED")
    require(value["migration_services"] == [name for name in DATABASES if previous[name] != target[name]], "ALCF_MIGRATION_SCOPE_CHANGED")
    require(isinstance(value["community_schema_sha256"], str) and HASH.fullmatch(value["community_schema_sha256"]), "ALCF_COMMUNITY_SCHEMA_INVALID")
    require(isinstance(value["previous_images"], dict) and set(value["previous_images"]) == {*DATABASES, "frontend"}
            and all(isinstance(image, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in value["previous_images"].values()), "ALCF_PREVIOUS_IMAGES_INVALID")
    hashes = value["baseline_receipts"]
    require(isinstance(hashes, list) and 0 < len(hashes) <= 20 and all(isinstance(row, dict) and set(row) == {"activation", "build", "verify"}
            and all(isinstance(h, str) and HASH.fullmatch(h) for h in row.values()) for row in hashes), "ALCF_BASELINE_INVALID")
    for field in ("previous_source_files", "retained_migration_files", "unmanaged_in_target_families"):
        rows = value[field]
        require(isinstance(rows, list) and len(rows) <= 100000 and (rows or field == "unmanaged_in_target_families")
                and all(isinstance(r, dict) and set(r) == {"path", "bytes", "sha256"}
                and isinstance(r["path"], str) and r["path"].startswith(("frontend/", "services/auth/", "services/learn/", "services/community/"))
                and "\\" not in r["path"] and not any(ord(c) < 32 for c in r["path"])
                and all(part not in {"", ".", ".."} for part in r["path"].split("/"))
                and type(r["bytes"]) is int and r["bytes"] >= 0
                and isinstance(r["sha256"], str) and HASH.fullmatch(r["sha256"]) for r in rows), "ALCF_SOURCE_FILES_INVALID")
        require(rows == sorted(rows, key=lambda r: r["path"]) and len({r["path"] for r in rows}) == len(rows), "ALCF_SOURCE_FILES_INVALID")
    require(not ({r["path"] for r in value["previous_source_files"]}
                 & {r["path"] for r in value["unmanaged_in_target_families"]}), "ALCF_UNMANAGED_OVERLAP")
    require(value["retained_migration_files"] == [r for r in value["previous_source_files"] if re.fullmatch(
            r"services/(auth|learn|community)/migrations/versions/[^/]+\.py", r["path"])], "ALCF_MIGRATION_FILES_INVALID")
    if inspection is not None:
        require({name: row["head"] for name, row in inspection["databases"].items()} == {name: [head] for name, head in previous.items()}, "ALCF_LIVE_HEADS_CHANGED")
        require(inspection["databases"]["community"]["schema_sha256"] == value["community_schema_sha256"], "ALCF_COMMUNITY_SCHEMA_CHANGED")
        live = sorted((r for r in inspection["code_files"] if r["path"].startswith(("frontend/", "services/auth/", "services/learn/", "services/community/"))), key=lambda r: r["path"])
        expected = sorted(value["previous_source_files"] + value["unmanaged_in_target_families"], key=lambda r: r["path"])
        require(live == expected, "ALCF_LIVE_BASELINE_SOURCE_CHANGED")
        for family, image in value["previous_images"].items():
            service = "frontend" if family == "frontend" else family + "-service"
            rows = [r for r in inspection["containers"] if r["service"] == service and r["running"]]
            require(len(rows) == 1 and rows[0]["image_id"] == image, "ALCF_LIVE_BASELINE_IMAGE_CHANGED")
    if migrations is not None:
        require(graph == migrations, "ALCF_MIGRATION_GRAPH_CHANGED")
    return value


def make_alcf_contract(source_rows, inspection, migrations, baseline, hashes):
    previous_rows = sorted(baseline["managed_files"], key=lambda r: r["path"])
    unmanaged = sorted((r for r in baseline["unmanaged_files"] if r["path"].startswith(
        ("frontend/", "services/auth/", "services/learn/", "services/community/"))), key=lambda r: r["path"])
    retained = [r for r in previous_rows if re.fullmatch(r"services/(auth|learn|community)/migrations/versions/[^/]+\.py", r["path"])]
    current = {r["path"]: r for r in source_rows}
    require(not any(r["path"] in current for r in unmanaged), "ALCF_UNMANAGED_SOURCE_APPLY_FORBIDDEN")
    require(retained and all(current.get(r["path"]) == r for r in retained), "ALCF_HISTORICAL_MIGRATION_BYTES_CHANGED")
    previous, target = dict(baseline["expected_heads"]), {name: row["head"] for name, row in migrations.items()}
    return validate_alcf_contract({"previous_heads": previous, "target_heads": target, "migration_graph": migrations,
        "migration_services": [name for name in DATABASES if previous[name] != target[name]], "baseline_receipts": hashes,
        "previous_source_files": previous_rows, "unmanaged_in_target_families": unmanaged,
        "previous_images": dict(baseline["images"]), "retained_migration_files": retained,
        "community_schema_sha256": inspection["databases"]["community"]["schema_sha256"]}, inspection, migrations)


def validate_alf_contract(value, inspection=None, migrations=None):
    require(isinstance(value, dict) and set(value) == {"previous_heads", "target_heads", "migration_graph", "migration_services", "baseline_receipts", "community_source_files", "community_schema_sha256"}, "ALF_CONTRACT_INVALID")
    require(isinstance(value["community_schema_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["community_schema_sha256"]), "ALF_COMMUNITY_SCHEMA_INVALID")
    for key in ("previous_heads", "target_heads"):
        require(isinstance(value[key], dict) and set(value[key]) == {"auth", "learn", "community"}
                and all(isinstance(head, str) and re.fullmatch(r"[A-Za-z0-9_]+", head) for head in value[key].values()), "ALF_HEADS_INVALID")
    previous, target = value["previous_heads"], value["target_heads"]
    graph = value["migration_graph"]
    require(isinstance(graph, dict) and set(graph) == set(previous)
            and all(isinstance(row, dict) and set(row) == {"head", "revisions"} and isinstance(row["revisions"], list)
                    and row["head"] == target[name] and previous[name] in row["revisions"] and row["head"] in row["revisions"]
                    and all(isinstance(revision, str) and re.fullmatch(r"[A-Za-z0-9_]+", revision) for revision in row["revisions"])
                    and len(set(row["revisions"])) == len(row["revisions"])
                    for name, row in graph.items()), "ALF_MIGRATION_GRAPH_CHANGED")
    require(previous["community"] == target["community"], "ALF_COMMUNITY_MIGRATION_FORBIDDEN")
    require(value["migration_services"] == [name for name in ("auth", "learn") if previous[name] != target[name]], "ALF_MIGRATION_SCOPE_CHANGED")
    hashes = value["baseline_receipts"]
    require(isinstance(hashes, list) and 0 < len(hashes) <= 20 and all(isinstance(row, dict)
            and set(row) == {"activation", "build", "verify"} and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in row.values()) for row in hashes), "ALF_BASELINE_INVALID")
    rows = value["community_source_files"]
    require(isinstance(rows, list) and rows and all(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256"}
                and isinstance(row["path"], str) and row["path"].startswith("services/community/")
                and ".." not in row["path"].split("/") and type(row["bytes"]) is int and row["bytes"] >= 0
                and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) for row in rows), "ALF_COMMUNITY_SOURCE_INVALID")
    require(rows == sorted(rows, key=lambda row: row["path"]) and len({row["path"] for row in rows}) == len(rows), "ALF_COMMUNITY_SOURCE_INVALID")
    if inspection is not None:
        require(value["community_schema_sha256"] == inspection["databases"]["community"]["schema_sha256"], "ALF_COMMUNITY_SCHEMA_CHANGED")
        require({name: row["head"] for name, row in inspection["databases"].items()}
                == {name: [head] for name, head in previous.items()}, "ALF_LIVE_HEADS_CHANGED")
        require(sorted((row for row in inspection["code_files"] if row["path"].startswith("services/community/")), key=lambda row: row["path"]) == rows, "ALF_LIVE_COMMUNITY_SOURCE_CHANGED")
    if migrations is not None:
        require(graph == migrations, "ALF_MIGRATION_GRAPH_CHANGED")
    return value


def make_alf_contract(source_rows, inspection, migrations, baseline, hashes):
    rows = sorted((row for row in source_rows if row["path"].startswith("services/community/")), key=lambda row: row["path"])
    require(rows == sorted((row for row in baseline["managed_files"] if row["path"].startswith("services/community/")), key=lambda row: row["path"]), "ALF_COMMUNITY_SOURCE_CHANGED")
    previous = dict(baseline["expected_heads"])
    target = {name: row["head"] for name, row in migrations.items()}
    return validate_alf_contract({"previous_heads": previous, "target_heads": target,
        "migration_graph": migrations,
        "community_schema_sha256": inspection["databases"]["community"]["schema_sha256"],
        "migration_services": [name for name in ("auth", "learn") if previous[name] != target[name]],
        "baseline_receipts": hashes, "community_source_files": rows}, inspection, migrations)


def code_only_phase(value):
    if alf_phase(value) or alcf_phase(value) or lf_phase(value):
        return False
    phase = value.get("release_phase")
    require("release_phase" not in value or phase == LEARN_CODE_PHASE, "RELEASE_PHASE_INVALID")
    require(phase is None or value.get("learn_only") is True and not value.get("frontend_only", False),
            "LEARN_CODE_SCOPE_INVALID")
    require(("learn_code_contract" in value) is (phase is not None), "LEARN_CODE_CONTRACT_REQUIRED")
    return phase is not None


def make_code_contract(source_rows, inspection, migrations, baseline, hashes):
    require(baseline.get("expected_heads", {}).get("learn") == "20261013_029"
            and migrations["learn"]["head"] == "20261013_029"
            and inspection["databases"]["learn"]["head"] == ["20261013_029"], "LEARN_CODE_HEAD_INVALID")
    rows = learn_migration_rows(source_rows)
    require(rows == learn_migration_rows(baseline["managed_files"])
            == learn_migration_rows(inspection["code_files"]), "LEARN_CODE_MIGRATION_BYTES_CHANGED")
    live = [row for row in inspection["containers"] if row["service"] == "learn-service" and row["running"]]
    require(len(live) == 1 and live[0]["image_id"] == baseline["images"]["learn"], "LEARN_CODE_PREIMAGE_CHANGED")
    return {"baseline_receipts": hashes, "migration_files": rows,
            "schema_sha256": inspection["databases"]["learn"]["schema_sha256"],
            "previous_learn_image": baseline["images"]["learn"]}


def validate_code_contract(value, inspection=None, migrations=None):
    require(isinstance(value, dict) and set(value) == {"baseline_receipts", "migration_files", "schema_sha256", "previous_learn_image"},
            "LEARN_CODE_CONTRACT_INVALID")
    require(isinstance(value["baseline_receipts"], list) and 0 < len(value["baseline_receipts"]) <= 20
            and all(isinstance(row, dict) and set(row) == {"activation", "build", "verify"}
                    and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in row.values())
                    for row in value["baseline_receipts"]), "LEARN_CODE_BASELINE_INVALID")
    rows = value["migration_files"]
    require(isinstance(rows, list) and rows == learn_migration_rows(rows)
            and len({row["path"] for row in rows}) == len(rows)
            and all(set(row) == {"path", "bytes", "sha256"} and type(row["bytes"]) is int and row["bytes"] >= 0
                    and isinstance(row["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) for row in rows),
            "LEARN_CODE_MIGRATION_ROWS_INVALID")
    require(isinstance(value["schema_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", value["schema_sha256"])
            and isinstance(value["previous_learn_image"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value["previous_learn_image"]),
            "LEARN_CODE_BASELINE_INVALID")
    if inspection is not None:
        require(inspection["databases"]["learn"]["head"] == ["20261013_029"]
                and inspection["databases"]["learn"]["schema_sha256"] == value["schema_sha256"]
                and learn_migration_rows(inspection["code_files"]) == rows, "LEARN_CODE_LIVE_BASELINE_CHANGED")
        live = [row for row in inspection["containers"] if row["service"] == "learn-service" and row["running"]]
        require(len(live) == 1 and live[0]["image_id"] == value["previous_learn_image"], "LEARN_CODE_PREIMAGE_CHANGED")
    if migrations is not None:
        require(migrations["learn"]["head"] == "20261013_029", "LEARN_CODE_SOURCE_HEAD_CHANGED")
    return value


LEARN_CODE_STATE_SQL = """SELECT json_build_object(
'head',(SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version),
'triggers_sha256',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(jsonb_build_object('table',c.relname,'name',t.tgname,'enabled',t.tgenabled,'definition',pg_get_triggerdef(t.oid)) ORDER BY c.relname,t.tgname),'[]'::jsonb)::text,'UTF8')),'hex') FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal AND n.nspname='public'),
'categories_sha256',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.key),'[]'::jsonb)::text,'UTF8')),'hex') FROM public.platform_event_categories r),
'category_audit_sha256',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id),'[]'::jsonb)::text,'UTF8')),'hex') FROM public.platform_event_category_audit_events r),
'events_sha256',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id),'[]'::jsonb)::text,'UTF8')),'hex') FROM public.platform_events r))"""


def validate_code_state(value):
    keys = {"head", "schema_sha256", "triggers_sha256", "categories_sha256", "category_audit_sha256", "events_sha256"}
    require(isinstance(value, dict) and set(value) == keys and value["head"] == ["20261013_029"]
            and all(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in keys - {"head"}),
            "LEARN_CODE_STATE_INVALID")
    return value


def code_runtime(snapshot, targets, *, include_labels=True):
    keys = ("service", "container_id", "image_id", "running", "pid", "started_at", "finished_at", "restart_count", "mounts")
    result = []
    for row in snapshot["containers"]:
        if row["service"] in targets:
            continue
        value = {key: row[key] for key in keys}
        if include_labels:
            labels = row.get("source_labels", snapshot.get("source_labels", {}).get(row["container_id"], {}))
            value["source_labels"] = {key: labels.get(key) for key in (
                "org.opencontainers.image.revision", "io.ctf-web.source-fingerprint", "io.ctf-web.release-nonce")}
        result.append(value)
    return sorted(result, key=lambda row: (row["service"], row["container_id"]))


def validate_code_preservation(value, contract):
    validate_code_contract(contract)
    before, after = validate_code_state(value.get("learn_code_before")), validate_code_state(value.get("learn_code_after"))
    require(before == after and before["schema_sha256"] == contract["schema_sha256"], "LEARN_CODE_DATA_OR_SCHEMA_CHANGED")
    require(value.get("migration_executed") is False and "category_before_migration" not in value,
            "LEARN_CODE_MIGRATION_FORBIDDEN")
    require(isinstance(value.get("compatibility_image_backup_receipt_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["compatibility_image_backup_receipt_sha256"]), "LEARN_CODE_PREIMAGE_BACKUP_REQUIRED")
    require(isinstance(value.get("untargeted_runtime_before"), list) and bool(value["untargeted_runtime_before"])
            and value["untargeted_runtime_before"] == value.get("untargeted_runtime_after"), "LEARN_CODE_UNTARGETED_RUNTIME_CHANGED")
    return before


# This program executes on the target only when the operator explicitly runs
# preflight. It emits no config/env values, SQL rows, object names or stderr.
REMOTE_HELPERS = r'''
import hashlib, io, json, os, pathlib, re, stat, subprocess, sys, tarfile, threading
ROOT = pathlib.Path('/home/deploy/CTF_Web')
PROJECT = 'ctf_web'
DATABASES = {'auth':'platform_db','learn':'learn_db','community':'community_db'}
SKIP = {'.git','.verification','.venv','__pycache__','node_modules','.next','.idea','.vscode','dist','build','out','.turbo','volumes','logs','vendor','.playwright-mcp','.playwright-cli'}
ROOTS = ['frontend','services','infra','ctfd','scripts']
class InspectionError(Exception): pass
def fail(code): raise InspectionError(code)
def run(args, limit=120):
    result = subprocess.run(args, capture_output=True, timeout=limit)
    if result.returncode: fail('READ_COMMAND_FAILED')
    return result.stdout
def digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()
def regular(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode): fail('REGULAR_FILE_REQUIRED')
def file_row(path, name):
    regular(path)
    return {'path':name,'bytes':path.stat().st_size,'sha256':digest(path)}
def code_rows(protected=()):
    if ROOT.is_symlink(): fail('LIVE_ROOT_LINK')
    protected_paths={row['path'] for row in protected}|{str(ROOT/'infra/.env')}
    rows=[]
    for name in ['README.md','.gitignore']:
        path=ROOT/name
        if path.exists(): rows.append(file_row(path,name))
    for name in ROOTS:
        root=ROOT/name
        if root.is_symlink(): fail('CODE_ROOT_LINK')
        if not root.is_dir(): continue
        for directory,children,files in os.walk(root,followlinks=False):
            base=pathlib.Path(directory)
            for child in children:
                if (base/child).is_symlink(): fail('CODE_LINK')
            children[:]=sorted(child for child in children if child not in SKIP and not (child=='data' and (base.relative_to(ROOT).as_posix() in {'infra','ctfd'} or base.parent==ROOT/'services')))
            for name in sorted(files):
                path=base/name
                if name.startswith('.env') and name not in {'.env.example','.env.sample','.env.template','.env.schema'}:
                    if str(path) not in protected_paths: fail('UNDECLARED_ENV_FILE')
                    continue
                if path.suffix.lower() in {'.pem','.key','.pyc','.log','.zip'} or name in {'MEMORY.md','Thumbs.db','.DS_Store','tsconfig.tsbuildinfo','next-env.d.ts'}: continue
                rows.append(file_row(path,path.relative_to(ROOT).as_posix()))
                if len(rows)>100000: fail('CODE_FILE_LIMIT')
    return sorted(rows,key=lambda row:row['path'])
def compose():
    return json.loads(run(['docker','compose','-p',PROJECT,'--project-directory',str(ROOT/'infra'),'--env-file',str(ROOT/'infra/.env'),'-f',str(ROOT/'infra/docker-compose.yml'),'-f',str(ROOT/'infra/docker-compose.prod.yml'),'config','--format','json']))
def protected_key_bytes(path):
    if path!=ROOT/'services/auth/jwt_private.pem': fail('PROTECTED_FALLBACK_SCOPE_FORBIDDEN')
    auth=singleton(containers(),'auth-service',False)
    destination='/run/secrets/auth_jwt_private_key'
    matches=[mount for mount in auth['mounts'] if mount['type']=='bind' and mount['source']==str(path) and mount['destination']==destination and mount['read_only'] is True]
    if len(matches)!=1: fail('PROTECTED_FALLBACK_MOUNT_UNVERIFIED')
    process=subprocess.Popen(['docker','cp',auth['container_id']+':'+destination,'-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    timer=threading.Timer(60,process.kill);timer.start()
    try:
        payload=process.stdout.read(2*1024*1024+1)
        if len(payload)>2*1024*1024: fail('PROTECTED_FALLBACK_TAR_TOO_LARGE')
        if process.wait(timeout=15): fail('PROTECTED_FALLBACK_COPY_FAILED')
        with tarfile.open(fileobj=io.BytesIO(payload),mode='r:*') as archive:
            members=archive.getmembers()
            if len(members)!=1: fail('PROTECTED_FALLBACK_SINGLE_FILE_REQUIRED')
            item=members[0]
            parsed=pathlib.PurePosixPath(item.name)
            if not item.isfile() or parsed.is_absolute() or '..' in parsed.parts or parsed.name!='auth_jwt_private_key' or item.size>1024*1024: fail('PROTECTED_FALLBACK_MEMBER_INVALID')
            data=archive.extractfile(item).read(1024*1024+1)
            if len(data)!=item.size or len(data)>1024*1024: fail('PROTECTED_FALLBACK_CONTENT_INVALID')
            return data
    finally:
        timer.cancel();process.stdout.close()
        if process.poll() is None: process.kill();process.wait()
def protected_row(path):
    try:
        return file_row(path,str(path))
    except PermissionError:
        content=protected_key_bytes(path)
        return {'path':str(path),'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
def protected_rows(model):
    paths={ROOT/name for name in ['infra/docker-compose.yml','infra/docker-compose.prod.yml','infra/caddy/Caddyfile','infra/.env']}
    secrets=model.get('secrets',{})
    if set(secrets)!={'auth_jwt_private_key','platform_jwt_public_key'}: fail('COMPOSE_SECRET_CONTRACT_UNKNOWN')
    for item in secrets.values():
        if not isinstance(item,dict) or not isinstance(item.get('file'),str): fail('COMPOSE_SECRET_FILE_UNKNOWN')
        paths.add(pathlib.Path(item['file']))
    for service in model.get('services',{}).values():
        for item in service.get('env_file',[]):
            paths.add(pathlib.Path(item if isinstance(item,str) else item['path']))
    rows=[]
    for path in sorted(paths):
        if not path.is_absolute(): path=ROOT/'infra'/path
        if not any(path.is_relative_to(base) for base in [pathlib.Path('/home/deploy'),pathlib.Path('/var/lib/ctf-web')]): fail('PROTECTED_PATH_SCOPE_UNKNOWN')
        for parent in [path,*path.parents]:
            if parent.is_symlink(): fail('PROTECTED_PATH_LINK')
        rows.append(protected_row(path))
    return rows
def containers():
    ids=run(['docker','ps','-a','--filter','label=com.docker.compose.project='+PROJECT,'--format','{{.ID}}']).decode().splitlines()
    if not ids: fail('COMPOSE_CONTAINERS_MISSING')
    rows=[]
    for item in json.loads(run(['docker','inspect',*ids])):
        labels=item.get('Config',{}).get('Labels',{}) or {}
        if labels.get('com.docker.compose.project')!=PROJECT: fail('COMPOSE_PROJECT_CHANGED')
        if labels.get('ctf-challenge')=='true': continue
        state=item.get('State',{})
        rows.append({'service':labels.get('com.docker.compose.service'),'container_id':item['Id'],'image_id':item['Image'],'running':state.get('Running') is True,
          'pid':state.get('Pid'),'status':state.get('Status'),'started_at':state.get('StartedAt'),'finished_at':state.get('FinishedAt'),'restart_count':item.get('RestartCount'),
          'health':state.get('Health',{}).get('Status'), 'mounts':sorted([{'type':m['Type'],'source':m['Source'],'destination':m['Destination'],'read_only':not m.get('RW',False)} for m in item.get('Mounts',[])],key=lambda m:(m['destination'],m['type'],m['source'],m['read_only']))})
    if not rows: fail('COMPOSE_CONTAINERS_MISSING')
    return sorted(rows,key=lambda row:(row['service'] or '',row['container_id']))
def singleton(rows,service,require_running=True):
    selected=[row for row in rows if row['service']==service and (row['running'] or not require_running)]
    if len(selected)!=1: fail('RUNNING_SERVICE_AMBIGUOUS')
    return selected[0]
def database_rows(postgres):
    rows={}
    shell='export PGPASSWORD="$POSTGRES_PASSWORD"; export PGOPTIONS="-c default_transaction_read_only=on"; exec psql -U "$POSTGRES_USER" -d "$1" -X -A -t -v ON_ERROR_STOP=1 -c "$2"'
    sql="SELECT json_build_object('head',(SELECT json_agg(version_num ORDER BY version_num) FROM public.alembic_version),'bytes',pg_database_size(current_database()),'table_count',(SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'),'client_session_count',(SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'))"
    for service,database in DATABASES.items():
        row=json.loads(run(['docker','exec',postgres,'sh','-c',shell,'renewal-inspect',database,sql]))
        schema_shell='export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_dump -U "$POSTGRES_USER" -d "$1" --schema-only --no-owner --no-privileges --no-comments'
        schema=run(['docker','exec',postgres,'sh','-c',schema_shell,'renewal-schema',database])
        # Recent pg_dump releases emit a random restrict token; exclude only those control lines.
        schema=b'\n'.join(line for line in schema.splitlines() if not line.startswith((b'\\restrict ',b'\\unrestrict ')))
        row.update({'database':database,'schema_sha256':hashlib.sha256(schema).hexdigest()})
        rows[service]=row
    return rows
def tar_inventory(stream):
    rows=[]
    with tarfile.open(fileobj=stream,mode='r|*') as archive:
        for item in archive:
            parts=pathlib.PurePosixPath(item.name).parts
            if item.name.startswith('/') or '..' in parts or not (item.isdir() or item.isfile()): fail('UNSAFE_ARCHIVE_MEMBER')
            if item.isfile():
                data=archive.extractfile(item)
                hashed=hashlib.file_digest(data,'sha256').hexdigest()
                name=pathlib.PurePosixPath(item.name).as_posix().removeprefix('./')
                rows.append([hashlib.sha256(name.encode()).hexdigest(),item.size,hashed])
                if len(rows)>1000000: fail('OBJECT_FILE_LIMIT')
    if len({row[0] for row in rows})!=len(rows): fail('DUPLICATE_OBJECT_MEMBER')
    rows.sort()
    return {'file_count':len(rows),'bytes':sum(row[1] for row in rows),'inventory_sha256':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()}
def minio_inventory(container):
    process=subprocess.Popen(['docker','cp',container+':/data/.','-'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    timer=threading.Timer(600,process.kill)
    timer.start()
    try:
        result=tar_inventory(process.stdout)
        process.stdout.close()
        if process.wait(timeout=15): fail('OBJECT_INSPECTION_FAILED')
        return result
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill();process.wait()
'''

INSPECTOR = REMOTE_HELPERS + r'''
stage='compose'
try:
    model=compose()
    stage='protected'; protected=protected_rows(model)
    stage='containers'; current=containers()
    postgres=singleton(current,'postgres'); minio=singleton(current,'minio',False)
    for service in ['auth-service','learn-service','community-service','frontend']: singleton(current,service,False)
    mounts=[row for row in minio['mounts'] if row['destination']=='/data' and row['type']=='volume' and not row['read_only']]
    if len(mounts)!=1: fail('MINIO_MOUNT_CONTRACT_UNKNOWN')
    stage='databases'; databases=database_rows(postgres['container_id'])
    stage='code'; code=code_rows(protected)
    stage='objects'; objects=minio_inventory(minio['container_id'])
    available=os.statvfs(ROOT)
    print(json.dumps({'schema_version':1,'verified':True,'live_root':str(ROOT),'project':PROJECT,'containers':current,'protected_files':protected,'code_files':code,'databases':databases,'objects':objects,
      'compose_sha256':hashlib.sha256(json.dumps(model,sort_keys=True,separators=(',',':')).encode()).hexdigest(),'disk_available_bytes':available.f_bavail*available.f_frsize}))
except Exception:
    print(json.dumps({'schema_version':1,'verified':False,'unverified_stage':stage}))
'''

# Recheck the write fence without rereading the entire object volume for every
# stream. The actual object archive is independently hashed against the full
# post-drain inventory by the local backup consumer.
FENCE_INSPECTOR = REMOTE_HELPERS + r'''
stage='fence'
try:
    model=compose(); current=containers(); postgres=singleton(current,'postgres'); protected=protected_rows(model)
    print(json.dumps({'schema_version':1,'verified':True,'kind':'renewal-fence-inspection','live_root':str(ROOT),'project':PROJECT,
      'containers':current,'protected_files':protected,'code_files':code_rows(protected),'databases':database_rows(postgres['container_id']),
      'compose_sha256':hashlib.sha256(json.dumps(model,sort_keys=True,separators=(',',':')).encode()).hexdigest()}))
except Exception:
    print(json.dumps({'schema_version':1,'verified':False,'unverified_stage':stage}))
'''


def remote_program(source: str) -> str:
    encoded = base64.b64encode(source.encode()).decode()
    return "python3 -c " + shlex.quote(f"import base64;exec(base64.b64decode({encoded!r}).decode())")


def validate_inspection(value, migrations, *, quiesced=False):
    require(isinstance(value, dict) and value.get("schema_version") == 1 and value.get("verified") is True, "REMOTE_INSPECTION_UNVERIFIED")
    require(set(value) == {"schema_version", "verified", "live_root", "project", "containers", "protected_files", "code_files", "databases", "objects", "compose_sha256", "disk_available_bytes"}, "INSPECTION_UNKNOWN_FIELDS")
    require(value.get("live_root") == ops.LIVE and value.get("project") == PROJECT, "REMOTE_TARGET_CHANGED")
    require(HASH.fullmatch(value.get("compose_sha256", "")), "COMPOSE_HASH_MISSING")
    require(set(value.get("databases", {})) == set(DATABASES), "DATABASE_SCOPE_INVALID")
    for service, database in DATABASES.items():
        row = value["databases"][service]
        require(set(row) == {"database", "head", "bytes", "table_count", "client_session_count", "schema_sha256"}, "DATABASE_UNKNOWN_FIELDS")
        require(row.get("database") == database and type(row.get("bytes")) is int and row["bytes"] > 0, "DATABASE_SIZE_UNVERIFIED")
        require(type(row.get("table_count")) is int and row["table_count"] > 0, "DATABASE_SCHEMA_UNVERIFIED")
        require(isinstance(row.get("head"), list) and len(row["head"]) == 1 and row["head"][0] in migrations[service]["revisions"], "DATABASE_HEAD_UNKNOWN")
        require(HASH.fullmatch(row.get("schema_sha256", "")), "DATABASE_SCHEMA_HASH_MISSING")
        require(type(row.get("client_session_count")) is int and row["client_session_count"] >= 0, "DATABASE_SESSIONS_UNVERIFIED")
    for key, absolute in (("code_files", False), ("protected_files", True)):
        rows = value.get(key)
        require(isinstance(rows, list) and rows, "FILE_INVENTORY_MISSING")
        names = set()
        for row in rows:
            require(set(row) == {"path", "bytes", "sha256"}, "FILE_INVENTORY_UNKNOWN_FIELDS")
            path = row.get("path", "")
            parsed = PurePosixPath(path)
            require(bool(path) and parsed.is_absolute() == absolute and ".." not in parsed.parts and path not in names, "FILE_INVENTORY_PATH_INVALID")
            if absolute:
                require(any(parsed.is_relative_to(root) for root in (PurePosixPath("/home/deploy"), PurePosixPath("/var/lib/ctf-web"))), "PROTECTED_PATH_SCOPE_UNKNOWN")
            require(HASH.fullmatch(row.get("sha256", "")) and type(row.get("bytes")) is int and row["bytes"] >= 0, "FILE_INVENTORY_HASH_INVALID")
            names.add(path)
    required = {f"{ops.LIVE}/{name}" for name in ("infra/docker-compose.yml", "infra/docker-compose.prod.yml", "infra/caddy/Caddyfile", "infra/.env")}
    require(required <= {row["path"] for row in value["protected_files"]}, "PROTECTED_FILES_INCOMPLETE")
    rows = value.get("containers", [])
    require(bool(rows), "CONTAINER_INVENTORY_MISSING")
    for row in rows:
        require(set(row) == {"service", "container_id", "image_id", "running", "pid", "status", "started_at", "finished_at", "restart_count", "health", "mounts"}, "CONTAINER_UNKNOWN_FIELDS")
        require(re.fullmatch(r"[a-z][a-z0-9_-]{0,100}", row.get("service", "")), "CONTAINER_SERVICE_INVALID")
        require(HASH.fullmatch(row.get("container_id", "")) and re.fullmatch(r"sha256:[0-9a-f]{64}", row.get("image_id", "")), "CONTAINER_ID_INVALID")
        require(type(row.get("pid")) is int and row["pid"] >= 0 and type(row.get("restart_count")) is int and row["restart_count"] >= 0, "CONTAINER_PROCESS_UNVERIFIED")
        require(row.get("status") in {"running", "exited", "created"} and isinstance(row.get("started_at"), str) and isinstance(row.get("finished_at"), str), "CONTAINER_STATE_UNVERIFIED")
        require(row["health"] in {None, "healthy", "unhealthy", "starting"}, "CONTAINER_HEALTH_UNKNOWN")
        require(all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", row[key]) for key in ("started_at", "finished_at")), "CONTAINER_TIMESTAMPS_UNVERIFIED")
        require(type(row.get("running")) is bool and (row["pid"] > 0) == row["running"], "CONTAINER_PID_STATE_MISMATCH")
        for mount in row.get("mounts", []):
            require(set(mount) == {"type", "source", "destination", "read_only"} and mount["type"] in {"volume", "bind", "tmpfs"}
                    and isinstance(mount["source"], str) and isinstance(mount["destination"], str) and type(mount["read_only"]) is bool, "CONTAINER_MOUNT_UNKNOWN")
    for service in ("postgres", "minio", "auth-service", "learn-service", "community-service", "frontend"):
        selected = [row for row in rows if row["service"] == service and (quiesced or row.get("running") is True)]
        require(len(selected) == 1, "RUNNING_SERVICE_AMBIGUOUS")
        if service == "postgres":
            require(selected[0]["running"], "POSTGRES_NOT_RUNNING")
    minio = next(row for row in rows if row["service"] == "minio")
    require(len([mount for mount in minio["mounts"] if mount["destination"] == "/data" and mount["type"] == "volume" and not mount["read_only"]]) == 1, "MINIO_MOUNT_CONTRACT_UNKNOWN")
    objects = value.get("objects", {})
    require(set(objects) == {"file_count", "bytes", "inventory_sha256"} and type(objects.get("file_count")) is int and objects["file_count"] >= 0 and type(objects.get("bytes")) is int and objects["bytes"] >= 0 and HASH.fullmatch(objects.get("inventory_sha256", "")), "OBJECT_INVENTORY_UNVERIFIED")
    require(type(value.get("disk_available_bytes")) is int and value["disk_available_bytes"] >= 0, "REMOTE_DISK_UNVERIFIED")


def backup_scope(*, learn_only=False, auth_learn_frontend=False, auth_learn_community_frontend=False, learn_frontend=False):
    require(sum((learn_only, auth_learn_frontend, auth_learn_community_frontend, learn_frontend)) <= 1, "BACKUP_SCOPE_INVALID")
    return {"databases": [DATABASES[name] for name in ("auth", "learn")] if auth_learn_frontend else [DATABASES["learn"]] if learn_only or learn_frontend else list(DATABASES.values()), "objects": "/data", "code": True,
            "protected_settings": True, "old_image_ids": True, "ctfd_db": False}


def make_plan(source, inspection, migrations, *, now=None, nonce=None, learn_only=False, code_contract=None, alf_contract=None, alcf_contract=None, lf_contract=None):
    validate_inspection(inspection, migrations)
    require(code_contract is None or learn_only, "LEARN_CODE_SCOPE_INVALID")
    require(alf_contract is None or not learn_only and code_contract is None, "ALF_SCOPE_INVALID")
    require(alcf_contract is None or not learn_only and code_contract is None and alf_contract is None, "ALCF_SCOPE_INVALID")
    require(lf_contract is None or not learn_only and code_contract is None and alf_contract is None and alcf_contract is None, "LF_SCOPE_INVALID")
    require(HASH.fullmatch(source.get("fingerprint", "")) and bool(source.get("files")), "SOURCE_MANIFEST_INVALID")
    return {"schema_version": 1, "kind": "renewal-release-plan", "task": TASK_ID,
            "nonce": str(nonce or uuid4()), "created_at": (now or datetime.now(timezone.utc)).isoformat(),
            "source_fingerprint": source["fingerprint"], "source_file_count": len(source["files"]),
            "target": {"host": ops.HOST, "port": SSH_PORT, "live_root": ops.LIVE, "project": PROJECT},
            "migrations": migrations, "inspection": inspection,
            "backup_scope": backup_scope(learn_only=learn_only, auth_learn_frontend=alf_contract is not None, auth_learn_community_frontend=alcf_contract is not None, learn_frontend=lf_contract is not None),
            "consistency": "online inspection only; Auth/Learn and MinIO will be fenced; Community remains running and is not captured" if alf_contract is not None else "online inspection only; Learn-only capture will fence Learn writers and MinIO; Auth/Community snapshots excluded" if learn_only else "independent online PostgreSQL snapshots and file inventories; cross-service point-in-time consistency unverified",
            **({"learn_only": True} if learn_only else {}),
            **({"release_phase": LEARN_CODE_PHASE, "learn_code_contract": validate_code_contract(code_contract, inspection, migrations)}
               if code_contract is not None and learn_only else {}),
            **({"auth_learn_frontend": True, "release_phase": ALF_PHASE,
                "alf_contract": validate_alf_contract(alf_contract, inspection, migrations)} if alf_contract is not None else {}),
            **({"auth_learn_community_frontend": True, "release_phase": ALCF_PHASE,
                "alcf_contract": validate_alcf_contract(alcf_contract, inspection, migrations)} if alcf_contract is not None else {}),
            **({"learn_frontend": True, "release_phase": LF_PHASE,
                "lf_contract": validate_lf_contract(lf_contract, inspection, migrations),
                "consistency": "Learn and MinIO fenced; Auth/Community remain running and are not captured"} if lf_contract is not None else {})}


def validate_plan(plan, source_fingerprint, *, now=None):
    require(isinstance(plan, dict), "PLAN_SCHEMA_INVALID")
    learn_only = plan.get("learn_only", False)
    alf = alf_phase(plan)
    alcf = alcf_phase(plan)
    lf = lf_phase(plan)
    code_only = code_only_phase(plan)
    require(type(learn_only) is bool and ("learn_only" not in plan or learn_only is True), "PLAN_SCOPE_INVALID")
    require(set(plan) == {"schema_version", "kind", "task", "nonce", "created_at", "source_fingerprint", "source_file_count", "target", "migrations", "inspection", "backup_scope", "consistency"}
            | ({"learn_only"} if learn_only else set())
            | ({"release_phase", "learn_code_contract"} if code_only else set())
            | (set(ALF_FIELDS) if alf else set()) | (set(ALCF_FIELDS) if alcf else set()) | (set(LF_FIELDS) if lf else set()), "PLAN_UNKNOWN_FIELDS")
    require(plan.get("schema_version") == 1 and plan.get("kind") == "renewal-release-plan" and plan.get("task") == TASK_ID, "PLAN_SCHEMA_INVALID")
    try:
        nonce = UUID(plan["nonce"])
        require(nonce.version == 4 and str(nonce) == plan["nonce"], "PLAN_NONCE_INVALID")
    except (ValueError, KeyError, AttributeError) as exc:
        raise ReleaseError("PLAN_NONCE_INVALID") from exc
    age = (now or datetime.now(timezone.utc)) - timestamp(plan.get("created_at"))
    require(timedelta(0) <= age <= MAX_PLAN_AGE, "PLAN_EXPIRED_OR_FUTURE")
    require(plan.get("source_fingerprint") == source_fingerprint, "SOURCE_FINGERPRINT_CHANGED")
    require(type(plan.get("source_file_count")) is int and plan["source_file_count"] > 0, "SOURCE_FILE_COUNT_INVALID")
    require(isinstance(plan["migrations"], dict) and set(plan["migrations"]) == set(DATABASES), "MIGRATION_GRAPH_INVALID")
    for row in plan["migrations"].values():
        require(isinstance(row, dict) and set(row) == {"head", "revisions"} and isinstance(row["revisions"], list)
            and row["head"] in row["revisions"] and len(set(row["revisions"])) == len(row["revisions"])
            and all(isinstance(revision, str) and REVISION.fullmatch(revision) for revision in row["revisions"]), "MIGRATION_GRAPH_INVALID")
    require(plan.get("target") == {"host": ops.HOST, "port": SSH_PORT, "live_root": ops.LIVE, "project": PROJECT}, "PLAN_TARGET_INVALID")
    require(plan.get("backup_scope") == backup_scope(learn_only=learn_only, auth_learn_frontend=alf, auth_learn_community_frontend=alcf, learn_frontend=lf), "PLAN_BACKUP_SCOPE_INVALID")
    validate_inspection(plan["inspection"], plan["migrations"])
    if code_only:
        validate_code_contract(plan["learn_code_contract"], plan["inspection"], plan["migrations"])
    if alf:
        validate_alf_contract(plan["alf_contract"], plan["inspection"], plan["migrations"])
    if alcf:
        validate_alcf_contract(plan["alcf_contract"], plan["inspection"], plan["migrations"])
    if lf:
        validate_lf_contract(plan["lf_contract"], plan["inspection"], plan["migrations"])


def assert_renewal_not_started(nonce):
    evidence = ops.TASK / "evidence"
    patterns = (f"image-backup-*-{nonce}.json", f"backup-*-{nonce}.json",
                f"quiesce-*-{nonce}.json", f"activate-*-{nonce}.json", f"quiesce-active-{nonce}.lock")
    require(not any(next(evidence.glob(pattern), None) is not None for pattern in patterns),
            "PLAN_RENEWAL_AFTER_MUTATION_FORBIDDEN")
    private = ops.TASK / "runtime" / "private-backups"
    require(not any(path.exists() or path.is_symlink() for path in (private / nonce, private / (nonce + "-images"))),
            "PLAN_RENEWAL_AFTER_BACKUP_FORBIDDEN")


def load_renewal_plan(path, expected_hash, source, migrations):
    require(isinstance(expected_hash, str) and HASH.fullmatch(expected_hash), "RENEW_PLAN_SHA256_REQUIRED")
    path = task_path(path, ops.TASK / "evidence")
    payload = path.read_bytes()
    require(sha256(payload) == expected_hash, "RENEW_PLAN_SHA256_MISMATCH")
    prior = parse_json(payload)
    require(isinstance(prior, dict), "PLAN_SCHEMA_INVALID")
    created = timestamp(prior.get("created_at"))
    # Only the prior's structure is reusable; a new actual inspection below is
    # mandatory. No consumer receives an extended age or rewritten old plan.
    validate_plan(prior, source["fingerprint"], now=created)
    require(created <= datetime.now(timezone.utc), "RENEW_PLAN_FUTURE")
    require(prior["source_file_count"] == len(source["files"]), "SOURCE_FILE_COUNT_CHANGED")
    require(prior["migrations"] == migrations, "SOURCE_MIGRATION_GRAPH_CHANGED")
    assert_renewal_not_started(prior["nonce"])
    return prior


def renewal_static_inspection(inspection):
    return {key: inspection[key] for key in ("live_root", "project", "compose_sha256", "protected_files", "code_files")} | {
        "containers": [{key: row[key] for key in ("service", "container_id", "image_id", "running", "pid", "status",
            "started_at", "finished_at", "restart_count", "health")} | {
                "mounts": sorted(row["mounts"], key=lambda mount: (mount["destination"], mount["type"], mount["source"], mount["read_only"]))}
            for row in inspection["containers"]],
        "databases": {name: {key: row[key] for key in ("database", "head", "schema_sha256", "table_count")}
                      for name, row in inspection["databases"].items()}}


def validate_renewal_inspection(prior, inspection, migrations):
    validate_inspection(inspection, migrations)
    require(renewal_static_inspection(prior["inspection"]) == renewal_static_inspection(inspection),
            "RENEW_PLAN_IMMUTABLE_LIVE_STATE_CHANGED")
    assert_renewal_not_started(prior["nonce"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="new plan path under this task's evidence directory")
    parser.add_argument("--renew-plan", type=Path, help="prior same-source plan; only renewable before backup/stop/activation")
    parser.add_argument("--renew-plan-sha256")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--learn-only", action="store_true")
    scope.add_argument("--auth-learn-frontend", action="store_true")
    scope.add_argument("--auth-learn-community-frontend", action="store_true")
    scope.add_argument("--learn-frontend", action="store_true")
    parser.add_argument("--learn-code-only", action="store_true", help="explicit 029-to-029 code update; requires --learn-only and a verified baseline chain")
    parser.add_argument("--baseline-release", nargs=3, action="append", type=Path, metavar=("ACTIVATION", "BUILD", "VERIFY"))
    args = parser.parse_args(argv)
    require(not args.learn_code_only or args.learn_only and args.baseline_release, "LEARN_CODE_BASELINE_REQUIRED")
    require(not args.auth_learn_frontend or not args.learn_only and not args.learn_code_only and args.baseline_release, "ALF_BASELINE_REQUIRED")
    require(not args.auth_learn_community_frontend or not args.learn_code_only and args.baseline_release, "ALCF_BASELINE_REQUIRED")
    require(not args.learn_frontend or not args.learn_code_only and args.baseline_release, "LF_BASELINE_REQUIRED")
    require(not args.baseline_release or args.learn_code_only or args.auth_learn_frontend or args.auth_learn_community_frontend or args.learn_frontend, "BASELINE_SCOPE_INVALID")
    require((args.renew_plan is None) == (args.renew_plan_sha256 is None), "RENEW_PLAN_INPUT_PAIR_REQUIRED")
    destination = task_path(args.plan, ops.TASK / "evidence")
    require(not destination.exists(), "PLAN_ALREADY_EXISTS")
    source = ops.manifest()
    migrations = source_migrations()
    prior = load_renewal_plan(args.renew_plan, args.renew_plan_sha256, source, migrations) if args.renew_plan is not None else None
    require(prior is None or prior.get("learn_only", False) is args.learn_only, "RENEW_PLAN_SCOPE_CHANGED")
    require(prior is None or code_only_phase(prior) is args.learn_code_only, "RENEW_PLAN_PHASE_CHANGED")
    require(prior is None or alf_phase(prior) is args.auth_learn_frontend, "RENEW_PLAN_SCOPE_CHANGED")
    require(prior is None or alcf_phase(prior) is args.auth_learn_community_frontend, "RENEW_PLAN_SCOPE_CHANGED")
    require(prior is None or lf_phase(prior) is args.learn_frontend, "RENEW_PLAN_SCOPE_CHANGED")
    chain = None
    if args.learn_code_only or args.auth_learn_frontend or args.auth_learn_community_frontend or args.learn_frontend:
        import verify_release
        chain = verify_release.load_baseline_chain(args.baseline_release)
        verify_release.retained_source_contract(chain["contract"], source["files"], learn_only=args.learn_only, auth_learn_frontend=args.auth_learn_frontend, auth_learn_community_frontend=args.auth_learn_community_frontend, learn_frontend=args.learn_frontend)
    inspection = ops.remote_python(INSPECTOR, timeout=900)
    if isinstance(inspection, dict) and inspection.get("verified") is not True:
        stage = inspection.get("unverified_stage")
        if stage in {"compose", "protected", "containers", "databases", "code", "objects"}:
            raise ReleaseError("REMOTE_INSPECTION_UNVERIFIED_" + stage.upper())
    require(ops.manifest()["fingerprint"] == source["fingerprint"], "SOURCE_CHANGED_DURING_INSPECTION")
    if prior is not None:
        validate_renewal_inspection(prior, inspection, migrations)
    code_contract = make_code_contract(source["files"], inspection, migrations, chain["contract"], chain["hashes"]) if args.learn_code_only else None
    alf_contract = make_alf_contract(source["files"], inspection, migrations, chain["contract"], chain["hashes"]) if args.auth_learn_frontend else None
    alcf_contract = make_alcf_contract(source["files"], inspection, migrations, chain["contract"], chain["hashes"]) if args.auth_learn_community_frontend else None
    lf_contract = make_lf_contract(source["files"], inspection, migrations, chain["contract"], chain["hashes"]) if args.learn_frontend else None
    require(prior is None or prior.get("learn_code_contract") == code_contract, "RENEW_PLAN_CODE_BASELINE_CHANGED")
    require(prior is None or prior.get("alf_contract") == alf_contract, "RENEW_PLAN_ALF_BASELINE_CHANGED")
    require(prior is None or prior.get("alcf_contract") == alcf_contract, "RENEW_PLAN_ALCF_BASELINE_CHANGED")
    require(prior is None or prior.get("lf_contract") == lf_contract, "RENEW_PLAN_LF_BASELINE_CHANGED")
    value = make_plan(source, inspection, migrations, nonce=prior["nonce"] if prior is not None else None,
                      learn_only=args.learn_only, code_contract=code_contract, alf_contract=alf_contract, alcf_contract=alcf_contract, lf_contract=lf_contract)
    digest = exclusive_json(destination, value)
    receipt_name = f"release-preflight-{value['nonce']}" + (f"-{digest[:12]}" if prior is not None else "") + ".json"
    details = {"schema_version": 1,
        "plan": str(destination), "plan_sha256": digest, "source_fingerprint": source["fingerprint"],
        "nonce": value["nonce"], "database_count": 3, "planned_backup_database_count": 2 if args.auth_learn_frontend else 1 if args.learn_only or args.learn_frontend else 3,
        "code_file_count": len(inspection["code_files"]),
        "protected_file_count": len(inspection["protected_files"]), "cross_service_consistency_verified": False}
    if prior is not None:
        details.update({"renewed_from_plan": str(args.renew_plan.absolute()), "renewed_from_plan_sha256": args.renew_plan_sha256,
                        "fresh_inspection_verified": True, "prior_receipts_modified": False})
    receipt = ops.receipt(receipt_name, details)
    print(json.dumps({"plan": str(destination), "plan_sha256": digest, "receipt": str(receipt), "source_fingerprint": source["fingerprint"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__}))
        sys.exit(1)
