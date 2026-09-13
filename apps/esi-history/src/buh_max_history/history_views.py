"""Permission-filtered archive history, coverage and paginated metadata exports."""

import shutil

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Min, Q, Sum
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET

from . import __version__
from .access import permission_map
from .capture import archive_root
from .collection import endpoints
from .models import (
    ArchiveCollectionTarget,
    ArchiveConfiguration,
    ArchiveObservation,
    ArchiveSnapshot,
    ArchiveStream,
    PublicArchiveFile,
    PublicArchiveRevision,
)
from .views import _require, _safe_archive_path


def filtered_streams(request, permissions):
    streams = ArchiveStream.objects.all()
    if not permissions["view_private_archive_metadata"]:
        streams = streams.filter(is_private=False)
    query = request.GET.get("q", "").strip()[:160]
    if query:
        match = Q(operation_id__icontains=query) | Q(safe_parameters__icontains=query)
        if query.isdigit():
            match |= Q(character_id=int(query))
        streams = streams.filter(match)
    kind = request.GET.get("source", "")
    if kind == "auth":
        streams = streams.filter(operation_id__startswith="auth:")
    elif kind == "esi":
        streams = streams.exclude(operation_id__startswith="auth:")
    return streams


@require_GET
@login_required
def history(request):
    _require(request.user, "view_history_archive")
    _require(request.user, "view_archive_coverage")
    permissions = permission_map(request.user)
    streams = filtered_streams(request, permissions)
    snapshots = ArchiveSnapshot.objects.filter(stream__in=streams).select_related("stream")
    dates = {}
    for parameter, lookup in (
        ("from", "last_observed_at__date__gte"),
        ("to", "first_observed_at__date__lte"),
    ):
        raw = request.GET.get(parameter, "")
        if raw:
            try:
                value = parse_date(raw)
            except ValueError:
                value = None
            if value is None:
                return JsonResponse({"error": "Dates must use YYYY-MM-DD."}, status=400)
            dates[lookup] = value
    if dates:
        spans = ArchiveObservation.objects.filter(**dates).values("snapshot_id")
        snapshots = snapshots.filter(
            Q(pk__in=spans) | (Q(observations__isnull=True) & Q(**dates))
        ).distinct()
    snapshots = snapshots.order_by("-last_observed_at", "-pk")
    page = Paginator(snapshots, 50).get_page(request.GET.get("page"))
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "count": page.paginator.count,
                "page": page.number,
                "pages": page.paginator.num_pages,
                "results": [
                    {
                        "id": row.pk,
                        "operation": row.stream.operation_id,
                        "character_id": row.stream.character_id,
                        "private": row.stream.is_private,
                        "parameters": row.stream.safe_parameters,
                        "sha256": row.payload_sha256,
                        "first_observed_at": row.first_observed_at,
                        "last_observed_at": row.last_observed_at,
                        "observations": row.observation_count,
                        "stored_bytes": row.stored_bytes,
                    }
                    for row in page
                ],
            }
        )
    targets = ArchiveCollectionTarget.objects.none()
    if permissions["view_private_archive_metadata"]:
        targets = ArchiveCollectionTarget.objects.all()
        query = request.GET.get("q", "").strip()[:160]
        if query:
            targets = targets.filter(
                Q(operation_id__icontains=query) | Q(parameters__icontains=query)
            )
    target_totals = list(
        targets.values("status").annotate(count=Count("pk")).order_by("status")
    )
    inventory = []
    if permissions["view_private_archive_metadata"]:
        coverage = {}
        for item in targets.values("operation_id", "status").annotate(count=Count("pk")):
            coverage.setdefault(item["operation_id"], []).append(
                f"{item['status']}: {item['count']}"
            )
        for entry in endpoints().values():
            if not entry["scopes"]:
                continue
            required = set(entry["required"]) - {
                "character_id",
                "corporation_id",
                "alliance_id",
            }
            status = ", ".join(coverage.get(entry["id"], []))
            if not status:
                status = (
                    "Awaiting " + ", ".join(sorted(required))
                    if required
                    else "Awaiting owned-character discovery"
                )
            inventory.append(
                {
                    "operation": entry["id"],
                    "scopes": ", ".join(entry["scopes"]),
                    "status": status,
                }
            )
    public = []
    if permissions["view_public_archive"]:
        public = (
            PublicArchiveFile.objects.values("dataset__name", "dataset__enabled")
            .annotate(
                cataloged=Count("pk"),
                stored=Count("pk", filter=Q(status="STORED")),
                pending=Count("pk", filter=Q(status__in=["PENDING", "DOWNLOADING"])),
                failed=Count("pk", filter=Q(status="FAILED")),
                oldest=Min("source_time", filter=Q(status="STORED")),
                newest=Max("source_time", filter=Q(status="STORED")),
                last_saved=Max("downloaded_at", filter=Q(status="STORED")),
                catalog_bytes=Sum("remote_size"),
            )
            .order_by("dataset__name")
        )
    timeline = []
    stream_id = request.GET.get("stream", "")
    if stream_id:
        if not stream_id.isdigit():
            raise Http404("Invalid stream.")
        selected = streams.filter(pk=int(stream_id)).first()
        if selected is None:
            raise Http404("Stream not found.")
        timeline = ArchiveObservation.objects.filter(stream=selected).select_related(
            "snapshot"
        )[:100]
    disk = None
    if permissions["view_archive_storage"]:
        try:
            disk = shutil.disk_usage(archive_root())
        except OSError:
            pass
    filters = request.GET.copy()
    filters.pop("page", None)
    filters.pop("format", None)
    return render(
        request,
        "buh_max_history/history.html",
        {
            "app_name": "ESI History Archive",
            "app_version": __version__,
            "permissions": permissions,
            "page": page,
            "filters": filters.urlencode(),
            "query": request.GET.get("q", ""),
            "targets": targets.order_by("last_success_at", "pk")[:100],
            "target_totals": target_totals,
            "inventory": inventory,
            "endpoint_count": sum(bool(row["scopes"]) for row in endpoints().values()),
            "public": public,
            "timeline": timeline,
            "disk": disk,
            "config": ArchiveConfiguration.get_solo(),
        },
    )


@require_GET
@login_required
def public_history(request):
    _require(request.user, "view_public_archive")
    query = request.GET.get("q", "").strip()[:160]
    versions = PublicArchiveRevision.objects.select_related("file", "file__dataset").all()
    if query:
        versions = versions.filter(
            Q(file__source_url__icontains=query) | Q(file__dataset__name__icontains=query)
        )
    page = Paginator(versions, 50).get_page(request.GET.get("page"))
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "count": page.paginator.count,
                "page": page.number,
                "pages": page.paginator.num_pages,
                "results": [
                    {
                        "id": row.pk,
                        "source_url": row.file.source_url,
                        "dataset": row.file.dataset.slug,
                        "sha256": row.payload_sha256,
                        "first_observed_at": row.first_observed_at,
                        "last_observed_at": row.last_observed_at,
                        "stored_bytes": row.stored_bytes,
                    }
                    for row in page
                ],
            }
        )
    return render(
        request,
        "buh_max_history/public_history.html",
        {
            "app_name": "ESI History Archive",
            "app_version": __version__,
            "permissions": permission_map(request.user),
            "page": page,
            "query": query,
        },
    )


@require_GET
@login_required
def download_revision(request, revision_id):
    _require(request.user, "download_public_archive")
    try:
        revision = PublicArchiveRevision.objects.select_related("file").get(pk=revision_id)
    except PublicArchiveRevision.DoesNotExist as exc:
        raise Http404("Revision not found.") from exc
    path = _safe_archive_path(revision.relative_path)
    filename = revision.file.relative_path.rsplit("/", 1)[-1]
    return FileResponse(path.open("rb"), as_attachment=True, filename=filename)
