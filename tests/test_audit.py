import io
import json
import os
import stat
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from aiagent.agent import run_agent
from aiagent.audit import AuditError, AuditRun, read_events, replay
from aiagent.cli import main
from aiagent.config import ConfigError
from aiagent.sandbox import CleanupError, run_command
from aiagent.tools import ToolRegistry
from aiagent.workspace import PolicyError
from tests.support import ProjectTest
from tests.test_agent_cli import response, tool


class AuditTests(ProjectTest):
    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry(self.config)

    def test_bundle_records_protocol_files_and_changes_without_credentials(self):
        (self.base / ".env").write_text("OPENROUTER_API_KEY=unique-private-key-888123")
        (self.config.workspace / "binary").write_bytes(b"\xff\x00\x80")
        replies = iter(
            [
                response(calls=[tool("write_file", '{"file_path":"result.txt","content":"42"}')]),
                response("The result is 42"),
            ]
        )
        client = OpenAI(
            api_key="unique-private-key-888123",
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
            patch("aiagent.cli.OpenAI", return_value=client),
            patch("aiagent.native.check_available", return_value="/fake/sandbox"),
            patch("aiagent.native.probe"),
            redirect_stderr(io.StringIO()),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["--config", str(self.config_path), "solve it"]), 0)
        (bundle,) = self.config.audit.directory.iterdir()
        events = list(read_events(bundle))
        types = [event["type"] for event in events]
        self.assertLess(types.index("tool_requested"), types.index("tool_result"))
        self.assertIn("model_request", types)
        self.assertIn("model_response", types)
        self.assertEqual(events[-1]["data"]["status"], "completed")
        self.assertEqual((bundle / "before/binary").read_bytes(), b"\xff\x00\x80")
        self.assertEqual((bundle / "after/result.txt").read_text(), "42")
        self.assertTrue((bundle / "source/aiagent/audit.py").is_file())
        self.assertTrue((bundle / "source/uv.lock").is_file())
        self.assertEqual(stat.S_IMODE(bundle.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((bundle / "events.jsonl").stat().st_mode), 0o600)
        summary = json.loads((bundle / "summary.json").read_text())
        self.assertEqual(summary["changes"]["created"], ["result.txt"])
        self.assertNotIn("unique-private-key-888123", (bundle / "events.jsonl").read_text())
        output = []
        self.assertEqual(replay(bundle, output.append), 0)
        self.assertIn("result.txt", "\n".join(output))

    def test_failure_and_interrupt_are_recorded(self):
        for error, status in (
            (RuntimeError("failure"), "failed"),
            (KeyboardInterrupt(), "interrupted"),
        ):
            audit = AuditRun(self.config, self.registry.workspace, "test")
            with self.assertRaises(type(error)), audit:
                raise error
            self.assertEqual(list(read_events(audit.path))[-1]["data"]["status"], status)

    def test_final_snapshot_failure_preserves_primary_cleanup_error(self):
        audit = AuditRun(self.config, self.registry.workspace, "test")
        with self.assertRaisesRegex(CleanupError, "docker rm -f exact-name"), audit:
            self.registry.workspace.snapshot = Mock(side_effect=PolicyError("snapshot failed"))
            raise CleanupError("docker rm -f exact-name")
        self.assertIn("audit incomplete", audit.finalization_error)
        self.assertEqual(
            json.loads((audit.path / "summary.json").read_text())["status"], "audit_failed"
        )

    def test_logging_failure_prevents_tool_execution(self):
        client = Mock()
        client.chat.completions.create.return_value = response(
            calls=[tool("write_file", '{"file_path":"never-written","content":"bad"}')]
        )
        audit = AuditRun(self.config, self.registry.workspace, "test")
        with self.assertRaises(AuditError), audit:
            record = audit.record

            def fail_before_execution(kind, **data):
                if kind == "tool_requested":
                    raise AuditError("simulated disk failure")
                record(kind, **data)

            with patch.object(audit, "record", side_effect=fail_before_execution):
                run_agent(client, self.config, "test", self.registry, audit=audit)
        self.assertFalse((self.config.workspace / "never-written").exists())

    def test_snapshots_skip_denied_links_and_special_files(self):
        root = self.config.workspace
        (root / ".env").write_text("hidden-key-111")
        outside = self.base / "outside"
        outside.write_text("outside-secret-222")
        (root / "symlink").symlink_to(outside)
        os.link(outside, root / "hardlink")
        os.mkfifo(root / "fifo")
        with AuditRun(self.config, self.registry.workspace, "test") as audit:
            pass
        manifest = json.loads((audit.path / "before.json").read_text())
        self.assertEqual(set(manifest["skipped"]), {".env", "symlink", "hardlink", "fifo"})
        self.assertEqual(list((audit.path / "before").iterdir()), [])
        self.assertNotIn("outside-secret-222", (audit.path / "events.jsonl").read_text())

    def test_snapshot_limit_fails_before_any_model_request(self):
        (self.config.workspace / "large").write_text("1234")
        config = replace(self.config, audit=replace(self.config.audit, max_snapshot_bytes=3))
        audit = AuditRun(config, self.registry.workspace, "test")
        with self.assertRaises(AuditError), audit:
            self.fail("Oversized initial snapshot must not start the run")
        self.assertEqual(
            json.loads((audit.path / "summary.json").read_text())["status"], "initialization_failed"
        )

    def test_log_budget_stops_and_marks_failure(self):
        config = replace(self.config, audit=replace(self.config.audit, max_log_bytes=20000))
        audit = AuditRun(config, self.registry.workspace, "test")
        with self.assertRaises(AuditError), audit:
            audit.record("too_big", content="x" * 20001)
        self.assertEqual(json.loads((audit.path / "summary.json").read_text())["status"], "failed")

    def test_replay_is_offline_and_detects_corruption_and_incomplete_runs(self):
        with AuditRun(self.config, self.registry.workspace, "test") as audit:
            pass
        with patch("aiagent.cli.OpenAI") as client, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--replay", str(audit.path), "--config", "/missing/config"]), 0)
            client.assert_not_called()
        log = audit.path / "events.jsonl"
        original = log.read_text()
        log.write_text(original.replace('"prompt":"test"', '"prompt":"tampered"'))
        with self.assertRaises(AuditError):
            list(read_events(audit.path))
        log.write_text(original.splitlines()[0] + "\n")
        output = []
        self.assertEqual(replay(audit.path, output.append), 1)
        self.assertIn("Incomplete audit", output[-1])

    def test_audit_directory_cannot_be_mounted_with_workspace(self):
        with self.assertRaises(ConfigError):
            self.change_config('directory = "runs"', 'directory = "workspace/runs"')

    def test_container_image_and_resolved_argv_recorded_before_start(self):
        events = []
        ok = subprocess.CompletedProcess([], 0, b"container")
        inspected = subprocess.CompletedProcess([], 0, b"sha256:123abc\n")
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch("aiagent.docker_sandbox.subprocess.run", side_effect=[ok, inspected, ok]),
            patch("aiagent.docker_sandbox._capture", return_value=(0, b"42", "")) as capture,
        ):
            run_command(
                self.registry.workspace,
                replace(self.config.execution, backend="docker"),
                1000,
                "python",
                ["test.py"],
                record=lambda kind, **data: events.append((kind, data)),
            )
        self.assertEqual(
            [kind for kind, _ in events],
            ["command_started", "command_finished", "container_removed"],
        )
        self.assertEqual(events[0][1]["image_id"], "sha256:123abc")
        self.assertEqual(events[0][1]["argv"][-1], "test.py")
        capture.assert_called_once()

    def test_audit_write_failure_after_container_creation_still_removes_it(self):
        ok = subprocess.CompletedProcess([], 0, b"container")
        inspected = subprocess.CompletedProcess([], 0, b"sha256:123abc\n")
        record = Mock(side_effect=[AuditError("disk full"), None])
        with (
            patch("aiagent.docker_sandbox.shutil.which", return_value="/usr/bin/docker"),
            patch(
                "aiagent.docker_sandbox.subprocess.run", side_effect=[ok, inspected, ok]
            ) as control,
            patch("aiagent.docker_sandbox._capture") as capture,
        ):
            with self.assertRaises(AuditError):
                run_command(
                    self.registry.workspace,
                    replace(self.config.execution, backend="docker"),
                    1000,
                    "python",
                    [],
                    record=record,
                )
        capture.assert_not_called()
        self.assertEqual(control.call_args.args[0][1:3], ["rm", "--force"])
