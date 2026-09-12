"""Read-only recovery evidence and unchanged immutable payload contracts."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from ops.deploy import collect_worker_recovery as collector, request_archive

ROOT = Path(__file__).resolve().parents[2]
RELEASE = "6074b965cbd2e6ab2630cd539ee455b8d419aef6"


class WorkerRecoveryTests(unittest.TestCase):
    def test_exact_v062_request_archive_does_not_replace_installed_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "immutable-checkout"
            for args in (
                ["clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(checkout)],
                ["-C", str(checkout), "config", "core.autocrlf", "false"],
                ["-C", str(checkout), "checkout", "--quiet", RELEASE],
            ):
                subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True)
            output = Path(tmp) / "synthetic-request.tar.gz"
            metadata = request_archive.build_archive(
                root=checkout,
                release_dir=checkout / "releases/platform/v0.6.2",
                repository="Fifty5D/B-UH-AllianceAuth",
                release_commit=RELEASE,
                mode="deploy",
                workflow_run_id="123456",
                workflow_run_attempt=1,
                output=output,
            )
            self.assertEqual(
                metadata["manifest_sha256"],
                "c9cf79d34c8b3f90556e405f338ee4c1e7818d9c686f0462eec7dca201b784f4",
            )
            with tarfile.open(output, "r:gz") as archive:
                names = archive.getnames()
                self.assertTrue(
                    all(
                        name == "REQUEST.json"
                        or name == "release"
                        or name.startswith("release/")
                        or name == "lineage"
                        or name.startswith("lineage/")
                        for name in names
                    )
                )
                self.assertFalse(any("docker_host.py" in name for name in names))
                for member in archive:
                    if member.isfile() and member.name.startswith("release/"):
                        payload = archive.extractfile(member).read()
                        self.assertEqual(
                            payload,
                            (
                                checkout
                                / "releases/platform/v0.6.2"
                                / Path(member.name).name
                            ).read_bytes(),
                        )

    def test_collector_emits_only_metadata_and_never_activates_recovery(self):
        secret = "must-not-appear-in-owner-report"
        plan = {
            "attempt_id": collector.ATTEMPT,
            "status": "active",
            "backup_path": str(collector.BACKUP),
            "phase": "candidate-slots-started",
            "flags": {"migration_started": True},
            "candidate_web_slots": [f"buh-web-candidate-{collector.ATTEMPT}-1"],
            "previous_web_slots": [f"buh-web-previous-{collector.ATTEMPT}-1"],
            "sensitive_unrelated_field": secret,
        }

        def read(path):
            if path.name in {"active-recovery.json", "RECOVERY.json"}:
                data = json.dumps(plan).encode()
            elif path.name == "INSTALL.json":
                data = json.dumps({"source_commit": "a" * 40}).encode()
            else:
                data = secret.encode()
            return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}, data

        with (
            mock.patch.object(os, "geteuid", return_value=0, create=True),
            mock.patch.object(collector, "read_file", side_effect=read),
            mock.patch.object(
                Path, "iterdir", return_value=iter([collector.BACKUP / "local.py"])
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(collector.main(), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["read_only"])
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("not verified rollback", result["warning"])

    def test_collector_failures_are_bounded_and_not_secret_bearing(self):
        with (
            mock.patch.object(
                collector, "collect", side_effect=ValueError("token=never-print-this")
            ),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(collector.main(), 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"read_only": True, "result": "incomplete", "error_type": "ValueError"},
        )
        self.assertNotIn("never-print-this", output.getvalue())

    @unittest.skipUnless(os.name == "posix", "Linux no-follow evidence boundary")
    def test_collector_rejects_unsafe_parent_without_reading_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o777)
            path = root / "evidence.json"
            path.write_text("secret-data", encoding="ascii")
            with (
                mock.patch.object(os, "open") as opened,
                self.assertRaisesRegex(ValueError, "unsafe evidence ancestor"),
            ):
                collector.read_file(path)
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
