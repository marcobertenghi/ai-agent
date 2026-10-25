# Security model

The operator policy, agent code, host OS, installed runtimes and operator account
are trusted. The model, challenge files and command output are untrusted. Tools
and named command recipes are default-deny allowlists, enforced on every dispatch.
Configuration, credentials, system prompts, code and audit records live outside
the dedicated workspace. Native execution requires a non-root account.

## Safe write roots

The workspace is a write authority for `write_file` even when commands are
read-only. Startup therefore rejects workspaces and audit directories overlapping
system/executable directories, host `$PATH` entries, the active Python runtime,
or known shell startup paths. Shell checks include bash/profile files, zsh files
under `ZDOTDIR`, fish configuration, and `BASH_ENV`/`ENV` hooks. Symlinks and
filesystem case aliases are resolved. A whole home or shared temporary root is
also rejected. Native scratch roots receive the same validation before launching
untrusted code.

Use a dedicated task directory and build outputs inside it. Temporary writes go
to a private per-command directory, not a blanket grant to shared `/tmp`.
Application-specific autoload paths and hooks are not universally discoverable;
the operator must keep those and other trusted executables outside task data.
See [the README write-permission table](../README.md#write-permissions) and
`tests/test_write_policy.py` for the two real write-boundary tests. Their unsafe
targets are user-writable test canaries, never real host profiles/system files.

## Host file tools

File tools accept relative workspace paths and reject `..`, absolute paths, NULs
and backslashes. Each directory is opened relative to a directory descriptor
with `O_NOFOLLOW`, rather than validating a string and following links later.
Only regular files with exactly one hard link may be read/written; writes check
the opened file before truncation. Symlinks, devices, pipes and sockets are blocked.

Deny patterns match full paths, ancestors and components after Unicode
normalization/case folding. Listings hide denied names. File bytes, listing
entries, tool arguments and returned output are bounded.

## Native execution

`execution.backend = "native"` selects built-in Seatbelt on macOS and bubblewrap
on Linux. There is no daemon, VM, Node.js or socat dependency. Linux requires
bubblewrap and a kernel/security policy that permits its user namespaces.
Missing support fails closed; the program never retries a command unsandboxed.
A fixed, trusted startup probe compiles/applies the real sandbox before any
model request, and its execution is audited.

Both platforms:

- Start with networking disabled and a read-only workspace.
- Grant read access only to the workspace and trusted runtime files, plus a
  private per-command scratch directory. The active Python runtime/virtualenv
  is included automatically. The helper is launched with `-I -S`.
- Build the child environment from a small fixed set. API keys, proxy settings,
  SSH agents, dynamic-loader variables and other parent variables are not inherited.
  The child's HOME and temporary directory point at private scratch storage.
- Pass argv directly, close inherited descriptors, use a separate process group,
  bound output/time, and remove scratch files after cleanup. No model-controlled
  text becomes a launcher option or host shell program.
- Refuse workspaces containing denied files, links, special files, nested
  filesystems, or more than 10,000 entries before execution. This blocks command
  access to data that file deny rules would hide. Do not modify the workspace
  concurrently from another host process.

On **macOS**, the Seatbelt profile starts with `(deny default)`. File contents
and directory listings are granted to read roots; writes are granted only to
scratch and, if explicitly enabled, the workspace. Path metadata is readable
for interpreter/symlink resolution, and names directly under `/` are visible.
This is not a chroot and does not conceal the existence/metadata of every host
file. Host-file contents outside the read allowances remain blocked.

The profile permits ordinary fork/exec child processes, with the same kernel
restrictions inherited by descendants. It denies `setsid`, `setpgid`, and the
`posix_spawn` syscall, whose flags otherwise provide a second detachment route.
Process-group cleanup runs on completion, failure, timeout, output limits and
Ctrl-C. Software that requires `posix_spawn`, new sessions, GUI/Mach services,
interactive terminals or long-lived background jobs may fail under this policy.
The runner deliberately does not grant general Mach/XPC or Unix-socket access.

On **Linux**, bubblewrap starts with an empty root, read-only runtime binds, an
isolated PID/IPC/user/network namespace, a fresh `/proc` and `/dev`, and no Linux
capabilities. It uses `--new-session`, `--die-with-parent`, and a read-only root;
only the workspace (when opted in) and scratch/tmp mounts are writable. The host
home, `/run`, Docker socket and host `/proc` are not exposed. User-namespace or
AppArmor restrictions may stop bubblewrap from starting. Configure an appropriate
distribution policy or use the optional Docker backend; the agent does not
change system-wide security settings.

## Trusted runtime read paths

The baseline is in `aiagent/native_policy.toml`. `execution.read_paths` adds
specific tool/library/data paths. All remain read-only. Paths overlapping the
workspace or exposing the configured policy, prompt, `.env` or audit directory
are rejected. For other secrets, keep them out of every runtime allowance.
Never add `/`, a home directory, or a whole project directory. A custom runtime
allowance is access to *all readable content underneath it*, not just executables.
Direct sockets/devices and host process/device/IPC directories are rejected.
Keep sockets out of every other allowed runtime directory too: on Linux, a
read-only socket bind can still grant access to the service behind that socket.

A program needs its interpreter, libraries, certificates and tool data outside
the workspace. Those are explicit runtime exceptions to data confinement.
The agent is not restricted to only four executable binaries: an allowlisted
Python interpreter permits arbitrary code and other installed programs reachable
inside its sandbox. Removing a tool recipe does not prevent another interpreter
from implementing the same operation. For strict file-only access, remove
`run_command` from `tools.allow`.

## Networking and VPNs

Native `network = "none"` blocks network use. Native `network = "outbound"`
explicitly grants broad IP egress using the host/VPN network. On macOS it does
not grant inbound listeners or general Unix-socket access. Linux outbound mode
shares the host network namespace and is not an inbound-listener restriction.
Neither mode enforces target IP/domain/port allowlists. In outbound mode, local
services may be reachable too; an unsafe local service can expose additional
host authority. Use a dedicated lab environment and network-level rules for
enforced target scope. Do not treat a target address in the prompt as a firewall.

The lightweight implementation does not include a network broker. `socat` is a
relay, not a filesystem sandbox or an egress policy. An explicit HTTP proxy such
as [mitmproxy](mitmproxy.md) can record web traffic, but programs can bypass it
unless external network controls require proxy-only traffic. Raw-packet tools
may require privileges that these policies intentionally do not supply.

## Resource and lifecycle limits

| Limit | Native macOS | Native Linux | Optional Docker |
| --- | --- | --- | --- |
| Wall timeout and output bytes | Yes | Yes | Yes |
| CPU time | Per process, timeout seconds | Per process, timeout seconds | Aggregate CPU quota |
| Memory | No hard memory limit promised | Per-process address space via `memory_mb` | Aggregate memory/swap limit |
| Open files/core dumps | 256 descriptors / no core | 256 descriptors / no core | Descriptor/core limits |
| Native file size | 64 MiB per file | 64 MiB per file | No workspace file-size quota |
| `cpus` / `pids` settings | Not applied | Not applied | Applied |
| Aggregate process/disk budget | No | No | PID cap; workspace disk has no quota |

Per-process limits do not contain aggregate fork/memory/disk exhaustion.
Native execution shares the host kernel and uses the host's installed programs.
It is intended for controlled lab tooling; use a disposable VM and appropriate
quotas for hostile binaries or stronger resource containment. Docker is an
[optional, image-based backend](docker.md), with its own trust assumptions.
SIGKILL/power loss cannot run host cleanup; a native process group may survive
an abruptly killed runner. There is no promise of background-job supervision
across host crashes.

## Model context and evidence

Prompts, returned file contents and tool output go to the configured provider.
Use data you may send there. System-prompt instructions do not guarantee prompt
injection resistance. Workspace writes are reversible only if you have suitable
backups; with `execution.writable = true`, commands can modify/delete task data
and create names matching deny patterns. Preflight checks cannot restrict files
a running command itself creates. Deny rules are additional protection, not a
place to store secrets safely.

Audit records remain outside the workspace and are not visible to commands.
See [auditing](auditing.md) for exact records, sensitive-data handling and
integrity limits. These restrictions do not defend against an attacker already
controlling your user account, trusted runtimes, configuration or Docker daemon.

References: [macOS Seatbelt concepts](https://chromium.googlesource.com/chromium/src/+/main/sandbox/mac/README.md),
[bubblewrap's security model](https://github.com/containers/bubblewrap),
[Docker runtime options](https://docs.docker.com/reference/cli/docker/container/run/).
