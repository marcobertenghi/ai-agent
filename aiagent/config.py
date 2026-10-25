"""Load trusted operator configuration. Model output never changes this policy."""

import os
import re
import sys
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

TOOL_NAMES = {"get_files_info", "get_file_content", "write_file", "run_command"}


def path_within(path: Path, directory: Path) -> bool:
    """Conservative containment, including future case/Unicode and inode aliases."""
    # Fold even nonexistent tails: on macOS a future PATH directory and a new
    # workspace can name the same location without either inode existing yet.
    # On case-sensitive filesystems this intentionally errs toward denying a grant.
    parts = tuple(unicodedata.normalize("NFC", p).casefold() for p in path.parts)
    root = tuple(unicodedata.normalize("NFC", p).casefold() for p in directory.parts)
    if parts[: len(root)] == root:
        return True
    if not directory.exists():
        return False
    return any(directory.samefile(parent) for parent in (path, *path.parents) if parent.exists())


class ConfigError(ValueError):
    """An invalid or unsafe operator configuration."""


def validate_write_root(workspace: Path, label: str = "workspace"):
    """A workspace grants file-tool writes even when commands are read-only.

    Reject persistent execution/configuration locations before creating files or
    starting a model. Resolve symlinks and compare inode aliases as well as paths.
    These checks prevent common unsafe configurations; arbitrary application
    hooks/auto-loaded files still belong outside the task workspace.
    """
    workspace = workspace.resolve()
    system = (
        "/etc",
        "/private/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/System",
        "/Library",
        "/Applications",
        "/boot",
        "/dev",
        "/proc",
        "/sys",
        "/run",
        "/var/run",
        "/private/var/run",
        "/var/lib",
        "/var/db",
        "/var/log",
        "/private/var/db",
        "/private/var/log",
        "/opt/homebrew",
        "/opt/local",
        "/nix/store",
    )
    executable_dirs = [Path(value or os.curdir).resolve() for value in os.get_exec_path()]
    executable_dirs += [Path("/usr/local/bin"), Path("/usr/local/sbin")]
    runtime_dirs = [Path(sys.prefix), Path(sys.base_prefix)]
    for category, roots in (
        ("system directory", map(Path, system)),
        ("PATH/executable directory", executable_dirs),
        ("Python runtime", runtime_dirs),
    ):
        for root in roots:
            root = root.resolve()
            if path_within(workspace, root) or path_within(root, workspace):
                raise ConfigError(f"Unsafe {label}: overlaps a {category}")

    home = Path.home().resolve()
    # Whole home/shared temp directories would expose other applications' files.
    # A dedicated project or private temporary subdirectory is permitted.
    for broad_root in (
        home,
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
        Path("/var/tmp").resolve(),
    ):
        if path_within(broad_root, workspace):
            raise ConfigError(
                f"Unsafe {label}: use a dedicated subdirectory, not a home/shared temp root"
            )

    zsh = Path(os.path.expandvars(os.environ.get("ZDOTDIR", str(home)))).expanduser().resolve()
    startup = [home / name for name in (".bashrc", ".bash_profile", ".bash_login", ".profile")]
    startup += [zsh / name for name in (".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout")]
    startup.append(home / ".config/fish")
    for variable in ("BASH_ENV", "ENV"):
        value = os.environ.get(variable)
        if value:
            # Expand path variables only; never evaluate shell expressions.
            startup.append(Path(os.path.expandvars(value)).expanduser())
    for path in startup:
        path = path.resolve()
        if path_within(path, workspace) or path_within(workspace, path):
            raise ConfigError(f"Unsafe {label}: overlaps a host shell startup path")


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
    backend: str = "native"
    read_paths: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()


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
    validate_write_root(workspace)
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
    validate_write_root(audit_dir, label="audit directory")
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
            "backend",
            "read_paths",
        },
    )
    backend = _text(execution, "backend")
    if backend not in {"native", "docker"}:
        raise ConfigError(
            "backend must be 'native' or 'docker'; unsandboxed execution is forbidden"
        )
    read_paths = tuple(
        (path.parent / value).resolve(strict=True) for value in _strings(execution, "read_paths")
    )
    image = _text(execution, "image")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image):
        raise ConfigError("Invalid container image name")
    network = _text(execution, "network")
    networks = {"none", "outbound"} if backend == "native" else {"none", "bridge"}
    if network not in networks:
        raise ConfigError(f"network must be one of {sorted(networks)} for backend {backend}")
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
        if not argv or (
            not argv[0].startswith("/") and not (backend == "native" and argv[0] == "python")
        ):
            raise ConfigError(
                f"Command {name} requires an absolute executable, or 'python' with native"
            )
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
            backend,
            read_paths,
            (path, prompt, (path.parent / ".env").resolve(), audit_dir),
        ),
        audit_obj,
    )
