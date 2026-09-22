"""Serialize refresh and invalid-token deletion for pinned django-esi 9.6.0.

Different app tasks can hold the same expired token in memory. A refresh must
reload that row under a database lock before using its rotating refresh token.
Deletion after a rejected refresh belongs to the same critical section.
"""

import logging
from functools import wraps

import esi
from django.db import router, transaction
from esi.errors import IncompleteResponseError, TokenError
from esi.managers import TokenQueryset
from esi.models import Token

logger = logging.getLogger(__name__)
_MARKER = "_buh_serialized_refresh"
_IDENTITY_FIELDS = ("user_id", "character_id", "character_owner_hash", "token_type")
_REFRESH_FIELDS = ("access_token", "refresh_token", "created", "sso_version")


def _database(token):
    return token._state.db or router.db_for_write(type(token), instance=token)


def _locked(token, alias):
    current = Token.objects.using(alias).select_for_update().get(pk=token.pk)
    if any(getattr(current, field) != getattr(token, field) for field in _IDENTITY_FIELDS):
        # Do not refresh, expose credentials, or delete a reassigned row using an
        # old caller's identity. This exception is deliberately not a TokenError.
        raise IncompleteResponseError("Token identity changed while awaiting refresh")
    return current


def install_token_refresh_guard() -> bool:
    """Install once for the reviewed dependency; future versions need review."""
    if esi.__version__ != "9.6.0":
        return False
    if getattr(Token.refresh, _MARKER, False):
        return False
    original_refresh = Token.refresh
    original_refresh_or_delete = Token.refresh_or_delete

    @wraps(original_refresh)
    def refresh(token, session=None, auth=None):
        if token.pk is None:
            return original_refresh(token, session=session, auth=auth)
        alias = _database(token)
        with transaction.atomic(using=alias):
            current = _locked(token, alias)
            if current.created <= token.created or current.expired:
                original_refresh(current, session=session, auth=auth)
            # A sibling already refreshed this same identity: reuse its result.
            # Keep the caller's instance current without saving a stale row.
            for field in _REFRESH_FIELDS:
                setattr(token, field, getattr(current, field))

    @wraps(original_refresh_or_delete)
    def refresh_or_delete(token):
        if token.pk is None:
            return original_refresh_or_delete(token)
        alias = _database(token)
        with transaction.atomic(using=alias):
            try:
                _locked(token, alias)
            except Token.DoesNotExist:
                return None
            return original_refresh_or_delete(token)

    @wraps(TokenQueryset.bulk_refresh)
    def bulk_refresh(queryset):
        alias = queryset.db
        observed = list(queryset.values_list("pk", "created").distinct())
        accepted = []
        for pk, created in observed:
            # Lock only the token row, not the joined scope/user tables. Finish
            # each token before locking another, including on failure.
            with transaction.atomic(using=alias):
                current = (
                    Token.objects.using(alias).select_for_update().filter(pk=pk).first()
                )
                if current is None or not queryset.filter(pk=pk).exists():
                    continue
                if current.created > created and not current.expired:
                    accepted.append(pk)
                    continue
                if not current.can_refresh:
                    if current.expired:
                        current.delete(using=alias)
                    else:
                        accepted.append(pk)
                    continue
                try:
                    current.refresh()
                except TokenError:
                    current.delete(using=alias)
                    logger.warning("Rejected ESI token removed during locked refresh")
                except IncompleteResponseError:
                    # Temporary/incomplete SSO replies remain retryable. Never
                    # return an expired access token or delete its grant.
                    continue
                else:
                    accepted.append(pk)
        return queryset.filter(pk__in=accepted)

    setattr(refresh, _MARKER, True)
    Token.refresh = refresh
    Token.refresh_or_delete = refresh_or_delete
    TokenQueryset.bulk_refresh = bulk_refresh
    return True
