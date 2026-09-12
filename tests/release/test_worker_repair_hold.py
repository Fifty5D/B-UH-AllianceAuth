"""A real merge of this receiver fix holds releases without reopening approval."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ops.release import validation_recovery as recovery

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops/release"))
BASE = "efdeebf9ff86a97cef18463605273a032403c4f0"


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr[-1000:])
    return result.stdout.strip()


class WorkerRepairHoldTests(unittest.TestCase):
    def test_receiver_repair_merge_preserves_release_and_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "checkout"
            git(
                ROOT,
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                str(ROOT),
                str(checkout),
            )
            for key, value in (
                ("core.autocrlf", "false"),
                ("user.name", "Worker Rehearsal"),
                ("user.email", "worker@example.invalid"),
            ):
                git(checkout, "config", key, value)
            git(checkout, "checkout", "--quiet", "-b", "synthetic-worker-fix", BASE)
            file = checkout / "ops/deploy/docker_host.py"
            file.write_bytes((ROOT / "ops/deploy/docker_host.py").read_bytes())
            git(checkout, "add", "ops/deploy/docker_host.py")
            git(checkout, "commit", "--quiet", "-m", "synthetic receiver repair")
            head = git(checkout, "rev-parse", "HEAD")
            git(checkout, "checkout", "--quiet", "-b", "synthetic-main", BASE)
            git(
                checkout,
                "merge",
                "--quiet",
                "--no-ff",
                head,
                "-m",
                "synthetic reviewed repair merge",
            )
            merge = git(checkout, "rev-parse", "HEAD")
            held = recovery.validate_hold(
                checkout, merge, contract=recovery.load_contract()
            )
            self.assertEqual(held["state"], "worker-recovery-unverified-held")
            self.assertEqual(
                held["production_authorization"],
                "consumed-requires-new-review-and-approval",
            )
            self.assertEqual(
                git(
                    checkout,
                    "diff",
                    "--name-only",
                    BASE,
                    merge,
                    "--",
                    "releases",
                    "changes",
                ),
                "",
            )
            # An unrelated application edit cannot join this bounded hold.
            git(checkout, "checkout", "--quiet", "synthetic-worker-fix")
            unrelated = checkout / "ops/readiness.py"
            unrelated.write_bytes(
                unrelated.read_bytes() + b"\n# unrelated synthetic change\n"
            )
            git(checkout, "add", "ops/readiness.py")
            git(checkout, "commit", "--quiet", "-m", "unrelated change")
            extra = git(checkout, "rev-parse", "HEAD")
            git(checkout, "checkout", "--quiet", "-b", "synthetic-other-main", BASE)
            git(checkout, "merge", "--quiet", "--no-ff", extra, "-m", "unqualified merge")
            with self.assertRaisesRegex(
                recovery.ValidationRecoveryError, "bounded release hold"
            ):
                recovery.validate_hold(checkout, "HEAD", contract=recovery.load_contract())
