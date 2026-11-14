"""Pure verification contracts; no SSH, Docker, database or service calls."""
from __future__ import annotations

import base64
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import verify_release as verify


def digest(name):
    return hashlib.sha256(name.encode()).hexdigest()


def row(name):
    return {"path": name, "bytes": 10, "sha256": digest(name)}


def category_projection(**changes):
    return {"registry_present": True, "audit_present": True, "registry_fk_present": True, "legacy_check_absent": True,
            "custom_category_count": 0, "custom_event_count": 0, **changes}


def fixture():
    binding = verify.binding_for("a" * 40, "b" * 64, "renewal-test-001")
    images = {name: "sha256:" + digest(name) for name in ("auth", "learn", "community", "frontend")}
    managed = verify.file_rows([row(name + "Dockerfile") for name in verify.CONTEXTS])
    unmanaged = verify.file_rows([row("infra/docker-compose.yml"), row("docs/design/existing.md")])
    protected = verify.file_rows([row("/home/deploy/CTF_Web/infra/.env")], absolute=True)
    containers = []
    for service, image in verify.SERVICE_IMAGES.items():
        containers.append({"service": service, "container_id": digest(service), "image_id": images[image],
                           "running": True, "health": None if service in verify.LEGACY_PROCESS_WORKERS else "healthy", "restart_count": 0,
                           "pid":100,"started_at":"2026-10-13T06:00:00Z",
                           **({"command":["python","-m",verify.LEGACY_PROCESS_WORKERS[service]]} if service in verify.LEGACY_PROCESS_WORKERS else {})})
    containers.append({"service": "postgres", "container_id": digest("postgres"), "image_id": "sha256:" + digest("postgres-image"),
                       "running": True, "health": "healthy", "restart_count": 0})
    containers.sort(key=lambda item: (item["service"], item["container_id"]))
    request = {"binding": binding, "images": images, "managed_files": managed, "unmanaged_files": unmanaged,
               "frontend_args_sha256":digest("frontend-build-args"),
               "protected_files": protected, "containers_before": verify.container_rows(containers),
               "image_bindings": {name: binding for name in images}, "changed_services": sorted(set(verify.SERVICE_IMAGES) | set(verify.MIGRATION_IMAGES))}
    snapshot = {"containers": containers, "code_files": verify.file_rows(managed + unmanaged), "protected_files": protected,"frontend_args_sha256":request["frontend_args_sha256"]}
    labels = {"org.opencontainers.image.revision": binding["commit"], "io.ctf-web.source-fingerprint": binding["source_fingerprint"],
              "io.ctf-web.release-nonce": binding["release_nonce"]}
    for current in containers:
        current["source_labels"] = dict(labels)
    image_labels = {value: dict(labels) for value in images.values()}
    image_labels[images["frontend"]][verify.FRONTEND_ARGS_LABEL] = request["frontend_args_sha256"]
    databases = {name: [head] for name, head in verify.EXPECTED_HEADS.items()}
    triggers = [{"table": name, "enabled": "O", "function": "bump_knowledge_graph_revision", "type": 28} for name in verify.TRIGGER_TABLES]
    return request, snapshot, image_labels, databases, triggers


def receipts(request, snapshot):
    binding = request["binding"]
    remote = {"schema_version": 1, "kind": "renewal-stage-build", "binding": binding, "status": "built-not-activated",
              "live_state_unchanged": True, "runtime_readability_verified": True, "live_before": snapshot, "live_after": snapshot,
              "completed_images": [{"service": name, "image_id": image, "tag": f"ctf-renewal/{name}:{binding['commit']}-{binding['release_nonce']}"}
                                   for name, image in request["images"].items()]}
    build = {"schema_version": 1, "kind": "renewal-stage-build", "binding": binding, "status": "built-not-activated", "remote": remote}
    next(row for row in remote["completed_images"] if row["service"] == "frontend")["frontend_args_sha256"] = request["frontend_args_sha256"]
    activation = {"schema_version": 1, "kind": "renewal-activate", "binding": binding, "status": "activated",
                  **{key: request[key] for key in ("managed_files", "unmanaged_files", "protected_files", "containers_before")}}
    return build, activation


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.request, self.snapshot, self.images, self.databases, self.triggers = fixture()

    def evaluate(self, **changes):
        return verify.evaluate_snapshot(self.request, changes.get("before", self.snapshot), changes.get("after", self.snapshot),
                                        changes.get("images", self.images), changes.get("databases", self.databases), changes.get("triggers", self.triggers))

    def test_complete_contract_passes_without_claiming_public_product_acceptance(self):
        value = self.evaluate()
        self.assertEqual(value["status"], "passed")
        self.assertIn("public_product_http_and_browser", value["unverified_fields"])
        self.assertEqual(verify.validate_result(value, self.request["binding"]), value)
        self.assertNotIn("POSTGRES_PASSWORD", json.dumps(value))
        self.assertNotIn("containers", value)

    def test_binding_rejects_command_fragments_and_old_source_shapes(self):
        for args in [("a;exit", "b" * 64, "renewal-test-001"), ("a" * 40, "B" * 64, "renewal-test-001"),
                     ("a" * 40, "b" * 64, "../release"), ("a" * 40, "b" * 64, "small")]:
            with self.assertRaises(verify.VerifyError):
                verify.binding_for(*args)

    def test_file_inventory_rejects_escape_secret_duplicate_and_unknown_fields(self):
        for name in ("../file", "services/auth/.env", "frontend/private.pem", "frontend\\file", "frontend//file"):
            with self.assertRaises(verify.VerifyError):
                verify.file_rows([row(name)])
        for rows in ([row("frontend/file"), row("frontend/file")], [{**row("frontend/file"), "value": "secret"}],
                     [{**row("frontend/file"), "bytes": True}]):
            with self.assertRaises(verify.VerifyError):
                verify.file_rows(rows)

    def test_build_and_activation_bind_exact_image_and_managed_rows(self):
        build, activation = receipts(self.request, self.snapshot)
        built = verify.build_contract(build, self.request["binding"])
        result = verify.activation_contract(activation, self.request["binding"], self.request["managed_files"], built)
        self.assertEqual(result, self.request)
        for key, wrong in (("status", "partial-unverified"), ("binding", {})):
            bad = copy.deepcopy(build)
            bad[key] = wrong
            with self.assertRaises(verify.VerifyError):
                verify.build_contract(bad, self.request["binding"])
        bad = copy.deepcopy(build)
        bad["remote"]["completed_images"][0]["tag"] = "latest"
        with self.assertRaises(verify.VerifyError):
            verify.build_contract(bad, self.request["binding"])
        for proof in (None, False, "true", 1):
            bad = copy.deepcopy(build)
            bad["remote"]["runtime_readability_verified"] = proof
            with self.assertRaises(verify.VerifyError):
                verify.build_contract(bad, self.request["binding"])
        bad = copy.deepcopy(activation)
        bad["managed_files"] = bad["managed_files"][:-1]
        with self.assertRaises(verify.VerifyError):
            verify.activation_contract(bad, self.request["binding"], self.request["managed_files"], built)

    def test_source_protected_and_unmanaged_changes_fail_separately(self):
        for key, check in (("code_files", "managed_files"), ("protected_files", "protected_files")):
            before = copy.deepcopy(self.snapshot)
            target = next(item for item in before[key] if key != "code_files" or item["path"].startswith(verify.CONTEXTS))
            target["sha256"] = "f" * 64
            self.assertEqual(self.evaluate(before=before)["checks"][check]["status"], "failed")
        before = copy.deepcopy(self.snapshot)
        before["code_files"].append(row("docs/design/unexpected.md"))
        self.assertEqual(self.evaluate(before=before)["checks"]["unmanaged_files"]["status"], "failed")

    def test_untargeted_and_during_read_container_changes_fail(self):
        before = copy.deepcopy(self.snapshot)
        next(item for item in before["containers"] if item["service"] == "postgres")["image_id"] = "sha256:" + "f" * 64
        self.assertEqual(self.evaluate(before=before)["checks"]["untargeted_containers"]["status"], "failed")
        after = copy.deepcopy(self.snapshot)
        after["containers"][0]["container_id"] = "f" * 64
        self.assertEqual(self.evaluate(after=after)["checks"]["readonly_container_identity"]["status"], "failed")
        after = copy.deepcopy(self.snapshot)
        after["containers"][0]["restart_count"] = 1
        self.assertEqual(self.evaluate(after=after)["checks"]["readonly_health"]["status"], "failed")

    def test_all_workers_must_use_exact_source_bound_image_and_be_healthy(self):
        for service in verify.SERVICE_IMAGES:
            before = copy.deepcopy(self.snapshot)
            current = next(item for item in before["containers"] if item["service"] == service)
            current["image_id"] = "sha256:" + "f" * 64
            self.assertEqual(self.evaluate(before=before)["checks"]["api_frontend_worker_images"]["status"], "failed", service)
            before = copy.deepcopy(self.snapshot)
            next(item for item in before["containers"] if item["service"] == service)["health"] = "unhealthy" if service in verify.LEGACY_PROCESS_WORKERS else None
            check = "legacy_worker_process_stability" if service in verify.LEGACY_PROCESS_WORKERS else "target_health"
            self.assertEqual(self.evaluate(before=before)["checks"][check]["status"], "failed", service)
        images = copy.deepcopy(self.images)
        next(iter(images.values()))["io.ctf-web.source-fingerprint"] = "f" * 64
        self.assertEqual(self.evaluate(images=images)["checks"]["image_source_labels"]["status"], "failed")
        before = copy.deepcopy(self.snapshot)
        before["containers"][0]["source_labels"] = {}
        self.assertEqual(self.evaluate(before=before)["checks"]["container_source_labels"]["status"], "failed")

    def test_schema_heads_and_exact_eighteen_enabled_statement_triggers(self):
        for name in self.databases:
            databases = copy.deepcopy(self.databases)
            databases[name] = ["old"]
            self.assertEqual(self.evaluate(databases=databases)["checks"]["database_heads"]["status"], "failed")
        for key, value in (("enabled", "D"), ("function", "wrong"), ("type", 29), ("table", "wrong")):
            triggers = copy.deepcopy(self.triggers)
            triggers[0][key] = value
            self.assertEqual(self.evaluate(triggers=triggers)["checks"]["learn_graph_revision_triggers"]["status"], "failed")
        self.assertEqual(self.evaluate(triggers=self.triggers[:-1])["checks"]["learn_graph_revision_triggers"]["status"], "failed")

    def test_unexpected_remote_secret_fields_and_false_pass_are_rejected(self):
        value = self.evaluate()
        for bad in ({**value, "Env": ["SECRET=do-not-log"]}, {**value, "checks": {}}, {**value, "unverified_fields": []}):
            with self.assertRaises(verify.VerifyError):
                verify.validate_result(bad, self.request["binding"])
        value["checks"]["target_health"]["status"] = "failed"
        with self.assertRaises(verify.VerifyError):
            verify.validate_result(value, self.request["binding"])

    def test_default_cli_is_local_plan_and_never_calls_ssh_or_writes_receipt(self):
        build, activation = receipts(self.request, self.snapshot)
        manifest = {"fingerprint": self.request["binding"]["source_fingerprint"], "files": self.request["managed_files"]}
        output = io.StringIO()
        with patch.object(verify.ops, "manifest", return_value=manifest), patch.object(verify, "load_receipt", side_effect=[(build, "c" * 64), (activation, "d" * 64)]), \
                patch.object(verify.ops, "ssh", side_effect=AssertionError("SSH forbidden")) as ssh, \
                patch.object(verify.ops, "receipt", side_effect=AssertionError("write forbidden")) as write, redirect_stdout(output):
            self.assertEqual(verify.main(["--commit", "a" * 40, "--source-fingerprint", "b" * 64, "--release-nonce", "renewal-test-001",
                                          "--build-receipt", "mock-build", "--activation-receipt", "mock-activation"]), 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value["execute"])
        self.assertIn("remote_state_not_read", value["unverified_fields"])
        ssh.assert_not_called()
        write.assert_not_called()

    def test_full_029_main_passes_bound_heads_into_assembled_readonly_verifier(self):
        for learn_head in (verify.EXPECTED_HEADS["learn"], verify.LEARN_CATEGORY_HEAD):
            heads = {**verify.EXPECTED_HEADS, "learn": learn_head}
            databases = {name: [head] for name, head in heads.items()}
            build, activation = receipts(self.request, self.snapshot)
            activation["schema"] = {"heads": databases}
            manifest = {"fingerprint": self.request["binding"]["source_fingerprint"], "files": self.request["managed_files"]}
            captured = []
            def readonly_remote(command, *, timeout, stdin):
                request = json.loads(stdin)
                program = base64.b64decode(request.pop("program")).decode()
                captured.append(request)
                namespace = {"REQUEST": request}
                exec(compile(program.rsplit("verify_main()", 1)[0], "<full-head-readonly-verifier>", "exec"), namespace)
                def run(selected, limit=120):
                    if selected[:3] == ["docker", "image", "inspect"]:
                        return json.dumps([{"Id": selected[3], "Config": {"Labels": self.images[selected[3]]}}]).encode()
                    self.assertEqual(selected[:2], ["docker", "exec"])
                    database, query = selected[-2:]
                    self.assertTrue(query.startswith("SELECT "))
                    if "public.alembic_version" in query:
                        family = next(name for name, value in verify.release_preflight.DATABASES.items() if value == database)
                        return json.dumps(databases[family]).encode()
                    if "FROM pg_trigger" in query: return json.dumps(self.triggers).encode()
                    if "registry_present" in query: return json.dumps(category_projection()).encode()
                    raise AssertionError("UNEXPECTED_READONLY_COMMAND")
                namespace.update(verify_snapshot=lambda: copy.deepcopy(self.snapshot), run=run)
                with patch("time.sleep"), redirect_stdout(io.StringIO()) as output:
                    namespace["verify_main"]()
                return output.getvalue().encode()
            args = ["--commit", self.request["binding"]["commit"], "--source-fingerprint", manifest["fingerprint"],
                    "--release-nonce", self.request["binding"]["release_nonce"], "--build-receipt", "mock-build",
                    "--activation-receipt", "mock-activation", "--execute"]
            with self.subTest(learn_head=learn_head), patch.object(verify.ops, "manifest", return_value=manifest), \
                    patch.object(verify, "load_receipt", side_effect=[(build, "c" * 64), (activation, "d" * 64)]), \
                    patch.object(verify.ops, "ssh", side_effect=readonly_remote), \
                    patch.object(verify.ops, "receipt", return_value=Path("mock-receipt")) as write, redirect_stdout(io.StringIO()):
                self.assertEqual(verify.main(args), 0)
            self.assertEqual(captured[0]["expected_heads"], heads)
            self.assertEqual(write.call_args.args[1]["status"], "passed")
            checks = write.call_args.args[1]["checks"]
            self.assertEqual(checks["database_heads"]["status"], "passed")
            self.assertEqual("event_category_registry" in checks, learn_head == verify.LEARN_CATEGORY_HEAD)

    def test_full_029_registry_contract_does_not_implicitly_enforce_defaults_only(self):
        build, activation = receipts(self.request, self.snapshot)
        request = verify.activation_contract(activation, self.request["binding"], self.request["managed_files"],
                                             verify.build_contract(build, self.request["binding"]))
        self.assertNotIn("category_defaults_only", request)
        request["expected_heads"] = {**verify.EXPECTED_HEADS, "learn": verify.LEARN_CATEGORY_HEAD}
        databases = {name: [head] for name, head in request["expected_heads"].items()}
        def evaluate(state):
            return verify.evaluate_snapshot(request, self.snapshot, self.snapshot, self.images, databases, self.triggers, state)
        self.assertEqual(evaluate(category_projection())["status"], "passed")
        self.assertEqual(evaluate(category_projection(custom_category_count=1, custom_event_count=1))["status"], "passed")
        for field in ("registry_present", "audit_present", "registry_fk_present", "legacy_check_absent"):
            with self.subTest(field=field):
                self.assertEqual(evaluate(category_projection(**{field: False}))["checks"]["event_category_registry"]["status"], "failed")
        request["category_defaults_only"] = True
        self.assertEqual(evaluate(category_projection())["status"], "passed")
        for field in ("custom_category_count", "custom_event_count"):
            with self.subTest(field=field):
                self.assertEqual(evaluate(category_projection(**{field: 1}))["checks"]["event_category_registry"]["status"], "failed")

    def test_remote_program_compiles_and_db_queries_are_read_only(self):
        program = verify.remote_program()
        compile(program, "<test-only>", "exec")
        command, payload = verify.execution_payload(self.request)
        self.assertTrue(command.startswith("python3 -c "))
        self.assertIn(b'"program"', payload)
        self.assertIn("default_transaction_read_only=on", verify.REMOTE)
        self.assertNotIn("alembic upgrade", verify.REMOTE)
        self.assertNotIn("docker compose up", verify.REMOTE)
        self.assertNotIn("docker restart", verify.REMOTE)

    def test_mock_execution_rejects_remote_secret_payload_and_local_source_drift(self):
        build, activation = receipts(self.request, self.snapshot)
        manifest = {"fingerprint": self.request["binding"]["source_fingerprint"], "files": self.request["managed_files"]}
        args = ["--commit", "a" * 40, "--source-fingerprint", "b" * 64, "--release-nonce", "renewal-test-001",
                "--build-receipt", "mock-build", "--activation-receipt", "mock-activation", "--execute"]
        for payload, manifests, reason in (({**self.evaluate(), "Env": "SECRET=do-not-log"}, [manifest] * 3, "remote_read_failed"),
                                            (self.evaluate(), [manifest, manifest, {**manifest, "fingerprint": "f" * 64}], "source_changed_during_verification")):
            output = io.StringIO()
            with patch.object(verify.ops, "manifest", side_effect=manifests), patch.object(verify, "load_receipt", side_effect=[(build, "c" * 64), (activation, "d" * 64)]), \
                    patch.object(verify.ops, "ssh", return_value=json.dumps(payload).encode()), \
                    patch.object(verify.ops, "receipt", return_value=Path("mock-receipt")) as write, redirect_stdout(output):
                self.assertEqual(verify.main(args), 1)
            saved = write.call_args.args[1]
            self.assertEqual(saved["status"], "unverified")
            self.assertIn(reason, saved["unverified_fields"])
            self.assertNotIn("do-not-log", json.dumps(saved))
            self.assertNotIn("do-not-log", output.getvalue())

    def test_unknown_extra_within_managed_context_is_preserved_by_exact_path(self):
        build, activation = receipts(self.request, self.snapshot)
        activation["unmanaged_files"] = verify.file_rows(activation["unmanaged_files"] + [row("frontend/unknown-existing.txt")])
        request = verify.activation_contract(activation, self.request["binding"], self.request["managed_files"], verify.build_contract(build, self.request["binding"]))
        self.assertIn(row("frontend/unknown-existing.txt"), request["unmanaged_files"])
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["code_files"] = verify.file_rows(snapshot["code_files"] + [row("frontend/unknown-existing.txt")])
        self.assertEqual(verify.evaluate_snapshot(request, snapshot, snapshot, self.images, self.databases, self.triggers)["status"], "passed")

    def test_frontend_only_uses_baseline_backend_images_labels_rows_and_ids(self):
        new_binding = verify.binding_for("c" * 40, "d" * 64, "renewal-test-002")
        build, activation = receipts(self.request, self.snapshot)
        build["binding"] = build["remote"]["binding"] = activation["binding"] = new_binding
        build["remote"]["frontend_only"] = True
        new_frontend_image = "sha256:" + digest("new-frontend")
        build["remote"]["completed_images"] = [{"service": "frontend", "image_id": new_frontend_image,
            "frontend_args_sha256":self.request["frontend_args_sha256"],
            "tag": f"ctf-renewal/frontend:{new_binding['commit']}-{new_binding['release_nonce']}"}]
        build["remote"]["backend_baseline"] = {"binding": self.request["binding"], "images": {name: self.request["images"][name] for name in ("auth", "learn", "community")}}
        frontend_rows = [{**current, "sha256": "f" * 64} for current in self.request["managed_files"] if current["path"].startswith("frontend/")]
        activation.update({"frontend_only": True, "managed_files": frontend_rows,
            "unmanaged_files": verify.file_rows(self.request["unmanaged_files"] + [current for current in self.request["managed_files"] if not current["path"].startswith("frontend/")])})
        built = verify.build_contract(build, new_binding, frontend_only=True, baseline=self.request)
        request = verify.activation_contract(activation, new_binding, frontend_rows, built, baseline=self.request)
        snapshot = copy.deepcopy(self.snapshot)
        frontend = next(current for current in snapshot["containers"] if current["service"] == "frontend")
        frontend.update({"container_id": digest("new-frontend-container"), "image_id": new_frontend_image,
            "source_labels": {"org.opencontainers.image.revision": new_binding["commit"], "io.ctf-web.source-fingerprint": new_binding["source_fingerprint"], "io.ctf-web.release-nonce": new_binding["release_nonce"]}})
        snapshot["code_files"] = verify.file_rows(request["managed_files"] + request["unmanaged_files"])
        images = {key: value for key, value in self.images.items() if key != self.request["images"]["frontend"]}
        images[new_frontend_image] = {**frontend["source_labels"],verify.FRONTEND_ARGS_LABEL:request["frontend_args_sha256"]}
        result = verify.evaluate_snapshot(request, snapshot, snapshot, images, self.databases, self.triggers)
        self.assertEqual(result["status"], "passed")
        snapshot["containers"][0]["container_id"] = digest("unexpected-backend-recreated")
        result = verify.evaluate_snapshot(request, snapshot, snapshot, images, self.databases, self.triggers)
        self.assertEqual(result["checks"]["untargeted_containers"]["status"], "failed")
        bad = copy.deepcopy(build)
        bad["remote"]["backend_baseline"]["images"]["auth"] = "sha256:" + "f" * 64
        with self.assertRaises(verify.VerifyError):
            verify.build_contract(bad, new_binding, frontend_only=True, baseline=self.request)

    def test_frontend_args_hash_is_bound_to_build_receipt_and_actual_image_label(self):
        build,_ = receipts(self.request,self.snapshot)
        bad = copy.deepcopy(build)
        next(row for row in bad["remote"]["completed_images"] if row["service"] == "frontend")["frontend_args_sha256"] = "0"*64
        with self.assertRaisesRegex(verify.VerifyError,"BUILD_FRONTEND_ARGS_HASH_UNVERIFIED"):
            verify.build_contract(bad,self.request["binding"])
        images = copy.deepcopy(self.images)
        images[self.request["images"]["frontend"]][verify.FRONTEND_ARGS_LABEL] = "0"*64
        self.assertEqual(self.evaluate(images=images)["checks"]["image_source_labels"]["status"],"failed")

    def test_legacy_workers_are_process_stable_not_reported_as_ready(self):
        result = self.evaluate()
        self.assertEqual(result["checks"]["target_health"]["count"],8)
        self.assertEqual(result["checks"]["legacy_worker_process_stability"],{"status":"passed","count":4})
        self.assertIn("legacy_learn_workers_readiness_not_available",result["unverified_fields"])
        for field,value in (("pid",0),("started_at","changed"),("restart_count",1),("command",["python","-m","unknown"])):
            after = copy.deepcopy(self.snapshot)
            next(row for row in after["containers"] if row["service"] in verify.LEGACY_PROCESS_WORKERS)[field] = value
            self.assertEqual(self.evaluate(after=after)["checks"]["legacy_worker_process_stability"]["status"],"failed")

    def test_frontend_only_requires_bound_phase_one_verify_pass(self):
        value = {**self.evaluate(), "local_source_unchanged": True, "recorded_at": "2026-10-13T06:00:00+00:00",
            "build_receipt_sha256": "c" * 64, "activation_receipt_sha256": "d" * 64, "frontend_only": False, "secret_values_recorded": False}
        verify.baseline_verify_contract(value, self.request["binding"], "c" * 64, "d" * 64)
        for key, wrong in (("status", "unverified"), ("frontend_only", True), ("local_source_unchanged", False), ("build_receipt_sha256", "f" * 64)):
            with self.assertRaises(verify.VerifyError):
                verify.baseline_verify_contract({**value, key: wrong}, self.request["binding"], "c" * 64, "d" * 64)
        with patch.object(verify.ops, "manifest", side_effect=AssertionError("must reject before source reads")):
            with self.assertRaises(verify.VerifyError):
                verify.main(["--commit", "a" * 40, "--source-fingerprint", "b" * 64, "--release-nonce", "renewal-test-001",
                    "--build-receipt", "mock-build", "--activation-receipt", "mock-activation", "--frontend-only"])


if __name__ == "__main__":
    unittest.main()
