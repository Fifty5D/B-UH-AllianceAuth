"""Bounded, read-only database evidence for a token-selection incident.

Run inside an existing Django process. Never call require_valid(): it may
delete rejected token rows. Refreshability is deliberately reported as untested.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re

MAX_OWNERS = 500
MAX_CHARACTERS = 5000
MAX_TOKENS = 10000
MAX_STATUSES = 15000


class EvidenceBoundExceeded(RuntimeError):
    pass


def bounded(queryset, limit):
    rows = list(queryset[:limit + 1])
    if len(rows) > limit:
        raise EvidenceBoundExceeded()
    return rows


def stamp(value):
    return value.isoformat() if value is not None else None


def error_evidence(message):
    """Keep known classes/codes and a digest; never echo arbitrary error text."""
    text = message or ""
    classes = sorted(set(re.findall(
        r"\b(?:TokenDoesNotExist|TokenInvalidError|IncompleteResponseError|"
        r"MissingTokenError|InvalidGrantError|TokenError|ConnectionError|"
        r"Timeout|HTTPForbidden|HTTPUnauthorized|HTTPServerError)\b", text
    )))
    codes = sorted(set(re.findall(
        r"\b(?:invalid_grant|invalid_client|invalid_scope|unauthorized_client|"
        r"access_denied)\b", text
    )))
    return {"classes": classes, "oauth_codes": codes,
            "message_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "message_length": len(text),
            "requires_fresh_validation": True}


def reject_writes(execute, sql, params, many, context):
    """Fail closed if reporting accidentally invokes a mutating ORM path."""
    normalized = sql.lstrip().upper()
    if (not re.match(r"^(?:SELECT|SHOW|EXPLAIN)\s", normalized)
            or ";" in normalized.rstrip(";")
            or re.search(r"\b(?:FOR\s+UPDATE|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b", normalized)):
        raise RuntimeError("read-only query boundary")
    return execute(sql, params, many, context)


def token_evidence(token, required_scopes):
    scopes = sorted(item.name for item in token.scopes.all())
    created = getattr(token, "created", None)
    return {
        "id": token.pk, "user_id": token.user_id,
        "character_id": token.character_id,
        "created": stamp(created),
        "has_access_credential": bool(token.access_token),
        "has_refresh_credential": bool(token.refresh_token),
        "has_owner_identity": bool(token.character_owner_hash),
        "scopes": scopes,
        "missing_scopes": sorted(set(required_scopes) - set(scopes)),
        "fresh_refresh_result": "not_attempted_read_only",
        "permanent_failure_proven": False,
    }


def ownership_evidence(ownership, tokens, required_scopes):
    character = ownership.character
    owner_hash = getattr(ownership, "owner_hash", None)
    selected_tokens = [token for token in tokens if token.character_id == character.character_id]
    token_rows = []
    for token in selected_tokens:
        row = token_evidence(token, required_scopes)
        row["owner_identity_matches_auth_link"] = (
            token.character_owner_hash == owner_hash
            if owner_hash and token.character_owner_hash else None)
        token_rows.append(row)
    return {
        "ownership_id": ownership.pk, "user_id": ownership.user_id,
        "user_active": ownership.user.is_active,
        "user_has_structures_owner_permission":
            ownership.user.has_perm("structures.add_structure_owner"),
        "auth_character_pk": character.pk,
        "character_id": character.character_id,
        "character_name": character.character_name,
        "stored_corporation_id": character.corporation_id,
        "stored_corporation_name": character.corporation_name,
        "live_esi_corporation_result": "not_attempted_read_only",
        "auth_owner_identity_present": bool(owner_hash),
        "tokens": token_rows,
        "matching_user_token_ids": [token.pk for token in tokens
                                   if token.character_id == character.character_id
                                   and token.user_id == ownership.user_id],
    }


def _collect():
    from django.db import connection
    from django.db.models import Count, Q
    from django.db.migrations.recorder import MigrationRecorder
    from django.db.migrations.executor import MigrationExecutor
    from allianceauth.authentication.models import CharacterOwnership
    from allianceauth.eveonline.models import EveCharacter
    from esi.models import Token
    from memberaudit.models import Character, CharacterUpdateStatus
    from structures.models import Owner, OwnerCharacter

    owners = bounded(Owner.objects.select_related("corporation").order_by("pk"),
                     MAX_OWNERS)
    corp_ids = {owner.corporation.corporation_id for owner in owners}
    sticky_character_pks = set(bounded(
        CharacterUpdateStatus.objects.filter(has_token_error=True)
        .values_list("character_id", flat=True).distinct().order_by("character_id"),
        MAX_CHARACTERS,
    ))
    pilot_pks = set(Character.objects.filter(
        eve_character__character_name__iexact="Fifty5D").values_list("pk", flat=True))
    statuses = bounded(
        CharacterUpdateStatus.objects.filter(
            character_id__in=sticky_character_pks | pilot_pks
        ).select_related("character__eve_character").order_by("character_id", "section"),
        MAX_STATUSES,
    )
    affected_ids = {status.character.eve_character.character_id for status in statuses}
    ownerships = bounded(
        CharacterOwnership.objects.filter(
            Q(character__corporation_id__in=corp_ids)
            | Q(character__character_id__in=affected_ids)
            | Q(pk__in=Owner.objects.values("characters__character_ownership_id"))
        ).select_related("character", "user").order_by("pk"), MAX_CHARACTERS,
    )
    eve_ids = affected_ids | {item.character.character_id for item in ownerships}
    tokens = bounded(
        Token.objects.filter(character_id__in=eve_ids).prefetch_related("scopes")
        .order_by("character_id", "pk"), MAX_TOKENS,
    )
    ownership_by_id = {item.pk: item for item in ownerships}
    ownership_by_character = {item.character.character_id: item for item in ownerships}
    structures_scopes = Owner.esi_scopes()
    memberaudit_scopes = Character.esi_scopes()
    result = {
        "read_only": True, "scan_complete": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bounds": {"owners": MAX_OWNERS, "characters": MAX_CHARACTERS,
                   "tokens": MAX_TOKENS, "statuses": MAX_STATUSES},
        "limits": ["No refresh, ESI call, token cleanup, status reset or deployment.",
                   "Stored corporation identity does not prove current ESI ownership.",
                   "A scope-complete token is not proven refreshable.",
                   "Error flags alone do not prove revoked credentials.",
                   "Current rows cannot prove when a missing link/token was removed."],
        "auth_totals": {"characters": EveCharacter.objects.count(),
                        "character_links": CharacterOwnership.objects.count(),
                        "esi_tokens": Token.objects.count()},
        "structures_required_scopes": sorted(structures_scopes),
        "memberaudit_required_scopes": sorted(memberaudit_scopes),
        "structures": [], "memberaudit": [],
    }
    for owner in owners:
        configured = bounded(OwnerCharacter.objects.filter(owner_id=owner.pk).order_by("pk"),
                             MAX_CHARACTERS)
        configured_rows = []
        for item in configured:
            ownership = ownership_by_id.get(item.character_ownership_id)
            row = {
                "owner_character_id": item.pk,
                "ownership_id": item.character_ownership_id,
                "enabled": item.is_enabled, "error_count": item.error_count,
                "created_at": stamp(item.created_at),
                "structures_last_used_at": stamp(item.structures_last_used_at),
                "notifications_last_used_at": stamp(item.notifications_last_used_at),
                "disabled_reason": error_evidence(item.disabled_reason),
                "disabled_for_no_valid_token":
                    item.disabled_reason == "No valid token found for character",
                "auth_link_exists": ownership is not None,
            }
            if ownership is not None:
                row["identity"] = ownership_evidence(ownership, tokens, structures_scopes)
                row["stored_corporation_matches_owner"] = (
                    ownership.character.corporation_id == owner.corporation.corporation_id
                )
            configured_rows.append(row)
        candidates = [ownership_evidence(item, tokens, structures_scopes)
                      for item in ownerships
                      if item.character.corporation_id == owner.corporation.corporation_id
                      and item.pk not in {row.character_ownership_id for row in configured}]
        result["structures"].append({
            "owner_pk": owner.pk,
            "corporation_id": owner.corporation.corporation_id,
            "corporation_name": owner.corporation.corporation_name,
            "enabled": owner.is_active,
            "included_in_service_status": owner.is_included_in_service_status,
            "is_up": owner.is_up,
            "legacy_ownership_id": owner.character_ownership_id,
            "configured_characters": configured_rows,
            "other_linked_characters_in_stored_corporation": candidates,
            "structure_count": owner.structures.count(),
            **{name: stamp(getattr(owner, name)) for name in (
                "structures_last_update_at", "assets_last_update_at",
                "notifications_last_update_at", "forwarding_last_update_at")},
            "classification": "awaiting_live_token_and_esi_validation",
        })
    grouped = {}
    for status in statuses:
        character = status.character
        row = grouped.setdefault(character.pk, {
            "memberaudit_character_pk": character.pk,
            "character_id": character.eve_character.character_id,
            "character_name": character.eve_character.character_name,
            "disabled": character.is_disabled,
            "token_error_notified_at": stamp(character.token_error_notified_at),
            "sections": [],
        })
        row["sections"].append({
            "status_pk": status.pk, "section": status.section,
            "has_token_error": status.has_token_error, "is_success": status.is_success,
            "error": error_evidence(status.error_message),
            **{name: stamp(getattr(status, name)) for name in (
                "run_started_at", "run_finished_at", "update_started_at", "update_finished_at")},
            "content_hashes_present": [bool(status.content_hash_1),
                                       bool(status.content_hash_2), bool(status.content_hash_3)],
        })
    for row in grouped.values():
        ownership = ownership_by_character.get(row["character_id"])
        row["auth_link_exists"] = ownership is not None
        if ownership is not None:
            row["identity"] = ownership_evidence(ownership, tokens, memberaudit_scopes)
        else:
            row["orphaned_token_records"] = [
                token_evidence(token, memberaudit_scopes) for token in tokens
                if token.character_id == row["character_id"]
            ]
        result["memberaudit"].append(row)
    sticky = [status for status in statuses if status.has_token_error]
    result["summary"] = {
        "structures_owners": len(owners),
        "structures_active": sum(owner.is_active for owner in owners),
        "structures_with_disabled_sync_characters": sum(
            any(not row["enabled"] for row in owner["configured_characters"])
            for owner in result["structures"]),
        "memberaudit_sticky_characters": len({status.character_id for status in sticky}),
        "memberaudit_sticky_sections": len(sticky),
        "memberaudit_sticky_by_section": dict(Counter(status.section for status in sticky)),
        "permanently_invalid_proven": None,
        "recovered_in_this_read_only_report": 0,
    }
    migrations = bounded(
        MigrationRecorder.Migration.objects.order_by("app", "name").values(
            "app", "name", "applied"), 10000,
    )
    result["migrations"] = [{**row, "applied": stamp(row["applied"])} for row in migrations]
    result["memberaudit_status_totals"] = list(
        CharacterUpdateStatus.objects.values("has_token_error", "is_success")
        .annotate(count=Count("pk")).order_by("has_token_error", "is_success")
    )
    executor = MigrationExecutor(connection)
    result["pending_migrations_for_current_runtime"] = [
        {"app": migration.app_label, "name": migration.name, "backwards": backwards}
        for migration, backwards in executor.migration_plan(executor.loader.graph.leaf_nodes())
    ]
    result["database_vendor"] = connection.vendor
    return result


def collect():
    from django.db import connection
    # Django's MariaDB/MySQL driver performs session-only SET statements while
    # opening a fresh connection. Initialize it before guarding report queries;
    # the guard still rejects every application-data write.
    connection.ensure_connection()
    with connection.execute_wrapper(reject_writes):
        return _collect()


def failure_evidence(exc):
    """Known source locations only; no messages, SQL, locals or credentials."""
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        name = frame.f_code.co_name
        filename = frame.f_code.co_filename
        if ((filename.endswith("/database_report.py")
             or filename == "<buh-database-report>")
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name)):
            frames.append({"function": name, "line": trace.tb_lineno})
        trace = trace.tb_next
    result = {"error_type": type(exc).__name__, "report_frames": frames[-12:]}
    if isinstance(exc, AttributeError):
        name = getattr(exc, "name", None)
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            result["attribute_name"] = name
    return result


def emit():
    """One marker-delimited report; never export caught exception messages."""
    try:
        result = collect()
    except Exception as exc:
        result = {"read_only": True, "scan_complete": False,
                  **failure_evidence(exc)}
    print("BUH_INCIDENT_REPORT_BEGIN")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    print("BUH_INCIDENT_REPORT_END")
