"""Pure payload transport contracts: no real SSH/SFTP or remote mutation."""
import ast
import base64
import hashlib
import io
import json
from pathlib import Path
import random
import shlex
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops


class RemoteTransportTests(unittest.TestCase):
    def decode_inline(self, command):
        self.assertLessEqual(len(command.encode()), 20000)
        program = shlex.split(command)[2]
        tree = ast.parse(program)
        encoded = next(node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "base64.b64decode")
        return zlib.decompress(base64.b64decode(encoded)).decode("utf-8")

    def test_compressible_large_program_stays_read_only_without_file_transport(self):
        program = "# payload\n" * 10000
        with patch.object(ops, "ssh", return_value=b'{"ok":true}') as call:
            self.assertEqual(ops.remote_python(program, timeout=97), {"ok": True})
        args, kwargs = call.call_args
        self.assertEqual(self.decode_inline(args[0]), program)
        self.assertEqual(kwargs, {"timeout": 97})

    def test_non_ascii_source_bytes_are_preserved(self):
        program = "value = '\uac80\uc99d'\n"
        with patch.object(ops, "ssh", return_value=b'{"ok":true}') as call:
            ops.remote_python(program)
        self.assertEqual(self.decode_inline(call.call_args.args[0]), program)
        self.assertNotIn("stdin", call.call_args.kwargs)

    def test_incompressible_program_uses_short_command_and_exact_payload(self):
        program = "value='" + random.Random(42).randbytes(40000).hex() + "'\n"
        with patch.object(ops, "ssh", return_value=b'{"ok":true}') as call:
            self.assertEqual(ops.remote_python(program, timeout=97), {"ok": True})
        self.assertLess(len(call.call_args.args[0]), 256)
        self.assertEqual(call.call_args.kwargs, {"stdin": program.encode(), "timeout": 97})

    def test_small_stdin_boundary_preserves_native_transport(self):
        with patch.object(ops, "_native_ssh", return_value=b"small") as native, patch.object(ops, "_payload_ssh") as payload:
            self.assertEqual(ops.ssh("command", stdin=b"x" * 8192, timeout=11), b"small")
            native.assert_called_once_with("command", stdin=b"x" * 8192, timeout=11)
            payload.assert_not_called()

    def test_large_stdin_never_enters_native_exec_channel(self):
        with patch.object(ops, "_native_ssh") as native, patch.object(ops, "_payload_ssh", return_value=b"large") as payload:
            data = b"x" * 8193
            self.assertEqual(ops.ssh("command", stdin=data, timeout=12), b"large")
            payload.assert_called_once_with("command", data, timeout=12)
            native.assert_not_called()

    def transport(self, fail_phase=None):
        nonce = UUID("e9838b38-e2af-49eb-9c4e-34f3444c7a83")
        data = b"private-payload-fixture" * 1000
        command = "cat >/dev/null; printf completed-fixture"
        created = {"created": True, "uid": 1000, "device": 5, "inode": 7}
        calls, records = [], []
        def native(encoded_command, **kwargs):
            source = self.decode_inline(encoded_command)
            module = ast.parse(source)
            values = {node.targets[0].id: ast.literal_eval(node.value) for node in module.body
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"SPEC", "ACTION"}}
            action, spec = values["ACTION"], values["SPEC"]
            calls.append(action)
            self.assertNotIn("stdin", kwargs)
            self.assertEqual(spec["bytes"], len(data))
            self.assertEqual(spec["sha256"], hashlib.sha256(data).hexdigest())
            if action == fail_phase:
                raise RuntimeError("sensitive diagnostic must not escape")
            if action == "create": return json.dumps(created).encode()
            if action == "execute": return b"completed-fixture"
            if action == "cleanup": return b'{"removed":true}'
            raise AssertionError("unknown action")
        def upload(path, raw, uid, **kwargs):
            self.assertEqual(path, ops.TRANSPORT_ROOT + "/" + str(nonce) + "/payload")
            self.assertEqual(raw, data)
            self.assertEqual(uid, 1000)
            if fail_phase == "upload": raise EOFError("private upload diagnostic")
        def receipt(name, value):
            records.append((name, value))
            return Path("fixture-receipt")
        with patch.object(ops, "uuid4", return_value=nonce), patch.object(ops, "ssh_invocation", return_value=["fixture"]), \
                patch.object(ops, "_native_ssh", side_effect=native), patch.object(ops, "_sftp_payload", side_effect=upload) as sftp, \
                patch.object(ops, "receipt", side_effect=receipt):
            if fail_phase:
                with self.assertRaisesRegex(RuntimeError, "^SSH_PAYLOAD_TRANSPORT_UNRESOLVED_" + str(nonce) + "$"):
                    ops.ssh(command, stdin=data)
                self.assertEqual(records[-1][1]["remote_payload_state"], "may_exist_preserved_no_blind_retry")
                self.assertFalse(records[-1][1]["cleanup_verified"])
            else:
                self.assertEqual(ops.ssh(command, stdin=data), b"completed-fixture")
                self.assertTrue(records[-1][1]["cleanup_verified"])
            self.assertTrue(records[0][0].startswith("transport-intent-"))
            serialized = json.dumps(records)
            self.assertNotIn("private-payload-fixture", serialized)
            self.assertNotIn(command, serialized)
            self.assertNotIn("diagnostic", serialized)
            self.assertTrue(all(value["automatic_retry_performed"] is False for _, value in records))
            if fail_phase == "create": sftp.assert_not_called()
        return calls, records

    def test_success_executes_only_after_upload_then_cleans_exact_owned_payload(self):
        calls, records = self.transport()
        self.assertEqual(calls, ["create", "execute", "cleanup"])
        self.assertEqual(len(records), 2)

    def test_partial_or_unknown_results_preserve_payload_without_cleanup_or_retry(self):
        for failure, expected in (("create", ["create"]), ("upload", ["create"]), ("execute", ["create", "execute"]),
                                  ("cleanup", ["create", "execute", "cleanup"])):
            with self.subTest(failure=failure):
                calls, records = self.transport(failure)
                self.assertEqual(calls, expected)
                self.assertEqual(len(records), 2)
                self.assertIn("unresolved", records[-1][0])

    def test_missing_identity_stops_before_intent_or_connection(self):
        with patch.object(ops, "ssh_invocation", side_effect=RuntimeError("MISSING")), patch.object(ops, "receipt") as receipt, \
                patch.object(ops, "_native_ssh") as native, patch.object(ops, "_sftp_payload") as upload:
            with self.assertRaisesRegex(RuntimeError, "MISSING"):
                ops.ssh("command", stdin=b"x" * 9000)
            receipt.assert_not_called(); native.assert_not_called(); upload.assert_not_called()

    def test_timeout_never_retries_and_preserves_an_unresolved_intent(self):
        records = []
        with patch.object(ops, "ssh_invocation", return_value=["fixture"]), \
                patch.object(ops, "_remaining", side_effect=RuntimeError("TRANSPORT_TIMEOUT")), \
                patch.object(ops, "receipt", side_effect=lambda name, value: records.append((name, value))), \
                patch.object(ops, "_native_ssh") as native, patch.object(ops, "_sftp_payload") as upload:
            with self.assertRaisesRegex(RuntimeError, "SSH_PAYLOAD_TRANSPORT_UNRESOLVED"):
                ops.ssh("command", stdin=b"x" * 9000)
            native.assert_not_called(); upload.assert_not_called()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1][1]["phase"], "create_pending")
        self.assertFalse(records[-1][1]["automatic_retry_performed"])

    def sftp_fixture(self, *, bad_mode=False, write_error=False):
        payload = b"fixture-public-bytes" * 1000
        client, sftp, stream = MagicMock(), MagicMock(), MagicMock()
        client.open_sftp.return_value.__enter__.return_value = sftp
        sftp.open.return_value.__enter__.return_value = stream
        sftp.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG | (0o644 if bad_mode else 0o600),
                                                  st_uid=1000, st_size=len(payload))
        if write_error: stream.write.side_effect = EOFError("private diagnostic")
        paramiko = SimpleNamespace(SSHClient=Mock(return_value=client), RejectPolicy=Mock(return_value=object()))
        with patch.object(ops, "HOST", "ctf.example.com"), patch.dict(sys.modules, {"paramiko": paramiko}), patch.object(Path, "is_file", return_value=True), \
                patch.object(ops.threading, "Timer") as timer:
            if bad_mode:
                with self.assertRaisesRegex(RuntimeError, "IDENTITY_INVALID"):
                    ops._sftp_payload("owned/payload", payload, 1000, deadline=ops.time.monotonic() + 10)
            elif write_error:
                with self.assertRaises(EOFError):
                    ops._sftp_payload("owned/payload", payload, 1000, deadline=ops.time.monotonic() + 10)
            else:
                ops._sftp_payload("owned/payload", payload, 1000, deadline=ops.time.monotonic() + 10)
            timer.return_value.cancel.assert_called_once()
        return payload, client, sftp, stream, paramiko

    def test_sftp_strict_identity_exclusive_nonpipelined_4k_flush(self):
        payload, client, sftp, stream, paramiko = self.sftp_fixture()
        client.load_host_keys.assert_called_once_with(str(ops.KNOWN_HOSTS))
        client.set_missing_host_key_policy.assert_called_once_with(paramiko.RejectPolicy.return_value)
        connection = client.connect.call_args.kwargs
        self.assertEqual(connection["key_filename"], str(ops.KEY))
        self.assertEqual(connection["username"], "deploy")
        self.assertEqual(connection["port"], 10022)
        self.assertFalse(connection["allow_agent"] or connection["look_for_keys"])
        self.assertNotIn("password", connection)
        sftp.open.assert_called_once_with("owned/payload", "wx")
        stream.set_pipelined.assert_called_once_with(False)
        writes = [call.args[0] for call in stream.write.call_args_list]
        self.assertTrue(all(len(chunk) <= 4096 for chunk in writes))
        self.assertEqual(b"".join(writes), payload)
        self.assertEqual(stream.flush.call_count, len(writes))
        client.close.assert_called_once()

    def test_sftp_failure_or_wrong_mode_closes_only_owned_client_without_retry(self):
        for kwargs in ({"bad_mode": True}, {"write_error": True}):
            with self.subTest(kwargs=kwargs):
                _, client, sftp, _, _ = self.sftp_fixture(**kwargs)
                client.close.assert_called_once()
                self.assertEqual(sftp.open.call_count, 1)

    def test_remote_helper_has_no_links_ownership_hash_and_fd_stdin_guards(self):
        compile("SPEC={}\nACTION='create'\n" + ops._TRANSPORT_HELPER, "transport-helper", "exec")
        for required in ("os.lstat", "st_uid", "0o700", "0o600", "st_nlink!=1", "os.O_NOFOLLOW", "dir_fd=directory_fd",
                         "hashlib.sha256", "os.dup2(fd,0)", "os.execv", "os.unlink('payload',dir_fd=directory_fd)",
                         "os.rmdir(SPEC['nonce'],dir_fd=parent_fd)"):
            self.assertIn(required, ops._TRANSPORT_HELPER)
        self.assertNotIn("rmtree", ops._TRANSPORT_HELPER)
        self.assertNotIn("chmod", ops._TRANSPORT_HELPER)

    def helper_namespace(self):
        data = b"owned-payload-fixture"
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000, st_dev=5, st_ino=7)
        payload = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1,
                                  st_size=len(data), st_dev=5, st_ino=8, st_mtime_ns=9)
        remote_os = Mock(O_RDONLY=0, O_DIRECTORY=2, O_NOFOLLOW=4)
        remote_os.getuid.return_value = 1000
        remote_os.lstat.return_value = directory
        remote_os.open.side_effect = lambda path, *args, **kwargs: 30 if path == "payload" else (20 if path.endswith("/transport") else 10)
        remote_os.fstat.side_effect = lambda fd: payload if fd == 30 else directory
        remote_os.stat.side_effect = lambda path, **kwargs: payload if path == "payload" else directory
        remote_os.read.side_effect = [data, b""]
        remote_os.listdir.return_value = ["payload"]
        spec = {"nonce": "fixture-uuid", "device": 5, "inode": 7, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "command": "fixture-command"}
        namespace = {"SPEC": spec, "os": remote_os, "stat": stat, "pwd": SimpleNamespace(getpwnam=lambda name: SimpleNamespace(pw_uid=1000)),
                     "hashlib": hashlib, "json": json, "sys": Mock()}
        tree = ast.parse(ops._TRANSPORT_HELPER)
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        return compile(tree, "transport-helper-fixture", "exec"), namespace, remote_os, directory

    def test_remote_create_rejects_links_wrong_owner_or_mode_and_never_reuses_uuid(self):
        for change, code in (({"st_mode": stat.S_IFLNK | 0o700}, "DIRECTORY_INVALID"),
                             ({"st_uid": 1001}, "OWNER_INVALID"), ({"st_mode": stat.S_IFDIR | 0o755}, "MODE_INVALID")):
            program, namespace, remote_os, directory = self.helper_namespace()
            wrong = SimpleNamespace(**(vars(directory) | change))
            remote_os.lstat.side_effect = lambda path: wrong if path.endswith(".ctf-web-renewal") else directory
            namespace["ACTION"] = "create"
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, code): exec(program, namespace)
            self.assertFalse(any(call.args[0].endswith("fixture-uuid") for call in remote_os.mkdir.call_args_list))
        program, namespace, remote_os, _ = self.helper_namespace()
        remote_os.mkdir.side_effect = lambda path, mode: (_ for _ in ()).throw(FileExistsError()) if path.endswith("fixture-uuid") else None
        namespace["ACTION"] = "create"
        with self.assertRaises(FileExistsError): exec(program, namespace)
        remote_os.unlink.assert_not_called(); remote_os.rmdir.assert_not_called()

    def test_cleanup_removes_only_payload_and_owned_empty_uuid_directory(self):
        for extra in (False, True):
            program, namespace, remote_os, _ = self.helper_namespace()
            namespace["ACTION"] = "cleanup"
            if extra: remote_os.listdir.return_value = ["payload", "preserve-user-file"]
            with patch("sys.stdout", io.StringIO()):
                if extra:
                    with self.assertRaisesRegex(RuntimeError, "NOT_EXCLUSIVE"): exec(program, namespace)
                    remote_os.unlink.assert_not_called(); remote_os.rmdir.assert_not_called()
                else:
                    exec(program, namespace)
                    remote_os.unlink.assert_called_once_with("payload", dir_fd=10)
                    remote_os.rmdir.assert_called_once_with("fixture-uuid", dir_fd=20)

    def test_payload_fd_rejects_link_owner_mode_size_hash_and_inode_change(self):
        data = b"owned-payload-fixture"
        info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=1000, st_nlink=1,
                               st_size=len(data), st_dev=1, st_ino=2, st_mtime_ns=5)
        definitions = ast.Module(body=[node for node in ast.parse(ops._TRANSPORT_HELPER).body
            if isinstance(node, ast.FunctionDef) and node.name == "payload_fd"], type_ignores=[])
        for bad, value, code in (("st_mode", stat.S_IFLNK | 0o600, "PAYLOAD_INVALID"), ("st_uid", 1001, "PAYLOAD_INVALID"),
                ("st_mode", stat.S_IFREG | 0o644, "PAYLOAD_INVALID"), ("st_size", len(data) + 1, "PAYLOAD_INVALID"),
                ("st_nlink", 2, "PAYLOAD_INVALID"), (None, None, None)):
            fake = SimpleNamespace(**vars(info))
            if bad: setattr(fake, bad, value)
            remote_os = Mock(O_RDONLY=0, O_NOFOLLOW=1)
            remote_os.stat.return_value = fake
            remote_os.open.return_value = 7
            remote_os.fstat.return_value = info
            remote_os.read.side_effect = [data, b""]
            namespace = {"os": remote_os, "stat": stat, "hashlib": hashlib, "UID": 1000,
                         "SPEC": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
            exec(compile(definitions, "payload-fd-test", "exec"), namespace)
            if code:
                with self.assertRaisesRegex(RuntimeError, code): namespace["payload_fd"](9)
                remote_os.open.assert_not_called()
            else:
                self.assertEqual(namespace["payload_fd"](9), 7)
                remote_os.open.assert_called_once_with("payload", 1, dir_fd=9)
                remote_os.read.side_effect = [b"wrong", b""]
                with self.assertRaisesRegex(RuntimeError, "HASH_CHANGED"): namespace["payload_fd"](9)
                remote_os.close.assert_called_once_with(7)
                remote_os.close.reset_mock(); remote_os.fstat.return_value = SimpleNamespace(**(vars(info) | {"st_ino": 3}))
                with self.assertRaisesRegex(RuntimeError, "PAYLOAD_CHANGED"): namespace["payload_fd"](9)
                remote_os.close.assert_called_once_with(7)


if __name__ == "__main__":
    unittest.main()
