"""One small registry for tool schemas, argument validation, and dispatch."""

import json
from dataclasses import dataclass
from typing import Callable

from aiagent.config import Config
from aiagent.sandbox import run_command
from aiagent.workspace import PolicyError, Workspace


@dataclass(frozen=True)
class Tool:
    description: str
    properties: dict
    required: tuple[str, ...]
    handler: Callable[..., str]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError("Duplicate JSON argument keys are forbidden")
        result[key] = value
    return result


def _invalid_constant(value):
    raise PolicyError(f"Invalid JSON constant: {value}")


class ToolRegistry:
    def __init__(self, config: Config):
        self.config = config
        self.record = None
        self.workspace = Workspace(
            config.workspace,
            config.denied_paths,
            config.limits.max_file_bytes,
            config.limits.max_directory_entries,
        )
        path = {
            "type": "string",
            "description": "Relative workspace path; links and '..' are forbidden",
        }
        self.tools = {
            "get_files_info": Tool(
                "List a workspace directory (bounded output)",
                {"directory": path},
                (),
                self.workspace.list,
            ),
            "get_file_content": Tool(
                "Read a UTF-8 workspace file (bounded output)",
                {"file_path": path},
                ("file_path",),
                self.workspace.read,
            ),
            "write_file": Tool(
                "Create or overwrite a UTF-8 workspace file",
                {"file_path": path, "content": {"type": "string"}},
                ("file_path", "content"),
                self.workspace.write,
            ),
            "run_command": Tool(
                f"Run a configured command with the {config.execution.backend} sandbox. "
                "Paths are relative to the workspace. "
                f"Network: {config.execution.network}; writable: {config.execution.writable}",
                {
                    "command": {"type": "string", "enum": list(config.execution.commands)},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Extra arguments appended to the configured argv",
                    },
                },
                ("command",),
                self._command,
            ),
        }

    def _command(self, command: str, args: list[str] | None = None) -> str:
        return run_command(
            self.workspace,
            self.config.execution,
            self.config.limits.max_output_bytes,
            command,
            args or [],
            record=self.record,
        )

    def schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": self.tools[name].description,
                    "parameters": {
                        "type": "object",
                        "properties": self.tools[name].properties,
                        "required": list(self.tools[name].required),
                        "additionalProperties": False,
                    },
                },
            }
            for name in self.config.allowed_tools
        ]

    def dispatch(self, name: str, raw_arguments: str) -> str:
        """Enforce policy even if the model invents a tool absent from its schema."""
        try:
            if name not in self.config.allowed_tools or name not in self.tools:
                raise PolicyError("Tool is not allowlisted")
            if not isinstance(raw_arguments, str) or len(raw_arguments.encode("utf-8")) > (
                self.config.limits.max_file_bytes * 6 + 16_384
            ):
                raise PolicyError("Tool arguments exceed the size limit")
            args = json.loads(
                raw_arguments, object_pairs_hook=_object, parse_constant=_invalid_constant
            )
            tool = self.tools[name]
            if not isinstance(args, dict):
                raise PolicyError("Tool arguments must be a JSON object")
            if args.keys() - tool.properties.keys() or set(tool.required) - args.keys():
                raise PolicyError("Unknown or missing tool arguments")
            for key, value in args.items():
                schema = tool.properties[key]
                if schema["type"] == "string":
                    if not isinstance(value, str) or "\x00" in value:
                        raise PolicyError(f"{key} must be a string without NUL bytes")
                elif (
                    not isinstance(value, list)
                    or len(value) > 128
                    or any(not isinstance(v, str) or "\x00" in v or len(v) > 8192 for v in value)
                    or sum(len(v.encode("utf-8")) for v in value) > 32768
                ):
                    raise PolicyError(
                        f"{key} must contain at most 128 string arguments, totaling at most 32768 bytes"
                    )
                if "enum" in schema and value not in schema["enum"]:
                    raise PolicyError(f"{key} is not allowlisted")
            result = tool.handler(**args)
        except (ValueError, OSError, RecursionError) as error:
            # Don't send host paths, OS details, or secrets from exception text to the model.
            result = (
                f"Error: {error}"
                if isinstance(error, PolicyError)
                else "Error: Invalid request or inaccessible workspace file"
            )
        limit = self.config.limits.max_output_bytes
        encoded = result.encode("utf-8")
        if len(encoded) > limit:
            marker = b"\n[Tool output truncated]"
            result = (
                encoded[: limit - len(marker)].decode("utf-8", errors="ignore") + marker.decode()
            )
        return result
