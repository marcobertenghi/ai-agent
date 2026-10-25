"""Run named commands in disposable Docker containers; never on the host."""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from aiagent.config import Execution
from aiagent.process import capture as _capture
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


def run_docker(
    workspace: Workspace,
    policy: Execution,
    output_limit: int,
    command: str,
    args: list[str],
    record=None,
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
        if record:
            # Inspect the CREATED container so mutable image tags cannot make
            # the audit refer to an image different from the one actually used.
            try:
                inspected = subprocess.run(
                    [docker, "inspect", "--format", "{{.Image}}", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise PolicyError(
                    "Sandbox image inspection timed out; command not started"
                ) from error
            image_id = inspected.stdout.decode("ascii", errors="replace").strip()
            if inspected.returncode or not image_id.startswith("sha256:"):
                raise PolicyError("Cannot record sandbox image identity; command not started")
            record(
                "command_started",
                container=name,
                image_id=image_id,
                docker_argv=argv,
                argv=[*policy.commands[command], *args],
                network=policy.network,
                writable=policy.writable,
            )
        code, output, reason = _capture(
            [docker, "start", "--attach", name], policy.timeout_seconds, output_limit
        )
        text = output.decode("utf-8", errors="replace")
        if record:
            record(
                "command_finished", container=name, exit_code=code, output=text, stop_reason=reason
            )
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
            if record:
                record("container_removed", container=name)
