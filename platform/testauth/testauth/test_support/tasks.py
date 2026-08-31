"""Small test-only tasks that prove a real worker can consume and return results."""

from celery import shared_task


@shared_task
def celery_roundtrip(value):
    return {"delivered": True, "value": value}
