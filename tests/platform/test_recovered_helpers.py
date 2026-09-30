"""Ensure the two active helper applications changed only their packaging contract."""

from __future__ import annotations

import hashlib
import tomllib
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RecoveredHelperSourceTests(unittest.TestCase):
    def test_existing_vps_host_agent_source_is_pinned(self):
        agent = ROOT / "apps/vps-health/src/host-agent/buh-vps-health-agent.py"
        self.assertEqual(
            hashlib.sha256(agent.read_bytes()).hexdigest(),
            "a4b37e4e38dd5b683afec767a9bb1c7973e137b87ceeaad7f6e0ed7378ad4f40",
        )

    def test_recovered_source_matches_installed_wheels(self):
        cases = (
            (
                "memberaudit-autoreg",
                "buh_memberaudit_autoreg",
                "aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl",
                "e26b225324996c679dddae29d45abf1afce573494fa0f293e755f2072631f0ce",
            ),
            (
                "vps-health",
                "buh_vps_health",
                "aa_buh_vps_health-0.2.0-py3-none-any.whl",
                "b2f10c7b25d7134e83c32abb75fcf85198a4de829a59d4da81d199bebfce67dc",
            ),
        )
        for app, package, filename, digest in cases:
            with self.subTest(app=app):
                wheel = ROOT / "tests/deploy/fixtures" / filename
                self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), digest)
                source = ROOT / "apps" / app / "src" / package
                with zipfile.ZipFile(wheel) as archive:
                    members = {
                        name.removeprefix(f"{package}/"): archive.read(name)
                        for name in archive.namelist()
                        if name.startswith(f"{package}/")
                    }
                actual = {
                    str(path.relative_to(source)).replace("\\", "/"): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                }
                self.assertEqual(actual, members)

                with (ROOT / "apps" / app / "pyproject.toml").open("rb") as stream:
                    dependencies = tomllib.load(stream)["project"]["dependencies"]
                self.assertIn("allianceauth>=5.4,<5.5", dependencies)


if __name__ == "__main__":
    unittest.main()
