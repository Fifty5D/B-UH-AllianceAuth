"""HTTP views for the Mining Analytics dashboard."""

from __future__ import annotations

import csv
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST
from memberaudit import tasks as memberaudit_tasks

from . import __version__, app_settings
from .access import available_scopes, has_app_access
from .services import (
    SelectionTooLarge,
    apply_selection,
    build_dashboard_payload,
    iter_export_records,
    resolve_date_range,
    selector_options,
)


def app_access_required(view_func):
    """Require authentication and at least one Mining Analytics permission."""

    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not has_app_access(request.user):
            raise PermissionDenied("You do not have access to Mining Analytics.")
        return view_func(request, *args, **kwargs)

    return wrapped


def _scope(request: HttpRequest) -> str:
    scopes = available_scopes(request.user)
    if not scopes:
        raise PermissionDenied("You do not have access to Mining Analytics.")
    return request.GET.get("scope") or scopes[0].value


def _parse_id_list(request: HttpRequest, key: str, source=None) -> list[int] | None:
    source = source if source is not None else request.GET
    if key not in source:
        return None
    raw_values = source.getlist(key)
    pieces = []
    for raw in raw_values:
        pieces.extend(part.strip() for part in raw.split(","))
    if not any(pieces):
        return []
    try:
        values = sorted({int(piece) for piece in pieces if piece})
    except ValueError as exc:
        raise ValidationError(f"{key} must contain numeric IDs.") from exc
    if any(value <= 0 for value in values):
        raise ValidationError(f"{key} contains an invalid ID.")
    return values


def _comparison(request: HttpRequest) -> str:
    value = request.GET.get("comparison", "combined")
    return value if value in {"combined", "character", "account"} else "combined"


def _selection_and_dates(request: HttpRequest, source=None):
    source = source if source is not None else request.GET
    scope = source.get("scope") or _scope(request)
    selection = apply_selection(
        request.user,
        scope,
        _parse_id_list(request, "accounts", source),
        _parse_id_list(request, "characters", source),
    )
    date_range = resolve_date_range(
        selection,
        source.get("preset", "30d"),
        source.get("start"),
        source.get("end"),
    )
    return scope, selection, date_range


def _json_error(exc: Exception, status: int = 400) -> JsonResponse:
    if hasattr(exc, "messages"):
        message = " ".join(exc.messages)
    else:
        message = str(exc)
    return JsonResponse({"error": message}, status=status)


@app_access_required
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    """Render the single-page mining dashboard."""

    scopes = available_scopes(request.user)
    requested_scope = request.GET.get("scope")
    allowed_values = {item.value for item in scopes}
    initial_scope = (
        requested_scope if requested_scope in allowed_values else scopes[0].value
    )
    context = {
        "app_name": app_settings.APP_NAME,
        "app_version": __version__,
        "scopes": scopes,
        "initial_scope": initial_scope,
        "initial_options": selector_options(request.user, initial_scope),
        "can_export": request.user.has_perm("buh_mining_analytics.export_data"),
        "can_refresh": app_settings.ENABLE_REFRESH,
    }
    return render(request, "buh_mining_analytics/dashboard.html", context)


@app_access_required
@require_GET
def options_api(request: HttpRequest) -> JsonResponse:
    """Return selectors for a newly chosen visibility scope."""

    try:
        return JsonResponse(selector_options(request.user, _scope(request)))
    except (PermissionDenied, ValidationError) as exc:
        return _json_error(
            exc, status=403 if isinstance(exc, PermissionDenied) else 400
        )


@app_access_required
@require_GET
def data_api(request: HttpRequest) -> JsonResponse:
    """Return aggregated mining analytics as JSON."""

    try:
        scope, selection, date_range = _selection_and_dates(request)
        payload = build_dashboard_payload(
            request.user,
            scope,
            selection,
            date_range,
            comparison=_comparison(request),
        )
        return JsonResponse(payload)
    except PermissionDenied as exc:
        return _json_error(exc, status=403)
    except (ValidationError, SelectionTooLarge) as exc:
        return _json_error(exc)


@app_access_required
@require_POST
def refresh_api(request: HttpRequest) -> JsonResponse:
    """Queue safe, non-forced Member Audit mining refreshes for a selection."""

    if not app_settings.ENABLE_REFRESH:
        return JsonResponse({"error": "Mining refresh is disabled."}, status=403)
    try:
        scope = request.POST.get("scope") or "mine"
        selection = apply_selection(
            request.user,
            scope,
            _parse_id_list(request, "accounts", request.POST),
            _parse_id_list(request, "characters", request.POST),
        )
    except PermissionDenied as exc:
        return _json_error(exc, status=403)
    except (ValidationError, SelectionTooLarge) as exc:
        return _json_error(exc)

    character_ids = list(
        selection.queryset.filter(is_disabled=False).values_list("pk", flat=True)[:100]
    )
    for character_id in character_ids:
        memberaudit_tasks.update_character_mining_ledger.delay(character_id, False)

    skipped = max(0, selection.character_count - len(character_ids))
    return JsonResponse(
        {
            "queued": len(character_ids),
            "skipped": skipped,
            "message": (
                f"Queued mining refresh for {len(character_ids)} character(s). "
                "Member Audit and CCP caching determine when new rows appear."
            ),
        }
    )


def _csv_text(value: str) -> str:
    """Prevent spreadsheet formula injection in exported text fields."""

    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@app_access_required
@require_GET
def export_csv(request: HttpRequest) -> HttpResponse:
    """Export allowed mining ledger rows for the active filters."""

    if not request.user.has_perm("buh_mining_analytics.export_data"):
        raise PermissionDenied("You do not have permission to export mining data.")

    try:
        scope, selection, date_range = _selection_and_dates(request)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        filename = f"mining-{scope}-{date_range.start}-{date_range.end}.csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        writer = csv.writer(response)
        writer.writerow(
            [
                "Date (UTC)",
                "Auth account / main",
                "Character",
                "Corporation",
                "Region",
                "Solar system",
                "Ore",
                "Units",
                "Unit volume (m3)",
                "Total volume (m3)",
                "Estimated unit price (ISK)",
                "Estimated total (ISK)",
            ]
        )
        for record in iter_export_records(selection, date_range):
            unit_volume = record.volume / record.quantity if record.quantity else 0
            unit_price = record.isk / record.quantity if record.quantity else 0
            writer.writerow(
                [
                    record.date.isoformat(),
                    _csv_text(record.account_name),
                    _csv_text(record.character_name),
                    _csv_text(record.corporation_name),
                    _csv_text(record.region_name),
                    _csv_text(record.system_name),
                    _csv_text(record.ore_name),
                    record.quantity,
                    round(unit_volume, 4),
                    round(record.volume, 2),
                    round(unit_price, 2),
                    round(record.isk, 2),
                ]
            )
        return response
    except PermissionDenied:
        raise
    except (ValidationError, SelectionTooLarge) as exc:
        return HttpResponse(str(exc), status=400, content_type="text/plain")
