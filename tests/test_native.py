import hashlib
import http.server
import os
import socket
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiagent.native import bubblewrap_argv, check_available, run_native, seatbelt_profile
from aiagent.workspace import PolicyError, Workspace
from tests.support import ProjectTest


class NativePolicyTests(ProjectTest):
    def setUp(self):
        super().setUp()
        self.ws = Workspace(self.config.workspace, self.config.denied_paths, 10000, 100)

    def test_missing_native_runtime_never_uses_host_fallback(self):
        with (
            patch("aiagent.native.sys.platform", "linux"),
            patch("aiagent.native.os.geteuid", return_value=1000),
            patch("aiagent.native.shutil.which", return_value=None),
            patch("aiagent.native.capture") as capture,
        ):
            with self.assertRaisesRegex(PolicyError, "bubblewrap"):
                run_native(self.ws, self.config.execution, 1000, "python", ["-c", "print(42)"])
            capture.assert_not_called()

    def test_native_root_execution_is_refused(self):
        with patch("aiagent.native.os.geteuid", return_value=0):
            with self.assertRaisesRegex(PolicyError, "regular user"):
                check_available()

    def test_runtime_socket_is_refused_even_with_network_disabled(self):
        path = self.base / "control.sock"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            with (
                patch("aiagent.native.check_available", return_value="/sandbox"),
                patch("aiagent.native.capture") as capture,
            ):
                with self.assertRaises(PolicyError):
                    run_native(
                        self.ws,
                        replace(self.config.execution, read_paths=(path,)),
                        1000,
                        "python",
                        [],
                    )
                capture.assert_not_called()

    def test_startup_probe_failure_happens_before_model_client(self):
        import io
        from contextlib import redirect_stderr

        from aiagent.cli import main

        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
            patch("aiagent.native.probe", side_effect=PolicyError("sandbox refused")),
            patch("aiagent.native.check_available", return_value="/fake/sandbox"),
            patch("aiagent.cli.OpenAI") as client,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--config", str(self.config_path), "test"]), 1)
            client.assert_not_called()

    def test_seatbelt_policy_is_default_deny_and_escapes_paths(self):
        scratch = self.base / 'quoted"scratch'
        scratch.mkdir()
        profile = seatbelt_profile(
            self.ws, self.config.execution, scratch, [Path("/usr/lib")], Path("/trusted/helper.py")
        )
        self.assertIn("(deny default)", profile)
        self.assertNotIn("(allow default)", profile)
        self.assertNotIn("(allow network", profile)
        self.assertIn("SYS_setsid", profile)
        self.assertIn("SYS_setpgid", profile)
        self.assertIn('quoted\\"scratch', profile)
        self.assertNotIn(f'(allow file-write* (subpath "{self.ws.root}"))', profile)

    def test_linux_empty_root_and_network_namespace(self):
        args = bubblewrap_argv(
            "/usr/bin/bwrap",
            self.ws,
            self.config.execution,
            self.base / "scratch",
            [Path("/usr/bin")],
            Path("/trusted/helper.py"),
            ["/usr/bin/python3", "solve.py"],
        )
        self.assertIn("--unshare-all", args)
        self.assertIn("--new-session", args)
        self.assertIn("--die-with-parent", args)
        self.assertNotIn("--share-net", args)
        self.assertNotIn("--ro-bind / /", " ".join(args))
        self.assertIn("--remount-ro", args)
        self.assertEqual(args[-2:], ["/usr/bin/python3", "solve.py"])
        outbound = bubblewrap_argv(
            "bwrap",
            self.ws,
            replace(self.config.execution, network="outbound"),
            self.base / "scratch",
            [],
            Path("/helper"),
            ["/tool"],
        )
        self.assertIn("--share-net", outbound)

    def test_read_allowance_cannot_expose_workspace_or_credentials(self):
        with (
            patch("aiagent.native.check_available", return_value="/sandbox"),
            patch("aiagent.native.capture") as capture,
        ):
            for root in (self.config.workspace, self.base):
                with self.subTest(root=root), self.assertRaises(PolicyError):
                    run_native(
                        self.ws,
                        replace(self.config.execution, read_paths=(root,)),
                        1000,
                        "python",
                        [],
                    )
            capture.assert_not_called()

    def test_read_allowance_case_alias_cannot_expose_project(self):
        alias = self.base.with_name(self.base.name.swapcase())
        if alias == self.base or not alias.exists() or not alias.samefile(self.base):
            self.skipTest("Filesystem is case-sensitive")
        with (
            patch("aiagent.native.check_available", return_value="/sandbox"),
            patch("aiagent.native.capture") as capture,
        ):
            with self.assertRaises(PolicyError):
                run_native(
                    self.ws, replace(self.config.execution, read_paths=(alias,)), 1000, "python", []
                )
            capture.assert_not_called()


@unittest.skipUnless(
    os.environ.get("CTF_AGENT_NATIVE_TESTS") == "1",
    "Set CTF_AGENT_NATIVE_TESTS=1 for real native isolation tests",
)
class NativeIntegrationTests(ProjectTest):
    def setUp(self):
        super().setUp()
        check_available()  # An explicitly requested integration suite must fail if unavailable.
        self.ws = Workspace(self.config.workspace, self.config.denied_paths, 10000, 100)
        self.policy = self.config.execution

    def run_python(self, source, *, policy=None, output_limit=16000, record=None):
        return run_native(
            self.ws, policy or self.policy, output_limit, "python", ["-c", source], record=record
        )

    def test_files_environment_and_child_inherit_isolation(self):
        outside = self.base / "host-only.txt"
        outside.write_text("host-only-marker")
        self.ws.write("input.txt", "challenge")
        child = f"""
import os, pathlib, socket
assert pathlib.Path('input.txt').read_text() == 'challenge'
assert 'OPENROUTER_API_KEY' not in os.environ
assert 'NATIVE_TEST_SECRET' not in os.environ
for name in ({str(outside)!r}, {str(self.config_path)!r}, {str(self.config.audit.directory)!r}):
    try:
        pathlib.Path(name).read_text()
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected host read: ' + name)
try:
    list(pathlib.Path({str(self.base)!r}).iterdir())
except OSError:
    pass
else:
    raise AssertionError('Unexpected host directory listing')
for name in ({str(self.base / "no-host-write")!r}, 'no-workspace-write'):
    try:
        pathlib.Path(name).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected write: ' + name)
print('child isolation passed')
"""
        source = f"""
import os, subprocess, sys
result = subprocess.run([sys.executable, '-I', '-c', {child!r}], capture_output=True, text=True)
print(result.stdout, result.stderr)
assert result.returncode == 0, result.returncode
print('parent isolation passed')
"""
        with patch.dict(os.environ, {"NATIVE_TEST_SECRET": "must-not-leak"}):
            result = self.run_python(source)
        self.assertIn("Exit code: 0", result)
        self.assertIn("child isolation passed", result)
        self.assertIn("parent isolation passed", result)
        self.assertFalse((self.base / "no-host-write").exists())

    def test_workspace_python_imports_and_writable_opt_in(self):
        self.ws.write("helper.py", "VALUE = 42")
        self.ws.write("solve.py", "import helper; print(helper.VALUE)")
        result = run_native(self.ws, self.policy, 1000, "python", ["solve.py"])
        self.assertIn("Exit code: 0\n42", result)
        result = self.run_python(
            "from pathlib import Path; Path('result').write_text('42'); print('saved')",
            policy=replace(self.policy, writable=True),
        )
        self.assertIn("Exit code: 0", result)
        self.assertEqual(self.ws.read("result"), "42")

    def test_writable_workspace_cannot_modify_a_readonly_runtime_through_hardlink(self):
        runtime = self.base / "trusted-runtime"
        runtime.mkdir()
        target = runtime / "readonly.txt"
        target.write_text("unchanged")
        source = f"""
import os
from pathlib import Path
try:
    os.link({str(target)!r}, 'alias')
    Path('alias').write_text('escaped')
except OSError:
    print('hardlink modification blocked')
else:
    raise AssertionError('Read-only runtime modified through workspace hardlink')
"""
        result = self.run_python(
            source, policy=replace(self.policy, writable=True, read_paths=(runtime,))
        )
        self.assertEqual(target.read_text(), "unchanged")
        self.assertIn("Exit code: 0", result)
        self.assertIn("hardlink modification blocked", result)

    def test_network_disabled_against_a_real_local_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            source = f"""
import socket
try:
    socket.create_connection(('127.0.0.1', {port}), timeout=1)
except OSError:
    print('network blocked')
else:
    raise AssertionError('Unexpected network access')
"""
            result = self.run_python(source)
        self.assertIn("Exit code: 0", result)
        self.assertIn("network blocked", result)

    def test_outbound_opt_in_reaches_local_test_service(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(5)
            port = listener.getsockname()[1]

            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connection.sendall(b"local-test-service")
                except TimeoutError:
                    pass

            server = threading.Thread(target=serve)
            server.start()
            try:
                result = self.run_python(
                    f"import socket; s=socket.create_connection(('127.0.0.1',{port}),timeout=2); print(s.recv(100).decode())",
                    policy=replace(self.policy, network="outbound"),
                )
            finally:
                server.join(timeout=6)
        self.assertIn("Exit code: 0", result)
        self.assertIn("local-test-service", result)

    @unittest.skipUnless(Path("/usr/bin/curl").is_file(), "Optional curl is not installed")
    def test_native_curl_fetches_local_http_service(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"native-http-evidence")

            def log_message(self, *args):
                pass

        with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 5
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            try:
                policy = replace(
                    self.policy,
                    network="outbound",
                    commands={
                        "curl": ("/usr/bin/curl", "--max-time", "3", "--silent", "--show-error")
                    },
                )
                result = run_native(
                    self.ws, policy, 4096, "curl", [f"http://localhost:{server.server_port}/"]
                )
            finally:
                thread.join(timeout=6)
        self.assertIn("Exit code: 0", result)
        self.assertIn("native-http-evidence", result)

    def test_timeout_and_output_limit(self):
        result = self.run_python(
            "import time; time.sleep(10)", policy=replace(self.policy, timeout_seconds=1)
        )
        self.assertIn("timed out", result)
        result = self.run_python("print('x' * 100000)", output_limit=1000)
        self.assertIn("output limit", result)
        self.assertLess(len(result), 1200)

    def test_native_audit_records_exact_executable_and_profile(self):
        events = []
        result = self.run_python(
            "print(42)", record=lambda kind, **data: events.append((kind, data))
        )
        self.assertIn("Exit code: 0", result)
        self.assertEqual(events[0][0], "command_started")
        self.assertIn(events[0][1]["backend"], {"seatbelt", "bubblewrap"})
        self.assertEqual(
            events[0][1]["executable_sha256"],
            hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        )
        self.assertIn("sandbox_argv", events[0][1])
        self.assertEqual(events[-1][0], "command_finished")

    def test_cli_runs_real_native_probe_and_command_with_fake_provider(self):
        import io
        import json
        from contextlib import redirect_stderr, redirect_stdout

        import httpx
        from openai import OpenAI

        from aiagent.audit import read_events, replay
        from aiagent.cli import main
        from tests.test_agent_cli import response, tool

        replies = iter(
            [
                response(
                    calls=[
                        tool(
                            "run_command",
                            json.dumps({"command": "python", "args": ["-c", "print(42)"]}),
                        )
                    ]
                ),
                response("Verified result: 42"),
            ]
        )
        client = OpenAI(
            api_key="test-only",
            base_url=self.config.provider.base_url,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json=next(replies).model_dump(exclude_none=True)
                    )
                )
            ),
        )
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
            patch("aiagent.cli.OpenAI", return_value=client),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--config", str(self.config_path), "Calculate 42 offline"]), 0)
        self.assertIn("Verified result: 42", output.getvalue())
        (bundle,) = self.config.audit.directory.iterdir()
        events = list(read_events(bundle))
        kinds = [event["type"] for event in events]
        self.assertLess(kinds.index("sandbox_ready"), kinds.index("model_request"))
        self.assertEqual(kinds.count("command_started"), 2)  # probe, then requested tool
        transcript = []
        self.assertEqual(replay(bundle, transcript.append), 0)
        self.assertIn("Sandbox argv", "\n".join(transcript))

    @unittest.skipUnless(sys.platform == "darwin", "Seatbelt-specific detachment check")
    def test_children_cannot_detach_from_cleanup_group(self):
        source = """
import os
for operation in (os.setsid, lambda: os.setpgid(0, 0)):
    try:
        operation()
    except OSError:
        pass
    else:
        raise AssertionError('Process detached from cleanup group')
print('detachment blocked')
"""
        result = self.run_python(source)
        self.assertIn("Exit code: 0", result)
        self.assertIn("detachment blocked", result)

    @unittest.skipUnless(sys.platform == "darwin", "Seatbelt-specific spawn check")
    def test_posix_spawn_cannot_bypass_process_group_restriction(self):
        result = self.run_python("""
import os, sys
for options in ({'setpgroup': 0}, {'setsid': True}):
    try:
        pid = os.posix_spawn(sys.executable, [sys.executable, '-c', 'pass'], dict(os.environ), **options)
    except (OSError, NotImplementedError):
        continue
    os.waitpid(pid, 0)
    raise AssertionError('posix_spawn accepted process detachment')
print('spawn detachment blocked')
""")
        self.assertIn("Exit code: 0", result)
        self.assertIn("spawn detachment blocked", result)

    def test_background_child_is_cleaned_after_success(self):
        source = """
import subprocess, sys
code = "import time; from pathlib import Path; time.sleep(0.5); Path('escaped-child').write_text('bad')"
subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print('parent finished')
"""
        result = self.run_python(source, policy=replace(self.policy, writable=True))
        self.assertIn("Exit code: 0", result)
        time.sleep(0.8)
        self.assertFalse((self.config.workspace / "escaped-child").exists())
