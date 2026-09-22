"""Retry only the pinned Moon Mining time-based SQL status transitions."""

import time
from functools import wraps

import moonmining
from django.db import OperationalError, connections, transaction
from moonmining.managers import ExtractionQuerySet

_MARKER = "_buh_status_deadlock_retry"


def install_moonmining_status_guard() -> bool:
    if moonmining.__version__ != "3.1.0.post1":
        return False
    if getattr(ExtractionQuerySet.update_status, _MARKER, False):
        return False
    original_status = ExtractionQuerySet.update_status

    @wraps(original_status)
    def update_status(queryset):
        connection = connections[queryset.db]
        nested = connection.in_atomic_block
        for attempt in range(3):
            try:
                # Retry just the two idempotent SQL status transitions. Do not
                # replay ESI calls, notification processing, or a caller's work.
                with transaction.atomic(using=queryset.db):
                    return original_status(queryset)
            except OperationalError as error:
                if (
                    connection.vendor != "mysql"
                    or nested
                    or not error.args
                    or error.args[0] != 1213
                    or attempt == 2
                ):
                    raise
                time.sleep(0.05 * (attempt + 1))

    setattr(update_status, _MARKER, True)
    ExtractionQuerySet.update_status = update_status
    return True
