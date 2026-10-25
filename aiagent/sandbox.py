"""Select an explicit execution backend. Never retry outside a sandbox."""

from aiagent.config import Execution
from aiagent.docker_sandbox import CleanupError, container_options, run_docker
from aiagent.process import capture as _capture
from aiagent.workspace import PolicyError, Workspace

__all__ = ["CleanupError", "container_options", "run_command", "_capture"]


def run_command(
    workspace: Workspace,
    policy: Execution,
    output_limit: int,
    command: str,
    args: list[str],
    record=None,
) -> str:
    if policy.backend == "docker":
        return run_docker(workspace, policy, output_limit, command, args, record)
    if policy.backend != "native":
        raise PolicyError("Unknown sandbox backend; unsandboxed execution is forbidden")
    from aiagent.native import run_native

    return run_native(workspace, policy, output_limit, command, args, record)
