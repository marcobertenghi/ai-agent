import json
from dataclasses import replace
from unittest.mock import patch

from aiagent.config import ConfigError
from aiagent.tools import ToolRegistry
from tests.support import ProjectTest


class ConfigTests(ProjectTest):
    def test_paths_are_relative_to_config(self):
        self.assertEqual(self.config.workspace, self.base / "workspace")
        self.assertEqual(self.config.prompt_path, self.base / "system_prompt.md")

    def test_policy_cannot_be_inside_workspace(self):
        with self.assertRaises(ConfigError):
            self.change_config('workspace = "workspace"', 'workspace = "."')

    def test_credentials_symlink_cannot_point_inside_workspace(self):
        credentials = self.config.workspace / "credentials.txt"
        credentials.write_text("not-a-real-key")
        (self.base / ".env").symlink_to(credentials)
        from aiagent.config import load_config

        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_workspace_cannot_be_a_regular_file(self):
        self.config.workspace.rmdir()
        self.config.workspace.touch()
        from aiagent.config import load_config

        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_invalid_policy_fails_closed(self):
        original = self.config_path.read_text()
        cases = [
            ("max_turns = 20", "max_turns = 0"),
            ("max_tool_calls = 40", "max_tool_calls = true"),
            ('network = "none"', 'network = "host"'),
            ('network = "none"', 'network = "typo"'),
            (
                'allow = ["get_files_info", "get_file_content", "write_file", "run_command"]',
                'allow = ["unknown"]',
            ),
            (
                'allow = ["get_files_info", "get_file_content", "write_file", "run_command"]',
                'allow = ["write_file", "write_file"]',
            ),
            ("max_turns = 20", "max_tunrs = 20"),
            ('base_url = "https://openrouter.ai/api/v1"', 'base_url = "http://openrouter.ai"'),
            ('python = ["python", "-E", "-s", "-B"]', 'python = ["unknown-relative-command"]'),
            ("writable = false", 'writable = "false"'),
            ('python = ["python", "-E", "-s", "-B"]', ""),
            ('backend = "native"', 'backend = "unsandboxed"'),
        ]
        for old, new in cases:
            with self.subTest(new=new):
                self.config_path.write_text(original)
                with self.assertRaises(ConfigError):
                    self.change_config(old, new)

    def test_writable_execution_requires_write_tool(self):
        self.change_config("writable = false", "writable = true")
        with self.assertRaises(ConfigError):
            self.change_config('"write_file", ', "")


class ToolTests(ProjectTest):
    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry(self.config)

    def test_disabled_tool_is_hidden_and_not_dispatched(self):
        registry = ToolRegistry(replace(self.config, allowed_tools=("get_files_info",)))
        self.assertEqual([s["function"]["name"] for s in registry.schemas()], ["get_files_info"])
        result = registry.dispatch("write_file", '{"file_path":"x", "content":"bad"}')
        self.assertIn("not allowlisted", result)
        self.assertFalse((self.config.workspace / "x").exists())

    def test_malformed_arguments_do_not_crash_or_write(self):
        values = [
            "not json",
            "[]",
            "null",
            '{"file_path": "x"}',
            '{"file_path": "x", "content": 2}',
            '{"file_path": "x", "content": "bad", "working_directory": "/"}',
            '{"file_path": "x", "file_path": "y", "content": "bad"}',
            '{"file_path": "x", "content": NaN}',
            '{"file_path": "x", "content": "bad", "extra": true}',
        ]
        for args in values:
            with self.subTest(args=args):
                self.assertTrue(self.registry.dispatch("write_file", args).startswith("Error:"))
        self.assertFalse((self.config.workspace / "x").exists())

    def test_unknown_command_and_bad_argv_never_start_processes(self):
        with patch("aiagent.tools.run_command") as run:
            for args in (
                {"command": "shell"},
                {"command": "python", "args": "--version"},
                {"command": "python", "args": [123]},
                {"command": "python", "args": ["a"] * 129},
            ):
                self.assertTrue(
                    self.registry.dispatch("run_command", json.dumps(args)).startswith("Error:")
                )
            run.assert_not_called()

    def test_outputs_are_capped_and_empty_reads_are_valid(self):
        registry = ToolRegistry(
            replace(self.config, limits=replace(self.config.limits, max_output_bytes=256))
        )
        (self.config.workspace / "large").write_text("é" * 500)
        result = registry.dispatch("get_file_content", '{"file_path":"large"}')
        self.assertLessEqual(len(result.encode()), 256)
        self.assertIn("truncated", result)
        (self.config.workspace / "empty").touch()
        self.assertEqual(registry.dispatch("get_file_content", '{"file_path":"empty"}'), "")
