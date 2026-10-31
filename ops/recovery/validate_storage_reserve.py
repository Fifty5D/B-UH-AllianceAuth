"""Run isolated SQLite reserve regressions without Auth or production services.

Dependencies: Django 5.2.17, django-celery-beat 2.9.0, Celery 5.6.3.
This profile bypasses startup hooks and supplies only a lock-module stub on
Windows. It tests archive logic and migrations, not Linux locking or full Auth.
"""

import sys
from pathlib import Path
from types import ModuleType

import django
from django.apps import AppConfig
from django.conf import settings
from django.core.management import call_command
from django.test.runner import DiscoverRunner


class ReserveTestConfig(AppConfig):
    name = "buh_max_history"
    default_auto_field = "django.db.models.BigAutoField"


def main():
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "apps/esi-history/src"))
    # collection/public_archive import job_lock. These tests exercise neither
    # flock nor multi-process locking; those remain Linux integration checks.
    if sys.platform == "win32":
        sys.modules["fcntl"] = ModuleType("fcntl")
    settings.configure(
        SECRET_KEY="synthetic-reserve-tests-only",
        INSTALLED_APPS=[
            "django.contrib.auth", "django.contrib.contenttypes", "django_celery_beat",
            "__main__.ReserveTestConfig",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()
    call_command("makemigrations", "buh_max_history", check=True, dry_run=True, verbosity=1)
    return DiscoverRunner(verbosity=2).run_tests(["buh_max_history.tests.test_storage_reserve"])


if __name__ == "__main__":
    raise SystemExit(bool(main()))
