from django.core.cache import cache
from django.db import connection
from django.test import TestCase

from testauth.celery import app


class SourcePlatformServiceTests(TestCase):
    def test_integration_lane_uses_mariadb(self):
        self.assertEqual(connection.vendor, "mysql")

    def test_redis_cache_round_trip(self):
        cache.set("buh-integration", {"ok": True}, timeout=10)
        self.assertEqual(cache.get("buh-integration"), {"ok": True})

    def test_owned_celery_tasks_are_registered(self):
        app.loader.import_default_modules()
        expected = {
            "buh_moon_tax.tasks.run_scheduled_audit",
            "buh_structure_ops.tasks.capture_and_evaluate",
            "testauth.test_support.tasks.celery_roundtrip",
        }
        self.assertTrue(expected.issubset(app.tasks), expected - set(app.tasks))
