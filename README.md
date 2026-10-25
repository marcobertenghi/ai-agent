# CTF agent

A small Python CLI for CTFs and authorized penetration testing. Give it a task,
challenge files, and an explicit tool policy. It works through a bounded
model/tool loop and leaves a local audit bundle you can inspect afterwards.

The project is designed to be read, modified, and extended. There are four tools:
list files, read files, write files, and run named commands in an OS sandbox. The two
runtime dependencies are the OpenAI-compatible SDK and python-dotenv.

## Install and run

You need Linux or macOS, Python 3.13+, and [uv](https://docs.astral.sh/uv/).
Native execution uses built-in Seatbelt on macOS and bubblewrap on Linux. It
requires no VM, daemon, Node.js, or socat. On Debian/Ubuntu, install bubblewrap
with `sudo apt-get install bubblewrap`. Other distributions provide the same
package. Docker is an [optional backend](docs/docker.md).

From the project directory:

```sh
uv sync --locked
# If .env doesn't already exist, copy the example and set your API key:
cp -n .env.example .env
# Edit .env: OPENROUTER_API_KEY=your-key

uv run ctf-agent --check
uv run ctf-agent "Write solve.py to decode SGVsbG8=, run it, and report the result."
```

The CLI creates `workspace/` for task files and `runs/<timestamp-id>/` for its
audit record. Organized progress is displayed by default on stderr; the final
answer goes to stdout. `uv run main.py "your task"` also works.

The default command uses the Python interpreter running the CLI. Networking is
disabled and commands cannot write workspace files by default. For a downloaded
challenge, put the files in `workspace/` and give the agent a concrete task:

```sh
mkdir -p workspace
cp /path/to/challenge.txt workspace/
uv run ctf-agent "Analyze challenge.txt, save a solver, run it, and explain the evidence."
```

For a network target, follow [the TryHackMe walkthrough](docs/tryhackme.md). It
covers the VPN, local curl/Nmap tools, connectivity checks, configuration, a first
scoped run, and common failures. Changing only the prompt does not enable the
network or install tools. A real sandbox startup probe runs before the first
model request; missing or unusable isolation stops the run with no host fallback.

## Follow a run

The console shows setup/sandbox checks, each model turn, token usage when
available, public agent updates, numbered tool calls, arguments, results, command
exit codes, timings, and the final run summary. A typical tool step looks like:

```text
Turn 1/20
  Requesting model response...
    Agent update:
      I'll inspect the challenge files before choosing a solver.
  Tool 1/40: 'get_files_info'
    Arguments:
      {
        "directory": "."
      }
    Tool returned in 0.01s
    Result:
      'challenge.txt': file, 42 bytes
```

Agent updates are concise, user-facing plans and conclusions supplied by the
model. Hidden provider reasoning is not displayed. If a response contains only
tool calls, the console reports the requested actions without inventing thoughts.

```sh
# Default: organized progress with compact previews.
uv run ctf-agent "Analyze challenge.txt and save a solver."

# Expanded argument/result previews, including generated file contents.
uv run ctf-agent --verbose "Analyze challenge.txt and save a solver."

# Successful runs print only the final answer; errors still go to stderr.
uv run ctf-agent --quiet "Analyze challenge.txt and save a solver."
```

`--verbose` and `--quiet` are mutually exclusive. Console previews are bounded
to keep scripts and long outputs readable; truncation is marked. The audit
bundle retains the recorded arguments/results subject to the existing audit
and tool-output limits. Even in quiet mode, audit recording and sandbox checks
remain enabled. Progress on stderr does not pollute redirected final answers:

```sh
uv run ctf-agent "Summarize challenge.txt" > answer.txt
```

## Review what happened

Every normal CLI run is audited automatically. Copy the printed run path into:

```sh
uv run ctf-agent --replay runs/<timestamp-id>
```

Replay prints the recorded task, assistant messages, tool arguments, results,
and final status. It works without an API key or configuration and **does not
execute commands or contact the target**.

A bundle includes `events.jsonl`, the policy and prompts, source and dependency
lockfile copies, before/after workspace snapshots, file hashes, and a summary of
changes. Native command events include resolved argv, executable hashes, runtime
read allowances, and the exact Seatbelt profile or bubblewrap invocation. Docker
events retain the image ID. See
[run auditing and reproduction](docs/auditing.md) for the layout, limits, and
how to repeat a run in a fresh directory. Logs may contain sensitive challenge
material; review them before sharing.

## Configure it

Edit [config.toml](config.toml) and [system_prompt.md](system_prompt.md). To keep
separate task settings, copy the policy to `config.local.toml` next to the original
and run `uv run ctf-agent --config config.local.toml "your task"`.

Paths resolve relative to the selected policy file. Its `.env` is loaded from
that same directory; exported environment variables take precedence. Keep
configuration, credentials, prompts, code, and audit records outside the workspace.

| Table | Controls |
| --- | --- |
| `agent` | Task workspace and system prompt file |
| `provider` | HTTPS endpoint, tool-capable model, key environment variable, timeout |
| `tools` | Exact tool allowlist and additional file deny patterns |
| `execution` | Backend, named command argv, runtime read paths, networking, workspace writes, resource limits |
| `limits` | Model turns, total calls, completion tokens, file/output sizes, listing size |
| `audit` | Run directory, byte budget per snapshot, event log byte budget |

The default provider is OpenRouter. Select a model that supports Chat Completions
function calling; provider availability and free routing can vary. A failed or
truncated response stops the run, and API requests have no automatic retries.

For file-only analysis without any command execution, remove `run_command` from `tools.allow`.
For read-only analysis, also remove `write_file` and keep `execution.writable = false`.
Unknown keys/tools and unsafe configuration fail at startup. `--check` validates
the policy offline without creating files; it does not execute the sandbox probe
or check connectivity. The normal CLI run probes native isolation before contacting a model.

Exit codes: 0 for a final model answer, 1 for failure/exhausted limits, 130 for
Ctrl-C. A successful exit is not proof that a challenge was solved.

## Write permissions

**Security warning:** broad write permissions can enable persistence, sandbox
escape, or privilege escalation when another program later loads modified files.
Keep write access limited to task data:

| Location | Write policy |
| --- | --- |
| `$PATH`/executable directories, such as `/usr/local/bin` | Never use as a workspace or write allowance |
| Host shell startup files, such as `~/.bashrc`, `~/.zshrc`, or a `ZDOTDIR`/`BASH_ENV` hook | Never grant writes |
| System directories, such as `/etc` | Never grant writes |
| Dedicated project task directory, such as `workspace/` | File tools may write here when `write_file` is allowlisted |
| Build outputs, such as `workspace/build/` | Stay inside the dedicated workspace |
| Private per-command temporary directory | Writable scratch, removed after execution |

Commands see the workspace **read-only by default**. Enable
`execution.writable = true` only when a command needs to create task/build files;
this also requires `write_file` in `tools.allow`. `execution.read_paths` grants
reads only and never authorizes writes to installed tools or libraries.

The CLI rejects workspace/audit locations that overlap system directories,
executable paths, the active Python runtime, or known host shell startup paths.
Symlinks, inode aliases, and conservatively folded case/Unicode paths are checked,
including directories that do not exist yet. Keep configuration, credentials,
agent code, and any other auto-loaded application files outside the workspace.
These checks cover common dangerous locations, not every application's possible
autoload mechanism.

Use a **private subdirectory** under the OS temporary location (`/tmp/...` on
Linux, or the OS-selected temporary location on macOS). Do not grant the whole
shared `/tmp` directory. The native runner already creates and validates its own
private scratch directory; no general write-path configuration is needed.
Allowed output is still untrusted: review it before executing it outside the sandbox.

The two write-boundary tests use temporary, parent-writable canaries representing
host executable directories, shell profiles, and system configuration. They
verify those writes remain blocked even with workspace writes enabled, then
verify workspace/build/private-temp writes succeed and scratch is cleaned up.
They never modify your real profiles, installed tools, or `/etc` files:

```sh
CTF_AGENT_NATIVE_TESTS=1 uv run --locked python -m unittest tests.test_write_policy.NativeWritePermissionTests -v
```

## Extend it

To add a command, install the binary locally and add its absolute executable to
`[execution.commands]`. For example, on Linux:

```toml
sha256 = ["/usr/bin/sha256sum"]
```

The agent can then use `run_command` with
`{"command":"sha256","args":["challenge.bin"]}`. Arguments are appended to
that argv; they are never interpreted by a host shell. The special native token
`python` selects this CLI's interpreter without a PATH lookup. Add narrowly scoped
library/data directories under `execution.read_paths` when a tool needs them.
The platform baseline lives in [aiagent/native_policy.toml](aiagent/native_policy.toml).
These are trusted runtime read allowances; keep credentials and project data out
of them. All runtime paths remain write-denied.

To add a Python tool, register its schema and handler in `aiagent/tools.py`, add
its name to `TOOL_NAMES` in `aiagent/config.py`, and opt it in with `tools.allow`.
The validator supports strings and string arrays with optional enums. Extend it
deliberately for other argument shapes. Reuse `Workspace` for files and the
sandbox runner for execution.

[Optional mitmproxy capture](docs/mitmproxy.md) records HTTP requests and responses
when you need traffic-level evidence. It stays outside the core dependencies.

## Security and development

Tools and commands use default-deny allowlists. File tools reject path traversal,
symlinks, multiple hard links, and special files. Native commands use a default-deny
Seatbelt policy or a fresh bubblewrap filesystem with read-only runtime mounts.
Provider credentials are stripped from the child environment. Network access and
command writes are disabled by default, with no unsandboxed fallback.

Native execution shares the host kernel and installed runtimes. It has a wall
timeout, bounded output and per-process limits, rather than Docker's aggregate
memory/process controls. See [the security model](docs/security.md) for exact
limits, background-process cleanup, and the consequences of enabling outbound
networking. Use Docker or a disposable VM for stronger resource containment.

The code is split by responsibility: `cli.py`, `agent.py`, `config.py`, `tools.py`,
`workspace.py`, `native.py`, `docker_sandbox.py`, `process.py`, `audit.py`, and
`display.py` for the console view.
`sandbox.py` selects the backend. The original `calculator/` remains
an optional local example.

```sh
# Offline suite; no extra test dependency or API key.
uv run --locked python -m unittest discover -s tests -v

# Real OS isolation tests; no API key, model calls, or external target required.
CTF_AGENT_NATIVE_TESTS=1 uv run --locked python -m unittest tests.test_native.NativeIntegrationTests -v

# Optional: after building the image, test Docker too.
CTF_AGENT_DOCKER_TESTS=1 uv run --locked python -m unittest tests.test_sandbox.DockerIntegrationTests -v
```

Tests do not contact a live model. Run the native checks on each OS/runtime you
intend to use; mocked launcher tests do not establish kernel isolation. Sandboxes
inside another sandbox may be blocked by the outer policy; the runner fails closed.
