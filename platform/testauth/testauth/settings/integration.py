"""MariaDB, Redis, real-Celery profile for blocking integration tests."""

import os

from .common import *  # noqa: F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("BUH_DB_NAME", "buh_test"),
        "USER": os.environ.get("BUH_DB_USER", "root"),
        "PASSWORD": os.environ.get("BUH_DB_PASSWORD", "test-only-root"),
        "HOST": os.environ.get("BUH_DB_HOST", "db"),
        "PORT": os.environ.get("BUH_DB_PORT", "3306"),
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "charset": "utf8mb4",
            "isolation_level": "read committed",
            "init_command": (
                "SET sql_mode='STRICT_TRANS_TABLES,ERROR_FOR_DIVISION_BY_ZERO,"
                "NO_ENGINE_SUBSTITUTION'"
            ),
        },
        "TEST": {
            "NAME": "test_buh_test",
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_unicode_ci",
        },
    }
}

BUH_REDIS_URL = os.environ.get("BUH_REDIS_URL", "redis://redis:6379")
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{BUH_REDIS_URL}/1",
    }
}
BROKER_URL = f"{BUH_REDIS_URL}/0"
CELERY_BROKER_URL = BROKER_URL
CELERY_RESULT_BACKEND = f"{BUH_REDIS_URL}/2"
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_EAGER_PROPAGATES = True
