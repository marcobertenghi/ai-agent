# Optional Docker backend

The default native backend requires no Docker installation. Docker is useful
when you want an isolated tool image and aggregate memory/CPU/process controls.
On Linux it runs on the Linux kernel; on macOS a runtime such as Docker Desktop
or Colima supplies a Linux VM. Use a local, trusted daemon with Engine 25+.

```sh
docker build -t ctf-agent:local sandbox
cp config.toml config.local.docker.toml
```

In the copy, change `execution.backend = "docker"`. Keep `network = "none"`
initially, and replace the Python recipe with its absolute in-container path:

```toml
[execution.commands]
python = ["/usr/local/bin/python", "-E", "-s", "-B"]
```

All Docker recipes use in-image paths. `execution.read_paths` applies only to
native execution; it never adds Docker mounts. The workspace appears at
`/workspace`, with only that directory bind-mounted. Images are never pulled
automatically during execution and there is no host fallback.

```sh
uv run ctf-agent --config config.local.docker.toml "Write and run a Python decoder."
CTF_AGENT_DOCKER_TESTS=1 uv run --locked python -m unittest tests.test_sandbox.DockerIntegrationTests -v
```

For curl/Nmap, optionally build the network image:

```sh
docker build -f sandbox/network.Dockerfile -t ctf-agent:network sandbox
```

Set `image = "ctf-agent:network"`, `network = "bridge"`, and add absolute
in-image recipes for `/usr/bin/curl` and `/usr/bin/nmap` as needed. Bridge mode
allows broad outbound access, including possibly services on the Docker host;
it does not enforce target scope. Verify that containers can reach the VPN route
before working on a room. See [the TryHackMe guide](tryhackme.md).

The runner uses a non-root UID, no Linux capabilities, no new privileges, the
runtime's default seccomp policy, a read-only root, and a read-only workspace
mount by default. It excludes nested mounts and sets memory, CPU, PID, time,
output and temporary-storage limits. Container logging is disabled. Containers
are removed after completion, interruption, timeout or output limits. A cleanup
failure stops the run and prints the removal command. After a host crash/SIGKILL,
check `docker ps -a --filter name=ctf-agent-` for leftovers.

Docker also shares a kernel with its daemon host; it is not a substitute for a
disposable VM when running hostile binaries. See [security](security.md).
