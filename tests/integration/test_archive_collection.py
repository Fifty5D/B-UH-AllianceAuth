"""Real MariaDB lock, schema upgrade and django-esi capture contracts."""

import gzip
import tempfile
from pathlib import Path

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from buh_max_history.capture import reset_configuration_cache
from buh_max_history.job_lock import (
    ArchiveLockLost,
    _lock_name,
    assert_public_archive_lock,
    public_archive_lock,
)
from buh_max_history.models import ArchiveConfiguration, ArchiveSnapshot


class ArchiveDatabaseContracts(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("This contract requires the disposable MariaDB service.")
        self.addCleanup(reset_configuration_cache)

    def test_mirror_lock_excludes_other_connection_and_releases_at_session_end(self):
        other = connection.copy(alias="archive-lock-test")
        try:
            with public_archive_lock() as acquired:
                self.assertTrue(acquired)
                with other.cursor() as cursor:
                    cursor.execute("SELECT GET_LOCK(%s, 0)", [_lock_name()])
                    self.assertEqual(cursor.fetchone()[0], 0)
            with other.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, 0)", [_lock_name()])
                self.assertEqual(cursor.fetchone()[0], 1)
            other.close()
            with public_archive_lock() as acquired:
                self.assertTrue(acquired)
        finally:
            other.close()

    def test_lost_database_session_stops_the_active_mirror(self):
        with public_archive_lock() as acquired:
            self.assertTrue(acquired)
            connection.close()
            with self.assertRaises(ArchiveLockLost):
                assert_public_archive_lock()
        with public_archive_lock() as acquired:
            self.assertTrue(acquired)

    def test_upgrade_keeps_existing_records_and_accepts_old_schema_writes(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes("buh_max_history")
        old_target = [("buh_max_history", "0002_publiccatalogindex")]
        executor.migrate(old_target)
        old = executor.loader.project_state(old_target).apps
        Dataset = old.get_model("buh_max_history", "PublicDataset")
        File = old.get_model("buh_max_history", "PublicArchiveFile")
        dataset = Dataset.objects.create(
            name="Synthetic archive",
            slug="synthetic",
            index_url="https://data.everef.net/synthetic/index.json",
        )
        before = File.objects.create(
            dataset=dataset,
            source_url="https://data.everef.net/old",
            relative_path="public/old",
            status="STORED",
            stored_bytes=42,
            payload_sha256="a" * 64,
        )
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            # A rollback-compatible additive schema still accepts inserts made
            # by the old model, which does not know the new non-null columns.
            File.objects.create(
                dataset=dataset,
                source_url="https://data.everef.net/new",
                relative_path="public/new",
            )
            from buh_max_history.models import PublicArchiveFile

            row = PublicArchiveFile.objects.get(pk=before.pk)
            self.assertEqual(
                (row.status, row.stored_bytes, row.payload_sha256, row.failure_count),
                ("STORED", 42, "a" * 64, 0),
            )
        finally:
            MigrationExecutor(connection).migrate(latest)

    def test_real_esi_client_response_is_saved_without_a_production_request(self):
        from unittest.mock import patch
        from esi.openapi_clients import ESIClientProvider

        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(BUH_ESI_ARCHIVE_ROOT=directory),
        ):
            ArchiveConfiguration.objects.create(singleton_id=1, minimum_free_gib=5)
            reset_configuration_cache()
            client = ESIClientProvider(
                compatibility_date="2025-12-16",
                ua_appname="ArchiveContractTests",
                ua_version="1.0.0",
                tags=["Market"],
            ).client
            with patch("buh_max_history.capture._free_bytes", return_value=100 * 1024**3):
                data = client.Market.GetMarketsRegionIdOrders(
                    order_type="buy",
                    region_id=10000002,
                    type_id=62454,
                ).results(use_etag=False, use_cache=False, store_cache=False)
            self.assertEqual(len(data), 1)
            snapshot = ArchiveSnapshot.objects.get()
            self.assertIn(
                b"1760",
                gzip.decompress((Path(directory) / snapshot.relative_path).read_bytes()),
            )
            self.assertEqual(snapshot.stream.safe_parameters["page"], 1)
