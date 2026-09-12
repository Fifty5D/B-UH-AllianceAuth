"""Execute the additive file activation from the exact PR #61 runtime bytes."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ops.deploy import worker_recovery as recovery
from ops.deploy.contracts import DeploymentError

ROOT = Path(__file__).resolve().parents[2]
INSTALL = Path("/etc/buh-platform-v2/INSTALL.json")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@unittest.skipUnless(os.name == "posix", "Linux ownership and atomic-file activation")
class LogReaderInstallTests(unittest.TestCase):
    def test_already_installed_continuation_integrity_and_failure_restore(self):
        old = subprocess.check_output(
            [
                "git",
                "show",
                "4f02900aed9e62c3f7fcb7cce4bcdde50325b5bc:ops/deploy/docker_host.py",
            ],
            cwd=ROOT,
        )
        self.assertEqual(digest(old), recovery.INSTALLED_WORKER_RUNTIME)
        original = subprocess.check_output(
            [
                "git",
                "show",
                "fc0229b71c50c1bcb15d37f3625189fd9a7cb495:ops/deploy/docker_host.py",
            ],
            cwd=ROOT,
        )
        self.assertEqual(digest(original), recovery.ORIGINAL_RUNTIME)
        new = (ROOT / "ops/deploy/docker_host.py").read_bytes()
        reviewed = digest(new)
        for failure in (
            None,
            "approval",
            "parent",
            "receipt",
            "backup",
            "payload",
            "post-write",
            "replay",
        ):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as tmp,
                ExitStack() as stack,
            ):
                root = Path(tmp)
                library, stage = root / "library", root / "stage"
                target = library / "ops/deploy/docker_host.py"
                target.parent.mkdir(parents=True)
                stage.mkdir()
                target.write_bytes(old)
                target.chmod(0o644)
                (stage / "docker_host.py").write_bytes(new)
                base_install = root / "INSTALL.json"
                base_install.write_bytes(b'{"synthetic":"original receipt preserved"}\n')
                install_hash = digest(base_install.read_bytes())
                receipt, log_receipt = (
                    root / "WORKER-REPAIR.json",
                    root / "RETAINED-LOG-REPAIR.json",
                )
                first_backup, next_parent = (
                    root / "worker-check-c33fe29ed735",
                    root / "next",
                )
                first_backup.mkdir(mode=0o700)
                next_parent.mkdir(mode=0o700)
                next_backup = next_parent / f"retained-logs-{reviewed[:12]}"
                (first_backup / "docker_host.py").write_bytes(original)
                (first_backup / "INSTALL.json").write_bytes(base_install.read_bytes())
                receipt.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "result": "receiver-file-repaired",
                            "base_source_commit": "fc0229b71c50c1bcb15d37f3625189fd9a7cb495",
                            "base_install_sha256": install_hash,
                            "path": str(target),
                            "previous_sha256": recovery.ORIGINAL_RUNTIME,
                            "sha256": recovery.INSTALLED_WORKER_RUNTIME,
                            "backup_path": str(first_backup),
                            "deployment_performed": False,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
                retained = {
                    p: p.read_bytes()
                    for p in (receipt, base_install, *first_backup.iterdir())
                }
                journal = root / "failed-journal.json"
                journal.write_bytes(b'{"failed":"retain"}\n')
                retained[journal] = journal.read_bytes()

                # Simulate ONLY UID 0 and the protected outer staging ancestors.
                # Actual modes, symlinks, nofollow reads, hashes, guards and atomic
                # writes inside the fixture remain live; no integrity mock.
                real_lstat, real_fstat = Path.lstat, os.fstat

                def lstat(path):
                    info = list(real_lstat(path))
                    info[4] = info[5] = 0
                    if path != root and root not in path.parents:
                        info[0] &= ~0o022
                    return os.stat_result(info)

                def fstat(fd):
                    info = list(real_fstat(fd))
                    info[4] = info[5] = 0
                    return os.stat_result(info)

                real_read = recovery.read_file

                def read(path, **kwargs):
                    return real_read(base_install if path == INSTALL else path, **kwargs)

                for name, value in (
                    ("LIBRARY", library),
                    ("__file__", str(stage / "worker_recovery.py")),
                    (
                        "PINS",
                        {INSTALL: install_hash, journal: digest(journal.read_bytes())},
                    ),
                    ("REPAIR_RECEIPT", receipt),
                    ("LOG_REPAIR_RECEIPT", log_receipt),
                ):
                    stack.enter_context(mock.patch.object(recovery, name, value))
                stack.enter_context(
                    mock.patch.object(recovery, "_repair_backup", return_value=first_backup)
                )
                stack.enter_context(
                    mock.patch.object(
                        recovery, "_log_repair_backup", return_value=next_backup
                    )
                )
                stack.enter_context(
                    mock.patch.object(recovery, "read_file", side_effect=read)
                )
                stack.enter_context(mock.patch.object(Path, "lstat", lstat))
                stack.enter_context(mock.patch("os.fstat", fstat))
                stack.enter_context(mock.patch("os.fchown"))
                if failure == "parent":
                    next_parent.chmod(0o777)
                elif failure == "receipt":
                    receipt.write_text("{}\n")
                    retained[receipt] = receipt.read_bytes()
                elif failure == "backup":
                    (first_backup / "docker_host.py").write_bytes(b"changed")
                    retained[first_backup / "docker_host.py"] = b"changed"
                elif failure == "payload":
                    (stage / "docker_host.py").write_bytes(new + b"# changed\n")
                elif failure == "replay":
                    log_receipt.write_text("{}\n")
                elif failure == "post-write":
                    real_atomic = recovery._atomic_bytes

                    def atomic(path, raw, *args, **kwargs):
                        real_atomic(path, raw, *args, **kwargs)
                        if path == target and raw == new:
                            raise DeploymentError("injected after replacement")

                    stack.enter_context(
                        mock.patch.object(recovery, "_atomic_bytes", side_effect=atomic)
                    )
                confirm = (
                    ""
                    if failure == "approval"
                    else f"INSTALL RETAINED LOG READER {reviewed}"
                )
                if failure:
                    with self.assertRaises((DeploymentError, ValueError)):
                        recovery.install_log_reader(reviewed, confirmation=confirm)
                    self.assertEqual(target.read_bytes(), old)
                else:
                    result = recovery.install_log_reader(reviewed, confirmation=confirm)
                    self.assertEqual(result["result"], "receiver-log-reader-repaired")
                    self.assertEqual(target.read_bytes(), new)
                    self.assertEqual((next_backup / "docker_host.py").read_bytes(), old)
                    recovery.verify_pins(reviewed)
                    recovery.verify_log_repair(reviewed)
                    for part in (
                        log_receipt,
                        next_backup / "WORKER-REPAIR.json",
                        next_backup / "docker_host.py",
                    ):
                        unchanged = part.read_bytes()
                        part.write_bytes(unchanged + b"changed")
                        with self.assertRaises(DeploymentError):
                            recovery.verify_log_repair(reviewed)
                        part.write_bytes(unchanged)
                    with self.assertRaises(DeploymentError):
                        recovery.install_log_reader(reviewed, confirmation=confirm)
                # The old installer is not the continuation, even with its marker.
                with self.assertRaises(DeploymentError):
                    recovery.install(
                        reviewed, confirmation=f"INSTALL WORKER CHECK {reviewed}"
                    )
                for path, raw in retained.items():
                    self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
