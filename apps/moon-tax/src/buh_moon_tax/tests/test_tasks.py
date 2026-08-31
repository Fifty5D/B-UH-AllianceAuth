import datetime as dt
from unittest.mock import patch

from django.test import TestCase
from django.utils.timezone import now

from buh_moon_tax.models import AuditRun, TaxConfiguration
from buh_moon_tax.tasks import expire_stale_audits, run_scheduled_audit


class ScheduledAuditGuardTests(TestCase):
    def setUp(self):
        TaxConfiguration.objects.create(singleton_id=1, audit_interval_hours=4)

    @patch("buh_moon_tax.tasks.refresh_sources.delay")
    def test_editable_interval_suppresses_a_recent_audit(self, delay):
        recent = AuditRun.objects.create(
            trigger=AuditRun.Trigger.MANUAL,
            status=AuditRun.Status.COMPLETE,
            finished_at=now(),
        )
        result = run_scheduled_audit()
        self.assertEqual(result, recent.pk)
        delay.assert_not_called()

    @patch("buh_moon_tax.tasks.refresh_sources.delay")
    def test_due_interval_creates_and_queues_an_audit(self, delay):
        old = AuditRun.objects.create(
            trigger=AuditRun.Trigger.MANUAL,
            status=AuditRun.Status.COMPLETE,
            finished_at=now() - dt.timedelta(hours=5),
        )
        AuditRun.objects.filter(pk=old.pk).update(
            queued_at=now() - dt.timedelta(hours=5)
        )
        result = run_scheduled_audit()
        created = AuditRun.objects.get(pk=result)
        self.assertEqual(created.trigger, AuditRun.Trigger.SCHEDULED)
        self.assertEqual(created.status, AuditRun.Status.QUEUED)
        delay.assert_called_once_with(created.pk)

    def test_stale_run_is_released_after_worker_restart(self):
        audit = AuditRun.objects.create(
            trigger=AuditRun.Trigger.MANUAL,
            status=AuditRun.Status.REFRESHING,
        )
        AuditRun.objects.filter(pk=audit.pk).update(
            queued_at=now() - dt.timedelta(hours=3)
        )
        self.assertEqual(expire_stale_audits(), 1)
        audit.refresh_from_db()
        self.assertEqual(audit.status, AuditRun.Status.FAILED)
        self.assertIn("released automatically", audit.error)
