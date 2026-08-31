"""Pure aggregation helpers used by the dashboard and tests."""

from __future__ import annotations

import datetime as dt
import itertools
import math
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass

PALETTE = (
    "#59d4c8",
    "#f5b942",
    "#6ea8fe",
    "#b58cff",
    "#ff7b72",
    "#78d46a",
    "#f28cc7",
    "#65c7f7",
    "#d5a86c",
    "#9aa7b8",
    "#ef8f6a",
    "#8bb8ff",
)


@dataclass(frozen=True)
class MiningRecord:
    """One normalized Member Audit mining ledger row."""

    date: dt.date
    character_id: int
    character_name: str
    account_id: int
    account_name: str
    corporation_name: str
    ore_id: int
    ore_name: str
    system_name: str
    region_name: str
    quantity: int
    volume: float
    isk: float
    price_known: bool
    volume_known: bool


def _empty_values() -> dict[str, float | int]:
    return {"isk": 0.0, "volume": 0.0, "units": 0}


def _add(values: dict, record_or_values: MiningRecord | dict) -> None:
    if isinstance(record_or_values, MiningRecord):
        values["isk"] += record_or_values.isk
        values["volume"] += record_or_values.volume
        values["units"] += record_or_values.quantity
    else:
        values["isk"] += record_or_values.get("isk", 0)
        values["volume"] += record_or_values.get("volume", 0)
        values["units"] += record_or_values.get("units", 0)


def _clean(values: dict) -> dict[str, float | int]:
    """Return finite, JSON-safe values with stable rounding."""

    isk = float(values.get("isk", 0) or 0)
    volume = float(values.get("volume", 0) or 0)
    return {
        "isk": round(isk if math.isfinite(isk) else 0.0, 2),
        "volume": round(volume if math.isfinite(volume) else 0.0, 2),
        "units": int(values.get("units", 0) or 0),
    }


def _scale(values: dict, divisor: float) -> dict[str, float | int]:
    if not divisor:
        return _empty_values()
    return _clean(
        {
            "isk": values["isk"] / divisor,
            "volume": values["volume"] / divisor,
            "units": round(values["units"] / divisor),
        }
    )


def _score(values: dict) -> float:
    """Rank by estimated value, with volume and units as safe fallbacks."""

    return float(values["isk"] or values["volume"] or values["units"])


def _percent_change(current: float, previous: float) -> float | None:
    if not previous:
        return None
    return round(((current - previous) / abs(previous)) * 100, 1)


def _metric_changes(current: dict, previous: dict | None) -> dict:
    if previous is None:
        return {"isk": None, "volume": None, "units": None}
    return {
        key: _percent_change(float(current[key]), float(previous[key]))
        for key in ("isk", "volume", "units")
    }


def _granularity(start: dt.date, end: dt.date) -> str:
    days = (end - start).days + 1
    if days <= 120:
        return "day"
    if days <= 730:
        return "week"
    return "month"


def _bucket_date(value: dt.date, granularity: str) -> dt.date:
    if granularity == "week":
        return value - dt.timedelta(days=value.weekday())
    if granularity == "month":
        return value.replace(day=1)
    return value


def _bucket_dates(start: dt.date, end: dt.date, granularity: str) -> list[dt.date]:
    current = _bucket_date(start, granularity)
    finish = _bucket_date(end, granularity)
    values = []
    while current <= finish:
        values.append(current)
        if granularity == "month":
            year = current.year + (1 if current.month == 12 else 0)
            month = 1 if current.month == 12 else current.month + 1
            current = dt.date(year, month, 1)
        elif granularity == "week":
            current += dt.timedelta(days=7)
        else:
            current += dt.timedelta(days=1)
    return values


def _rollup(records: Iterable[MiningRecord]) -> dict:
    total = _empty_values()
    for record in records:
        _add(total, record)
    return _clean(total)


def _dimension_rollup(
    records: list[MiningRecord],
    key_func: Callable[[MiningRecord], Hashable],
    label_func: Callable[[MiningRecord], str],
    limit: int = 9,
) -> list[dict]:
    groups: dict[Hashable, dict] = {}
    for record in records:
        key = key_func(record)
        item = groups.setdefault(
            key,
            {"id": str(key), "label": label_func(record), "values": _empty_values()},
        )
        _add(item["values"], record)

    ranked = sorted(
        groups.values(), key=lambda item: _score(item["values"]), reverse=True
    )
    shown = ranked[:limit]
    hidden = ranked[limit:]
    if hidden:
        other = {
            "id": "other",
            "label": f"Other ({len(hidden)})",
            "values": _empty_values(),
        }
        for item in hidden:
            _add(other["values"], item["values"])
        shown.append(other)

    total_score = sum(_score(item["values"]) for item in ranked)
    for index, item in enumerate(shown):
        item["values"] = _clean(item["values"])
        item["color"] = PALETTE[index % len(PALETTE)]
        item["share"] = (
            round((_score(item["values"]) / total_score) * 100, 1) if total_score else 0
        )
    return shown


def _longest_streak(active_dates: set[dt.date], today: dt.date) -> tuple[int, int]:
    if not active_dates:
        return 0, 0

    ordered = sorted(active_dates)
    longest = 1
    running = 1
    for previous, current in itertools.pairwise(ordered):
        if current == previous + dt.timedelta(days=1):
            running += 1
            longest = max(longest, running)
        else:
            running = 1

    current_streak = 0
    cursor = today
    # A streak from yesterday still counts while today's mining is not yet recorded.
    if cursor not in active_dates and cursor - dt.timedelta(days=1) in active_dates:
        cursor -= dt.timedelta(days=1)
    while cursor in active_dates:
        current_streak += 1
        cursor -= dt.timedelta(days=1)
    return longest, current_streak


def _personality(
    character_count: int,
    account_count: int,
    system_count: int,
    active_ratio: float,
    longest_streak: int,
    top_ore_share: float,
) -> dict[str, str]:
    if character_count >= 4 and account_count <= 2:
        return {
            "name": "Multibox Maestro",
            "icon": "fa-solid fa-layer-group",
            "description": "A coordinated mining wing that turns several characters into one machine.",
        }
    if system_count >= 6:
        return {
            "name": "Belt Nomad",
            "icon": "fa-solid fa-route",
            "description": "You follow the rocks instead of waiting for the rocks to find you.",
        }
    if longest_streak >= 7:
        return {
            "name": "Mining Machine",
            "icon": "fa-solid fa-gears",
            "description": "Consistency is the real yield bonus—and your streak proves it.",
        }
    if top_ore_share >= 55:
        return {
            "name": "Rock Specialist",
            "icon": "fa-solid fa-gem",
            "description": "You know what you want, and most of your lasers agree.",
        }
    if active_ratio >= 0.5:
        return {
            "name": "Belt Regular",
            "icon": "fa-solid fa-calendar-check",
            "description": "The belts know your name—and probably your mining schedule.",
        }
    return {
        "name": "Industrial Generalist",
        "icon": "fa-solid fa-compass",
        "description": "A flexible mining portfolio spread across rocks, systems, and opportunities.",
    }


def build_analytics(
    records: Iterable[MiningRecord],
    start: dt.date,
    end: dt.date,
    comparison: str = "combined",
    previous_records: Iterable[MiningRecord] | None = None,
    selected_character_count: int | None = None,
    selected_account_count: int | None = None,
) -> dict:
    """Build every dashboard aggregate from normalized mining rows."""

    current = [record for record in records if start <= record.date <= end]
    previous = list(previous_records) if previous_records is not None else None
    total = _rollup(current)
    previous_total = _rollup(previous) if previous is not None else None

    character_ids = {record.character_id for record in current}
    account_ids = {record.account_id for record in current}
    character_count = (
        selected_character_count
        if selected_character_count is not None
        else len(character_ids)
    )
    account_count = (
        selected_account_count
        if selected_account_count is not None
        else len(account_ids)
    )

    daily: dict[dt.date, dict] = defaultdict(_empty_values)
    for record in current:
        _add(daily[record.date], record)
    active_dates = set(daily)
    active_days = len(active_dates)
    calendar_days = max(1, (end - start).days + 1)

    best_day_date = None
    best_day_values = _empty_values()
    if daily:
        best_day_date, best_day_values = max(
            daily.items(), key=lambda item: _score(item[1])
        )

    ore_mix = _dimension_rollup(
        current, lambda row: row.ore_id, lambda row: row.ore_name
    )
    systems = _dimension_rollup(
        current, lambda row: row.system_name, lambda row: row.system_name
    )
    characters = _dimension_rollup(
        current,
        lambda row: row.character_id,
        lambda row: row.character_name,
        limit=50,
    )
    accounts = _dimension_rollup(
        current,
        lambda row: row.account_id,
        lambda row: row.account_name,
        limit=50,
    )

    longest_streak, current_streak = _longest_streak(active_dates, end)
    top_ore_share = ore_mix[0]["share"] if ore_mix else 0
    personality = _personality(
        character_count=character_count,
        account_count=account_count,
        system_count=len({row.system_name for row in current}),
        active_ratio=active_days / calendar_days,
        longest_streak=longest_streak,
        top_ore_share=top_ore_share,
    )

    granularity = _granularity(start, end)
    buckets = _bucket_dates(start, end, granularity)
    bucket_keys = {item: index for index, item in enumerate(buckets)}

    if comparison == "character":
        group_key = lambda row: row.character_id
        group_label = lambda row: row.character_name
    elif comparison == "account":
        group_key = lambda row: row.account_id
        group_label = lambda row: row.account_name
    else:
        comparison = "combined"
        group_key = lambda row: "combined"
        group_label = lambda row: "All selected"

    group_totals: dict[Hashable, dict] = defaultdict(_empty_values)
    group_labels: dict[Hashable, str] = {}
    for record in current:
        key = group_key(record)
        group_labels[key] = group_label(record)
        _add(group_totals[key], record)

    ranked_groups = sorted(
        group_totals, key=lambda key: _score(group_totals[key]), reverse=True
    )
    visible_groups = ranked_groups[:12]
    hidden_groups = set(ranked_groups[12:])
    if hidden_groups:
        visible_groups.append("other")
        group_labels["other"] = f"Other ({len(hidden_groups)})"

    chart_values: dict[Hashable, list[dict]] = {
        key: [_empty_values() for _ in buckets] for key in visible_groups
    }
    for record in current:
        key = group_key(record)
        if key in hidden_groups:
            key = "other"
        if key not in chart_values:
            continue
        bucket = _bucket_date(record.date, granularity)
        _add(chart_values[key][bucket_keys[bucket]], record)

    series = []
    for index, key in enumerate(visible_groups):
        series.append(
            {
                "id": str(key),
                "label": group_labels[key],
                "color": PALETTE[index % len(PALETTE)],
                "values": [_clean(item) for item in chart_values[key]],
            }
        )

    heatmap_start = max(start, end - dt.timedelta(days=111))
    heatmap_days = []
    cursor = heatmap_start
    while cursor <= end:
        heatmap_days.append(
            {
                "date": cursor.isoformat(),
                "values": _clean(daily.get(cursor, _empty_values())),
            }
        )
        cursor += dt.timedelta(days=1)

    price_rows = sum(1 for row in current if row.price_known)
    volume_rows = sum(1 for row in current if row.volume_known)
    row_count = len(current)
    latest_date = max(active_dates).isoformat() if active_dates else None

    return {
        "kpis": {
            "total": total,
            "average_active_day": _scale(total, active_days),
            "average_calendar_day": _scale(total, calendar_days),
            "pace_30_days": _scale(total, calendar_days / 30),
            "active_days": active_days,
            "calendar_days": calendar_days,
            "character_count": character_count,
            "account_count": account_count,
            "ore_count": len({row.ore_id for row in current}),
            "system_count": len({row.system_name for row in current}),
            "entry_count": row_count,
            "best_day": {
                "date": best_day_date.isoformat() if best_day_date else None,
                "values": _clean(best_day_values),
            },
            "longest_streak": longest_streak,
            "current_streak": current_streak,
            "latest_mining_date": latest_date,
            "change": _metric_changes(total, previous_total),
            "price_coverage": round(price_rows / row_count * 100, 1)
            if row_count
            else 100.0,
            "volume_coverage": round(volume_rows / row_count * 100, 1)
            if row_count
            else 100.0,
        },
        "trend": {
            "labels": [item.isoformat() for item in buckets],
            "granularity": granularity,
            "comparison": comparison,
            "series": series,
        },
        "ore_mix": ore_mix,
        "systems": systems,
        "characters": characters,
        "accounts": accounts,
        "heatmap": {
            "start": heatmap_start.isoformat(),
            "end": end.isoformat(),
            "days": heatmap_days,
        },
        "personality": personality,
    }
