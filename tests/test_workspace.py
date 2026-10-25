import os
from unittest.mock import patch

from aiagent.workspace import PolicyError, Workspace
from tests.support import ProjectTest


class WorkspaceTests(ProjectTest):
    def setUp(self):
        super().setUp()
        self.ws = Workspace(self.config.workspace, self.config.denied_paths, 100, 10)
        self.outside = self.base / "outside.txt"
        self.outside.write_text("keep me")

    def test_normal_file_lifecycle(self):
        self.ws.write("nested/solve.py", "print(42)")
        self.assertEqual(self.ws.read("nested/solve.py"), "print(42)")
        self.ws.write("nested/solve.py", "short")
        self.assertEqual(self.ws.read("nested/solve.py"), "short")
        self.assertIn("solve.py", self.ws.list("nested"))
        self.ws.assert_mount_safe()

    def test_rejects_escape_absolute_and_denied_paths(self):
        for name in (
            "../outside.txt",
            str(self.outside),
            "sub/../../outside.txt",
            ".env",
            ".ENV",
            "sub/.GIT/config",
            "sub/private.KEY",
            "bad\x00path",
            "..\\outside.txt",
        ):
            with self.subTest(name=name):
                for operation in (
                    self.ws.read,
                    lambda p: self.ws.write(p, "changed"),
                    self.ws.list,
                ):
                    with self.assertRaises((PolicyError, OSError)):
                        operation(name)
        self.assertEqual(self.outside.read_text(), "keep me")

    def test_symlink_file_and_directory_cannot_escape(self):
        root = self.config.workspace
        (root / "file-link").symlink_to(self.outside)
        (root / "dir-link").symlink_to(self.base, target_is_directory=True)
        (root / "dangling").symlink_to(self.base / "new-outside.txt")
        for name in ("file-link", "dir-link/outside.txt", "dangling"):
            with self.subTest(name=name):
                with self.assertRaises((PolicyError, OSError)):
                    self.ws.read(name)
                with self.assertRaises((PolicyError, OSError)):
                    self.ws.write(name, "changed")
        with self.assertRaises(OSError):
            self.ws.list("dir-link")
        self.assertEqual(self.outside.read_text(), "keep me")
        self.assertFalse((self.base / "new-outside.txt").exists())

    def test_internal_symlinks_are_rejected_too(self):
        self.ws.write("real", "hello")
        (self.config.workspace / "link").symlink_to("real")
        with self.assertRaises(OSError):
            self.ws.read("link")

    def test_hard_link_not_truncated_or_read(self):
        os.link(self.outside, self.config.workspace / "hard-link")
        with self.assertRaises(PolicyError):
            self.ws.read("hard-link")
        with self.assertRaises(PolicyError):
            self.ws.write("hard-link", "changed")
        self.assertEqual(self.outside.read_text(), "keep me")

    def test_fifo_read_does_not_block(self):
        os.mkfifo(self.config.workspace / "pipe")
        with self.assertRaises(PolicyError):
            self.ws.read("pipe")

    def test_directory_swap_does_not_redirect_pinned_fd(self):
        self.ws.write("sub/target", "inside")
        original_open = os.open
        root = self.config.workspace

        def swap_after_open(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if path == "sub":
                (root / "sub").rename(root / "old-sub")
                (root / "sub").symlink_to(self.base, target_is_directory=True)
            return fd

        with patch("aiagent.workspace.os.open", side_effect=swap_after_open):
            self.ws.write("sub/target", "still inside")
        self.assertEqual((root / "old-sub/target").read_text(), "still inside")
        self.assertFalse((self.base / "target").exists())

    def test_file_limits_use_utf8_bytes(self):
        with self.assertRaises(PolicyError):
            self.ws.write("large", "é" * 51)
        (self.config.workspace / "large").write_text("x" * 101)
        self.assertEqual(self.ws.read("large"), "x" * 100 + "\n[File truncated]")

    def test_listing_hides_denied_entries_and_is_bounded(self):
        (self.config.workspace / ".env").write_text("secret")
        self.assertNotIn(".env", self.ws.list())
        for number in range(15):
            (self.config.workspace / f"file-{number}").touch()
        self.assertIn("truncated", self.ws.list())
        self.assertLessEqual(len(self.ws.list().splitlines()), 11)

    def test_mount_preflight_blocks_denied_paths_links_and_special_files(self):
        root = self.config.workspace
        for kind in ("denied", "symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                name = root / (".env" if kind == "denied" else "unsafe")
                if kind == "denied":
                    name.write_text("secret")
                elif kind == "symlink":
                    name.symlink_to(self.outside)
                elif kind == "hardlink":
                    os.link(self.outside, name)
                else:
                    os.mkfifo(name)
                with self.assertRaises(PolicyError):
                    self.ws.assert_mount_safe()
                name.unlink()

    def test_path_globs_cover_descendants(self):
        ws = Workspace(self.config.workspace, ("private/*",), 100, 10)
        self.assertTrue(ws.denied_path("private/data/nested/secret.txt"))
