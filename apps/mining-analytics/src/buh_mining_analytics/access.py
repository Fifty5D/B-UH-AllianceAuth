"""Server-side access control and selection scoping."""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.db.models import QuerySet
from memberaudit.models import Character

SCOPE_MINE = "mine"
SCOPE_CORP_MEMBERS = "corp_members"
SCOPE_CORP_CHARACTERS = "corp_characters"
SCOPE_ALL = "all"


@dataclass(frozen=True)
class ScopeOption:
    """A scope a user can choose in the dashboard."""

    value: str
    label: str
    description: str


def has_app_access(user: User) -> bool:
    """Return whether a user can open any Mining Analytics view."""

    return bool(
        user.is_authenticated
        and (
            user.has_perm("buh_mining_analytics.basic_access")
            or user.has_perm("buh_mining_analytics.view_corporation")
            or user.has_perm("buh_mining_analytics.view_all")
        )
    )


def main_corporation_id(user: User) -> int | None:
    """Return the corporation ID of the user's Auth main character."""

    try:
        main_character = user.profile.main_character
    except (AttributeError, ObjectDoesNotExist):
        return None
    return main_character.corporation_id if main_character else None


def available_scopes(user: User) -> list[ScopeOption]:
    """Return every scope the user may select."""

    if not has_app_access(user):
        return []

    scopes = [
        ScopeOption(
            SCOPE_MINE,
            "My characters",
            "Every registered character owned by your Auth account",
        )
    ]
    if user.has_perm("buh_mining_analytics.view_corporation") and main_corporation_id(
        user
    ):
        scopes.extend(
            [
                ScopeOption(
                    SCOPE_CORP_MEMBERS,
                    "Corporation members",
                    "All registered characters owned by members whose Auth main is in your corporation",
                ),
                ScopeOption(
                    SCOPE_CORP_CHARACTERS,
                    "In-corp characters",
                    "Only registered characters currently in your corporation",
                ),
            ]
        )
    if user.has_perm("buh_mining_analytics.view_all"):
        scopes.append(
            ScopeOption(
                SCOPE_ALL,
                "All Auth characters",
                "Every registered Member Audit character on this Auth installation",
            )
        )
    return scopes


def validate_scope(user: User, scope: str) -> str:
    """Validate and return a requested scope or raise PermissionDenied."""

    allowed = {item.value for item in available_scopes(user)}
    if scope not in allowed:
        raise PermissionDenied("You do not have permission to use that mining scope.")
    return scope


def characters_for_scope(user: User, scope: str) -> QuerySet[Character]:
    """Return the Member Audit characters visible inside one exact scope."""

    validate_scope(user, scope)
    queryset = Character.objects.filter(
        eve_character__character_ownership__isnull=False
    )

    if scope == SCOPE_MINE:
        queryset = queryset.filter(eve_character__character_ownership__user_id=user.pk)
    elif scope == SCOPE_CORP_MEMBERS:
        corporation_id = main_corporation_id(user)
        if not corporation_id:
            raise PermissionDenied("Your Auth main character has no corporation.")
        queryset = queryset.filter(
            eve_character__character_ownership__user__profile__main_character__corporation_id=corporation_id
        )
    elif scope == SCOPE_CORP_CHARACTERS:
        corporation_id = main_corporation_id(user)
        if not corporation_id:
            raise PermissionDenied("Your Auth main character has no corporation.")
        queryset = queryset.filter(eve_character__corporation_id=corporation_id)
    elif scope != SCOPE_ALL:
        raise PermissionDenied("Unknown mining scope.")

    return queryset.select_related(
        "eve_character",
        "eve_character__character_ownership__user",
        "eve_character__character_ownership__user__profile",
        "eve_character__character_ownership__user__profile__main_character",
    ).order_by("eve_character__character_name")


def scope_label(user: User, scope: str) -> str:
    """Return a display label for a validated scope."""

    validate_scope(user, scope)
    return next(item.label for item in available_scopes(user) if item.value == scope)
