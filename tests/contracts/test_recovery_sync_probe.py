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

from buh_moon_tax.models import AuditRun, TaxConfiguration
from ops.deploy.recovery_sync import PROBE_CODE


class RecoverySyncProbeTests(TestCase):
    def test_actual_orm_fields_schedules_success_and_identity_are_read_without_updates(
        self,
    ):
        TaxConfiguration.objects.update_or_create(
            singleton_id=1, defaults={"audit_interval_hours": 4}
        )
        clock = now() - timedelta(minutes=10)
        AuditRun.objects.create(
            trigger="SCHEDULED",
            status="COMPLETE",
            source_refresh_requested_at=clock,
            finished_at=clock + timedelta(minutes=3),
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
