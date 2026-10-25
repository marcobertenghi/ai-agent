# Security model


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
model provider. Use data you are allowed to send there. Turn/token
limits bound the loop; they are not an exact monetary budget or disk quota.


Audit bundles are stored outside the workspace and are never mounted into commands.
See [auditing](auditing.md) for log sensitivity and integrity limitations.

References: [Docker runtime options](https://docs.docker.com/reference/cli/docker/container/run/)
and [bind-mount isolation](https://docs.docker.com/engine/storage/bind-mounts/).
