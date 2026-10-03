"""Process-local existing-grant recovery; never installed as application code."""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import logging
import re
import signal
import time
from unittest.mock import patch

import requests
from allianceauth.authentication.models import CharacterOwnership
from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import QuerySet
from django.db.models.signals import pre_save
from django.utils.timezone import now
from esi.models import Token
from memberaudit.models import Character, CharacterUpdateStatus


class RecoveryStop(Exception):
    def __init__(self, category, status_code=None):
        self.category, self.status_code = category, status_code
        super().__init__(category)


def stamp(value):
    return value.isoformat() if value else None


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, cls=DjangoJSONEncoder, separators=(",", ":"),
    ).encode()).hexdigest()


def identity(token):
    return token.pk, token.user_id, token.character_id, token.character_owner_hash, token.token_type


def classify(error):
    names, codes, statuses, seen = [], set(), set(), set()
    current = error
    mismatch = False
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        name = type(current).__name__
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            names.append(name)
        code = getattr(current, "error", None)
        if code in {"invalid_grant", "invalid_client", "invalid_scope", "invalid_token",
                    "unauthorized_client", "temporarily_unavailable", "server_error"}:
            codes.add(code)
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and 100 <= status <= 599:
            statuses.add(status)
        if name == "InvalidTokenError" and getattr(current, "description", None) == "Ownership Changed! Revoke me!":
            mismatch = True
        current = current.__cause__ or current.__context__
    if isinstance(error, RecoveryStop):
        category = error.category
    elif "invalid_grant" in codes or "InvalidGrantError" in names:
        category = "permanent_invalid_grant"
    elif mismatch:
        category = "permanent_ownership_mismatch"
    elif "invalid_client" in codes or "unauthorized_client" in codes:
        category = "application_sso_configuration_failure"
    elif statuses & {420, 429}:
        category = "provider_throttling"
    elif any(s >= 500 for s in statuses):
        category = "provider_server_failure"
    elif set(names) & {"Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError", "IncompleteResponseError"} or codes & {"server_error", "temporarily_unavailable"}:
        category = "transient_failure"
    else:
        category = "other_specific_blocker"
    sites = []
    trace = error.__traceback__
    while trace is not None and len(sites) < 12:
        module = trace.tb_frame.f_globals.get("__name__", "")
        filename = trace.tb_frame.f_code.co_filename
        if filename in {globals().get("__file__"), "<buh-memberaudit-recovery>"}:
            component = "memberaudit_recovery"
        elif re.fullmatch(r"(?:memberaudit|esi)(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module):
            component = module
        else:
            component = None
        if component:
            sites.append({"component": component, "function": trace.tb_frame.f_code.co_name,
                          "line": trace.tb_lineno})
        trace = trace.tb_next
    return {"category": category, "exception_types": names,
            "oauth_codes": sorted(codes), "http_statuses": sorted(statuses),
            "failure_sites": sites}


def status_evidence(status):
    return {
        "status_pk": status.pk, "section": status.section,
        "has_token_error": status.has_token_error, "is_success": status.is_success,
        "run_started_at": stamp(status.run_started_at),
        "run_finished_at": stamp(status.run_finished_at),
        "update_started_at": stamp(status.update_started_at),
        "update_finished_at": stamp(status.update_finished_at),
        "content_hashes": [status.content_hash_1, status.content_hash_2, status.content_hash_3],
        "error_sha256": hashlib.sha256(status.error_message.encode()).hexdigest(),
    }


def inventory(target):
    return list(Token.objects.filter(character_id=target["character_id"]).order_by("pk")
                .values_list("pk", flat=True))


def link_matches(target):
    return CharacterOwnership.objects.filter(
        pk=target["auth_link_pk"], user_id=target["user_id"],
        character__character_id=target["character_id"],
    ).exists()


def load_member(target):
    if not link_matches(target):
        raise RecoveryStop("missing_auth_link")
    member = Character.objects.select_related("eve_character").get(pk=target["memberaudit_character_pk"])
    if member.eve_character.character_id != target["character_id"]:
        raise RecoveryStop("auth_character_identity_changed")
    if member.is_disabled or not member.user or member.user.pk != target["user_id"] or not member.user.is_active:
        raise RecoveryStop("memberaudit_character_or_user_disabled_or_changed")
    return member


def outage_status(member, selected, lock=False):
    query = CharacterUpdateStatus.objects
    if lock:
        query = query.select_for_update()
    try:
        status = query.get(pk=selected["status_pk"], character=member, section=selected["section"])
    except CharacterUpdateStatus.DoesNotExist:
        raise RecoveryStop("outage_status_record_missing") from None
    original = datetime.fromisoformat(selected["run_finished_at"])
    if (not status.has_token_error and status.is_success is True
            and status.run_finished_at and status.run_finished_at > original
            and status.run_started_at and status.run_finished_at >= status.run_started_at
            and status.content_hash_1):
        return status, "already_completed_after_outage"
    if (not status.has_token_error or status.is_success is not False
            or status.run_finished_at != original
            or hashlib.sha256(status.error_message.encode()).hexdigest() != selected["error_sha256"]):
        raise RecoveryStop("outage_status_changed_do_not_clear")
    return status, "matching_sticky_outage"


@contextmanager
def bounded_requests():
    original = requests.sessions.Session.request
    deadline = time.monotonic() + 360
    state = {"requests": 0, "status_counts": {}, "lowest_esi_error_budget": None}
    old_level, old_handler = logging.root.manager.disable, signal.getsignal(signal.SIGALRM)

    def alarm(signum, frame):
        raise RecoveryStop("operation_deadline_exceeded")

    def request(session, method, url, *args, **kwargs):
        remaining = deadline - time.monotonic()
        state["requests"] += 1
        if remaining <= 0 or state["requests"] > 256:
            raise RecoveryStop("operation_deadline_exceeded" if remaining <= 0 else "request_bound_exceeded")
        kwargs["timeout"] = min(15, remaining)
        response = original(session, method, url, *args, **kwargs)
        code = response.status_code
        state["status_counts"][str(code)] = state["status_counts"].get(str(code), 0) + 1
        remain = response.headers.get("X-Esi-Error-Limit-Remain")
        if remain is not None and str(remain).isdigit():
            remain = int(remain)
            state["lowest_esi_error_budget"] = min(remain, state["lowest_esi_error_budget"] or remain)
        category = (
            "provider_throttling" if code in {420, 429} else
            "provider_server_failure" if code >= 500 else
            "provider_error_budget_low" if remain is not None and str(remain).isdigit() and int(remain) <= 5 else None
        )
        if category:
            response.close()
            raise RecoveryStop(category, code)
        return response

    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(360)
    logging.disable(logging.CRITICAL)
    try:
        with patch.object(requests.sessions.Session, "request", request):
            yield state
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        logging.disable(old_level)


@contextmanager
def credential_guard(token, expected_identity):
    """Block credential/link insert/delete and foreign identity mutation, including SQL."""
    quoted = connection.ops.quote_name
    protected = {
        Token._meta.db_table, CharacterOwnership._meta.db_table,
        CharacterOwnership._meta.get_field("character").remote_field.model._meta.db_table,
        Token._meta.get_field("user").remote_field.model._meta.db_table,
        Token.scopes.through._meta.db_table,
    }
    token_table = Token._meta.db_table

    def before_save(sender, instance, **kwargs):
        if sender is Token and (identity(instance) != expected_identity or instance.pk != token.pk):
            raise RecoveryStop("credential_identity_mutation_blocked")
        if sender._meta.db_table in protected and sender is not Token:
            raise RecoveryStop("auth_identity_mutation_blocked")

    def sql_guard(execute, sql, params, many, context):
        command = sql.strip()
        mutation = re.match(r'(INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+([`"]?)([a-zA-Z0-9_]+)\2\b', command, re.I)
        # Quoted table endings have no word boundary: use an independent table check.
        if mutation is None:
            mutation = re.match(r'(INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+[`"]?([a-zA-Z0-9_]+)[`"]?', command, re.I)
            table = mutation.group(2) if mutation else None
        else:
            table = mutation.group(3)
        if table in protected:
            if not re.match(r"UPDATE\b", command, re.I) or table != token_table:
                raise RecoveryStop("credential_or_auth_sql_mutation_blocked")
            # The pinned token refresh saves exactly one pre-existing row.
            tail = re.search(r"\bWHERE\s+" + re.escape(quoted(token_table)) +
                             r"\." + re.escape(quoted("id")) + r"\s*=\s*%s\s*$", command, re.I)
            if many or not tail or not params or params[-1] != token.pk:
                raise RecoveryStop("unbounded_credential_update_blocked")
            assignments = command.split(" SET ", 1)[-1].rsplit(" WHERE ", 1)[0]
            fields = re.findall(r'[`"]([a-z_]+)[`"]\s*=\s*%s', assignments)
            if len(fields) != len(params) - 1:
                raise RecoveryStop("unrecognized_credential_update_shape")
            expected = dict(zip(
                ("user_id", "character_id", "character_owner_hash", "token_type"),
                expected_identity[1:],
            ))
            for column, value in zip(fields, params[:-1]):
                if column in expected and expected[column] != value:
                    raise RecoveryStop("credential_identity_sql_change_blocked")
        return execute(sql, params, many, context)

    pre_save.connect(before_save, weak=False)
    try:
        with connection.execute_wrapper(sql_guard):
            yield
    finally:
        pre_save.disconnect(before_save)


CURRENT_MODELS = {
    "CharacterContact", "CharacterContactLabel", "CharacterImplant", "CharacterJumpClone",
    "CharacterJumpCloneImplant", "CharacterRole", "CharacterPlanet", "CharacterSkill",
    "CharacterStanding", "CharacterSkillqueueEntry", "CharacterMailLabel",
}
HISTORY_MODELS = {"CharacterMail", "CharacterWalletJournalEntry", "CharacterWalletTransaction"}


def owner_lookup(model):
    if model.__name__ == "CharacterJumpCloneImplant":
        return "jump_clone__character_id"
    if model._meta.auto_created:
        parent = model._meta.auto_created
        if parent is Character:
            return next(f.name for f in model._meta.fields if f.is_relation and f.remote_field.model is Character) + "_id"
        if parent.__name__ not in CURRENT_MODELS | HISTORY_MODELS:
            return None
        for field in model._meta.fields:
            if field.is_relation and field.remote_field.model is parent:
                return field.name + "__character_id"
        return None
    if any(f.name == "character" for f in model._meta.fields):
        return "character_id"
    return None


@contextmanager
def preserve_history(member):
    """Native current snapshots may change; retained mails/wallet/ESI history may not be pruned."""
    original_delete = QuerySet.delete
    counts = {"retention_deletions_prevented": 0, "current_rows_replaced": 0}
    allowed_models = {
        m._meta.db_table: m for m in apps.get_models(include_auto_created=True)
        if m._meta.app_label == "memberaudit"
        and (m.__name__ in CURRENT_MODELS or (m._meta.auto_created
             and (m._meta.auto_created.__name__ in CURRENT_MODELS | HISTORY_MODELS
                  or m is Character.mailing_lists.through)))
    }

    def checked_delete(query):
        if query.model.__name__ in HISTORY_MODELS:
            lookup = owner_lookup(query.model)
            if not lookup or query.exclude(**{lookup: member.pk}).exists():
                raise RecoveryStop("unrelated_history_deletion_blocked")
            counts["retention_deletions_prevented"] += query.count()
            return 0, {}
        return original_delete(query)

    def sql_guard(execute, sql, params, many, context):
        command = sql.strip()
        if re.match(r"DELETE\b", command, re.I):
            match = re.fullmatch(r'DELETE\s+FROM\s+([`"]?)([a-zA-Z0-9_]+)\1\s+(WHERE\s+.+)', command, re.I | re.S)
            if not match or many or match.group(2) not in allowed_models:
                raise RecoveryStop("protected_record_deletion_blocked")
            model = allowed_models[match.group(2)]
            lookup = owner_lookup(model)
            if not lookup:
                raise RecoveryStop("unproven_current_snapshot_owner")
            # Validate the actual SQL predicate, including fast/M2M deletes, in the same transaction.
            with connection.cursor() as cursor:
                cursor.execute("SELECT " + connection.ops.quote_name(model._meta.pk.column)
                               + " FROM " + connection.ops.quote_name(model._meta.db_table)
                               + " " + match.group(3) + " LIMIT 50001", params)
                ids = [r[0] for r in cursor.fetchall()]
            if len(ids) > 50000 or model.objects.filter(pk__in=ids).exclude(**{lookup: member.pk}).exists():
                raise RecoveryStop("unrelated_current_snapshot_deletion_blocked")
            counts["current_rows_replaced"] += len(ids)
        return execute(sql, params, many, context)

    with patch.object(QuerySet, "delete", checked_delete), connection.execute_wrapper(sql_guard):
        yield counts


def candidates(target, member):
    required = set(member.esi_scopes())
    link = CharacterOwnership.objects.get(pk=target["auth_link_pk"])
    existing = list(Token.objects.filter(
        pk__in=target["token_ids"], user_id=target["user_id"], character_id=target["character_id"],
    ).order_by("pk"))
    if not existing:
        raise RecoveryStop("missing_token_record")
    matching = [t for t in existing if link.owner_hash and t.character_owner_hash == link.owner_hash]
    if not matching:
        raise RecoveryStop("stored_ownership_mismatch_unproven")
    # These are frozen, originally full-scope grants. Fresh signed claims, rather
    # than possibly stale scope-table metadata, decide a permanent scope rejection.
    usable = [t for t in matching if t.refresh_token]
    usable.sort(key=lambda t: (bool(required - set(t.scopes.values_list("name", flat=True))), t.pk))
    if not usable:
        raise RecoveryStop("missing_refresh_credential")
    return usable


def verify_claims(token, member):
    data = Token.get_token_data(token.access_token)
    if not isinstance(data, dict):
        raise RecoveryStop("signed_claims_incomplete")
    subject = data.get("sub")
    value = data.get("character_id")
    if subject is not None:
        match = re.fullmatch(r"CHARACTER:EVE:([1-9][0-9]*)", str(subject))
        if not match:
            raise RecoveryStop("signed_subject_incomplete")
        value = int(match.group(1))
    if value != token.character_id or data.get("owner") != token.character_owner_hash:
        raise RecoveryStop("permanent_ownership_mismatch")
    scopes = data.get("scopes", data.get("scp"))
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, (list, tuple, set)):
        raise RecoveryStop("signed_scopes_incomplete")
    if set(member.esi_scopes()) - set(scopes):
        raise RecoveryStop("missing_required_scopes")


def refresh_existing(target, member, outcomes):
    eligible = candidates(target, member)
    for token in eligible:
        outcome = {"token_pk": token.pk, "refresh_attempted": True, "refresh_succeeded": False}
        outcomes.append(outcome)
        expected = identity(token)
        try:
            # Serialize against concurrent normal refresh. Rotated grant commits separately from sections.
            with transaction.atomic(), credential_guard(token, expected):
                token = Token.objects.select_for_update().get(pk=token.pk)
                if identity(token) != expected:
                    raise RecoveryStop("credential_identity_changed_before_refresh")
                before = token.created
                token.refresh()
                token.refresh_from_db()
                if identity(token) != expected or token.created <= before:
                    raise RecoveryStop("fresh_refresh_not_proven")
            # Never roll back a rotated refresh credential when later validation/update fails.
            outcome.update({"refresh_succeeded": True, "refreshed_at": stamp(token.created)})
            verify_claims(token, member)
            outcome["signed_identity_and_scopes_verified"] = True
            missing = sorted(set(member.esi_scopes()) - set(token.scopes.values_list("name", flat=True)))
            if missing:
                outcome["missing_stored_scope_metadata"] = missing
                raise RecoveryStop("stored_scope_metadata_inconsistent")
            outcome.update({"refresh_succeeded": True, "signed_identity_and_scopes_verified": True,
                            "refreshed_at": stamp(token.created), "category": "valid_existing_grant"})
            return token
        except Exception as error:
            outcome.update(classify(error))
            if outcome["category"] not in {
                "permanent_invalid_grant", "permanent_ownership_mismatch", "missing_required_scopes",
            }:
                raise
    categories = {o["category"] for o in outcomes}
    category = ("missing_required_scopes" if categories == {"missing_required_scopes"}
                else "permanent_invalid_grant" if "permanent_invalid_grant" in categories
                else "permanent_ownership_mismatch")
    raise RecoveryStop(category)


# Native related managers used to independently reread the retained section rows.
RELATIONS = {
    "location": ["location"], "online_status": ["online_status"], "ship": ["ship"],
    "skill_queue": ["skillqueue"], "contacts": ["contacts", "contact_labels"],
    "mails": ["mails", "mail_labels", "mailing_lists"], "implants": ["implants"],
    "jump_clones": ["jump_clones", "clone_info"], "planets": ["planets"],
    "roles": ["roles"], "skills": ["skills", "skillpoints"], "standings": ["standings"],
    "mining_ledger": ["mining_ledger"], "wallet_balance": ["wallet_balance"],
    "wallet_transactions": ["wallet_transactions"],
}


def retained_rows(member, section):
    rows = {}
    for relation in RELATIONS[section]:
        field = member._meta.get_field(relation)
        model = field.related_model
        if field.many_to_many:
            query = getattr(member, relation).all()
        else:
            query = model.objects.filter(**{field.field.name: member.pk})
        values = list(query.order_by("pk").values()[:50001])
        if len(values) > 50000:
            raise RecoveryStop("section_row_bound_exceeded")
        rows[relation] = {"count": len(values), "sha256": digest(values)}
    return rows


def verify_primary_payload(member, section, data):
    """Match the four pilot payloads with actual native row fields, including empty queue."""
    from memberaudit import models
    if section == "location":
        row = models.CharacterLocation.objects.get(character=member)
        if (row.eve_solar_system_id != data["solar_system_id"]
                or row.location_id != (data.get("station_id") or data.get("structure_id") or data["solar_system_id"])):
            raise RecoveryStop("location_payload_row_mismatch")
    elif section == "online_status":
        row = models.CharacterOnlineStatus.objects.get(character=member)
        actual = {"logins": row.logins, "last_login": row.last_login, "last_logout": row.last_logout}
        expected = {key: data.get(key) for key in actual}
        if digest(actual) != digest(expected):
            raise RecoveryStop("online_payload_row_mismatch")
    elif section == "ship":
        row = models.CharacterShip.objects.get(character=member)
        if (row.item_id != data["ship_item_id"] or row.eve_type_id != data["ship_type_id"]
                or row.name != data["ship_name"]):
            raise RecoveryStop("ship_payload_row_mismatch")
    elif section == "skill_queue":
        rows = list(models.CharacterSkillqueueEntry.objects.filter(character=member).order_by("queue_position"))
        expected = sorted(data, key=lambda r: r["queue_position"])
        if len(rows) != len(expected):
            raise RecoveryStop("queue_payload_row_count_mismatch")
        for row, incoming in zip(rows, expected):
            for model_field, payload_field in (
                ("queue_position", "queue_position"), ("eve_type_id", "skill_id"),
                ("finished_level", "finished_level"), ("finish_date", "finish_date"),
                ("start_date", "start_date"), ("training_start_sp", "training_start_sp"),
                ("level_end_sp", "level_end_sp"), ("level_start_sp", "level_start_sp"),
            ):
                if digest(getattr(row, model_field)) != digest(incoming.get(payload_field)):
                    raise RecoveryStop("queue_payload_row_mismatch")
    elif section == "implants":
        if sorted(member.implants.values_list("eve_type_id", flat=True)) != sorted(data):
            raise RecoveryStop("implant_payload_rows_mismatch")
    elif section == "contacts":
        actual = {r.eve_entity_id: r for r in member.contacts.all()}
        if set(actual) != {r["contact_id"] for r in data}:
            raise RecoveryStop("contacts_payload_rows_mismatch")
        for incoming in data:
            row = actual[incoming["contact_id"]]
            if row.standing != incoming["standing"]:
                raise RecoveryStop("contact_standing_not_retained")
    elif section == "standings":
        actual = dict(member.standings.values_list("eve_entity_id", "standing"))
        expected = {r["from_id"]: r["standing"] for r in data}
        if actual != expected:
            raise RecoveryStop("standing_payload_rows_mismatch")
    elif section == "skills":
        actual = {r.eve_type_id: r for r in member.skills.all()}
        expected = {r["skill_id"]: r for r in data.get("skills", [])}
        if set(actual) != set(expected):
            raise RecoveryStop("skills_payload_rows_mismatch")
        for key, incoming in expected.items():
            for field in ("active_skill_level", "trained_skill_level", "skillpoints_in_skill"):
                if getattr(actual[key], field) != incoming[field]:
                    raise RecoveryStop("skill_payload_fields_mismatch")
        points = models.CharacterSkillpoints.objects.get(character=member)
        if points.total != data.get("total_sp") or points.unallocated != data.get("unallocated_sp"):
            raise RecoveryStop("skillpoints_payload_mismatch")
    elif section == "planets":
        if set(member.planets.values_list("eve_planet_id", flat=True)) != {r["planet_id"] for r in data}:
            raise RecoveryStop("planets_payload_rows_mismatch")
    elif section == "jump_clones":
        if set(member.jump_clones.values_list("jump_clone_id", flat=True)) != {
            r["jump_clone_id"] for r in data.get("jump_clones", [])
        }:
            raise RecoveryStop("jump_clone_payload_rows_mismatch")
        if not models.CharacterCloneInfo.objects.filter(character=member).exists():
            raise RecoveryStop("clone_info_missing")
    elif section == "mining_ledger":
        for incoming in data:
            if not member.mining_ledger.filter(
                date=incoming["date"], eve_solar_system_id=incoming["solar_system_id"],
                eve_type_id=incoming["type_id"], quantity=incoming["quantity"],
            ).exists():
                raise RecoveryStop("mining_payload_row_missing")
    elif section == "wallet_transactions":
        if not {r["transaction_id"] for r in data}.issubset(
            set(member.wallet_transactions.values_list("transaction_id", flat=True))
        ):
            raise RecoveryStop("wallet_transaction_payload_rows_missing")
    elif section == "mails":
        if not set(data).issubset(set(member.mails.values_list("mail_id", flat=True))):
            raise RecoveryStop("mail_header_payload_rows_missing")
    elif section == "wallet_balance":
        if models.CharacterWalletBalance.objects.get(character=member).total != Decimal(str(data)):
            raise RecoveryStop("wallet_payload_row_mismatch")


def update_one(member, selected, token):
    section = Character.UpdateSection(selected["section"])
    observations, payloads = [], {}
    original_if_changed = member.update_section_if_changed
    original_fetch = member.fetch_token
    original_hash = member.update_section_content_hash

    def checked_token(scopes=None):
        required = set(scopes.split() if isinstance(scopes, str) else scopes or member.esi_scopes())
        if required - set(token.scopes.values_list("name", flat=True)):
            raise RecoveryStop("missing_required_scopes")
        return token

    def checked_if_changed(section, fetch_func, store_func, force_update=False, hash_num=1):
        def fetch(**kwargs):
            # Raise before upstream's special HTTP-500 "success" fallback can hide a failure.
            try:
                value = fetch_func(**kwargs)
            except Exception as error:
                outcome = classify(error)
                if outcome["category"] == "provider_server_failure":
                    raise RecoveryStop("provider_server_failure", outcome["http_statuses"][0]) from error
                raise
            encoded = json.dumps(value, cls=DjangoJSONEncoder).encode()
            if len(encoded) > 8 * 1024 * 1024:
                raise RecoveryStop("section_payload_bound_exceeded")
            payloads[hash_num] = value
            observations.append({"hash_num": hash_num, "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                                 "payload_bytes": len(encoded)})
            return value
        result = original_if_changed(section, fetch, store_func, force_update=True, hash_num=hash_num)
        if result.is_changed is None or not result.is_updated:
            raise RecoveryStop("native_update_not_proven")
        current = CharacterUpdateStatus.objects.get(pk=selected["status_pk"])
        value = payloads[hash_num]
        if getattr(current, "content_hash_" + str(hash_num)) != current._calculate_hash(value):
            raise RecoveryStop("native_payload_hash_not_persisted")
        return result

    def checked_hash(section, content, hash_num=1):
        encoded = json.dumps(content, cls=DjangoJSONEncoder).encode()
        if len(encoded) > 8 * 1024 * 1024:
            raise RecoveryStop("section_payload_bound_exceeded")
        payloads[hash_num] = content
        if not any(o["hash_num"] == hash_num for o in observations):
            observations.append({"hash_num": hash_num, "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                                 "payload_bytes": len(encoded)})
        original_hash(section=section, content=content, hash_num=hash_num)
        current = CharacterUpdateStatus.objects.get(pk=selected["status_pk"])
        if getattr(current, "content_hash_" + str(hash_num)) != current._calculate_hash(content):
            raise RecoveryStop("native_payload_hash_not_persisted")

    member.fetch_token, member.update_section_if_changed = checked_token, checked_if_changed
    member.update_section_content_hash = checked_hash
    try:
        before_rows = retained_rows(member, section)
        with transaction.atomic(), preserve_history(member) as history:
            Character.objects.select_for_update().get(pk=member.pk)
            status, state = outage_status(member, selected, lock=True)
            # A later green status can come from the native HTTP-500 fallback.
            # Revalidate the selected payload; never accept status/hash metadata alone.
            if section not in Character.UpdateSection.enabled_sections():
                raise RecoveryStop("outage_section_no_longer_enabled")
            started = now()
            member.reset_update_section(section)
            methods = {
                "contacts": ("update_contact_labels", "update_contacts"),
                "mails": ("update_mailing_lists", "update_mail_labels", "update_mail_headers"),
            }.get(section, ("update_" + section,))
            for method in methods:
                member.perform_update_with_error_logging(
                    section, getattr(member, method), force_update=True,
                )
            # Finish the mail chain synchronously without spawning uncontrolled body tasks.
            bodies = 0
            if section == "mails":
                pending = list(member.mails.filter(mail_id__in=payloads[1], body="").order_by("pk")[:129])
                if len(pending) > 128:
                    raise RecoveryStop("mail_body_bound_exceeded")
                for mail in pending:
                    body = member.mails._fetch_mail_body_from_esi(member, mail)
                    if not isinstance(body, str) or len(body.encode()) > 1024 * 1024:
                        raise RecoveryStop("mail_body_payload_invalid_or_oversized")
                    member.mails.filter(pk=mail.pk).update(body=body)
                    mail.refresh_from_db()
                    if mail.body != body:
                        raise RecoveryStop("mail_body_not_retained")
                    bodies += 1
            if not observations or 1 not in payloads:
                raise RecoveryStop("fresh_section_payload_not_proven")
            verify_primary_payload(member, section, payloads[1])
            member.update_section_log_result(section, is_success=True, is_updated=True)
            status.refresh_from_db()
            if (status.has_token_error or status.is_success is not True or status.error_message
                    or not status.run_finished_at or not status.update_finished_at
                    or status.run_started_at < started or status.run_finished_at < status.run_started_at
                    or status.update_finished_at < started):
                raise RecoveryStop("section_success_timestamps_not_persisted")
            after_rows = retained_rows(member, section)
            step = {"section": section, "verified": True, "category": "recovered_existing_token",
                    "status": status_evidence(status), "payloads": observations,
                    "before_rows": before_rows, "retained_rows": after_rows,
                    "mail_bodies_retained": bodies, "previous_state": state, **history}
        # Independently reread AFTER the section transaction has committed.
        persisted = CharacterUpdateStatus.objects.get(pk=selected["status_pk"])
        if status_evidence(persisted) != step["status"] or retained_rows(member, section) != after_rows:
            raise RecoveryStop("postcommit_section_evidence_changed")
        step["transaction_committed"] = True
        return step
    finally:
        member.fetch_token, member.update_section_if_changed = original_fetch, original_if_changed
        member.update_section_content_hash = original_hash


def run(target, *, apply=False):
    result = {
        "schema_version": 1, "read_only": not apply,
        "memberaudit_character_pk": target["memberaudit_character_pk"],
        "character_id": target["character_id"], "character_name": target["character_name"],
        "user_id": target["user_id"], "auth_link_pk": target["auth_link_pk"],
        "started_at": stamp(now()), "category": "not_attempted", "phase": "preflight",
        "recovered": False, "requires_reauthorization": False, "sections": [], "token_attempts": [],
    }
    before = inventory(target)
    result["before_token_inventory"] = before
    ownership_before = CharacterOwnership.objects.filter(pk=target["auth_link_pk"]).values(
        "pk", "user_id", "character_id", "owner_hash",
    ).first()
    result["before_auth_identity_sha256"] = digest(ownership_before)
    result["record_presence"] = {
        "auth_link_exists": link_matches(target),
        "matching_baseline_token_exists": Token.objects.filter(
            pk__in=target["token_ids"], user_id=target["user_id"], character_id=target["character_id"],
        ).exists(),
        "memberaudit_character_exists": Character.objects.filter(pk=target["memberaudit_character_pk"]).exists(),
    }
    try:
        if not result["record_presence"]["matching_baseline_token_exists"]:
            raise RecoveryStop("missing_token_record")
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise RecoveryStop("pending_migrations_for_current_runtime")
        member = load_member(target)
        result["before_sections"] = [
            status_evidence(outage_status(member, s)[0]) for s in target["sections"]
        ]
        result["baseline_existing_token_pks"] = target["token_ids"]
        if not apply:
            candidates(target, member)
            result["category"] = "eligible_existing_token_not_yet_validated"
            return result
        with bounded_requests() as network:
            result["phase"] = "refresh_existing_token"
            token = refresh_existing(target, member, result["token_attempts"])
            result["validated_token_pk"] = token.pk
            result["phase"] = "native_memberaudit_sections"
            with credential_guard(token, identity(token)):
                for selected in target["sections"]:
                    result["phase"] = "update_" + selected["section"]
                    result["sections"].append(update_one(member, selected, token))
            result["network"] = network
        member.reset_token_error_notified_if_status_ok()
        member.clear_cache()
        result["category"] = "recovered_existing_token"
        result["recovered"] = all(s["verified"] for s in result["sections"]) and len(result["sections"]) == len(target["sections"])
        result["phase"] = "complete"
    except Exception as error:
        result.update(classify(error))
        result["failure_phase"] = result["phase"]
        if "network" in locals():
            result["network"] = network
        # No missing link or arbitrary 403 is proof that a stored credential is revoked.
        result["requires_reauthorization"] = (
            bool(result["token_attempts"])
            and result["phase"] == "refresh_existing_token"
            and result["category"] in {"permanent_invalid_grant", "permanent_ownership_mismatch", "missing_required_scopes"}
        )
    finally:
        result["after_token_inventory"] = inventory(target)
        result["same_token_inventory"] = result["after_token_inventory"] == before
        ownership_after = CharacterOwnership.objects.filter(pk=target["auth_link_pk"]).values(
            "pk", "user_id", "character_id", "owner_hash",
        ).first()
        result["after_auth_identity_sha256"] = digest(ownership_after)
        result["same_auth_link"] = link_matches(target)
        result["same_auth_owner_identity"] = ownership_before == ownership_after
        result["finished_at"] = stamp(now())
        if (not result["same_token_inventory"] or not result["same_auth_link"]
                or not result["same_auth_owner_identity"]):
            result["recovered"] = False
            result["preservation_failure"] = True
    return result


def excluded_snapshot(excluded):
    rows = []
    for selected in excluded:
        status = CharacterUpdateStatus.objects.get(pk=selected["status_pk"],
                                                  character_id=selected["memberaudit_character_pk"])
        rows.append(status_evidence(status))
    return {"sections": len(rows), "characters": len({t["memberaudit_character_pk"] for t in excluded}),
            "states_sha256": digest(rows)}


def emit(target, apply=False, excluded=None):
    result = run(target, apply=apply)
    if excluded is not None:
        result["excluded_snapshot"] = excluded_snapshot(excluded)
    print("BUH_MEMBERAUDIT_BEGIN")
    print(json.dumps(result, sort_keys=True))
    print("BUH_MEMBERAUDIT_END")
