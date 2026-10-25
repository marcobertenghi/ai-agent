import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from openai.types import CompletionUsage

from aiagent.audit import AuditError, AuditRun
from aiagent.cli import main
from aiagent.display import Progress
from tests.support import ProjectTest
from tests.test_agent_cli import response, tool


class DisplayTests(ProjectTest):
    def run_cli(self, *flags):
        update = response(
            "I'll save the solver, then report the result.",
            calls=[
                tool(
                    "write_file",
                    json.dumps(
                        {"file_path": "solve.py", "content": "print('solver-content-marker')"}
                    ),
                )
            ],
        )
        # A provider may return this extra field. It is not public narration.
        update.choices[0].message.reasoning_content = "PRIVATE_REASONING_MUST_NOT_DISPLAY"
        update.usage = CompletionUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.side_effect = [update, response("Saved the solver.")]
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
            patch("aiagent.cli.OpenAI", return_value=client),
            patch("aiagent.native.check_available", return_value="/fake/sandbox"),
            patch("aiagent.native.probe"),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            code = main(["--config", str(self.config_path), *flags, "Save a solver"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_progress_is_ordered_public_and_separate_from_final_answer(self):
        code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "Saved the solver.\n")
        self.assertIn("Model: ", stderr)
        self.assertIn("Turn 1/20", stderr)
        self.assertIn("100 input | 20 output | 120 total", stderr)
        self.assertLess(stderr.index("Agent update:"), stderr.index("Tool 1/40"))
        self.assertLess(stderr.index("Arguments:"), stderr.index("Tool returned"))
        self.assertLess(stderr.index("Result:"), stderr.index("Completed:"))
        self.assertIn("I'll save the solver", stderr)
        self.assertNotIn("PRIVATE_REASONING", stdout + stderr)
        self.assertNotIn("solver-content-marker", stderr)  # Compact write preview by default.
        self.assertIn("use --verbose to view", stderr)
        self.assertEqual(
            (self.config.workspace / "solve.py").read_text(), "print('solver-content-marker')"
        )

    def test_verbose_expands_generated_content(self):
        code, stdout, stderr = self.run_cli("--verbose")
        self.assertEqual(code, 0)
        self.assertIn("solver-content-marker", stderr)
        self.assertNotIn("PRIVATE_REASONING", stdout + stderr)

    def test_quiet_preserves_answer_and_audit_without_progress(self):
        code, stdout, stderr = self.run_cli("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "Saved the solver.\n")
        self.assertEqual(stderr, "")
        (bundle,) = self.config.audit.directory.iterdir()
        self.assertTrue((bundle / "events.jsonl").is_file())
        self.assertTrue((bundle / "after/solve.py").is_file())

    def test_audit_failure_is_not_reported_as_completed(self):
        original = AuditRun._snapshot

        def fail_final_snapshot(audit, phase):
            if phase == "after":
                raise AuditError("simulated final snapshot failure")
            return original(audit, phase)

        with patch.object(AuditRun, "_snapshot", new=fail_final_snapshot):
            code, stdout, stderr = self.run_cli()
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("audit incomplete", stderr)
        self.assertNotIn("Completed:", stderr)
        self.assertNotIn("Final answer:", stderr)

    def test_untrusted_previews_are_bounded_and_control_characters_are_inert(self):
        progress = Progress(verbose=True, limit=256)
        with redirect_stderr(io.StringIO()) as stderr:
            progress.tool(1, 40, "bad\nCompleted: injected\x1b[2J", "not JSON\x1b[31m")
            progress.event("command_started", argv=["/tool", "line\nCompleted: injected\x1b[2J"])
            progress.event("command_finished", exit_code=1, stop_reason="Command timed out")
            progress.result("x" * 100000, 0.1)
        text = stderr.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\nCompleted: injected", text)
        self.assertIn("Display truncated", text)
        self.assertIn("Command exit: 1 | Command timed out", text)
        self.assertLess(len(text), 2000)
