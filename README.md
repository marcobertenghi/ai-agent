# CTF agent

A small Python CLI for CTFs and authorized penetration testing. Give it a task,
challenge files, and an explicit tool policy. It works through a bounded
model/tool loop and leaves a local audit bundle you can inspect afterwards.

The project is designed to be read, modified, and extended. There are four tools:
list files, read files, write files, and run named commands in Docker. The two
runtime dependencies are the OpenAI-compatible SDK and python-dotenv.

## Install and run

You need Linux or macOS, Python 3.13+, [uv](https://docs.astral.sh/uv/), and a local
[Docker Engine/Desktop](https://docs.docker.com/engine/install/) with Engine 25+
for command execution.

From the project directory:

```sh
uv sync --locked
# If .env doesn't already exist, copy the example and set your API key:
cp -n .env.example .env
# Edit .env: OPENROUTER_API_KEY=your-key

docker build -t ctf-agent:local sandbox
uv run ctf-agent --check
uv run ctf-agent "Write solve.py to decode SGVsbG8=, run it, and report the result."
```

The CLI creates `workspace/` for task files and `runs/<timestamp-id>/` for its
audit record. The final answer goes to stdout; the audit path and optional
`--verbose` progress go to stderr. `uv run main.py "your task"` also works.

The default image has Python and the network is disabled. For a downloaded
challenge, put the files in `workspace/` and give the agent a concrete task:

```sh
mkdir -p workspace
cp /path/to/challenge.txt workspace/
uv run ctf-agent "Analyze challenge.txt, save a solver, run it, and explain the evidence."
```

For a network target, follow [the TryHackMe walkthrough](docs/tryhackme.md). It
covers the VPN, a curl/Nmap image, connectivity checks, configuration, a first
scoped run, and common failures. Changing only the prompt does not enable the
network or install tools.

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
changes. Command events include resolved argv and the Docker image ID. See
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
| `execution` | Trusted image, named command argv, networking, writable mount, resource limits |
| `limits` | Model turns, total calls, completion tokens, file/output sizes, listing size |
| `audit` | Run directory, byte budget per snapshot, event log byte budget |

The default provider is OpenRouter. Select a model that supports Chat Completions
function calling; provider availability and free routing can vary. A failed or
truncated response stops the run, and API requests have no automatic retries.

For file-only analysis without Docker, remove `run_command` from `tools.allow`.
For read-only analysis, also remove `write_file` and keep `execution.writable = false`.
Unknown keys/tools and unsafe configuration fail at startup. `--check` validates
the policy offline without creating files; it does not check Docker or connectivity.

Exit codes: 0 for a final model answer, 1 for failure/exhausted limits, 130 for
Ctrl-C. A successful exit is not proof that a challenge was solved.

## Extend it

To add a command, install the binary in a Dockerfile, rebuild the image, and
add its absolute in-container executable to `[execution.commands]`:

```toml
sha256 = ["/usr/bin/sha256sum"]
```

The agent can then use `run_command` with
`{"command":"sha256","args":["challenge.bin"]}`. Arguments are appended to
that argv; they are never interpreted by a host shell.

To add a Python tool, register its schema and handler in `aiagent/tools.py`, add
its name to `TOOL_NAMES` in `aiagent/config.py`, and opt it in with `tools.allow`.
The validator supports strings and string arrays with optional enums. Extend it
deliberately for other argument shapes. Reuse `Workspace` for files and the
sandbox runner for execution.

[Optional mitmproxy capture](docs/mitmproxy.md) records HTTP requests and responses
when you need traffic-level evidence. It stays outside the core dependencies.

## Security and development

Tools and commands use default-deny allowlists. File tools reject path traversal,
symlinks, multiple hard links, and special files. Commands require Docker, with
no host fallback; network access and command writes are disabled by default.
The container root is read-only, capabilities are dropped, and resource limits
apply. See [the security model](docs/security.md) for the trust assumptions,
network limitations, and why an allowlisted Python interpreter is still
arbitrary code inside the container.

The code is split by responsibility: `cli.py`, `agent.py`, `config.py`, `tools.py`,
`workspace.py`, `sandbox.py`, and `audit.py`. The original `calculator/` remains
an optional local example.

```sh
# Offline suite; no extra test dependency or API key.
uv run --locked python -m unittest discover -s tests -v

# After building the image, opt in to tests against your real Docker runtime.
CTF_AGENT_DOCKER_TESTS=1 uv run --locked python -m unittest tests.test_sandbox.DockerIntegrationTests -v
```

Tests do not contact a live model. Run the Docker checks on your own runtime;
mocked control-plane tests do not establish kernel isolation.
