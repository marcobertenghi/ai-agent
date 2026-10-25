# Optional HTTP evidence with mitmproxy

The core audit log records what the model and tools did. A proxy adds evidence
of HTTP requests and responses that a tool may not print. It is useful for web
CTFs, but it adds a service, certificate management and sensitive capture files,
so it is an optional companion rather than a core dependency.

[mitmproxy](https://docs.mitmproxy.org/stable/) can record HTTP conversations;
`mitmdump` is its command-line interface. It does not record raw TCP scans,
SSH sessions, or arbitrary application traffic. No transparent interception is
configured by this project.

## Explicit proxy with native execution

With the default native backend, start the proxy on the same machine as the CLI:

```sh
mkdir -m 700 -p captures
mitmdump --listen-host 127.0.0.1 --listen-port 8080 -w captures/web.flows
```

Keep `backend = "native"`, set `network = "outbound"`, and add this recipe,
using your actual local curl path:

```toml
curl_proxy = ["/usr/bin/curl", "--connect-timeout", "5", "--max-time", "30", "--proxy", "http://127.0.0.1:8080"]
```

The native process reaches this loopback listener through the host network.
For HTTPS, the public certificate setup below applies; use its **absolute host
workspace path** in `--cacert`, rather than `/workspace/proxy-ca.crt`.
This remains opt-in capture, not enforced proxy-only egress.

## Explicit proxy from the optional Docker backend

Follow [the TryHackMe network setup](tryhackme.md) first. Install mitmproxy in
the lab VM using its [official installation instructions](https://docs.mitmproxy.org/stable/overview/installation/).
Keep its processes, CA private key, and capture files outside the agent workspace.

Find the VM address on Docker's default bridge:

```sh
ip -4 addr show docker0
```

The following uses **172.17.0.1 as an example**. Substitute the address actually
reported on your VM in both the listener and command recipe. Do not use this
recipe if that is not your Docker bridge. In a separate terminal:

```sh
mkdir -m 700 -p captures
mitmdump --listen-host 172.17.0.1 --listen-port 8080 -w captures/web.flows
```

The listener is bound to the Docker-facing interface, not all interfaces.
Restrict it with the VM firewall if other containers can reach that network.
`127.0.0.1` inside an agent container refers to that container, not this proxy.

Keep `backend = "docker"`, set `network = "bridge"`, and add this recipe to the copied task policy:

```toml
curl_proxy = ["/usr/bin/curl", "--connect-timeout", "5", "--max-time", "30", "--proxy", "http://172.17.0.1:8080"]
```

Ask the agent to use `curl_proxy` for the authorized target. For example:

```sh
uv run ctf-agent --config config.local.tryhackme.toml \
  "Use curl_proxy to fetch the authorized room target at http://$TARGET_IP/. Report the HTTP status and relevant response headers, then stop."
```

## HTTPS

For intercepted HTTPS, curl needs to trust the proxy's public CA certificate.
After mitmproxy has initialized its certificate directory:

```sh
cp "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" workspace/proxy-ca.crt
```

This copies the **public certificate only**, renamed `.crt` so it is not blocked
by the default `*.pem` deny rule. Do not copy `mitmproxy-ca.pem`: it contains the
CA private key. Add `"--cacert", "/workspace/proxy-ca.crt"` to the `curl_proxy`
argv prefix for the Docker backend before using it with an HTTPS target. Native
execution uses the actual absolute host path to that certificate. Do not disable certificate
validation as a substitute. See [mitmproxy's certificate documentation](https://docs.mitmproxy.org/stable/concepts/certificates/).

## Preserve and inspect the evidence

Stop mitmdump with Ctrl-C when the run finishes so buffered flows are written.
Then copy the capture into the printed run directory, for example:

```sh
cp captures/web.flows runs/<the-actual-run-id>/http.flows
mitmdump -nr runs/<the-actual-run-id>/http.flows
```

`-n` prevents a proxy listener and `-r` reads the saved flow file. This inspects
the recorded traffic; it does not replay requests against the target. The copied
capture is supplementary evidence; the agent's event stream does not automatically
checksum or correlate its individual flows. Use separate captures per run and
retain their timestamps. Captures can contain credentials/cookies and full
request/response bodies; treat them as sensitive alongside the audit bundle.

## What a proxy does not enforce

This is **explicit, opt-in proxying**. Python, Nmap and curl with different
arguments can bypass it; an allowlisted interpreter can also construct its own
network requests. Mitmproxy's HTTP block lists do not constitute a general
network boundary. Enforced proxy-only egress needs firewall/network rules that
permit the proxy and deny direct traffic. See the
[official feature guide](https://docs.mitmproxy.org/stable/overview/features/).

Certificate-pinned clients and non-HTTP protocols need other capture approaches.
The proxy is not placed between this CLI and the model provider, so it does not
intercept provider API credentials. The core audit captures parsed model/tool
events instead of provider TLS traffic.
