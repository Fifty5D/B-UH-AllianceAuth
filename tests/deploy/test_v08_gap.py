"""Exercise the exact v0.8.0 host to v0.8.2 release-gap contract."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ops.deploy import contracts, request_archive
from ops.deploy.docker_host import DockerHost
from ops.release import buh_release, recovery_policy


ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "releases/platform"
POLICY = recovery_policy.load_transition_policy(recovery_policy.V08_GAP_POLICY_ID)
BASE = json.loads((RELEASES / "v0.8.0/RELEASE.json").read_text())
IMMEDIATE = json.loads((RELEASES / "v0.8.1/RELEASE.json").read_text())


def target_manifest() -> dict:
    target = copy.deepcopy(IMMEDIATE)
    target["platform_version"] = "0.8.2"
    target["source_commit"] = "a" * 40
    target["previous_release"] = {
        "manifest_sha256": POLICY["published_releases"][1]["manifest_sha256"],
        "platform_version": "0.8.1",
        "source_commit": POLICY["published_releases"][1]["source_commit"],
    }
    target["deployment_recovery"] = recovery_policy.manifest_recovery(POLICY)
    return target


def request_for(target: dict) -> dict:
    target_identity = {
        "manifest_sha256": "c" * 64,
        "platform_version": target["platform_version"],
        "release_commit": "b" * 40,
        "release_ref": f"release/platform-v{target['platform_version']}",
        "source_commit": target["source_commit"],
    }
    return {
        "schema_version": 2,
        "mode": "preflight",
        "repository": "Fifty5D/B-UH-AllianceAuth",
        "release_commit": target_identity["release_commit"],
        "release_ref": target_identity["release_ref"],
        "platform_version": target_identity["platform_version"],
        "manifest_sha256": target_identity["manifest_sha256"],
        "workflow_run_id": "36763568003",
        "workflow_run_attempt": 1,
        "recovery_transition": {
            "policy_id": POLICY["policy_id"],
            "policy_sha256": recovery_policy.transition_policy_sha256(POLICY),
            "purpose": "production-recovery",
            "releases": [*POLICY["published_releases"], target_identity],
        },
    }


class V08GapTests(unittest.TestCase):
    def test_published_lineage_and_target_compatibility_are_exact(self) -> None:
        self.assertEqual(
            [buh_release.sha256_file(RELEASES / f"v{version}/RELEASE.json") for version in ("0.8.0", "0.8.1")],
            [item["manifest_sha256"] for item in POLICY["published_releases"]],
        )
        target = target_manifest()
        recovery_policy.validate_v08_gap_compatibility([BASE, IMMEDIATE], target)
        changed = copy.deepcopy(target)
        changed["compatibility"]["values"]["applications"]["vps-health"]["module"] = "unexpected"
        with self.assertRaises(recovery_policy.RecoveryPolicyError):
            recovery_policy.validate_v08_gap_compatibility([BASE, IMMEDIATE], changed)
        changed = copy.deepcopy(target)
        changed["platform_version"] = "0.8.3"
        with self.assertRaises(recovery_policy.RecoveryPolicyError):
            recovery_policy.validate_v08_gap_compatibility([BASE, IMMEDIATE], changed)

    def test_request_archive_receiver_and_live_predecessor_agree(self) -> None:
        target = target_manifest()
        data = request_for(target)
        self.assertEqual(
            request_archive._recovery_transition(
                manifest=target,
                target=data["recovery_transition"]["releases"][-1],
                bootstrap_recovery=False,
            ),
            data["recovery_transition"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "REQUEST.json").write_bytes(recovery_policy.canonical_json_bytes(data))
            lineage = root / "lineage"
            lineage.mkdir()
            for version in ("0.8.0", "0.8.1"):
                (lineage / f"v{version}.RELEASE.json").write_bytes(
                    (RELEASES / f"v{version}/RELEASE.json").read_bytes()
                )
            request = contracts.DeploymentRequest.load(root / "REQUEST.json")
            self.assertEqual(len(contracts._load_recovery_lineage(root, request, target, buh_release)), 2)
            host = DockerHost.__new__(DockerHost)
            current = {
                key: POLICY["baseline"][key]
                for key in ("platform_version", "release_commit", "source_commit", "manifest_sha256")
            }
            host._load_current = lambda: current
            bundle = contracts.ValidatedBundle(root, root / "release", request, target, {})
            host._validate_release_transition(bundle)
            current["manifest_sha256"] = "d" * 64
            with self.assertRaisesRegex(contracts.DeploymentError, "Recovery baseline"):
                host._validate_release_transition(bundle)

            tampered = copy.deepcopy(data)
            tampered["recovery_transition"]["releases"][0]["release_commit"] = "d" * 40
            (root / "REQUEST.json").write_bytes(recovery_policy.canonical_json_bytes(tampered))
            with self.assertRaises(contracts.DeploymentError):
                contracts.DeploymentRequest.load(root / "REQUEST.json")


if __name__ == "__main__":
    unittest.main()
