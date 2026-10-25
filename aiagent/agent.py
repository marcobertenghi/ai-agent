"""The entire agent loop: ask the model, dispatch bounded tools, repeat."""

import sys

from aiagent.config import Config
from aiagent.tools import ToolRegistry


class AgentError(RuntimeError):
    """The task did not finish within its configured limits or API contract."""


def run_agent(
    client, config: Config, prompt: str, registry: ToolRegistry, verbose: bool = False
) -> str:
    messages = [
        {"role": "system", "content": config.prompt_path.read_text(encoding="utf-8")},
        {"role": "user", "content": prompt},
    ]
    calls_used = 0
    for turn in range(config.limits.max_turns):
        request = {
            "model": config.provider.model,
            "messages": messages,
            "max_completion_tokens": config.limits.max_output_tokens,
        }
        schemas = registry.schemas()
        if schemas:
            request["tools"] = schemas
        response = client.chat.completions.create(**request)
        if not response.choices:
            raise AgentError("Provider returned no choices")
        choice = response.choices[0]
        message = choice.message
        if verbose:
            usage = response.usage
            tokens = f", tokens={usage.total_tokens}" if usage else ""
            print(f"Turn {turn + 1}/{config.limits.max_turns}{tokens}", file=sys.stderr)
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise AgentError(f"Provider stopped before completion ({choice.finish_reason})")
        if not message.tool_calls:
            if not message.content:
                raise AgentError("Provider returned an empty response")
            return message.content
        # Refuse the entire batch before doing any work if it exceeds the budget.
        if calls_used + len(message.tool_calls) > config.limits.max_tool_calls:
            raise AgentError("Tool-call limit reached; task incomplete")
        if any(call.type != "function" for call in message.tool_calls):
            raise AgentError("Provider requested an unsupported tool type")
        ids = [call.id for call in message.tool_calls]
        if any(not call_id for call_id in ids) or len(ids) != len(set(ids)):
            raise AgentError("Provider returned missing or duplicate tool-call IDs")
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            if verbose:
                # Names come from the model; repr keeps terminal control codes inert.
                print(f"Tool: {call.function.name!r}", file=sys.stderr)
            result = registry.dispatch(call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            calls_used += 1
    raise AgentError("Turn limit reached; task incomplete")
