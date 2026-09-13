"""Tests for maximum-history services."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from memberaudit.models import Character

from buh_max_history import checks
from buh_max_history.management.commands.buh_history_report import _format_range, _stamp
from buh_max_history.services import (
    backfill_character_mail,
    configure_runtime_mail_limit,
    queue_non_mail_sections,
)


class ConfigurationTests(SimpleTestCase):
    def test_unlimited_retention_check_passes(self):
        with patch("memberaudit.app_settings.MEMBERAUDIT_DATA_RETENTION_LIMIT", None):
            self.assertEqual(checks.check_unlimited_memberaudit_retention(), [])

    def test_runtime_mail_limit_is_process_local_and_returns_old_value(self):
        from memberaudit.managers import character_sections_2

        original = character_sections_2.MEMBERAUDIT_MAX_MAILS
        try:
            old = configure_runtime_mail_limit(999_999)
            self.assertEqual(old, original)
            self.assertEqual(character_sections_2.MEMBERAUDIT_MAX_MAILS, 999_999)
        finally:
            character_sections_2.MEMBERAUDIT_MAX_MAILS = original

    def test_runtime_mail_limit_rejects_too_small_value(self):
        with self.assertRaises(ValueError):
            configure_runtime_mail_limit(249)


class ReportFormattingTests(SimpleTestCase):
    def test_stamp_formats_datetime_to_seconds(self):
        value = datetime(2026, 8, 23, 3, 24, 38, 123456, tzinfo=UTC)

        self.assertEqual(_stamp(value), "2026-08-23T03:24:38+00:00")

    def test_stamp_formats_date_without_datetime_timespec(self):
        self.assertEqual(_stamp(date(2026, 8, 23)), "2026-08-23")

    def test_mining_range_accepts_date_only_values(self):
        output = _format_range(
            "mining ledger",
            {
                "count": 2,
                "oldest": date(2026, 5, 25),
                "newest": date(2026, 8, 23),
            },
        )

        self.assertIn("oldest=2026-05-25", output)
        self.assertIn("newest=2026-08-23", output)


class QueueTests(SimpleTestCase):
    @patch("buh_max_history.services.tasks.update_character_assets")
    @patch("buh_max_history.services.Character.UpdateSection.enabled_sections")
    def test_queue_excludes_mail_but_includes_other_sections(
        self, enabled_sections, assets_task
    ):
        enabled_sections.return_value = {
            Character.UpdateSection.MAILS,
            Character.UpdateSection.ASSETS,
        }
        character = MagicMock(pk=42)

        count = queue_non_mail_sections(character)

        self.assertEqual(count, 1)
        assets_task.apply_async.assert_called_once()
        call = assets_task.apply_async.call_args.kwargs
        self.assertEqual(call["kwargs"], {"character_pk": 42, "force_update": True})


class MailBackfillTests(SimpleTestCase):
    @patch("buh_max_history.services.time.sleep")
    def test_backfill_loads_only_missing_bodies_and_records_success(self, sleep):
        character = MagicMock()
        character.perform_update_with_error_logging.return_value = MagicMock(
            is_updated=True
        )
        character.mails.count.return_value = 10
        character.mails.filter.return_value.order_by.return_value.values_list.return_value = [
            9002,
            9001,
        ]
        character.mails.get.side_effect = [MagicMock(), MagicMock()]
        output = []

        result = backfill_character_mail(
            character=character,
            body_delay=0.2,
            max_retries=3,
            progress=output.append,
        )

        self.assertTrue(result.is_success)
        self.assertEqual(result.header_count, 10)
        self.assertEqual(result.bodies_requested, 2)
        self.assertEqual(result.bodies_loaded, 2)
        self.assertEqual(character.update_mail_body.call_count, 2)
        character.update_section_log_result.assert_called_once()
        self.assertEqual(sleep.call_count, 2)
