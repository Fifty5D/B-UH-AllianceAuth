"""Contracts for the immutable legacy-v1 upgrade baseline."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "platform/baselines/legacy-v1-v0.3.3.json"


class LegacyBaselineContracts(unittest.TestCase):
    def setUp(self):
        self.baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    def test_baseline_identity_and_schema_are_strict(self):
        self.assertEqual(
            set(self.baseline),
            {
                "schema_version",
                "fixture",
                "migration_targets",
                "platform_version",
                "release_commit",
                "release_path",
                "wheels",
            },
        )
        self.assertEqual(self.baseline["schema_version"], 1)
        self.assertEqual(self.baseline["platform_version"], "0.3.3")
        self.assertEqual(
            self.baseline["release_commit"],
            "e70a8ee713d358e2f0e3c1dc5b23305c863aa628",
        )
        self.assertTrue(
            (ROOT / "platform/baselines/legacy-baseline.schema.json").is_file()
        )
        wheel_fields = {
            "distribution", "version", "filename", "git_blob_sha", "sha256"
        }
        names = set()
        for wheel in self.baseline["wheels"]:
            self.assertEqual(set(wheel), wheel_fields)
            self.assertRegex(wheel["git_blob_sha"], r"^[0-9a-f]{40}$")
            self.assertRegex(wheel["sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn(wheel["filename"], names)
            names.add(wheel["filename"])

    def test_reviewed_wheel_identities_drive_docker_and_ignore_allowlist(self):
        dockerfile = (ROOT / "platform/testenv/Dockerfile.baseline").read_text()
        dockerignore = (ROOT / ".dockerignore").read_text()
        release = ROOT / self.baseline["release_path"]
        for wheel in self.baseline["wheels"]:
            filename = wheel["filename"]
            digest = wheel["sha256"]
            with self.subTest(filename=filename):
                self.assertIn(f"COPY {self.baseline['release_path']}/{filename}", dockerfile)
                self.assertIn(digest, dockerfile)
                self.assertIn(f"!{self.baseline['release_path']}/{filename}", dockerignore)
                path = release / filename
                if path.exists():
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_migration_targets_exist_and_fixture_is_disposable_only(self):
        for app, migration in self.baseline["migration_targets"].items():
            # Application directory names are registry-owned, not inferred from
            # Python module spelling.
            candidates = list(ROOT.glob(f"apps/*/src/{app}/migrations/{migration}.py"))
            self.assertEqual(len(candidates), 1, f"Missing baseline migration for {app}")
        seed = (ROOT / "platform/baselines/seed-v0.3.3.py").read_text()
        verification = (ROOT / "tests/upgrade/verify_v0_3_3.py").read_text()
        runner = (ROOT / "platform/testenv/run-upgrade.sh").read_text()
        for text in (seed, verification):
            self.assertIn('database.get("HOST") != "db"', text)
            self.assertIn("Refusing", text)
        self.assertIn("mariadb-dump", runner)
        self.assertIn("buh_restore", runner)
        self.assertGreaterEqual(runner.count("export MYSQL_PWD"), 3)
        self.assertGreaterEqual(len(re.findall(r"manage\.py migrate", runner)), 4)


if __name__ == "__main__":
    unittest.main()
