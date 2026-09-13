"""Incremental read-only ESI collection using existing character authorizations.

The reviewed manifest defines the GET operations. Credentials never enter task
arguments, cursors, archive metadata, or diagnostic messages.
"""

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from django.db.models import Q
from django.utils.timezone import now

from . import __version__
from .capture import _CAPTURE_LOCAL, _free_bytes, archive_esi_result
from .models import ArchiveCollectionTarget, ArchiveConfiguration
from .job_lock import ArchiveLockLost, assert_public_archive_lock


@lru_cache(maxsize=1)
def endpoint_manifest():
    return json.loads(Path(__file__).with_name("esi_endpoints.json").read_text())


@lru_cache(maxsize=1)
def endpoints():
    return {entry["id"]: entry for entry in endpoint_manifest()["endpoints"]}


@lru_cache(maxsize=1)
def esi_client():
    from esi.openapi_clients import ESIClientProvider

    return ESIClientProvider(
        compatibility_date=endpoint_manifest()["compatibility_date"],
        ua_appname="B-UH History Archive",
        ua_version=__version__,
        ua_url="https://github.com/Fifty5D/B-UH-AllianceAuth",
        operations=list(endpoints()),
    ).client


def target_key(kind, operation_id, parameters, character_id=None):
    return hashlib.sha256(
        json.dumps(
            [kind, operation_id, parameters, character_id],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def new_target(entry, parameters, character_id, corporation_id=None):
    return ArchiveCollectionTarget(
        key=target_key("esi", entry["id"], parameters, character_id),
        operation_id=entry["id"],
        parameters=parameters,
        character_id=character_id,
        corporation_id=corporation_id,
    )


def owned_characters():
    from allianceauth.authentication.models import CharacterOwnership

    return CharacterOwnership.objects.select_related("character", "user").filter(
        user__is_active=True
    )


def seed_targets(*, limit=30):
    """Rotate through owners; existing jobs/cursors and disabled targets survive."""
    config = ArchiveConfiguration.get_solo()
    owners = list(
        owned_characters()
        .filter(pk__gt=config.discovery_character_cursor)
        .order_by("pk")[:limit]
    )
    if not owners:
        ArchiveConfiguration.objects.filter(singleton_id=1).update(
            discovery_character_cursor=0
        )
        return 0
    targets = []
    for owner in owners:
        char = owner.character
        base = {"character_id": char.character_id, "corporation_id": char.corporation_id}
        if char.alliance_id:
            base["alliance_id"] = char.alliance_id
        for entry in endpoints().values():
            required = set(entry["required"])
            if not required <= base.keys():
                continue
            parameters = {key: base[key] for key in entry["required"]}
            # Corp endpoints are shared, not fetched once for every member.
            is_corp = "corporation_id" in required and "character_id" not in required
            identity = None if is_corp else char.character_id
            targets.append(
                new_target(
                    entry, parameters, identity, char.corporation_id if is_corp else None
                )
            )
    ArchiveCollectionTarget.objects.bulk_create(
        targets, ignore_conflicts=True, batch_size=200
    )
    ArchiveConfiguration.objects.filter(singleton_id=1).update(
        discovery_character_cursor=owners[-1].pk
    )
    return len(targets)


def token_for(target, entry):
    from esi.models import Token

    owners = owned_characters()
    if target.character_id is not None:
        owners = owners.filter(character__character_id=target.character_id)
    elif target.corporation_id is not None:
        owners = owners.filter(character__corporation_id=target.corporation_id)
    else:
        return None
    pairs = list(owners.values_list("character__character_id", "user_id", "owner_hash"))
    if not pairs:
        return None
    match = Q()
    for character_id, user_id, owner_hash in pairs:
        match |= Q(
            character_id=character_id, user_id=user_id, character_owner_hash=owner_hash
        )
    query = Token.objects.filter(match)
    if entry["scopes"]:
        query = query.require_scopes(entry["scopes"])
    # After a role denial, try another already-authorized corporation character.
    denied = target.cursor.get("denied_characters", [])
    return query.exclude(character_id__in=denied).order_by("pk").first()


@contextmanager
def manual_capture():
    previous = getattr(_CAPTURE_LOCAL, "active", False)
    _CAPTURE_LOCAL.active = True
    try:
        yield
    finally:
        _CAPTURE_LOCAL.active = previous


def response_rows(data):
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        rows = [data]
        for value in data.values():
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
        return rows
    return []


def discover_children(target, entry, data):
    """Follow identifiers returned by authorized reads, never enumerate guesses."""
    rows = response_rows(data)
    children = []
    for child in endpoints().values():
        nested = child["path"].startswith(entry["path"] + "/{")
        related = child["path"].startswith(
            ("/killmails/", "/fleets/", "/universe/structures/", "/markets/structures/")
        )
        if not nested and not related:
            continue
        for row in rows:
            parameters = dict(target.parameters)
            parameters.update({key: row[key] for key in child["required"] if key in row})
            missing = set(child["required"]) - parameters.keys()
            if nested and len(missing) == 1 and "id" in row:
                parameters[next(iter(missing))] = row["id"]
            if not set(child["required"]) <= parameters.keys():
                continue
            parameters = {key: parameters[key] for key in child["required"]}
            if any(
                not isinstance(v, (str, int)) or isinstance(v, bool) or len(str(v)) > 100
                for v in parameters.values()
            ):
                continue
            if child["path"].endswith("/bids") and row.get("type") not in (None, "auction"):
                continue
            children.append(
                new_target(child, parameters, target.character_id, target.corporation_id)
            )
    ArchiveCollectionTarget.objects.bulk_create(
        children, ignore_conflicts=True, batch_size=200
    )
    return len(children)


def next_page(entry, data, headers, cursor):
    parameters = set(entry["parameters"])
    page = int(cursor.get("page", 1))
    if "page" in parameters and page < int(headers.get("x-pages", "1")):
        return {"page": page + 1}
    if "last_mail_id" in parameters and data:
        values = [row["mail_id"] for row in response_rows(data) if "mail_id" in row]
        if values:
            value = min(values)
            if value != cursor.get("last_mail_id"):
                return {"last_mail_id": value}
    if "from_id" in parameters and data:
        values = [
            row["transaction_id"] for row in response_rows(data) if "transaction_id" in row
        ]
        if values:
            value = min(values)
            if value != cursor.get("from_id"):
                return {"from_id": value}
    if "from_event" in parameters and data:
        values = [row["event_id"] for row in response_rows(data) if "event_id" in row]
        if values and max(values) != cursor.get("from_event"):
            return {"from_event": max(values)}
    if "before" in parameters and isinstance(data, dict):
        value = (
            data.get("cursor", {}).get("before")
            if isinstance(data.get("cursor"), dict)
            else None
        )
        if value and value != cursor.get("before"):
            return {"before": value}
    return {}


def default_interval(entry):
    if any(word in entry["path"] for word in ("/location", "/online", "/ship", "/fleet")):
        return timedelta(minutes=15)
    return timedelta(hours=1)


def cache_due(entry, headers):
    due = now() + default_interval(entry)
    try:
        expires = parsedate_to_datetime(headers.get("expires", ""))
        if expires.tzinfo is not None:
            due = max(due, expires)
    except (TypeError, ValueError):
        pass
    return due


def collect_target(target, client):
    assert_public_archive_lock("collection")
    entry = endpoints().get(target.operation_id)
    if entry is None:
        target.status, target.detail = (
            "unsupported",
            "Endpoint is not in the reviewed read-only manifest.",
        )
        target.next_attempt_at = now() + timedelta(days=1)
        target.save()
        return False
    token = token_for(target, entry)
    if token is None:
        target.status = "missing_access"
        target.detail = "No current owned character token with: " + (
            ", ".join(entry["scopes"]) or "character ownership"
        )
        target.last_attempt_at = now()
        target.next_attempt_at = now() + timedelta(hours=6)
        target.cursor.pop("denied_characters", None)
        target.save()
        return False
    parameters = {k: v for k, v in target.parameters.items() if k != "archive_history"} | {
        k: v for k, v in target.cursor.items() if k in entry["parameters"]
    }
    if "include_completed" in entry["parameters"]:
        parameters["include_completed"] = True
    if "state" in entry["parameters"] and "/projects" in entry["path"]:
        parameters["state"] = "All"
    if "limit" in entry["parameters"]:
        parameters["limit"] = 100
    if "before" in entry["parameters"]:
        parameters.setdefault("before", "0")
    target.last_attempt_at = now()
    target.next_attempt_at = now() + timedelta(minutes=15)
    target.status = "running"
    target.save()
    try:
        operation = getattr(getattr(client, entry["tag"].replace(" ", "_")), entry["id"])
        if entry["scopes"]:
            parameters["token"] = token
        operation = operation(**parameters)
        if operation.method.upper() != "GET":
            raise ValueError("Only reviewed GET operations may run in the collector.")
        with manual_capture():
            data, response = operation.result(return_response=True)
        assert_public_archive_lock("collection")
        try:
            data = json.loads(response.content)
        except (TypeError, ValueError):
            from .capture import _json_safe

            data = _json_safe(data)
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        # Related unauthenticated routes (e.g. killmail hashes) inherit the
        # private origin of this collector, rather than becoming public exports.
        operation.token = SimpleNamespace(
            character_id=token.character_id, user_id=token.user_id
        )
        snapshot = archive_esi_result(operation, data, response)
        if snapshot is None:
            target.status, target.detail = (
                "storage_paused",
                "Response was not saved; inspect capture guards.",
            )
            target.save()
            return True
        discover_children(target, entry, data)
        cursor = next_page(entry, data, headers, target.cursor)
        is_history = bool(target.parameters.get("archive_history"))
        if cursor and not is_history:
            history = new_target(
                entry,
                target.parameters | {"archive_history": True},
                target.character_id,
                target.corporation_id,
            )
            historical, created = ArchiveCollectionTarget.objects.get_or_create(
                key=history.key,
                defaults={
                    "operation_id": history.operation_id,
                    "parameters": history.parameters,
                    "character_id": history.character_id,
                    "corporation_id": history.corporation_id,
                    "cursor": cursor,
                    "status": "backlog",
                },
            )
            if not created and historical.status == "history_complete":
                historical.cursor, historical.status = cursor, "backlog"
                historical.next_attempt_at = now()
                historical.save()
            # The first page keeps its own refresh schedule while the historical
            # lane advances. A long mailbox import never suspends new-mail reads.
            cursor = {}
        target.cursor = cursor
        target.last_success_at = now()
        target.failure_count = 0
        target.detail = ""
        target.status = "backlog" if cursor else "current"
        target.next_attempt_at = now() if cursor else cache_due(entry, headers)
        if is_history and not cursor:
            # The head collector explicitly reopens a completed history pass.
            # Do not independently restart from page one and duplicate head reads.
            target.status = "history_complete"
            target.next_attempt_at = now() + timedelta(days=3650)
        if not cursor:
            target.completed_at = now()
        target.save()
    except ArchiveLockLost:
        raise
    except Exception as exc:
        from esi.exceptions import ESIBucketLimitException, ESIErrorLimitException

        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
        if status is None:
            status = getattr(exc, "status", None)
        if isinstance(exc, (ESIBucketLimitException, ESIErrorLimitException)):
            status = 429
        target.failure_count += 1
        target.status = "missing_access" if status in (401, 403) else "failed"
        if isinstance(exc, AttributeError):
            target.status = "unsupported"
        target.detail = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
        delay = min(86400, 900 * 2 ** min(target.failure_count - 1, 7))
        if status in (401, 403):
            target.cursor["denied_characters"] = sorted(
                set(target.cursor.get("denied_characters", []) + [token.character_id])
            )[:100]
        if status in (420, 429):
            try:
                delay = max(delay, int(getattr(exc, "reset", 0) or 0))
            except (TypeError, ValueError):
                pass
            try:
                delay = max(
                    delay,
                    int(
                        (
                            getattr(response, "headers", None)
                            or getattr(exc, "headers", {})
                        ).get("Retry-After", "900")
                    ),
                )
            except (AttributeError, ValueError):
                pass
            ArchiveConfiguration.objects.filter(singleton_id=1).update(
                active_retry_at=now() + timedelta(seconds=delay)
            )
        target.next_attempt_at = now() + timedelta(seconds=delay)
        target.save()
    return True


def collect_esi_batch(*, deadline=None):
    config = ArchiveConfiguration.get_solo()
    if not (
        config.capture_enabled and config.capture_private_esi and config.active_esi_enabled
    ):
        return {"paused": "disabled"}
    if config.active_retry_at and config.active_retry_at > now():
        return {"paused": "rate_limit", "retry_at": config.active_retry_at.isoformat()}
    if _free_bytes() < config.minimum_free_gib * 1024**3:
        return {"paused": "storage"}
    seed_targets()
    deadline = deadline or time.monotonic() + 180
    attempted = checked = 0
    # Fresh states and history cursors both get service. Each target receives
    # one page per batch, so large mailboxes cannot monopolize the worker.
    due = (
        ArchiveCollectionTarget.objects.filter(kind="esi", next_attempt_at__lte=now())
        .exclude(status="disabled")
        .order_by("last_attempt_at", "pk")
    )
    from itertools import zip_longest

    limit = config.active_requests_per_run * 3
    lanes = [
        list(due.filter(last_attempt_at__isnull=True).exclude(status="backlog")[:limit]),
        list(due.filter(status="backlog")[:limit]),
        list(due.filter(last_attempt_at__isnull=False).exclude(status="backlog")[:limit]),
    ]
    candidates = (item for row in zip_longest(*lanes) for item in row if item is not None)
    client = None
    for target in candidates:
        if (
            checked >= limit
            or attempted >= config.active_requests_per_run
            or time.monotonic() >= deadline
        ):
            break
        if _free_bytes() < config.minimum_free_gib * 1024**3:
            return {"attempted": attempted, "checked": checked, "paused": "storage"}
        if client is None:
            client = esi_client()
        attempted += int(collect_target(target, client))
        checked += 1
        config.refresh_from_db(fields=["active_retry_at"])
        if config.active_retry_at and config.active_retry_at > now():
            break
    return {"attempted": attempted, "checked": checked}
