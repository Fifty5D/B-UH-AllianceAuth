import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReceiverUpgradeContractTests(unittest.TestCase):
    def test_upgrade_is_exact_commit_tested_and_restored_on_failure(self):
        script = (ROOT / "ops/deploy/upgrade-receiver.sh").read_text()
        self.assertIn('rev-parse HEAD)', script)
        self.assertIn("git -C \"${repo_root}\" diff --quiet", script)
        self.assertIn("python3 -m unittest discover -s tests/deploy", script)
        self.assertIn("trap restore ERR", script)
        self.assertIn("receiver.tar", script)
        self.assertIn('preflight <"${request}" >/dev/null', script)

    def test_upgrade_never_handles_ssh_key_or_environment_files(self):
        script = (ROOT / "ops/deploy/upgrade-receiver.sh").read_text()
        self.assertNotIn("authorized_keys", script)
        self.assertNotIn(".env", script)
