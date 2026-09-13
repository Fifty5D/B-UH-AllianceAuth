"""Failure, ownership, pagination and chronology contracts for active history."""

import gzip
import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app_utils.testdata_factories import UserMainFactory
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.timezone import now

from buh_max_history.capture import archive_esi_result, reset_configuration_cache
from buh_max_history.collection import (
    collect_target,
    discover_children,
    endpoints,
    new_target,
    next_page,
    seed_targets,
    token_for,
)
from buh_max_history.discovery import discover_public_datasets
from buh_max_history.models import (
    ArchiveCollectionTarget,
    ArchiveConfiguration,
    ArchiveObservation,
    ArchiveSnapshot,
    PublicArchiveFile,
    PublicDataset,
)
from buh_max_history.revisions import retain_public_revision


class ExpandedHistoryTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        settings = override_settings(BUH_ESI_ARCHIVE_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.config, _ = ArchiveConfiguration.objects.update_or_create(
            singleton_id=1, defaults={"minimum_free_gib": 5}
        )
        reset_configuration_cache()
        self.addCleanup(reset_configuration_cache)
        free = patch("buh_max_history.capture._free_bytes", return_value=100 * 1024**3)
        free.start()
        self.addCleanup(free.stop)
        disk = patch(
            "buh_max_history.capture.shutil.disk_usage",
            return_value=SimpleNamespace(free=100 * 1024**3, used=0, total=100 * 1024**3),
        )
        disk.start()
        self.addCleanup(disk.stop)

    def operation(self, operation_id="GetCharactersCharacterIdWallet", **kwargs):
        return SimpleNamespace(
            method="GET",
            token=SimpleNamespace(character_id=99000001, user_id=1),
            url="/characters/{character_id}/wallet",
            _kwargs={"character_id": 99000001, **kwargs},
            operation=SimpleNamespace(operationId=operation_id),
            api=SimpleNamespace(app_name="History tests"),
        )

    def save_payload(self, body, operation=None):
        raw = json.dumps(body).encode()
        return archive_esi_result(
            operation or self.operation(),
            body,
            SimpleNamespace(content=raw, status_code=200, headers={}),
        )

    def target(self, operation_id, **parameters):
        target = new_target(
            endpoints()[operation_id], parameters or {"character_id": 99000001}, 99000001
        )
        target.save()
        return target

    def fake_client(self, operation_id, data, headers=None, method="GET"):
        entry = endpoints()[operation_id]
        operation = self.operation(operation_id)
        operation.method = method
        operation.result = Mock(
            return_value=(
                data,
                SimpleNamespace(
                    content=json.dumps(data).encode(),
                    status_code=200,
                    headers=headers or {},
                ),
            )
        )
        factory = Mock(return_value=operation)
        client = SimpleNamespace(
            **{entry["tag"].replace(" ", "_"): SimpleNamespace(**{operation_id: factory})}
        )
        return client, operation, factory

    def test_return_to_an_earlier_state_retains_order_without_duplicate_payloads(self):
        saved = [self.save_payload(value) for value in (100, 100, 200, 100)]
        self.assertEqual(ArchiveSnapshot.objects.count(), 2)
        spans = list(ArchiveObservation.objects.order_by("pk"))
        self.assertEqual(
            [span.snapshot_id for span in spans], [saved[0].pk, saved[2].pk, saved[0].pk]
        )
        self.assertEqual([span.observation_count for span in spans], [2, 1, 1])
        self.assertEqual(len(list(self.root.rglob("*.json.gz"))), 2)
        saved[0].stream.refresh_from_db()
        self.assertEqual(saved[0].stream.current_payload_sha256, saved[0].payload_sha256)

    def test_public_versions_preserve_repeated_states_and_detect_corrupt_retained_bytes(
        self,
    ):
        dataset = PublicDataset.objects.create(
            name="Synthetic",
            slug="synthetic",
            index_url="https://data.everef.net/synthetic/index.json",
        )
        record = PublicArchiveFile.objects.create(
            dataset=dataset,
            source_url="https://data.everef.net/synthetic/current.json",
            relative_path="public/current.json",
        )
        for i, body in enumerate((b"first", b"other", b"first")):
            path = self.root / str(i)
            path.write_bytes(body)
            retain_public_revision(record, path, deadline=time.monotonic() + 10)
        rows = list(record.revisions.order_by("pk"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].relative_path, rows[2].relative_path)
        self.assertEqual((self.root / rows[1].relative_path).read_bytes(), b"other")
        (self.root / rows[0].relative_path).write_bytes(b"wrong")
        path = self.root / "fresh"
        path.write_bytes(b"first")
        with self.assertRaises(ValueError):
            retain_public_revision(record, path, deadline=time.monotonic() + 10)
        self.assertEqual(record.revisions.count(), 3)

    @patch("buh_max_history.discovery._request_json")
    def test_discovery_adds_siblings_and_preserves_disabled_subtrees(self, fetch):
        PublicDataset.objects.create(
            name="Selected",
            slug="selected",
            index_url="https://data.everef.net/ccp/sde/index.json",
            enabled=False,
        )
        fetch.side_effect = [
            (
                {
                    "directories": [
                        {"index_url": "https://data.everef.net/ccp/index.json"},
                        {"index_url": "https://data.everef.net/wars/index.json"},
                    ]
                },
                {},
            ),
            (
                {
                    "directories": [
                        {"index_url": "https://data.everef.net/ccp/sde/index.json"},
                        {"index_url": "https://data.everef.net/ccp/mer/index.json"},
                    ]
                },
                {},
            ),
        ]
        self.assertEqual(discover_public_datasets(), 2)
        self.assertFalse(PublicDataset.objects.get(slug="selected").enabled)
        self.assertTrue(
            PublicDataset.objects.filter(
                index_url="https://data.everef.net/ccp/mer/index.json"
            ).exists()
        )
        self.assertEqual(discover_public_datasets(), 0)
        self.assertEqual(fetch.call_count, 2)

    def test_manifest_seeds_owned_character_reads_without_requiring_protocol_headers(self):
        user = UserMainFactory()
        self.assertGreater(seed_targets(), 20)
        self.assertTrue(
            ArchiveCollectionTarget.objects.filter(
                operation_id="GetCharactersCharacterIdIndustryJobs",
                character_id=user.profile.main_character.character_id,
            ).exists()
        )
        self.assertFalse(
            ArchiveCollectionTarget.objects.filter(
                parameters__has_key="X-Compatibility-Date"
            ).exists()
        )
        for entry in endpoints().values():
            self.assertTrue(entry["id"].startswith("Get"))
            self.assertFalse(any(name.startswith("X-") for name in entry["required"]))

    def test_tokens_must_match_both_character_ownership_and_scopes(self):
        from esi.models import Scope, Token

        owner = UserMainFactory()
        other = UserMainFactory()
        character_id = owner.profile.main_character.character_id
        entry = endpoints()["GetCharactersCharacterIdWallet"]
        from allianceauth.authentication.models import CharacterOwnership

        owner_hash = CharacterOwnership.objects.get(
            character__character_id=character_id
        ).owner_hash
        wrong = Token.objects.bulk_create(
            [
                Token(
                    user=other,
                    character_id=character_id,
                    character_name="Synthetic",
                    character_owner_hash=owner_hash,
                    access_token="not-a-real-token",
                    refresh_token="not-a-real-token",
                )
            ]
        )[0]
        scope, _ = Scope.objects.get_or_create(name=entry["scopes"][0])
        wrong.scopes.add(scope)
        target = self.target(entry["id"], character_id=character_id)
        target.character_id = character_id
        self.assertIsNone(token_for(target, entry))
        correct = Token.objects.bulk_create(
            [
                Token(
                    user=owner,
                    character_id=character_id,
                    character_name="Synthetic",
                    character_owner_hash=owner_hash,
                    access_token="not-a-real-token",
                    refresh_token="not-a-real-token",
                )
            ]
        )[0]
        self.assertIsNone(token_for(target, entry))
        correct.scopes.add(scope)
        self.assertEqual(token_for(target, entry), correct)
        replacement = Token.objects.bulk_create(
            [
                Token(
                    user=owner,
                    character_id=character_id,
                    character_name="Synthetic replacement",
                    character_owner_hash=owner_hash,
                    access_token="replacement-not-real",
                    refresh_token="replacement-not-real",
                )
            ]
        )[0]
        replacement.scopes.add(scope)
        self.assertEqual(token_for(target, entry), replacement)

    @patch("buh_max_history.collection.token_for", return_value=None)
    def test_missing_access_never_calls_the_api(self, token):
        target = self.target("GetCharactersCharacterIdIndustryJobs")
        client = Mock()
        self.assertFalse(collect_target(target, client))
        self.assertEqual(target.status, "missing_access")
        self.assertGreater(target.next_attempt_at, now() + timedelta(hours=5))
        self.assertIsNotNone(target.last_attempt_at)
        self.assertEqual(client.mock_calls, [])

    @patch(
        "buh_max_history.collection.token_for",
        return_value=SimpleNamespace(character_id=99000001, user_id=1),
    )
    def test_current_reads_continue_while_history_resumes_and_completed_history_sleeps(
        self, token
    ):
        name = "GetCharactersCharacterIdContacts"
        target = self.target(name)
        client, operation, _ = self.fake_client(name, [{"contact_id": 1}], {"X-Pages": "4"})
        collect_target(target, client)
        historical = ArchiveCollectionTarget.objects.exclude(pk=target.pk).get(
            operation_id=name
        )
        self.assertEqual((target.cursor, target.status), ({}, "current"))
        self.assertEqual(historical.cursor, {"page": 2})
        collect_target(historical, client)
        self.assertEqual(historical.cursor, {"page": 3})
        collect_target(target, client)
        historical.refresh_from_db()
        self.assertEqual(historical.cursor, {"page": 3})
        collect_target(historical, client)
        collect_target(historical, client)
        self.assertEqual(historical.status, "history_complete")
        self.assertGreater(historical.next_attempt_at, now() + timedelta(days=365))
        collect_target(target, client)
        historical.refresh_from_db()
        self.assertEqual(historical.cursor, {"page": 2})
        self.assertEqual(historical.status, "backlog")

    def test_pagination_uses_both_legacy_and_new_api_cursors(self):
        for name, data, headers, expected in [
            (
                "GetCharactersCharacterIdMail",
                [{"mail_id": 21}, {"mail_id": 20}],
                {},
                {"last_mail_id": 20},
            ),
            (
                "GetCharactersCharacterIdWalletTransactions",
                [{"transaction_id": 11}],
                {},
                {"from_id": 11},
            ),
            (
                "GetCharactersCharacterIdCalendar",
                [{"event_id": 5}, {"event_id": 7}],
                {},
                {"from_event": 7},
            ),
            (
                "GetCorporationsProjectsListing",
                {"cursor": {"before": "older", "after": "newer"}},
                {},
                {"before": "older"},
            ),
        ]:
            with self.subTest(name=name):
                self.assertEqual(next_page(endpoints()[name], data, headers, {}), expected)
                self.assertEqual(next_page(endpoints()[name], data, headers, expected), {})

    @patch(
        "buh_max_history.collection.token_for",
        return_value=SimpleNamespace(character_id=99000001, user_id=1),
    )
    def test_completed_jobs_are_included_and_post_operations_never_execute(self, token):
        name = "GetCharactersCharacterIdIndustryJobs"
        client, operation, factory = self.fake_client(name, [])
        target = self.target(name)
        collect_target(target, client)
        self.assertTrue(factory.call_args.kwargs["include_completed"])
        self.assertEqual(target.status, "current")
        client, operation, factory = self.fake_client(name, [], method="POST")
        collect_target(target, client)
        operation.result.assert_not_called()
        self.assertEqual(target.status, "failed")

    @patch(
        "buh_max_history.collection.token_for",
        return_value=SimpleNamespace(character_id=99000001, user_id=1),
    )
    def test_rate_limit_stops_collection_and_retains_a_safe_retry_reason(self, token):
        name = "GetCharactersCharacterIdIndustryJobs"
        client, operation, _ = self.fake_client(name, [])
        error = RuntimeError("secret response text must not enter metadata")
        error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "1800"})
        operation.result.side_effect = error
        target = self.target(name)
        collect_target(target, client)
        self.config.refresh_from_db()
        self.assertGreater(self.config.active_retry_at, now() + timedelta(minutes=29))
        self.assertNotIn("secret", target.detail)
        self.assertEqual(target.failure_count, 1)
        self.assertEqual(ArchiveSnapshot.objects.count(), 0)

    def test_authorized_parent_discovers_mail_bodies_contract_items_and_wallet_divisions(
        self,
    ):
        for name, rows, child in [
            (
                "GetCharactersCharacterIdMail",
                [{"mail_id": 42}],
                "GetCharactersCharacterIdMailMailId",
            ),
            (
                "GetCharactersCharacterIdContracts",
                [{"contract_id": 9, "type": "item_exchange"}],
                "GetCharactersCharacterIdContractsContractIdItems",
            ),
            (
                "GetCorporationsCorporationIdWallets",
                [{"division": 3}],
                "GetCorporationsCorporationIdWalletsDivisionJournal",
            ),
        ]:
            with self.subTest(name=name):
                target = self.target(
                    name,
                    **(
                        {"corporation_id": 98000001}
                        if "Corporations" in name
                        else {"character_id": 99000001}
                    ),
                )
                discover_children(target, endpoints()[name], rows)
                self.assertTrue(
                    ArchiveCollectionTarget.objects.filter(operation_id=child).exists()
                )
        self.assertFalse(
            ArchiveCollectionTarget.objects.filter(
                operation_id="GetCharactersCharacterIdContractsContractIdBids"
            ).exists()
        )

    def test_private_history_exports_and_timeline_require_private_metadata_permission(self):
        snapshot = self.save_payload(100)
        user = UserMainFactory(
            permissions=[
                "buh_max_history.view_history_archive",
                "buh_max_history.view_archive_coverage",
            ]
        )
        self.client.force_login(user)
        url = reverse("buh_max_history:history")
        self.assertEqual(self.client.get(url, {"format": "json"}).json()["count"], 0)
        self.assertEqual(
            self.client.get(url, {"stream": snapshot.stream_id}).status_code, 404
        )
        self.assertEqual(self.client.get(url, {"from": "bad"}).status_code, 400)
        from django.contrib.auth.models import Permission

        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="buh_max_history",
                codename="view_private_archive_metadata",
            )
        )
        result = self.client.get(url, {"format": "json"}).json()
        self.assertEqual(result["count"], 1)
        self.assertNotIn("relative_path", result["results"][0])
        self.assertContains(self.client.get(url), "History &amp; coverage")

    def test_business_history_preserves_changes_deletions_and_rollback_boundary(self):
        from buh_moon_tax.models import TaxConfiguration
        from buh_max_history.local_history import model_fields, eligible
        from esi.models import Token

        self.assertFalse(eligible(Token))
        self.assertFalse(
            any("token" in name.lower() for name in model_fields(TaxConfiguration))
        )
        with self.captureOnCommitCallbacks(execute=True):
            row = TaxConfiguration.objects.create(singleton_id=1)
        self.assertEqual(ArchiveSnapshot.objects.count(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            row.delete()
        snapshot = ArchiveSnapshot.objects.latest("pk")
        payload = json.loads(
            gzip.decompress((self.root / snapshot.relative_path).read_bytes())
        )
        self.assertTrue(payload["deleted"])
        self.assertTrue(snapshot.stream.is_private)
        from django.db import transaction

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    TaxConfiguration.objects.create(singleton_id=1)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(ArchiveSnapshot.objects.count(), 2)

    def test_date_filter_uses_actual_spans_instead_of_returned_payload_min_max(self):
        a = self.save_payload("A")
        self.save_payload("B")
        self.save_payload("A")
        spans = list(ArchiveObservation.objects.order_by("pk"))
        for i, span in enumerate(spans):
            stamp = now() - timedelta(days=4 - i)
            ArchiveObservation.objects.filter(pk=span.pk).update(
                first_observed_at=stamp, last_observed_at=stamp
            )
        ArchiveSnapshot.objects.filter(pk=a.pk).update(
            first_observed_at=now() - timedelta(days=4),
            last_observed_at=now() - timedelta(days=2),
        )
        user = UserMainFactory(
            permissions=[
                "buh_max_history.view_history_archive",
                "buh_max_history.view_archive_coverage",
                "buh_max_history.view_private_archive_metadata",
            ]
        )
        self.client.force_login(user)
        day = (now() - timedelta(days=3)).date().isoformat()
        result = self.client.get(
            reverse("buh_max_history:history"), {"format": "json", "from": day, "to": day}
        ).json()
        self.assertEqual(result["count"], 1)
        self.assertNotEqual(result["results"][0]["id"], a.pk)

    @patch("buh_max_history.discovery._request_json", side_effect=TimeoutError)
    @patch(
        "buh_max_history.tasks.sync_public_archive", return_value={"downloaded_files": 1}
    )
    def test_failed_discovery_does_not_stop_existing_public_mirror(self, sync, discovery):
        from buh_max_history.models import ArchiveCaptureIssue, ArchiveJob
        from buh_max_history.tasks import _run_job

        result = _run_job(ArchiveJob.objects.create(kind="SYNC"))
        self.assertTrue(result["ok"])
        sync.assert_called_once()
        self.assertTrue(
            ArchiveCaptureIssue.objects.filter(reason="discovery_failed").exists()
        )

    def test_local_scan_checkpoint_does_not_skip_rows_when_deadline_interrupts_page(self):
        from buh_max_history.local_history import collect_local_batch
        from buh_moon_tax.models import TaxConfiguration

        TaxConfiguration.objects.create(singleton_id=1)
        clock = iter([0, 20])
        with (
            patch(
                "buh_max_history.local_history.apps.get_models",
                return_value=[TaxConfiguration],
            ),
            patch(
                "buh_max_history.local_history.time.monotonic",
                side_effect=lambda: next(clock),
            ),
        ):
            result = collect_local_batch(deadline=10)
        target = ArchiveCollectionTarget.objects.get(kind="auth")
        self.assertEqual(result["captured"], 0)
        self.assertEqual(target.status, "backlog")
        self.assertIsNone(target.completed_at)
        self.assertEqual(target.cursor, {})

    @patch(
        "buh_max_history.collection.token_for",
        return_value=SimpleNamespace(character_id=99000001, user_id=1),
    )
    def test_client_rate_limit_exception_persists_cooldown_without_retry_storm(self, token):
        from esi.exceptions import ESIErrorLimitException

        name = "GetCharactersCharacterIdIndustryJobs"
        client, operation, _ = self.fake_client(name, [])
        operation.result.side_effect = ESIErrorLimitException(reset=3600)
        target = self.target(name)
        collect_target(target, client)
        self.config.refresh_from_db()
        self.assertGreater(self.config.active_retry_at, now() + timedelta(minutes=59))

    @patch(
        "buh_max_history.collection.token_for",
        return_value=SimpleNamespace(character_id=99000001, user_id=1),
    )
    def test_modern_history_starts_from_newest_and_includes_closed_projects(self, token):
        name = "GetCorporationsProjectsListing"
        client, _, factory = self.fake_client(name, {"projects": [], "cursor": {}})
        target = self.target(name, corporation_id=98000001)
        collect_target(target, client)
        self.assertEqual(factory.call_args.kwargs["before"], "0")
        self.assertEqual(factory.call_args.kwargs["state"], "All")
        self.assertEqual(factory.call_args.kwargs["limit"], 100)
