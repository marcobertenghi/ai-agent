"""Native execution: deny-by-default Seatbelt or an empty bubblewrap root.

Only the workspace, a private per-command scratch directory, and explicitly
trusted runtime files are visible. No Node runtime, daemon or proxy is required.
Outbound mode intentionally grants broad host networking; it is not an IP filter.
"""

import hashlib
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from aiagent.config import Execution, path_within
from aiagent.process import capture
from aiagent.workspace import PolicyError, Workspace


def check_available() -> str:
    if os.geteuid() == 0:
        raise PolicyError("Run native commands as a regular user, not root")
    if sys.platform == "darwin":
        path = Path("/usr/bin/sandbox-exec")
        if path.is_file():
            return str(path)
    elif sys.platform == "linux":
        path = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
        if path:
            return str(Path(path).resolve())
        raise PolicyError(
            "Native sandbox requires bubblewrap: install your distribution's bubblewrap package"
        )
    raise PolicyError(
        "Native sandbox unavailable: require macOS Seatbelt or Linux bubblewrap; no fallback"
    )


def probe(workspace: Workspace, policy: Execution, record=None):
    """Compile/apply the real policy before the first model request. No fallback."""
    if record:
        record("sandbox_preflight", backend="native")
    check = replace(policy, commands={"_check": (sys.executable, "-I", "-S", "-c", "pass")})
    result = run_native(workspace, check, 4096, "_check", [], record=record)
    if not result.startswith("Exit code: 0\n"):
        raise PolicyError("Native sandbox startup failed; no model request made. " + result)
    if record:
        record("sandbox_ready", backend="native")


def _runtime_paths(policy: Execution, executable: Path) -> list[Path]:
    with Path(__file__).with_name("native_policy.toml").open("rb") as handle:
        defaults = tomllib.load(handle)[sys.platform]["read_paths"]
    # The active interpreter may be installed by uv/Homebrew outside system paths.
    # Grant its libraries, and a dedicated virtualenv, rather than the user's home.
    extra = [
        Path(sysconfig.get_path("stdlib")).parent,
        Path(sysconfig.get_config_var("LIBDIR") or sys.base_prefix),
    ]
    # macOS Python may re-exec its bundled Python.app outside lib/. A dedicated
    # uv/Homebrew/framework prefix is a runtime, unlike a general /usr prefix.
    if sys.base_prefix not in {"/", "/usr", "/usr/local", "/opt/homebrew"}:
        extra.append(Path(sys.base_prefix))
    if sys.prefix != sys.base_prefix:
        extra.append(Path(sys.prefix))
    return list(
        dict.fromkeys(
            p
            for p in [
                *(Path(p) for p in defaults),
                *extra,
                *policy.read_paths,
                executable,
                Path(sys.executable),
            ]
            if p.exists()
        )
    )


def _quote(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


def seatbelt_profile(
    workspace: Workspace, policy: Execution, scratch: Path, reads: list[Path], helper: Path
) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow process-info* (target same-sandbox))",
        "(allow signal (target same-sandbox))",
        # Prevent a subprocess from escaping the group that timeout cleanup kills.
        # posix_spawn can change groups inside the kernel without calling setpgid.
        # Restrict it too; ordinary fork/exec subprocesses remain supported.
        "(deny syscall-unix (syscall-number SYS_setsid) (syscall-number SYS_setpgid) "
        "(syscall-number SYS_posix_spawn))",
        '(allow file-read* (literal "/"))',
        "(allow file-read-metadata)",
        '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.") '
        '(sysctl-name "kern.osrelease") (sysctl-name "kern.ostype") '
        '(sysctl-name "kern.osversion") (sysctl-name "kern.argmax"))',
        '(allow file-read* (literal "/dev/null") (literal "/dev/zero") '
        '(literal "/dev/random") (literal "/dev/urandom"))',
        '(allow file-write-data (literal "/dev/null"))',
    ]
    for path in [workspace.root, scratch, *reads, helper]:
        kind = "subpath" if path.is_dir() else "literal"
        lines.append(f"(allow file-read* ({kind} {_quote(path.resolve())}))")
    lines.append(f"(allow file-write* (subpath {_quote(scratch)}))")
    if policy.writable:
        lines.append(f"(allow file-write* (subpath {_quote(workspace.root)}))")
    if policy.network == "outbound":
        lines += [
            "(allow network-outbound (remote ip))",
            '(allow mach-lookup (global-name "com.apple.mDNSResponder"))',
        ]
    return "\n".join(lines) + "\n"


def bubblewrap_argv(
    binary: str,
    workspace: Workspace,
    policy: Execution,
    scratch: Path,
    reads: list[Path],
    helper: Path,
    command: list[str],
) -> list[str]:
    argv = [binary, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    if policy.network == "outbound":
        argv.append("--share-net")
    # A brand-new root: never bind '/' or expose /run, home, or the host's /proc.
    # Avoid overmounting symlink executables inside an already mounted runtime.
    mounts = [
        path
        for path in reads
        if not any(
            parent != path and parent.is_dir() and path.is_relative_to(parent) for parent in reads
        )
    ]
    for path in [*mounts, helper]:
        argv += ["--ro-bind", str(path.resolve()), str(path)]
    argv += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--bind",
        str(scratch),
        str(scratch),
        "--bind" if policy.writable else "--ro-bind",
        str(workspace.root),
        str(workspace.root),
        "--chdir",
        str(workspace.root),
        "--remount-ro",
        "/",
        "--",
        *command,
    ]
    return argv


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def run_native(
    workspace: Workspace,
    policy: Execution,
    output_limit: int,
    command: str,
    args: list[str],
    record=None,
) -> str:
    if command not in policy.commands:
        raise PolicyError("Command is not allowlisted")
    binary = check_available()
    recipe = list(policy.commands[command])
    executable = Path(sys.executable if recipe[0] == "python" else recipe[0]).absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise PolicyError("Configured native executable is missing or not executable")
    if executable.resolve().is_relative_to(workspace.root):
        raise PolicyError("Native executable must be outside the workspace")
    workspace.assert_mount_safe()
    reads = _runtime_paths(policy, executable)
    # Runtime allowances are trusted code/data, never an accidental grant of a
    # whole home, workspace, or project directory containing credentials.
    for path in reads:
        resolved = path.resolve()
        if not (path.is_file() or path.is_dir()):
            raise PolicyError(
                "Runtime read paths must be ordinary files/directories, not sockets or devices"
            )
        # Never turn a read allowance into access to host process handles, device
        # nodes or a directory of IPC services. Specific resolver files in /run
        # remain usable, but a read-only socket bind would still permit control.
        blocked = [Path("/proc"), Path("/sys"), Path("/dev")]
        if path.is_dir():
            blocked += [Path("/run"), Path("/var/run"), Path("/private/var/run")]
        if any(path_within(resolved, root.resolve()) for root in blocked):
            raise PolicyError(
                "Host process, device and IPC directories cannot be runtime read paths"
            )
        if path_within(workspace.root, resolved) or path_within(resolved, workspace.root):
            raise PolicyError("Runtime read paths must be separate from the workspace")
        if any(path_within(protected, resolved) for protected in policy.protected_paths):
            raise PolicyError(
                "Runtime read path would expose configuration, credentials, prompt, or audit records"
            )
    helper = Path(__file__).with_name("_native_exec.py").resolve()
    argv = [str(executable), *recipe[1:], *args]
    limits = {"timeout_seconds": policy.timeout_seconds, "memory_mb": policy.memory_mb}
    launcher = [sys.executable, "-I", "-S", str(helper), json.dumps(limits), *argv]
    with tempfile.TemporaryDirectory(prefix="ctf-agent-") as directory:
        scratch = Path(directory).resolve()
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        profile = None
        if sys.platform == "darwin":
            profile = seatbelt_profile(workspace, policy, scratch, reads, helper)
            launch = [binary, "-p", profile, *launcher]
        else:
            launch = bubblewrap_argv(binary, workspace, policy, scratch, reads, helper, launcher)
        if record:
            record(
                "command_started",
                backend="seatbelt" if profile else "bubblewrap",
                argv=argv,
                executable_sha256=_digest(executable),
                sandbox_executable_sha256=_digest(Path(binary)),
                launcher_sha256=_digest(helper),
                sandbox_argv=launch,
                runtime_read_paths=[str(p.resolve()) for p in reads],
                network=policy.network,
                writable=policy.writable,
                limits={
                    **limits,
                    "memory_enforced": sys.platform == "linux",
                    "aggregate_limits": False,
                    "file_size_bytes": 64 * 1024 * 1024,
                },
            )
        code, data, reason = capture(
            launch,
            policy.timeout_seconds,
            output_limit,
            cwd=workspace.root,
            env=env,
            process_group=True,
        )
        text = data.decode("utf-8", errors="replace")
        if record:
            record(
                "command_finished",
                backend=policy.backend,
                exit_code=code,
                output=text,
                stop_reason=reason,
            )
        status = f"Exit code: {code}" + (f"\n[{reason}]" if reason else "")
        return f"{status}\n{text or '[No output]'}"
