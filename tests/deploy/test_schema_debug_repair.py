"""Transaction checks for the one-file ESI schema log repair."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


@unittest.skipUnless(os.name == "posix", "host receiver repair runs on Linux")
class SchemaDebugRepairTests(unittest.TestCase):
    def setUp(self):
        from ops.deploy import schema_debug_repair as repair

        self.repair = repair
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(self.temporary)
        source = root / "source"
        source.mkdir()
        (source / "schema_debug_repair.py").write_text("", encoding="ascii")
        self.replacement = source / "docker_host.py"
        self.replacement.write_bytes(b"print('reviewed replacement')\n")
        self.replacement_sha = hashlib.sha256(self.replacement.read_bytes()).hexdigest()
        self.target = root / "installed.py"
        self.target.write_bytes(b"previous receiver")
        self.receipt = root / "REPAIR.json"
        self.backups = root / "backups"
        self.backups.mkdir()
        self.lock = root / "deploy.lock"

        def read(path):
            path = Path(path)
            if not path.exists():
                return {"present": False}, None
            raw = path.read_bytes()
            return {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "mode": oct(path.stat().st_mode & 0o777),
            }, raw

        def write(path, raw, mode, *, owner):
            path = Path(path)
            path.write_bytes(raw)
            path.chmod(mode)

        for name, value in (
            ("__file__", str(source / "schema_debug_repair.py")),
            ("TARGET", self.target),
            ("CONFIG", root / "receiver.json"),
            ("RECEIPT", self.receipt),
            ("BACKUP_ROOT", self.backups),
            ("PREVIOUS_SHA256", hashlib.sha256(self.target.read_bytes()).hexdigest()),
        ):
            self.enterContext(mock.patch.object(repair, name, value))
        self.enterContext(mock.patch.object(repair, "read_file", side_effect=read))
        self.enterContext(mock.patch.object(repair, "_atomic_bytes", side_effect=write))
        self.enterContext(mock.patch.object(repair, "_baseline", return_value=self.target.read_bytes()))
        self.enterContext(mock.patch.object(repair, "_verify_backup_root"))
        self.enterContext(mock.patch.object(repair.ReceiverConfig, "load", return_value=object()))
        self.enterContext(mock.patch.object(
            repair, "_open_lock", side_effect=lambda _config: os.open(self.lock, os.O_CREAT | os.O_RDWR)
        ))

    def call(self, mode):
        return self.repair.run(
            mode=mode,
            replacement_sha256=self.replacement_sha,
            source_commit="a" * 40,
            confirmation=f"INSTALL ESI SCHEMA DEBUG CHECKER {self.replacement_sha}",
        )

    def test_plan_does_not_install_and_install_retains_previous_file(self):
        self.assertEqual(self.call("plan")["result"], "ready-to-install")
        self.assertEqual(self.target.read_bytes(), b"previous receiver")
        self.assertFalse(list(self.backups.iterdir()))
        report = self.call("install")
        self.assertEqual(self.target.read_bytes(), self.replacement.read_bytes())
        self.assertEqual((Path(report["backup_path"]) / "docker_host.py").read_bytes(), b"previous receiver")
        self.assertEqual(json.loads(self.receipt.read_text(encoding="ascii")), report)

    def test_receipt_failure_restores_previous_file_and_keeps_backup(self):
        real_write = self.repair._atomic_bytes

        def fail_receipt(path, raw, mode, *, owner):
            if path == self.receipt:
                raise OSError("synthetic receipt failure")
            return real_write(path, raw, mode, owner=owner)

        with mock.patch.object(self.repair, "_atomic_bytes", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.call("install")
        self.assertEqual(self.target.read_bytes(), b"previous receiver")
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        self.assertFalse(self.receipt.exists())


@unittest.skipUnless(os.name == "posix", "host receiver repair runs on Linux")
class SchemaDebugFollowupTests(SchemaDebugRepairTests):
    def setUp(self):
        super().setUp()
        from ops.deploy import schema_debug_followup as followup

        root = Path(self.temporary)
        self.receipt = root / "FOLLOWUP.json"
        for name, value in (
            ("__file__", str(self.replacement.with_name("schema_debug_followup.py"))),
            ("TARGET", self.target),
            ("CONFIG", root / "receiver.json"),
            ("RECEIPT", self.receipt),
            ("BACKUP_ROOT", self.backups),
            ("PREVIOUS_SHA256", hashlib.sha256(self.target.read_bytes()).hexdigest()),
        ):
            self.enterContext(mock.patch.object(followup, name, value))
        self.enterContext(mock.patch.object(followup, "_baseline", return_value=self.target.read_bytes()))
        self.enterContext(mock.patch.object(followup, "_atomic_bytes", side_effect=self.repair._atomic_bytes))
        self.enterContext(mock.patch.object(
            followup, "_open_lock", side_effect=lambda _config: os.open(self.lock, os.O_CREAT | os.O_RDWR)
        ))
        self.repair = followup

    def call(self, mode):
        return self.repair.run(
            mode=mode,
            replacement_sha256=self.replacement_sha,
            source_commit="b" * 40,
            confirmation=f"INSTALL ESI SCHEMA MIRROR CHECKER {self.replacement_sha}",
        )


if __name__ == "__main__":
    unittest.main()
