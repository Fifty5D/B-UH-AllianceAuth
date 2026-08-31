"""Permission-aware schedule aggregation for live and custom operations events."""

from __future__ import annotations

import datetime as dt

from django.urls import reverse
from django.utils.timezone import now
from moonmining.models import Extraction
from structures.models import Structure

from .models import ScheduleEvent, TrackedCorporation, WarState

EVENT_TYPES = (
    {
        "key": "moon",
        "label": "Moon pops",
        "permission": "buh_structure_ops.view_schedule_moons",
        "color": "#a78bfa",
        "icon": "fa-moon",
    },
    {
        "key": "fuel",
        "label": "Fuel timers",
        "permission": "buh_structure_ops.view_schedule_fuel",
        "color": "#f59e0b",
        "icon": "fa-gas-pump",
    },
    {
        "key": "timer",
        "label": "Structure timers",
        "permission": "buh_structure_ops.view_schedule_structure_timers",
        "color": "#ef4444",
        "icon": "fa-shield-halved",
    },
    {
        "key": "war",
        "label": "War timers",
        "permission": "buh_structure_ops.view_schedule_wars",
        "color": "#f43f5e",
        "icon": "fa-burst",
    },
    {
        "key": "custom",
        "label": "Custom events",
        "permission": "buh_structure_ops.view_schedule_custom",
        "color": "#14b8a6",
        "icon": "fa-calendar-plus",
    },
)

CUSTOM_COLORS = {
    "teal": "#14b8a6",
    "blue": "#3b82f6",
    "violet": "#8b5cf6",
    "gold": "#eab308",
    "orange": "#f97316",
    "red": "#ef4444",
    "slate": "#64748b",
}


def allowed_event_types(user) -> list[dict]:
    if user.is_superuser:
        return list(EVENT_TYPES)
    return [item for item in EVENT_TYPES if user.has_perm(item["permission"])]


def _tracked_ids():
    return TrackedCorporation.objects.filter(is_enabled=True).values_list(
        "corporation_id", flat=True
    )


def _within(value, start, end):
    return value is not None and start <= value < end


def _base_event(
    *, event_id, kind, title, start, color, icon, end=None, description="", location="", url="", all_day=False
):
    return {
        "id": str(event_id),
        "kind": kind,
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat() if end else None,
        "allDay": all_day,
        "color": color,
        "icon": icon,
        "description": description,
        "location": location,
        "url": url,
        "editable": False,
        "updateUrl": "",
        "deleteUrl": "",
    }


def _moon_events(user, start, end):
    qs = (
        Extraction.objects.filter(refinery__owner_id__in=_tracked_ids())
        .filter(chunk_arrival_at__lt=end, auto_fracture_at__gte=start)
        .selected_related_defaults()
        .select_related(
            "refinery__moon__eve_moon__eve_planet__eve_solar_system",
            "refinery__owner__corporation",
        )
        .order_by("chunk_arrival_at")
    )
    can_open = user.is_superuser or user.has_perm(
        "moonmining.view_extraction_details"
    )
    events = []
    for extraction in qs:
        moon = extraction.refinery.moon
        system = moon.eve_moon.eve_planet.eve_solar_system.name
        corporation = extraction.refinery.owner.corporation.corporation_name
        description = (
            f"{corporation} · {extraction.get_status_display().title()} extraction. "
            "Moon chunk arrival time from EVE ESI."
        )
        url = (
            reverse("moonmining:extraction_details", args=(extraction.pk,))
            + "?new_page=1"
            if can_open
            else ""
        )
        if _within(extraction.chunk_arrival_at, start, end):
            events.append(
                _base_event(
                    event_id=f"moon-ready-{extraction.pk}",
                    kind="moon",
                    title=f"Moon ready · {extraction.refinery.name}",
                    start=extraction.chunk_arrival_at,
                    color="#a78bfa",
                    icon="fa-moon",
                    description=description,
                    location=f"{moon} · {system}",
                    url=url,
                )
            )
        if _within(extraction.auto_fracture_at, start, end):
            events.append(
                _base_event(
                    event_id=f"moon-fracture-{extraction.pk}",
                    kind="moon",
                    title=f"Auto-fracture · {extraction.refinery.name}",
                    start=extraction.auto_fracture_at,
                    color="#7c3aed",
                    icon="fa-meteor",
                    description="Automatic fracture deadline for this moon chunk.",
                    location=f"{moon} · {system}",
                    url=url,
                )
            )
    return events


def _structure_queryset():
    return Structure.objects.filter(owner_id__in=_tracked_ids()).select_related(
        "owner__corporation", "eve_solar_system"
    )


def _fuel_events(start, end):
    events = []
    for structure in _structure_queryset().filter(
        fuel_expires_at__gte=start, fuel_expires_at__lt=end
    ):
        events.append(
            _base_event(
                event_id=f"fuel-{structure.pk}",
                kind="fuel",
                title=f"Fuel expires · {structure.name}",
                start=structure.fuel_expires_at,
                color="#f59e0b",
                icon="fa-gas-pump",
                description=(
                    f"Fuel expiry reported by EVE ESI for "
                    f"{structure.owner.corporation.corporation_name}."
                ),
                location=structure.eve_solar_system.name,
            )
        )
    return events


def _timer_events(start, end):
    events = []
    for structure in _structure_queryset().filter(
        state_timer_end__gte=start, state_timer_end__lt=end
    ):
        events.append(
            _base_event(
                event_id=f"state-timer-{structure.pk}",
                kind="timer",
                title=f"{structure.get_state_display().title()} ends · {structure.name}",
                start=structure.state_timer_end,
                color="#ef4444",
                icon="fa-shield-halved",
                description="Current structure state timer reported by EVE ESI.",
                location=structure.eve_solar_system.name,
            )
        )
    for structure in _structure_queryset().filter(
        unanchors_at__gte=start, unanchors_at__lt=end
    ):
        events.append(
            _base_event(
                event_id=f"unanchor-{structure.pk}",
                kind="timer",
                title=f"Unanchor completes · {structure.name}",
                start=structure.unanchors_at,
                color="#dc2626",
                icon="fa-link-slash",
                description="Structure unanchor completion time reported by EVE ESI.",
                location=structure.eve_solar_system.name,
            )
        )
    for structure in _structure_queryset().filter(
        next_reinforce_apply__gte=start, next_reinforce_apply__lt=end
    ):
        events.append(
            _base_event(
                event_id=f"reinforce-window-{structure.pk}",
                kind="timer",
                title=f"Reinforcement hour changes · {structure.name}",
                start=structure.next_reinforce_apply,
                color="#fb7185",
                icon="fa-clock-rotate-left",
                description="Pending reinforcement-hour change becomes effective.",
                location=structure.eve_solar_system.name,
            )
        )
    return events


def _war_events(start, end):
    names = dict(
        TrackedCorporation.objects.filter(is_enabled=True).values_list(
            "corporation_id", "corporation_name"
        )
    )
    events = []
    for war in WarState.objects.filter(corporation_id__in=names):
        corporation = names.get(war.corporation_id, str(war.corporation_id))
        if _within(war.declared_at, start, end):
            events.append(
                _base_event(
                    event_id=f"war-start-{war.pk}",
                    kind="war",
                    title=f"War declared · {corporation}",
                    start=war.declared_at,
                    color="#f43f5e",
                    icon="fa-burst",
                    description=war.last_event_type,
                    location=war.war_hq_name,
                )
            )
        if _within(war.ends_at, start, end):
            events.append(
                _base_event(
                    event_id=f"war-end-{war.pk}",
                    kind="war",
                    title=f"War ends · {corporation}",
                    start=war.ends_at,
                    color="#22c55e",
                    icon="fa-shield",
                    description=war.last_event_type,
                    location=war.war_hq_name,
                )
            )
    return events


def _custom_events(user, start, end):
    qs = ScheduleEvent.objects.filter(starts_at__lt=end).filter(
        ends_at__isnull=True, starts_at__gte=start
    ) | ScheduleEvent.objects.filter(starts_at__lt=end, ends_at__gte=start)
    can_change = user.is_superuser or user.has_perm(
        "buh_structure_ops.change_schedule_event"
    )
    can_delete = user.is_superuser or user.has_perm(
        "buh_structure_ops.delete_schedule_event"
    )
    events = []
    for item in qs.select_related("created_by", "updated_by").order_by("starts_at"):
        event = _base_event(
            event_id=f"custom-{item.pk}",
            kind="custom",
            title=item.title,
            start=item.starts_at,
            end=item.ends_at,
            color=CUSTOM_COLORS[item.color],
            icon="fa-calendar-plus",
            description=item.description,
            location=item.location,
            url=item.link,
            all_day=item.all_day,
        )
        event.update(
            {
                "customId": item.pk,
                "editable": can_change,
                "deletable": can_delete,
                "updateUrl": (
                    reverse("buh_structure_ops:schedule_event_update", args=(item.pk,))
                    if can_change
                    else ""
                ),
                "deleteUrl": (
                    reverse("buh_structure_ops:schedule_event_delete", args=(item.pk,))
                    if can_delete
                    else ""
                ),
                "colorName": item.color,
                "createdBy": item.created_by.get_username(),
            }
        )
        events.append(event)
    return events


def build_schedule_events(user, start, end):
    """Return only event categories the requesting user may see."""

    allowed = {item["key"] for item in allowed_event_types(user)}
    events = []
    if "moon" in allowed:
        events.extend(_moon_events(user, start, end))
    if "fuel" in allowed:
        events.extend(_fuel_events(start, end))
    if "timer" in allowed:
        events.extend(_timer_events(start, end))
    if "war" in allowed:
        events.extend(_war_events(start, end))
    if "custom" in allowed:
        events.extend(_custom_events(user, start, end))
    return sorted(events, key=lambda item: (item["start"], item["title"].casefold()))


def default_range():
    current = now()
    return current - dt.timedelta(days=45), current + dt.timedelta(days=180)
