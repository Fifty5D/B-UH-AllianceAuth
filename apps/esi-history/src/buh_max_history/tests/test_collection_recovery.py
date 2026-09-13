"""Regressions for abandoned jobs, failed transfers and incomplete history."""

import gzip
import io
import json
import select
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils.timezone import now

from buh_max_history.capture import archive_esi_result, reset_configuration_cache
from buh_max_history.job_lock import public_archive_lock
from buh_max_history.models import (
    ArchiveConfiguration,
    ArchiveJob,
    ArchiveSnapshot,
    ArchiveStream,
    PublicArchiveFile,
    PublicCatalogIndex,
    PublicDataset,
)
from buh_max_history.public_archive import (
    DownloadBudgetExceeded,
    _catalog_file,
    _download_file,
    _remote_size,
    catalog_dataset,
    sync_public_archive,
    verify_archive_files,
)
from buh_max_history.tasks import _run_job, scheduled_public_archive_sync


class Response(io.BytesIO):
    def __init__(self, content=b"abc", headers=None):
        super().__init__(content)
        self.headers = headers or {}
        self.status = 200


class CollectionTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = override_settings(BUH_ESI_ARCHIVE_ROOT=directory.name)
        config.enable()
        self.addCleanup(config.disable)
        self.addCleanup(reset_configuration_cache)
        cache.clear()
        reset_configuration_cache()
        self.config = ArchiveConfiguration.objects.create(
            singleton_id=1,
            minimum_free_gib=5,
            public_max_files_per_run=3,
            public_max_gib_per_run=1,
            discover_public_datasets=False,
        )
        disk = patch(
            "buh_max_history.public_archive.shutil.disk_usage",
            return_value=SimpleNamespace(free=100 * 1024**3),
        )
        disk.start()
        self.addCleanup(disk.stop)
        self.dataset = self.make_dataset("wars")

    def make_dataset(self, slug):
        return PublicDataset.objects.create(
            name=slug, slug=slug, index_url=f"https://data.everef.net/{slug}/index.json"
        )

    def file(self, name, **kwargs):
        dataset = kwargs.pop("dataset", self.dataset)
        return PublicArchiveFile.objects.create(
            dataset=dataset,
            source_url=f"https://data.everef.net/{dataset.slug}/{name}",
            relative_path=f"public/{dataset.slug}/{name}",
            remote_size=3,
            **kwargs,
        )

    @patch(
        "buh_max_history.tasks.sync_public_archive", return_value={"downloaded_files": 1}
    )
    def test_hourly_schedule_recovers_abandoned_running_and_queued_jobs(self, sync):
        stale = ArchiveJob.objects.create(
            kind="SYNC", status="RUNNING", started_at=now() - timedelta(days=9)
        )
        queued = ArchiveJob.objects.create(kind="SYNC")
        ArchiveJob.objects.filter(pk=queued.pk).update(
            requested_at=now() - timedelta(days=9)
        )
        partial = self.file("partial", status="DOWNLOADING")
        result = scheduled_public_archive_sync()
        self.assertTrue(result["ok"])
        stale.refresh_from_db()
        queued.refresh_from_db()
        partial.refresh_from_db()
        self.assertEqual(
            (stale.status, queued.status, partial.status), ("FAILED", "FAILED", "PENDING")
        )
        sync.assert_called_once()

    @patch("buh_max_history.tasks.sync_public_archive")
    def test_live_lock_wins_over_even_very_old_running_row(self, sync):
        live = ArchiveJob.objects.create(
            kind="SYNC", status="RUNNING", started_at=now() - timedelta(days=9)
        )
        with public_archive_lock() as acquired:
            self.assertTrue(acquired)
            self.assertIn("skipped", scheduled_public_archive_sync())
        live.refresh_from_db()
        self.assertEqual(live.status, "RUNNING")
        sync.assert_not_called()

    def test_kernel_releases_lock_when_worker_is_killed(self):
        # Real separate process, not a mocked age check or fake cache lock.
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()",
                str(self.root / ".public-archive.lock"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0])
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with public_archive_lock() as acquired:
                self.assertFalse(acquired)
            child.kill()
            child.wait(timeout=5)
            with public_archive_lock() as acquired:
                self.assertTrue(acquired)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    @patch(
        "buh_max_history.tasks.sync_public_archive",
        return_value={"errors": [{"error": "offline"}], "downloaded_files": 1},
    )
    def test_partial_failures_are_visible_and_completed_job_is_not_replayed(self, sync):
        job = ArchiveJob.objects.create(kind="SYNC")
        self.assertFalse(_run_job(job)["ok"])
        job.refresh_from_db()
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(_run_job(job)["skipped"], "job_already_started")
        self.assertEqual(sync.call_count, 1)

    @patch("buh_max_history.tasks.sync_public_archive", return_value={})
    def test_management_command_uses_same_process_lock(self, sync):
        with public_archive_lock():
            call_command("buh_archive_public_sync", "--no-catalog", stdout=io.StringIO())
        sync.assert_not_called()

    @patch("buh_max_history.public_archive._download_file", side_effect=OSError("offline"))
    def test_failed_attempts_are_bounded_and_receive_delayed_retries(self, download):
        for i in range(20):
            self.file(f"{i:02}")
        result = sync_public_archive(catalog_first=False)
        self.assertEqual(result["attempted_files"], 3)
        self.assertEqual(download.call_count, 3)
        self.assertEqual(
            PublicArchiveFile.objects.filter(status="FAILED", retry_at__gt=now()).count(), 3
        )
        self.dataset.refresh_from_db()
        self.assertIsNone(self.dataset.last_sync_at)

    @patch("buh_max_history.public_archive._download_file", return_value=3)
    def test_due_retry_gets_a_turn_even_with_a_large_pending_backlog(self, download):
        retry = self.file(
            "retry", status="FAILED", retry_at=now() - timedelta(minutes=1), failure_count=1
        )
        for i in range(10):
            self.file(f"fresh-{i}")
        sync_public_archive(catalog_first=False)
        self.assertIn(retry.pk, [call.args[0].pk for call in download.call_args_list])

    @patch("buh_max_history.public_archive._download_file", side_effect=OSError("offline"))
    def test_no_dataset_is_starved_by_high_priority_backlog(self, download):
        for i in range(12):
            self.file(f"first-{i}")
        others = [self.make_dataset("market"), self.make_dataset("contracts")]
        for dataset in others:
            self.file("one", dataset=dataset)
        sync_public_archive(catalog_first=False)
        self.assertEqual(
            {call.args[0].dataset_id for call in download.call_args_list},
            {self.dataset.pk, *(d.pk for d in others)},
        )

    @patch(
        "buh_max_history.public_archive._download_file",
        side_effect=HTTPError(
            "https://data.everef.net/x", 429, "limit", {"Retry-After": "7200"}, None
        ),
    )
    def test_rate_limit_pauses_all_datasets_and_respects_retry_after(self, download):
        self.file("one")
        self.file("two", dataset=self.make_dataset("other"))
        result = sync_public_archive(catalog_first=False)
        self.assertEqual(result["blocked"], "remote_rate_limit")
        self.assertEqual(download.call_count, 1)
        self.assertEqual(
            sync_public_archive(catalog_first=False)["blocked"], "remote_rate_limit"
        )
        self.assertEqual(download.call_count, 1)

    def test_storage_guard_stops_before_catalog_and_downloads(self):
        with (
            patch(
                "buh_max_history.public_archive.shutil.disk_usage",
                return_value=SimpleNamespace(free=1),
            ),
            patch("buh_max_history.public_archive.catalog_enabled_datasets") as catalog,
        ):
            self.assertEqual(sync_public_archive()["blocked"], "minimum_free_space")
            catalog.assert_not_called()

    def test_catalog_and_http_validator_formats_do_not_redownload_unchanged_files(self):
        record = self.file(
            "one",
            status="STORED",
            etag='"abc"',
            remote_modified="Sun, 13 Sep 2026 00:00:00 GMT",
        )
        _created, changed = _catalog_file(
            self.dataset,
            record.source_url,
            {"size": 3, "etag": "abc", "last_modified": "2026-09-13T00:00:00Z"},
        )
        record.refresh_from_db()
        self.assertEqual(changed, 0)
        self.assertEqual(record.status, "STORED")
        _created, changed = _catalog_file(
            self.dataset, record.source_url, {"size": 3, "etag": "different"}
        )
        self.assertEqual(changed, 1)
        record.refresh_from_db()
        self.assertEqual(record.status, "PENDING")

    def test_remote_size_accepts_alternative_metadata_keys(self):
        self.assertEqual(_remote_size({"bytes": 42}), 42)
        self.assertEqual(_remote_size({"size": "invalid", "content_length": 42}), 42)

    @patch("buh_max_history.public_archive._request_json")
    def test_large_catalog_resumes_after_entry_limit_without_skipping_files(self, request):
        request.return_value = (
            {"files": [{"name": str(i), "size": 3} for i in range(5)]},
            {},
        )
        with patch("buh_max_history.public_archive.MAX_CATALOG_ENTRIES_PER_RUN", 2):
            catalog_dataset(self.dataset, max_indexes=1)
            self.assertEqual(PublicCatalogIndex.objects.get().cursor, 2)
            catalog_dataset(self.dataset, max_indexes=1)
            self.assertEqual(PublicCatalogIndex.objects.get().cursor, 4)
            catalog_dataset(self.dataset, max_indexes=1)
        self.assertEqual(PublicArchiveFile.objects.count(), 5)
        self.assertEqual(PublicCatalogIndex.objects.get().status, "CATALOGED")

    @patch("buh_max_history.public_archive._request_json")
    def test_changed_index_resets_cursor_instead_of_skipping_new_entries(self, request):
        request.return_value = (
            {"files": [{"name": str(i), "size": 3} for i in range(3)]},
            {},
        )
        with patch("buh_max_history.public_archive.MAX_CATALOG_ENTRIES_PER_RUN", 2):
            catalog_dataset(self.dataset, max_indexes=1)
            request.return_value[0]["files"].insert(0, {"name": "new", "size": 3})
            catalog_dataset(self.dataset, max_indexes=1)
        self.assertTrue(
            PublicArchiveFile.objects.filter(source_url__endswith="/new").exists()
        )

    def test_dataset_retry_rotation_persists_between_small_batches(self):
        self.config.public_max_files_per_run = 1
        self.config.save()
        for i in range(8):
            self.file(f"pending-{i}")
        retry = self.file(
            "due-retry", status="FAILED", retry_at=now() - timedelta(minutes=1)
        )
        with patch(
            "buh_max_history.public_archive._download_file", return_value=3
        ) as download:
            for _ in range(3):
                sync_public_archive(catalog_first=False)
        self.assertEqual(download.call_args_list[-1].args[0].pk, retry.pk)

    def test_lost_database_lock_cannot_publish_downloaded_file(self):
        from buh_max_history.job_lock import ArchiveLockLost

        record = self.file("protected")
        target = self.root / record.relative_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        with (
            patch("buh_max_history.public_archive.urlopen", return_value=Response(b"abc")),
            patch(
                "buh_max_history.public_archive.assert_public_archive_lock",
                side_effect=ArchiveLockLost("lost"),
            ),
        ):
            with self.assertRaises(ArchiveLockLost):
                _download_file(record, 0, budget_bytes=100, deadline=time.monotonic() + 5)
        self.assertEqual(target.read_bytes(), b"old")

    def test_download_deadline_does_not_replace_existing_payload(self):
        record = self.file("timeout")
        with patch("buh_max_history.public_archive.urlopen", return_value=Response(b"abc")):
            with self.assertRaises(TimeoutError):
                _download_file(record, 0, budget_bytes=100, deadline=time.monotonic() - 1)
        self.assertFalse((self.root / record.relative_path).exists())

    def test_failed_download_preserves_existing_payload_and_charges_partial_bytes(self):
        record = self.file("one")
        target = self.root / record.relative_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        with patch(
            "buh_max_history.public_archive.urlopen", return_value=Response(b"larger")
        ):
            with self.assertRaises(DownloadBudgetExceeded) as caught:
                _download_file(record, 0, budget_bytes=4, deadline=time.monotonic() + 5)
        self.assertGreaterEqual(caught.exception.archive_bytes, 4)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(list(target.parent.glob("*.download-part")))

    def test_complete_download_keeps_catalog_validators(self):
        record = self.file("one", etag="abc", remote_modified="2026-09-13T00:00:00Z")
        with patch(
            "buh_max_history.public_archive.urlopen",
            return_value=Response(
                b"abc", {"ETag": '"abc"', "Last-Modified": "Sun, 13 Sep 2026 00:00:00 GMT"}
            ),
        ):
            self.assertEqual(
                _download_file(record, 0, budget_bytes=100, deadline=time.monotonic() + 5),
                3,
            )
        record.refresh_from_db()
        self.assertEqual(
            (record.status, record.etag, record.remote_modified),
            ("STORED", "abc", "2026-09-13T00:00:00Z"),
        )

    def test_verification_rotates_and_requeues_missing_files(self):
        first = self.file("one", status="STORED", stored_bytes=3)
        second = self.file("two", status="STORED", stored_bytes=3)
        verify_archive_files(limit=1)
        first.refresh_from_db()
        self.assertEqual(first.status, "PENDING")
        verify_archive_files(limit=1)
        second.refresh_from_db()
        self.assertEqual(second.status, "PENDING")

    def test_capture_repairs_missing_payload_and_keeps_explicit_page_identity(self):
        operation = SimpleNamespace(
            token=None,
            method="GET",
            url="/assets/",
            operation=SimpleNamespace(operationId="GetAssets"),
            _kwargs={},
        )
        response = SimpleNamespace(content=b"[1]", status_code=200, headers={})
        archive_esi_result(operation, [1], response, {"page": 1})
        snapshot = ArchiveSnapshot.objects.get()
        path = self.root / snapshot.relative_path
        path.unlink()
        archive_esi_result(operation, [1], response, {"page": 1})
        self.assertEqual(gzip.decompress(path.read_bytes()), b"[1]")
        archive_esi_result(
            operation, [1], response, {"page": 2, "token": "synthetic-secret"}
        )
        self.assertEqual(ArchiveStream.objects.count(), 2)
        self.assertEqual(
            {
                row["page"]
                for row in ArchiveStream.objects.values_list("safe_parameters", flat=True)
            },
            {1, 2},
        )
        self.assertNotIn(
            "synthetic-secret",
            json.dumps(
                list(ArchiveStream.objects.values_list("safe_parameters", flat=True))
            ),
        )
