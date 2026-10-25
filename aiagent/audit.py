"""Private run bundles and an offline, non-executing transcript viewer.

JSONL events are flushed and fsynced before side effects. Hash chaining detects
edits, not forgery: an owner can rewrite a whole bundle and recompute hashes.
No provider credentials or HTTP headers are collected by this module.
"""

import hashlib
import json
import os
import platform
import shlex
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from aiagent.config import Config
from aiagent.workspace import PolicyError, Workspace


class AuditError(RuntimeError):
    """An audit failure stops further model/tool operations."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


class AuditRun:
    def __init__(self, config: Config, workspace: Workspace, prompt: str):
        self.config, self.workspace, self.prompt = config, workspace, prompt
        self.system_prompt = config.prompt_path.read_text(encoding="utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = config.audit.directory / f"{stamp}-{uuid.uuid4().hex[:12]}"
        self.sequence = self.bytes_written = 0
        self.previous = "0" * 64
        self.stream = None
        self.before = None
        self.finalization_error = None

    def _save(self, name: str, content: str):
        with (self.path / name).open("x", encoding="utf-8") as handle:
            os.chmod(handle.fileno(), 0o600)
            handle.write(content)

    def record(self, event: str, **data):
        body = {
            "sequence": self.sequence,
            "time": datetime.now(timezone.utc).isoformat(),
            "type": event,
            "data": data,
            "previous": self.previous,
        }
        digest = hashlib.sha256(_json(body).encode()).hexdigest()
        line = (_json({**body, "sha256": digest}) + "\n").encode()
        if self.bytes_written + len(line) > self.config.audit.max_log_bytes:
            raise AuditError("Audit log limit reached; run stopped (audit.max_log_bytes)")
        try:
            self.stream.write(line)
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except OSError as error:
            raise AuditError("Cannot write audit log; run stopped") from error
        self.sequence += 1
        self.bytes_written += len(line)
        self.previous = digest

    def _snapshot(self, phase: str) -> dict:
        try:
            manifest = self.workspace.snapshot(
                self.path / phase, self.config.audit.max_snapshot_bytes
            )
        except (OSError, PolicyError) as error:
            raise AuditError(f"Cannot complete {phase} workspace snapshot: {error}") from error
        self._save(f"{phase}.json", _json(manifest) + "\n")
        self.record("workspace_snapshot", phase=phase, manifest=manifest)
        return manifest

    def _sources(self) -> dict:
        """Save only known source files, never a repository-wide copy or .env."""
        package = Path(__file__).resolve().parent
        root = package.parent
        paths = [(f"aiagent/{p.name}", p) for p in package.glob("*.py")]
        paths += [
            (name, root / name)
            for name in (
                "main.py",
                "pyproject.toml",
                "uv.lock",
                "README.md",
                ".python-version",
                ".gitignore",
                ".env.example",
                "sandbox/Dockerfile",
                "sandbox/network.Dockerfile",
            )
            if (root / name).is_file()
        ]
        paths += [(f"docs/{p.name}", p) for p in (root / "docs").glob("*.md")]
        hashes = {}
        for name, source in paths:
            target = self.path / "source" / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = source.read_bytes()
            target.write_bytes(data)
            target.chmod(0o600)
            hashes[name] = hashlib.sha256(data).hexdigest()
        return hashes

    def __enter__(self):
        self.config.audit.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.mkdir(mode=0o700)
        self.stream = (self.path / "events.jsonl").open("xb")
        os.chmod(self.stream.fileno(), 0o600)
        try:
            self._save("policy.toml", self.config.path.read_text(encoding="utf-8"))
            self._save("system_prompt.md", self.system_prompt)
            self._save("prompt.txt", self.prompt)
            self.record(
                "run_started",
                format_version=1,
                prompt=self.prompt,
                system_prompt=self.system_prompt,
                policy=asdict(self.config),
                source_sha256=self._sources(),
                runtime={
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "openai": version("openai"),
                    "python-dotenv": version("python-dotenv"),
                },
            )
            self.before = self._snapshot("before")
            return self
        except BaseException as error:
            try:
                self._save(
                    "summary.json",
                    _json({"status": "initialization_failed", "error_type": type(error).__name__})
                    + "\n",
                )
            finally:
                self.stream.close()
            raise

    def answer(self, text: str):
        self._save("answer.txt", text)

    def __exit__(self, exc_type, exc, traceback):
        status = (
            "completed"
            if exc_type is None
            else "interrupted"
            if exc_type is KeyboardInterrupt
            else "failed"
        )
        summary = {"status": status, "error_type": exc_type.__name__ if exc_type else None}
        audit_failure = None
        try:
            after = self._snapshot("after")
            before_files, after_files = self.before["files"], after["files"]
            summary["changes"] = {
                "created": sorted(after_files.keys() - before_files.keys()),
                "deleted": sorted(before_files.keys() - after_files.keys()),
                "modified": sorted(
                    name
                    for name in before_files.keys() & after_files.keys()
                    if before_files[name] != after_files[name]
                ),
            }
            self.record("run_finished", **summary)
        except (AuditError, OSError) as error:
            audit_failure = error
            summary = {
                "status": "audit_failed",
                "error_type": type(error).__name__,
                "run_error_type": exc_type.__name__ if exc_type else None,
            }
        finally:
            try:
                self._save("summary.json", _json(summary) + "\n")
            except OSError as error:
                audit_failure = error
            try:
                self.stream.close()
            except OSError as error:
                audit_failure = error
        if audit_failure:
            notice = f"Run audit incomplete; inspect {self.path}"
            if exc_type is None:
                raise AuditError(notice) from audit_failure
            # Preserve the primary failure, especially an exact container removal
            # instruction. The CLI reports this additional notice separately.
            self.finalization_error = notice
        return False


def read_events(path: Path):
    """Verify event order/hash chain while reading; never execute logged content."""
    previous = "0" * 64
    sequence = total = 0
    ended = False
    try:
        with (path / "events.jsonl").open("rb") as handle:
            for line in iter(lambda: handle.readline(100_000_001), b""):
                total += len(line)
                if total > 100_000_000:
                    raise AuditError("Replay log exceeds the 100 MB safety limit")
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                    raise AuditError("Invalid audit event")
                if ended:
                    raise AuditError("Unexpected event after run_finished")
                if not line.endswith(b"\n"):
                    raise AuditError("Incomplete audit event")
                digest = event.pop("sha256")
                if (
                    event["sequence"] != sequence
                    or event["previous"] != previous
                    or hashlib.sha256(_json(event).encode()).hexdigest() != digest
                ):
                    raise AuditError("Audit event order or checksum mismatch")
                if sequence == 0 and (
                    event["type"] != "run_started" or event["data"].get("format_version") != 1
                ):
                    raise AuditError("Unsupported audit format")
                yield event
                ended = event["type"] == "run_finished"
                sequence += 1
                previous = digest
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        raise AuditError("Cannot read a valid audit transcript") from error


def replay(path: Path, emit) -> int:
    """Print the recorded steps. Replay deliberately has no execution capability."""
    finished = None
    for event in read_events(path):
        kind, data = event["type"], event["data"]
        if kind == "run_started":
            emit(f"Task: {data['prompt']}")
        elif kind == "tool_requested":
            emit(f"\nTool {data['call_id']}: {data['name']}\n{data['arguments']}")
        elif kind == "tool_result":
            emit(f"Result {data['call_id']}:\n{data['content']}")
        elif kind == "command_started":
            emit(f"Container argv ({data['image_id']}): {shlex.join(data['argv'])}")
        elif kind == "model_response":
            choices = data["response"].get("choices", [])
            if choices and choices[0]["message"].get("content"):
                emit(f"\nAssistant: {choices[0]['message']['content']}")
        elif kind == "run_finished":
            finished = data["status"]
            emit(f"\nRun status: {finished}\nFile changes: {_json(data['changes'])}")
    if finished is None:
        emit("\nIncomplete audit: no run_finished event (interrupted process or audit failure).")
    return 0 if finished == "completed" else 1
