"""Asynchronous section repair after a new full-scope authorization."""

from celery import shared_task

from .recovery import recover_character


@shared_task(time_limit=120, max_retries=0)
def recover_character_sections(character_pk):
    """Keep token refresh and ESI status checks outside the SSO response."""
    return recover_character(character_pk, apply=True)
