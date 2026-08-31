"""Database selection, normalization, diagnostics, and dashboard services."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Max, Min, QuerySet
from django.utils import timezone
from memberaudit.models import (
    Character,
    CharacterMiningLedgerEntry,
    CharacterUpdateStatus,
)

from . import app_settings
from .access import characters_for_scope, scope_label
from .analytics import MiningRecord, build_analytics


class SelectionTooLarge(ValidationError):
    """Raised when a request selects an unsafe number of characters."""


@dataclass(frozen=True)
class Selection:
    """A validated character selection."""

    queryset: QuerySet[Character]
    character_ids: tuple[int, ...]
    account_ids: tuple[int, ...]

    @property
    def character_count(self) -> int:
        return len(self.character_ids)

    @property
    def account_count(self) -> int:
        return len(self.account_ids)


@dataclass(frozen=True)
class DateRange:
    """A validated reporting date range."""

    start: dt.date
    end: dt.date
    preset: str
    include_previous: bool


def _account_name(user) -> str:
    try:
        main = user.profile.main_character
    except (AttributeError, ObjectDoesNotExist):
        main = None
    return main.character_name if main else user.username


def selector_options(user, scope: str) -> dict:
    """Return privacy-safe Auth-account and character selector options."""

    characters = list(characters_for_scope(user, scope))
    accounts: dict[int, dict] = {}
    character_options = []

    for character in characters:
        eve_character = character.eve_character
        owner = eve_character.character_ownership.user
        account = accounts.setdefault(
            owner.pk,
            {
                "id": owner.pk,
                "name": _account_name(owner),
                "character_count": 0,
            },
        )
        account["character_count"] += 1
        character_options.append(
            {
                "id": character.pk,
                "eve_character_id": eve_character.character_id,
                "name": eve_character.character_name,
                "account_id": owner.pk,
                "account_name": account["name"],
                "corporation_id": eve_character.corporation_id,
                "corporation_name": eve_character.corporation_name,
                "disabled": character.is_disabled,
            }
        )

    return {
        "scope": scope,
        "scope_label": scope_label(user, scope),
        "accounts": sorted(accounts.values(), key=lambda item: item["name"].casefold()),
        "characters": sorted(
            character_options, key=lambda item: item["name"].casefold()
        ),
    }


def apply_selection(
    user,
    scope: str,
    account_ids: list[int] | None,
    character_ids: list[int] | None,
) -> Selection:
    """Intersect requested IDs with a user's exact server-side scope."""

    queryset = characters_for_scope(user, scope)
    if account_ids is not None:
        if account_ids:
            queryset = queryset.filter(
                eve_character__character_ownership__user_id__in=account_ids
            )
        else:
            queryset = queryset.none()
    if character_ids is not None:
        if character_ids:
            queryset = queryset.filter(pk__in=character_ids)
        else:
            queryset = queryset.none()

    queryset = queryset.distinct()
    resolved = list(
        queryset.values_list("pk", "eve_character__character_ownership__user_id")
    )
    if len(resolved) > app_settings.MAX_SELECTED_CHARACTERS:
        raise SelectionTooLarge(
            f"Please select {app_settings.MAX_SELECTED_CHARACTERS} or fewer characters."
        )

    selected_character_ids = tuple(row[0] for row in resolved)
    selected_account_ids = tuple(sorted({row[1] for row in resolved}))
    return Selection(
        queryset=queryset.filter(pk__in=selected_character_ids),
        character_ids=selected_character_ids,
        account_ids=selected_account_ids,
    )


def stored_date_range(selection: Selection) -> tuple[dt.date | None, dt.date | None]:
    """Return the oldest and newest mining ledger dates in a selection."""

    aggregate = CharacterMiningLedgerEntry.objects.filter(
        character_id__in=selection.character_ids
    ).aggregate(oldest=Min("date"), newest=Max("date"))
    return aggregate["oldest"], aggregate["newest"]


def resolve_date_range(
    selection: Selection,
    preset: str,
    start_text: str | None = None,
    end_text: str | None = None,
) -> DateRange:
    """Resolve date presets and custom input into a safe UTC date range."""

    today = timezone.now().date()
    preset = (
        preset if preset in {"7d", "30d", "90d", "month", "all", "custom"} else "30d"
    )

    if preset == "7d":
        start, end = today - dt.timedelta(days=6), today
    elif preset == "90d":
        start, end = today - dt.timedelta(days=89), today
    elif preset == "month":
        start, end = today.replace(day=1), today
    elif preset == "all":
        oldest, newest = stored_date_range(selection)
        start = oldest or (today - dt.timedelta(days=app_settings.DEFAULT_DAYS - 1))
        end = max(today, newest) if newest else today
    elif preset == "custom":
        try:
            start = dt.date.fromisoformat(start_text or "")
            end = dt.date.fromisoformat(end_text or "")
        except ValueError as exc:
            raise ValidationError("Enter valid start and end dates.") from exc
        if end < start:
            raise ValidationError("The end date must be on or after the start date.")
        if (end - start).days > 3_650:
            raise ValidationError("Custom ranges are limited to ten years.")
    else:
        start, end = today - dt.timedelta(days=29), today
        preset = "30d"

    span = (end - start).days + 1
    return DateRange(
        start=start,
        end=end,
        preset=preset,
        include_previous=preset != "all" and span <= 366,
    )


def _entry_queryset(selection: Selection, start: dt.date, end: dt.date):
    return (
        CharacterMiningLedgerEntry.objects.filter(
            character_id__in=selection.character_ids,
            date__gte=start,
            date__lte=end,
        )
        .select_related(
            "character__eve_character",
            "character__eve_character__character_ownership__user",
            "character__eve_character__character_ownership__user__profile",
            "character__eve_character__character_ownership__user__profile__main_character",
            "eve_type",
            "eve_type__market_price",
            "eve_solar_system",
            "eve_solar_system__eve_constellation",
            "eve_solar_system__eve_constellation__eve_region",
        )
        .order_by("date", "character__eve_character__character_name", "eve_type__name")
    )


def normalize_entry(entry: CharacterMiningLedgerEntry) -> MiningRecord:
    """Convert one Member Audit model to a stable analytics record."""

    character = entry.character
    eve_character = character.eve_character
    owner = eve_character.character_ownership.user
    eve_type = entry.eve_type

    try:
        average_price = eve_type.market_price.average_price
    except (AttributeError, ObjectDoesNotExist):
        average_price = None
    unit_price = float(average_price) if average_price is not None else 0.0
    unit_volume = float(eve_type.volume) if eve_type.volume is not None else 0.0

    try:
        region_name = entry.eve_solar_system.eve_constellation.eve_region.name
    except AttributeError:
        region_name = "Unknown region"

    return MiningRecord(
        date=entry.date,
        character_id=character.pk,
        character_name=eve_character.character_name,
        account_id=owner.pk,
        account_name=_account_name(owner),
        corporation_name=eve_character.corporation_name,
        ore_id=eve_type.id,
        ore_name=eve_type.name,
        system_name=entry.eve_solar_system.name,
        region_name=region_name,
        quantity=int(entry.quantity),
        volume=int(entry.quantity) * unit_volume,
        isk=int(entry.quantity) * unit_price,
        price_known=average_price is not None,
        volume_known=eve_type.volume is not None,
    )


def records_for_range(
    selection: Selection, start: dt.date, end: dt.date
) -> list[MiningRecord]:
    """Load and normalize ledger records for a date range."""

    return [normalize_entry(entry) for entry in _entry_queryset(selection, start, end)]


def update_health(selection: Selection) -> dict:
    """Summarize Member Audit mining-section freshness for selected characters."""

    statuses = CharacterUpdateStatus.objects.filter(
        character_id__in=selection.character_ids,
        section=Character.UpdateSection.MINING_LEDGER,
    )
    aggregate = statuses.aggregate(
        last_attempt=Max("run_finished_at"),
        last_changed=Max("update_finished_at"),
    )
    return {
        "last_attempt": (
            aggregate["last_attempt"].isoformat() if aggregate["last_attempt"] else None
        ),
        "last_changed": (
            aggregate["last_changed"].isoformat() if aggregate["last_changed"] else None
        ),
        "ok": statuses.filter(is_success=True, has_token_error=False).count(),
        "errors": statuses.filter(is_success=False).count(),
        "token_errors": statuses.filter(has_token_error=True).count(),
        "never_updated": max(0, selection.character_count - statuses.count()),
        "disabled": Character.objects.filter(
            pk__in=selection.character_ids, is_disabled=True
        ).count(),
    }


def build_dashboard_payload(
    user,
    scope: str,
    selection: Selection,
    date_range: DateRange,
    comparison: str,
) -> dict:
    """Build the complete JSON response for the dashboard."""

    current = records_for_range(selection, date_range.start, date_range.end)
    previous = None
    previous_range = None
    if date_range.include_previous:
        span = (date_range.end - date_range.start).days + 1
        previous_end = date_range.start - dt.timedelta(days=1)
        previous_start = previous_end - dt.timedelta(days=span - 1)
        previous = records_for_range(selection, previous_start, previous_end)
        previous_range = {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
        }

    analytics = build_analytics(
        current,
        start=date_range.start,
        end=date_range.end,
        comparison=comparison,
        previous_records=previous,
        selected_character_count=selection.character_count,
        selected_account_count=selection.account_count,
    )
    oldest, newest = stored_date_range(selection)

    return {
        "meta": {
            "scope": scope,
            "scope_label": scope_label(user, scope),
            "start": date_range.start.isoformat(),
            "end": date_range.end.isoformat(),
            "preset": date_range.preset,
            "previous_range": previous_range,
            "selected_character_ids": list(selection.character_ids),
            "selected_character_count": selection.character_count,
            "selected_account_count": selection.account_count,
            "stored_oldest": oldest.isoformat() if oldest else None,
            "stored_newest": newest.isoformat() if newest else None,
            "generated_at": timezone.now().isoformat(),
            "update_health": update_health(selection),
            "source_note": (
                "CCP exposes the personal mining ledger for the previous 30 days. "
                "Member Audit keeps imported mining rows, so this dashboard can chart "
                "older dates that were collected while the character was registered."
            ),
        },
        **analytics,
    }


def iter_export_records(
    selection: Selection, date_range: DateRange
) -> Iterable[MiningRecord]:
    """Yield normalized rows for CSV export with a hard safety limit."""

    queryset = _entry_queryset(selection, date_range.start, date_range.end)
    row_count = queryset.count()
    if row_count > app_settings.MAX_EXPORT_ROWS:
        raise SelectionTooLarge(
            f"This export contains {row_count:,} rows. Narrow the selection below "
            f"{app_settings.MAX_EXPORT_ROWS:,} rows and try again."
        )
    for entry in queryset.iterator(chunk_size=2_000):
        yield normalize_entry(entry)
