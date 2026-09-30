"""Signal handlers for automatic registration."""

import logging
from weakref import WeakSet

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from esi.models import Token

from .services import RegistrationStatus, register_token

logger = logging.getLogger(__name__)
_tokens_awaiting_user = WeakSet()


def _log_result(result) -> None:
    """Write useful registration outcomes to the Auth log."""

    if result.status == RegistrationStatus.REGISTERED:
        logger.info(
            "Automatically registered EVE character %s in Member Audit for user %s.",
            result.character_id,
            result.user_id,
        )
    elif result.status == RegistrationStatus.REENABLED:
        logger.info(
            "Automatically re-enabled EVE character %s in Member Audit for user %s.",
            result.character_id,
            result.user_id,
        )
    elif result.status == RegistrationStatus.MISSING_OWNERSHIP:
        logger.warning(
            "Skipped Member Audit auto-registration for EVE character %s: "
            "the token user does not own this character in Alliance Auth.",
            result.character_id,
        )


@receiver(
    m2m_changed,
    sender=Token.scopes.through,
    dispatch_uid="buh_memberaudit_autoreg.token_scopes_changed",
)
def token_scopes_changed(sender, instance, action, reverse, **kwargs):  # noqa: ARG001
    """Register a character after django-esi finishes adding all required scopes."""

    if action != "post_add" or reverse:
        return

    result = register_token(instance)
    if result.status == RegistrationStatus.MISSING_USER:
        # Initial SSO/login tokens receive their user only after all scopes are
        # attached. Remember this exact in-memory token until the following save.
        _tokens_awaiting_user.add(instance)
        return
    _log_result(result)


@receiver(
    post_save,
    sender=Token,
    dispatch_uid="buh_memberaudit_autoreg.token_user_attached",
)
def token_user_attached(sender, instance, **kwargs):  # noqa: ARG001
    """Finish registration when core Auth attaches a user after EVE SSO."""

    if instance not in _tokens_awaiting_user or not instance.user_id:
        return
    _tokens_awaiting_user.discard(instance)
    _log_result(register_token(instance))


@receiver(
    post_delete,
    sender=Token,
    dispatch_uid="buh_memberaudit_autoreg.token_deleted",
)
def token_deleted(sender, instance, **kwargs):  # noqa: ARG001
    """Discard abandoned pending tokens without retaining memory."""

    _tokens_awaiting_user.discard(instance)
