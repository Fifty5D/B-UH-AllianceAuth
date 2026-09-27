"""Private diagnostic retention and retrieval contracts, without a live host."""

import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ops.diagnostics import buh_host_diagnostics as diagnostics


class HostDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.root_patch = patch.object(diagnostics, "ROOT", root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        redactor = Path(__file__).resolve().parents[2] / "ops/buh-redact-diagnostics.py"
        self.redactor_patch = patch.object(diagnostics, "REDACTOR", redactor)
        self.redactor_patch.start()
        self.addCleanup(self.redactor_patch.stop)
        self.db = diagnostics.database()
        self.addCleanup(self.db.close)

    def test_report_and_history_are_redacted_and_stale_sources_are_visible(self):
        current = datetime(2026, 9, 27, 21, 0, tzinfo=UTC)
        diagnostics.insert_log(
            self.db, "docker", "allianceauth_worker", diagnostics.stamp(current),
            "ERROR Authorization: Bearer an-actual-sensitive-token", diagnostics.redactor(),
        )
        diagnostics.gap(self.db, "docker", "Container disappeared before final read")
        self.db.commit()
        report = diagnostics.create_report(self.db, [], current + timedelta(minutes=1))
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["sources"]["docker"]["stale"])
        self.assertGreaterEqual(report["coverage_gap_count"], 1)
        self.assertNotIn("an-actual-sensitive-token", str(report))
        rows = list(diagnostics.query(
            self.db, current - timedelta(minutes=1), current + timedelta(minutes=1),
            "allianceauth_worker", "ERROR", 10,
        ))
        self.assertEqual(len(rows), 1)
        self.assertIn("<redacted>", rows[0]["message"])
        self.assertNotIn("an-actual-sensitive-token", rows[0]["message"])
        oauth = diagnostics.redactor().redact(
            "GET /sso/callback?code=one-time-value&session_state=private-value"
        )
        self.assertNotIn("one-time-value", oauth)
        self.assertNotIn("private-value", oauth)

    def test_timestamp_range_and_row_limit_reject_ambiguous_or_excessive_queries(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            diagnostics.parse_stamp("2026-09-27T12:00:00")
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        for number in range(3):
            diagnostics.insert_log(
                self.db, "docker", "aa-docker-nginx-1",
                diagnostics.stamp(current + timedelta(seconds=number)),
                f"ordinary line {number}", diagnostics.redactor(),
            )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "row limit"):
            list(diagnostics.query(
                self.db, current, current + timedelta(minutes=1), None, None, 2,
            ))
        rows = list(diagnostics.query(
            self.db, current, current + timedelta(minutes=1), None, "line 2", 1,
        ))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "ordinary line 2")

    def test_atomic_report_and_readonly_database(self):
        current = datetime.now(UTC)
        report = diagnostics.create_report(self.db, [], current)
        diagnostics.atomic_report(report)
        self.assertTrue((diagnostics.ROOT / "buh-diagnostics-latest.json").is_file())
        with closing(diagnostics.readonly_database()) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
