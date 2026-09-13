import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_utils.testdata_factories import UserMainFactory
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from buh_max_history.access import PERMISSION_CODES
from buh_max_history.capture import archive_esi_result, reset_configuration_cache
from buh_max_history.models import (
    ArchiveConfiguration,
    ArchiveSnapshot,
    ArchiveStream,
    PublicArchiveFile,
    PublicCatalogIndex,
    PublicDataset,
)
from buh_max_history.public_archive import catalog_dataset, validate_everef_url


class ArchiveCaptureTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = override_settings(BUH_ESI_ARCHIVE_ROOT=self.directory.name)
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        ArchiveConfiguration.objects.create(singleton_id=1, minimum_free_gib=5)
        reset_configuration_cache()

    def operation(self, *, private=False):
        token = SimpleNamespace(character_id=90000001, user_id=42) if private else None
        return SimpleNamespace(
            token=token,
            method="GET",
            url="/characters/{character_id}/wallet/",
            operation=SimpleNamespace(operationId="get_characters_character_id_wallet"),
            api=SimpleNamespace(app_name="Member Audit"),
            _kwargs={
                "character_id": 90000001,
                "page": 1,
                "refresh_token": "must-not-be-stored",
            },
        )

    @staticmethod
    def response(payload):
        return SimpleNamespace(
            content=payload,
            status_code=200,
            headers={"content-type": "application/json", "etag": '"abc"'},
        )

    def test_unchanged_payload_is_observed_without_duplicate_file(self):
        operation = self.operation(private=True)
        archive_esi_result(operation, {}, self.response(b'{"balance":100}'))
        archive_esi_result(operation, {}, self.response(b'{"balance":100}'))

        stream = ArchiveStream.objects.get()
        snapshot = ArchiveSnapshot.objects.get()
        self.assertTrue(stream.is_private)
        self.assertEqual(stream.character_id, 90000001)
        self.assertEqual(stream.request_count, 2)
        self.assertEqual(stream.snapshot_count, 1)
        self.assertEqual(snapshot.observation_count, 2)
        self.assertNotIn("refresh_token", stream.safe_parameters)
        self.assertTrue((Path(self.directory.name) / snapshot.relative_path).is_file())

    def test_changed_payload_creates_second_snapshot(self):
        operation = self.operation()
        archive_esi_result(operation, {}, self.response(b'{"balance":100}'))
        archive_esi_result(operation, {}, self.response(b'{"balance":200}'))
        stream = ArchiveStream.objects.get()
        self.assertEqual(stream.snapshot_count, 2)
        self.assertEqual(ArchiveSnapshot.objects.count(), 2)


class PublicArchiveTests(TestCase):
    def test_url_allowlist_rejects_other_hosts_and_traversal(self):
        self.assertEqual(
            validate_everef_url("https://data.everef.net/wars/index.json"),
            "https://data.everef.net/wars/index.json",
        )
        with self.assertRaises(ValidationError):
            validate_everef_url("https://example.com/wars/index.json")
        with self.assertRaises(ValidationError):
            validate_everef_url("https://data.everef.net/a/../secret")

    @patch("buh_max_history.public_archive._request_json")
    def test_catalog_adds_files_without_downloading(self, request_json):
        request_json.return_value = (
            {
                "files": [
                    {
                        "name": "wars-2026-08-29.json.bz2",
                        "size": 1234,
                        "etag": "abc",
                    }
                ]
            },
            {},
        )
        dataset = PublicDataset.objects.create(
            name="Wars",
            slug="wars",
            index_url="https://data.everef.net/wars/index.json",
        )
        result = catalog_dataset(dataset)
        record = PublicArchiveFile.objects.get()
        self.assertEqual(result["created"], 1)
        self.assertEqual(record.remote_size, 1234)
        self.assertEqual(record.status, PublicArchiveFile.Status.PENDING)

    @patch("buh_max_history.public_archive._request_json")
    def test_catalog_persists_directory_frontier_for_future_runs(self, request_json):
        request_json.return_value = (
            {
                "files": [],
                "directories": [
                    {
                        "name": "history",
                        "index_url": "https://data.everef.net/wars/history/index.json",
                    }
                ],
            },
            {},
        )
        dataset = PublicDataset.objects.create(
            name="Wars",
            slug="wars-frontier",
            index_url="https://data.everef.net/wars/index.json",
        )
        result = catalog_dataset(dataset, max_indexes=1)
        self.assertEqual(result["indexes"], 1)
        child = PublicCatalogIndex.objects.get(depth=1)
        self.assertEqual(
            child.source_url, "https://data.everef.net/wars/history/index.json"
        )
        self.assertEqual(child.status, PublicCatalogIndex.Status.PENDING)


class ArchiveAccessTests(TestCase):
    def test_setup_grants_all_permissions_to_existing_director(self):
        director = Group.objects.create(name="Director")
        call_command("buh_archive_setup")
        self.assertEqual(
            director.permissions.filter(content_type__app_label="buh_max_history").count(),
            len(PERMISSION_CODES),
        )
        self.assertEqual(PublicDataset.objects.count(), 9)

    def test_full_archive_dashboard_renders(self):
        user = UserMainFactory(
            permissions=[f"buh_max_history.{code}" for code in PERMISSION_CODES]
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(BUH_ESI_ARCHIVE_ROOT=directory),
        ):
            self.client.force_login(user)
            response = self.client.get(reverse("buh_max_history:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ESI History Archive")

    def test_private_snapshot_download_requires_separate_permission(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(BUH_ESI_ARCHIVE_ROOT=directory),
        ):
            path = Path(directory) / "private" / "payload.json.gz"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"gzip-data")
            stream = ArchiveStream.objects.create(
                stream_key="a" * 64,
                operation_id="private_test",
                url_template="/private/",
                is_private=True,
            )
            snapshot = ArchiveSnapshot.objects.create(
                stream=stream,
                payload_sha256="b" * 64,
                relative_path="private/payload.json.gz",
            )
            viewer = UserMainFactory(permissions=["buh_max_history.view_history_archive"])
            downloader = UserMainFactory(
                permissions=[
                    "buh_max_history.view_history_archive",
                    "buh_max_history.download_private_archive",
                ]
            )
            url = reverse("buh_max_history:download_snapshot", args=[snapshot.pk])
            self.client.force_login(viewer)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.client.force_login(downloader)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
