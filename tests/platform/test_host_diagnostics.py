"""Private diagnostic retention and retrieval contracts, without a live host."""

import tempfile
import json
import subprocess
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ops.diagnostics import buh_host_diagnostics as diagnostics
from ops.diagnostics import buh_diagnostics_publish as publisher


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
        self.expected_patch = patch.object(
            diagnostics, "EXPECTED_SERVICES_FILE", root / "expected-services.json"
        )
        self.expected_patch.start()
        self.addCleanup(self.expected_patch.stop)
        (root / "expected-services.json").write_text(json.dumps({
            "schema_version": 1, "compose_project": "aa-docker",
            "services": {"allianceauth_gunicorn": 1, "allianceauth_worker": 5,
                         "allianceauth_worker_services": 1, "allianceauth_beat": 1,
                         "auth_mysql": 1, "redis": 1, "nginx": 1},
        }), encoding="utf-8")
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
        self.assertEqual(report["earliest_retained_log_at"], diagnostics.stamp(current))
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

    def test_resource_history_remains_visible_beyond_one_day(self):
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        for day, free in ((2, 55), (0, 40)):
            self.db.execute(
                "INSERT INTO resources(at,free_bytes,available_memory_bytes,load_1m) "
                "VALUES (?,?,?,?)",
                (diagnostics.stamp(current - timedelta(days=day)), free, 100, 1.0),
            )
        self.db.commit()
        report = diagnostics.create_report(self.db, [], current)
        self.assertEqual(len(report["resource_days_30d"]), 2)
        self.assertEqual(report["resource_days_30d"][0]["minimum_free_bytes"], 55)
        self.assertEqual(report["resource_days_30d"][1]["minimum_free_bytes"], 40)

    def test_atomic_report_and_readonly_database(self):
        current = datetime.now(UTC)
        report = diagnostics.create_report(self.db, [], current)
        diagnostics.atomic_report(report)
        self.assertTrue((diagnostics.ROOT / "buh-diagnostics-latest.json").is_file())
        self.assertEqual(report["resource_days_30d"], [])
        with closing(diagnostics.readonly_database()) as reader:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM logs").fetchone()[0], 0)

    def test_retention_prunes_old_records_and_reopen_keeps_recent_records(self):
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        redact = diagnostics.redactor()
        for offset in (31, 29):
            diagnostics.insert_log(
                self.db, "journal", "kernel",
                diagnostics.stamp(current - timedelta(days=offset)),
                f"warning from {offset} days ago", redact,
            )
        diagnostics.prune_history(
            self.db, diagnostics.stamp(current - timedelta(days=30))
        )
        self.db.commit()
        with closing(diagnostics.readonly_database()) as reader:
            rows = list(diagnostics.query(
                reader, current - timedelta(days=30), current,
                None, None, 10,
            ))
        self.assertEqual([row["message"] for row in rows], ["warning from 29 days ago"])

    def test_docker_capture_keeps_normal_context_and_reports_missing_source(self):
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        container = {"name": "allianceauth_beat", "service": "allianceauth_beat",
                     "id": "synthetic123"}
        lines = [
            "2026-09-27T12:00:00.000001Z task archive started",
            "2026-09-27T12:00:01.000001Z ERROR archive failed token=private-value",
        ]
        with patch.object(diagnostics, "iter_command_lines", return_value=iter(lines)):
            diagnostics.collect_docker(self.db, [container], diagnostics.redactor(), current)
        self.db.commit()
        rows = list(diagnostics.query(
            self.db, current, current + timedelta(minutes=1),
            "allianceauth_beat", None, 10,
        ))
        self.assertEqual(len(rows), 2)
        self.assertIn("task archive started", rows[0]["message"])
        self.assertNotIn("private-value", rows[1]["message"])
        report = diagnostics.create_report(self.db, [dict(container, state="running")], current)
        self.assertEqual(report["sources"]["docker"]["records"], 2)
        self.assertFalse(report["sources"]["docker"]["stale"])
        self.assertTrue(report["sources"]["journal"]["stale"])
        self.assertEqual(report["status"], "unknown")

    def test_compose_labels_discover_actual_beat_and_missing_replicas_degrade_report(self):
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        rows = [json.dumps({"ID": f"id{n}", "Names": name, "State": "running",
                            "Status": "Up"}) for n, name in enumerate((
            "allianceauth_gunicorn", "aa-docker-allianceauth_worker-1",
            "aa-docker-allianceauth_worker_services-1", "aa-docker-allianceauth_beat-1",
            "aa-docker-auth_mysql-1", "aa-docker-redis-1", "aa-docker-nginx-1",
        ))]
        details = []
        for name in ("allianceauth_gunicorn", "allianceauth_worker",
                     "allianceauth_worker_services", "allianceauth_beat",
                     "auth_mysql", "redis", "nginx"):
            labels = ({"com.docker.compose.project": "aa-docker",
                       "com.docker.compose.service": name}
                      if name != "allianceauth_gunicorn" else {})
            details.append(json.dumps(labels) + "|" + json.dumps({
                "StartedAt": diagnostics.stamp(current), "Health": {"Status": "healthy"},
                "OOMKilled": False, "ExitCode": 0,
            }) + "|0")
        with patch.object(diagnostics, "read_command",
                          side_effect=["\n".join(rows), "\n".join(details)]):
            containers = diagnostics.docker_containers()
        self.assertIn("allianceauth_beat", {item["service"] for item in containers})
        for source in ("docker", "journal", "docker-event", "app", "resources"):
            diagnostics.set_meta(self.db, "source:" + source + ":last_success",
                                 diagnostics.stamp(current))
        self.db.commit()
        report = diagnostics.create_report(self.db, containers, current)
        self.assertEqual(report["service_inventory"]["mismatched"], {
            "allianceauth_worker": {"expected": 5, "running": 1}
        })
        self.assertEqual(report["status"], "degraded")

        without_beat = [item for item in containers if item["service"] != "allianceauth_beat"]
        report = diagnostics.create_report(self.db, without_beat, current)
        self.assertEqual(report["service_inventory"]["mismatched"]["allianceauth_beat"],
                         {"expected": 1, "running": 0})
        self.assertEqual(report["status"], "degraded")

    def test_private_snapshot_has_redacted_evidence_and_prunes_expired_hours(self):
        if not __import__("shutil").which("git"):
            self.skipTest("Git is required for private transport test")
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        diagnostics.insert_log(
            self.db, "docker", "allianceauth_worker_beat", diagnostics.stamp(current),
            "ERROR token=private-value", diagnostics.redactor(),
        )
        self.db.commit()
        diagnostics.atomic_report(diagnostics.create_report(self.db, [], current))
        remote = diagnostics.ROOT / "private-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True)
        stage = diagnostics.ROOT / "private-stage"
        with (
            patch.object(publisher, "HISTORY", diagnostics.ROOT),
            patch.object(publisher, "STAGING", stage),
            patch.object(publisher, "REMOTE", str(remote)),
            patch.object(publisher, "MINIMUM_FREE_BYTES", 0),
            patch.object(publisher, "utcnow", return_value=current + timedelta(minutes=1)),
        ):
            publisher.publish()
        old_commit = subprocess.run(
            ["git", "rev-parse", "refs/heads/data"], cwd=stage / "repo",
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        filename = "evidence/2026-09-27/12/part-000.jsonl"
        result = subprocess.run(
            ["git", "--git-dir", str(remote), "show", "refs/heads/data:" + filename],
            check=True, capture_output=True, text=True,
        )
        self.assertIn("<redacted>", result.stdout)
        self.assertNotIn("private-value", result.stdout)
        latest = subprocess.run(
            ["git", "--git-dir", str(remote), "show",
             "refs/heads/data:buh-diagnostics-latest.json"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(latest.stdout)["evidence"]["ref"], "data")
        diagnostics.prune_history(
            self.db, diagnostics.stamp(current + timedelta(days=31) - diagnostics.RETENTION)
        )
        self.db.commit()
        with (
            patch.object(publisher, "HISTORY", diagnostics.ROOT),
            patch.object(publisher, "STAGING", stage),
            patch.object(publisher, "REMOTE", str(remote)),
            patch.object(publisher, "MINIMUM_FREE_BYTES", 0),
            patch.object(publisher, "utcnow", return_value=current + timedelta(days=31)),
        ):
            publisher.publish()
        missing = subprocess.run(
            ["git", "--git-dir", str(remote), "cat-file", "-e",
             "refs/heads/data:" + filename], capture_output=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        expired_local_object = subprocess.run(
            ["git", "cat-file", "-e", old_commit + ":" + filename],
            cwd=stage / "repo", capture_output=True,
        )
        self.assertNotEqual(expired_local_object.returncode, 0)

    def test_late_recovered_log_republishes_its_original_hour(self):
        if not __import__("shutil").which("git"):
            self.skipTest("Git is required for private transport test")
        current = datetime(2026, 9, 27, 12, tzinfo=UTC)
        diagnostics.insert_log(
            self.db, "docker", "allianceauth_worker", diagnostics.stamp(current),
            "ordinary recent work", diagnostics.redactor(),
        )
        self.db.commit()
        diagnostics.atomic_report(diagnostics.create_report(self.db, [], current))
        remote = diagnostics.ROOT / "private-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True)
        stage = diagnostics.ROOT / "private-stage"
        with (
            patch.object(publisher, "HISTORY", diagnostics.ROOT),
            patch.object(publisher, "STAGING", stage),
            patch.object(publisher, "REMOTE", str(remote)),
            patch.object(publisher, "MINIMUM_FREE_BYTES", 0),
            patch.object(publisher, "utcnow", return_value=current + timedelta(hours=1)),
        ):
            publisher.publish()

        # The highest ID was removed after publication. AUTOINCREMENT must
        # still give the recovered previous-day record a new insertion ID.
        self.db.execute("DELETE FROM logs WHERE id=(SELECT MAX(id) FROM logs)")
        diagnostics.insert_log(
            self.db, "journal", "kernel",
            diagnostics.stamp(current - timedelta(days=1)),
            "ERROR recovered from collector outage", diagnostics.redactor(),
        )
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT MAX(id) FROM logs").fetchone()[0], 2)
        with (
            patch.object(publisher, "HISTORY", diagnostics.ROOT),
            patch.object(publisher, "STAGING", stage),
            patch.object(publisher, "REMOTE", str(remote)),
            patch.object(publisher, "MINIMUM_FREE_BYTES", 0),
            patch.object(publisher, "utcnow", return_value=current + timedelta(hours=1, minutes=5)),
        ):
            publisher.publish()
        previous_day = "evidence/2026-09-26/12/part-000.jsonl"
        recovered = subprocess.run(
            ["git", "--git-dir", str(remote), "show", "refs/heads/data:" + previous_day],
            check=True, capture_output=True, text=True,
        )
        self.assertIn("recovered from collector outage", recovered.stdout)
        index = subprocess.run(
            ["git", "--git-dir", str(remote), "show", "refs/heads/data:evidence-index.json"],
            check=True, capture_output=True, text=True,
        )
        self.assertIn("2026-09-26", json.loads(index.stdout)["days"])


if __name__ == "__main__":
    unittest.main()
