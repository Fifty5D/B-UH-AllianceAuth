"""Exercise the real receiver against the disposable CI MariaDB container."""

from __future__ import annotations

import dataclasses
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import DockerHost
from tests.deploy.test_docker_host import make_bundle, make_config


class SnapshotBackupRehearsal(unittest.TestCase):
    def setUp(self):
        self.container = os.environ["BUH_TEST_DATABASE_CONTAINER"]
        if re.fullmatch(r"[0-9a-f]{64}", self.container) is None:
            raise RuntimeError("Expected the disposable Compose database container ID")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config = dataclasses.replace(make_config(self.root), health_interval_seconds=1)
        self.host = DockerHost(config)
        self.database = "buh_snapshot_regression"
        self.host.backup_path = self.root / "backup"
        self.host.backup_path.mkdir()
        self.host._database_query(
            self.container,
            f"DROP DATABASE IF EXISTS {self.database}; CREATE DATABASE {self.database}; "
            f"CREATE TABLE {self.database}.django_migrations (id INT PRIMARY KEY); "
            f"CREATE TABLE {self.database}.buh_evidence (id INT PRIMARY KEY); "
            f"INSERT INTO {self.database}.django_migrations VALUES (1); "
            f"INSERT INTO {self.database}.buh_evidence VALUES (1)",
        )
        self.addCleanup(
            self.host._database_query, self.container, f"DROP DATABASE {self.database}"
        )
        self.finished = threading.Event()
        self.writer_errors: list[BaseException] = []
        self.writer: threading.Thread | None = None

    def start_writer(self):
        def write():
            try:
                self.host._database_query(
                    self.container,
                    f"INSERT INTO {self.database}.buh_evidence VALUES (2)",
                )
            except BaseException as error:
                self.writer_errors.append(error)
            finally:
                self.finished.set()

        self.writer = threading.Thread(target=write, daemon=True)
        self.writer.start()

    def require_writer_completed(self):
        self.assertTrue(self.finished.wait(10), "write did not resume after lock release")
        self.assertEqual(self.writer_errors, [])
        if self.writer is not None:
            self.writer.join(timeout=1)

    def run_backup(self):
        # The source database is an isolated synthetic fixture, not Auth's live
        # discovery target. Dump, lock, restore and comparison are real code.
        with mock.patch.object(self.host, "_database_container", return_value=self.container), \
             mock.patch.object(self.host, "_application_database", return_value=self.database):
            return self.host.backup(make_bundle(self.root))

    def test_live_write_after_dump_waits_and_verified_restore_succeeds(self):
        dump = self.host._run_to_file

        def capture(*args, **kwargs):
            dump(*args, **kwargs)
            # Reproduce a scheduled capture immediately after the dump snapshot.
            self.start_writer()
            self.assertFalse(self.finished.wait(.3), "snapshot did not block concurrent write")

        with mock.patch.object(self.host, "_run_to_file", side_effect=capture):
            backup = self.run_backup()
        self.require_writer_completed()
        self.assertEqual(backup["evidence_rows"], 2)
        self.assertEqual(
            self.host._evidence_counts(self.container, self.database)["buh_evidence"], 2
        )
        self.assertTrue((self.host.backup_path / "database.sql.gz").is_file())

    def test_genuine_restore_mismatch_still_fails_closed(self):
        counts = self.host._evidence_counts

        def corrupt_restored_count(container, database):
            if container != self.container:
                self.host._database_query(container, f"DELETE FROM {database}.buh_evidence")
            return counts(container, database)

        with mock.patch.object(self.host, "_evidence_counts", side_effect=corrupt_restored_count):
            with self.assertRaisesRegex(DeploymentError, "Restored accounting evidence differs"):
                self.run_backup()
        self.assertFalse((self.host.backup_path / "BACKUP.json").exists())

    def test_capture_error_releases_the_lock(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic capture error"):
            with self.host._database_read_lock(self.container):
                self.start_writer()
                self.assertFalse(self.finished.wait(.3))
                raise RuntimeError("synthetic capture error")
        self.require_writer_completed()

    def test_watchdog_expiry_releases_writes_and_rejects_snapshot(self):
        with mock.patch("ops.deploy.docker_host.BACKUP_LOCK_SECONDS", 2):
            with self.assertRaisesRegex(DeploymentError, "expired or disconnected"):
                with self.host._database_read_lock(self.container):
                    self.start_writer()
                    self.assertFalse(self.finished.wait(.3))
                    # Exercise the actual in-container watchdog, not a mocked
                    # process status. No production operation runs in this test.
                    time.sleep(2.2)
                    self.require_writer_completed()


if __name__ == "__main__":
    unittest.main(verbosity=2)
