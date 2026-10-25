"""Run named commands in disposable Docker containers; never on the host."""

import os
import selectors
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from aiagent.config import Execution
from aiagent.workspace import PolicyError, Workspace


class CleanupError(RuntimeError):
    """Stop the agent if we cannot confirm that its container was removed."""


def container_options(workspace: Workspace, policy: Execution, name: str) -> list[str]:
    """Only trusted configuration may supply Docker options; args go after image."""
    root = str(workspace.root)
    if "," in root or "\n" in root:
        raise PolicyError("Workspace paths cannot contain commas or newlines (Docker mount syntax)")
    uid = os.getuid() or 65534
    gid = os.getgid() or 65534
    mount = f"type=bind,src={root},dst=/workspace,bind-recursive=disabled"
    if not policy.writable:
        mount += ",readonly"
    return [
        "create",
        "--name",
        name,
        "--pull=never",
        "--network",
        policy.network,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
        "--user",
        f"{uid}:{gid}",
        "--workdir",
        "/workspace",
        "--memory",
        f"{policy.memory_mb}m",
        "--memory-swap",
        f"{policy.memory_mb}m",
        "--cpus",
        str(policy.cpus),
        "--pids-limit",
        str(policy.pids),
        "--ulimit",
        "nofile=256:256",
        "--ulimit",
        "core=0:0",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        mount,
        "--log-driver=none",
        "--init",
        "--entrypoint",
        "",
    ]


def _capture(argv: list[str], timeout: int, limit: int) -> tuple[int, bytes, str]:
    """Stream merged output into a bounded buffer; stop on time or output limits."""
    output = bytearray()
    reason = ""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "Command timed out"
                        break
                    if not selector.select(min(remaining, 0.1)):
                        continue
                    chunk = os.read(process.stdout.fileno(), min(8192, limit - len(output) + 1))
                    if not chunk:
                        break
                    available = limit - len(output)
                    output.extend(chunk[:available])
                    if len(chunk) > available:
                        reason = "Command stopped at output limit"
                        break
            if reason:
                process.kill()  # The caller removes the container, not just this Docker client.
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "Command timed out"
                process.kill()
                process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return process.returncode, bytes(output), reason


def run_command(
    workspace: Workspace, policy: Execution, output_limit: int, command: str, args: list[str]
) -> str:
    if command not in policy.commands:
        raise PolicyError("Command is not allowlisted")
    docker = shutil.which("docker")
    if docker is None:
        raise PolicyError(
            "Docker is required for commands. Build sandbox/Dockerfile; no host fallback is allowed"
        )
    docker = str(Path(docker).resolve())
    if Path(docker).is_relative_to(workspace.root):
        raise PolicyError("Docker executable must be outside the workspace")
    workspace.assert_mount_safe()
    name = "ctf-agent-" + uuid.uuid4().hex
    argv = [
        docker,
        *container_options(workspace, policy, name),
        policy.image,
        *policy.commands[command],
        *args,
    ]
    # Create before starting. This gives cleanup a known container even if the
    # attached client is killed. No agent credentials are passed with --env.
    needs_cleanup = True
    try:
        try:
            created = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PolicyError(f"Docker create timed out; check container {name}") from error
        if created.returncode:
            needs_cleanup = False  # Docker create failed; there is no container to start.
            raise PolicyError(
                "Docker could not create the sandbox. Check daemon and locally built image"
            )
        code, output, reason = _capture(
            [docker, "start", "--attach", name], policy.timeout_seconds, output_limit
        )
        text = output.decode("utf-8", errors="replace")
        status = f"Exit code: {code}" + (f"\n[{reason}]" if reason else "")
        return f"{status}\n{text or '[No output]'}"
    finally:
        if needs_cleanup:
            try:
                removed = subprocess.run(
                    [docker, "rm", "--force", "--volumes", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                if removed.returncode:
                    raise OSError("Docker removal failed")
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CleanupError(
                    f"Sandbox cleanup failed. Agent stopped; remove container with: docker rm -f {name}"
                ) from error
