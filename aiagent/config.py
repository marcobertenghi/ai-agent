"""Load trusted operator configuration. Model output never changes this policy."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TOOL_NAMES = {"get_files_info", "get_file_content", "write_file", "run_command"}


class ConfigError(ValueError):
    """An invalid or unsafe operator configuration."""


@dataclass(frozen=True)
class Provider:
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: int


@dataclass(frozen=True)
class Limits:
    max_turns: int
    max_tool_calls: int
    max_output_tokens: int
    max_file_bytes: int
    max_output_bytes: int
    max_directory_entries: int


@dataclass(frozen=True)
class Execution:
    image: str
    network: str
    writable: bool
    timeout_seconds: int
    memory_mb: int
    cpus: int
    pids: int
    commands: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Audit:
    directory: Path
    max_snapshot_bytes: int
    max_log_bytes: int


@dataclass(frozen=True)
class Config:
    path: Path
    workspace: Path
    prompt_path: Path
    provider: Provider
    limits: Limits
    allowed_tools: tuple[str, ...]
    denied_paths: tuple[str, ...]
    execution: Execution
    audit: Audit


def _table(parent: dict, name: str, keys: set[str]) -> dict:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing table [{name}]")
    unknown = value.keys() - keys
    if unknown:
        raise ConfigError(f"Unknown keys in [{name}]: {', '.join(sorted(unknown))}")
    return value


def _text(table: dict, key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{key} must be a nonempty string")
    return value


def _integer(table: dict, key: str, low: int, high: int) -> int:
    value = table.get(key)
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{key} must be an integer between {low} and {high}")
    return value


def _strings(table: dict, key: str, unique: bool = True) -> tuple[str, ...]:
    values = table.get(key)
    if not isinstance(values, list) or any(
        not isinstance(v, str) or not v or "\x00" in v for v in values
    ):
        raise ConfigError(f"{key} must be a list of nonempty strings")
    if unique and len(values) != len(set(values)):
        raise ConfigError(f"{key} contains duplicates")
    return tuple(values)


def load_config(path: Path) -> Config:
    """Resolve paths against the TOML file, never the process's current directory."""
    path = path.resolve(strict=True)
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    unknown = data.keys() - {"agent", "provider", "limits", "tools", "execution", "audit"}
    if unknown:
        raise ConfigError(f"Unknown configuration tables: {', '.join(sorted(unknown))}")

    agent = _table(data, "agent", {"workspace", "prompt_file"})
    workspace = (path.parent / _text(agent, "workspace")).resolve()
    prompt = (path.parent / _text(agent, "prompt_file")).resolve(strict=True)
    if workspace.exists() and not workspace.is_dir():
        raise ConfigError("workspace must be a directory")
    # The model must not be able to rewrite the policy, prompt, or API credentials.
    for protected in (path, prompt, path.parent / ".env", Path(__file__).resolve()):
        protected = protected.resolve()
        inside = protected.is_relative_to(workspace)
        if workspace.exists():
            # Resolve alone does not canonicalize case on every filesystem.
            # Inode identity catches an alternate spelling of an existing root.
            inside = inside or any(workspace.samefile(parent) for parent in protected.parents)
        if inside:
            raise ConfigError(
                "Keep configuration, prompt, .env, and agent code outside the workspace"
            )
    if not prompt.is_file():
        raise ConfigError("prompt_file must be a regular text file")

    audit = _table(data, "audit", {"directory", "max_snapshot_bytes", "max_log_bytes"})
    audit_dir = (path.parent / _text(audit, "directory")).resolve()
    # Logs contain privileged configuration and must never be exposed by a mount.
    if audit_dir.is_relative_to(workspace) or workspace.is_relative_to(audit_dir):
        raise ConfigError("Audit directory and workspace must be separate, non-nested directories")
    if audit_dir.exists() and workspace.exists():
        if audit_dir.samefile(workspace) or any(workspace.samefile(p) for p in audit_dir.parents):
            raise ConfigError("Audit directory must be outside the workspace")
    audit_obj = Audit(
        audit_dir,
        _integer(audit, "max_snapshot_bytes", 1, 100_000_000),
        _integer(audit, "max_log_bytes", 4096, 100_000_000),
    )

    provider = _table(data, "provider", {"base_url", "model", "api_key_env", "timeout_seconds"})
    url = _text(provider, "base_url")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError("base_url must be an HTTPS URL without embedded credentials")
    key_env = _text(provider, "api_key_env")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ConfigError("api_key_env must be an environment variable name")

    limits = _table(
        data,
        "limits",
        {
            "max_turns",
            "max_tool_calls",
            "max_output_tokens",
            "max_file_bytes",
            "max_output_bytes",
            "max_directory_entries",
        },
    )
    limits_obj = Limits(
        _integer(limits, "max_turns", 1, 100),
        _integer(limits, "max_tool_calls", 1, 500),
        _integer(limits, "max_output_tokens", 1, 32768),
        _integer(limits, "max_file_bytes", 1, 1_000_000),
        _integer(limits, "max_output_bytes", 256, 1_000_000),
        _integer(limits, "max_directory_entries", 1, 10_000),
    )
    policy = _table(data, "tools", {"allow", "deny_paths"})
    allowed = _strings(policy, "allow")
    if set(allowed) - TOOL_NAMES:
        raise ConfigError(f"Unknown tools: {', '.join(sorted(set(allowed) - TOOL_NAMES))}")
    denied = _strings(policy, "deny_paths")

    execution = _table(
        data,
        "execution",
        {
            "image",
            "network",
            "writable",
            "timeout_seconds",
            "memory_mb",
            "cpus",
            "pids",
            "commands",
        },
    )
    image = _text(execution, "image")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image):
        raise ConfigError("Invalid container image name")
    network = _text(execution, "network")
    if network not in {"none", "bridge"}:
        raise ConfigError("network must be 'none' or 'bridge'; host networking is forbidden")
    writable = execution.get("writable")
    if type(writable) is not bool:
        raise ConfigError("writable must be a boolean")
    if writable and "write_file" not in allowed:
        raise ConfigError("Writable command execution also requires write_file in tools.allow")
    commands = execution.get("commands")
    if not isinstance(commands, dict):
        raise ConfigError("execution.commands must be a table of names to argv lists")
    recipes = {}
    for name in commands:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ConfigError("Command names must be simple identifiers")
        argv = _strings(commands, name, unique=False)
        if not argv or not argv[0].startswith("/"):
            raise ConfigError(f"Command {name} must start with an absolute in-container executable")
        recipes[name] = argv
    if "run_command" in allowed and not recipes:
        raise ConfigError("run_command requires at least one configured command")

    return Config(
        path,
        workspace,
        prompt,
        Provider(
            url, _text(provider, "model"), key_env, _integer(provider, "timeout_seconds", 1, 300)
        ),
        limits_obj,
        allowed,
        denied,
        Execution(
            image,
            network,
            writable,
            _integer(execution, "timeout_seconds", 1, 300),
            _integer(execution, "memory_mb", 32, 4096),
            _integer(execution, "cpus", 1, 4),
            _integer(execution, "pids", 8, 256),
            recipes,
        ),
        audit_obj,
    )
