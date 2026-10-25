"""Trusted stdlib-only child launcher. Invoked with -I -S, never imported.

Resource limits are applied in this separate process, avoiding preexec_fn in a
possibly multithreaded SDK process. Filesystem/network isolation is already in
force when this runs. The inherited hard limits also apply to child processes.
"""

import json
import os
import resource
import sys


def restrict(kind, desired):
    _, hard = resource.getrlimit(kind)
    cap = desired if hard == resource.RLIM_INFINITY else min(hard, desired)
    resource.setrlimit(kind, (cap, cap))


def main():
    limits = json.loads(sys.argv[1])
    os.umask(0o077)
    restrict(resource.RLIMIT_CORE, 0)
    restrict(resource.RLIMIT_NOFILE, 256)
    restrict(resource.RLIMIT_CPU, limits["timeout_seconds"])
    restrict(resource.RLIMIT_FSIZE, 64 * 1024 * 1024)
    if sys.platform == "linux":
        restrict(resource.RLIMIT_AS, limits["memory_mb"] * 1024 * 1024)
    # No shell, no PATH lookup, no inherited provider credentials.
    os.execve(sys.argv[2], sys.argv[2:], dict(os.environ))


if __name__ == "__main__":
    main()
