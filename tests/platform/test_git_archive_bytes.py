"""Packaging must preserve source identity despite Windows checkout conversion."""

import hashlib
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from ops.recovery.build_git_archive import build_archive


class GitArchiveBytesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "true")
        self.git("config", "user.name", "Synthetic archive test")
        self.git("config", "user.email", "archive@example.invalid")
        (self.repo / "source.py").write_bytes(b"first\nsecond\n")
        (self.repo / "other.txt").write_bytes(b"other\n")
        self.git("add", "source.py", "other.txt")
        self.git("commit", "-qm", "synthetic LF source")
        self.commit = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *args, data=None):
        return subprocess.run(["git", "-C", str(self.repo), *args], input=data,
                              check=True, capture_output=True).stdout

    def test_raw_blob_bytes_and_commit_survive_crlf_checkout_and_repeat_build(self):
        (self.repo / "source.py").write_bytes(b"first\r\nsecond\r\n")
        first, second = self.base / "first.tar", self.base / "second.tar"
        report = build_archive(self.repo, self.commit, first)
        build_archive(self.repo, self.commit, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(report["archive_sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
        with tarfile.open(first) as archive:
            self.assertEqual(archive.pax_headers["comment"], self.commit)
            self.assertEqual(archive.extractfile("source.py").read(), self.git("cat-file", "blob", self.commit + ":source.py"))
            self.assertEqual(archive.extractfile("source.py").read(), b"first\nsecond\n")

    def test_selected_paths_omit_other_tracked_files(self):
        output = self.base / "selected.tar"
        build_archive(self.repo, self.commit, output, ["source.py"])
        with tarfile.open(output) as archive:
            self.assertEqual(archive.getnames(), ["source.py"])

    def test_symlink_git_mode_is_rejected_without_following_checkout(self):
        oid = self.git("hash-object", "-w", "--stdin", data=b"source.py").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{oid},link")
        self.git("commit", "-qm", "synthetic symlink")
        with self.assertRaisesRegex(ValueError, "regular Git blobs"):
            build_archive(self.repo, "HEAD", self.base / "unsafe.tar")


if __name__ == "__main__":
    unittest.main()
