"""Write boundaries: unsafe host locations versus explicitly allowed task data.

Integration tests use writable, temporary canaries. They never modify real shell
profiles, system configuration, or installed executables.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from aiagent.config import ConfigError, validate_write_root
from aiagent.native import check_available, run_native
from aiagent.workspace import Workspace
from tests.support import ProjectTest


class WriteRootPolicyTests(ProjectTest):
    def test_system_and_standard_executable_directories_are_rejected(self):
        for root in (
            "/",
            "/etc",
            "/private/etc",
            "/usr/local/bin",
            "/usr/local/bin/ctf",
            "/usr/bin",
            "/System",
            "/opt/homebrew/bin",
        ):
            with self.subTest(root=root), self.assertRaises(ConfigError):
                validate_write_root(Path(root))

    def test_path_directory_and_ancestors_are_rejected(self):
        executable = self.base / "custom-bin"
        executable.mkdir()
        with patch.dict(os.environ, {"PATH": str(executable)}):
            for root in (executable, executable / "nested", self.base):
                with (
                    self.subTest(root=root),
                    self.assertRaisesRegex(ConfigError, "PATH/executable"),
                ):
                    validate_write_root(root)

    def test_future_path_case_alias_is_rejected_before_directory_creation(self):
        executable = self.base / "future-bin"
        alias = executable.with_name(executable.name.upper())
        self.assertFalse(executable.exists())
        with patch.dict(os.environ, {"PATH": str(executable)}):
            with self.assertRaisesRegex(ConfigError, "PATH/executable"):
                validate_write_root(alias)
        self.assertFalse(alias.exists())

    def test_shell_startup_paths_and_environment_hooks_are_rejected(self):
        home = self.base / "fake-home"
        home.mkdir()
        zsh = self.base / "zsh-config"
        zsh.mkdir()
        bash_hook = self.base / "bash-hooks/auto.sh"
        sh_hook = self.base / "sh-hooks/auto.sh"
        with (
            patch("aiagent.config.Path.home", return_value=home),
            patch.dict(
                os.environ, {"ZDOTDIR": str(zsh), "BASH_ENV": str(bash_hook), "ENV": str(sh_hook)}
            ),
        ):
            for root in (
                home,
                home / ".bashrc",
                home / ".bash_profile",
                home / ".profile",
                home / ".config/fish",
                zsh,
                zsh / ".zshrc",
                zsh / ".zshenv",
                bash_hook.parent,
                sh_hook.parent,
            ):
                with self.subTest(root=root), self.assertRaises(ConfigError):
                    validate_write_root(root)

    def test_active_python_runtime_is_rejected(self):
        for root in (Path(sys.prefix), Path(sys.base_prefix)):
            with self.subTest(root=root), self.assertRaises(ConfigError):
                validate_write_root(root)

    def test_private_task_and_build_directories_are_allowed(self):
        for root in (
            self.config.workspace,
            self.config.workspace / "build",
            self.base / "private-task",
        ):
            with self.subTest(root=root):
                validate_write_root(root)

    def test_shared_temporary_roots_are_rejected(self):
        for root in (Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir())):
            with self.subTest(root=root), self.assertRaises(ConfigError):
                validate_write_root(root)

    def test_symlink_and_case_aliases_cannot_bypass_path_checks(self):
        executable = self.base / "custom-bin"
        executable.mkdir()
        alias = self.base / "bin-alias"
        alias.symlink_to(executable, target_is_directory=True)
        with patch.dict(os.environ, {"PATH": str(executable)}):
            with self.assertRaises(ConfigError):
                validate_write_root(alias)
            case_alias = executable.with_name(executable.name.swapcase())
            if case_alias.exists() and case_alias.samefile(executable):
                with self.assertRaises(ConfigError):
                    validate_write_root(case_alias)

    def test_shell_startup_symlink_into_workspace_is_rejected(self):
        home = self.base / "fake-home"
        home.mkdir()
        (home / ".bashrc").symlink_to(self.config.workspace / "autoload.sh")
        with patch("aiagent.config.Path.home", return_value=home):
            with self.assertRaisesRegex(ConfigError, "shell startup"):
                validate_write_root(self.config.workspace)

    def test_config_loader_rejects_unsafe_workspace_without_creating_it(self):
        path_dir = self.base / "custom-bin"
        path_dir.mkdir()
        target = path_dir / "new-task"
        with patch.dict(os.environ, {"PATH": str(path_dir)}):
            with self.assertRaisesRegex(ConfigError, "PATH/executable"):
                self.change_config('workspace = "workspace"', f'workspace = "{target}"')
        self.assertFalse(target.exists())

    def test_config_loader_rejects_an_audit_directory_on_path(self):
        executable = self.base / "custom-bin"
        executable.mkdir()
        with patch.dict(os.environ, {"PATH": str(executable)}):
            with self.assertRaisesRegex(ConfigError, "Unsafe audit directory"):
                self.change_config('directory = "runs"', f'directory = "{executable}"')

    def test_cli_rechecks_write_roots_after_dotenv_loading(self):
        from aiagent.cli import main

        self.config.workspace.rmdir()
        (self.base / ".env").write_text(
            f"PATH={self.config.workspace}\nOPENROUTER_API_KEY=test-only\n"
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("aiagent.cli.OpenAI") as client,
            redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(main(["--config", str(self.config_path), "test"]), 1)
            client.assert_not_called()
        self.assertIn("PATH/executable", error.getvalue())
        self.assertFalse(self.config.workspace.exists())


@unittest.skipUnless(
    os.environ.get("CTF_AGENT_NATIVE_TESTS") == "1",
    "Set CTF_AGENT_NATIVE_TESTS=1 for real native write-boundary tests",
)
class NativeWritePermissionTests(ProjectTest):
    def setUp(self):
        super().setUp()
        check_available()
        self.ws = Workspace(self.config.workspace, self.config.denied_paths, 10000, 100)
        # Test the meaningful boundary even when command workspace writes are ON.
        self.policy = replace(self.config.execution, writable=True)

    def test_forbidden_host_writes_remain_blocked(self):
        home = self.base / "host-home"
        executable = self.base / "host-path-bin"
        system = self.base / "host-system-etc"
        for directory in (home, executable, system):
            directory.mkdir(mode=0o700)
        targets = [
            executable / "existing-tool",
            home / ".bashrc",
            home / ".zshrc",
            home / ".profile",
            system / "existing-config",
        ]
        for path in targets:
            path.write_text("unchanged canary")
            self.assertTrue(os.access(path, os.W_OK))  # Ordinary OS permissions allow the parent.
        new_targets = [directory / "new-write-canary" for directory in (home, executable, system)]
        source = f"""
from pathlib import Path
Path('allowed-control.txt').write_text('workspace writing is enabled')
for name in {[str(p) for p in targets + new_targets]!r}:
    try:
        Path(name).write_text('write-test canary')
    except OSError:
        pass
    else:
        raise AssertionError('Forbidden host write succeeded: ' + name)
print('PATH, shell-profile and system-directory writes blocked')
"""
        with patch.dict(os.environ, {"PATH": str(executable) + os.pathsep + os.defpath}):
            result = run_native(self.ws, self.policy, 4096, "python", ["-c", source])
        self.assertIn("Exit code: 0", result)
        self.assertIn("writes blocked", result)
        self.assertEqual(self.ws.read("allowed-control.txt"), "workspace writing is enabled")
        for path in targets:
            self.assertEqual(path.read_text(), "unchanged canary")
        for path in new_targets:
            self.assertFalse(path.exists())

    def test_private_workspace_temp_and_build_writes_succeed(self):
        source = """
import json, os, tempfile
from pathlib import Path
Path('answer.txt').write_text('42')
Path('build').mkdir()
Path('build/output.txt').write_text('build evidence')
scratch = Path(os.environ['TMPDIR'])
assert Path.home() == scratch
temporary = scratch / 'temporary-output.txt'
temporary.write_text('temporary evidence')
assert temporary.read_text() == 'temporary evidence'
with tempfile.TemporaryDirectory() as directory:
    nested = Path(directory) / 'nested-output.txt'
    nested.write_text('private temporary evidence')
    assert nested.read_text() == 'private temporary evidence'
print(json.dumps({'scratch': str(scratch), 'safe_writes': 'passed'}))
"""
        result = run_native(self.ws, self.policy, 4096, "python", ["-c", source])
        self.assertTrue(result.startswith("Exit code: 0\n"), result)
        evidence = json.loads(result.split("\n", 1)[1])
        self.assertEqual(evidence["safe_writes"], "passed")
        self.assertEqual(self.ws.read("answer.txt"), "42")
        self.assertEqual(self.ws.read("build/output.txt"), "build evidence")
        self.assertFalse(Path(evidence["scratch"]).exists())  # Private scratch was cleaned up.
