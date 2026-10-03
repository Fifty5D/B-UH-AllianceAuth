"""Bounded Structures repair using an existing token; no token cleanup APIs.

Executed in a fresh Django shell by the owner-operated incident launcher.
No installed application, infrastructure, Member Audit status or release is changed.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import re
import signal
import time

import requests
from unittest.mock import patch

from allianceauth.authentication.models import CharacterOwnership
from django.db import connection, transaction
from django.db.models.signals import pre_delete, pre_save
from django.utils.timezone import now
from esi.models import Token
from structures.models import Owner, OwnerCharacter, Structure, StructureItem, StructureService


DISABLE_REASON = "No valid token found for character"
SYNC_FIELDS = (
    "structures_last_update_at", "assets_last_update_at",
    "notifications_last_update_at",
)


class RecoveryStop(Exception):
    def __init__(self, category, *, status_code=None, protected_record_type=None):
        self.category = category
        self.status_code = status_code
        self.protected_record_type = protected_record_type
        super().__init__(category)


def stamp(value):
    return value.isoformat() if value else None


def exception_category(error):
    """Use types/codes only. Never print exception messages or OAuth responses."""
    classes, oauth, statuses = [], [], []
    ownership_rejected = False
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        name = type(current).__name__
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            classes.append(name)
        if name == "InvalidTokenError" and getattr(current, "description", None) == "Ownership Changed! Revoke me!":
            ownership_rejected = True
        code = getattr(current, "error", None)
        if code in {"invalid_grant", "invalid_client", "invalid_scope", "invalid_token",
                    "unauthorized_client", "temporarily_unavailable", "server_error"}:
            oauth.append(code)
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and 100 <= status <= 599:
            statuses.append(status)
        current = current.__cause__ or current.__context__
    if "invalid_grant" in oauth or "InvalidGrantError" in classes:
        category = "permanent_invalid_grant"
    elif ownership_rejected:
        category = "permanent_ownership_mismatch"
    elif "invalid_client" in oauth or "ImproperlyConfigured" in classes:
        category = "application_sso_configuration_failure"
    elif any(code in {420, 429} for code in statuses):
        category = "provider_throttling"
    elif isinstance(error, RecoveryStop):
        category = error.category
    else:
        category = "retryable_or_unclassified_failure"
    result = {"category": category, "exception_types": classes,
              "oauth_codes": sorted(set(oauth)), "http_statuses": sorted(set(statuses))}
    if isinstance(error, RecoveryStop) and error.protected_record_type and re.fullmatch(
        r"[a-z_][a-z0-9_.]{0,100}", error.protected_record_type,
    ):
        result["protected_record_type"] = error.protected_record_type
    return result



def failure_sites(error):
    """Bounded code locations only; no paths, messages, locals or response bodies."""
    sites, seen = [], set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        trace = current.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            module = trace.tb_frame.f_globals.get("__name__", "")
            if code.co_filename in {globals().get("__file__"), "<buh-structures-recovery>"}:
                component = "recovery"
            elif re.fullmatch(r"(?:structures|esi)(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module):
                component = module
            else:
                component = None
            if component and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", code.co_name):
                site = {"component": component, "function": code.co_name, "line": trace.tb_lineno}
                if site not in sites:
                    sites.append(site)
                    if len(sites) >= 16:
                        return {"failure_sites": sites, "failure_sites_truncated": True}
            trace = trace.tb_next
        current = current.__cause__ or current.__context__
    return {"failure_sites": sites, "failure_sites_truncated": current is not None}


def public_character_identity(character_id):
    """Fresh public identity lookup independent of Structures' restricted client.

    No stored credential is sent. The enclosing request/deadline guard applies
    to this GET as well as the native SSO/ESI calls.
    """
    if type(character_id) is not int or character_id <= 0:
        raise RecoveryStop("invalid_public_character_id")
    url = f"https://esi.evetech.net/latest/characters/{character_id}/?datasource=tranquility"
    started = now()
    with requests.get(
        url, headers={"User-Agent": "B-UH-existing-token-incident-recovery",
                      "Cache-Control": "no-cache"},
        stream=True, allow_redirects=False, timeout=15,
    ) as response:
        if response.status_code != 200:
            raise RecoveryStop(
                "public_character_unavailable" if response.status_code == 404
                else "public_character_http_rejection",
                status_code=response.status_code,
            )
        if response.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise RecoveryStop("public_character_response_not_json")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=4096):
            size += len(chunk)
            if size > 16 * 1024:
                raise RecoveryStop("public_character_response_exceeds_bound")
            chunks.append(chunk)
        try:
            data = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            raise RecoveryStop("public_character_response_incomplete") from None
        if (not isinstance(data, dict) or type(data.get("corporation_id")) is not int
                or data["corporation_id"] <= 0):
            raise RecoveryStop("public_character_identity_incomplete")
        return {"character_id": character_id, "corporation_id": data["corporation_id"],
                "http_status": response.status_code, "byte_size": size,
                "requested_at": stamp(started), "received_at": stamp(now())}


def identity(token):
    return (token.pk, token.user_id, token.character_id, token.character_owner_hash,
            token.token_type)


@contextmanager
def preserve_records(owner_pk, token_pk, expected_token_identity):
    """Keep history/credentials; replace only this owner's current items/services."""
    current_models = (StructureItem, StructureService)
    approved_ids = {model._meta.db_table: set() for model in current_models}
    delete_patterns = {}
    for table in approved_ids:
        qualified_id = connection.ops.quote_name(table) + "." + connection.ops.quote_name("id")
        delete_patterns[table] = re.compile(
            r"DELETE\s+FROM\s+" + re.escape(connection.ops.quote_name(table))
            + r"\s+WHERE\s+" + re.escape(qualified_id)
            + r"\s+IN\s*\((?P<slots>%s(?:,\s*%s)*)\)\s*;?",
            re.I,
        )

    def before_delete(sender, instance, **kwargs):
        if sender in current_models and instance.structure.owner_id == owner_pk:
            approved_ids[sender._meta.db_table].add(instance.pk)
            if len(approved_ids[sender._meta.db_table]) > 50000:
                raise RecoveryStop("current_snapshot_replacement_exceeds_bound")
            return
        raise RecoveryStop("protected_record_deletion_blocked",
                           protected_record_type=sender._meta.label_lower)

    def before_save(sender, instance, **kwargs):
        if sender is Token and (
            instance.pk != token_pk or identity(instance) != expected_token_identity
        ):
            raise RecoveryStop("token_identity_change_blocked")
        if sender is Structure:
            previous = Structure.objects.filter(pk=instance.pk).values_list(
                "owner_id", flat=True
            ).first()
            if instance.owner_id != owner_pk or previous not in {None, owner_pk}:
                raise RecoveryStop("structure_owner_change_blocked")
        if sender in current_models and instance.structure.owner_id != owner_pk:
            raise RecoveryStop("current_snapshot_owner_change_blocked")

    def guard_sql(execute, sql, params, many, context):
        command = sql.strip()
        if re.match(r"DELETE\s", command, re.I):
            for table, pattern in delete_patterns.items():
                match = pattern.fullmatch(command)
                if match and not many and params and (
                    len(params) == match.group("slots").count("%s")
                    and all(type(value) is int and value in approved_ids[table] for value in params)
                ):
                    result = execute(sql, params, many, context)
                    approved_ids[table].difference_update(params)
                    return result
            raise RecoveryStop("protected_record_deletion_blocked",
                               protected_record_type="unverified_sql_delete")
        return execute(sql, params, many, context)

    pre_delete.connect(before_delete, weak=False)
    pre_save.connect(before_save, weak=False)
    try:
        with connection.execute_wrapper(guard_sql):
            yield
    finally:
        pre_save.disconnect(before_save)
        pre_delete.disconnect(before_delete)


@contextmanager
def bounded_requests(maximum_seconds=360):
    """Bound requests used by the pinned SSO/ESI client; emit no credential logs."""
    original = requests.sessions.Session.request
    deadline = time.monotonic() + maximum_seconds
    previous_level = logging.root.manager.disable
    previous_handler = signal.getsignal(signal.SIGALRM)

    def alarm_handler(signum, frame):
        raise RecoveryStop("operation_deadline_exceeded")

    signal.signal(signal.SIGALRM, alarm_handler)
    signal.alarm(maximum_seconds)

    def request(session, method, url, *args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RecoveryStop("operation_deadline_exceeded")
        kwargs["timeout"] = min(15, remaining)
        response = original(session, method, url, *args, **kwargs)
        failure = None
        if response.status_code in {420, 429}:
            failure = "provider_throttling"
        elif response.status_code >= 500:
            failure = "provider_server_failure"
        remaining_errors = response.headers.get("X-Esi-Error-Limit-Remain")
        if (failure is None and remaining_errors is not None
                and str(remaining_errors).isdigit() and int(remaining_errors) <= 5):
            failure = "provider_error_budget_low"
        if failure:
            response.close()
            raise RecoveryStop(failure, status_code=response.status_code)
        return response

    logging.disable(logging.CRITICAL)
    try:
        with patch.object(requests.sessions.Session, "request", request):
            yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        logging.disable(previous_level)


def describe(owner_character, token):
    owner = owner_character.owner
    ownership = owner_character.character_ownership
    return {
        "owner_pk": owner.pk, "owner_name": owner.corporation.corporation_name,
        "owner_active": owner.is_active, "owner_is_up": owner.is_up,
        "owner_character_pk": owner_character.pk,
        "character_name": ownership.character.character_name,
        "character_id": ownership.character.character_id,
        "auth_link_pk": ownership.pk, "token_pk": token.pk,
        "token_created_at": stamp(token.created),
        "enabled": owner_character.is_enabled,
        "disabled_for_no_valid_token": owner_character.disabled_reason == DISABLE_REASON,
        "error_count": owner_character.error_count,
        "required_missing_scopes": sorted(
            set(Owner.esi_scopes()) - set(token.scopes.values_list("name", flat=True))
        ),
        "token_identity_matches_auth": bool(ownership.owner_hash)
            and ownership.owner_hash == token.character_owner_hash,
        "token_has_refresh_credential": bool(token.refresh_token),
        "owner_timestamps": {field: stamp(getattr(owner, field)) for field in SYNC_FIELDS},
        "freshness": {
            "structures": owner.is_structure_sync_fresh,
            "assets": owner.is_assets_sync_fresh,
            "notifications": owner.is_notification_sync_fresh,
            "forwarding": owner.is_forwarding_sync_fresh,
            "all": owner.are_all_syncs_ok,
        },
        "forwarding_last_update_at": stamp(owner.forwarding_last_update_at),
        "structure_count": owner.structures.count(),
        "current_service_count": StructureService.objects.filter(structure__owner_id=owner.pk).count(),
        "current_item_count": StructureItem.objects.filter(structure__owner_id=owner.pk).count(),
        "notification_count": owner.notification_set.count(),
        "existing_token_ids": list(Token.objects.filter(
            character_id=ownership.character.character_id
        ).order_by("pk").values_list("pk", flat=True)),
    }


def eligible(owner_character, token, target):
    ownership = owner_character.character_ownership
    owner = owner_character.owner
    if owner.pk != target["owner_pk"] or ownership.pk != target["auth_link_pk"]:
        raise RecoveryStop("selection_changed")
    if not owner.is_active:
        raise RecoveryStop("owner_not_active")
    if ownership.character.character_id != target["character_id"]:
        raise RecoveryStop("character_identity_changed")
    if (token.user_id != ownership.user_id
            or token.character_id != ownership.character.character_id):
        raise RecoveryStop("token_identity_mismatch")
    if (not ownership.user.is_active
            or not ownership.user.has_perm("structures.add_structure_owner")):
        raise RecoveryStop("required_auth_permission_missing")
    if ownership.character.corporation_id != owner.corporation.corporation_id:
        raise RecoveryStop("stored_corporation_mismatch")
    if (not ownership.owner_hash
            or ownership.owner_hash != token.character_owner_hash):
        raise RecoveryStop("owner_identity_mismatch_or_unproven")
    if set(Owner.esi_scopes()) - set(token.scopes.values_list("name", flat=True)):
        raise RecoveryStop("missing_required_scopes")
    if not token.refresh_token:
        raise RecoveryStop("missing_refresh_credential")
    if not owner_character.is_enabled and owner_character.disabled_reason != DISABLE_REASON:
        raise RecoveryStop("different_disabled_state")


def verify_claims(token):
    data = Token.get_token_data(token.access_token)
    if not isinstance(data, dict):
        raise RecoveryStop("fresh_token_identity_validation_incomplete")
    subject = data.get("sub")
    subject_id = data.get("character_id")
    if subject is not None:
        match = re.fullmatch(r"CHARACTER:EVE:([1-9][0-9]*)", str(subject))
        if not match:
            raise RecoveryStop("fresh_token_subject_validation_incomplete")
        subject_id = int(match.group(1))
    if subject_id != token.character_id or data.get("owner") != token.character_owner_hash:
        raise RecoveryStop("fresh_token_ownership_mismatch")
    scopes = data.get("scopes", data.get("scp"))
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, (list, set, tuple)):
        raise RecoveryStop("fresh_token_scope_validation_incomplete")
    missing = sorted(set(Owner.esi_scopes()) - set(scopes))
    if missing:
        raise RecoveryStop("fresh_token_missing_required_scopes")


def selected(target):
    # Missing tokens can trigger upstream selector-removal signals. Report the
    # actual missing credential first, while recording every presence bit below.
    try:
        token = Token.objects.get(pk=target["token_pk"])
    except Token.DoesNotExist:
        raise RecoveryStop("existing_token_record_missing") from None
    try:
        owner_character = OwnerCharacter.objects.select_related(
            "owner__corporation", "character_ownership__character", "character_ownership__user"
        ).get(pk=target["owner_character_pk"])
    except OwnerCharacter.DoesNotExist:
        raise RecoveryStop("configured_character_or_auth_link_missing") from None
    return owner_character, token


def run(target, *, apply=False):
    result = {
        "schema_version": 1, "read_only": not apply, "apply_requested": apply,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "category": "not_attempted", "phase": "preflight",
        "recovered": False, "refresh_attempted": False,
        "memberaudit_changes": False, "tokens_deleted": False, "links_removed": False,
        "historical_records_deleted": False, "steps": [],
    }
    before = None
    try:
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise RecoveryStop("pending_migrations_for_current_runtime")
        result["record_presence"] = {
            "existing_token_exists": Token.objects.filter(pk=target["token_pk"]).exists(),
            "auth_link_exists": CharacterOwnership.objects.filter(pk=target["auth_link_pk"]).exists(),
            "configured_character_exists": OwnerCharacter.objects.filter(pk=target["owner_character_pk"]).exists(),
        }
        result["phase"] = "select_existing_records"
        owner_character, token = selected(target)
        eligible(owner_character, token, target)
        before = describe(owner_character, token)
        result["before"] = before
        if not apply:
            result["category"] = "eligible_existing_token_not_yet_validated"
            return result
        expected_identity = identity(token)
        with bounded_requests(), preserve_records(
            owner_character.owner_id, token.pk, expected_identity
        ):
            result["phase"] = "refresh_existing_token"
            result["refresh_attempted"] = True
            # Deliberately never require_valid, refresh_or_delete, or create a token.
            token.refresh()
            token.refresh_from_db()
            if identity(token) != expected_identity or token.created <= datetime.fromisoformat(
                before["token_created_at"]
            ):
                raise RecoveryStop("fresh_refresh_not_proven")
            result["refresh_succeeded"] = True
            result["phase"] = "verify_fresh_claims"
            verify_claims(token)
            result["fresh_identity_and_scopes_verified"] = True
            result["phase"] = "verify_public_character_identity"
            current_character = public_character_identity(token.character_id)
            result["public_character_identity"] = current_character
            if current_character["corporation_id"] != owner_character.owner.corporation.corporation_id:
                raise RecoveryStop("current_corporation_changed")
            result["current_corporation_verified"] = True
            # The disabled selector stays disabled throughout the native sync.
            # Failed syncs roll back sync writes without rolling back a rotated refresh grant.
            with transaction.atomic():
                owner = Owner.objects.select_for_update().select_related("corporation").get(
                    pk=owner_character.owner_id
                )
                OwnerCharacter.objects.select_for_update().get(pk=owner_character.pk)
                owner_character.refresh_from_db()
                eligible(owner_character, token, target)
                original_fetch = owner.fetch_token
                owner.fetch_token = lambda *args, **kwargs: token
                original_assets = owner._fetch_owner_assets_from_esi
                original_notifications = owner._fetch_notifications_from_esi

                def limited_assets(*args, **kwargs):
                    value = original_assets(*args, **kwargs)
                    if len(value) > 50000:
                        raise RecoveryStop("assets_exceed_pilot_bound")
                    return value

                def limited_notifications(*args, **kwargs):
                    value = original_notifications(*args, **kwargs)
                    if len(value) > 5000:
                        raise RecoveryStop("notifications_exceed_pilot_bound")
                    return value

                owner._fetch_owner_assets_from_esi = limited_assets
                owner._fetch_notifications_from_esi = limited_notifications
                try:
                    for method, field in (
                        ("update_structures_esi", "structures_last_update_at"),
                        ("update_asset_esi", "assets_last_update_at"),
                        ("fetch_notifications_esi", "notifications_last_update_at"),
                    ):
                        result["phase"] = method
                        started = now()
                        getattr(owner, method)()
                        owner.refresh_from_db()
                        finished = getattr(owner, field)
                        if not finished or finished < started:
                            raise RecoveryStop("native_sync_did_not_complete_" + method)
                        result["steps"].append({
                            "step": method, "completed": True,
                            "started_at": stamp(started), "persisted_at": stamp(finished),
                        })
                finally:
                    owner.fetch_token = original_fetch
                    owner._fetch_owner_assets_from_esi = original_assets
                    owner._fetch_notifications_from_esi = original_notifications
                result["phase"] = "reenable_verified_selector"
                if not owner_character.is_enabled:
                    changed = OwnerCharacter.objects.filter(
                        pk=owner_character.pk, is_enabled=False,
                        disabled_reason=DISABLE_REASON,
                        character_ownership_id=target["auth_link_pk"], owner_id=target["owner_pk"],
                    ).update(is_enabled=True, disabled_reason="", error_count=0)
                    if changed != 1:
                        raise RecoveryStop("disabled_state_changed_during_sync")
                result["phase"] = "verify_record_preservation"
                if not CharacterOwnership.objects.filter(
                    pk=target["auth_link_pk"], user_id=token.user_id,
                    character__character_id=token.character_id,
                ).exists():
                    raise RecoveryStop("auth_link_preservation_not_proven")
                if list(Token.objects.filter(character_id=token.character_id).order_by("pk")
                        .values_list("pk", flat=True)) != before["existing_token_ids"]:
                    raise RecoveryStop("token_inventory_changed_during_sync")
        result["phase"] = "complete"
        result["category"] = "recovered_existing_token"
        result["recovered"] = True
        result["sync_transaction_committed"] = True
        for step in result["steps"]:
            step["committed"] = True
    except Exception as error:
        result.update(exception_category(error))
        result.update(failure_sites(error))
        result["failure_phase"] = result["phase"]
        result["sync_transaction_committed"] = False
        # Attempts that completed before a failure were rolled back together.
        for step in result["steps"]:
            step["committed"] = False
    finally:
        if before:
            try:
                owner_character, token = selected(target)
                result["after"] = describe(owner_character, token)
                result["same_token_pk"] = token.pk == before["token_pk"]
                result["same_auth_link_pk"] = owner_character.character_ownership_id == before["auth_link_pk"]
                result["same_token_inventory"] = result["after"]["existing_token_ids"] == before["existing_token_ids"]
            except Exception as error:
                result["after_evidence_error_type"] = type(error).__name__
                result["recovered"] = False
                result["category"] = "preservation_evidence_incomplete"
        result["permanent_oauth_failure_proven"] = result["category"] in {
            "permanent_invalid_grant", "permanent_ownership_mismatch",
        }
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result



def outage_snapshot(targets):
    """Read current health for the bounded roster from the retained incident report."""
    if not targets or len(targets) > 16:
        raise RecoveryStop("outage_snapshot_exceeds_bound")
    rows = []
    for target in targets:
        row = {"owner_pk": target["owner_pk"],
               "owner_character_pk": target["owner_character_pk"],
               "character_id": target["character_id"], "token_pk": target["token_pk"]}
        try:
            owner_character, token = selected(target)
            row["state"] = describe(owner_character, token)
            row["same_auth_link_pk"] = owner_character.character_ownership_id == target["auth_link_pk"]
            row["same_owner_pk"] = owner_character.owner_id == target["owner_pk"]
            row["same_character_id"] = token.character_id == target["character_id"]
        except Exception as error:
            row.update(exception_category(error))
        rows.append(row)
    # Read only the current counts; no section flags are cleared or tasks enqueued.
    from memberaudit.models import CharacterUpdateStatus
    sticky = CharacterUpdateStatus.objects.filter(has_token_error=True)
    return {"read_only": True, "observed_at": stamp(now()), "owners": rows,
            "memberaudit_sticky_characters": sticky.values("character_id").distinct().count(),
            "memberaudit_sticky_sections": sticky.count()}


def emit(target, *, apply=False, snapshot_targets=None):
    result = run(target, apply=apply)
    if snapshot_targets is not None:
        try:
            result["outage_snapshot"] = outage_snapshot(snapshot_targets)
        except Exception as error:
            result["outage_snapshot_error_type"] = type(error).__name__
    print("BUH_STRUCTURES_RECOVERY_BEGIN")
    print(json.dumps(result, sort_keys=True))
    print("BUH_STRUCTURES_RECOVERY_END")
