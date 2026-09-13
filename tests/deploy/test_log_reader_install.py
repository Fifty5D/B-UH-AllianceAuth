"""Execute additive file activations from the exact PR #61 and #62 bytes."""

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
        self.exercise_install(log_classifier=False)

    def test_classifier_continuation_requires_both_installed_receipts_and_backups(self):
        self.exercise_install(log_classifier=True)

    def exercise_install(self, *, log_classifier):
        worker = subprocess.check_output(
            [
                "git",
                "show",
                "4f02900aed9e62c3f7fcb7cce4bcdde50325b5bc:ops/deploy/docker_host.py",
            ],
            cwd=ROOT,
        )
        self.assertEqual(digest(worker), recovery.INSTALLED_WORKER_RUNTIME)
        old = worker
        if log_classifier:
            old = subprocess.check_output(
                [
                    "git",
                    "show",
                    "96c51a6aca6b07aac23c17934fb2465cf47e9f29:ops/deploy/docker_host.py",
                ],
                cwd=ROOT,
            )
            self.assertEqual(digest(old), recovery.INSTALLED_LOG_RUNTIME)
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
        failures = (
            None,
            "approval",
            "parent",
            "receipt",
            "backup",
            "payload",
            "post-write",
            "replay",
        )
        if log_classifier:
            failures += (
                "log-receipt",
                "log-backup",
                "saved-parent-receipt",
                "old-installer-baseline",
            )
        for failure in failures:
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
                prefix = "log-classifier" if log_classifier else "retained-logs"
                next_backup = next_parent / f"{prefix}-{reviewed[:12]}"
                installed_log_backup = root / "retained-logs-d75575649e7d"
                classifier_receipt = root / "LOG-CLASSIFIER-REPAIR.json"
                new_receipt = classifier_receipt if log_classifier else log_receipt
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
                if log_classifier:
                    installed_log_backup.mkdir(mode=0o700)
                    (installed_log_backup / "docker_host.py").write_bytes(worker)
                    (installed_log_backup / "INSTALL.json").write_bytes(
                        base_install.read_bytes()
                    )
                    (installed_log_backup / "WORKER-REPAIR.json").write_bytes(
                        receipt.read_bytes()
                    )
                    log_receipt.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "result": "receiver-log-reader-repaired",
                                "base_source_commit": "fc0229b71c50c1bcb15d37f3625189fd9a7cb495",
                                "base_install_sha256": install_hash,
                                "path": str(target),
                                "previous_sha256": recovery.INSTALLED_WORKER_RUNTIME,
                                "sha256": recovery.INSTALLED_LOG_RUNTIME,
                                "backup_path": str(installed_log_backup),
                                "parent_receipt_sha256": digest(receipt.read_bytes()),
                                "parent_receipt_path": str(receipt),
                                "deployment_performed": False,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    retained.update(
                        {
                            p: p.read_bytes()
                            for p in (log_receipt, *installed_log_backup.iterdir())
                        }
                    )
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
                    ("CLASSIFIER_REPAIR_RECEIPT", classifier_receipt),
                ):
                    stack.enter_context(mock.patch.object(recovery, name, value))
                stack.enter_context(
                    mock.patch.object(recovery, "_repair_backup", return_value=first_backup)
                )
                stack.enter_context(
                    mock.patch.object(
                        recovery,
                        "_log_repair_backup",
                        return_value=installed_log_backup
                        if log_classifier
                        else next_backup,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        recovery, "_classifier_repair_backup", return_value=next_backup
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
                    new_receipt.write_text("{}\n")
                elif failure in {"log-receipt", "log-backup", "saved-parent-receipt"}:
                    changed = {
                        "log-receipt": log_receipt,
                        "log-backup": installed_log_backup / "docker_host.py",
                        "saved-parent-receipt": installed_log_backup / "WORKER-REPAIR.json",
                    }[failure]
                    changed.write_bytes(b"changed\n")
                    retained[changed] = changed.read_bytes()
                elif failure == "old-installer-baseline":
                    target.write_bytes(worker)
                elif failure == "post-write":
                    real_atomic = recovery._atomic_bytes

                    def atomic(path, raw, *args, **kwargs):
                        real_atomic(path, raw, *args, **kwargs)
                        if path == target and raw == new:
                            raise DeploymentError("injected after replacement")

                    stack.enter_context(
                        mock.patch.object(recovery, "_atomic_bytes", side_effect=atomic)
                    )
                marker = (
                    "INSTALL LOG CLASSIFIER"
                    if log_classifier
                    else "INSTALL RETAINED LOG READER"
                )
                installer = (
                    recovery.install_log_classifier
                    if log_classifier
                    else recovery.install_log_reader
                )
                verifier = (
                    recovery.verify_classifier_repair
                    if log_classifier
                    else recovery.verify_log_repair
                )
                confirm = "" if failure == "approval" else f"{marker} {reviewed}"
                if failure:
                    with self.assertRaises((DeploymentError, ValueError)):
                        installer(reviewed, confirmation=confirm)
                    self.assertEqual(
                        target.read_bytes(),
                        worker if failure == "old-installer-baseline" else old,
                    )
                else:
                    result = installer(reviewed, confirmation=confirm)
                    self.assertEqual(
                        result["result"],
                        "receiver-log-classifier-repaired"
                        if log_classifier
                        else "receiver-log-reader-repaired",
                    )
                    self.assertEqual(target.read_bytes(), new)
                    self.assertEqual((next_backup / "docker_host.py").read_bytes(), old)
                    recovery.verify_pins(reviewed)
                    verifier(reviewed)
                    for part in (
                        new_receipt,
                        next_backup / "WORKER-REPAIR.json",
                        next_backup / "docker_host.py",
                    ) + (
                        (next_backup / "RETAINED-LOG-REPAIR.json",)
                        if log_classifier
                        else ()
                    ):
                        unchanged = part.read_bytes()
                        part.write_bytes(unchanged + b"changed")
                        with self.assertRaises(DeploymentError):
                            verifier(reviewed)
                        part.write_bytes(unchanged)
                    with self.assertRaises(DeploymentError):
                        installer(reviewed, confirmation=confirm)
                if log_classifier and failure != "old-installer-baseline":
                    with self.assertRaises(DeploymentError):
                        recovery.install_log_reader(
                            reviewed, confirmation=f"INSTALL RETAINED LOG READER {reviewed}"
                        )
                # The old installer is not the continuation, even with its marker.
                with self.assertRaises(DeploymentError):
                    recovery.install(
                        reviewed, confirmation=f"INSTALL WORKER CHECK {reviewed}"
                    )
                for path, raw in retained.items():
                    self.assertEqual(path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
