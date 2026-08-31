"""Server-rendered Structure Operations dashboard and safe actions."""

from __future__ import annotations

import csv
import datetime as dt
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.timezone import is_naive, make_aware, now
from django.views.decorators.http import require_GET, require_POST

from . import __version__, app_settings
from .access import (
    has_admin_access,
    has_manager_access,
    has_schedule_access,
    has_view_access,
)
from .forms import ScheduleEventForm, StructurePreferenceForm, TrackedCorporationForm
from .models import (
    AlertEvent,
    ScheduleEvent,
    StructurePreference,
    TrackedCorporation,
    WarState,
)
from .schedule import allowed_event_types, build_schedule_events, default_range
from .services import build_structure_rows, dashboard_summary, readiness_report
from .tasks import queue_source_refreshes


def _required(check, message):
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not check(request.user):
                raise PermissionDenied(message)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


view_required = _required(
    has_view_access, "You do not have Structure Operations access."
)
manager_required = _required(has_manager_access, "Manager access is required.")
admin_required = _required(has_admin_access, "Full administrator access is required.")
schedule_required = _required(
    has_schedule_access, "You do not have Operations Schedule access."
)


def _has_permission(codename):
    return _required(
        lambda user: user.is_superuser
        or user.has_perm(f"buh_structure_ops.{codename}"),
        f"The {codename.replace('_', ' ')} permission is required.",
    )


schedule_add_required = _has_permission("add_schedule_event")
schedule_change_required = _has_permission("change_schedule_event")
schedule_delete_required = _has_permission("delete_schedule_event")


def _filters(request):
    corporation = request.GET.get("corporation", "").strip()
    try:
        corporation_id = int(corporation) if corporation else None
    except ValueError:
        corporation_id = None
    fuel = request.GET.get("fuel")
    return {
        "corporation": corporation_id,
        "fuel": fuel if fuel in {"critical", "warning", "healthy", "unknown"} else None,
        "search": request.GET.get("search", "").strip()[:100],
        "attention": request.GET.get("attention") == "1",
    }


@view_required
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    filters = _filters(request)
    rows = build_structure_rows(filters)
    readiness = readiness_report()
    context = {
        "app_name": app_settings.APP_NAME,
        "app_version": __version__,
        "rows": rows,
        "summary": dashboard_summary(rows),
        "readiness": readiness,
        "alerts": AlertEvent.objects.filter(is_active=True).order_by(
            "acknowledged_at", "-occurred_at"
        )[:50],
        "recent_events": AlertEvent.objects.order_by("-occurred_at")[:25],
        "active_wars": WarState.objects.filter(is_active=True)[:25],
        "corporations": TrackedCorporation.objects.filter(is_enabled=True),
        "filters": filters,
        "can_manage": has_manager_access(request.user),
        "can_admin": has_admin_access(request.user),
        "corporation_form": TrackedCorporationForm(),
        "structure_owner_url": reverse("structures:add_structure_owner"),
        "refinery_owner_url": reverse("moonmining:add_owner"),
    }
    return render(request, "buh_structure_ops/dashboard.html", context)


@schedule_required
@require_GET
def schedule(request: HttpRequest) -> HttpResponse:
    event_types = allowed_event_types(request.user)
    context = {
        "app_name": app_settings.APP_NAME,
        "app_version": __version__,
        "event_types": event_types,
        "event_form": ScheduleEventForm(),
        "can_add_event": request.user.is_superuser
        or request.user.has_perm("buh_structure_ops.add_schedule_event"),
        "can_change_event": request.user.is_superuser
        or request.user.has_perm("buh_structure_ops.change_schedule_event"),
        "can_delete_event": request.user.is_superuser
        or request.user.has_perm("buh_structure_ops.delete_schedule_event"),
    }
    return render(request, "buh_structure_ops/schedule.html", context)


def _calendar_datetime(value):
    parsed = parse_datetime(value or "")
    if parsed and is_naive(parsed):
        parsed = make_aware(parsed)
    return parsed


@schedule_required
@require_GET
def schedule_feed(request: HttpRequest) -> JsonResponse:
    start = _calendar_datetime(request.GET.get("start"))
    end = _calendar_datetime(request.GET.get("end"))
    if start is None or end is None:
        start, end = default_range()
    if end <= start or end - start > dt.timedelta(days=370):
        return JsonResponse({"error": "Invalid calendar range."}, status=400)
    return JsonResponse(
        {
            "events": build_schedule_events(request.user, start, end),
            "generatedAt": now().isoformat(),
        }
    )


def _form_error_text(form):
    return " ".join(
        f"{field}: {'; '.join(errors)}" for field, errors in form.errors.items()
    )[:700]


@schedule_add_required
@require_POST
def schedule_event_add(request: HttpRequest) -> HttpResponse:
    form = ScheduleEventForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.created_by = request.user
        item.updated_by = request.user
        item.save()
        messages.success(request, f'Added schedule event "{item.title}".')
    else:
        messages.error(request, f"Could not add event. {_form_error_text(form)}")
    return redirect("buh_structure_ops:schedule")


@schedule_change_required
@require_POST
def schedule_event_update(request: HttpRequest, event_id: int) -> HttpResponse:
    item = get_object_or_404(ScheduleEvent, pk=event_id)
    form = ScheduleEventForm(request.POST, instance=item)
    if form.is_valid():
        item = form.save(commit=False)
        item.updated_by = request.user
        item.save()
        messages.success(request, f'Updated schedule event "{item.title}".')
    else:
        messages.error(request, f"Could not update event. {_form_error_text(form)}")
    return redirect("buh_structure_ops:schedule")


@schedule_delete_required
@require_POST
def schedule_event_delete(request: HttpRequest, event_id: int) -> HttpResponse:
    item = get_object_or_404(ScheduleEvent, pk=event_id)
    title = item.title
    item.delete()
    messages.success(request, f'Deleted schedule event "{title}".')
    return redirect("buh_structure_ops:schedule")


@manager_required
@require_POST
def refresh_now(request: HttpRequest) -> HttpResponse:
    cache_key = "buh_structure_ops:manual_refresh"
    if cache.add(cache_key, request.user.pk, timeout=120):
        queue_source_refreshes.delay()
        messages.success(
            request,
            "Structure, notification, and extraction refreshes were queued. ESI caching still applies.",
        )
    else:
        messages.warning(
            request, "A refresh was already queued within the last two minutes."
        )
    return redirect("buh_structure_ops:dashboard")


@manager_required
@require_POST
def acknowledge_alert(request: HttpRequest, alert_id: int) -> HttpResponse:
    alert = get_object_or_404(AlertEvent, pk=alert_id)
    alert.acknowledged_at = now()
    alert.acknowledged_by = request.user
    alert.save(update_fields=("acknowledged_at", "acknowledged_by", "last_seen_at"))
    messages.success(request, f"Acknowledged: {alert.title}")
    return redirect("buh_structure_ops:dashboard")


@manager_required
@require_POST
def update_preference(request: HttpRequest, structure_id: int) -> HttpResponse:
    preference, _ = StructurePreference.objects.get_or_create(structure_id=structure_id)
    form = StructurePreferenceForm(request.POST, instance=preference)
    if form.is_valid():
        preference = form.save(commit=False)
        preference.updated_by = request.user
        preference.save()
        messages.success(request, "Structure refill target and notes updated.")
    else:
        messages.error(
            request, "Could not save the structure settings. Target must be 7–180 days."
        )
    return redirect("buh_structure_ops:dashboard")


@admin_required
@require_POST
def add_tracked_corporation(request: HttpRequest) -> HttpResponse:
    form = TrackedCorporationForm(request.POST)
    if form.is_valid():
        corporation = form.save(commit=False)
        corporation.is_required = True
        corporation.is_enabled = True
        corporation.save()
        messages.success(
            request, f"{corporation.corporation_name} is now required at launch."
        )
    else:
        messages.error(
            request, "Could not add that corporation. Check its numeric ID and name."
        )
    return redirect("buh_structure_ops:dashboard")


@admin_required
@require_POST
def remove_tracked_corporation(
    request: HttpRequest, corporation_id: int
) -> HttpResponse:
    corporation = get_object_or_404(TrackedCorporation, corporation_id=corporation_id)
    corporation.is_enabled = False
    corporation.save(update_fields=("is_enabled", "updated_at"))
    messages.success(
        request,
        f"Stopped tracking {corporation.corporation_name}. Existing history was retained.",
    )
    return redirect("buh_structure_ops:dashboard")


def _csv_text(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@manager_required
@require_GET
def export_csv(request: HttpRequest) -> HttpResponse:
    rows = build_structure_rows(_filters(request))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        'attachment; filename="buh-structure-operations.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(
        [
            "Corporation",
            "Structure",
            "Structure ID",
            "Type",
            "System",
            "State",
            "Power",
            "Fuel expires (UTC)",
            "Fuel days",
            "Fuel blocks",
            "Blocks/day",
            "Target days",
            "Blocks to target",
            "Next extraction (UTC)",
            "Timer (UTC)",
            "Active war",
            "Online services",
            "Rigs",
            "Notes",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _csv_text(row["corporation"]),
                _csv_text(row["name"]),
                row["id"],
                _csv_text(row["type"]),
                _csv_text(row["system"]),
                _csv_text(row["state"]),
                _csv_text(row["power"]),
                row["fuel_expires_at"].isoformat() if row["fuel_expires_at"] else "",
                round(row["fuel_days"], 2) if row["fuel_days"] is not None else "",
                row["fuel_quantity"] if row["fuel_quantity"] is not None else "",
                row["fuel_blocks_per_day"]
                if row["fuel_blocks_per_day"] is not None
                else "",
                row["target_days"],
                row["refill_blocks"] if row["refill_blocks"] is not None else "",
                row["extraction"].chunk_arrival_at.isoformat()
                if row["extraction"]
                else "",
                row["timer_at"].isoformat() if row["timer_at"] else "",
                bool(row["active_wars"]),
                "; ".join(row["services"]),
                "; ".join(row["rigs"]),
                _csv_text(row["notes"]),
            ]
        )
    return response
