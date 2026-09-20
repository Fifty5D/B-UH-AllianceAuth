"""Execute the embedded owner launcher against isolated files and fake processes."""

from contextlib import redirect_stdout
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    (ROOT / "ops/deploy/run-reviewed-recovery.ps1")
    .read_text()
    .split("$rootScript = @'\n", 1)[1]
    .split("\n'@", 1)[0]
)


def archive_source(label, runtime):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, value in {
            "ops/deploy/docker_host.py": runtime,
            "source-identity": label.encode(),
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            tar.addfile(info, io.BytesIO(value))
    return raw.getvalue()


class ReviewedLauncherTests(unittest.TestCase):
    def exercise(self, *, failed_probe=False, wrong_installer=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upload = root / "upload"
            upload.mkdir()
            stage = root / "stage"
            stage.mkdir()
            verification = root / "v062-verification-only.tar.gz"
            verification.write_bytes(b"archive")
            runtime = b"unchanged approved receiver bytes"
            runtime_hash = hashlib.sha256(runtime).hexdigest()
            installer_commit = "c" * 40
            source = archive_source("new verification", runtime)
            installer = archive_source(
                "original installer", b"wrong" if wrong_installer else runtime
            )
            historical = b"{}"
            review = json.dumps(
                {
                    "receiver_sha256": runtime_hash,
                    "installer_commit": installer_commit,
                    "historical_log_evidence_sha256": hashlib.sha256(
                        historical
                    ).hexdigest(),
                }
            ).encode()
            for name, value in {
                "source.tar": source,
                "installer.tar": installer,
                "review.json": review,
                "historical-log-details.json": historical,
            }.items():
                (upload / name).write_bytes(value)
            calls = []

            def run(command, *, cwd, env, **kwargs):
                del kwargs
                calls.append((command, cwd))
                self.assertEqual(env["PYTHONPATH"], str(cwd))
                module = command[4]
                if module == "ops.deploy.worker_recovery":
                    self.assertEqual(
                        (cwd / "source-identity").read_text(), "original installer"
                    )
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "result": "receiver-owner-exhaustion-repaired",
                                "sha256": runtime_hash,
                            }
                        ),
                    )
                self.assertEqual((cwd / "source-identity").read_text(), "new verification")
                self.assertEqual(module, "ops.deploy.reviewed_recovery")
                return SimpleNamespace(
                    returncode=1 if failed_probe else 0,
                    stdout=json.dumps({"result": "failed" if failed_probe else "verified"}),
                )

            confirmation = base64.b64encode(
                ("INSTALL OWNER EXHAUSTION CHECK " + runtime_hash).encode()
            ).decode()
            args = [
                "launcher",
                str(upload),
                hashlib.sha256(source).hexdigest(),
                "d" * 40,
                runtime_hash,
                hashlib.sha256(review).hexdigest(),
                "InstallAndVerify",
                confirmation,
                hashlib.sha256(installer).hexdigest(),
                installer_commit,
            ]
            # Only the immutable archive hash is synthetic. All upload/source hash
            # and execution-order checks run from the actual launcher body.
            script = SCRIPT.replace(
                "0d1dec44c94a5cad8c6568b195a206954d3f8b660c304af418de4f6e12273ad0",
                hashlib.sha256(b"archive").hexdigest(),
            )
            with (
                mock.patch("sys.argv", args),
                mock.patch("tempfile.mkdtemp", return_value=str(stage)),
                mock.patch.object(Path, "glob", return_value=[verification]),
                mock.patch("subprocess.run", side_effect=run),
                mock.patch("os.umask"),
                redirect_stdout(io.StringIO()),
            ):
                if failed_probe or wrong_installer:
                    with self.assertRaises(SystemExit) as caught:
                        exec(compile(script, "owner-launcher", "exec"), {})
                    self.assertEqual(caught.exception.code, 1)
                else:
                    exec(compile(script, "owner-launcher", "exec"), {})
            if wrong_installer:
                self.assertFalse(calls)
            elif failed_probe:
                self.assertEqual(len(calls), 1)
                self.assertIn("--before-install", calls[0][0])
                self.assertTrue((stage / "before-install-report.json").exists())
            else:
                self.assertEqual(len(calls), 3)
                self.assertIn("--before-install", calls[0][0])
                self.assertIn("install-exhaustion", calls[1][0])
                self.assertNotIn("--before-install", calls[2][0])
                self.assertTrue((stage / "reviewed_recovery-report.json").exists())
            self.assertFalse(any("complete" in command for command, _ in calls))

    def test_original_approved_installer_is_used_between_staged_checks(self):
        self.exercise()

    def test_preinstallation_failure_preserves_receiver_and_returns_report(self):
        self.exercise(failed_probe=True)

    def test_changed_installer_payload_blocks_before_any_operation(self):
        self.exercise(wrong_installer=True)
