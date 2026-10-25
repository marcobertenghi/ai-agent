# Work on a TryHackMe room

The agent runs on your Mac or Linux machine/VM. You provide the active room IP,
authorized scope and room text/files. It does not log into TryHackMe, start room
machines, or download VPN credentials.

## 1. Connect the host to the room

Start the target and copy its current IP. Connect the **host running this CLI**
to TryHackMe with its OpenVPN profile, using your VPN application or, on Linux:

```sh
sudo openvpn --config /absolute/path/to/your-profile.ovpn
```

Keep the VPN outside the agent workspace and sandbox. Targets are on TryHackMe's
private network; the VPN or AttackBox provides access. See the
[official network guide](https://help.tryhackme.com/en/articles/8991552-networks-explained-vpn-attackbox-and-security-tips)
and [OpenVPN walkthrough](https://tryhackme.com/room/openvpn).
A dedicated Linux lab VM is useful for separating the lab from personal data;
native execution itself does not require a VM on macOS. An AttackBox can run
the agent if it supports the required Python version and bubblewrap/user namespaces.

In a second terminal, verify a service the room says should be listening:

```sh
# Replace this example with the active room machine's real IP.
TARGET_IP=10.10.10.10
curl --connect-timeout 5 --max-time 10 -I "http://$TARGET_IP/"
```

Port 80 is an example; use the actual service/port. An HTTP error response still
establishes connectivity. A timeout does not. Do not put `.ovpn`, API keys or
private keys in `workspace/`.

## 2. Install local tools

Follow the main README to install the Python project and set `.env`.
On Debian/Ubuntu:

```sh
sudo apt-get install bubblewrap curl nmap
```

On macOS, Seatbelt and `/usr/bin/curl` are built in. If needed, install Nmap
with `brew install nmap`. Find the tool paths:

```sh
command -v curl
command -v nmap
```

Tools run as a regular user. Use Nmap TCP connect mode (`-sT`) without raw-packet
privileges. The native runner does not grant sudo/root or packet capabilities.

## 3. Make a task policy

```sh
cp config.toml config.local.tryhackme.toml
```

In the copy's **existing** `[execution]` table, set:

```toml
backend = "native"
network = "outbound"
timeout_seconds = 90
writable = false
```

Keep the Python recipe and add these to the existing `[execution.commands]`
table, using the actual absolute paths reported above. On Linux:

```toml
curl = ["/usr/bin/curl", "--connect-timeout", "5", "--max-time", "30"]
nmap = ["/usr/bin/nmap", "-sT", "-Pn", "-n"]
```

On Apple Silicon Homebrew, Nmap is usually `/opt/homebrew/bin/nmap`. Replace the
Nmap recipe path accordingly. Its libraries/data also need read access. In the
existing `[execution]` table, a practical Homebrew runtime allowance is:

```toml
read_paths = ["/opt/homebrew/Cellar"]
```

That grants read-only access to installed Homebrew packages. For a tighter policy,
list just Nmap's version directory and its required dependency/data directories;
`otool -L /opt/homebrew/bin/nmap` lists its linked libraries. Intel Homebrew or
other package managers use different paths. Never solve a missing-library error
by granting read access to your home or the whole project. Some tools may need
runtime allowances beyond this minimal profile; inspect their failures and add
only trusted library/data paths, or use the [optional Docker image](docker.md).

Outbound mode uses the host/VPN routes. It permits broad IP networking, including
local services, and is **not an enforced target allowlist**. Apply lab/network
firewall rules if scope must be enforced. Python can make requests too, so command
recipe names are not a network boundary. See [security](security.md).

## 4. Start with a bounded task

```sh
uv run ctf-agent --config config.local.tryhackme.toml --check
mkdir -p workspace
uv run ctf-agent --config config.local.tryhackme.toml --verbose \
  "I am authorized to assess the active room machine at $TARGET_IP only. First use curl to check its HTTP service and Nmap TCP connect scanning for ports 80 and 443 only. Summarize evidence, save findings.md, and suggest the next step. Do not attempt exploitation or scan other hosts in this run."
```

`--check` validates policy without contacting a model. The normal run then tests
the real native sandbox before its first model call. A sandbox startup failure
stops the run; it never switches to direct execution.

Start narrowly so you can check evidence before expanding scope. Include any
virtual-host names and room-specific constraints. Each invocation starts a new
conversation while workspace files persist; ask it to read `findings.md` when
continuing. This CLI does not automatically complete rooms or submit flags.

Use relative workspace paths. Commands see that directory read-only by default;
the model can save bounded evidence through `write_file`. Prefer command stdout
over Nmap/curl options that write workspace files. Split large tasks or adjust
`limits.max_output_bytes` when evidence is truncated. A higher command timeout
does not increase the model turn/tool-call budgets.

## 5. Review the record

The CLI prints the run path:

```sh
uv run ctf-agent --replay runs/<the-actual-run-id>
```

Inspect the commands, executable hashes/sandbox policy, results, model conclusions,
file changes and status. [Auditing](auditing.md) explains restoring the starting
workspace. Native runs use host-installed tools; retain their versions/dependencies
when reproducing work. [Docker](docker.md) remains available for image-based
reproducibility and different resource controls. Update target scope after a room
reset or IP change before another live run.

## Current limits

- Network-denied mode is the default. Outbound mode grants broad networking;
  an IP in the prompt alone does not constrain it.
- Native commands are short-lived and cleaned up. Long-lived listeners, detached
  jobs, interactive shells, and reverse-shell workflows are not supported by
  the default lifecycle. macOS denies listener binding; Linux outbound mode
  shares host networking, but jobs still end with the command.
- No raw-packet privileges. Some tools or spawn mechanisms fail under the policy.
- HTTP evidence is limited to what tools print. Use [optional mitmproxy capture](mitmproxy.md)
  for request/response records, with proxy-only enforcement supplied by external rules.
