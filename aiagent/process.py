"""Bound command output/time. Native commands additionally own a process group."""

import os
import selectors
import signal
import subprocess
import time


def capture(
    argv: list[str], timeout: int, limit: int, *, cwd=None, env=None, process_group: bool = False
) -> tuple[int, bytes, str]:
    output = bytearray()
    reason = ""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=env,
        start_new_session=process_group,
        close_fds=True,
    ) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "Command timed out"
                        break
                    if not selector.select(min(remaining, 0.1)):
                        continue
                    chunk = os.read(process.stdout.fileno(), min(8192, limit - len(output) + 1))
                    if not chunk:
                        break
                    available = limit - len(output)
                    output.extend(chunk[:available])
                    if len(chunk) > available:
                        reason = "Command stopped at output limit"
                        break
            if reason:
                process.kill()
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                reason = "Command timed out"
                process.kill()
                process.wait()
        finally:
            # Seatbelt denies setsid/setpgid; bubblewrap also has a PID namespace.
            # Clean remaining children on success, failure, interruption and timeout.
            if process_group:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
                process.wait()
    return process.returncode, bytes(output), reason
