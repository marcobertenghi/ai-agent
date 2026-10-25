import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI
from openai.types.chat import ChatCompletion

from aiagent.agent import AgentError, run_agent
from aiagent.cli import _terminal_text, main
from aiagent.tools import ToolRegistry
from tests.support import ProjectTest


def response(content=None, calls=None, finish=None):
    return ChatCompletion.model_validate(
        {
            "id": "test",
            "created": 0,
            "model": "test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish or ("tool_calls" if calls else "stop"),
                    "message": {"role": "assistant", "content": content, "tool_calls": calls},
                }
            ],
        }
    )


def tool(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class AgentTests(ProjectTest):
    def setUp(self):
        super().setUp()
        self.registry = ToolRegistry(self.config)
        self.create = Mock()
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        )

    def test_tool_result_round_trip_without_usage(self):
        (self.config.workspace / "empty").touch()
        self.create.side_effect = [
            response(calls=[tool("get_file_content", '{"file_path":"empty"}')]),
            response("Done"),
        ]
        self.assertEqual(run_agent(self.client, self.config, "test", self.registry, True), "Done")
        messages = self.create.call_args.kwargs["messages"]
        self.assertEqual(messages[-1], {"role": "tool", "tool_call_id": "call-1", "content": ""})
        self.assertEqual(
            self.create.call_args.kwargs["max_completion_tokens"],
            self.config.limits.max_output_tokens,
        )

    def test_real_sdk_protocol_with_in_memory_provider(self):
        requests = []
        replies = iter(
            [
                response(
                    calls=[
                        tool("write_file", '{"file_path":"answer.txt","content":"42"}'),
                        tool("get_file_content", '{"file_path":"answer.txt"}', "call-2"),
                    ]
                ),
                response("Verified: 42"),
            ]
        )

        def provider(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=next(replies).model_dump(exclude_none=True))

        with OpenAI(
            api_key="classroom-test-key",
            base_url=self.config.provider.base_url,
            http_client=httpx.Client(transport=httpx.MockTransport(provider)),
        ) as client:
            self.assertEqual(run_agent(client, self.config, "test", self.registry), "Verified: 42")
        self.assertEqual((self.config.workspace / "answer.txt").read_text(), "42")
        self.assertEqual(requests[1]["messages"][-1]["content"], "42")
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call-2")

    def test_exhaustion_is_an_error(self):
        config = replace(self.config, limits=replace(self.config.limits, max_turns=1))
        self.create.return_value = response(calls=[tool("get_files_info", "{}")])
        with self.assertRaisesRegex(AgentError, "Turn limit"):
            run_agent(self.client, config, "test", self.registry)

    def test_over_budget_batch_has_no_partial_side_effects(self):
        config = replace(self.config, limits=replace(self.config.limits, max_tool_calls=1))
        self.create.return_value = response(
            calls=[
                tool("write_file", '{"file_path":"x","content":"bad"}'),
                tool("get_files_info", "{}", "call-2"),
            ]
        )
        with self.assertRaisesRegex(AgentError, "Tool-call limit"):
            run_agent(self.client, config, "test", self.registry)
        self.assertFalse((config.workspace / "x").exists())

    def test_disabled_tool_returns_denial_to_model(self):
        registry = ToolRegistry(replace(self.config, allowed_tools=()))
        self.create.side_effect = [
            response(calls=[tool("write_file", '{"file_path":"x","content":"bad"}')]),
            response("Denied"),
        ]
        self.assertEqual(run_agent(self.client, self.config, "test", registry), "Denied")
        self.assertNotIn("tools", self.create.call_args.kwargs)
        self.assertIn("not allowlisted", self.create.call_args.kwargs["messages"][-1]["content"])

    def test_provider_truncation_empty_reply_and_duplicate_ids_fail(self):
        for item in (
            response("Partial", finish="length"),
            response(),
            response(calls=[tool("get_files_info", "{}"), tool("get_files_info", "{}")]),
        ):
            with self.subTest(item=item):
                self.create.return_value = item
                with self.assertRaises(AgentError):
                    run_agent(self.client, self.config, "test", self.registry)


class CliTests(ProjectTest):
    def test_check_needs_no_key_network_or_workspace_creation(self):
        self.config.workspace.rmdir()
        with patch("aiagent.cli.OpenAI") as client, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.config_path), "--check"]), 0)
            client.assert_not_called()
        self.assertFalse(self.config.workspace.exists())

    def test_missing_key_has_useful_error(self):
        with patch.dict("os.environ", {}, clear=True), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["--config", str(self.config_path), "test"]), 1)
        self.assertIn("OPENROUTER_API_KEY", error.getvalue())

    def test_terminal_control_codes_are_inert(self):
        self.assertEqual(_terminal_text("hello\x1b[2J\x00\nworld"), "hello?[2J?\nworld")
