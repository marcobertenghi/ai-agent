# Classroom CTF agent

A small Python CLI for CTFs and authorized security labs. It can list, read, and
write workspace files, and run explicitly configured commands in Docker. There
is no shell on the host, interactive editor, memory service, plugin framework,
or background agent. The runtime dependencies are the OpenAI-compatible SDK and
python-dotenv; configuration and tests use the Python standard library.

## Quick start

Use Linux or macOS, Python 3.13+, and [uv](https://docs.astral.sh/uv/).
Docker Engine/Desktop is required only for command execution. Use a current
local Docker runtime with support for `bind-recursive=disabled` (Engine 25+).

```sh
uv sync --locked
cp .env.example .env
# Edit .env to set OPENROUTER_API_KEY. Keep it outside workspace/.

docker build -t ctf-agent:local sandbox
uv run ctf-agent --check
uv run ctf-agent "Create a Python script that decodes SGVsbG8= and run it."
```

The agent creates `workspace/` on its first run. Put challenge files there.
`uv run main.py "your task"` also works. `--verbose` prints turn counts, token
usage when available, and tool names to stderr; the final answer goes to stdout.
Exit codes: 0 for a final model answer, 1 for errors or exhausted limits, 130 for
Ctrl-C. A final answer is not proof that a challenge was solved; inspect evidence.

For a first exercise with the original calculator sample:

```sh
mkdir -p workspace
cp -R calculator workspace/calculator
uv run ctf-agent "Inspect calculator/, run its tests, and explain any failures."
```

No Docker available? Remove `run_command` from `tools.allow` in `config.toml`.
File tools still work. An attempted command without Docker returns an error;
there is no host fallback. `--check` validates configuration offline without a
key, creating files, contacting the model, or checking Docker.

## Configuration students can change

Edit [config.toml](config.toml) and [system_prompt.md](system_prompt.md).
Paths in the configuration resolve relative to the configuration file. Select
a separate policy with `--config /path/to/config.toml`; its prompt and `.env`
live next to that file. Exported environment variables override `.env` values.
The workspace must not contain the configuration, prompt, `.env`, or agent code.

| Setting | Purpose |
| --- | --- |
| `agent.workspace` | Dedicated challenge directory, separate from this repository |
| `agent.prompt_file` | Editable system instructions; these are guidance, not a security boundary |
| `provider.*` | HTTPS OpenAI-compatible endpoint, model, key environment variable, request timeout |
| `limits.*` | Turns, total tool calls, completion tokens per request, file bytes, output bytes, directory entries |
| `tools.allow` | Exact tool names; all others are denied |
| `tools.deny_paths` | Additional case-insensitive path globs for sensitive files/directories |
| `execution.commands` | Named fixed executable/argument prefixes inside the container |
| `execution.image` | Explicitly built, trusted tool image; commands never pull automatically |
| `execution.network` | `none` by default; `bridge` enables outbound network access |
| `execution.writable` | Whether commands may change workspace files; default `false` |
| Other `execution.*` | Command timeout, memory, CPU and process limits |

The existing OpenRouter endpoint/model are retained. Choose a model that supports
Chat Completions function calling; free routing availability and tool support
depend on the provider. Requests have a timeout and no automatic retries. This
project uses the SDK's Chat Completions interface, not every provider/model API.

Example file-only policy:

```toml
# Replace the existing [tools] table, retaining the other tables.
[tools]
allow = ["get_files_info", "get_file_content"]
deny_paths = [".env", ".env.*", ".git", ".ssh", "*.pem", "*.key"]
```

Invalid types, unknown keys, unknown tools, and unsafe workspace placement fail
at startup. An empty allowlist is valid and gives the model no tools. Configuration
files are trusted operator input; do not accept policy files from a challenge.

## Security model

Use **allowlists for tools and commands**, with **deny patterns as additional
protection for files**. A blacklist of dangerous shell words cannot safely
constrain arbitrary code. Policy is checked in Python on every dispatch, even
when the model invents an unadvertised tool or unexpected arguments.

Host file tools:

- Accept relative workspace paths, reject `..`, absolute paths, NULs, and backslashes.
- Open each directory relative to a directory descriptor using `O_NOFOLLOW`;
  reject all symlinks, including internal and dangling links.
- Read/write regular files with exactly one hard link; reject devices, sockets,
  pipes, and multiply linked files. Writes validate the opened file before truncation.
- Match denied globs against the entire relative path, its ancestors, and each
  component, after Unicode normalization and case folding. For example `.git`
  also blocks `.GIT`, and `private/*` blocks its descendants. This avoids case
  aliases bypassing policy on macOS filesystems. Listings hide denied names.
- Bound file sizes, directory listings, argument sizes, and returned output.

Commands:

- Run in a fresh Docker container with only the dedicated workspace bind-mounted,
  at `/workspace`; nested mounts are excluded. The repository, `.env`, home
  directory, Docker socket, and API credentials are not mounted or forwarded.
- Use a non-root UID, no Linux capabilities, no new privileges, the runtime's
  default seccomp policy, and a read-only container root. Workspace mounts are
  read-only by default; `/tmp` is bounded temporary storage.
- Have no network by default, and have memory, CPU, process, time, and output limits.
  Container logging is disabled so command output cannot grow Docker log files.
- Append model arguments after the configured executable inside the container;
  no model input becomes a Docker option or host shell command.
- Refuse a workspace containing denied files, links, special files, nested
  filesystems, or more than 10,000 entries before mounting it. This prevents
  commands from bypassing file deny rules through the mount.
- Create the container before starting it, then remove it after completion,
  output/time limits, errors, or Ctrl-C. A cleanup failure stops the agent and
  prints the exact removal command. Power loss/SIGKILL cannot run cleanup; check
  `docker ps -a --filter name=ctf-agent-` after an interrupted host process.

“Workspace confinement” means **host file access is confined to the configured
workspace**. Container programs can read the container's Python installation,
libraries, `/proc`, and other runtime files; this is necessary to run tools. A
working directory alone is not a sandbox. Docker supplies the execution boundary;
containers still share a kernel with their Docker host. Use a disposable VM for
hostile challenge binaries or higher assurance, and keep Docker patched.

The host OS, local Docker daemon, image, configuration, and operator are trusted.
Do not run the agent as root or modify/mount things into its workspace concurrently
from another host process. These checks are not a defense against an attacker
already controlling your user account or Docker daemon. Keep secrets out of the
workspace even when deny patterns exist. Files/tools are untrusted model context,
and the system prompt cannot guarantee resistance to prompt injection.

An allowlisted interpreter can execute arbitrary in-container code, launch other
installed programs, and read workspace files regardless of which file tools are
advertised. For strict file-only access, remove `run_command`. Enabling
`execution.writable` also requires `write_file` in the allowlist, but then commands
can modify/delete workspace data directly; deny globs cannot restrict names they
create. There is no bind-mount disk quota. Use disposable data and VM/filesystem
quotas for writable execution.

`network = "bridge"` allows general outbound traffic, including possibly the
local network/Docker host. It does **not** enforce target allowlists. For network
labs, use a dedicated VM and network-level egress rules for authorized targets;
the CLI deliberately has no arbitrary Docker flags, host network, or privileged
mode. Some raw-packet tools will not work with the default restrictions.

Prompts, file contents returned by tools, and command output go to the configured
model provider. Use classroom data you are allowed to send there. Turn/token
limits bound the loop; they are not an exact monetary budget or disk quota.

## Extend it without a framework

To add a command, install its binary in `sandbox/Dockerfile`, rebuild the image,
and add a named argv prefix under `[execution.commands]`. For example, a tool
that prints a digest needs only:

```toml
sha256 = ["/usr/bin/sha256sum"]
```

The model can call `run_command` with
`{"command": "sha256", "args": ["challenge.bin"]}`. Verify the executable exists
in your image. Fixed argv prefixes may repeat options. To allow only digests,
remove the Python recipe: keeping Python permits general-purpose execution.

To add a Python tool, add a `Tool` entry in `aiagent/tools.py` with its description,
JSON properties, required argument names, and handler; add its name to
`TOOL_NAMES` in `aiagent/config.py`, then opt it in with `tools.allow`. The small
validator supports string and string-array arguments with optional enums; extend
validation deliberately if another shape is needed. Reuse `Workspace` for host
file access and `run_command` for execution. Never introduce host `eval`, shell
execution, or `subprocess` in a new model-callable handler.

## Code map and checks

| File | Responsibility |
| --- | --- |
| `aiagent/cli.py` | Arguments, explicit `.env` loading, client setup, exit codes |
| `aiagent/agent.py` | Bounded model/tool loop; sequential tool dispatch |
| `aiagent/config.py` | Validated configuration and operator policy |
| `aiagent/tools.py` | Tool schemas, runtime argument validation, allowlist dispatch |
| `aiagent/workspace.py` | Confined host file operations |
| `aiagent/sandbox.py` | Docker lifecycle and bounded output capture |
| `tests/` | File boundary, policy, sandbox, and fake-provider regression tests |
| `calculator/` | Original sample application, retained as an optional exercise |

```sh
# Offline tests, no API key, Docker, or additional test dependencies required.
uv run --locked python -m unittest discover -s tests -v

# Real Docker tests, explicitly opt in after building the image.
CTF_AGENT_DOCKER_TESTS=1 uv run --locked python -m unittest tests.test_sandbox.DockerIntegrationTests -v
```

The Docker checks exercise read-only mounts, host-file exclusion, a non-root user,
absent credentials/socket, disabled networking, and cleanup after a timeout. Run
them on the classroom runtime before distributing. Unit tests mock the Docker
control plane; they do not prove kernel isolation. No test contacts a model or
uses a real API key.

Implementation references: [Docker runtime options](https://docs.docker.com/reference/cli/docker/container/run/),
[bind-mount isolation options](https://docs.docker.com/engine/storage/bind-mounts/),
and [function-calling protocol](https://developers.openai.com/api/docs/guides/function-calling).
