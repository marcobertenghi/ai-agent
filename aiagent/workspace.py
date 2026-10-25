"""Host file tools rooted at directory descriptors, without following links.

Checking a path then opening it by name has a symlink race. Instead we open each
directory with O_NOFOLLOW and resolve the next component relative to that fd.
This module intentionally supports POSIX hosts (Linux/macOS), not Windows.
"""

import os
import stat
import unicodedata
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path


class PolicyError(ValueError):
    """A tool request violates the operator's policy."""


class Workspace:
    def __init__(self, root: Path, denied: tuple[str, ...], file_limit: int, entry_limit: int):
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise PolicyError("File confinement requires Linux or macOS")
        self.root = root
        self.denied = denied
        self.file_limit = file_limit
        self.entry_limit = entry_limit

    def denied_path(self, path: str) -> bool:
        parts = Path(path).parts
        # Check ancestors too: a glob such as 'private/*' must cover descendants.
        candidates = [*parts, *("/".join(parts[:i]) for i in range(1, len(parts) + 1))]

        # macOS commonly uses case-insensitive, normalization-insensitive paths.
        # Apply the conservative matching rule on every OS so aliases such as
        # '.ENV' cannot bypass a '.env' deny rule on those filesystems.
        def normalize(value):
            return unicodedata.normalize("NFC", value).casefold()

        return any(
            fnmatchcase(normalize(p), normalize(pattern))
            for p in candidates
            for pattern in self.denied
        )

    def _parts(self, path: str) -> tuple[str, ...]:
        if not path or "\x00" in path or "\\" in path or Path(path).is_absolute():
            raise PolicyError("Use a relative workspace path")
        parts = Path(path).parts
        if ".." in parts or self.denied_path(path):
            raise PolicyError("Path is outside the workspace or denied by policy")
        return parts

    @contextmanager
    def _directory(self, parts: tuple[str, ...], create: bool = False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.root, flags)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _regular(fd: int):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PolicyError("Only regular files with one hard link are permitted")

    def read(self, file_path: str) -> str:
        parts = self._parts(file_path)
        if not parts:
            raise PolicyError("Specify a file, not the workspace directory")
        with self._directory(parts[:-1]) as parent:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                self._regular(handle.fileno())
                data = handle.read(self.file_limit + 1)
        content = data[: self.file_limit].decode("utf-8", errors="replace")
        return content + ("\n[File truncated]" if len(data) > self.file_limit else "")

    def write(self, file_path: str, content: str) -> str:
        parts = self._parts(file_path)
        data = content.encode("utf-8")
        if not parts or len(data) > self.file_limit:
            raise PolicyError("Specify a file and keep content within max_file_bytes")
        with self._directory(parts[:-1], create=True) as parent:
            # Do not truncate until AFTER checking the opened inode. In particular,
            # truncating a hard link first would already damage its other name.
            fd = os.open(
                parts[-1],
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(fd, "wb") as handle:
                self._regular(handle.fileno())
                os.ftruncate(handle.fileno(), 0)
                handle.write(data)
        return f"Wrote {len(data)} bytes to {file_path}"

    def list(self, directory: str = ".") -> str:
        parts = self._parts(directory)
        lines = []
        with self._directory(parts) as fd, os.scandir(fd) as entries:
            for index, entry in enumerate(entries):
                # Count hidden/denied entries too so scanning is always bounded.
                if index >= self.entry_limit:
                    lines.append("[Directory listing truncated]")
                    break
                relative = "/".join((*parts, entry.name))
                if self.denied_path(relative):
                    continue
                info = entry.stat(follow_symlinks=False)
                kind = (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    else "blocked"
                )
                lines.append(f"{entry.name!r}: {kind}, {info.st_size} bytes")
        return "\n".join(sorted(lines)) or "[Empty directory]"

    def assert_mount_safe(self):
        """Don't expose denied files through a command's whole-workspace mount.

        This preflight assumes no other host process modifies the workspace while
        the agent runs. Container-created links remain untrusted to file tools.
        """
        pending = [()]
        count = 0
        root_device = self.root.stat().st_dev
        while pending:
            parts = pending.pop()
            with self._directory(parts) as fd, os.scandir(fd) as entries:
                for entry in entries:
                    count += 1
                    if count > 10_000:
                        raise PolicyError(
                            "Command workspace exceeds the 10,000-entry safety scan limit"
                        )
                    relative = "/".join((*parts, entry.name))
                    info = entry.stat(follow_symlinks=False)
                    if self.denied_path(relative):
                        raise PolicyError(
                            "Command workspace contains a denied path; remove it before execution"
                        )
                    if info.st_dev != root_device:
                        raise PolicyError("Nested filesystem mounts are not permitted")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((*parts, entry.name))
                    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise PolicyError("Command workspace contains a link or special file")
