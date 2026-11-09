"""Storage reserve switches must not disable capture or work limits."""

import gzip
import io
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.forms import modelform_factory
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils.timezone import now

from buh_max_history import capture, collection, local_history, public_archive
from buh_max_history.models import (
    ArchiveCaptureIssue,
    ArchiveCollectionTarget,
    ArchiveConfiguration,
    ArchiveSnapshot,
    PublicArchiveFile,
    PublicDataset,
)


class Response(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.headers = {"content-length": str(len(content))}
        self.status = 200


class StorageReserveTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        override = override_settings(BUH_ESI_ARCHIVE_ROOT=directory.name)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(capture.reset_configuration_cache)
        capture.reset_configuration_cache()
        self.config = ArchiveConfiguration.objects.create(minimum_free_gib=0)

    def save_config(self, **values):
        for name, value in values.items():
            setattr(self.config, name, value)
        self.config.save()
        capture.reset_configuration_cache()

    def archive(self, *, private=False, payload=b'{"value":1}'):
        operation = SimpleNamespace(
            token=SimpleNamespace(character_id=90000001, user_id=None) if private else None,
            method="GET", url="/synthetic/", _kwargs={},
            operation=SimpleNamespace(operationId="synthetic_reserve"),
        )
        response = SimpleNamespace(content=payload, status_code=200, headers={})
        return capture.archive_esi_result(operation, {}, response)

    def file(self, name="data"):
        dataset, _ = PublicDataset.objects.get_or_create(
            slug="synthetic", defaults={
                "name": "Synthetic", "index_url": "https://data.everef.net/synthetic/index.json",
            },
        )
        return PublicArchiveFile.objects.create(
            dataset=dataset, source_url=f"https://data.everef.net/synthetic/{name}",
            relative_path=f"public/synthetic/{name}", remote_size=3,
        )

    def test_model_and_form_field_allow_zero_and_preserve_bounds_and_default(self):
        field = ArchiveConfiguration._meta.get_field("minimum_free_gib")
        form = modelform_factory(ArchiveConfiguration, fields=("minimum_free_gib",))
        self.assertEqual(field.default, 25)
        for value in (0, 1, 25, 2048):
            self.assertEqual(field.clean(value, self.config), value)
            self.assertTrue(form({"minimum_free_gib": value}, instance=self.config).is_valid())
        for value in (-1, 2049):
            with self.assertRaises(ValidationError):
                field.clean(value, self.config)
            self.assertFalse(form({"minimum_free_gib": value}, instance=self.config).is_valid())
        self.assertIn("0 to disable", field.help_text)

    @override_settings(BUH_ESI_ARCHIVE_MINIMUM_FREE_GIB=25)
    def test_database_zero_overrides_fallback_and_cache_refreshes_within_sixty_seconds(self):
        self.assertEqual(capture._configuration()["minimum_free_gib"], 0)
        self.save_config(minimum_free_gib=25)
        with patch.object(capture.time, "monotonic", return_value=100):
            self.assertEqual(capture._configuration()["minimum_free_gib"], 25)
        ArchiveConfiguration.objects.filter(pk=1).update(minimum_free_gib=0)
        with patch.object(capture.time, "monotonic", return_value=159):
            self.assertEqual(capture._configuration()["minimum_free_gib"], 25)
        with patch.object(capture.time, "monotonic", return_value=160):
            self.assertEqual(capture._configuration()["minimum_free_gib"], 0)
        ArchiveConfiguration.objects.all().delete()
        capture.reset_configuration_cache()
        self.assertEqual(capture._configuration()["minimum_free_gib"], 25)

    def test_zero_reserve_captures_public_and_private_payloads_without_space_probe(self):
        with (
            patch.object(capture, "_free_bytes", side_effect=AssertionError("reserve probe")),
            patch.object(capture.shutil, "disk_usage", side_effect=AssertionError("reserve probe")),
        ):
            public = self.archive()
            private = self.archive(private=True)
        self.assertEqual(ArchiveSnapshot.objects.count(), 2)
        self.assertEqual(gzip.decompress((self.root / public.relative_path).read_bytes()), b'{"value":1}')
        self.assertTrue(private.stream.is_private)

    def test_positive_reserve_still_skips_passive_capture(self):
        self.save_config(minimum_free_gib=1)
        with patch.object(capture, "_free_bytes", return_value=0):
            self.assertIsNone(self.archive())
        self.assertFalse(ArchiveSnapshot.objects.exists())
        self.assertTrue(ArchiveCaptureIssue.objects.filter(reason="minimum_free_space_guard").exists())

    def test_positive_reserve_still_checks_compressed_write(self):
        self.save_config(minimum_free_gib=1)
        with (
            patch.object(capture, "_free_bytes", return_value=100 * 1024**3),
            patch.object(capture.shutil, "disk_usage", return_value=SimpleNamespace(free=0)),
        ):
            with self.assertRaisesRegex(OSError, "reserve reached"):
                self.archive()
        self.assertFalse(ArchiveSnapshot.objects.exists())
        self.assertFalse(list(self.root.rglob("*.gz")))

    def test_zero_reserve_keeps_response_size_and_capture_switches(self):
        self.save_config(max_response_mib=1)
        self.assertIsNone(self.archive(payload=b"x" * (1024**2 + 1)))
        self.assertTrue(ArchiveCaptureIssue.objects.filter(reason="response_too_large").exists())
        self.save_config(capture_enabled=False)
        self.assertIsNone(self.archive())
        self.assertFalse(ArchiveSnapshot.objects.exists())

    def test_zero_reserve_applies_to_local_business_history(self):
        model = SimpleNamespace(_meta=SimpleNamespace(
            label_lower="memberaudit.synthetic", pk=SimpleNamespace(attname="id"),
        ))
        with (
            patch.object(capture, "_free_bytes", side_effect=AssertionError("reserve probe")),
            patch.object(capture.shutil, "disk_usage", side_effect=AssertionError("reserve probe")),
        ):
            snapshot = local_history.capture_record(model, {"id": 1, "name": "synthetic"})
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.stream.is_private)
        self.assertTrue((self.root / snapshot.relative_path).is_file())

    def test_zero_reserve_active_collection_keeps_request_budget(self):
        self.save_config(active_requests_per_run=2)
        for number in range(4):
            ArchiveCollectionTarget.objects.create(key=f"{number:064x}", operation_id="synthetic")
        with (
            patch.object(collection, "_free_bytes", side_effect=AssertionError("reserve probe")),
            patch.object(collection, "seed_targets"),
            patch.object(collection, "esi_client"),
            patch.object(collection, "collect_target", return_value=True) as collect,
        ):
            self.assertEqual(collection.collect_esi_batch(), {"attempted": 2, "checked": 2})
            self.assertEqual(collect.call_count, 2)

    def test_active_collection_keeps_positive_reserve_and_rate_cooldown(self):
        self.save_config(minimum_free_gib=1)
        with patch.object(collection, "_free_bytes", return_value=0):
            self.assertEqual(collection.collect_esi_batch(), {"paused": "storage"})
        self.save_config(minimum_free_gib=0, active_retry_at=now() + timedelta(hours=1))
        with patch.object(collection, "seed_targets") as seed:
            self.assertEqual(collection.collect_esi_batch()["paused"], "rate_limit")
            seed.assert_not_called()

    def test_zero_reserve_mirror_keeps_pause_and_attempt_budget(self):
        self.file()
        self.save_config(public_mirror_enabled=False, public_max_files_per_run=2)
        with patch.object(public_archive, "catalog_enabled_datasets") as catalog:
            self.assertFalse(public_archive.sync_public_archive()["enabled"])
            catalog.assert_not_called()
        self.save_config(public_mirror_enabled=True)
        with (
            patch.object(public_archive.shutil, "disk_usage", side_effect=AssertionError("reserve probe")),
            patch.object(public_archive, "_download_file", return_value=3) as download,
        ):
            result = public_archive.sync_public_archive(catalog_first=False)
        self.assertEqual(result["attempted_files"], 2)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(download.call_args.args[1], 0)

    def test_positive_mirror_reserve_stops_before_catalog(self):
        self.save_config(minimum_free_gib=1)
        with (
            patch.object(public_archive.shutil, "disk_usage", return_value=SimpleNamespace(free=0)),
            patch.object(public_archive, "catalog_enabled_datasets") as catalog,
        ):
            self.assertEqual(public_archive.sync_public_archive()["blocked"], "minimum_free_space")
            catalog.assert_not_called()

    def test_zero_reserve_download_retains_old_revision_and_keeps_byte_budget(self):
        record = self.file()
        target = self.root / record.relative_path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        with (
            patch.object(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), create=True),
            patch.object(public_archive.shutil, "disk_usage", side_effect=AssertionError("reserve probe")),
            patch.object(public_archive, "urlopen", side_effect=lambda *a, **k: Response(b"new")),
        ):
            with self.assertRaises(public_archive.DownloadBudgetExceeded):
                public_archive._download_file(record, 0, budget_bytes=2, deadline=time.monotonic() + 30)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(public_archive._download_file(record, 0, budget_bytes=3, deadline=time.monotonic() + 30), 3)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(record.revisions.count(), 2)
        self.assertEqual({(self.root / rev.relative_path).read_bytes() for rev in record.revisions.all()}, {b"old", b"new"})
        self.assertFalse(list(target.parent.glob("*.download-part")))

    def test_positive_download_reserve_checks_initial_space_and_stream_chunks(self):
        record = self.file()
        target = self.root / record.relative_path
        with patch.object(public_archive.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(public_archive.StoragePaused):
                public_archive._download_file(record, 1024**3, budget_bytes=3, deadline=time.monotonic() + 30)
        with (
            patch.object(os, "O_NOFOLLOW", getattr(os, "O_NOFOLLOW", 0), create=True),
            patch.object(public_archive.shutil, "disk_usage", side_effect=[SimpleNamespace(free=2 * 1024**3), SimpleNamespace(free=0)]),
            patch.object(public_archive, "urlopen", return_value=Response(b"new")),
        ):
            with self.assertRaises(public_archive.StoragePaused):
                public_archive._download_file(record, 1024**3, budget_bytes=3, deadline=time.monotonic() + 30)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob("*.download-part")))


class StorageReserveMigrationTests(TransactionTestCase):
    def test_validator_migration_preserves_configuration_and_is_reversible(self):
        before = ("buh_max_history", "0007_public_provider_gaps_and_response_validators")
        after = ("buh_max_history", "0008_allow_disabled_storage_reserve")
        executor = MigrationExecutor(connection)
        executor.migrate([before])
        old_model = executor.loader.project_state([before]).apps.get_model("buh_max_history", "ArchiveConfiguration")
        old_model.objects.update_or_create(singleton_id=1, defaults={"minimum_free_gib": 25, "public_mirror_enabled": False})
        old_apps = executor.loader.project_state([before]).apps
        stream = old_apps.get_model("buh_max_history", "ArchiveStream").objects.create(
            stream_key="a" * 64, operation_id="synthetic_migration", url_template="/synthetic/",
        )
        snapshot = old_apps.get_model("buh_max_history", "ArchiveSnapshot").objects.create(
            stream_id=stream.pk, payload_sha256="b" * 64,
            relative_path="synthetic/preserved.json.gz", stored_bytes=12,
        )
        try:
            executor = MigrationExecutor(connection)
            executor.migrate([after])
            model = executor.loader.project_state([after]).apps.get_model("buh_max_history", "ArchiveConfiguration")
            self.assertEqual(model.objects.get(pk=1).minimum_free_gib, 25)
            self.assertFalse(model.objects.get(pk=1).public_mirror_enabled)
            self.assertEqual(ArchiveSnapshot.objects.get(pk=snapshot.pk).relative_path, "synthetic/preserved.json.gz")
            self.assertEqual(model._meta.get_field("minimum_free_gib").clean(0, model()), 0)
            model.objects.filter(pk=1).update(minimum_free_gib=0)
            executor = MigrationExecutor(connection)
            executor.migrate([before])
            self.assertEqual(old_model.objects.get(pk=1).minimum_free_gib, 0)
            self.assertFalse(old_model.objects.get(pk=1).public_mirror_enabled)
            self.assertEqual(old_apps.get_model("buh_max_history", "ArchiveSnapshot").objects.get(pk=snapshot.pk).stored_bytes, 12)
        finally:
            MigrationExecutor(connection).migrate([after])
