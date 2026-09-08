from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from ops.deploy import contracts
from ops.release import recovery_policy


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "ops/deploy/request_archive.py"
V061_RELEASE = "43234a8c0b6371fdfc62b59fa75a7980924ac3a5"
POWERSHELL = shutil.which("pwsh")


def run(*arguments: object, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class RequestArchiveExecutionTests(unittest.TestCase):
    def test_bootstrap_cli_is_deterministic_and_receiver_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            checkout = base / "exact-checkout"
            clone = run(
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                ROOT,
                checkout,
            )
            self.assertEqual(clone.returncode, 0, clone.stderr[-1000:])
            configured = run("git", "config", "core.autocrlf", "false", cwd=checkout)
            self.assertEqual(configured.returncode, 0, configured.stderr[-1000:])
            head = run("git", "-C", ROOT, "rev-parse", "HEAD").stdout.strip()
            checked_out = run(
                "git", "checkout", "--quiet", "--detach", head, cwd=checkout
            )
            self.assertEqual(checked_out.returncode, 0, checked_out.stderr[-1000:])

            outputs = [base / "first.tar.gz", base / "second.tar.gz"]
            metadata = []
            for output in outputs:
                result = run(
                    sys.executable,
                    BUILDER,
                    "--root",
                    checkout,
                    "--release-dir",
                    checkout / "releases/platform/v0.6.1",
                    "--repository",
                    "Fifty5D/B-UH-AllianceAuth",
                    "--release-commit",
                    V061_RELEASE,
                    "--mode",
                    "preflight",
                    "--workflow-run-id",
                    "4323400000000000000",
                    "--workflow-run-attempt",
                    "1",
                    "--bootstrap-recovery",
                    "--output",
                    output,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-1000:])
                metadata.append(json.loads(result.stdout))

            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
            self.assertEqual(metadata[0], metadata[1])
            self.assertEqual(
                metadata[0]["recovery_transition_sha256"],
                "78961ac44eceb253f6c9db7ac863f514bb00336e0522c8486c70178f112078ff",
            )
            self.assertEqual(
                [item["platform_version"] for item in metadata[0]["release_refs"]],
                ["0.5.6", "0.6.0", "0.6.1"],
            )

            with tarfile.open(outputs[0], "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}
            self.assertIn("lineage/v0.5.6.RELEASE.json", names)
            self.assertIn("lineage/v0.6.0.RELEASE.json", names)
            self.assertNotIn("lineage/v0.6.1.RELEASE.json", names)

            extracted = base / "extracted"
            contracts.extract_archive(outputs[0].read_bytes(), extracted)
            config = contracts.ReceiverConfig.load(
                ROOT / "ops/deploy/receiver-config.example.json"
            )
            bundle = contracts.load_validated_bundle(extracted, config)
            self.assertEqual(bundle.request.platform_version, "0.6.1")
            self.assertEqual(bundle.request.mode, "preflight")
            self.assertEqual(len(bundle.lineage_manifests), 2)
            self.assertEqual(
                hashlib.sha256(outputs[0].read_bytes()).hexdigest(),
                hashlib.sha256(outputs[1].read_bytes()).hexdigest(),
            )

    def test_policy_binds_exact_server_evidence_and_immutable_manifests(self):
        policy = recovery_policy.load_policy()
        self.assertEqual(
            policy["server_evidence"],
            {
                "captured_at": "2026-09-07T22:17:17.791522+00:00",
                "sha256": (
                    "df9865e121e068cc65c3e6b05cb287f1b87f05772e54ecd4c7d07fa61137fc38"
                ),
            },
        )
        for identity in policy["published_releases"]:
            version = identity["platform_version"]
            manifest = run(
                "git",
                "show",
                f"{identity['release_commit']}:releases/platform/v{version}/RELEASE.json",
            )
            self.assertEqual(manifest.returncode, 0, manifest.stderr[-1000:])
            self.assertEqual(
                hashlib.sha256(manifest.stdout.encode("utf-8")).hexdigest(),
                identity["manifest_sha256"],
            )

    @unittest.skipUnless(POWERSHELL, "requires PowerShell")
    def test_windows_helper_prepares_the_exact_deterministic_bootstrap_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            checkout = base / "reviewed-checkout"
            clone = run(
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                ROOT,
                checkout,
            )
            self.assertEqual(clone.returncode, 0, clone.stderr[-1000:])
            configured = run("git", "config", "core.autocrlf", "false", cwd=checkout)
            self.assertEqual(configured.returncode, 0, configured.stderr[-1000:])
            head = run("git", "-C", ROOT, "rev-parse", "HEAD").stdout.strip()
            checked_out = run(
                "git", "checkout", "--quiet", "--detach", head, cwd=checkout
            )
            self.assertEqual(checked_out.returncode, 0, checked_out.stderr[-1000:])

            output = base / "prepared-bootstrap.tar.gz"
            environment = os.environ.copy()
            venv_scripts = ROOT / ".venv" / "Scripts"
            if venv_scripts.is_dir():
                environment["PATH"] = f"{venv_scripts}{os.pathsep}{environment['PATH']}"
            result = subprocess.run(
                [
                    POWERSHELL,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    checkout / "setup/Prepare-BUH-PlatformV2Preflight.ps1",
                    "-ReviewedCommit",
                    head,
                    "-OutputPath",
                    output,
                ],
                cwd=checkout,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-1000:])
            metadata = json.loads(
                Path(f"{output}.metadata.json").read_text(encoding="ascii")
            )
            self.assertEqual(
                metadata["recovery_transition_sha256"],
                "78961ac44eceb253f6c9db7ac863f514bb00336e0522c8486c70178f112078ff",
            )
            extracted = base / "helper-extracted"
            contracts.extract_archive(output.read_bytes(), extracted)
            config = contracts.ReceiverConfig.load(
                checkout / "ops/deploy/receiver-config.example.json"
            )
            bundle = contracts.load_validated_bundle(extracted, config)
            self.assertEqual(bundle.request.platform_version, "0.6.1")
            self.assertEqual(
                bundle.request.recovery_transition["purpose"],
                "receiver-upgrade-preflight",
            )


if __name__ == "__main__":
    unittest.main()
