import asyncio
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

RENEWAL = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(RENEWAL))
import activate_release as ACT

BINDING = {"commit":"a"*40,"source_fingerprint":"b"*64,"release_nonce":"11111111-2222-4333-8444-555555555555"}
PRIOR = {"commit":"c"*40,"source_fingerprint":"d"*64,"release_nonce":"baseline-nonce-123"}
SCHEMA = {"heads":{name:[head] for name,head in ACT.EXPECTED_HEADS.items()},"trigger_count":18,"required_schema_verified":True}


def row(name,payload):
    return {"path":name,"bytes":len(payload),"sha256":ACT.sha(payload)}


class ActivateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.temporary.cleanup()

    def test_steps_baseline_precedes_every_worker_and_frontend_is_last(self):
        steps = ACT.activation_steps(sorted(ACT.NEW_WORKERS))
        self.assertEqual([subject for action,subject in steps if action == "migration"],["auth","learn","community"])
        baseline = steps.index(("baseline","auth"))
        self.assertTrue(all(steps.index(("up",worker)) > baseline for worker in ACT.NEW_WORKERS))
        self.assertEqual(steps[-3:],[("up","frontend"),("health","frontend"),("verify","all")])
        self.assertEqual(ACT.activation_steps(frontend_only=True),[("source","frontend"),("up","frontend"),("health","frontend"),("verify","all")])

    def test_worker_derivation_rejects_unknown_or_mismatching_consumers(self):
        current = [{"service":name,"image_id":"sha256:"+str(index)*64} for index,name in enumerate(ACT.API_ORDER,1)]
        services = {name:{} for name in ACT.SERVICE_IMAGES}
        self.assertEqual(ACT.derive_workers(current,services),sorted(ACT.NEW_WORKERS))
        for extra in ({"service":"unknown-runner","image_id":current[0]["image_id"]},
                      {"service":"community-inquiry-runner","image_id":"sha256:"+"9"*64}):
            with self.assertRaises(ACT.ActivateError):
                ACT.derive_workers(current+[extra],services)

    def _api_containers(self):
        return [{"service":name,"image_id":"sha256:"+str(index)*64}
                for index,name in enumerate(ACT.API_ORDER,1)]

    def test_template_workers_are_recognized_before_creation_and_on_redeployment(self):
        apis = self._api_containers()
        api_images = {item["service"]:item["image_id"] for item in apis}
        services = {name:{} for name in ACT.SERVICE_IMAGES if name not in ACT.NEW_WORKERS}
        legacy = [{"service":name,"image_id":api_images[family+"-service"]}
                  for name,family in ACT.SERVICE_IMAGES.items()
                  if name not in (*ACT.API_ORDER,"frontend",*ACT.NEW_WORKERS)]
        existing = [{"service":name,"image_id":api_images[api]}
                    for name,(api,*_) in ACT.NEW_WORKERS.items()]
        expected = sorted(set(ACT.SERVICE_IMAGES)-set(ACT.API_ORDER)-{"frontend"})
        for prior_workers in ([],existing):
            with self.subTest(existing_new_workers=bool(prior_workers)):
                current = apis+legacy+prior_workers
                preserved = copy.deepcopy((current,services))
                self.assertEqual(ACT.derive_workers(current,services),expected)
                self.assertEqual((current,services),preserved)

    def test_template_worker_name_does_not_allow_another_api_image_family(self):
        apis = self._api_containers()
        services = {name:{} for name in ACT.SERVICE_IMAGES if name not in ACT.NEW_WORKERS}
        for name,(api,*_) in ACT.NEW_WORKERS.items():
            for other in apis:
                if other["service"] == api:
                    continue
                with self.subTest(worker=name,wrong_api=other["service"]),self.assertRaisesRegex(ACT.ActivateError,"WORKER_API_IMAGE_MISMATCH"):
                    ACT.derive_workers(apis+[{"service":name,"image_id":other["image_id"]}],services)

    def test_unknown_same_image_consumers_fail_even_if_compose_defines_them(self):
        apis = self._api_containers()
        for api in apis:
            name = "unknown-"+api["service"]+"-runner"
            for present in (False,True):
                services = {item["service"]:{} for item in apis}
                if present:
                    services[name] = {}
                with self.subTest(api=api["service"],compose_present=present),self.assertRaisesRegex(ACT.ActivateError,"UNKNOWN_SAME_IMAGE_CONSUMER"):
                    ACT.derive_workers(apis+[{"service":name,"image_id":api["image_id"]}],services)

    def test_legacy_worker_missing_from_base_compose_remains_forbidden(self):
        apis = self._api_containers()
        api_images = {item["service"]:item["image_id"] for item in apis}
        for name,family in ACT.SERVICE_IMAGES.items():
            if name in (*ACT.API_ORDER,"frontend",*ACT.NEW_WORKERS):
                continue
            services = {item:{} for item in ACT.SERVICE_IMAGES if item != name}
            with self.subTest(worker=name),self.assertRaisesRegex(ACT.ActivateError,"UNKNOWN_SAME_IMAGE_CONSUMER"):
                ACT.derive_workers(apis+[{"service":name,"image_id":api_images[family+"-service"]}],services)

    def _model(self):
        environment = {key:value if "${" not in str(value) else "synthetic" for template in ACT.worker_templates().values() for key,value in template["environment"].items()}
        environment.update({"ENVIRONMENT":"production","EXISTING":"synthetic","PYTHONPATH":"/app","MINIO_SECRET_KEY":"synthetic","CTFD_ADMIN_TOKEN":"synthetic"})
        api = {"environment":environment,"networks":{"ctf-internal":{"aliases":["api"]},"public":{}},
               "secrets":[{"source":"auth_jwt_private_key","target":"private"},{"source":"platform_jwt_public_key","target":"public"},{"source":"unneeded","target":"unneeded"}],"volumes":["data:/app/data"],
               "cap_drop":["ALL"],"security_opt":["no-new-privileges:true"],"read_only":True,"user":"1001:1001",
               "tmpfs":["/tmp"],"ports":["8080:8000"],"depends_on":{"postgres":{}},"container_name":"existing",
               "deploy":{"resources":{"limits":{"memory":"1G"}},"replicas":1}}
        return {"services":{**{name:copy.deepcopy(api) for name in ACT.API_ORDER},"frontend":{"image":"old"}}}

    def test_overlay_copies_api_security_without_ports_or_secret_disclosure(self):
        model = self._model()
        images = {name:"sha256:"+str(index)*64 for index,name in enumerate(("auth","learn","community","frontend"),1)}
        templates = ACT.worker_templates()
        overlay = ACT.make_overlay(model,images,BINDING,sorted(ACT.NEW_WORKERS),Path("/private/runtime"),templates=templates)
        for name,(api,module,port,key) in ACT.NEW_WORKERS.items():
            worker = overlay["services"][name]
            self.assertTrue(not set(worker) & {"ports","container_name","depends_on"})
            expected_secrets = {item if isinstance(item,str) else item["source"] for item in templates[name]["secrets"]}
            self.assertEqual({item["source"] for item in worker["secrets"]},expected_secrets)
            self.assertEqual(worker["security_opt"],model["services"][api]["security_opt"])
            self.assertEqual(worker["networks"],{"ctf-internal":{}})
            self.assertNotIn("volumes",worker)
            self.assertNotIn("MINIO_SECRET_KEY",worker["environment"])
            self.assertNotIn("CTFD_ADMIN_TOKEN",worker["environment"])
            self.assertEqual(worker["command"],["python","-m",module])
            self.assertEqual(worker["environment"][key],str(port))
        for job in ACT.MIGRATION_IMAGES:
            definition = overlay["services"][job]
            self.assertEqual(definition["environment"]["PGOPTIONS"],ACT.PGOPTIONS)
            self.assertEqual(definition["environment"]["PYTHONPATH"],"/renewal-runtime:/app")
            self.assertTrue(definition["volumes"][-1]["read_only"])
        self.assertIn("unneeded",{item["source"] for item in model["services"]["auth-service"]["secrets"]})

    def test_redeployment_without_base_worker_uses_the_same_approved_template(self):
        model,apis = self._model(),self._api_containers()
        api_images = {item["service"]:item["image_id"] for item in apis}
        existing = [{"service":name,"image_id":api_images[api]}
                    for name,(api,*_) in ACT.NEW_WORKERS.items()]
        images = {family:"sha256:"+char*64 for family,char in zip(("auth","learn","community","frontend"),"abcd")}
        templates = ACT.worker_templates()
        preserved = copy.deepcopy((model,templates))
        first = ACT.make_overlay(model,images,BINDING,ACT.derive_workers(apis,model["services"]),Path("/runtime"),templates=templates)
        repeated = ACT.make_overlay(model,images,BINDING,ACT.derive_workers(apis+existing,model["services"]),Path("/runtime"),templates=templates)
        self.assertEqual(repeated,first)
        self.assertEqual((model,templates),preserved)
        for name,(api,module,port,key) in ACT.NEW_WORKERS.items():
            worker = repeated["services"][name]
            self.assertEqual(worker["image"],images[ACT.SERVICE_IMAGES[api]])
            self.assertEqual(worker["command"],["python","-m",module])
            self.assertEqual(worker["environment"][key],str(port))
            self.assertFalse(set(worker) & {"ports","volumes","depends_on","container_name","privileged"})

    def test_recognized_existing_worker_still_requires_its_template(self):
        model,apis = self._model(),self._api_containers()
        api_images = {item["service"]:item["image_id"] for item in apis}
        current = apis+[{"service":name,"image_id":api_images[api]}
                      for name,(api,*_) in ACT.NEW_WORKERS.items()]
        images = dict.fromkeys(("auth","learn","community","frontend"),"sha256:"+"a"*64)
        workers = ACT.derive_workers(current,model["services"])
        for name in ACT.NEW_WORKERS:
            templates = ACT.worker_templates()
            del templates[name]
            with self.subTest(worker=name),self.assertRaisesRegex(ACT.ActivateError,"WORKER_TEMPLATE_REQUIRED"):
                ACT.make_overlay(model,images,BINDING,workers,Path("/runtime"),templates=templates)

    def test_existing_base_worker_runtime_is_preserved_during_redeployment(self):
        model,apis = self._model(),self._api_containers()
        api_images = {item["service"]:item["image_id"] for item in apis}
        current = apis+[{"service":name,"image_id":api_images[api]}
                      for name,(api,*_) in ACT.NEW_WORKERS.items()]
        for name,template in ACT.worker_templates().items():
            model["services"][name] = ACT.minimal_worker(model["services"][ACT.NEW_WORKERS[name][0]],template)
        preserved = copy.deepcopy(model)
        images = dict.fromkeys(("auth","learn","community","frontend"),"sha256:"+"a"*64)
        overlay = ACT.make_overlay(model,images,BINDING,ACT.derive_workers(current,model["services"]),Path("/runtime"))
        self.assertEqual(model,preserved)
        for name in ACT.NEW_WORKERS:
            self.assertEqual(overlay["services"][name],{"image":images[ACT.SERVICE_IMAGES[name]],"labels":ACT.source_labels(BINDING)})

    def test_frontend_overlay_contains_no_backend_migration_or_runtime_values(self):
        overlay = ACT.make_overlay(self._model(),{"frontend":"sha256:"+"a"*64},BINDING,[],Path("/unused"),frontend_only=True)
        self.assertEqual(set(overlay["services"]),{"frontend"})
        self.assertEqual(set(overlay["services"]["frontend"]),{"image","labels"})

    def test_resolved_dollar_values_are_escaped_and_existing_migration_environment_is_not_recopied(self):
        model = self._model()
        model["services"]["auth-service"]["environment"]["DATABASE_URL"] = "synthetic-$NAME-${NAME}"
        model["services"]["auth-migrate"] = {"environment":{"DATABASE_URL":"synthetic-$NAME","PYTHONPATH":"/app/$PATH"}}
        images = dict.fromkeys(("auth","learn","community","frontend"),"sha256:"+"a"*64)
        overlay = ACT.make_overlay(model,images,BINDING,sorted(ACT.NEW_WORKERS),Path("/runtime"),templates=ACT.worker_templates())
        self.assertEqual(overlay["services"]["personal-notification-runner"]["environment"]["DATABASE_URL"],"synthetic-$$NAME-$${NAME}")
        migration = overlay["services"]["auth-migrate"]["environment"]
        self.assertEqual(set(migration),{"PGOPTIONS","PYTHONPATH"})
        self.assertEqual(migration["PYTHONPATH"],"/renewal-runtime:/app/$$PATH")

    def test_canonical_worker_production_security_is_preserved(self):
        templates = ACT.worker_templates()
        auth = templates["personal-notification-runner"]
        self.assertEqual(set(auth["cap_add"]),{"CHOWN","SETUID","SETGID"})
        self.assertEqual(auth["deploy"]["resources"]["limits"],{"memory":"256M","cpus":"0.5"})
        for name,template in templates.items():
            self.assertTrue(template["read_only"])
            self.assertEqual(template["cap_drop"],["ALL"])
            self.assertEqual(template["security_opt"],["no-new-privileges:true"])
            self.assertNotIn("volumes",template)

    def test_copy_only_selected_files_preserves_unknown_extras_and_raw_bytes(self):
        staged,live = self.root/"staged",self.root/"live"
        name = "services/auth/entrypoint.sh"
        for root,payload in ((staged,b"#!/bin/sh\nnew\n"),(live,b"old\r\n")):
            (root/name).parent.mkdir(parents=True)
            (root/name).write_bytes(payload)
        (live/"services/auth/unknown.txt").write_bytes(b"preserve")
        target = [row(name,(staged/name).read_bytes())]
        ACT.copy_managed(staged,live,target,{name:0o755},[row(name,b"old\r\n")],BINDING["release_nonce"])
        self.assertEqual((live/name).read_bytes(),b"#!/bin/sh\nnew\n")
        self.assertEqual((live/"services/auth/unknown.txt").read_bytes(),b"preserve")
        with self.assertRaisesRegex(ACT.ActivateError,"LIVE_MANAGED_FILE_CHANGED"):
            ACT.copy_managed(staged,live,target,{name:0o755},[row(name,b"old\r\n")],BINDING["release_nonce"])

    def test_copy_validates_all_files_before_first_mutation(self):
        staged,live = self.root/"staged",self.root/"live"
        names = ["frontend/a.txt","services/auth/b.txt"]
        for root in (staged,live):
            for name in names:
                (root/name).parent.mkdir(parents=True,exist_ok=True)
                (root/name).write_bytes(b"old" if root == live else b"new")
        with self.assertRaisesRegex(ACT.ActivateError,"STAGED_SOURCE_CHANGED"):
            ACT.copy_managed(staged,live,[row(names[0],b"new"),row(names[1],b"wrong")],dict.fromkeys(names,0o644),[row(name,b"old") for name in names],BINDING["release_nonce"])
        self.assertTrue(all((live/name).read_bytes() == b"old" for name in names))

    def test_timeout_wrapper_only_patches_child_connect_and_preserves_settings(self):
        calls = []
        async def connect(*args,**kwargs):
            calls.append((args,kwargs))
            return "connected"
        module = SimpleNamespace(connect=connect)
        with patch.dict(sys.modules,{"asyncpg":module}),patch.dict(os.environ,{"PGOPTIONS":ACT.PGOPTIONS}):
            exec(compile(ACT.MIGRATION_SITE_CUSTOMIZE,"sitecustomize.py","exec"),{})
            result = asyncio.run(module.connect("synthetic",server_settings={"application_name":"preserved","lock_timeout":"1"}))
        self.assertEqual(result,"connected")
        self.assertEqual(calls[0][1]["server_settings"],{"application_name":"preserved","lock_timeout":"10000","statement_timeout":"120000"})
        self.assertIs(sys.modules.get("asyncpg"),None)

    def test_backup_requires_image_capture_and_exact_old_ids(self):
        current = [{"service":name,"image_id":"sha256:"+str(index)*64,"running":True} for index,name in enumerate((*ACT.API_ORDER,"frontend"),1)]
        backup = {"schema_version":1,"kind":"renewal-stream-backup","nonce":BINDING["release_nonce"],"source_fingerprint":BINDING["source_fingerprint"],
                  "plan_sha256":"1"*64,"quiescence_sha256":"2"*64,"quiescence_verified":True,"release_eligible":True,"cross_service_consistency_verified":True,
                  "image_backup_verified":True,"database_count":3,"ctfd_db_included":False,"files":[{}]*8,
                  "selected_image_ids":sorted(row["image_id"] for row in current),"image_backup_receipt_sha256":"3"*64}
        ACT.backup_contract(backup,BINDING,"1"*64,"2"*64,current)
        for key,value in (("image_backup_verified",False),("selected_image_ids",[]),("files",[{}]*7)):
            with self.assertRaises(ACT.ActivateError):
                ACT.backup_contract({**backup,key:value},BINDING,"1"*64,"2"*64,current)

    def test_backup_archives_are_stream_hash_checked_under_private_root(self):
        path = self.root/"archive.tar"
        path.write_bytes(b"archive")
        value = {"files":[row(str(path),b"archive")]}
        self.assertEqual(ACT.verify_backup_files(value,self.root),1)
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ACT.ActivateError,"BACKUP_FILE_HASH_CHANGED"):
            ACT.verify_backup_files(value,self.root)

    def test_remote_bootstrap_has_small_command_and_stdin_bound_program(self):
        command,payload = ACT.execution_payload({"binding":BINDING})
        self.assertLess(len(command),2000)
        value = json.loads(payload)
        program = base64.b64decode(value["program"]).decode()
        compile(program,"<activation-remote>","exec")
        self.assertEqual(program,ACT.remote_program())
        self.assertNotIn("sudo",program)

    def test_legacy_wait_requires_two_stable_process_observations_without_claiming_health(self):
        namespace = {}
        exec(compile(ACT.remote_program().rsplit("activation_main()",1)[0],"<pure-wait-fixture>","exec"),namespace)
        name = "knowledge-candidate-runner"
        item = {"service":name,"container_id":"1"*64,"image_id":"sha256:"+"2"*64,"running":True,"pid":123,
                "started_at":"2026-10-13T06:00:00Z","restart_count":0,"health":None}
        observed = []
        def containers():
            observed.append(1)
            return [dict(item)]
        ticks = iter(range(0,1000))
        namespace.update({"containers":containers,"run":lambda *args:json.dumps([{"Config":{"Cmd":["python","-m",ACT.LEGACY_PROCESS_WORKERS[name]],"Labels":ACT.source_labels(BINDING)}}]).encode(),
                          "time":SimpleNamespace(monotonic=lambda:next(ticks),sleep=lambda _:None)})
        result = namespace["wait_service"](name,item["image_id"],BINDING)
        self.assertEqual(len(observed),2)
        self.assertFalse(result["readiness_available"])
        self.assertTrue(result["process_stable"])
        self.assertIsNone(result["health"])
        def unstable():
            item["restart_count"] += 1
            return [dict(item)]
        namespace["containers"] = unstable
        with self.assertRaisesRegex(namespace["ActivateError"],"TARGET_HEALTH_UNVERIFIED"):
            namespace["wait_service"](name,item["image_id"],BINDING)

    def _simulate(self, *, frontend_only=False, learn_only=False, fail_health=None, ambiguous_up=False, frontend_prior=None, existing_new_workers=False,
                  dependency_unready=False, dependency_recovery_failure=False, full_learn_head=None):
        namespace = {}
        exec(compile(ACT.remote_program().rsplit("activation_main()",1)[0],"<pure-activate-fixture>","exec"),namespace)
        live = self.root/"live"
        release_root = self.root/"releases"
        release = release_root/BINDING["release_nonce"]
        staged = release/"source"
        names = ("frontend/app.tsx","services/auth/app.py","services/learn/app.py","services/community/app.py")
        for name in names:
            (live/name).parent.mkdir(parents=True,exist_ok=True)
            (staged/name).parent.mkdir(parents=True,exist_ok=True)
            (live/name).write_bytes(b"old\n")
            (staged/name).write_bytes(b"new\n" if not frontend_only or name.startswith("frontend/") else b"old\n")
        (live/"services/auth/unknown.txt").write_bytes(b"preserve\n")
        old_images = {family:"sha256:"+str(index)*64 for index,family in enumerate(("auth","learn","community","frontend"),1)}
        images = {family:"sha256:"+char*64 for family,char in zip(("auth","learn","community","frontend"),"abcd")}
        if frontend_only:
            images.update({family:old_images[family] for family in ("auth","learn","community")})
        if learn_only:
            images.update({family:old_images[family] for family in ("auth","community","frontend")})
        workers = sorted(set(ACT.SERVICE_IMAGES)-set(ACT.API_ORDER)-{"frontend"})
        current = [{"service":service,"container_id":f"{index:064x}","image_id":old_images[family],
                    "running":frontend_only or service=="frontend" or learn_only and family!="learn","pid":123 if frontend_only or service=="frontend" or learn_only and family!="learn" else 0,
                    "health":None if service in ACT.LEGACY_PROCESS_WORKERS else "healthy","started_at":"2026-10-13T06:00:00Z","restart_count":0,"mounts":[],
                    **({"command":list(ACT.PERSONAL_WORKER_COMMAND)} if service==ACT.PERSONAL_WORKER else {}),
                    **({"command":["python","-m",ACT.LEGACY_PROCESS_WORKERS[service]]} if service in ACT.LEGACY_PROCESS_WORKERS else {})}
                   for index,(service,family) in enumerate(ACT.SERVICE_IMAGES.items(),1) if frontend_only or learn_only or existing_new_workers or service not in ACT.NEW_WORKERS]
        current.extend({"service":service,"container_id":f"{index:064x}","image_id":"sha256:"+"9"*64,"running":True,"pid":200,"health":"healthy"}
                       for index,service in enumerate(("postgres","minio"),100))
        protected = [row("/home/deploy/CTF_Web/"+name,b"protected") for name in ("infra/.env","infra/docker-compose.yml","infra/docker-compose.prod.yml","infra/caddy/Caddyfile","services/auth/jwt_private.pem","services/auth/jwt_public.pem")]
        managed = sorted([row(name,(staged/name).read_bytes()) for name in names if not frontend_only or name.startswith("frontend/")],key=lambda item:item["path"])
        if learn_only:
            managed = [item for item in managed if item["path"].startswith("services/learn/")]
        model = self._model()
        model["services"].update({name:{} for name in workers if name not in ACT.NEW_WORKERS})
        def snapshot():
            files = sorted([row(path.relative_to(live).as_posix(),path.read_bytes()) for path in live.rglob("*") if path.is_file()],key=lambda item:item["path"])
            return {"containers":copy.deepcopy(current),"protected_files":protected,"code_files":files,
                    "source_labels":{item["container_id"]:ACT.source_labels(frontend_prior if item["service"] == "frontend" and frontend_prior else PRIOR) for item in current}}
        before = snapshot()
        if dependency_unready:
            next(item for item in current if item["service"]==ACT.PERSONAL_WORKER)["health"]="unhealthy"
        request = {"binding":BINDING,"frontend_only":frontend_only,"managed_files":managed,"modes":{item["path"]:0o644 for item in managed},
                   "images":images,"protected_files":protected,
                   "current_container_baseline":[{key:value for key,value in item.items() if key!="command"} for item in before["containers"]],
                   "plan_sha256":"1"*64,"backup_receipt_sha256":"2"*64,"quiescence_sha256":"3"*64,
                   "migration_wrapper_sha256":ACT.sha(ACT.MIGRATION_SITE_CUSTOMIZE.encode()),
                   "worker_templates":None if frontend_only else ACT.worker_templates(),
                   "backend_baseline":{"binding":PRIOR,"images":old_images,"managed_files":[row(name,b"old\n") for name in names],"protected_files":protected}}
        if learn_only:
            request.update({"learn_only":True,"baseline_receipts":[],"expected_heads":{**ACT.EXPECTED_HEADS,"learn":ACT.verify.LEARN_CATEGORY_HEAD}})
        elif full_learn_head is not None:
            self.assertFalse(frontend_only)
            request["expected_heads"]={**ACT.EXPECTED_HEADS,"learn":full_learn_head}
        events = []
        ready = False
        def private_command(command,log,timeout=600):
            nonlocal ready
            events.append(command)
            log.write_bytes(b"private synthetic log")
            if "--baseline-only" in command:
                ready = True
            elif "up" in command:
                service = command[-1]
                if ambiguous_up:
                    raise TimeoutError("synthetic")
                target = next((item for item in current if item["service"]==service),None)
                if target is None:
                    target = {"service":service,"container_id":f"{len(current)+300:064x}"}
                    current.append(target)
                rollback = "rollback-frontend" in str(log)
                target.update({"image_id":old_images["frontend"] if rollback else images[ACT.SERVICE_IMAGES[service]],"running":True,"pid":300,
                               "health":None if service in ACT.LEGACY_PROCESS_WORKERS else "healthy"})
                if learn_only and service=="learn-service" and not dependency_recovery_failure:
                    next(item for item in current if item["service"]==ACT.PERSONAL_WORKER)["health"]="healthy"
        self.wait_bindings = []
        def wait_service(service,image,binding):
            self.wait_bindings.append((service,image,copy.deepcopy(binding)))
            target = next(item for item in current if item["service"]==service)
            if service == fail_health and image == images[ACT.SERVICE_IMAGES[service]]:
                target["health"] = "unhealthy"
                raise namespace["ActivateError"]("TARGET_HEALTH_UNVERIFIED")
            return target
        def run(command,limit=120):
            if command[-3:] == ["config","--format","json"]:
                overlay = json.loads(Path(command[command.index("-f",command.index("-f",command.index("-f")+1)+1)+1]).read_bytes())
                return json.dumps({"services":{name:{"image":images[ACT.SERVICE_IMAGES[name]]} for name in overlay["services"] if name in ACT.SERVICE_IMAGES}}).encode()
            if command[:2] == ["docker","stop"]:
                events.append(command)
                for item in current:
                    if item["container_id"] in command:
                        item.update({"running":False,"pid":0})
                return b""
            raise AssertionError("REAL_COMMAND_FORBIDDEN")
        schema = copy.deepcopy(SCHEMA)
        if learn_only: schema["heads"]["learn"] = [ACT.verify.LEARN_CATEGORY_HEAD]
        elif full_learn_head is not None: schema["heads"]["learn"] = [full_learn_head]
        clock={"now":0.0}
        def monotonic():
            clock["now"]+=0.01
            return clock["now"]
        def probe_personal_worker(container_id):
            item=next(item for item in current if item["container_id"]==container_id)
            return {"health_status":200,"ready_status":503 if item["health"]=="unhealthy" else 200,"baseline_complete":True}
        namespace.update({"REQUEST":request,"ROOT":live,"RELEASE_ROOT":str(release_root),"safe_snapshot":snapshot,"compose":lambda:model,
                          "containers":lambda:copy.deepcopy(current),"sql":lambda *args:0,"schema_contract":lambda:copy.deepcopy(schema),
                          "category_compatibility":lambda:{"custom_category_count":0,"custom_event_count":0,"legacy_enum_image_rollback_forbidden":False,"automatic_backend_rollback_performed":False},
                          "category_before_migration":lambda:{"previous_head":"20261013_028","registry_absent":True,"legacy_constraint_valid":True,"custom_event_count":0},
                          "notification_state":lambda:{"ready":ready,"notifications":0,"events":0},"private_command":private_command,"wait_service":wait_service,"run":run,"check_images":lambda:None,
                          "probe_personal_worker":probe_personal_worker,
                          "time":SimpleNamespace(monotonic=monotonic,sleep=lambda seconds:clock.update(now=clock["now"]+seconds))})
        with patch.object(sys,"stdout",new_callable=io.StringIO) as output:
            namespace["activation_main"]()
        return json.loads(output.getvalue()),request,events,current,release,live

    def test_learn_only_simulation_migrates_one_database_and_restarts_only_learn_consumers(self):
        result, request, events, current, release, live = self._simulate(learn_only=True)
        self.assertEqual(result["status"], "activated")
        result["runtime_overlay"] = Path(result["runtime_overlay"]).as_posix()
        with patch.object(ACT,"RELEASE_ROOT",release.parent.as_posix()):
            ACT.validate_remote_receipt(result, request)
        migrations = [command for command in events if "--rm" in command]
        self.assertEqual(len(migrations), 1)
        self.assertIn("learn-migrate", migrations[0])
        targets = {command[-1] for command in events if "up" in command}
        self.assertEqual(targets, {name for name, family in ACT.SERVICE_IMAGES.items() if family == "learn"})
        self.assertFalse(any("--baseline-only" in command for command in events))
        for name in ("frontend/app.tsx", "services/auth/app.py", "services/community/app.py"):
            self.assertEqual((live/name).read_bytes(), b"old\n")
        self.assertEqual((live/"services/learn/app.py").read_bytes(), b"new\n")
        self.assertFalse(result["category_compatibility"]["legacy_enum_image_rollback_forbidden"])

    def test_learn_only_backend_failure_never_restarts_or_rolls_back_other_families(self):
        result, request, events, current, _, _ = self._simulate(learn_only=True, fail_health="learn-service")
        self.assertEqual(result["status"], "partial-unverified")
        self.assertTrue(result["writer_fence_maintained"])
        self.assertFalse(any("rollback-frontend" in str(command) for command in events))
        for row in current:
            if row["service"] in ACT.SERVICE_IMAGES and ACT.SERVICE_IMAGES[row["service"]] != "learn":
                self.assertTrue(row["running"])

    def test_learn_only_dependency_503_is_explicit_and_same_original_worker_recovers(self):
        result,request,events,current,release,_=self._simulate(learn_only=True,dependency_unready=True)
        self.assertEqual(result["status"],"activated")
        self.assertTrue(all("command" not in row for row in request["current_container_baseline"]))
        evidence=result["learn_dependency_readiness"]
        self.assertTrue(evidence["before"]["readiness_deferred"])
        self.assertEqual(evidence["before"]["docker_health"],"unhealthy")
        self.assertEqual(evidence["before"]["ready_status"],503)
        self.assertEqual(evidence["recovery"]["ready_status"],200)
        self.assertEqual(evidence["recovery"]["identity"],evidence["original_identity"])
        self.assertFalse(any(command[-1]==ACT.PERSONAL_WORKER for command in events))
        self.assertTrue(next(row for row in current if row["service"]==ACT.PERSONAL_WORKER)["running"])
        result["runtime_overlay"]=Path(result["runtime_overlay"]).as_posix()
        with patch.object(ACT,"RELEASE_ROOT",release.parent.as_posix()):
            ACT.validate_remote_receipt(result,request)
            missing=copy.deepcopy(result);missing.pop("learn_dependency_readiness")
            with self.assertRaisesRegex(ACT.ActivateError,"REMOTE_DEPENDENCY_EVIDENCE_MISSING"):
                ACT.validate_remote_receipt(missing,request)
            missing=copy.deepcopy(result);missing["learn_dependency_readiness"].pop("recovery")
            with self.assertRaisesRegex(ACT.ActivateError,"REMOTE_DEPENDENCY_RECOVERY_MISSING"):
                ACT.validate_remote_receipt(missing,request)
            for name in ("recovery_outcome_unverified", "writer_fence_maintained", "frontend_image_rolled_back"):
                failed=copy.deepcopy(result);failed[name]=True
                with self.subTest(flag=name), self.assertRaisesRegex(ACT.ActivateError,"REMOTE_DEPENDENCY_SUCCESS_FAILURE_FLAG"):
                    ACT.validate_remote_receipt(failed,request)

    def test_learn_only_dependency_recovery_timeout_fences_learn_without_auth_restart(self):
        result,request,events,current,_,_=self._simulate(learn_only=True,dependency_unready=True,dependency_recovery_failure=True)
        self.assertEqual(result["status"],"partial-unverified")
        self.assertEqual(result["error"],"LEARN_DEPENDENCY_RECOVERY_TIMEOUT")
        self.assertTrue(result["writer_fence_maintained"])
        self.assertNotIn("recovery",result["learn_dependency_readiness"])
        self.assertFalse(any(command[-1]==ACT.PERSONAL_WORKER for command in events))
        original=next(row for row in request["current_container_baseline"] if row["service"]==ACT.PERSONAL_WORKER)
        actual=next(row for row in current if row["service"]==ACT.PERSONAL_WORKER)
        self.assertEqual(ACT.personal_identity(actual),ACT.personal_identity(original))

    def test_full_remote_simulation_migrates_baselines_workers_and_preserves_files(self):
        result,request,events,current,release,live = self._simulate()
        self.assertEqual(result["status"],"activated")
        migrations = [command for command in events if "--rm" in command and "--baseline-only" not in command]
        self.assertEqual([command[command.index("--name")+2] for command in migrations],["auth-migrate","learn-migrate","community-migrate"])
        baseline = next(index for index,command in enumerate(events) if "--baseline-only" in command)
        self.assertTrue(all(index > baseline for index,command in enumerate(events) if "up" in command and command[-1] in ACT.NEW_WORKERS))
        self.assertEqual(events[-1][-1],"frontend")
        self.assertTrue(all("--no-deps" in command and "--pull" in command for command in events))
        self.assertEqual((live/"services/auth/unknown.txt").read_bytes(),b"preserve\n")
        self.assertEqual((release/"activation/runtime/sitecustomize.py").read_bytes(),ACT.MIGRATION_SITE_CUSTOMIZE.encode())
        result["runtime_overlay"] = Path(result["runtime_overlay"]).as_posix()
        with patch.object(ACT,"RELEASE_ROOT",release.parent.as_posix()):
            ACT.validate_remote_receipt(result,request)

    def test_full_remote_simulation_redeploys_existing_workers_absent_from_base(self):
        result,request,events,current,release,live = self._simulate(existing_new_workers=True)
        self.assertEqual(result["status"],"activated")
        baseline = next(index for index,command in enumerate(events) if "--baseline-only" in command)
        for name in ACT.NEW_WORKERS:
            self.assertEqual(len([item for item in current if item["service"] == name]),1)
            self.assertTrue(next(item for item in current if item["service"] == name)["running"])
            up = [index for index,command in enumerate(events) if "up" in command and command[-1] == name]
            self.assertEqual(len(up),1)
            self.assertGreater(up[0],baseline)
        self.assertEqual((live/"services/auth/unknown.txt").read_bytes(),b"preserve\n")
        overlay = json.loads((release/"activation/runtime-overlay.json").read_bytes())
        for name,(api,module,port,key) in ACT.NEW_WORKERS.items():
            self.assertEqual(overlay["services"][name]["command"],["python","-m",module])
            self.assertEqual(overlay["services"][name]["environment"][key],str(port))
        result["runtime_overlay"] = Path(result["runtime_overlay"]).as_posix()
        with patch.object(ACT,"RELEASE_ROOT",release.parent.as_posix()):
            ACT.validate_remote_receipt(result,request)

    def test_backend_health_failure_fences_new_writers_without_old_restart(self):
        result,request,events,current,_,_ = self._simulate(fail_health="learn-service")
        self.assertEqual(result["status"],"partial-unverified")
        self.assertTrue(result["writer_fence_maintained"])
        self.assertTrue(any(command[:2] == ["docker","stop"] for command in events))
        self.assertFalse(any(command[-1] == "frontend" for command in events))
        self.assertFalse(any(item["running"] for item in current if item["service"] in ACT.API_ORDER))

    def test_frontend_only_has_no_migration_or_worker_change_or_runtime_wrapper(self):
        result,request,events,_,release,live = self._simulate(frontend_only=True)
        self.assertEqual(result["status"],"activated")
        self.assertEqual(len(events),1)
        self.assertEqual(events[0][-1],"frontend")
        self.assertFalse((release/"activation/runtime").exists())
        self.assertEqual((live/"services/auth/app.py").read_bytes(),b"old\n")

    def test_confirmed_frontend_failure_restores_only_old_frontend(self):
        result,_,events,_,_,_ = self._simulate(frontend_only=True,fail_health="frontend")
        self.assertTrue(result["frontend_image_rolled_back"])
        self.assertEqual(len(events),2)
        self.assertTrue(all(command[-1] == "frontend" for command in events))

    def test_chained_frontend_failure_restores_observed_frontend_not_backend_baseline_labels(self):
        prior = {"commit":"e"*40,"source_fingerprint":"f"*64,"release_nonce":"intermediate-frontend-123"}
        result,_,events,_,release,_ = self._simulate(frontend_only=True,fail_health="frontend",frontend_prior=prior)
        self.assertTrue(result["frontend_image_rolled_back"])
        self.assertEqual(len(events),2)
        self.assertEqual(self.wait_bindings[-1][2],prior)
        rollback = json.loads((release/"activation/rollback-frontend.json").read_text())
        self.assertEqual(rollback["services"]["frontend"]["labels"],ACT.source_labels(prior))

    def test_frontend_rollback_requires_complete_observed_current_source_labels(self):
        snapshot = {"containers":[{"service":"frontend","running":True,"container_id":"current"}],
                    "source_labels":{"current":ACT.source_labels(PRIOR)}}
        self.assertEqual(ACT.frontend_rollback_binding(snapshot),PRIOR)
        for labels in ({}, {**ACT.source_labels(PRIOR), "io.ctf-web.release-nonce":None},
                       {**ACT.source_labels(PRIOR), "org.opencontainers.image.revision":"unknown"}):
            with self.subTest(labels=labels),self.assertRaises(ACT.ActivateError):
                ACT.frontend_rollback_binding({**snapshot,"source_labels":{"current":labels}})

    def test_ambiguous_frontend_up_is_not_repeated_or_rolled_back(self):
        result,_,events,_,_,_ = self._simulate(frontend_only=True,ambiguous_up=True)
        self.assertEqual(result["status"],"partial-unverified")
        self.assertEqual(len(events),1)
        self.assertNotIn("frontend_image_rolled_back",result)

    def test_partial_nested_response_fields_and_wrong_hash_are_rejected(self):
        result,request,_,_,_,_ = self._simulate(fail_health="learn-service")
        for key,value in (("schema",{"Env":["synthetic"]}),("current_step",["up","unknown"]),
                          ("containers_before",[{"Env":["synthetic"]}]),("backup_receipt_sha256","0"*64)):
            with self.subTest(key=key),self.assertRaises((ACT.ActivateError,ACT.verify.VerifyError)):
                ACT.validate_remote_receipt({**result,key:value},request)

    def test_phase2_backend_files_health_and_labels_are_required(self):
        result,request,_,current,_,live = self._simulate(frontend_only=True)
        snapshot = {"containers":current,"protected_files":request["protected_files"],
                    "code_files":[row(path.relative_to(live).as_posix(),path.read_bytes()) for path in live.rglob("*") if path.is_file()],
                    "source_labels":{item["container_id"]:ACT.source_labels(PRIOR) for item in current}}
        ACT.check_backend_baseline(snapshot,request["backend_baseline"])
        for field,value in (("health","unhealthy"),("image_id","sha256:"+"f"*64)):
            changed = copy.deepcopy(snapshot)
            next(item for item in changed["containers"] if item["service"]=="auth-service")[field] = value
            with self.assertRaisesRegex(ACT.ActivateError,"FRONTEND_ONLY_BACKEND_RUNTIME_CHANGED"):
                ACT.check_backend_baseline(changed,request["backend_baseline"])
        snapshot["source_labels"] = {}
        with self.assertRaises(ACT.ActivateError):
            ACT.check_backend_baseline(snapshot,request["backend_baseline"])

    def test_full_029_remote_activation_binds_schema_and_requires_category_evidence(self):
        result,request,events,_,release,_=self._simulate(full_learn_head=ACT.verify.LEARN_CATEGORY_HEAD)
        self.assertEqual(result["status"],"activated")
        self.assertEqual(result["schema"]["heads"],{name:[head] for name,head in request["expected_heads"].items()})
        self.assertEqual(result["category_compatibility"],{"custom_category_count":0,"custom_event_count":0,
            "legacy_enum_image_rollback_forbidden":False,"automatic_backend_rollback_performed":False})
        self.assertNotIn("learn_dependency_readiness",result)
        self.assertEqual({command[-1] for command in events if "up" in command},set(ACT.SERVICE_IMAGES))
        result["runtime_overlay"]=Path(result["runtime_overlay"]).as_posix()
        with patch.object(ACT,"RELEASE_ROOT",release.parent.as_posix()):
            ACT.validate_remote_receipt(result,request)
            missing=copy.deepcopy(result);missing.pop("category_compatibility")
            with self.assertRaisesRegex(ACT.ActivateError,"REMOTE_CATEGORY_COMPATIBILITY_UNVERIFIED"):
                ACT.validate_remote_receipt(missing,request)
            old=copy.deepcopy(result);old["schema"]["heads"]["learn"]=[ACT.EXPECTED_HEADS["learn"]]
            with self.assertRaisesRegex(ACT.ActivateError,"REMOTE_ACTIVATION_SCHEMA_INVALID"):
                ACT.validate_remote_receipt(old,request)

    def test_assembled_full_029_schema_requires_matching_heads_registry_and_audit(self):
        namespace={}
        exec(compile(ACT.remote_program().rsplit("activation_main()",1)[0],"<full-029-schema>","exec"),namespace)
        heads={**ACT.EXPECTED_HEADS,"learn":ACT.verify.LEARN_CATEGORY_HEAD}
        actual=dict(heads)
        tables={name:list(values) for name,values in ACT.SCHEMA_TABLES.items()}
        tables["learn"] += ["platform_event_categories","platform_event_category_audit_events"]
        queries=[]
        def sql(database,query):
            queries.append(query)
            family=next(name for name,value in ACT.preflight.DATABASES.items() if value==database)
            if "public.alembic_version" in query: return [actual[family]]
            if "information_schema.tables" in query: return tables[family]
            if "information_schema.columns" in query: return list(ACT.SCHEMA_COLUMNS[family])
            if "FROM pg_trigger" in query:
                return [{"table":name,"enabled":"O","function":"bump_knowledge_graph_revision","type":28}
                        for name in ACT.TRIGGER_TABLES]
            raise AssertionError("UNEXPECTED_SQL")
        namespace.update(REQUEST={"expected_heads":heads},sql=sql)
        self.assertEqual(namespace["schema_contract"](),{"heads":{name:[head] for name,head in heads.items()},
            "trigger_count":18,"required_schema_verified":True})
        for name in ("platform_event_categories","platform_event_category_audit_events"):
            tables["learn"].remove(name)
            with self.subTest(missing=name),self.assertRaisesRegex(namespace["ActivateError"],"EVENT_CATEGORY_SCHEMA_MISSING"):
                namespace["schema_contract"]()
            tables["learn"].append(name)
        actual["learn"]=ACT.EXPECTED_HEADS["learn"]
        with self.assertRaisesRegex(namespace["ActivateError"],"MIGRATION_HEAD_UNVERIFIED"):
            namespace["schema_contract"]()
        namespace["REQUEST"]={"expected_heads":ACT.EXPECTED_HEADS}
        tables["learn"]=list(ACT.SCHEMA_TABLES["learn"])
        self.assertEqual(namespace["schema_contract"]()["heads"]["learn"],[ACT.EXPECTED_HEADS["learn"]])
        self.assertTrue(all(query.startswith("SELECT ") for query in queries))

    def _main_fixture(self,execute=False,learn_head=None):
        paths = {name:self.root/(name+".json") for name in ("plan","build-receipt","backup-receipt","quiescence-attestation")}
        managed = [row("frontend/app.tsx",b"source")]
        source_rows = managed+[row("scripts/renewal/activate_release.py",(RENEWAL/"activate_release.py").read_bytes())]
        source_rows += [row("scripts/renewal/"+name+".py",(RENEWAL/(name+".py")).read_bytes()) for name in ACT.COMPONENT_SOURCE_SHA256]
        current = [{"service":name,"container_id":f"{index:064x}","image_id":"sha256:"+str(index)*64,"running":True,"pid":100}
                   for index,name in enumerate((*ACT.API_ORDER,"frontend","minio"),1)]
        protected = [row("/home/deploy/CTF_Web/infra/.env",b"protected")]
        plan = {"nonce":BINDING["release_nonce"],"source_file_count":len(source_rows),"inspection":{"containers":current,"protected_files":protected},
                "migrations":{name:{"head":head} for name,head in ACT.EXPECTED_HEADS.items()}}
        if learn_head is not None: plan["migrations"]["learn"]["head"]=learn_head
        built = {"remote":{"live_before":{"frontend_args_sha256":"9"*64}}}
        contract = {"protected_files":protected,"images":{"auth":"sha256:"+"a"*64},"image_bindings":{"auth":BINDING}}
        values = {paths["plan"]:(plan,"1"*64),paths["build-receipt"]:(built,"2"*64),paths["backup-receipt"]:({},"3"*64),paths["quiescence-attestation"]:({},"4"*64)}
        argv = [argument for name,path in paths.items() for argument in ("--"+name,str(path))]
        argv += ["--plan-sha256","1"*64,"--commit",BINDING["commit"],"--source-fingerprint",BINDING["source_fingerprint"],"--release-nonce",BINDING["release_nonce"]]
        if execute: argv.append("--execute")
        patches = (patch.object(ACT.ops,"manifest",return_value={"fingerprint":BINDING["source_fingerprint"],"files":source_rows}),
                   patch.object(ACT.stage,"validate_manifest",return_value={item["path"]:item for item in source_rows}),
                   patch.object(ACT.stage,"bind_checkout",return_value={item["path"]:0o644 for item in source_rows}),
                   patch.object(ACT.verify,"load_receipt",side_effect=lambda path:values[path]),
                   patch.object(ACT.preflight,"validate_plan"),patch.object(ACT.verify,"build_contract",return_value=contract),
                   patch.object(ACT,"backup_contract"),patch.object(ACT.backup_release,"validate_quiescence",return_value={"containers":current}),
                   patch.object(ACT,"verify_backup_files",return_value=8))
        return argv,patches

    def test_full_main_forwards_preflight_029_head_into_actual_remote_payload_without_028_fallback(self):
        from contextlib import ExitStack, nullcontext
        for learn_head in (ACT.EXPECTED_HEADS["learn"],ACT.verify.LEARN_CATEGORY_HEAD):
            argv,patches=self._main_fixture(execute=True,learn_head=learn_head)
            captured=[]
            def read_payload(command,*,timeout,stdin):
                request=json.loads(stdin)
                compile(ACT.base64.b64decode(request.pop("program")),"<full-main-payload>","exec")
                captured.append(request)
                raise TimeoutError("SYNTHETIC_REMOTE_NOT_EXECUTED")
            with self.subTest(learn_head=learn_head),ExitStack() as stack:
                for item in patches: stack.enter_context(item)
                stack.enter_context(patch.object(ACT.ops,"ssh",side_effect=read_payload))
                stack.enter_context(patch.object(ACT.ops,"receipt",return_value=self.root/"mock-receipt"))
                stack.enter_context(patch.object(ACT.quiesce,"action_lease",return_value=nullcontext()))
                stack.enter_context(patch.object(ACT.stage,"git_bytes",return_value=BINDING["commit"].encode()))
                stack.enter_context(patch.object(sys,"stdout",new_callable=io.StringIO))
                self.assertEqual(ACT.main(argv),1)
            self.assertEqual(len(captured),1)
            self.assertEqual(captured[0]["expected_heads"],{**ACT.EXPECTED_HEADS,"learn":learn_head})
            self.assertFalse(captured[0]["frontend_only"])
            self.assertFalse(captured[0]["learn_only"])

    def test_default_main_has_no_ssh_intent_or_remote_payload(self):
        from contextlib import ExitStack
        argv,patches = self._main_fixture()
        with ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            stack.enter_context(patch.object(ACT.ops,"ssh",side_effect=AssertionError("SSH_FORBIDDEN")))
            stack.enter_context(patch.object(ACT.ops,"receipt",side_effect=AssertionError("INTENT_FORBIDDEN")))
            stack.enter_context(patch.object(ACT,"execution_payload",side_effect=AssertionError("PAYLOAD_FORBIDDEN")))
            output = stack.enter_context(patch.object(sys,"stdout",new_callable=io.StringIO))
            self.assertEqual(ACT.main(argv),0)
        self.assertFalse(json.loads(output.getvalue())["activation_performed"])

    def test_execute_claims_exclusive_intent_and_never_retries_unknown_remote_outcome(self):
        from contextlib import ExitStack, contextmanager
        argv,patches = self._main_fixture(execute=True)
        claimed = set()
        receipts = []
        leased = False
        @contextmanager
        def lease(*args):
            nonlocal leased
            self.assertFalse(leased)
            leased = True
            try: yield
            finally: leased = False
        def exclusive(name,value):
            self.assertTrue(leased)
            if name in claimed: raise FileExistsError("EXCLUSIVE_INTENT")
            claimed.add(name)
            receipts.append(copy.deepcopy(value))
            return self.root/name
        with ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            ssh = stack.enter_context(patch.object(ACT.ops,"ssh",side_effect=TimeoutError("synthetic")))
            stack.enter_context(patch.object(ACT.ops,"receipt",side_effect=exclusive))
            stack.enter_context(patch.object(ACT.quiesce,"action_lease",side_effect=lease))
            stack.enter_context(patch.object(ACT.stage,"git_bytes",return_value=BINDING["commit"].encode()))
            stack.enter_context(patch.object(ACT,"execution_payload",return_value=("small-command",b"stdin")))
            stack.enter_context(patch.object(sys,"stdout",new_callable=io.StringIO))
            self.assertEqual(ACT.main(argv),1)
            with self.assertRaises(FileExistsError): ACT.main(argv)
        self.assertEqual(ssh.call_count,1)
        self.assertEqual(receipts[-1]["status"],"remote-outcome-unconfirmed")
        self.assertFalse(receipts[0]["frontend_only"])


if __name__ == "__main__":
    unittest.main()
