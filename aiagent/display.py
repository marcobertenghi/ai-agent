"""Small plain-text progress display; no terminal library or extra model calls.

Only public assistant content and observed tool activity are displayed. Provider
reasoning fields are deliberately never inspected. Progress stays on stderr so
stdout remains suitable for piping the final answer.
"""

import json
import shlex
import sys
import time


def terminal_text(text: str) -> str:
    """Neutralize ANSI/control characters from untrusted model and tool text."""
    return "".join(c if c in "\n\t" or c.isprintable() else "?" for c in text)


class Progress:
    def __init__(self, *, enabled: bool = True, verbose: bool = False, limit: int = 16000):
        self.enabled = enabled
        self.verbose = verbose
        self.limit = limit
        self.started = time.monotonic()
        self.preflight = False
        self.turns = self.calls = 0

    def line(self, text: str = ""):
        if self.enabled:
            print(terminal_text(text), file=sys.stderr, flush=True)

    def block(self, label: str, text: str, indent: int = 4):
        if not self.enabled:
            return
        prefix = " " * indent
        self.line(f"{prefix}{label}:")
        # Console previews are separate from faithful audit records. Bound both
        # line count and characters so generated scripts/binary dumps stay usable.
        chars, lines = (self.limit, 100) if self.verbose else (min(self.limit, 1200), 12)
        clipped = text[:chars]
        visible = clipped.splitlines()
        for value in visible[:lines] or ["[Empty]"]:
            self.line(f"{prefix}  {value}")
        if len(text) > chars or len(visible) > lines:
            self.line(f"{prefix}  [Display truncated; full record in the audit bundle]")

    def start(self, config, audit_path, prompt: str):
        self.line("Run")
        self.block("Task", prompt, indent=2)
        self.line(f"  Model: {config.provider.model}")
        self.line(f"  Workspace: {config.workspace}")
        writes = "enabled" if config.execution.writable else "read-only"
        self.line(
            f"  Sandbox: {config.execution.backend} | network: {config.execution.network} | command writes: {writes}"
        )
        self.line(f"  Audit: {audit_path}")
        self.line("  Setup: saving policy, source, and starting workspace snapshot...")

    def event(self, kind: str, **data):
        """Observe execution metadata after it has been recorded by the audit log."""
        if not self.enabled:
            return
        if kind == "sandbox_preflight":
            self.preflight = True
            self.line("  Setup: checking native sandbox isolation...")
        elif kind == "sandbox_ready":
            self.preflight = False
            self.line("  Setup: sandbox ready.")
        elif kind == "command_started" and not self.preflight:
            self.block("Sandbox command", shlex.join(data["argv"]))
            if self.verbose:
                identity = data.get("image_id") or data.get("executable_sha256")
                if identity:
                    self.line(f"    Executable/image identity: {identity}")
        elif kind == "command_finished" and not self.preflight:
            reason = f" | {data['stop_reason']}" if data.get("stop_reason") else ""
            self.line(f"    Command exit: {data['exit_code']}{reason}")

    def turn(self, number: int, maximum: int):
        self.line()
        self.line(f"Turn {number}/{maximum}")
        self.line("  Requesting model response...")

    def usage(self, usage):
        if usage is not None:
            self.line(
                f"  Tokens: {usage.prompt_tokens} input | {usage.completion_tokens} output | {usage.total_tokens} total"
            )

    def update(self, content: str | None, call_count: int):
        if content:
            self.block("Agent update", content)
        else:
            self.line(f"  Agent requested {call_count} tool call(s).")

    def tool(self, number: int, maximum: int, name: str, raw_arguments: str):
        if not self.enabled:
            return
        self.line(f"  Tool {number}/{maximum}: {name!r}")
        body = None
        try:
            args = json.loads(raw_arguments)
            if name == "write_file" and isinstance(args, dict):
                args = dict(args)
                content = args.get("content")
                if isinstance(content, str):
                    if self.verbose:
                        body = content
                        args["content"] = f"[{len(content)} characters; shown below]"
                    else:
                        args["content"] = f"[{len(content)} characters; use --verbose to view]"
            text = json.dumps(args, indent=2, ensure_ascii=True)
        except (ValueError, TypeError, RecursionError):
            text = str(raw_arguments)
        self.block("Arguments", text)
        if body is not None:
            self.block("File content", body)

    def result(self, content: str, elapsed: float):
        state = "error" if content.startswith("Error:") else "returned"
        self.line(f"    Tool {state} in {elapsed:.2f}s")
        self.block("Result", content)

    def model_finished(self, turns: int, calls: int):
        self.turns, self.calls = turns, calls
        self.line("  Model finished; saving the final answer and audit snapshot...")

    def completed(self):
        self.line()
        self.line(
            f"Completed: {self.turns} turn(s), {self.calls} tool call(s), {time.monotonic() - self.started:.2f}s"
        )
        self.line("Final answer:")
