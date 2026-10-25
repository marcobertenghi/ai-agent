"""The entire agent loop: ask the model, dispatch bounded tools, repeat."""

import time

from aiagent.config import Config
from aiagent.display import Progress
from aiagent.tools import ToolRegistry


class AgentError(RuntimeError):
    """The task did not finish within its configured limits or API contract."""


def run_agent(
    client,
    config: Config,
    prompt: str,
    registry: ToolRegistry,
    verbose: bool = False,
    audit=None,
    progress: Progress | None = None,
) -> str:
    progress = progress or Progress(
        enabled=verbose, verbose=verbose, limit=config.limits.max_output_bytes
    )
    messages = [
        {
            "role": "system",
            "content": audit.system_prompt
            if audit
            else config.prompt_path.read_text(encoding="utf-8"),
        },
        {"role": "user", "content": prompt},
    ]
    calls_used = 0
    for turn in range(config.limits.max_turns):
        progress.turn(turn + 1, config.limits.max_turns)
        request = {
            "model": config.provider.model,
            "messages": messages,
            "max_completion_tokens": config.limits.max_output_tokens,
        }
        schemas = registry.schemas()
        if schemas:
            request["tools"] = schemas
        if audit:
            # Earlier events contain the conversation; don't duplicate its full
            # growing history on every request. Keep exact per-request options.
            audit.record(
                "model_request",
                turn=turn + 1,
                options={key: value for key, value in request.items() if key != "messages"},
            )
        response = client.chat.completions.create(**request)
        if audit:
            audit.record(
                "model_response",
                turn=turn + 1,
                response=response.model_dump(mode="json", exclude_none=True),
            )
        if not response.choices:
            raise AgentError("Provider returned no choices")
        choice = response.choices[0]
        message = choice.message
        progress.usage(response.usage)
        if choice.finish_reason not in {"stop", "tool_calls"}:
            raise AgentError(f"Provider stopped before completion ({choice.finish_reason})")
        if not message.tool_calls:
            if not message.content:
                raise AgentError("Provider returned an empty response")
            progress.model_finished(turn + 1, calls_used)
            return message.content
        # Refuse the entire batch before doing any work if it exceeds the budget.
        if calls_used + len(message.tool_calls) > config.limits.max_tool_calls:
            raise AgentError("Tool-call limit reached; task incomplete")
        if any(call.type != "function" for call in message.tool_calls):
            raise AgentError("Provider requested an unsupported tool type")
        ids = [call.id for call in message.tool_calls]
        if any(not call_id for call_id in ids) or len(ids) != len(set(ids)):
            raise AgentError("Provider returned missing or duplicate tool-call IDs")
        progress.update(message.content, len(message.tool_calls))
        messages.append(message.model_dump(exclude_none=True))
        for call in message.tool_calls:
            progress.tool(
                calls_used + 1,
                config.limits.max_tool_calls,
                call.function.name,
                call.function.arguments,
            )
            if audit:
                audit.record(
                    "tool_requested",
                    call_id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
            started = time.monotonic()
            result = registry.dispatch(call.function.name, call.function.arguments)
            if audit:
                audit.record("tool_result", call_id=call.id, content=result)
            progress.result(result, time.monotonic() - started)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            calls_used += 1
    raise AgentError("Turn limit reached; task incomplete")
