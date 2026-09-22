"""Run the staged SELECT-only collector against the real installed model contract."""

from contextlib import redirect_stdout
from datetime import timedelta
import io
import json
from unittest import mock

from app_utils.testdata_factories import EveCharacterFactory
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.timezone import now
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from memberaudit.models import Character, CharacterUpdateStatus

from buh_moon_tax.models import AuditRun, PriceSnapshot, TaxConfiguration
from ops.deploy.recovery_sync import PROBE_CODE


class RecoverySyncProbeTests(TestCase):
    def test_actual_orm_fields_schedules_success_and_identity_are_read_without_updates(
        self,
    ):
        TaxConfiguration.objects.update_or_create(
            singleton_id=1, defaults={"audit_interval_hours": 4}
        )
        clock = now() - timedelta(minutes=10)
        completed = AuditRun.objects.create(
            trigger="SCHEDULED",
            status="COMPLETE",
            source_refresh_requested_at=clock,
            finished_at=clock + timedelta(minutes=3),
        )
        PriceSnapshot.objects.create(
            audit_run=completed,
            compressed_type_id=12345,
            compressed_type_name="Synthetic Ore",
            requested_quantity=2,
            priced_quantity=2,
            weighted_unit_price=10,
            total_value=20,
            source="synthetic",
            complete=True,
        )
        interval, _ = IntervalSchedule.objects.get_or_create(every=3600, period="seconds")
        PeriodicTask.objects.create(
            name="synthetic recovery audit",
            task="buh_moon_tax.tasks.run_scheduled_audit",
            interval=interval,
            enabled=True,
            last_run_at=clock,
        )
        character = Character.objects.create(eve_character=EveCharacterFactory())
        CharacterUpdateStatus.objects.create(
            character=character,
            section="assets",
            is_success=True,
            has_token_error=False,
            run_finished_at=clock,
        )
        stream = io.StringIO()
        with (
            CaptureQueriesContext(connection) as queries,
            redirect_stdout(stream),
            mock.patch(
                "celery.app.task.Task.apply_async",
                side_effect=AssertionError("collector queued a task"),
            ),
            mock.patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("collector called an API"),
            ),
        ):
            exec(compile(PROBE_CODE, "recovery_sync_probe", "exec"), {})
        self.assertTrue(queries)
        self.assertTrue(
            all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries),
            list(queries),
        )
        output = json.loads(stream.getvalue().removeprefix("BUH_SYNC="))
        self.assertEqual(output["moon_tax_config"], [{"audit_interval_hours": 4}])
        schedule = next(row for row in output["moon_tax_schedule"] if row["enabled"])
        self.assertEqual(schedule["interval__every"], 3600)
        self.assertEqual(
            output["asset_stale_minutes"],
            Character.UpdateSection.time_until_section_updates_are_stale()["assets"],
        )
        self.assertEqual(
            output["asset_characters"][0]["eve_character__character_id"],
            character.eve_character.character_id,
        )
        self.assertEqual(output["asset_status"][0]["character_id"], character.pk)
        self.assertTrue(output["asset_status"][0]["is_success"])
        self.assertFalse(output["asset_status"][0]["has_token_error"])
        self.assertEqual(
            output["completed_audits"],
            [
                {
                    "status": "COMPLETE",
                    "queued_at": str(completed.queued_at),
                    "source_refresh_requested_at": str(clock),
                    "finished_at": str(clock + timedelta(minutes=3)),
                    "warning_schema_valid": True,
                    "warning_count": 0,
                    "warning_categories": {},
                    "has_error": False,
                    "price_snapshot_count": 1,
                    "incomplete_price_snapshot_count": 0,
                    "incomplete_price_snapshot_schema_valid": True,
                    "jita_depth_incomplete_price_snapshot_count": 0,
                }
            ],
        )

    def test_newest_warning_is_summarized_without_exposing_valuation_names(self):
        older = AuditRun.objects.create(
            trigger="SCHEDULED",
            status="COMPLETE",
            source_refresh_requested_at=now() - timedelta(hours=1),
            finished_at=now() - timedelta(minutes=50),
        )
        latest = AuditRun.objects.create(
            trigger="SCHEDULED",
            status="WARNING",
            source_refresh_requested_at=now() - timedelta(minutes=10),
            finished_at=now() - timedelta(minutes=5),
            warnings=[
                "Jita buy depth was insufficient for Synthetic Ore; "
                "2.00000000 compressed units were not priced."
            ],
        )
        PriceSnapshot.objects.create(
            audit_run=latest,
            compressed_type_id=23456,
            compressed_type_name="Synthetic Ore",
            requested_quantity=5,
            priced_quantity=3,
            weighted_unit_price=10,
            total_value=30,
            source="ESI Jita 4-4 buy-order depth",
            complete=False,
            raw_evidence={
                "orders_considered": 1,
                "orders_used": 1,
                "best_price": "10",
                "worst_price": "10",
                "shortfall": "2.00000000",
            },
        )
        stream = io.StringIO()

        with redirect_stdout(stream):
            exec(compile(PROBE_CODE, "recovery_sync_probe", "exec"), {})

        output = json.loads(stream.getvalue().removeprefix("BUH_SYNC="))
        audit = output["completed_audits"][0]
        self.assertNotEqual(older.pk, latest.pk)
        self.assertEqual(audit["status"], "WARNING")
        self.assertEqual(audit["warning_categories"], {"jita-depth-insufficient": 1})
        self.assertTrue(audit["warning_schema_valid"])
        self.assertEqual(audit["incomplete_price_snapshot_count"], 1)
        self.assertTrue(audit["incomplete_price_snapshot_schema_valid"])
        self.assertEqual(audit["jita_depth_incomplete_price_snapshot_count"], 1)
        self.assertNotIn("Synthetic Ore", stream.getvalue())

    def test_newest_failed_audit_is_not_hidden_by_older_complete(self):
        AuditRun.objects.create(
            trigger="SCHEDULED",
            status="COMPLETE",
            source_refresh_requested_at=now() - timedelta(hours=1),
            finished_at=now() - timedelta(minutes=50),
        )
        AuditRun.objects.create(
            trigger="SCHEDULED",
            status="FAILED",
            source_refresh_requested_at=now() - timedelta(minutes=10),
            finished_at=now() - timedelta(minutes=5),
            error="synthetic failure",
        )
        stream = io.StringIO()

        with redirect_stdout(stream):
            exec(compile(PROBE_CODE, "recovery_sync_probe", "exec"), {})

        audit = json.loads(stream.getvalue().removeprefix("BUH_SYNC="))[
            "completed_audits"
        ][0]
        self.assertEqual(audit["status"], "FAILED")
        self.assertTrue(audit["has_error"])
        self.assertNotIn("synthetic failure", stream.getvalue())
