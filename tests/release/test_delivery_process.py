"""Delivery holds survive feature work; uncertain changes always receive full CI."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from ops.release import ci_scope, delivery_state

ROOT = Path(__file__).resolve().parents[2]


class DeliveryProcessTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "README.md").write_text("Initial prose\n")
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def test_documentation_only_and_mixed_changes_use_different_lanes(self):
        (self.root / "README.md").write_text("Updated prose\n")
        self.assertTrue(ci_scope.docs_only(self.root, self.base, self.commit()))
        (self.root / "runtime.py").write_text("print('changed')\n")
        self.assertFalse(ci_scope.docs_only(self.root, self.base, self.commit()))

    def test_renaming_code_into_docs_does_not_hide_runtime_changes(self):
        (self.root / "runtime.py").write_text("print('code')\n")
        base = self.commit()
        (self.root / "docs").mkdir()
        (self.root / "runtime.py").rename(self.root / "docs" / "code.md")
        self.assertFalse(ci_scope.docs_only(self.root, base, self.commit()))

    def test_unknown_empty_and_unavailable_diffs_run_every_lane(self):
        for base in ("", "0" * 40, "1" * 40, self.base, "--help"):
            self.assertFalse(ci_scope.docs_only(self.root, base, self.base))
        for path in ("docs/test.py", "docs/browser.js", "platform/README.md", ".github/workflows/ci.yml"):
            self.assertFalse(ci_scope.prose_path(path))

    def write_hold(self, value):
        path = self.root / delivery_state.HOLD_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def test_active_hold_allows_arbitrary_feature_commits_without_claiming_deployment(self):
        hold = json.loads((ROOT / delivery_state.HOLD_PATH).read_text())
        self.write_hold(hold)
        (self.root / "feature.py").write_text("print('new feature')\n")
        head = self.commit()
        report = delivery_state.status(self.root)
        self.assertEqual(report["source_commit"], head)
        self.assertEqual(report["feature_work"], "allowed")
        self.assertEqual(report["production"], "blocked-by-recovery")
        self.assertIsNone(report["installed_release"])

    def test_malformed_and_symlink_holds_fail_closed(self):
        valid = json.loads((ROOT / delivery_state.HOLD_PATH).read_text())
        for key, value in (("state", "inactive"), ("attempt_id", []), ("target_release", None), ("reason", "")):
            path = self.write_hold({**valid, key: value})
            with self.assertRaises(ValueError):
                delivery_state.status(self.root)
        path.unlink()
        path.symlink_to(self.root / "missing.json")
        with self.assertRaises(ValueError):
            delivery_state.status(self.root)

    def test_legacy_contract_still_holds_and_no_hold_still_requires_approval(self):
        path = self.root / delivery_state.LEGACY_HOLD_PATH
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        self.assertEqual(delivery_state.status(self.root)["release_preparation"], "held")
        path.unlink()
        self.assertEqual(delivery_state.status(self.root)["production"], "requires-exact-production-approval")
