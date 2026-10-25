"""Minimal CLI; importing modules never reads keys or makes network requests."""

import argparse
import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError, OpenAI

from aiagent.agent import AgentError, run_agent
from aiagent.audit import AuditError, AuditRun, replay
from aiagent.config import ConfigError, load_config, validate_write_root
from aiagent.sandbox import CleanupError
from aiagent.tools import ToolRegistry
from aiagent.workspace import PolicyError


def _terminal_text(text: str) -> str:
    # Tool/provider text must not execute ANSI terminal control sequences.
    return "".join(c if c in "\n\t" or c.isprintable() else "?" for c in text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A small AI agent for CTFs and authorized security labs"
    )
    parser.add_argument("prompt", nargs="?", help="Task and authorized targets")
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"), help="Trusted TOML policy"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print turns, token usage, and tool names"
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate policy offline; no API key required"
    )
    parser.add_argument(
        "--replay",
        type=Path,
        metavar="RUN_DIRECTORY",
        help="View a saved audit transcript offline; never executes tools",
    )
    args = parser.parse_args(argv)
    if args.replay and (args.prompt or args.check):
        parser.error("--replay cannot be combined with a prompt or --check")
    if not args.check and not args.replay and (not args.prompt or not args.prompt.strip()):
        parser.error("a nonempty prompt is required unless using --check")
    audit = None
    try:
        if args.replay:
            return replay(args.replay, lambda text: print(_terminal_text(text)))
        config = load_config(args.config)
        registry = ToolRegistry(config)
        if args.check:
            print(f"Policy valid. Workspace: {config.workspace}")
            print(f"Allowed tools: {', '.join(config.allowed_tools) or '(none)'}")
            print(
                f"Command network: {config.execution.network}; writable: {config.execution.writable}"
            )
            print(f"Execution backend: {config.execution.backend}")
            print("Runtime availability and isolation are not checked by --check.")
            print(f"Audit directory: {config.audit.directory}")
            return 0
        load_dotenv(config.path.parent / ".env", override=False)
        # .env may supply PATH or shell startup variables that were absent when
        # the policy was loaded. Recheck before any workspace/audit writes.
        validate_write_root(config.workspace)
        validate_write_root(config.audit.directory, label="audit directory")
        api_key = os.environ.get(config.provider.api_key_env)
        if not api_key:
            raise ConfigError(
                f"Set {config.provider.api_key_env} in the environment or .env next to the config"
            )
        if "run_command" in config.allowed_tools and config.execution.backend == "native":
            from aiagent.native import check_available

            check_available()
        config.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        audit = AuditRun(config, registry.workspace, args.prompt)
        print(f"Audit: {audit.path}", file=sys.stderr)
        with audit:
            registry.record = audit.record
            if "run_command" in config.allowed_tools and config.execution.backend == "native":
                from aiagent.native import probe

                probe(registry.workspace, config.execution, record=audit.record)
            with OpenAI(
                base_url=config.provider.base_url,
                api_key=api_key,
                timeout=config.provider.timeout_seconds,
                max_retries=0,
            ) as client:
                answer = run_agent(client, config, args.prompt, registry, args.verbose, audit=audit)
            audit.answer(answer)
        print(_terminal_text(answer))
        return 0
    except APIError:
        # Provider exception bodies can include credentials or prompt fragments.
        print(
            "Error: Model request failed. Check provider settings, key, model, and connectivity.",
            file=sys.stderr,
        )
        return 1
    except (
        ConfigError,
        PolicyError,
        AgentError,
        CleanupError,
        AuditError,
        tomllib.TOMLDecodeError,
    ) as error:
        print(f"Error: {_terminal_text(str(error))}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print(
            "Error: Could not access a required config, prompt, workspace, or audit file.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    finally:
        if audit is not None and audit.finalization_error:
            print(f"Warning: {_terminal_text(audit.finalization_error)}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
