"""Pure narrow-release contracts. No SSH, database, Docker or ACL access."""
from __future__ import annotations

import copy
import ast
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activate_release as activate
import backup_release as backup
import quiesce_release as quiesce
import release_preflight as preflight
import stage_build_release as stage
import verify_release as verify
from test_release_backup import fixtures
from test_verify_release import category_projection, fixture, receipts, digest, row


def dependency_fixture():
    request, snapshot, _, _, _ = fixture()
    baseline = {**request, "binding": request["binding"]}
    for item in snapshot["containers"]:
        item["mounts"] = []
        if item["service"] in activate.LEARN_WRITERS:
            item.update(running=False, pid=0)
        if item["service"] == activate.PERSONAL_WORKER:
            item.update(health="unhealthy", command=list(activate.PERSONAL_WORKER_COMMAND))
    frozen = copy.deepcopy(snapshot["containers"])
    for item in frozen:
        item.pop("command", None)
    next(item for item in frozen if item["service"] == activate.PERSONAL_WORKER)["health"] = "healthy"
    snapshot["source_labels"] = {item["container_id"]: activate.source_labels(request["binding"])
                                 for item in snapshot["containers"]}
    probe = {"health_status": 200, "ready_status": 503, "baseline_complete": True}
    return snapshot, baseline, frozen, probe


def recovered_dependency_evidence():
    snapshot, baseline, frozen, status = dependency_fixture()
    value = activate.check_backend_baseline(snapshot, baseline, learn_only=True, frozen=frozen, probe=lambda _: status)
    value["recovery"] = {"identity": copy.deepcopy(value["original_identity"]),
        "command": list(activate.PERSONAL_WORKER_COMMAND), "source_labels": copy.deepcopy(value["source_labels"]),
        "docker_health": "healthy", "health_status": 200, "ready_status": 200, "baseline_complete": True,
        "observations": 2, "elapsed_seconds": 2.5, "maximum_wait_seconds": 180}
    request = {"learn_only": True, "frontend_only": False, "backend_baseline": baseline,
               "current_container_baseline": frozen}
    return value, request


def baseline_chain(*, full_learn_head=None, families=(None, "frontend", "learn", "frontend"), full_images=None):
    request, snapshot, image_labels, databases, triggers = fixture()
    if full_images is not None:
        prior_images = request["images"]
        replacements = {prior_images[name]: full_images[name] for name in prior_images}
        request["images"] = dict(full_images)
        for item in snapshot["containers"]:
            item["image_id"] = replacements.get(item["image_id"], item["image_id"])
        image_labels = {replacements.get(image, image): labels for image, labels in image_labels.items()}
    if full_learn_head is not None:
        request["expected_heads"] = {**verify.EXPECTED_HEADS, "learn": full_learn_head}
        databases["learn"] = [full_learn_head]
    full_build, full_activation = receipts(request, snapshot)
    full_activation["schema"] = {"heads": databases}
    categories = category_projection() if databases["learn"] == [verify.LEARN_CATEGORY_HEAD] else None
    result = verify.evaluate_snapshot(request, snapshot, snapshot, image_labels, databases, triggers, categories)
    inventory = {}
    triples, hashes = [], []
    baseline = None
    for index, family in enumerate(families):
        binding = verify.binding_for(format(index + 1, "040x"), format(index + 2, "064x"), "chain-test-" + str(index))
        if family is None:
            binding = request["binding"]
            build, activation = copy.deepcopy(full_build), copy.deepcopy(full_activation)
        else:
            build = copy.deepcopy(full_build)
            build["binding"] = binding
            remote = build["remote"]
            remote.update({"binding": binding, "frontend_only": family == "frontend", "learn_only": family == "learn",
                           "retained_baseline": verify.baseline_summary(baseline, (family,))})
            built_row = {"service": family, "image_id": "sha256:" + digest("image-" + str(index)),
                         "tag": "ctf-renewal/" + family + ":" + binding["commit"] + "-" + binding["release_nonce"]}
            if family == "frontend":
                built_row["frontend_args_sha256"] = request["frontend_args_sha256"]
            remote["completed_images"] = [built_row]
            prefix = "frontend/" if family == "frontend" else "services/learn/"
            selected = [{**item, "sha256": digest("source-" + str(index))} for item in baseline["managed_files"] if item["path"].startswith(prefix)]
            retained = [item for item in baseline["managed_files"] if not item["path"].startswith(prefix)]
            heads = dict(baseline["expected_heads"])
            if family == "learn":
                heads["learn"] = verify.LEARN_CATEGORY_HEAD
            activation = {**copy.deepcopy(full_activation), "binding": binding, "frontend_only": family == "frontend", "learn_only": family == "learn",
                          "managed_files": selected, "unmanaged_files": verify.file_rows(retained + baseline["unmanaged_files"]),
                          "schema": {"heads": {name: [head] for name, head in heads.items()}}, "baseline_receipts": copy.deepcopy(hashes)}
            if family == "frontend" and heads["learn"] == verify.LEARN_CATEGORY_HEAD:
                activation["compatibility_image_backup_receipt_sha256"] = "e" * 64
        build_sha, activation_sha = verify.sha(verify.canonical(build)), verify.sha(verify.canonical(activation))
        checked = {**copy.deepcopy(result), "binding": binding, "local_source_unchanged": True,
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "build_receipt_sha256": build_sha,
                   "activation_receipt_sha256": activation_sha, "frontend_only": family == "frontend", "secret_values_recorded": False}
        if family is not None:
            checked.update({"learn_only": family == "learn", "baseline_receipts": copy.deepcopy(hashes)})
        if activation["schema"]["heads"]["learn"] == [verify.LEARN_CATEGORY_HEAD]:
            checked["checks"]["event_category_registry"] = {"status": "passed", "count": 0}
        checked_sha = verify.sha(verify.canonical(checked))
        triple = tuple(str(index) + name for name in ("-activation", "-build", "-verify"))
        for name, value, sha in zip(triple, (activation, build, checked), (activation_sha, build_sha, checked_sha)):
            inventory[name] = (value, sha)
        triples.append(triple)
        contract = verify.build_contract(build, binding, frontend_only=family == "frontend", learn_only=family == "learn", baseline=baseline)
        baseline = verify.activation_contract(activation, binding, activation["managed_files"], contract, baseline=baseline, learn_only=family == "learn")
        baseline["expected_heads"] = {name: value[0] for name, value in activation["schema"]["heads"].items()}
        hashes.append({"activation": activation_sha, "build": build_sha, "verify": checked_sha})
    return triples, inventory


def full_image_archive(binding, frontend_args_sha256):
    entries, manifest, images = [], [], {}
    for family in ("auth", "learn", "community", "frontend"):
        labels = activate.source_labels(binding)
        if family == "frontend": labels[verify.FRONTEND_ARGS_LABEL] = frontend_args_sha256
        payload = preflight.canonical({"config": {"Labels": labels, "Cmd": [family]}, "os": "linux", "architecture": "amd64"})
        config_digest = preflight.sha256(payload)
        images[family] = "sha256:" + config_digest
        config, layer = config_digest + ".json", "layers/" + family + "/layer.tar"
        entries.extend(((config, payload), (layer, b"test-owned-layer")))
        manifest.append({"Config": config, "RepoTags": ["fixture:" + family], "Layers": [layer]})
    entries.append(("manifest.json", preflight.canonical(manifest)))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, payload in entries:
            member = tarfile.TarInfo(name); member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return output.getvalue(), images


def learn_fence():
    source, migrations, plan, _, _, _ = fixtures()
    rows = plan["inspection"]["containers"]
    next(item for item in rows if item["service"] == "auth-personal-notifications")["service"] = "personal-notification-runner"
    learn = next(item for item in rows if item["service"] == "learn-service")
    for index, name in enumerate(verify.LEGACY_PROCESS_WORKERS, 50):
        item = copy.deepcopy(learn)
        item.update({"service": name, "container_id": format(index, "064x"), "health": None})
        rows.append(item)
    rows.sort(key=lambda item: (item["service"], item["container_id"]))
    plan["learn_only"] = True
    plan["backup_scope"] = preflight.backup_scope(learn_only=True)
    frozen = copy.deepcopy(plan["inspection"])
    targets = quiesce.action_targets(plan, "stop", learn_only=True)
    ids = {item["container_id"] for item in targets}
    for item in frozen["containers"]:
        if item["container_id"] in ids:
            item.update({"running": False, "pid": 0, "status": "exited", "finished_at": "2026-10-13T02:00:00Z"})
    frozen["databases"]["learn"]["client_session_count"] = 0
    plan_sha = preflight.sha256(preflight.canonical(plan))
    value = {"schema_version": 1, "kind": "renewal-quiescence", "task": preflight.TASK_ID, "nonce": plan["nonce"],
             "plan_sha256": plan_sha, "source_fingerprint": source["fingerprint"], "verified_at": datetime.now(timezone.utc).isoformat(),
             "drain_verified": True, "external_writers_absent_verified": True, "fence_maintained_until_capture_complete": True,
             "inspection": frozen, "learn_only": True}
    return plan, plan_sha, value


class LearnScopeContracts(unittest.TestCase):
    def test_dependency_defer_requires_exact_stopped_fence_and_preserves_readiness_meaning(self):
        snapshot, baseline, frozen, status = dependency_fixture()
        self.assertTrue(all("command" not in item for item in frozen))
        result = activate.check_backend_baseline(snapshot, baseline, learn_only=True, frozen=frozen, probe=lambda _: status)
        self.assertEqual({item["service"] for item in result["fence"]}, set(activate.LEARN_WRITERS))
        self.assertEqual(result["before"], {"docker_health": "unhealthy", "health_status": 200,
            "ready_status": 503, "baseline_complete": True, "readiness_deferred": True})
        for healthy, ready in (("healthy", 200), ("healthy", 503), ("unhealthy", 503)):
            next(item for item in snapshot["containers"] if item["service"] == activate.PERSONAL_WORKER)["health"] = healthy
            candidate = {**status, "ready_status": ready}
            with self.subTest(healthy=healthy, ready=ready):
                evidence = activate.check_backend_baseline(snapshot, baseline, learn_only=True, frozen=frozen, probe=lambda _: candidate)
                self.assertIs(evidence["before"]["readiness_deferred"], ready == 503)

    def test_dependency_exception_is_unavailable_in_every_default_and_other_scope(self):
        snapshot, baseline, frozen, status = dependency_fixture()
        for kwargs in ({}, {"learn_only": True}, {"frozen": frozen, "probe": lambda _: status},
                       {"learn_only": True, "frozen": frozen}, {"learn_only": True, "probe": lambda _: status}):
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(activate.ActivateError):
                activate.check_backend_baseline(snapshot, baseline, **kwargs)

    def test_dependency_fence_rejects_live_missing_duplicate_or_different_original_writer(self):
        snapshot, baseline, frozen, status = dependency_fixture()
        changes = (
            lambda rows: next(item for item in rows if item["service"] == "learn-service").update(running=True, pid=100),
            lambda rows: next(item for item in rows if item["service"] == "learn-service").update(pid=True),
            lambda rows: rows.pop(next(index for index, item in enumerate(rows) if item["service"] == "knowledge-candidate-runner")),
            lambda rows: rows.append(copy.deepcopy(next(item for item in rows if item["service"] == "learn-service"))),
            lambda rows: next(item for item in rows if item["service"] == "learn-service").update(container_id="f" * 64),
            lambda rows: next(item for item in rows if item["service"] == "learn-service").update(image_id="sha256:" + "f" * 64),
            lambda rows: rows.append({**copy.deepcopy(next(item for item in rows if item["service"] == "learn-service")), "service": "unknown-learn-worker"}),
        )
        for change in changes:
            altered = copy.deepcopy(snapshot)
            change(altered["containers"])
            with self.subTest(change=changes.index(change)), self.assertRaises(activate.ActivateError):
                activate.check_backend_baseline(altered, baseline, learn_only=True, frozen=frozen, probe=lambda _: status)
        altered = copy.deepcopy(frozen)
        next(item for item in altered if item["service"] == "learn-service").update(running=True, pid=100)
        with self.assertRaisesRegex(activate.ActivateError, "LEARN_DEPENDENCY_FENCE_CHANGED"):
            activate.check_backend_baseline(snapshot, baseline, learn_only=True, frozen=altered, probe=lambda _: status)

    def test_dependency_original_pid_start_restart_image_command_labels_and_mounts_are_bound(self):
        snapshot, baseline, frozen, status = dependency_fixture()
        for key, bad in (("container_id", "f" * 64), ("image_id", "sha256:" + "f" * 64), ("pid", 101),
                         ("pid", True), ("started_at", "2026-10-13T07:00:00Z"), ("restart_count", 1),
                         ("command", ["python", "other.py"]), ("mounts", [{"source": "/changed", "destination": "/secret"}])):
            changed = copy.deepcopy(snapshot)
            next(item for item in changed["containers"] if item["service"] == activate.PERSONAL_WORKER)[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(activate.ActivateError):
                activate.check_backend_baseline(changed, baseline, learn_only=True, frozen=frozen, probe=lambda _: status)
        changed = copy.deepcopy(snapshot)
        original = next(item for item in changed["containers"] if item["service"] == activate.PERSONAL_WORKER)
        changed["source_labels"][original["container_id"]]["io.ctf-web.release-nonce"] = "changed-nonce"
        with self.assertRaisesRegex(activate.ActivateError, "LEARN_DEPENDENCY_WORKER_CHANGED"):
            activate.check_backend_baseline(changed, baseline, learn_only=True, frozen=frozen, probe=lambda _: status)

    def test_dependency_does_not_allow_generic_unhealthy_or_bad_probe_and_other_services(self):
        snapshot, baseline, frozen, status = dependency_fixture()
        for candidate in ({**status, "health_status": 503}, {**status, "ready_status": 500},
                          {**status, "ready_status": True}, {**status, "baseline_complete": False},
                          {**status, "baseline_complete": 1}, {**status, "secret": "not-allowed"}):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(activate.ActivateError, "LEARN_DEPENDENCY_PROBE_UNVERIFIED"):
                activate.check_backend_baseline(snapshot, baseline, learn_only=True, frozen=frozen, probe=lambda _: candidate)
        for health, ready in ((None, 503), ("starting", 503), ("unhealthy", 200)):
            changed = copy.deepcopy(snapshot)
            next(item for item in changed["containers"] if item["service"] == activate.PERSONAL_WORKER)["health"] = health
            with self.subTest(health=health, ready=ready), self.assertRaisesRegex(activate.ActivateError, "LEARN_DEPENDENCY_HEALTH_STATE_INVALID"):
                activate.check_backend_baseline(changed, baseline, learn_only=True, frozen=frozen, probe=lambda _: {**status, "ready_status": ready})
        for service in ("auth-service", "community-service", "frontend", "comment-notification-runner"):
            changed = copy.deepcopy(snapshot)
            next(item for item in changed["containers"] if item["service"] == service)["health"] = "unhealthy"
            with self.subTest(service=service), self.assertRaisesRegex(activate.ActivateError, "FRONTEND_ONLY_BACKEND_RUNTIME_CHANGED"):
                activate.check_backend_baseline(changed, baseline, learn_only=True, frozen=frozen, probe=lambda _: status)

    def test_dependency_loopback_probe_validates_fixed_bodies_and_badge_baseline(self):
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<dependency-probe>", "exec"), namespace)
        payload = {"health": {"http_status": 200, "body": {"status": "ok"}},
                   "ready": {"http_status": 503, "body": {"status": "not_ready", "baseline_complete": True}}}
        commands = []
        def run(command, limit):
            commands.append((command, limit))
            return json.dumps(payload).encode()
        namespace["run"] = run
        self.assertEqual(namespace["probe_personal_worker"]("a" * 64),
                         {"health_status": 200, "ready_status": 503, "baseline_complete": True})
        self.assertEqual(commands[0][0][:5], ["docker", "exec", "a" * 64, "python", "-B"])
        self.assertEqual(commands[0][1], 8)
        child = commands[0][0][-1]
        self.assertIn("read(4097)", child)
        self.assertIn("timeout=2", child)
        self.assertIn("class NoRedirect", child)
        cases = [({"status": "not_ready", "baseline_complete": bad}, 503) for bad in (False, 1, "true")]
        cases += [({"status": "not_ready", "baseline_complete": True}, code) for code in (400, 401, 404, 500)]
        cases += [({"status": "ready", "baseline_complete": True}, 503), ({"status": "not_ready", "baseline_complete": True}, 200)]
        for body, code in cases:
            payload["ready"] = {"http_status": code, "body": body}
            with self.subTest(body=body, code=code), self.assertRaisesRegex(namespace["ActivateError"], "LEARN_DEPENDENCY_READY_BODY_INVALID"):
                namespace["probe_personal_worker"]("a" * 64)
        payload["health"] = {"http_status": 200, "body": {"status": "not_ok"}}
        with self.assertRaisesRegex(namespace["ActivateError"], "LEARN_DEPENDENCY_LIVENESS_INVALID"):
            namespace["probe_personal_worker"]("a" * 64)

    def test_dependency_receipt_rejects_forged_fence_identity_status_scope_and_recovery(self):
        evidence, request = recovered_dependency_evidence()
        activate.dependency_receipt_contract(evidence, request, recovered=True)
        mutations = (
            lambda value: value["fence"].pop(),
            lambda value: value["fence"][0].update(pid=False),
            lambda value: value["original_identity"].update(pid=True),
            lambda value: value["original_identity"].update(mounts_sha256="f" * 64),
            lambda value: value["source_labels"].update(extra="forbidden"),
            lambda value: value["before"].update(readiness_deferred=False),
            lambda value: value["before"].update(baseline_complete=False),
            lambda value: value["recovery"].update(ready_status=503),
            lambda value: value["recovery"].update(docker_health="unhealthy"),
            lambda value: value["recovery"].update(elapsed_seconds=180.001),
            lambda value: value["recovery"].update(observations=True),
            lambda value: value["recovery"]["identity"].update(restart_count=1),
            lambda value: value["recovery"].update(baseline_complete=False),
            lambda value: value.pop("recovery"),
        )
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(evidence)
            mutate(changed)
            with self.subTest(index=index), self.assertRaises(activate.ActivateError):
                activate.dependency_receipt_contract(changed, request, recovered=True)
        for scope in ({"learn_only": False}, {"frontend_only": True}):
            with self.subTest(scope=scope), self.assertRaisesRegex(activate.ActivateError, "REMOTE_DEPENDENCY_SCOPE_INVALID"):
                activate.dependency_receipt_contract(evidence, {**request, **scope}, recovered=True)
        for key, bad in (("image_id", "sha256:" + "f" * 64),):
            changed = copy.deepcopy(request)
            next(item for item in changed["current_container_baseline"] if item["service"] == activate.PERSONAL_WORKER)[key] = bad
            with self.subTest(original_field=key), self.assertRaisesRegex(activate.ActivateError, "REMOTE_DEPENDENCY_ORIGINAL_WORKER_CHANGED"):
                activate.dependency_receipt_contract(evidence, changed, recovered=True)

    def test_dependency_probe_child_executes_bounded_reads_without_redirect_or_network(self):
        import urllib.error
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<dependency-probe-child>", "exec"), namespace)
        captured = []
        payload = {"health": {"http_status": 200, "body": {"status": "ok"}},
                   "ready": {"http_status": 503, "body": {"status": "not_ready", "baseline_complete": True}}}
        namespace["run"] = lambda command, limit: (captured.append(command[-1]) or json.dumps(payload).encode())
        namespace["probe_personal_worker"]("a" * 64)
        class Response(io.BytesIO):
            code = 200
            def read(self, size=-1):
                self.observed_size = size
                return super().read(size)
        health = Response(b'{"status":"ok"}')
        ready = urllib.error.HTTPError("http://127.0.0.1:8040/ready", 503, "not ready", {},
                                      io.BytesIO(b'{"status":"not_ready","baseline_complete":true}'))
        with patch("urllib.request.build_opener") as builder, redirect_stdout(io.StringIO()) as output:
            opener = builder.return_value
            opener.open.side_effect = [health, ready]
            exec(compile(captured[0], "<bounded-http-probe>", "exec"), {})
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertEqual(health.observed_size, 4097)
        self.assertEqual([call.args[0] for call in opener.open.call_args_list],
                         ["http://127.0.0.1:8040/health", "http://127.0.0.1:8040/ready"])
        self.assertTrue(all(call.kwargs == {"timeout": 2} for call in opener.open.call_args_list))
        self.assertEqual(builder.call_args.args[0].proxies, {})
        self.assertIsNone(builder.call_args.args[1]().redirect_request(None, None, None, None, None, None))
        oversized = Response(b"x" * 4097)
        with patch("urllib.request.build_opener") as builder:
            builder.return_value.open.return_value = oversized
            with self.assertRaises(SystemExit) as error:
                exec(compile(captured[0], "<oversized-http-probe>", "exec"), {})
        self.assertEqual(error.exception.code, 2)

    def test_four_stage_chain_preserves_per_family_bindings_and_029_head(self):
        triples, inventory = baseline_chain()
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            chain = verify.load_baseline_chain(triples)
        state = chain["contract"]
        self.assertEqual(state["expected_heads"]["learn"], "20261013_029")
        self.assertEqual(state["image_bindings"]["auth"]["release_nonce"], "renewal-test-001")
        self.assertEqual(state["image_bindings"]["community"]["release_nonce"], "renewal-test-001")
        self.assertEqual(state["image_bindings"]["learn"]["release_nonce"], "chain-test-2")
        self.assertEqual(state["image_bindings"]["frontend"]["release_nonce"], "chain-test-3")
        self.assertEqual(chain["backup_anchor"]["contract"]["binding"]["release_nonce"], "chain-test-2")

    def test_full_029_to_frontend_029_chain_retains_exact_new_backend_and_requires_archive(self):
        triples, inventory = baseline_chain(full_learn_head=verify.LEARN_CATEGORY_HEAD, families=(None, "frontend"))
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            chain = verify.load_baseline_chain(triples)
            full = chain["first"]["contract"]
            final = chain["contract"]
            retained = stage.load_retained_baseline(triples[:1], final["managed_files"], frontend_only=True)
            activation_baseline = activate.load_baseline(triples[:1], {"files": final["managed_files"]},
                                                        frontend_only=True, chain_mode=True)
        self.assertEqual(full["expected_heads"], {**verify.EXPECTED_HEADS, "learn": verify.LEARN_CATEGORY_HEAD})
        self.assertEqual(final["expected_heads"], full["expected_heads"])
        self.assertEqual(final["changed_services"], ["frontend"])
        self.assertEqual(chain["backup_anchor"]["contract"]["binding"], full["binding"])
        self.assertEqual(activation_baseline["contract"]["images"], full["images"])
        for family in ("auth", "learn", "community"):
            self.assertEqual(final["images"][family], full["images"][family])
            self.assertEqual(final["image_bindings"][family], full["binding"])
            self.assertEqual(retained["images"][family], full["images"][family])
        self.assertNotEqual(final["images"]["frontend"], full["images"]["frontend"])
        inventory["1-activation"][0].pop("compatibility_image_backup_receipt_sha256")
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            with self.assertRaisesRegex(verify.VerifyError, "ACTIVATION_COMPATIBILITY_IMAGE_BACKUP_MISSING"):
                verify.load_baseline_chain(triples)
        triples, inventory = baseline_chain(full_learn_head=verify.LEARN_CATEGORY_HEAD, families=(None, "frontend"))
        inventory["0-verify"][0]["checks"].pop("event_category_registry")
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            with self.assertRaisesRegex(verify.VerifyError, "BASELINE_EVENT_CATEGORY_CHECK_MISSING"):
                verify.load_baseline_chain(triples)

    def test_chain_rejects_wrong_parent_hash_and_cannot_start_at_frontend(self):
        triples, inventory = baseline_chain()
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            with self.assertRaisesRegex(verify.VerifyError, "MUST_START_FULL"):
                verify.load_baseline_chain(triples[1:])
            inventory["2-verify"][0]["baseline_receipts"][0]["verify"] = "f" * 64
            with self.assertRaisesRegex(verify.VerifyError, "PARENT_HASH_CHANGED"):
                verify.load_baseline_chain(triples[:3])

    def test_chain_rejects_family_baseline_drift_and_schema_reversal(self):
        for mutate in (lambda value: value["remote"]["retained_baseline"]["image_bindings"]["auth"].update(commit="f" * 40),
                       lambda value: value["remote"].update(learn_only=False)):
            triples, inventory = baseline_chain()
            mutate(inventory["2-build"][0])
            with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
                with self.assertRaises(verify.VerifyError):
                    verify.load_baseline_chain(triples[:3])
        triples, inventory = baseline_chain()
        inventory["3-activation"][0]["schema"]["heads"]["learn"] = ["20261013_028"]
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            with self.assertRaisesRegex(verify.VerifyError, "SCHEMA_BINDING_CHANGED"):
                verify.load_baseline_chain(triples)

    def test_legacy_frontend_receipt_can_extend_full_baseline_without_rewriting_evidence(self):
        triples, inventory = baseline_chain()
        build, _ = inventory["1-build"]
        retained = build["remote"].pop("retained_baseline")
        build["remote"].pop("learn_only")
        build["remote"]["backend_baseline"] = {name: retained[name] for name in ("binding", "images")}
        build_sha = verify.sha(verify.canonical(build))
        inventory["1-build"] = (build, build_sha)
        checked, _ = inventory["1-verify"]
        parent = checked.pop("baseline_receipts")[0]
        checked.pop("learn_only")
        checked["build_receipt_sha256"] = build_sha
        checked.update({"baseline_" + name + "_receipt_sha256": value for name, value in parent.items()})
        inventory["1-verify"] = (checked, verify.sha(verify.canonical(checked)))
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            state = verify.load_baseline_chain(triples[:2])["contract"]
        self.assertEqual(state["image_bindings"]["frontend"]["release_nonce"], "chain-test-1")
        self.assertEqual(state["expected_heads"]["learn"], "20261013_028")

    def test_legacy_frontend_verify_cli_keeps_original_three_hash_metadata(self):
        triples, inventory = baseline_chain()
        activation = copy.deepcopy(inventory["1-activation"][0])
        activation.pop("baseline_receipts")
        activation.pop("learn_only")
        inventory["legacy-activation"] = (activation, "f" * 64)
        files = verify.file_rows(activation["managed_files"] + [item for item in activation["unmanaged_files"] if item["path"].startswith(verify.CONTEXTS)])
        binding = activation["binding"]
        output = io.StringIO()
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])), \
                patch.object(verify.ops, "manifest", return_value={"fingerprint": binding["source_fingerprint"], "files": files}), \
                patch.object(verify.ops, "ssh", side_effect=AssertionError("SSH_FORBIDDEN")), \
                patch.object(verify.ops, "receipt", side_effect=AssertionError("WRITE_FORBIDDEN")), redirect_stdout(output):
            self.assertEqual(verify.main(["--commit", binding["commit"], "--source-fingerprint", binding["source_fingerprint"],
                "--release-nonce", binding["release_nonce"], "--build-receipt", "1-build", "--activation-receipt", "legacy-activation",
                "--frontend-only", "--baseline-activation-receipt", "0-activation", "--baseline-build-receipt", "0-build",
                "--baseline-verify-receipt", "0-verify"]), 0)
        value = json.loads(output.getvalue())
        self.assertNotIn("baseline_receipts", value)
        self.assertEqual(value["baseline_verify_receipt_sha256"], inventory["0-verify"][1])

    def test_learn_preflight_scope_is_explicit_and_cannot_claim_three_database_backup(self):
        plan, _, _ = learn_fence()
        preflight.validate_plan(plan, plan["source_fingerprint"])
        self.assertEqual(plan["backup_scope"]["databases"], ["learn_db"])
        bad = copy.deepcopy(plan)
        bad["backup_scope"]["databases"].append("platform_db")
        with self.assertRaisesRegex(preflight.ReleaseError, "PLAN_BACKUP_SCOPE_INVALID"):
            preflight.validate_plan(bad, plan["source_fingerprint"])
        bad = copy.deepcopy(plan)
        bad.pop("learn_only")
        with self.assertRaisesRegex(preflight.ReleaseError, "PLAN_BACKUP_SCOPE_INVALID"):
            preflight.validate_plan(bad, plan["source_fingerprint"])

    def test_pending_frontend_source_is_allowed_only_for_learn_compatibility_stage(self):
        triples, inventory = baseline_chain()
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            state = verify.load_baseline_chain(triples[:2])["contract"]
        rows = copy.deepcopy(state["managed_files"])
        next(item for item in rows if item["path"].startswith("frontend/"))["sha256"] = "f" * 64
        verify.retained_source_contract(state, rows, learn_only=True)
        next(item for item in rows if item["path"].startswith("services/auth/"))["sha256"] = "e" * 64
        with self.assertRaisesRegex(verify.VerifyError, "RETAINED_BACKEND_SOURCE_CHANGED"):
            verify.retained_source_contract(state, rows, learn_only=True)

    def test_learn_build_and_overlay_contain_no_other_family_or_frontend(self):
        binding = verify.binding_for("a" * 40, "b" * 64, "learn-test-001")
        args = {"NEXT_PUBLIC_SITE_URL": "https://ctf.example.com", "NEXT_PUBLIC_ENV_PROXY_BASE": "https://ctf.example.com/env", "NEXT_PUBLIC_ENV_ACCESS_MODE": "proxy"}
        commands = stage.build_commands(Path("/release/source"), binding, args, learn_only=True)
        self.assertEqual([name for name, _ in commands], ["learn"])
        model = {"services": {name: {"environment": {}, "networks": ["backend"]} for name in (*activate.API_ORDER, "frontend", *verify.LEGACY_PROCESS_WORKERS)}}
        overlay = activate.make_overlay(model, {"learn": "sha256:" + "a" * 64}, binding, list(verify.LEGACY_PROCESS_WORKERS), Path("/runtime"), learn_only=True)
        self.assertEqual(set(overlay["services"]), {"learn-service", "learn-migrate", *verify.LEGACY_PROCESS_WORKERS})
        self.assertEqual({subject for action, subject in activate.activation_steps(list(verify.LEGACY_PROCESS_WORKERS), learn_only=True) if action == "migration"}, {"learn"})

    def test_learn_fence_stops_exactly_six_and_leaves_other_database_clients(self):
        plan, plan_sha, value = learn_fence()
        targets = quiesce.action_targets(plan, "stop", learn_only=True)
        self.assertEqual({item["service"] for item in targets}, {"minio", "learn-service", *verify.LEGACY_PROCESS_WORKERS})
        self.assertEqual(targets[-1]["service"], "minio")
        self.assertEqual(backup.validate_quiescence(value, plan, plan_sha, learn_only=True), value["inspection"])
        self.assertGreater(value["inspection"]["databases"]["auth"]["client_session_count"], 0)
        specs = backup.stream_specs(plan, learn_only=True)
        self.assertEqual([spec["name"] for spec in specs if spec["kind"] == "database"], ["learn_db.dump"])
        with self.assertRaises(preflight.ReleaseError):
            backup.validate_quiescence(value, plan, plan_sha)

    def test_learn_fence_rejects_foreign_stop_live_learn_client_and_missing_worker(self):
        for change in ("foreign-stop", "learn-client", "missing-worker"):
            plan, plan_sha, value = learn_fence()
            if change == "foreign-stop":
                next(item for item in value["inspection"]["containers"] if item["service"] == "auth-service").update(running=False, pid=0, status="exited")
            elif change == "learn-client":
                value["inspection"]["databases"]["learn"]["client_session_count"] = 1
            else:
                plan["inspection"]["containers"] = [item for item in plan["inspection"]["containers"] if item["service"] != "knowledge-candidate-runner"]
            with self.assertRaises(preflight.ReleaseError):
                backup.validate_quiescence(value, plan, plan_sha, learn_only=True)

    def test_learn_backup_cannot_be_misrepresented_as_global_consistent_backup(self):
        request, snapshot, *_ = fixture()
        value = {"schema_version": 1, "kind": "renewal-stream-backup", "nonce": request["binding"]["release_nonce"],
                 "source_fingerprint": request["binding"]["source_fingerprint"], "plan_sha256": "1" * 64, "quiescence_sha256": "2" * 64,
                 "quiescence_verified": True, "release_eligible": True, "image_backup_verified": True, "learn_only": True,
                 "database_count": 1, "ctfd_db_included": False, "cross_service_consistency_verified": False,
                 "learn_object_consistency_verified": True, "captured_databases": ["learn_db"], "files": [{}] * 6,
                 "image_backup_receipt_sha256": "3" * 64,
                 "selected_image_ids": sorted(set(request["images"].values()))}
        activate.backup_contract(value, request["binding"], "1" * 64, "2" * 64, snapshot["containers"], learn_only=True)
        with self.assertRaises(activate.ActivateError):
            activate.backup_contract(value, request["binding"], "1" * 64, "2" * 64, snapshot["containers"])
        with self.assertRaises(activate.ActivateError):
            activate.backup_contract({**value, "cross_service_consistency_verified": True}, request["binding"], "1" * 64, "2" * 64, snapshot["containers"], learn_only=True)

    def test_frontend_029_requires_current_compatible_learn_image_archive(self):
        state = {"expected_heads": {**verify.EXPECTED_HEADS, "learn": verify.LEARN_CATEGORY_HEAD}, "images": {"learn": "sha256:" + "a" * 64}}
        value = {"selected_image_ids": [state["images"]["learn"]]}
        with self.assertRaisesRegex(activate.ActivateError, "COMPATIBILITY_IMAGE_BACKUP_REQUIRED"):
            activate.compatibility_image_backup(None, None, {}, "b" * 64, state, required=True)
        with patch.object(verify, "load_receipt", return_value=(value, "c" * 64)), \
                patch.object(activate.backup_images, "validate_receipt") as checked:
            self.assertEqual(activate.compatibility_image_backup("mock", "c" * 64, {}, "b" * 64, state, required=True), "c" * 64)
            checked.assert_called_once_with(value, {}, "b" * 64)
            with self.assertRaisesRegex(activate.ActivateError, "COMPATIBILITY_IMAGE_BACKUP_HASH_CHANGED"):
                activate.compatibility_image_backup("mock", "d" * 64, {}, "b" * 64, state, required=True)
            value["selected_image_ids"] = ["sha256:" + "f" * 64]
            with self.assertRaisesRegex(activate.ActivateError, "COMPATIBILITY_LEARN_IMAGE_NOT_CAPTURED"):
                activate.compatibility_image_backup("mock", "c" * 64, {}, "b" * 64, state, required=True)
        triples, inventory = baseline_chain()
        inventory["3-activation"][0].pop("compatibility_image_backup_receipt_sha256")
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            with self.assertRaisesRegex(verify.VerifyError, "ACTIVATION_COMPATIBILITY_IMAGE_BACKUP_MISSING"):
                verify.load_baseline_chain(triples)

    def test_full_029_frontend_compatibility_checks_actual_receipt_archive_and_current_learn_id(self):
        request, *_ = fixture()
        payload, images = full_image_archive(request["binding"], request["frontend_args_sha256"])
        triples, inventory = baseline_chain(full_learn_head=verify.LEARN_CATEGORY_HEAD, families=(None,), full_images=images)
        with patch.object(verify, "load_receipt", side_effect=lambda path: copy.deepcopy(inventory[str(path)])):
            baseline = verify.load_baseline_chain(triples)["contract"]
        _, _, plan, *_ = fixtures()
        replacements = {}
        for service, family in (("auth-service", "auth"), ("learn-service", "learn"),
                                ("community-service", "community"), ("frontend", "frontend")):
            prior = next(item for item in plan["inspection"]["containers"] if item["service"] == service)
            replacements[prior["image_id"]] = images[family]
        for item in plan["inspection"]["containers"]:
            item["image_id"] = replacements.get(item["image_id"], item["image_id"])
        plan["migrations"]["learn"]["head"] = verify.LEARN_CATEGORY_HEAD
        plan["migrations"]["learn"]["revisions"].append(verify.LEARN_CATEGORY_HEAD)
        plan["inspection"]["databases"]["learn"]["head"] = [verify.LEARN_CATEGORY_HEAD]
        plan_sha = preflight.sha256(preflight.canonical(plan))
        selected, binding_sha = activate.backup_images.selection(plan)
        self.assertEqual(selected, sorted(images.values()))
        owned = activate.ops.TASK / "runtime" / "work"
        self.assertTrue(owned.resolve().is_relative_to(activate.ops.TASK.resolve()))
        with tempfile.TemporaryDirectory(dir=owned) as directory:
            root = Path(directory)
            archive = root / (plan["nonce"] + "-images") / "old-release-images.tar"
            archive.parent.mkdir(); archive.write_bytes(payload)
            checked = activate.backup_images.validate_image_archive(archive, selected)
            receipt = {"schema_version": 1, "kind": "renewal-image-backup", "task": preflight.TASK_ID,
                "nonce": plan["nonce"], "plan_sha256": plan_sha, "source_fingerprint": plan["source_fingerprint"],
                "source_file_count": plan["source_file_count"], "target": plan["target"],
                "started_at": plan["created_at"], "captured_at": plan["created_at"], "selected_image_ids": selected,
                "container_binding_sha256": binding_sha, "images_metadata_sha256": "a" * 64, "acl_sha256": "b" * 64,
                "archive": {"path": str(archive), "bytes": len(payload), "sha256": preflight.sha256(payload), "file_count": checked["file_count"]},
                "capture_verified": True, "config_matching_verified": True, "restore_verified": False,
                "remote_temporary_bytes": 0, "image_identity_mode": checked["image_identity_mode"],
                "digest_chain_verified": checked["digest_chain_verified"], "service_mutations_performed": False,
                "tag_mutations_performed": False}
            evidence = root / "evidence"; evidence.mkdir()
            receipt_path = evidence / "compatibility-receipt.json"
            def save(value):
                data = preflight.canonical(value) + b"\n"
                receipt_path.write_bytes(data)
                return preflight.sha256(data)
            receipt_sha = save(receipt)
            with patch.object(activate.ops, "TASK", root), patch.object(activate.backup_images, "PRIVATE_ROOT", root):
                self.assertEqual(activate.compatibility_image_backup(receipt_path, receipt_sha, plan, plan_sha,
                                                                    baseline, required=True), receipt_sha)
                with self.assertRaisesRegex(activate.ActivateError, "COMPATIBILITY_IMAGE_BACKUP_HASH_CHANGED"):
                    activate.compatibility_image_backup(receipt_path, "f" * 64, plan, plan_sha, baseline, required=True)
                old = copy.deepcopy(baseline); old["images"]["learn"] = "sha256:" + "f" * 64
                with self.assertRaisesRegex(activate.ActivateError, "COMPATIBILITY_LEARN_IMAGE_NOT_CAPTURED"):
                    activate.compatibility_image_backup(receipt_path, receipt_sha, plan, plan_sha, old, required=True)
                for changes in ({"plan_sha256": "f" * 64}, {"source_fingerprint": preflight.sha256(b"different-source")},
                                {"capture_verified": False}, {"selected_image_ids": selected[:-1]}):
                    changed = {**receipt, **changes}
                    with self.subTest(changes=changes), self.assertRaises(preflight.ReleaseError):
                        activate.compatibility_image_backup(receipt_path, save(changed), plan, plan_sha, baseline, required=True)
                receipt_sha = save(receipt)
                archive.write_bytes(payload + b"changed")
                with self.assertRaisesRegex(preflight.ReleaseError, "IMAGE_ARCHIVE_SIZE_CHANGED"):
                    activate.compatibility_image_backup(receipt_path, receipt_sha, plan, plan_sha, baseline, required=True)

    def test_category_rollback_guard_is_read_only_and_forbids_legacy_after_custom_write(self):
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<guard-only>", "exec"), namespace)
        queries = []
        def sql(database, query):
            queries.append((database, query))
            return {"custom_category_count": 1, "custom_event_count": 0}
        namespace["sql"] = sql
        result = namespace["category_compatibility"]()
        self.assertTrue(result["legacy_enum_image_rollback_forbidden"])
        self.assertFalse(result["automatic_backend_rollback_performed"])
        self.assertEqual(queries[0][0], "learn_db")
        self.assertTrue(queries[0][1].startswith("SELECT "))

    def test_category_guard_rejects_unsafe_projection_and_029_verify_is_mandatory(self):
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<guard-only>", "exec"), namespace)
        for counts in ({"custom_category_count": True, "custom_event_count": 0}, {"custom_category_count": -1, "custom_event_count": 0},
                       {"custom_category_count": "0", "custom_event_count": 0}, {"custom_category_count": 0, "custom_event_count": 0, "password": "forbidden"}):
            namespace["sql"] = lambda *_, counts=counts: counts
            with self.assertRaisesRegex(namespace["ActivateError"], "CATEGORY_ROLLBACK_GUARD_UNVERIFIED"):
                namespace["category_compatibility"]()
        request, snapshot, images, databases, triggers = fixture()
        request["expected_heads"] = {**verify.EXPECTED_HEADS, "learn": verify.LEARN_CATEGORY_HEAD}
        request["category_defaults_only"] = True
        databases["learn"] = [verify.LEARN_CATEGORY_HEAD]
        state = {"registry_present": True, "audit_present": True, "registry_fk_present": True, "legacy_check_absent": True,
                 "custom_category_count": 0, "custom_event_count": 0}
        value = verify.evaluate_snapshot(request, snapshot, snapshot, images, databases, triggers, state)
        self.assertEqual(value["status"], "passed")
        verify.validate_result(value, request["binding"], expected_heads=request["expected_heads"])
        missing = copy.deepcopy(value)
        missing["checks"].pop("event_category_registry")
        with self.assertRaisesRegex(verify.VerifyError, "REMOTE_EVENT_CATEGORY_CHECK_MISSING"):
            verify.validate_result(missing, request["binding"], expected_heads=request["expected_heads"])
        for change in ({"custom_category_count": 1}, {"custom_event_count": 1}, {"registry_fk_present": False}, {"legacy_check_absent": False}):
            result = verify.evaluate_snapshot(request, snapshot, snapshot, images, databases, triggers, {**state, **change})
            self.assertEqual(result["checks"]["event_category_registry"]["status"], "failed")
        request["category_defaults_only"] = False
        self.assertEqual(verify.evaluate_snapshot(request, snapshot, snapshot, images, databases, triggers,
                                                  {**state, "custom_category_count": 1})["status"], "passed")

    def test_pre_migration_projection_requires_exact_028_constraint_and_zero_custom_events(self):
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<before-migration-only>", "exec"), namespace)
        valid = {"registry_absent": True, "legacy_constraint_valid": True, "custom_event_count": 0}
        for head, state in ((["20261013_029"], valid), (["20261013_028"], {**valid, "custom_event_count": 1}),
                            (["20261013_028"], {**valid, "custom_event_count": False}),
                            (["20261013_028"], {**valid, "registry_absent": False}),
                            (["20261013_028"], {**valid, "legacy_constraint_valid": 1})):
            namespace["sql"] = lambda *_, head=head, state=state: head if "alembic_version" in _[1] else state
            with self.assertRaises(namespace["ActivateError"]):
                namespace["category_before_migration"]()
        namespace["sql"] = lambda *args: ["20261013_028"] if "alembic_version" in args[1] else valid
        self.assertEqual(namespace["category_before_migration"](), {"previous_head": "20261013_028", **valid})


def code_fixture():
    request, snapshot, labels, heads, triggers = fixture()
    paths = ["services/learn/alembic.ini", "services/learn/app/scripts/learn_migrate.py",
             "services/learn/app/config.py", "services/learn/app/database.py",
             "services/learn/app/utils/learn_schema_bootstrap_contract.py",
             "services/learn/migrations/env.py", "services/learn/migrations/script.py.mako",
             "services/learn/migrations/versions/029.py"]
    request["managed_files"] = verify.file_rows(request["managed_files"] + [row(name) for name in paths])
    snapshot["code_files"] = verify.file_rows(request["managed_files"] + request["unmanaged_files"])
    for item in snapshot["containers"]:
        item.setdefault("pid", 200); item.setdefault("started_at", "2026-10-13T06:00:00Z")
        item.update(finished_at="0001-01-01T00:00:00Z", mounts=[])
    heads["learn"] = [verify.LEARN_CATEGORY_HEAD]
    request["expected_heads"] = {name: value[0] for name, value in heads.items()}
    full_build, full_activation = receipts(request, snapshot)
    full_activation["schema"] = {"heads": heads}
    full_contract = verify.build_contract(full_build, request["binding"])
    baseline = verify.activation_contract(full_activation, request["binding"], request["managed_files"], full_contract)
    baseline["expected_heads"] = request["expected_heads"]
    full_checked = verify.evaluate_snapshot(request, snapshot, snapshot, labels, heads, triggers, category_projection(custom_category_count=2))
    full_a_sha, full_b_sha = (verify.sha(verify.canonical(value)) for value in (full_activation, full_build))
    full_checked.update(local_source_unchanged=True, recorded_at=datetime.now(timezone.utc).isoformat(),
        build_receipt_sha256=full_b_sha, activation_receipt_sha256=full_a_sha, frontend_only=False, secret_values_recorded=False)
    hashes = [{"activation": full_a_sha, "build": full_b_sha, "verify": verify.sha(verify.canonical(full_checked))}]
    source, migrations, plan, *_ = fixtures()
    source["files"] = snapshot["code_files"]
    plan["inspection"]["code_files"] = snapshot["code_files"]
    for name in heads:
        migrations[name] = {"head": heads[name][0], "revisions": [heads[name][0]]}
        plan["inspection"]["databases"][name]["head"] = heads[name]
    next(item for item in plan["inspection"]["containers"] if item["service"] == "learn-service")["image_id"] = baseline["images"]["learn"]
    contract = preflight.make_code_contract(source["files"], plan["inspection"], migrations, baseline, hashes)
    plan = preflight.make_plan(source, plan["inspection"], migrations, learn_only=True, code_contract=contract)
    binding = verify.binding_for("1" * 40, "2" * 64, "code-only-test-029")
    build = copy.deepcopy(full_build)
    build.update(binding=binding, learn_only=True, release_phase=preflight.LEARN_CODE_PHASE, learn_code_contract=contract)
    remote = build["remote"]
    remote.update(binding=binding, frontend_only=False, learn_only=True, release_phase=preflight.LEARN_CODE_PHASE,
                  learn_code_contract=contract, retained_baseline=verify.baseline_summary(baseline, ("learn",)))
    new_image = "sha256:" + digest("code-only-image")
    remote["completed_images"] = [{"service": "learn", "image_id": new_image,
        "tag": f"ctf-renewal/learn:{binding['commit']}-{binding['release_nonce']}"}]
    selected = [{**item, "sha256": digest("updated-code")} if item["path"] == "services/learn/Dockerfile" else item
                for item in baseline["managed_files"] if item["path"].startswith("services/learn/")]
    retained = [item for item in baseline["managed_files"] if not item["path"].startswith("services/learn/")]
    state = {"head": [verify.LEARN_CATEGORY_HEAD], **{name: digest(name) for name in (
        "triggers_sha256", "categories_sha256", "category_audit_sha256", "events_sha256")}, "schema_sha256": contract["schema_sha256"]}
    targets = sorted(name for name, family in verify.SERVICE_IMAGES.items() if family == "learn")
    activation = {**copy.deepcopy(full_activation), "binding": binding, "learn_only": True, "frontend_only": False,
        "release_phase": preflight.LEARN_CODE_PHASE, "learn_code_contract": contract, "managed_files": selected,
        "unmanaged_files": verify.file_rows(retained + baseline["unmanaged_files"]), "baseline_receipts": hashes,
        "learn_code_before": state, "learn_code_after": state, "migration_executed": False,
        "compatibility_image_backup_receipt_sha256": "a" * 64,
        "untargeted_runtime_before": preflight.code_runtime(snapshot, targets),
        "untargeted_runtime_after": preflight.code_runtime(snapshot, targets)}
    built = verify.build_contract(build, binding, learn_only=True, baseline=baseline)
    result = verify.activation_contract(activation, binding, selected, built, baseline=baseline, learn_only=True)
    current = copy.deepcopy(snapshot)
    current["code_files"] = verify.file_rows(result["managed_files"] + result["unmanaged_files"])
    new_labels = activate.source_labels(binding)
    for item in current["containers"]:
        if item["service"] in targets: item.update(image_id=new_image, source_labels=new_labels)
    final_labels = {key: value for key, value in labels.items() if key != baseline["images"]["learn"]} | {new_image: new_labels}
    checked = verify.evaluate_snapshot(result, current, current, final_labels, heads, triggers,
        category_projection(custom_category_count=2), [state, state])
    checked.update(local_source_unchanged=True, recorded_at=datetime.now(timezone.utc).isoformat(), frontend_only=False,
        learn_only=True, release_phase=preflight.LEARN_CODE_PHASE, baseline_receipts=hashes, secret_values_recorded=False,
        build_receipt_sha256=verify.sha(verify.canonical(build)), activation_receipt_sha256=verify.sha(verify.canonical(activation)))
    inventory = {"full-a": (full_activation, full_a_sha), "full-b": (full_build, full_b_sha), "full-v": (full_checked, hashes[0]["verify"]),
                 "code-a": (activation, checked["activation_receipt_sha256"]), "code-b": (build, checked["build_receipt_sha256"]),
                 "code-v": (checked, verify.sha(verify.canonical(checked)))}
    return source, plan, baseline, build, activation, result, current, inventory, state, final_labels, heads, triggers


class LearnCodeOnlyTests(unittest.TestCase):
    def test_new_plan_is_explicit_source_bound_and_old_shape_unchanged(self):
        source, plan, *_ = code_fixture()
        preflight.validate_plan(plan, source["fingerprint"])
        self.assertEqual(plan["release_phase"], "learn-code-only-029")
        _, _, old, *_ = fixtures()
        self.assertNotIn("release_phase", old)
        self.assertNotIn("learn_code_contract", old)
        for change in ({"learn_only": False}, {"release_phase": None}, {"release_phase": "other"}):
            with self.subTest(change=change), self.assertRaises(preflight.ReleaseError):
                preflight.validate_plan({**plan, **change}, source["fingerprint"])

    def test_all_three_heads_and_exact_migration_configuration_bytes_are_required(self):
        source, plan, baseline, *_ = code_fixture()
        for target in ("baseline", "source", "live", "config", "version", "preimage"):
            live, graph, base, rows = copy.deepcopy(plan["inspection"]), copy.deepcopy(plan["migrations"]), copy.deepcopy(baseline), copy.deepcopy(source["files"])
            if target == "baseline": base["expected_heads"]["learn"] = "20261013_028"
            if target == "source": graph["learn"]["head"] = "20261013_028"
            if target == "live": live["databases"]["learn"]["head"] = ["20261013_028"]
            if target == "config": next(item for item in rows if item["path"].endswith("alembic.ini"))["sha256"] = "f" * 64
            if target == "version": next(item for item in live["code_files"] if "/versions/" in item["path"])["sha256"] = "f" * 64
            if target == "preimage": base["images"]["learn"] = "sha256:" + "f" * 64
            with self.subTest(target=target), self.assertRaises(preflight.ReleaseError):
                preflight.make_code_contract(rows, live, graph, base, plan["learn_code_contract"]["baseline_receipts"])

    def test_029_baseline_branch_does_not_relax_the_original_028_guard(self):
        source, _, baseline, *_ = code_fixture()
        chain = {"contract": baseline, "hashes": [], "backup_anchor": {"contract": baseline, "activation": {}, "build": {"remote": {"live_before": {"containers": []}}}}}
        with patch.object(verify, "load_baseline_chain", return_value=chain):
            for function in (stage.load_retained_baseline, activate.load_baseline):
                args = ([], source["files"]) if function is stage.load_retained_baseline else ([], source)
                keywords = {"learn_only": True, "frontend_only": False}
                if function is activate.load_baseline: keywords["chain_mode"] = True
                with self.assertRaisesRegex(Exception, "BASELINE_HEAD_INVALID"): function(*args, **keywords)
                function(*args, **keywords, learn_code_only=True)

    def test_code_update_has_no_migration_job_overlay_or_unrelated_restart(self):
        targets = sorted(verify.LEGACY_PROCESS_WORKERS)
        steps = activate.activation_steps(targets, learn_only=True, learn_code_only=True)
        self.assertEqual([subject for action, subject in steps if action == "up"], ["learn-service", *targets])
        self.assertFalse(any(action == "migration" for action, _ in steps))
        self.assertEqual([subject for action, subject in activate.activation_steps(targets, learn_only=True) if action == "migration"], ["learn"])
        overlay = activate.make_overlay({"services": {name: {} for name in (*activate.API_ORDER, "frontend", *targets)}},
            {"learn": "sha256:" + "a" * 64}, {"commit": "b" * 40, "source_fingerprint": "c" * 64, "release_nonce": "code-test-029"},
            targets, Path("/unused"), learn_only=True, learn_code_only=True)
        self.assertEqual(set(overlay["services"]), {"learn-service", *targets})
        self.assertNotIn("learn-migrate", overlay["services"])

    def test_backup_quiescence_still_requires_the_original_minio_and_learn_fence(self):
        plan, _, proof = learn_fence()
        self.assertTrue(proof["learn_only"])
        stopped = {row["service"] for row in proof["inspection"]["containers"] if not row["running"]}
        self.assertEqual(stopped, {"learn-service", "minio", *verify.LEGACY_PROCESS_WORKERS})
        _, code_plan, *_ = code_fixture()
        plan.update(release_phase=preflight.LEARN_CODE_PHASE, learn_code_contract=copy.deepcopy(code_plan["learn_code_contract"]),
                    migrations=code_plan["migrations"])
        for inspection in (plan["inspection"], proof["inspection"]):
            inspection["code_files"] = code_plan["inspection"]["code_files"]
            for name in preflight.DATABASES:
                inspection["databases"][name]["head"] = code_plan["inspection"]["databases"][name]["head"]
        plan["learn_code_contract"]["previous_learn_image"] = next(item["image_id"] for item in plan["inspection"]["containers"] if item["service"] == "learn-service")
        plan_sha = preflight.sha256(preflight.canonical(plan))
        proof["plan_sha256"] = plan_sha
        preflight.validate_plan(plan, plan["source_fingerprint"])
        self.assertEqual(backup.validate_quiescence(proof, plan, plan_sha, learn_only=True), proof["inspection"])
        specs = backup.stream_specs(plan, learn_only=True)
        self.assertEqual(len([item for item in specs if item["kind"] == "database"]), 1)
        self.assertTrue(any(item["kind"] == "objects" for item in specs))

    def test_code_chain_accepts_custom_categories_and_requires_new_preservation_checks(self):
        *_, inventory, state, labels, heads, triggers = code_fixture()
        triples = [("full-a", "full-b", "full-v"), ("code-a", "code-b", "code-v")]
        with patch.object(verify, "load_receipt", side_effect=lambda name: copy.deepcopy(inventory[name])):
            chain = verify.load_baseline_chain(triples)
        self.assertFalse(chain["contract"]["category_defaults_only"])
        self.assertEqual(chain["contract"]["expected_heads"]["learn"], "20261013_029")
        self.assertNotIn("learn-migrate", chain["contract"]["changed_services"])
        for key in ("learn_code_preservation", "learn_code_untargeted_runtime"):
            changed = copy.deepcopy(inventory)
            changed["code-v"][0]["checks"].pop(key)
            with patch.object(verify, "load_receipt", side_effect=lambda name: changed[name]), self.assertRaises(verify.VerifyError):
                verify.load_baseline_chain(triples)

    def test_state_schema_trigger_category_event_and_runtime_drift_never_pass(self):
        _, _, _, _, activation, request, current, _, state, labels, heads, triggers = code_fixture()
        for key in ("schema_sha256", "triggers_sha256", "categories_sha256", "category_audit_sha256", "events_sha256"):
            changed = {**state, key: "f" * 64}
            value = verify.evaluate_snapshot(request, current, current, labels, heads, triggers,
                category_projection(custom_category_count=2), [state, changed])
            self.assertEqual(value["status"], "failed")
            bad = {**activation, "learn_code_after": changed}
            with self.assertRaises(preflight.ReleaseError): preflight.validate_code_preservation(bad, activation["learn_code_contract"])
        for field in ("pid", "started_at", "restart_count", "source_labels"):
            changed = copy.deepcopy(current)
            item = next(row for row in changed["containers"] if row["service"] == "auth-service")
            item[field] = "wrong" if field != "source_labels" else {}
            value = verify.evaluate_snapshot(request, changed, changed, labels, heads, triggers,
                category_projection(custom_category_count=2), [state, state])
            self.assertEqual(value["checks"]["learn_code_untargeted_runtime"]["status"], "failed")

    def test_actual_activation_and_verify_label_locations_have_identical_explicit_projection(self):
        _, _, _, _, _, _, current, *_ = code_fixture()
        activation_snapshot = copy.deepcopy(current)
        activation_snapshot["source_labels"] = {row["container_id"]: row["source_labels"]
            for row in current["containers"] if row["service"] in verify.SERVICE_IMAGES}
        for row in activation_snapshot["containers"]: row.pop("source_labels", None)
        verification_snapshot = copy.deepcopy(current)
        postgres = next(row for row in verification_snapshot["containers"] if row["service"] == "postgres")
        postgres["source_labels"] = dict.fromkeys(activate.source_labels({"commit": None, "source_fingerprint": None, "release_nonce": None}))
        self.assertEqual(preflight.code_runtime(activation_snapshot, []), preflight.code_runtime(verification_snapshot, []))

    def labelled_env_snapshots(self, *, code_only=True):
        _, _, _, _, _, request, current, _, state, images, heads, triggers = code_fixture()
        current = copy.deepcopy(current)
        env_labels = {"org.opencontainers.image.revision": "5e9cf6bfd1786791a22d82cd9e2642a5551524b5",
            "io.ctf-web.source-fingerprint": "a25ac604263bb1afd2ba146064045d9f82517e38f714fa098eb8fb2ccf14a332",
            "io.ctf-web.release-nonce": "bb81c388-b3e6-4c7e-bca3-b09235605b36"}
        current["containers"].append({"service": "env-orchestrator", "container_id": digest("env-owned-fixture"),
            "image_id": "sha256:" + digest("env-image-fixture"), "running": True, "health": "healthy", "pid": 456,
            "started_at": "2026-10-13T06:00:00Z", "finished_at": "0001-01-01T00:00:00Z", "restart_count": 0,
            "mounts": [], "source_labels": env_labels})
        current["containers"].sort(key=lambda item: (item["service"], item["container_id"]))
        configurations = {}
        for item in current["containers"]:
            labels = item.pop("source_labels", {}) if item["service"] in {*verify.SERVICE_IMAGES, "env-orchestrator"} else {}
            item.pop("source_labels", None)
            configurations[item["container_id"]] = {"Labels": labels, "Cmd": item.get("command")}
        inspections = {"activation": [], "verify": []}
        namespaces = []
        for name, module, entry in (("activation", activate, "activation_main"), ("verify", verify, "verify_main")):
            namespace = {"REQUEST": request if code_only else {"binding": request["binding"]}}
            exec(compile(module.remote_program().rsplit(entry + "()", 1)[0], "<label-" + name + ">", "exec"), namespace)
            def read(argv, *, name=name, **kwargs):
                self.assertEqual(argv[:2], ["docker", "inspect"])
                inspections[name].extend(argv[2:])
                return json.dumps([{"Id": identifier, "Config": configurations[identifier]} for identifier in argv[2:]]).encode()
            namespace.update(compose=lambda: {}, protected_rows=lambda _: current["protected_files"],
                containers=lambda: copy.deepcopy(current["containers"]), code_rows=lambda _: current["code_files"], run=read)
            namespaces.append(namespace)
        request["containers_before"] = verify.container_rows(current["containers"])
        return namespaces, configurations, inspections, request, state, images, heads, triggers

    def test_code_only_collects_real_env_labels_before_after_and_in_independent_verify(self):
        namespaces, configurations, inspections, request, state, images, heads, triggers = self.labelled_env_snapshots()
        before = namespaces[0]["safe_snapshot"]()
        after = namespaces[0]["safe_snapshot"]()
        readonly = namespaces[1]["verify_snapshot"]()
        targets = request["changed_services"]
        projection = preflight.code_runtime(before, targets)
        self.assertEqual(projection, preflight.code_runtime(after, targets))
        self.assertEqual(projection, preflight.code_runtime(readonly, targets))
        env = next(item for item in projection if item["service"] == "env-orchestrator")
        self.assertEqual(env["source_labels"]["org.opencontainers.image.revision"], "5e9cf6bfd1786791a22d82cd9e2642a5551524b5")
        request["untargeted_runtime"] = projection
        value = verify.evaluate_snapshot(request, readonly, readonly, images, heads, triggers,
            category_projection(custom_category_count=2), [state, state])
        self.assertEqual(value["status"], "passed")
        configurations[env["container_id"]]["Labels"]["io.ctf-web.source-fingerprint"] = "f" * 64
        changed = namespaces[1]["verify_snapshot"]()
        value = verify.evaluate_snapshot(request, changed, changed, images, heads, triggers,
            category_projection(custom_category_count=2), [state, state])
        self.assertEqual(value["checks"]["learn_code_untargeted_runtime"]["status"], "failed")
        self.assertEqual(value["status"], "failed")

    def test_existing_phases_keep_the_original_service_only_inspect_scope(self):
        namespaces, configurations, inspections, *_ = self.labelled_env_snapshots(code_only=False)
        snapshot = namespaces[0]["safe_snapshot"]()
        expected = {row["container_id"] for row in snapshot["containers"] if row["service"] in verify.SERVICE_IMAGES}
        self.assertEqual(set(inspections["activation"]), expected)
        self.assertEqual(set(snapshot["source_labels"]), expected)
        self.assertNotIn(digest("env-owned-fixture"), inspections["activation"])

    def test_code_receipt_cannot_claim_migration_or_omit_actual_029_image_backup(self):
        _, _, _, _, activation, *_ = code_fixture()
        for change in ({"migration_executed": True}, {"category_before_migration": {}},
                       {"compatibility_image_backup_receipt_sha256": None}, {"untargeted_runtime_after": []}):
            with self.subTest(change=change), self.assertRaises(preflight.ReleaseError):
                preflight.validate_code_preservation({**activation, **change}, activation["learn_code_contract"])

    def test_remote_programs_contain_actual_new_helpers_and_execute_mocked_reads(self):
        _, plan, _, _, _, _, _, _, state, *_ = code_fixture()
        namespace = {}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<code-activate>", "exec"), namespace)
        namespace["containers"] = lambda: [{"service": "postgres", "container_id": "p", "running": True}]
        namespace["database_rows"] = lambda _: {"learn": {"head": state["head"], "schema_sha256": state["schema_sha256"]}}
        namespace["sql"] = lambda database, query: {key: value for key, value in state.items() if key != "schema_sha256"}
        self.assertEqual(namespace["learn_code_snapshot"](), state)
        namespace["validate_code_contract"](plan["learn_code_contract"])
        self.assertFalse(any(action == "migration" for action, _ in namespace["activation_steps"]([], learn_only=True, learn_code_only=True)))
        verify_namespace = {}
        exec(compile(verify.remote_program().rsplit("verify_main()", 1)[0], "<code-verify>", "exec"), verify_namespace)
        verify_namespace["validate_code_state"](state)
        self.assertIn("events_sha256", verify_namespace["LEARN_CODE_STATE_SQL"])
        compile(stage.remote_program(), "<code-build>", "exec")

    def test_annotated_source_and_all_configuration_rows_are_in_the_new_contract(self):
        source, plan, *_ = code_fixture()
        paths = {item["path"] for item in preflight.learn_migration_rows(source["files"])}
        for suffix in ("alembic.ini", "migrations/env.py", "migrations/script.py.mako", "app/config.py", "app/database.py",
                       "app/scripts/learn_migrate.py", "app/utils/learn_schema_bootstrap_contract.py"):
            self.assertIn("services/learn/" + suffix, paths)
        for key in ("schema_sha256", "previous_learn_image", "migration_files", "baseline_receipts"):
            changed = copy.deepcopy(plan)
            changed["learn_code_contract"].pop(key)
            with self.assertRaises(preflight.ReleaseError): preflight.validate_plan(changed, source["fingerprint"])

    def test_new_phase_requires_a_new_source_bound_preflight_even_in_local_build_planning(self):
        source, plan, _, build, *_ = code_fixture()
        with self.assertRaisesRegex(stage.StageError, "PREFLIGHT_REQUIRED"):
            stage.load_preflight(None, None, build["binding"], len(source["files"]), learn_only=True, learn_code_only=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); evidence = root / "evidence"; evidence.mkdir()
            path = evidence / "fresh.json"; path.write_bytes(preflight.canonical(plan))
            binding = {**build["binding"], "source_fingerprint": source["fingerprint"], "release_nonce": plan["nonce"]}
            with patch.object(stage.ops, "TASK", root):
                checked = stage.load_preflight(path, preflight.sha256(path.read_bytes()), binding, len(source["files"]), learn_only=True, learn_code_only=True)
                self.assertEqual(checked["learn_code_contract"], plan["learn_code_contract"])
                with self.assertRaisesRegex(stage.StageError, "PHASE_CHANGED"):
                    stage.load_preflight(path, preflight.sha256(path.read_bytes()), binding, len(source["files"]), learn_only=True)

    def mocked_activation(self, *, fail_up=False, drift=False, stop_outcome=None):
        _, plan, baseline, build, activation, result, current, _, state, *_ = code_fixture()
        targets = sorted(name for name, family in verify.SERVICE_IMAGES.items() if family == "learn")
        before = copy.deepcopy(current)
        before["code_files"] = verify.file_rows(baseline["managed_files"] + baseline["unmanaged_files"])
        for item in before["containers"]:
            if item["service"] in targets:
                item.update(image_id=baseline["images"]["learn"], running=False, pid=0, source_labels=activate.source_labels(baseline["binding"]))
        for snapshot in (before, current):
            snapshot["containers"].append({"service": "minio", "container_id": digest("minio"), "image_id": "sha256:" + digest("minio-image"),
                "running": True, "pid": 999, "started_at": "2026-10-13T06:00:00Z", "finished_at": "0001-01-01T00:00:00Z", "restart_count": 0, "mounts": []})
            snapshot["containers"].sort(key=lambda item: (item["service"], item["container_id"]))
            snapshot["source_labels"] = {item["container_id"]: item.get("source_labels", {}) for item in snapshot["containers"]}
        request = {**result, "frontend_only": False, "learn_only": True, "current_container_baseline": before["containers"],
            "backend_baseline": baseline, "worker_templates": None, "modes": {}, "plan_sha256": "1" * 64,
            "backup_receipt_sha256": "2" * 64, "quiescence_sha256": "3" * 64, "baseline_receipts": plan["learn_code_contract"]["baseline_receipts"],
            "compatibility_image_backup_receipt_sha256": "a" * 64}
        namespace = {"REQUEST": request}
        exec(compile(activate.remote_program().rsplit("activation_main()", 1)[0], "<mocked-activation>", "exec"), namespace)
        commands, written = [], []
        running = [False]
        stop_attempted = [False]
        class FakePath:
            def __init__(self, value): self.value = str(value)
            def __truediv__(self, value): return FakePath(self.value + "/" + str(value))
            def __str__(self): return self.value
            @property
            def parents(self): return []
            def is_dir(self): return True
            def is_symlink(self): return False
            def mkdir(self, **kwargs): pass
        def command(argv, *args, **kwargs):
            commands.append(argv)
            if "up" in argv:
                running[0] = True
                if fail_up: raise namespace["ActivateError"]("ACTIVATION_COMMAND_FAILED")
            return b""
        services = {name: {} for name in (*activate.API_ORDER, "frontend", *targets)}
        def run(argv, **kwargs):
            commands.append(argv)
            if argv[:2] == ["docker", "stop"]:
                stop_attempted[0] = True
                self.assertEqual(kwargs.get("limit"), 180)
                if stop_outcome in {"timeout-running", "timeout-stopped", "inspect-error"}:
                    if stop_outcome == "timeout-stopped":
                        for item in current["containers"]:
                            if item["container_id"] in argv: item.update(running=False, pid=0)
                    raise TimeoutError("synthetic stop timeout")
                if stop_outcome == "ack-running":
                    return b""
                for item in current["containers"]:
                    if item["container_id"] in argv: item.update(running=False, pid=0)
                return b""
            return json.dumps({"services": {name: {"image": result["images"][verify.SERVICE_IMAGES[name]]} for name in targets}}).encode()
        def inspect_containers():
            commands.append(["PURE_READ_CONTAINERS"])
            if stop_attempted[0] and stop_outcome == "inspect-error":
                raise RuntimeError("synthetic inspection failure")
            return copy.deepcopy(current if running[0] else before)["containers"]
        def read_state():
            return {**state, **({"events_sha256": "f" * 64} if drift and running[0] else {})}
        namespace.update(Path=FakePath, ROOT=FakePath("/home/deploy/CTF_Web"),
            safe_snapshot=lambda: copy.deepcopy(current if running[0] else before), compose=lambda: {"services": services}, check_images=lambda: None,
            derive_workers=lambda *args: [name for name in targets if name != "learn-service"], check_backend_baseline=lambda *args, **kwargs: {},
            containers=inspect_containers, sql=lambda *args: 0,
            learn_code_snapshot=read_state, private_json=lambda path, value: written.append((str(path), copy.deepcopy(value))),
            private_command=command, run=run, copy_managed=lambda *args: None, wait_service=lambda *args: None,
            schema_contract=lambda: {"heads": activation["schema"]["heads"], "trigger_count": 18, "required_schema_verified": True},
            category_compatibility=lambda: {"custom_category_count": 2, "custom_event_count": 1,
                "legacy_enum_image_rollback_forbidden": True, "automatic_backend_rollback_performed": False},
            wait_personal_dependency_recovery=lambda *args: (copy.deepcopy(current), {}))
        with redirect_stdout(io.StringIO()) as output: namespace["activation_main"]()
        return json.loads(output.getvalue()), commands, written

    def test_serialized_remote_success_executes_no_migration_and_preserves_custom_data(self):
        value, commands, written = self.mocked_activation()
        self.assertEqual(value["status"], "activated")
        self.assertFalse(value["migration_executed"])
        self.assertEqual(value["learn_code_before"], value["learn_code_after"])
        self.assertTrue(value["category_compatibility"]["legacy_enum_image_rollback_forbidden"])
        self.assertFalse(any("run" in argv or any("migrate" in str(arg) for arg in argv) for argv in commands))
        self.assertTrue(any(path.endswith("/intent.json") and item["release_phase"] == preflight.LEARN_CODE_PHASE for path, item in written))

    def test_serialized_remote_failure_fences_new_writers_without_migration_or_restore(self):
        for kwargs in ({"fail_up": True}, {"drift": True}):
            with self.subTest(kwargs=kwargs):
                value, commands, _ = self.mocked_activation(**kwargs)
                self.assertEqual(value["status"], "partial-unverified")
                self.assertTrue(value["writer_fence_maintained"])
                self.assertFalse(value["migration_executed"])
                self.assertFalse(value["database_restore_performed"])
                stopped = [argv for argv in commands if argv[:2] == ["docker", "stop"]]
                self.assertEqual(len(stopped), 1)
                self.assertEqual(stopped[0][2:4], ["--time", "-1"])
                self.assertFalse(any("run" in argv for argv in commands))

    def test_uncertain_graceful_stop_is_inspected_without_retry_restore_or_success(self):
        for outcome in ("timeout-running", "timeout-stopped", "ack-running", "inspect-error"):
            with self.subTest(outcome=outcome):
                value, commands, _ = self.mocked_activation(fail_up=True, stop_outcome=outcome)
                self.assertEqual(value["status"], "partial-unverified")
                self.assertEqual(value["error"], "ACTIVATION_COMMAND_FAILED")
                self.assertFalse(value["migration_executed"])
                self.assertFalse(value["database_restore_performed"])
                self.assertFalse(value["old_migration_jobs_executed"])
                self.assertNotIn("frontend_image_rolled_back", value)
                stops = [index for index, argv in enumerate(commands) if argv[:2] == ["docker", "stop"]]
                self.assertEqual(len(stops), 1)
                stop = commands[stops[0]]
                self.assertEqual(stop[2:4], ["--time", "-1"])
                self.assertNotIn("kill", stop)
                self.assertEqual(commands[stops[0] + 1], ["PURE_READ_CONTAINERS"])
                if outcome == "timeout-stopped":
                    self.assertTrue(value["writer_fence_maintained"])
                else:
                    self.assertIsNot(value.get("writer_fence_maintained"), True)
                if outcome != "ack-running":
                    self.assertTrue(value["recovery_outcome_unverified"])
                self.assertFalse(any("run" in argv for argv in commands))

    def test_legacy_migration_failure_keeps_its_existing_stop_command(self):
        tree = ast.parse(activate.REMOTE)
        stops = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run" and node.args and isinstance(node.args[0], ast.List):
                prefix = [item.value for item in node.args[0].elts[:4] if isinstance(item, ast.Constant)]
                if prefix[:2] == ["docker", "stop"]: stops.append(prefix)
        self.assertCountEqual(stops, [["docker", "stop", "--time", "-1"], ["docker", "stop", "--time", "30"]])

    def test_phase_cli_rejects_missing_scope_or_baseline_before_remote_access(self):
        with patch.object(preflight.ops, "remote_python") as remote:
            with self.assertRaisesRegex(preflight.ReleaseError, "BASELINE_REQUIRED"):
                preflight.main(["--plan", "unused", "--learn-code-only"])
            remote.assert_not_called()
        for module in (stage, verify):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            self.assertTrue(any(isinstance(node, ast.Constant) and node.value == "--learn-code-only" for node in ast.walk(tree)))

    def test_serialized_build_consumes_the_new_phase_before_creating_any_release(self):
        from test_stage_build_release import context_payloads
        _, plan, _, build, *_ = code_fixture()
        payloads = {name: value for name, value in context_payloads().items() if name.startswith("services/learn/")}
        archive, rows = stage.make_tar(payloads, dict.fromkeys(payloads, 0o644), contexts=stage.selected_contexts(learn_only=True))
        request = {"binding": build["binding"], "files": rows, "source_file_count": len(rows), "tar_bytes": len(archive),
            "tar_sha256": stage.digest(archive), "frontend_only": False, "learn_only": True, "baseline_chain": True,
            "release_phase": preflight.LEARN_CODE_PHASE, "learn_code_contract": plan["learn_code_contract"],
            "backend_baseline": {}, "preflight": {"protected_files": plan["inspection"]["protected_files"],
                "containers": plan["inspection"]["containers"], "learn_code_contract": plan["learn_code_contract"]}}
        namespace = {"REQUEST": request}
        exec(compile(stage.remote_program().rsplit("remote_main()", 1)[0], "<phase-build>", "exec"), namespace)
        namespace["live_snapshot"] = lambda: {**plan["inspection"], "frontend_args": {}}
        namespace["check_backend_baseline"] = lambda *args: None
        def stop_before_any_build(*args, **kwargs):
            raise namespace["StageError"]("PURE_BEFORE_BUILD_STOP")
        namespace["build_commands"] = stop_before_any_build
        with patch.object(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(archive)})()), redirect_stdout(io.StringIO()) as output:
            namespace["remote_main"]()
        value = json.loads(output.getvalue())
        self.assertEqual(value["error"], "PURE_BEFORE_BUILD_STOP")
        self.assertEqual(value["learn_code_contract"], plan["learn_code_contract"])
        self.assertEqual(value["release_phase"], preflight.LEARN_CODE_PHASE)
        self.assertEqual(value["completed_images"], [])

    def test_serialized_readonly_verifier_executes_both_code_snapshots_and_runtime_checks(self):
        _, plan, _, _, _, request, current, _, state, labels, heads, triggers = code_fixture()
        namespace = {"REQUEST": request}
        exec(compile(verify.remote_program().rsplit("verify_main()", 1)[0], "<phase-readonly>", "exec"), namespace)
        namespace["verify_snapshot"] = lambda: copy.deepcopy(current)
        namespace["database_rows"] = lambda _: plan["inspection"]["databases"]
        state_reads = []
        def read(argv, **kwargs):
            if argv[:3] == ["docker", "image", "inspect"]:
                return json.dumps([{"Id": argv[-1], "Config": {"Labels": labels[argv[-1]]}}]).encode()
            query, database = argv[-1], argv[-2]
            if query == preflight.LEARN_CODE_STATE_SQL:
                state_reads.append(query)
                value = {key: value for key, value in state.items() if key != "schema_sha256"}
            elif "version_num" in query:
                value = heads[next(name for name, actual in preflight.DATABASES.items() if actual == database)]
            elif "pg_trigger" in query: value = triggers
            elif "registry_present" in query: value = category_projection(custom_category_count=2)
            else: raise AssertionError("UNEXPECTED_SQL")
            return json.dumps(value).encode()
        namespace["run"] = read
        with patch("time.sleep", return_value=None), redirect_stdout(io.StringIO()) as output:
            namespace["verify_main"]()
        value = json.loads(output.getvalue())
        self.assertEqual(value["status"], "passed")
        self.assertEqual(len(state_reads), 2)
        verify.validate_result(value, request["binding"], expected_heads=request["expected_heads"], learn_code_only=True)

    def test_frontend_after_code_chain_retains_029_source_and_requires_compatible_archive(self):
        _, _, baseline, build, activation, code, _, _, _, *_ = code_fixture()
        binding = verify.binding_for("3" * 40, "4" * 64, "frontend-after-code")
        following = copy.deepcopy(build)
        for value in (following, following["remote"]):
            value.pop("release_phase"); value.pop("learn_code_contract")
            value.update(binding=binding, frontend_only=True, learn_only=False)
        following["remote"]["retained_baseline"] = verify.baseline_summary(code, ("frontend",))
        following["remote"]["completed_images"] = [{"service": "frontend", "image_id": "sha256:" + "f" * 64,
            "tag": f"ctf-renewal/frontend:{binding['commit']}-{binding['release_nonce']}", "frontend_args_sha256": code["frontend_args_sha256"]}]
        built = verify.build_contract(following, binding, frontend_only=True, baseline=code)
        selected = [item for item in code["managed_files"] if item["path"].startswith("frontend/")]
        retained = [item for item in code["managed_files"] if not item["path"].startswith("frontend/")]
        after = {key: value for key, value in activation.items() if key not in {
            "release_phase", "learn_code_contract", "learn_code_before", "learn_code_after", "migration_executed", "untargeted_runtime_before", "untargeted_runtime_after"}}
        after.update(binding=binding, frontend_only=True, learn_only=False, managed_files=selected,
                     unmanaged_files=verify.file_rows(retained + code["unmanaged_files"]))
        result = verify.activation_contract(after, binding, selected, built, baseline=code)
        self.assertEqual(result["images"]["learn"], code["images"]["learn"])
        self.assertEqual(result["image_bindings"]["learn"], code["image_bindings"]["learn"])
        after.pop("compatibility_image_backup_receipt_sha256")
        with self.assertRaisesRegex(verify.VerifyError, "COMPATIBILITY_IMAGE_BACKUP_MISSING"):
            verify.activation_contract(after, binding, selected, built, baseline=code)


if __name__ == "__main__":
    unittest.main()
