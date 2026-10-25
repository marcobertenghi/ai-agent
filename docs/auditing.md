# Audit a run and reproduce its steps

Every model/tool run creates `runs/<UTC timestamp>-<random id>/` and
prints the path to stderr. `--check` and `--replay` do not create runs. Auditing
is mandatory in the CLI: if a record cannot be written, further operations stop.
There is no logging server or additional dependency. Configuration errors and
missing API keys fail before a run starts.

## Inspect a run

```sh
uv run ctf-agent --replay runs/<timestamp-id>
```

This is an offline transcript viewer. It verifies the event order and hash chain,
then displays the task, assistant text, tool requests/results, resolved command argv, status, and
file changes. It never reexecutes a logged instruction. You can also read the
JSON files directly with your usual tools.

```text
runs/<timestamp-id>/
  events.jsonl       Timestamped, ordered, hash-chained events
  policy.toml        Selected operator policy (not .env)
  system_prompt.md  Exact system prompt supplied to the model
  prompt.txt        User task
  answer.txt        Final answer, if reached
  before/           Initial permitted workspace files, including binary data
  before.json       Initial files' SHA-256 hashes, sizes, modes, skipped entries
  after/            Final permitted workspace files
  after.json        Final snapshot manifest
  source/           Agent modules/native policy, project metadata/lockfile, Dockerfiles
  summary.json      Status and created/modified/deleted regular files
```

The event stream contains:

- The effective policy, task/system prompts, Python/platform/dependency versions,
  and hashes of the source copies.
- Each model request's options and tool schemas, and its full parsed SDK response:
  model identifier, usage when supplied, assistant text, and proposed tool calls.
  Conversation history is reconstructed from these events; it is not duplicated
  on every turn. Raw HTTP headers and SDK client credentials are not collected.
- Each dispatched tool request before execution, then the result actually returned
  to the model. Malformed and disallowed requests and their denials are included.
  Calls rejected as an over-budget batch remain visible in the model response.
- For native commands: resolved argv, hashes of the executable, sandbox binary
  and trusted launcher, runtime read paths, limits and the exact generated
  Seatbelt profile/bubblewrap invocation. The fixed startup probe is also recorded.
- For Docker commands: exact create argv, resolved in-container argv, image ID
  and successful removal. Both backends record merged bounded stdout/stderr
  and exit/stop information. Missing results indicate an interrupted or failed
  action, not evidence of completion.
- Start/end workspace manifests and a completed, failed, or interrupted status.
  Exception *types* are logged; provider exception bodies are deliberately omitted.

Before each request/tool side effect, its event is flushed and fsynced. There is
still an unavoidable interval between performing a side effect and recording its
result. A crash in that interval leaves an attempted action with an unknown result.
SIGKILL/power loss can also leave a container running. Check
`docker ps -a --filter name=ctf-agent-` after an abruptly terminated process.

## Repeat the work safely

Three different things matter: reading the original transcript, repeating the
same tool steps, and asking a model to solve the task again. Only the first is
guaranteed to show the same recorded output. The CLI intentionally does not
automatically execute a saved transcript against a target.

To make a separate copy of the original starting point:

```sh
# Replace RUN with the actual bundle path. Keep this directory as evidence.
RUN=/absolute/path/to/runs/<timestamp-id>
REPEAT=/absolute/path/to/a-new-empty-directory
mkdir -p "$REPEAT"
cp -R "$RUN/source/." "$REPEAT/"
cp "$RUN/policy.toml" "$REPEAT/config.toml"
cp "$RUN/system_prompt.md" "$REPEAT/system_prompt.md"
mkdir "$REPEAT/workspace"
cp -R "$RUN/before/." "$REPEAT/workspace/"
```

In the copied policy, set `agent.workspace = "workspace"`,
`agent.prompt_file = "system_prompt.md"`, and `audit.directory = "runs"` so it
uses the new directory. Supply credentials separately; they are not in the bundle.
Inspect `before.json` for skipped files/directories and recorded file modes.
Snapshots exclude denied paths, symlinks, special files, multiply linked files,
and nested filesystems; they do not claim to preserve those objects.

For **native execution**, use `command_started` events to identify the resolved
host executable, its SHA-256 hash, runtime read paths and sandbox invocation.
The bundle records Python/platform versions and includes the native baseline
policy and helper source. Keep compatible local interpreter/tool installations
and dependencies; their binaries/libraries are not copied into the bundle.
Absolute paths, OS behavior and versions can differ on another machine. Review
and adjust runtime paths in the restored policy. Scratch paths in a recorded
invocation are temporary; let the runner create fresh ones instead of blindly
executing a saved launcher command.

For **Docker execution**, check `command_started` events for exact image IDs. If multiple IDs were
used, reproduce each step with its recorded image. Preserve a relevant image
before pruning Docker, for example:

```sh
# Substitute the actual sha256:... value from an event.
docker image save --output tool-image.tar sha256:<recorded-image-id>
# Later: docker image load --input tool-image.tar
```

An image ID describes the image actually used, even if its tag later changes.
The bundle does not contain image layers, the host kernel, external services,
or VPN configuration. Rebuilding the saved Dockerfile later may fetch different
base images/packages. For repeatability, retain the image and pin base digests.

Use the restored source/configuration and images to repeat reviewed commands in
the same sandbox policy. `--replay` shows the exact tool arguments; use the JSONL
for resolved command argv and intermediate results. Scripts written by `write_file`
are saved verbatim in those events and are usually present in `after/`; if a
script was subsequently overwritten, recover its earlier contents from its event.
The before/after hashes let you compare saved files. There is no automatic restore
or execution of untrusted log content.

To ask the model to attempt the task again after restoring and reviewing it:

```sh
cd "$REPEAT"
uv sync --locked
uv run ctf-agent "$(cat "$RUN/prompt.txt")"
```

This makes **new model calls and new tool actions**. Models, free-route choices,
network responses, target state, timestamps and random values can differ. Reset
the lab target if appropriate, and confirm its current IP/authorization before
repeating network actions. A transcript is evidence of the original run, not a
promise of deterministic model or target replay.

## Limits, integrity, and sensitive data

`audit.max_snapshot_bytes` bounds each snapshot (10 MB by default); snapshots
also stop at 10,000 entries. An oversized initial snapshot prevents the model
from starting. `audit.max_log_bytes` bounds the JSONL stream (20 MB by default).
These limits stop the run rather than silently discarding audit data. An audit
failure produces a nonzero CLI exit; `summary.json` may say `audit_failed` or
`initialization_failed`, or the log may have no end event. Partial bundles remain
for investigation. Command output is still bounded by `limits.max_output_bytes`;
full process output beyond that limit is not recoverable from the log.

Run directories use mode 0700 and event/policy/prompt files use 0600. Snapshot
files retain ordinary permission bits inside that private directory. Default
`runs/`, `captures/`, and `workspace/` are ignored by Git. Custom locations need
their own ignore and retention rules; there is no automatic deletion of evidence.

No `.env` or SDK credentials are copied automatically. However, prompts, scripts,
tool arguments/results, policies, snapshots and HTTP captures can themselves
contain passwords, tokens, flags, personal data, or secrets you supplied. Logs
are deliberately faithful rather than automatically redacted. Review and redact
a *copy* before sharing it; editing the JSONL invalidates its original checksums.

Hash chaining detects accidental edits, missing/reordered events, and truncation
of a completed transcript. It is not an authenticated, immutable audit service:
someone who can rewrite the bundle can recompute its chain. Snapshot/source
hashes are recorded for comparison; the viewer checks the event stream, not all
artifact file contents. Preserve an externally signed/checksummed copy or use
controlled storage if stronger evidence handling is needed.
