"""Execute the installed observer's actual embedded Python attempt reader."""

import copy
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ops.release import recovery_policy


ROOT = Path(__file__).resolve().parents[2]
RECOVERY = {
    "baseline_platform_version": "0.8.0",
    "policy_id": "production-v0.8.0-v0.8.1-gap-20260930",
    "purpose": "production-recovery",
    "release_count": 3,
    "sha256": "5d1fc6eff2ec52da3e7daa31ee60228a3b551fb7b58b9f51a5d42b447a7503c6",
}


class ObserverAttemptTests(unittest.TestCase):
    def setUp(self):
        source = (ROOT / "ops/buh-github-observe-root").read_text(encoding="utf-8")
        program = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        self.program = compile(program, "buh-github-observe-root:attempt", "exec")
        self.record = {
            "schema_version": 1, "attempt_id": "gh-36789079453-2",
            "operation": "preflight", "result": "success",
            "repository": "Fifty5D/B-UH-AllianceAuth",
            "platform_version": "0.8.2",
            "source_commit": "a8f60c83ddecf883cdefdd21f90dc426726bf5d3",
            "release_commit": "ff36151aa2065b0914a6cc2858a2284523fb098f",
            "manifest_sha256": "ee1120131935b78ba583072ad6c3b35ffb5585c98472f524214281174856f860",
            "started_at": "2026-09-30T18:08:05-05:00",
            "updated_at": "2026-09-30T18:08:54-05:00",
            "state": "candidate_validated", "failure": None,
            "failure_detail": None, "recovery": "Prior state restored.",
            "release_recovery": copy.deepcopy(RECOVERY), "verification": None,
            "rollback": {"result": "passed", "detail": "Prior state restored."},
            "cleanup": None,
        }

    def read(self, record, **details):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            metadata = {
                "st_mode": stat.S_IFREG | 0o600, "st_uid": 0,
                "st_size": path.stat().st_size, **details,
            }
            output = StringIO()
            with mock.patch.object(Path, "lstat", return_value=SimpleNamespace(**metadata)), \
                    mock.patch.object(sys, "argv", ["observer", str(path), record["attempt_id"]]), \
                    redirect_stdout(output):
                exec(self.program, {})
            lines = output.getvalue().splitlines()
            self.assertEqual(lines[0], "B-UH Platform v2 guarded attempt report")
            self.assertEqual(len(lines), 2)
            return json.loads(lines[1])

    def test_accepts_the_exact_v082_transition_in_preflight_and_deploy(self):
        for operation in ("preflight", "deploy"):
            with self.subTest(operation=operation):
                self.record["operation"] = operation
                self.assertEqual(self.read(self.record)["release_recovery"], RECOVERY)

    def test_pinned_transition_hash_matches_the_immutable_release_chain(self):
        raw = (ROOT / "releases/platform/v0.8.2/RELEASE.json").read_bytes()
        manifest = json.loads(raw)
        policy = recovery_policy.load_transition_policy(RECOVERY["policy_id"])
        transition = {
            "policy_id": policy["policy_id"],
            "policy_sha256": recovery_policy.transition_policy_sha256(policy),
            "purpose": RECOVERY["purpose"],
            "releases": [*policy["published_releases"], {
                "platform_version": manifest["platform_version"],
                "source_commit": manifest["source_commit"],
                "release_commit": self.record["release_commit"],
                "release_ref": "release/platform-v0.8.2",
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            }],
        }
        self.assertEqual(recovery_policy.recovery_digest(transition), RECOVERY["sha256"])
        self.assertEqual(len(transition["releases"]), RECOVERY["release_count"])
        self.assertEqual(transition["releases"][0]["platform_version"],
                         RECOVERY["baseline_platform_version"])

    def test_rejects_unknown_policy_and_altered_transition_data(self):
        changes = {
            "policy_id": "production-unreviewed-gap",
            "sha256": "0" * 64, "baseline_platform_version": "0.7.0",
            "purpose": "receiver-upgrade-preflight", "release_count": 4,
        }
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaisesRegex(
                SystemExit, "recovery evidence is unsafe"
            ):
                record = copy.deepcopy(self.record)
                record["release_recovery"][field] = value
                self.read(record)

    def test_rejects_correct_transition_hash_attached_to_another_release(self):
        for field in ("repository", "platform_version", "source_commit",
                      "release_commit", "manifest_sha256"):
            with self.subTest(field=field), self.assertRaisesRegex(
                SystemExit, "recovery evidence is unsafe"
            ):
                record = copy.deepcopy(self.record)
                record[field] = "mismatched"
                self.read(record)

    def test_rejects_extra_missing_or_wrong_type_recovery_fields(self):
        cases = [None, {**RECOVERY, "extra": "unreviewed"},
                 {key: value for key, value in RECOVERY.items() if key != "purpose"},
                 {**RECOVERY, "release_count": "3"}]
        for value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(
                SystemExit, "recovery evidence is unsafe"
            ):
                record = copy.deepcopy(self.record)
                record["release_recovery"] = value if value is not None else []
                self.read(record)

    def test_preserves_historical_policy_and_non_recovery_reports(self):
        for purpose in ("receiver-upgrade-preflight", "production-recovery"):
            record = copy.deepcopy(self.record)
            record["release_recovery"] = {
                **RECOVERY, "policy_id": "production-v0.5.6-published-gap-20260907",
                "baseline_platform_version": "0.5.6", "purpose": purpose,
                "sha256": "1" * 64,
            }
            self.assertEqual(self.read(record)["release_recovery"], record["release_recovery"])
        self.record["release_recovery"] = None
        self.assertIsNone(self.read(self.record)["release_recovery"])

    def test_existing_file_and_rollback_guards_remain_enforced(self):
        for metadata in ({"st_uid": 1000}, {"st_mode": stat.S_IFREG | 0o644},
                         {"st_mode": stat.S_IFLNK | 0o600}, {"st_size": 65537}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(
                SystemExit, "attempt report is unsafe"
            ):
                self.read(self.record, **metadata)
        self.record["rollback"]["result"] = "unverified"
        with self.assertRaisesRegex(SystemExit, "rollback evidence is unsafe"):
            self.read(self.record)
