"""Prove that the disposable broker, worker, and result backend work end to end."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from testauth.test_support.tasks import celery_roundtrip


class Command(BaseCommand):
    help = "Send a test-only task through Celery and verify its returned result."

    def handle(self, *args, **options):
        if not getattr(settings, "BUH_TEST_SUPPORT_ENABLED", False):
            raise CommandError("Test support is disabled in this settings profile.")
        marker = "buh-celery-roundtrip"
        result = celery_roundtrip.delay(marker)
        try:
            payload = result.get(timeout=20, disable_sync_subtasks=False)
        except Exception as exc:
            raise CommandError(f"Celery round trip failed: {exc}") from exc
        expected = {"delivered": True, "value": marker}
        if payload != expected:
            raise CommandError(f"Unexpected Celery result: {payload!r}")
        self.stdout.write(self.style.SUCCESS("Celery round trip succeeded."))
