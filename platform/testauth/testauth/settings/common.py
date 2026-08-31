"""Secret-free settings shared by every disposable test profile."""

import os
from urllib.parse import urlparse

from .base import *  # noqa: F403

ROOT_URLCONF = "testauth.urls"
WSGI_APPLICATION = "testauth.wsgi.application"
SECRET_KEY = os.environ.get("BUH_TEST_SECRET", "buh-disposable-tests-only")
SITE_NAME = "B-UH Test Auth"
SITE_URL = os.environ.get("BUH_TEST_SITE_URL", "http://127.0.0.1:8000")
ALLOWED_HOSTS = [urlparse(SITE_URL).hostname, "localhost", "127.0.0.1", "web"]
CSRF_TRUSTED_ORIGINS = [SITE_URL]
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
DEBUG = False
DISPLAY_DEBUG = False

for app in (
    "eveuniverse",
    "structures",
    "moonmining",
    "memberaudit",
    "buh_structure_ops",
    "buh_mining_analytics",
    "buh_moon_tax",
    "testauth.test_support",
):
    if app not in INSTALLED_APPS:  # noqa: F405
        INSTALLED_APPS.append(app)  # noqa: F405

STATICFILES_DIRS = []
STATIC_ROOT = os.environ.get("BUH_TEST_STATIC_ROOT", "/tmp/buh-test-static")
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
}
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
REGISTRATION_VERIFY_EMAIL = False

ESI_SSO_CALLBACK_URL = f"{SITE_URL}/sso/callback"
ESI_SSO_CLIENT_ID = "test-only-client"
ESI_SSO_CLIENT_SECRET = "test-only-secret"
ESI_USER_CONTACT_EMAIL = "test@example.invalid"
BUH_FAKE_ESI_ROOT = os.environ.get(
    "BUH_FAKE_ESI_ROOT", "http://127.0.0.1:18080/"
).rstrip("/")
ESI_API_URL = f"{BUH_FAKE_ESI_ROOT}/"

BUH_TEST_SUPPORT_ENABLED = os.environ.get("BUH_TEST_SUPPORT_ENABLED", "0") == "1"
BUH_MOON_TAX_ESI_BASE_URL = os.environ.get(
    "BUH_FAKE_ESI_URL", f"{BUH_FAKE_ESI_ROOT}/latest"
).rstrip("/")

LOGIN_TOKEN_SCOPES = sorted(  # noqa: F405
    set(LOGIN_TOKEN_SCOPES)  # noqa: F405
    | {
        "esi-contracts.read_character_contracts.v1",
        "esi-wallet.read_character_wallet.v1",
    }
)

# Mirror the production task topology without ever running a beat service in CI.
# The disposable worker can execute these tasks explicitly when a test needs them.
CELERYBEAT_SCHEDULE.update(  # noqa: F405
    {
        "buh-test-structures": {
            "task": "structures.tasks.update_all_structures",
            "schedule": 3600,
        },
        "buh-test-structure-notifications": {
            "task": "structures.tasks.fetch_all_notifications",
            "schedule": 3600,
        },
        "buh-test-moonmining": {
            "task": "moonmining.tasks.run_regular_updates",
            "schedule": 3600,
        },
        "buh-test-structure-ops": {
            "task": "buh_structure_ops.tasks.capture_and_evaluate",
            "schedule": 3600,
        },
        "buh-test-moon-tax": {
            "task": "buh_moon_tax.tasks.run_scheduled_audit",
            "schedule": 3600,
        },
    }
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "WARNING")},
}
