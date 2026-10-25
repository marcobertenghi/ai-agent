# Work on a TryHackMe room

The agent runs on your machine or VM. It does not log into TryHackMe, start room
machines, download VPN credentials, or automatically know the room's target IP.
You provide the active target, permitted actions, and relevant room text/files.

## 1. Get the target reachable

Start the room's target machine and copy its current IP. Use your own Linux VM
with Docker and the room's OpenVPN connection for the most direct setup. Keep
the VPN client on that VM, outside the agent container. Start it manually using
your downloaded profile, for example:

```sh
sudo openvpn --config /absolute/path/to/your-profile.ovpn
```

Leave that terminal running. TryHackMe's targets are on its private network;
access is provided through OpenVPN or an already connected AttackBox. See the
[official network guide](https://help.tryhackme.com/en/articles/8991552-networks-explained-vpn-attackbox-and-security-tips)
and [OpenVPN walkthrough](https://tryhackme.com/room/openvpn).
An AttackBox is also useful for manual work, but do not assume it provides the
Docker runtime required by this project. Use the agent there only if that
environment supports the required Docker setup.

In another VM terminal, verify a service the room says should be listening:

```sh
# Replace this example with the active room machine's actual IP.
TARGET_IP=10.10.10.10
curl --connect-timeout 5 --max-time 10 -I "http://$TARGET_IP/"
```

Port 80 is only an example. Use the room's documented port/protocol if different.
An HTTP error response still establishes connectivity; a timeout does not.
Do not put your VPN profile or private keys in `workspace/`.

## 2. Build the optional network image

From the project directory, after installing the project and setting `.env` as
shown in the main README:

```sh
docker build -t ctf-agent:local sandbox
docker build -f sandbox/network.Dockerfile -t ctf-agent:network sandbox
```

This adds curl, Nmap, and CA certificates. The default image stays Python-only.
There is no need to install an entire penetration-testing distribution in it.

## 3. Enable the tools and network explicitly

```sh
cp config.toml config.local.tryhackme.toml
```

Edit this copy, keeping the rest of the configuration. Replace these settings
in the existing `[execution]` table:

```toml
image = "ctf-agent:network"
network = "bridge"
timeout_seconds = 90
writable = false
```

Add these recipes to the existing `[execution.commands]` table:

```toml
curl = ["/usr/bin/curl", "--connect-timeout", "5", "--max-time", "30"]
nmap = ["/usr/bin/nmap", "-sT", "-Pn", "-n"]
```

[`-sT` uses TCP connect scans](https://nmap.org/book/scan-methods-connect-scan.html),
which work without raw-packet privileges. No added
capabilities, privileged container, or host network is needed. Other Nmap modes
may require permissions the sandbox intentionally does not grant.

`network = "bridge"` permits outbound connections broadly; the IP in a prompt
is task scope, **not an enforced network allowlist**. If you need enforced scope,
apply egress rules on the lab VM/network. Keeping Python enabled also permits
other network clients written in Python. Removing a curl recipe does not stop
an interpreter from making HTTP requests.

## 4. Verify connectivity from Docker too

Before asking a model to work, check the same service from the network image:

```sh
docker run --rm --network bridge --cap-drop=ALL --read-only \
  ctf-agent:network /usr/bin/curl \
  --connect-timeout 5 --max-time 10 -I "http://$TARGET_IP/"
uv run ctf-agent --config config.local.tryhackme.toml --check
```

The image runs as a non-root user. This diagnostic has no host workspace or
credential mounts. If host curl works but this fails, investigate VPN forwarding,
firewall/NAT rules and Docker subnet overlap on your VM. Avoid choosing a Docker
subnet that overlaps a lab/VPN route. Docker Desktop on macOS adds a VM network
layer, so host VPN reachability may not be available from containers; running
Docker and OpenVPN together inside a Linux VM is the suggested troubleshooting
baseline. Do not work around routing by giving the agent host networking or
mounting the Docker socket/VPN credentials.

## 5. Give it a bounded first task

```sh
mkdir -p workspace
# Optionally copy challenge files or room notes into workspace/.
uv run ctf-agent --config config.local.tryhackme.toml --verbose \
  "I am authorized to assess the active room machine at $TARGET_IP only. First use curl to check its HTTP service and Nmap TCP connect scanning for ports 80 and 443 only. Summarize evidence, save findings.md, and suggest the next step. Do not attempt exploitation or scan other hosts in this run."
```

Begin with a narrow task so you can check the evidence and connectivity before
expanding the scope. Include room-specific constraints and known virtual-host
names in subsequent prompts. A new CLI invocation starts a new conversation;
saved workspace files persist, so instruct it to read `findings.md` if relevant.
This tool does not automatically complete every room or submit flags.

Commands see `workspace/` as `/workspace`, read-only by default. Nmap/curl output
is returned to the model; it can save the relevant evidence with `write_file`.
Use stdout rather than command options that write into `/workspace`. Increase
`limits.max_output_bytes` for evidence that otherwise gets truncated, or split
large tasks. Increasing only the command timeout does not increase turn/call limits.

## 6. Review the record

The CLI prints a path such as `runs/20261025T120000Z-a1b2c3d4e5f6`:

```sh
uv run ctf-agent --replay runs/<the-actual-run-id>
```

Check the attempted commands, returned evidence, model conclusions, file
changes and completion status. See [auditing](auditing.md) for restoring the
starting workspace and retaining the exact tool image. Room machines can reset
or receive new IPs; update the scope before another live run.

## Current limits

- No long-lived sessions or published container ports: reverse-shell callbacks,
  inbound listeners and interactive shells are outside this workflow.
- No raw-packet capabilities or privileged execution. Choose unprivileged modes.
- No browser automation or TryHackMe account integration. Provide task context.
- HTTP traffic is only represented by what the tool prints. Use
  [optional mitmproxy capture](mitmproxy.md) for full HTTP exchanges.
