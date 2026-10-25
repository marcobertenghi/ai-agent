import os
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from aiagent.sandbox import CleanupError, _capture, container_options, run_command
from aiagent.workspace import PolicyError, Workspace
from tests.support import ProjectTest


class SandboxTests(ProjectTest):
    def setUp(self):
        super().setUp()
        self.ws = Workspace(self.config.workspace, self.config.denied_paths, 1000, 100)
        self.policy = self.config.execution
        self.policy = replace(
            self.policy,
            backend="docker",
            commands={"python": ("/usr/local/bin/python", "-E", "-s", "-B")},
        )

    def test_missing_docker_has_no_host_fallback(self):
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value=None),
            patch("aiagent.docker_sandbox.subprocess.run") as run,
        ):
            with self.assertRaisesRegex(PolicyError, "Docker is required"):
                run_command(self.ws, self.policy, 1000, "python", ["-c", "print(42)"])
            run.assert_not_called()

    def test_container_flags_and_argument_separation(self):
        created = subprocess.CompletedProcess([], 0, b"container-id")
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aiagent.docker_sandbox.subprocess.run", return_value=created) as run,
            patch("aiagent.docker_sandbox._capture", return_value=(0, b"42", "")),
        ):
            result = run_command(
                self.ws, self.policy, 1000, "python", ["--privileged", "$(touch /bad)"]
            )
        self.assertIn("42", result)
        argv = run.call_args_list[0].args[0]
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("--security-opt=no-new-privileges=true", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("none", argv)
        self.assertIn("--pull=never", argv)
        self.assertNotIn("--env", argv)
        mount = argv[argv.index("--mount") + 1]
        self.assertIn("readonly", mount)
        self.assertIn("bind-recursive=disabled", mount)
        self.assertEqual(argv.count("--mount"), 1)
        image_index = argv.index(self.policy.image)
        self.assertEqual(
            argv[image_index + 1 :],
            [*self.policy.commands["python"], "--privileged", "$(touch /bad)"],
        )
        self.assertEqual(run.call_args_list[-1].args[0][1:3], ["rm", "--force"])

    def test_workspace_preflight_happens_before_container_create(self):
        (self.config.workspace / ".env").write_text("secret")
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aiagent.docker_sandbox.subprocess.run") as run,
        ):
            with self.assertRaises(PolicyError):
                run_command(self.ws, self.policy, 1000, "python", [])
            run.assert_not_called()

    def test_cleanup_runs_after_interrupt_and_failure_is_fatal(self):
        ok = subprocess.CompletedProcess([], 0, b"id")
        failed = subprocess.CompletedProcess([], 1)
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aiagent.docker_sandbox.subprocess.run", side_effect=[ok, ok]) as run,
            patch("aiagent.docker_sandbox._capture", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_command(self.ws, self.policy, 1000, "python", [])
            self.assertEqual(run.call_count, 2)
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aiagent.docker_sandbox.subprocess.run", side_effect=[ok, failed]),
            patch("aiagent.docker_sandbox._capture", return_value=(0, b"", "")),
        ):
            with self.assertRaises(CleanupError):
                run_command(self.ws, self.policy, 1000, "python", [])

    def test_create_timeout_attempts_cleanup(self):
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch(
                "aiagent.docker_sandbox.subprocess.run",
                side_effect=[
                    subprocess.TimeoutExpired("docker create", 15),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as run,
        ):
            with self.assertRaisesRegex(PolicyError, "timed out"):
                run_command(self.ws, self.policy, 1000, "python", [])
            self.assertEqual(run.call_count, 2)

    def test_capture_is_bounded_and_handles_timeout(self):
        code, data, reason = _capture([sys.executable, "-c", "print('x' * 1000000)"], 5, 1000)
        self.assertLessEqual(len(data), 1000)
        self.assertIn("output limit", reason)
        code, data, reason = _capture([sys.executable, "-c", "import time; time.sleep(5)"], 1, 1000)
        self.assertIn("timed out", reason)

    def test_closed_stdout_still_obeys_timeout(self):
        script = "import os,time; os.close(1); os.close(2); time.sleep(5)"
        _, _, reason = _capture([sys.executable, "-c", script], 1, 1000)
        self.assertIn("timed out", reason)

    def test_writable_policy_and_nonroot_user(self):
        opts = container_options(self.ws, replace(self.policy, writable=True), "test")
        self.assertNotIn("readonly", opts[opts.index("--mount") + 1])
        self.assertNotEqual(opts[opts.index("--user") + 1].split(":")[0], "0")


@unittest.skipUnless(
    os.environ.get("CTF_AGENT_DOCKER_TESTS") == "1",
    "Set CTF_AGENT_DOCKER_TESTS=1 with Docker and image built",
)
class DockerIntegrationTests(ProjectTest):
    """Opt-in checks of the real runtime, not a substitute for kernel isolation."""

    def setUp(self):
        super().setUp()
        self.config = replace(
            self.config,
            execution=replace(
                self.config.execution,
                backend="docker",
                commands={"python": ("/usr/local/bin/python", "-E", "-s", "-B")},
            ),
        )

    def test_real_container_confinement(self):
        ws = Workspace(self.config.workspace, self.config.denied_paths, 10000, 100)
        outside = self.base / "host-only.txt"
        outside.write_text("host secret")
        ws.write("input.txt", "challenge")
        ws.write("helper.py", "VALUE = 42")
        script = f"""
import os, pathlib, socket
import helper
assert helper.VALUE == 42
assert os.getuid() != 0
assert pathlib.Path('/workspace/input.txt').read_text() == 'challenge'
assert not pathlib.Path({str(outside)!r}).exists()
assert 'OPENROUTER_API_KEY' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
for name in ('/workspace/no-write', '/no-root-write'):
    try:
        pathlib.Path(name).write_text('bad')
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected write access')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('Unexpected outbound network')
print('isolation checks passed')
"""
        ws.write("check.py", script)
        result = run_command(ws, self.config.execution, 10000, "python", ["check.py"])
        self.assertIn("Exit code: 0", result)
        self.assertIn("isolation checks passed", result)

    def test_real_container_timeout_removes_container(self):
        ws = Workspace(self.config.workspace, (), 10000, 100)
        # Assert the timed-out container name is gone, not just the attached CLI.
        from aiagent import sandbox

        original = sandbox.container_options
        names = []

        def remember(workspace, policy, name):
            names.append(name)
            return original(workspace, policy, name)

        with patch("aiagent.docker_sandbox.container_options", side_effect=remember):
            result = run_command(
                ws,
                replace(self.config.execution, timeout_seconds=1),
                1000,
                "python",
                ["-c", "import time; time.sleep(60)"],
            )
        self.assertIn("timed out", result)
        inspected = subprocess.run(["docker", "inspect", names[0]], capture_output=True)
        self.assertNotEqual(inspected.returncode, 0)
