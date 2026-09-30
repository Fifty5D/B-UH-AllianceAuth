"""Core registration service shared by signals and management commands."""

from dataclasses import dataclass
from enum import Enum
from functools import partial

from django.db import transaction

from esi.models import Token

from allianceauth.authentication.models import CharacterOwnership
from memberaudit import tasks
from memberaudit.app_settings import MEMBERAUDIT_TASKS_NORMAL_PRIORITY
from memberaudit.models import Character, ComplianceGroupDesignation


class RegistrationStatus(str, Enum):
    """Possible outcomes from evaluating a token."""

    REGISTERED = "registered"
    REENABLED = "reenabled"
    ALREADY_REGISTERED = "already_registered"
    MISSING_USER = "missing_user"
    MISSING_SCOPES = "missing_scopes"
    MISSING_OWNERSHIP = "missing_ownership"


@dataclass(frozen=True)
class RegistrationResult:
    """Result returned by ``register_token``."""

    status: RegistrationStatus
    character_id: int
    user_id: int | None
    memberaudit_character_pk: int | None = None

    @property
    def changed(self) -> bool:
        """Return whether Member Audit state was created or re-enabled."""

        return self.status in {
            RegistrationStatus.REGISTERED,
            RegistrationStatus.REENABLED,
        }


def _queue_memberaudit_updates(character_pk: int, user_pk: int) -> None:
    tasks.update_character.apply_async(
        kwargs={
            "character_pk": character_pk,
            "force_update": True,
            "ignore_stale": True,
        },
        priority=MEMBERAUDIT_TASKS_NORMAL_PRIORITY,
    )
    if ComplianceGroupDesignation.objects.exists():
        tasks.update_compliance_groups_for_user.apply_async(
            args=[user_pk], priority=MEMBERAUDIT_TASKS_NORMAL_PRIORITY
        )


def register_token(
    token: Token,
    *,
    dry_run: bool = False,
    queue_updates: bool = True,
) -> RegistrationResult:
    """Register a full-scope ESI token's character in Member Audit.

    The function is idempotent. Tokens without a user, incomplete scope sets,
    and tokens whose character is not owned by the same Auth user are ignored.
    """

    required_scopes = set(Character.esi_scopes())
    granted_scopes = set(token.scopes.values_list("name", flat=True))
    if not required_scopes.issubset(granted_scopes):
        return RegistrationResult(
            RegistrationStatus.MISSING_SCOPES,
            token.character_id,
            token.user_id,
        )

    if not token.user_id:
        return RegistrationResult(
            RegistrationStatus.MISSING_USER, token.character_id, None
        )

    ownership = (
        CharacterOwnership.objects.select_related("character")
        .filter(
            character__character_id=token.character_id,
            user_id=token.user_id,
            owner_hash=token.character_owner_hash,
        )
        .first()
    )
    if ownership is None:
        return RegistrationResult(
            RegistrationStatus.MISSING_OWNERSHIP,
            token.character_id,
            token.user_id,
        )

    audit_character = Character.objects.filter(
        eve_character=ownership.character
    ).first()
    if dry_run:
        if audit_character is None:
            status = RegistrationStatus.REGISTERED
        elif audit_character.is_disabled:
            status = RegistrationStatus.REENABLED
        else:
            status = RegistrationStatus.ALREADY_REGISTERED
        return RegistrationResult(
            status,
            token.character_id,
            token.user_id,
            audit_character.pk if audit_character else None,
        )

    with transaction.atomic():
        audit_character, created = Character.objects.get_or_create(
            eve_character=ownership.character,
            defaults={"is_disabled": False},
        )
        if created:
            status = RegistrationStatus.REGISTERED
        elif audit_character.is_disabled:
            status = RegistrationStatus.REENABLED
            audit_character.is_disabled = False
            audit_character.save(update_fields=["is_disabled"])
        else:
            status = RegistrationStatus.ALREADY_REGISTERED

        if queue_updates and status in {
            RegistrationStatus.REGISTERED,
            RegistrationStatus.REENABLED,
        }:
            transaction.on_commit(
                partial(
                    _queue_memberaudit_updates,
                    audit_character.pk,
                    token.user_id,
                )
            )

    return RegistrationResult(
        status,
        token.character_id,
        token.user_id,
        audit_character.pk,
    )
