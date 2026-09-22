"""MariaDB is required to test the production token locking boundary."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from unittest.mock import patch

from app_utils.testdata_factories import EveCharacterFactory
from django.contrib.auth import get_user_model
from django.db import OperationalError, close_old_connections, connection
from django.test import TransactionTestCase
from django.utils.timezone import now
from esi.managers import TokenManager
from esi.models import Token
from moonmining.managers import ExtractionQuerySet
from moonmining.models import Extraction, Owner
from moonmining.tests.testdata.factories import ExtractionFactory
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError


class SyncConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("The production row-lock/deadlock contract requires MariaDB")

    def test_concurrent_refresh_and_cleanup_preserve_one_rotating_grant(self):
        user = get_user_model().objects.create_user("integration-token-owner")
        EveCharacterFactory(character_id=90000002)
        token = Token.objects.create(
            user=user, character_id=90000002, character_name="Synthetic",
            character_owner_hash="owner", access_token="old-access", refresh_token="old-refresh",
        )
        Token.objects.filter(pk=token.pk).update(created=now() - timedelta(hours=1))
        barrier = Barrier(2)
        calls = []
        gate = Lock()

        class RotatingSso:
            def refresh_token(self, _url, *, refresh_token, auth):
                with gate:
                    calls.append(refresh_token)
                    if len(calls) > 1 and refresh_token == "old-refresh":
                        raise InvalidGrantError()
                    return {"access_token": "new-access", "refresh_token": "new-refresh"}

        def run(cleanup):
            close_old_connections()
            try:
                stale = Token.objects.get(pk=token.pk)
                barrier.wait(timeout=10)
                if cleanup:
                    stale.refresh_or_delete()
                else:
                    stale.refresh()
                return stale.access_token
            finally:
                close_old_connections()

        with (
            patch("esi.models.OAuth2Session", return_value=RotatingSso()),
            patch.object(TokenManager, "validate_access_token", return_value={"owner": "owner"}),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            values = list(pool.map(run, [True, False]))
        self.assertEqual(values, ["new-access", "new-access"])
        self.assertEqual(calls, ["old-refresh"])
        self.assertEqual(Token.objects.get(pk=token.pk).refresh_token, "new-refresh")

    def test_deadlock_retries_only_sql_after_rolling_back_partial_transition(self):
        extraction = ExtractionFactory(
            chunk_arrival_at=now() - timedelta(hours=1),
            auto_fracture_at=now() + timedelta(hours=1), status=Extraction.Status.STARTED,
        )
        original = ExtractionQuerySet.update
        calls = []

        def interrupted(queryset, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise OperationalError(1213, "Synthetic deadlock after first SQL update")
            if len(calls) == 3:
                self.assertEqual(Extraction.objects.get(pk=extraction.pk).status, Extraction.Status.STARTED)
            return original(queryset, **kwargs)

        with (
            patch.object(ExtractionQuerySet, "update", interrupted),
            patch("buh_structure_ops.moonmining_compat.time.sleep") as sleep,
            patch.object(Owner, "update_extractions_from_esi") as source,
            patch.object(Owner, "update_extractions_from_notifications") as notifications,
        ):
            extraction.refinery.owner.update_extractions()
        extraction.refresh_from_db()
        self.assertEqual(extraction.status, Extraction.Status.READY)
        self.assertEqual(len(calls), 4)
        sleep.assert_called_once_with(0.05)
        source.assert_called_once()
        notifications.assert_called_once()

    def test_persistent_deadlock_and_other_database_errors_remain_failures(self):
        for code, attempts in ((1213, 3), (1205, 1), (2006, 1)):
            with (
                self.subTest(code=code),
                patch.object(ExtractionQuerySet, "update", side_effect=OperationalError(code, "Synthetic")) as update,
                patch("buh_structure_ops.moonmining_compat.time.sleep"),
            ):
                with self.assertRaises(OperationalError):
                    Extraction.objects.all().update_status()
                self.assertEqual(update.call_count, attempts)
