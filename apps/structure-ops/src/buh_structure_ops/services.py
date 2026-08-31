"""Operational aggregation, alert evaluation, and readiness logic."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml
from allianceauth.notifications import notify
from django.db import transaction
from django.db.models import Q
from django.utils.html import strip_tags
from django.utils.timezone import now
from moonmining.models import Extraction
from moonmining.models import Owner as MoonOwner
from structures.core.notification_types import NotificationType
from structures.models import (
    Notification as StructureNotification,
)
from structures.models import (
    Owner as StructureOwner,
)
from structures.models import (
    Structure,
)

from . import app_settings
from .access import notification_recipients
from .models import (
    AlertEvent,
    StructurePreference,
    StructureSnapshot,
    TrackedCorporation,
    WarState,
)

ATTACK_NOTIFICATION_TYPES = {
    "OrbitalAttacked",
    "OrbitalReinforced",
    "SkyhookLostShields",
    "SkyhookUnderAttack",
    "SovStructureReinforced",
    "StructureLostArmor",
    "StructureLostShields",
    "StructureUnderAttack",
    "TowerAlertMsg",
    "TowerReinforcedExtra",
}
WAR_DECLARED_TYPES = {"DeclareWar", "WarDeclared", "WarAdopted", "WarInherited"}
WAR_ENDED_TYPES = {
    "AllWarSurrenderMsg",
    "CorpWarSurrenderMsg",
    "WarHQRemovedFromSpace",
    "WarInvalid",
    "WarRetractedByConcord",
}


def _notification_label(value: str) -> str:
    """Return a readable label for an ESI notification type.

    aa-structures deliberately stores ``notif_type`` as a plain CharField, so
    Django does not generate ``get_notif_type_display`` for it.  Prefer the
    source application's canonical TextChoices labels, while retaining a
    readable fallback for future ESI types.
    """

    try:
        return str(NotificationType(value).label)
    except ValueError:
        return re.sub(r"(?<!^)(?=[A-Z])", " ", value).strip() or "notification"


def _safe_int(value: Any) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(value):
    return value.isoformat() if value else None


def _tracked_ids() -> list[int]:
    return list(
        TrackedCorporation.objects.filter(is_enabled=True).values_list(
            "corporation_id", flat=True
        )
    )


def synchronize_tracked_corporations() -> int:
    """Import every corporation already connected in either source application."""

    discovered: dict[int, str] = {}
    for owner in StructureOwner.objects.select_related("corporation"):
        discovered[owner.pk] = owner.corporation.corporation_name
    for owner in MoonOwner.objects.select_related("corporation"):
        discovered[owner.pk] = owner.corporation.corporation_name
    created = 0
    for corporation_id, corporation_name in discovered.items():
        _, was_created = TrackedCorporation.objects.get_or_create(
            corporation_id=corporation_id,
            defaults={"corporation_name": corporation_name, "is_required": True},
        )
        created += int(was_created)
    return created


@dataclass(frozen=True)
class ReadinessRow:
    corporation_id: int
    corporation_name: str
    structure_owner: bool
    structure_character: str
    structure_sync_at: dt.datetime | None
    refinery_owner: bool
    refinery_character: str
    refinery_sync_at: dt.datetime | None
    ready: bool


def readiness_report() -> dict:
    tracked = list(TrackedCorporation.objects.filter(is_enabled=True))
    structure_owners = {
        owner.pk: owner
        for owner in StructureOwner.objects.filter(
            pk__in=[item.corporation_id for item in tracked]
        )
        .select_related("corporation")
        .prefetch_related("characters__character_ownership__character")
    }
    moon_owners = {
        owner.pk: owner
        for owner in MoonOwner.objects.filter(
            pk__in=[item.corporation_id for item in tracked]
        ).select_related("character_ownership__character", "corporation")
    }
    rows = []
    for corporation in tracked:
        structure_owner = structure_owners.get(corporation.corporation_id)
        moon_owner = moon_owners.get(corporation.corporation_id)
        structure_characters = []
        if structure_owner:
            structure_characters = [
                character.character_ownership.character.character_name
                for character in structure_owner.characters.all()
                if character.is_enabled
            ]
        refinery_character = ""
        if moon_owner and moon_owner.character_ownership:
            refinery_character = moon_owner.character_ownership.character.character_name
        row_ready = bool(
            structure_owner
            and structure_owner.is_active
            and structure_characters
            and moon_owner
            and moon_owner.is_enabled
            and refinery_character
        )
        rows.append(
            ReadinessRow(
                corporation_id=corporation.corporation_id,
                corporation_name=corporation.corporation_name,
                structure_owner=bool(structure_owner and structure_owner.is_active),
                structure_character=", ".join(structure_characters),
                structure_sync_at=(
                    structure_owner.structures_last_update_at
                    if structure_owner
                    else None
                ),
                refinery_owner=bool(moon_owner and moon_owner.is_enabled),
                refinery_character=refinery_character,
                refinery_sync_at=moon_owner.last_update_at if moon_owner else None,
                ready=row_ready,
            )
        )
    required_count = sum(1 for item in tracked if item.is_required)
    required_ids = {item.corporation_id for item in tracked if item.is_required}
    return {
        "rows": rows,
        "required_count": required_count,
        "ready_count": sum(1 for row in rows if row.ready),
        "is_ready": bool(required_count)
        and all(row.ready for row in rows if row.corporation_id in required_ids),
    }


def _fuel_status(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < app_settings.FUEL_CRITICAL_DAYS:
        return "critical"
    if days < app_settings.FUEL_WARNING_DAYS:
        return "warning"
    return "healthy"


def _active_extractions() -> dict[int, Extraction]:
    queryset = (
        Extraction.objects.filter(status__in=Extraction.Status.considered_active())
        .select_related(
            "refinery",
            "refinery__owner",
            "refinery__owner__corporation",
            "refinery__moon__eve_moon",
        )
        .order_by("chunk_arrival_at")
    )
    mapping = {}
    for extraction in queryset:
        mapping.setdefault(extraction.refinery_id, extraction)
    return mapping


def _fitted_items(structure: Structure, prefix: str) -> list[str]:
    return [
        item.eve_type.name
        for item in structure.items.all()
        if item.location_flag.startswith(prefix)
    ]


def build_structure_rows(filters: dict | None = None) -> list[dict]:
    """Build complete, presentation-ready structure rows."""

    filters = filters or {}
    tracked_ids = _tracked_ids()
    queryset = (
        Structure.objects.filter(owner_id__in=tracked_ids)
        .select_related(
            "owner__corporation",
            "eve_type__eve_group__eve_category",
            "eve_solar_system",
        )
        .prefetch_related("services", "items__eve_type", "tags")
        .order_by("owner__corporation__corporation_name", "name")
    )
    if filters.get("corporation"):
        queryset = queryset.filter(owner_id=filters["corporation"])
    if filters.get("search"):
        term = filters["search"]
        queryset = queryset.filter(
            Q(name__icontains=term)
            | Q(eve_solar_system__name__icontains=term)
            | Q(owner__corporation__corporation_name__icontains=term)
        )

    structures = list(queryset)
    preferences = {
        item.structure_id: item
        for item in StructurePreference.objects.filter(
            structure_id__in=[structure.id for structure in structures]
        )
    }
    extractions = _active_extractions()
    wars_by_corporation: dict[int, list[WarState]] = {}
    for war in WarState.objects.filter(is_active=True, corporation_id__in=tracked_ids):
        wars_by_corporation.setdefault(war.corporation_id, []).append(war)

    snapshot_map: dict[int, list[dict]] = {}
    snapshots = StructureSnapshot.objects.filter(
        structure_id__in=[structure.id for structure in structures],
        captured_at__gte=now() - dt.timedelta(days=14),
    ).order_by("structure_id", "captured_at")
    for snapshot in snapshots:
        snapshot_map.setdefault(snapshot.structure_id, []).append(
            {
                "at": snapshot.captured_at.isoformat(),
                "fuel": snapshot.fuel_quantity,
                "days": (
                    max(
                        0,
                        (
                            snapshot.fuel_expires_at - snapshot.captured_at
                        ).total_seconds()
                        / 86400,
                    )
                    if snapshot.fuel_expires_at
                    else None
                ),
            }
        )

    rows = []
    current = now()
    for structure in structures:
        preference = preferences.get(structure.id)
        target_days = (
            preference.target_fuel_days if preference else app_settings.FUEL_TARGET_DAYS
        )
        fuel_days = None
        if structure.fuel_expires_at:
            fuel_days = max(
                0.0, (structure.fuel_expires_at - current).total_seconds() / 86400
            )
        fuel_quantity = structure.structure_fuel_quantity
        fuel_usage = structure.structure_fuel_usage()
        target_blocks = fuel_usage * target_days if fuel_usage else None
        refill_blocks = (
            max(0, target_blocks - (fuel_quantity or 0))
            if target_blocks is not None and fuel_quantity is not None
            else None
        )
        extraction = extractions.get(structure.id)
        timer_at = structure.state_timer_end or structure.unanchors_at
        online_services = [
            service.name
            for service in structure.services.all()
            if service.state == service.State.ONLINE
        ]
        rigs = _fitted_items(structure, "RigSlot")
        fitted_services = _fitted_items(structure, "ServiceSlot")
        corporation_id = structure.owner_id
        active_wars = wars_by_corporation.get(corporation_id, [])
        row = {
            "id": structure.id,
            "name": structure.name,
            "corporation_id": corporation_id,
            "corporation": structure.owner.corporation.corporation_name,
            "system": structure.eve_solar_system.name,
            "type": structure.eve_type.name,
            "state": structure.get_state_display(),
            "power": structure.get_power_mode_display(),
            "is_reinforced": structure.is_reinforced,
            "timer_at": timer_at,
            "fuel_expires_at": structure.fuel_expires_at,
            "fuel_days": fuel_days,
            "fuel_status": _fuel_status(fuel_days),
            "fuel_quantity": fuel_quantity,
            "fuel_blocks_per_day": fuel_usage,
            "target_days": target_days,
            "target_blocks": target_blocks,
            "refill_blocks": refill_blocks,
            "services": online_services,
            "fitted_services": fitted_services,
            "rigs": rigs,
            "extraction": extraction,
            "active_wars": active_wars,
            "last_updated_at": structure.last_updated_at,
            "notes": preference.notes if preference else "",
            "history_json": json.dumps(snapshot_map.get(structure.id, [])),
        }
        if filters.get("fuel") and row["fuel_status"] != filters["fuel"]:
            continue
        if filters.get("attention") and not (
            row["fuel_status"] in {"warning", "critical"}
            or row["is_reinforced"]
            or row["timer_at"]
            or row["active_wars"]
        ):
            continue
        rows.append(row)
    return rows


def capture_snapshots() -> int:
    """Store at most one snapshot per structure every 25 minutes."""

    current = now()
    cutoff = current - dt.timedelta(minutes=25)
    tracked_ids = _tracked_ids()
    created = 0
    for structure in (
        Structure.objects.filter(owner_id__in=tracked_ids)
        .select_related("owner__corporation")
        .prefetch_related("services", "items__eve_type__eve_group")
    ):
        if StructureSnapshot.objects.filter(
            structure_id=structure.id, captured_at__gte=cutoff
        ).exists():
            continue
        StructureSnapshot.objects.create(
            structure_id=structure.id,
            corporation_id=structure.owner_id,
            structure_name=structure.name,
            captured_at=current,
            fuel_expires_at=structure.fuel_expires_at,
            fuel_quantity=structure.structure_fuel_quantity,
            fuel_blocks_per_day=structure.structure_fuel_usage(),
            state=structure.get_state_display(),
            service_count=structure.services.filter(
                state=2  # StructureService.State.ONLINE
            ).count(),
        )
        created += 1
    StructureSnapshot.objects.filter(
        captured_at__lt=current
        - dt.timedelta(days=app_settings.SNAPSHOT_RETENTION_DAYS)
    ).delete()
    return created


def _state_alert(
    *,
    event_key: str,
    active: bool,
    kind: str,
    severity: str,
    title: str,
    message: str,
    corporation_id: int | None = None,
    structure_id: int | None = None,
) -> AlertEvent | None:
    current = now()
    event = AlertEvent.objects.filter(event_key=event_key).first()
    if active:
        if event is None:
            return AlertEvent.objects.create(
                event_key=event_key,
                kind=kind,
                severity=severity,
                title=title,
                message=message,
                corporation_id=corporation_id,
                structure_id=structure_id,
                occurred_at=current,
            )
        fields = ["title", "message", "severity", "last_seen_at"]
        event.title = title
        event.message = message
        event.severity = severity
        if not event.is_active:
            event.is_active = True
            event.resolved_at = None
            event.acknowledged_at = None
            event.acknowledged_by = None
            event.notification_sent_at = None
            event.occurred_at = current
            event.occurrence_count += 1
            fields.extend(
                [
                    "is_active",
                    "resolved_at",
                    "acknowledged_at",
                    "acknowledged_by",
                    "notification_sent_at",
                    "occurred_at",
                    "occurrence_count",
                ]
            )
        event.save(update_fields=fields)
        return event
    if event and event.is_active:
        event.is_active = False
        event.resolved_at = current
        event.save(update_fields=("is_active", "resolved_at", "last_seen_at"))
    return event


def _one_time_alert(
    *,
    event_key: str,
    kind: str,
    severity: str,
    title: str,
    message: str,
    occurred_at,
    corporation_id: int | None = None,
    structure_id: int | None = None,
    source_id: str = "",
) -> AlertEvent:
    event, _ = AlertEvent.objects.get_or_create(
        event_key=event_key,
        defaults={
            "kind": kind,
            "severity": severity,
            "title": title,
            "message": message,
            "occurred_at": occurred_at,
            "corporation_id": corporation_id,
            "structure_id": structure_id,
            "source_id": source_id,
        },
    )
    return event


def evaluate_fuel_alerts() -> int:
    changed = 0
    current = now()
    for structure in Structure.objects.filter(
        owner_id__in=_tracked_ids()
    ).select_related("owner__corporation"):
        days = None
        if structure.fuel_expires_at:
            days = max(
                0.0, (structure.fuel_expires_at - current).total_seconds() / 86400
            )
        corporation_name = structure.owner.corporation.corporation_name
        remaining = f"{days:.1f}" if days is not None else "unknown"
        common = {
            "kind": AlertEvent.Kind.FUEL,
            "corporation_id": structure.owner_id,
            "structure_id": structure.id,
        }
        warning_active = (
            days is not None
            and app_settings.FUEL_CRITICAL_DAYS <= days < app_settings.FUEL_WARNING_DAYS
        )
        critical_active = days is not None and days < app_settings.FUEL_CRITICAL_DAYS
        if _state_alert(
            event_key=f"fuel:warning:{structure.id}",
            active=warning_active,
            severity=AlertEvent.Severity.WARNING,
            title=f"Fuel warning: {structure.name}",
            message=f"{structure.name} ({corporation_name}) has approximately {remaining} days of fuel remaining.",
            **common,
        ):
            changed += int(warning_active)
        if _state_alert(
            event_key=f"fuel:critical:{structure.id}",
            active=critical_active,
            severity=AlertEvent.Severity.DANGER,
            title=f"Fuel critical: {structure.name}",
            message=f"{structure.name} ({corporation_name}) has approximately {remaining} days of fuel remaining. Refill immediately.",
            **common,
        ):
            changed += int(critical_active)
    return changed


def evaluate_extraction_alerts() -> int:
    current = now()
    count = 0
    active_source_ids = set()
    for extraction in Extraction.objects.filter(
        status__in=Extraction.Status.considered_active(),
        refinery__owner_id__in=_tracked_ids(),
    ).select_related("refinery__owner__corporation"):
        source_id = f"extraction:{extraction.pk}"
        active_source_ids.add(source_id)
        corporation_name = extraction.refinery.owner.corporation.corporation_name
        if (
            extraction.status == Extraction.Status.READY
            or extraction.chunk_arrival_at <= current
        ):
            _one_time_alert(
                event_key=f"{source_id}:ready",
                source_id=source_id,
                kind=AlertEvent.Kind.EXTRACTION,
                severity=AlertEvent.Severity.SUCCESS,
                title=f"Extraction ready: {extraction.refinery.name}",
                message=f"The moon chunk at {extraction.refinery.name} ({corporation_name}) is ready to fracture.",
                occurred_at=max(current, extraction.chunk_arrival_at),
                corporation_id=extraction.refinery.owner_id,
                structure_id=extraction.refinery_id,
            )
            count += 1
            continue
        hours = (extraction.chunk_arrival_at - current).total_seconds() / 3600
        band = next(
            (
                threshold
                for threshold in sorted(app_settings.EXTRACTION_REMINDER_HOURS)
                if hours <= threshold
            ),
            None,
        )
        if band is not None:
            _one_time_alert(
                event_key=f"{source_id}:approaching:{band}",
                source_id=source_id,
                kind=AlertEvent.Kind.EXTRACTION,
                severity=AlertEvent.Severity.WARNING
                if band <= 24
                else AlertEvent.Severity.INFO,
                title=f"Extraction approaching: {extraction.refinery.name}",
                message=f"The extraction at {extraction.refinery.name} ({corporation_name}) arrives in about {hours:.1f} hours.",
                occurred_at=current,
                corporation_id=extraction.refinery.owner_id,
                structure_id=extraction.refinery_id,
            )
            count += 1
    AlertEvent.objects.filter(kind=AlertEvent.Kind.EXTRACTION, is_active=True).exclude(
        source_id__in=active_source_ids
    ).update(is_active=False, resolved_at=current)
    return count


def _notification_data(notification: StructureNotification) -> dict:
    try:
        value = yaml.safe_load(notification.text) if notification.text else {}
        return value if isinstance(value, dict) else {}
    except yaml.YAMLError:
        return {}


def process_structure_events() -> int:
    cutoff = now() - dt.timedelta(days=app_settings.EVENT_LOOKBACK_DAYS)
    queryset = (
        StructureNotification.objects.filter(
            owner_id__in=_tracked_ids(),
            timestamp__gte=cutoff,
            notif_type__in=ATTACK_NOTIFICATION_TYPES
            | WAR_DECLARED_TYPES
            | WAR_ENDED_TYPES,
        )
        .select_related("owner__corporation")
        .prefetch_related("structures")
        .order_by("timestamp", "notification_id")
    )
    count = 0
    for event in queryset:
        event_key = f"esi:{event.owner_id}:{event.notification_id}"
        if AlertEvent.objects.filter(event_key=event_key).exists():
            continue
        data = _notification_data(event)
        related_structure = event.structures.first()
        structure_id = (
            related_structure.id
            if related_structure
            else _safe_int(
                data.get("structureID")
                or data.get("structureId")
                or data.get("warHQ_IdType")
            )
        )
        structure_name = (
            related_structure.name
            if related_structure
            else strip_tags(
                str(
                    data.get("structureName")
                    or data.get("warHQ")
                    or "a corporation asset"
                )
            )
        )
        corporation_name = event.owner.corporation.corporation_name
        if event.notif_type in ATTACK_NOTIFICATION_TYPES:
            reinforced = "Reinforced" in event.notif_type or "Lost" in event.notif_type
            title = (
                f"Structure reinforced: {structure_name}"
                if reinforced
                else f"Structure attacked: {structure_name}"
            )
            _one_time_alert(
                event_key=event_key,
                source_id=str(event.notification_id),
                kind=AlertEvent.Kind.ATTACK,
                severity=AlertEvent.Severity.DANGER,
                title=title,
                message=f"{corporation_name} received {_notification_label(event.notif_type)} at {event.timestamp:%Y-%m-%d %H:%M} UTC.",
                occurred_at=event.timestamp,
                corporation_id=event.owner_id,
                structure_id=structure_id,
            )
        else:
            declared = event.notif_type in WAR_DECLARED_TYPES
            aggressor = _safe_int(data.get("declaredByID") or data.get("ownerID1"))
            defender = _safe_int(data.get("againstID") or data.get("ownerID2"))
            hq_id = _safe_int(data.get("warHQ_IdType"))
            hq_name = strip_tags(str(data.get("warHQ") or ""))
            if declared:
                signature = f"{event.owner_id}:{aggressor or 0}:{defender or 0}:{hq_id or 0}:{event.notification_id}"
                WarState.objects.update_or_create(
                    signature=signature,
                    defaults={
                        "corporation_id": event.owner_id,
                        "aggressor_id": aggressor,
                        "defender_id": defender,
                        "war_hq_structure_id": hq_id,
                        "war_hq_name": hq_name,
                        "declared_at": event.timestamp,
                        "is_active": True,
                        "last_event_type": event.notif_type,
                        "last_notification_id": event.notification_id,
                    },
                )
                title = f"War declared: {corporation_name}"
                message = f"{corporation_name} received a war declaration."
                if hq_name:
                    message += f" Listed war HQ: {hq_name}."
                severity = AlertEvent.Severity.DANGER
            else:
                candidates = WarState.objects.filter(
                    corporation_id=event.owner_id, is_active=True
                )
                if aggressor:
                    candidates = candidates.filter(
                        Q(aggressor_id=aggressor) | Q(defender_id=aggressor)
                    )
                if defender:
                    candidates = candidates.filter(
                        Q(aggressor_id=defender) | Q(defender_id=defender)
                    )
                candidates.update(
                    is_active=False,
                    ends_at=event.timestamp,
                    last_event_type=event.notif_type,
                    last_notification_id=event.notification_id,
                )
                title = f"War ended or ending: {corporation_name}"
                message = f"{corporation_name} received {_notification_label(event.notif_type)}."
                severity = AlertEvent.Severity.SUCCESS
            _one_time_alert(
                event_key=event_key,
                source_id=str(event.notification_id),
                kind=AlertEvent.Kind.WAR,
                severity=severity,
                title=title,
                message=message,
                occurred_at=event.timestamp,
                corporation_id=event.owner_id,
                structure_id=hq_id,
            )
        count += 1
    return count


def evaluate_sync_alerts() -> int:
    current = now()
    count = 0
    report = readiness_report()
    structure_owner_map = {
        owner.pk: owner
        for owner in StructureOwner.objects.filter(pk__in=_tracked_ids())
    }
    moon_owner_map = {
        owner.pk: owner for owner in MoonOwner.objects.filter(pk__in=_tracked_ids())
    }
    for row in report["rows"]:
        structure_owner = structure_owner_map.get(row.corporation_id)
        structure_problem = None
        if not row.structure_owner or not row.structure_character:
            structure_problem = "Missing or disabled Structures director authorization."
        elif structure_owner and structure_owner.is_up is False:
            structure_problem = (
                "The Structures source reports a token or synchronization failure."
            )
        elif row.structure_sync_at and row.structure_sync_at < current - dt.timedelta(
            minutes=app_settings.STRUCTURE_STALE_MINUTES
        ):
            structure_problem = (
                f"Structure data has not refreshed since {_iso(row.structure_sync_at)}."
            )
        _state_alert(
            event_key=f"sync:structures:{row.corporation_id}",
            active=bool(structure_problem),
            kind=AlertEvent.Kind.SYNC,
            severity=AlertEvent.Severity.DANGER,
            title=f"Structure sync failure: {row.corporation_name}",
            message=structure_problem or "Structure sync recovered.",
            corporation_id=row.corporation_id,
        )
        count += int(bool(structure_problem))

        moon_owner = moon_owner_map.get(row.corporation_id)
        refinery_problem = None
        if not row.refinery_owner or not row.refinery_character:
            refinery_problem = "Missing or disabled Moon Mining director authorization."
        elif moon_owner and moon_owner.last_update_ok is False:
            refinery_problem = (
                "The Moon Mining source reports a token or synchronization failure."
            )
        elif row.refinery_sync_at and row.refinery_sync_at < current - dt.timedelta(
            minutes=app_settings.EXTRACTION_STALE_MINUTES
        ):
            refinery_problem = (
                f"Extraction data has not refreshed since {_iso(row.refinery_sync_at)}."
            )
        _state_alert(
            event_key=f"sync:moonmining:{row.corporation_id}",
            active=bool(refinery_problem),
            kind=AlertEvent.Kind.SYNC,
            severity=AlertEvent.Severity.DANGER,
            title=f"Extraction sync failure: {row.corporation_name}",
            message=refinery_problem or "Extraction sync recovered.",
            corporation_id=row.corporation_id,
        )
        count += int(bool(refinery_problem))
    return count


def dispatch_pending_notifications() -> int:
    recipients = list(notification_recipients())
    sent = 0
    for event in AlertEvent.objects.filter(notification_sent_at__isnull=True).order_by(
        "occurred_at"
    ):
        for user in recipients:
            notify(
                user=user,
                title=event.title,
                message=event.message,
                level=event.severity,
            )
            sent += 1
        event.notification_sent_at = now()
        event.save(update_fields=("notification_sent_at", "last_seen_at"))
    return sent


@transaction.atomic
def evaluate_all() -> dict:
    """Evaluate every requested alert family and dispatch Auth notifications."""

    results = {
        "fuel": evaluate_fuel_alerts(),
        "extractions": evaluate_extraction_alerts(),
        "events": process_structure_events(),
        "sync": evaluate_sync_alerts(),
    }
    results["notifications"] = dispatch_pending_notifications()
    return results


def dashboard_summary(rows: list[dict]) -> dict:
    current = now()
    next_extraction = min(
        (row["extraction"].chunk_arrival_at for row in rows if row["extraction"]),
        default=None,
    )
    next_timer = min(
        (
            row["timer_at"]
            for row in rows
            if row["timer_at"] and row["timer_at"] >= current
        ),
        default=None,
    )
    return {
        "structures": len(rows),
        "fuel_critical": sum(row["fuel_status"] == "critical" for row in rows),
        "fuel_warning": sum(row["fuel_status"] == "warning" for row in rows),
        "reinforced": sum(row["is_reinforced"] for row in rows),
        "active_wars": WarState.objects.filter(
            is_active=True, corporation_id__in=_tracked_ids()
        ).count(),
        "next_extraction": next_extraction,
        "next_timer": next_timer,
        "active_alerts": AlertEvent.objects.filter(is_active=True).count(),
        "unacknowledged": AlertEvent.objects.filter(
            is_active=True, acknowledged_at__isnull=True
        ).count(),
    }
