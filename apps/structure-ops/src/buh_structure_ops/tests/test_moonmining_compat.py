"""Preserve extraction semantics while adding bounded deadlock handling."""

from datetime import timedelta
from unittest.mock import patch

from app_utils.testing import NoSocketsTestCase
from django.db import OperationalError, connection
from django.utils.timezone import now
from moonmining.managers import ExtractionQuerySet
from moonmining.models import Extraction, Owner
from moonmining.tests.testdata.factories import ExtractionFactory

from buh_structure_ops.moonmining_compat import install_moonmining_status_guard


class MoonMiningStatusTests(NoSocketsTestCase):
    def test_owner_update_preserves_global_time_based_transitions(self):
        own = ExtractionFactory(
            chunk_arrival_at=now() - timedelta(hours=1),
            auto_fracture_at=now() + timedelta(hours=1), status=Extraction.Status.STARTED,
        )
        other = ExtractionFactory(
            chunk_arrival_at=now() - timedelta(hours=1),
            auto_fracture_at=now() + timedelta(hours=1), status=Extraction.Status.STARTED,
        )
        with (
            patch.object(Owner, "update_extractions_from_esi") as source,
            patch.object(Owner, "update_extractions_from_notifications") as notifications,
        ):
            own.refinery.owner.update_extractions()
        own.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(own.status, Extraction.Status.READY)
        self.assertEqual(other.status, Extraction.Status.READY)
        source.assert_called_once()
        notifications.assert_called_once()

    def test_completed_and_canceled_transitions_keep_upstream_rules(self):
        complete = ExtractionFactory(
            chunk_arrival_at=now() - timedelta(hours=2),
            auto_fracture_at=now() - timedelta(hours=1), status=Extraction.Status.READY,
        )
        canceled = ExtractionFactory(
            chunk_arrival_at=now() - timedelta(hours=2),
            auto_fracture_at=now() - timedelta(hours=1), status=Extraction.Status.CANCELED,
        )
        Extraction.objects.all().update_status()
        complete.refresh_from_db()
        canceled.refresh_from_db()
        self.assertEqual(complete.status, Extraction.Status.COMPLETED)
        self.assertEqual(canceled.status, Extraction.Status.CANCELED)

    def test_error_inside_outer_transaction_is_not_retried(self):
        error = OperationalError(1213, "Synthetic deadlock")
        with (
            patch.object(connection, "vendor", "mysql"),
            patch.object(ExtractionQuerySet, "update", side_effect=error) as update,
            patch("buh_structure_ops.moonmining_compat.time.sleep") as sleep,
        ):
            with self.assertRaises(OperationalError):
                Extraction.objects.all().update_status()
        self.assertEqual(update.call_count, 1)
        sleep.assert_not_called()

    def test_installation_is_version_scoped_and_idempotent(self):
        method = ExtractionQuerySet.update_status
        self.assertFalse(install_moonmining_status_guard())
        with patch("buh_structure_ops.moonmining_compat.moonmining.__version__", "3.2.0"):
            self.assertFalse(install_moonmining_status_guard())
        self.assertIs(ExtractionQuerySet.update_status, method)
