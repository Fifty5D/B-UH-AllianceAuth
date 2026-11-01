#!/usr/bin/env python3
"""Host-side, private-state recovery of explicitly allowed Structures token-disabled owners.

Run on the Docker host as root. Never print the private snapshot: it includes
token material needed to preserve pre-refresh state. No installed wheel changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

DEFAULT_OWNERS_FILE = Path("/var/lib/buh-auth-recovery/structures/allowed-owners.json")
TOKEN_DISABLED_REASON = "No valid token found for character"
RESULT_MARKER = "BUH_STRUCTURES_RESULT="
BOOTSTRAP = "import sys; n=int(sys.stdin.readline()); s=sys.stdin.read(n); exec(compile(s, 'structures_recovery.py', 'exec'))"


def validate_owner_ids(value: object) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(item) is not int or item <= 0 for item in value)
    ):
        raise ValueError("Owner IDs must be a nonempty list of positive integers")
    return sorted(set(value))


def load_owner_ids(path: Path) -> list[int]:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ValueError("Owner allowlist must be a root-owned private regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"owner_ids"}:
        raise ValueError("Owner allowlist must contain only owner_ids")
    return validate_owner_ids(data["owner_ids"])


def eligible_character(character: dict, allowed_owner_ids: list[int]) -> bool:
    """Only recover the precise token-disable condition for explicitly allowed owners."""
    return (
        character["owner_id"] in allowed_owner_ids
        and not character["enabled"]
        and character["disabled_reason"] == TOKEN_DISABLED_REASON
        and character["membership_ok"]
        and character["permission_ok"]
        and character["scope_complete_tokens"] > 0
    )


def within_retry_budget(
    record: dict, now: dt.datetime, cooldown_hours: int, max_attempts: int
) -> bool:
    if record.get("attempts", 0) >= max_attempts:
        return False
    if not record.get("last_attempt"):
        return True
    previous = dt.datetime.fromisoformat(record["last_attempt"])
    return (now - previous).total_seconds() >= cooldown_hours * 3600


def authorization_budget(record: dict, character: dict) -> dict:
    """Only a genuinely new matching authorization grants a new retry budget."""
    if any(
        character.get(field) is False
        for field in ("membership_ok", "permission_ok", "owner_active")
    ) or not character.get("matching_token_ids"):
        return dict(record)
    fingerprint = character["authorization_fingerprint"]
    if record.get("authorization_fingerprint", fingerprint) != fingerprint:
        return dict(record)
    result = dict(record)
    old_ids = set(record.get("matching_token_ids", []))
    current_ids = set(character["matching_token_ids"])
    if record.get("authorization_fingerprint") and current_ids - old_ids:
        result.update(attempts=0, last_attempt=None)
    result["authorization_fingerprint"] = fingerprint
    # Retain removed IDs: removal and later reappearance are not new consent.
    result["matching_token_ids"] = sorted(old_ids | current_ids)
    return result


@contextmanager
def state_lock(state_dir: Path):
    """Serialize inspection, reservation, mutation, dispatch and rollback."""
    import fcntl

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = state_dir.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise ValueError("Recovery state must be an owned private directory")
    os.chmod(state_dir, 0o700)
    descriptor = os.open(
        state_dir / ".recovery.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid():
            raise ValueError("Recovery lock must be an owned regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".recovery-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def container_call(container: str, action: str, payload: dict) -> dict:
    source = Path(__file__).read_text(encoding="utf-8")
    request = f"{len(source)}\n{source}" + json.dumps(payload)
    result = subprocess.run(
        ["docker", "exec", "-i", container, "python", "-c", BOOTSTRAP, "--inside", action],
        input=request.encode("utf-8"),
        capture_output=True,
        timeout=240,
    )
    # Never relay raw output: snapshots contain private token fields.
    lines = [
        line[len(RESULT_MARKER) :]
        for line in result.stdout.decode("utf-8").splitlines()
        if line.startswith(RESULT_MARKER)
    ]
    if result.returncode or len(lines) != 1:
        raise RuntimeError(
            f"Container recovery step {action!r} failed (exit {result.returncode}); private output suppressed"
        )
    return json.loads(lines[0])


def django_context():
    import logging

    logging.disable(logging.CRITICAL)
    os.environ["DJANGO_SETTINGS_MODULE"] = "myauth.settings.local"
    import django

    django.setup()
    from structures.models import Owner, OwnerCharacter
    from esi.models import Token

    return Owner, OwnerCharacter, Token


def ownership_authorization(ownership) -> tuple:
    """Immutable identity that was authorized before contacting ESI."""
    return (
        ownership.pk,
        ownership.user_id,
        ownership.owner_hash,
        ownership.character_id,
        ownership.character.character_id,
        ownership.character.corporation_id,
    )


def authorization_fingerprint(ownership) -> str:
    return hashlib.sha256(
        json.dumps(ownership_authorization(ownership)).encode("utf-8")
    ).hexdigest()


def token_authorization(token) -> tuple:
    return (token.user_id, token.character_owner_hash, token.character_id)


def inspect_characters(Owner, Token) -> list[dict]:
    output = []
    for owner in Owner.objects.select_related("corporation").order_by("pk"):
        for character in owner.characters.select_related(
            "character_ownership__character", "character_ownership__user"
        ):
            ownership = character.character_ownership
            tokens = Token.objects.filter(
                user=ownership.user,
                character_id=ownership.character.character_id,
                character_owner_hash=ownership.owner_hash,
            ).require_scopes(Owner.esi_scopes())
            output.append(
                {
                    "owner_id": owner.pk,
                    "owner_name": owner.corporation.corporation_name,
                    "character_pk": character.pk,
                    "ownership_pk": ownership.pk,
                    "character_name": ownership.character.character_name,
                    "enabled": character.is_enabled,
                    "disabled_reason": character.disabled_reason,
                    "membership_ok": ownership.character.corporation_id
                    == owner.corporation.corporation_id,
                    "permission_ok": ownership.user.has_perm(
                        "structures.add_structure_owner"
                    ),
                    "scope_complete_tokens": tokens.count(),
                    "matching_token_ids": list(
                        tokens.order_by("pk").values_list("pk", flat=True)
                    ),
                    "authorization_fingerprint": authorization_fingerprint(ownership),
                    "cached_unexpired_tokens": tokens.count()
                    - tokens.get_expired().count(),
                    "owner_active": owner.is_active,
                    "owner_up": owner.is_up,
                    "alliance_main": owner.is_alliance_main,
                    "structures_updated": str(owner.structures_last_update_at),
                    "notifications_updated": str(owner.notifications_last_update_at),
                    "assets_updated": str(owner.assets_last_update_at),
                    "forwarding_updated": str(owner.forwarding_last_update_at),
                }
            )
    return output


def dispatch_inside(request: dict, Owner, OwnerCharacter, Token) -> dict:
    """Submit one pending task only while the restored authorization still matches."""
    from structures import tasks
    from django.db import transaction
    from allianceauth.authentication.models import CharacterOwnership

    task = {
        "structures": tasks.update_structures_for_owner,
        "notifications": tasks.process_notifications_for_owner,
    }[request["task"]]
    pending = request["pending"]
    with transaction.atomic():
        owner = (
            Owner.objects.select_for_update()
            .select_related("corporation")
            .get(pk=request["owner_id"])
        )
        character = OwnerCharacter.objects.select_for_update().get(
            pk=pending["character_pk"], owner=owner
        )
        ownership = (
            CharacterOwnership.objects.select_for_update()
            .select_related("character", "user")
            .get(pk=character.character_ownership_id)
        )
        if (
            not owner.is_active
            or not character.is_enabled
            or character.disabled_reason
            or authorization_fingerprint(ownership) != pending["authorization_fingerprint"]
            or ownership.character.corporation_id != owner.corporation.corporation_id
            or not ownership.user.has_perm("structures.add_structure_owner")
            or not Token.objects.filter(
                user_id=ownership.user_id,
                character_id=ownership.character.character_id,
                character_owner_hash=ownership.owner_hash,
            )
            .require_scopes(Owner.esi_scopes())
            .exists()
        ):
            return {
                "result": "not_submitted",
                "reason": "Current authorization does not match pending recovery",
            }
        # A task ID is a correlation identifier, not broker deduplication.
        # Any failure after this invocation starts is uncertain to the caller.
        result = task.apply_async(args=[owner.pk], task_id=request["task_id"], retry=False)
        if result.id != request["task_id"]:
            raise RuntimeError("Broker acknowledgement did not identify the pending task")
        return {"result": "accepted", "task_id": result.id}


def inside(action: str, request: dict) -> dict:
    Owner, OwnerCharacter, Token = django_context()
    if action == "inspect":
        return {"characters": inspect_characters(Owner, Token)}
    if action == "dispatch":
        return dispatch_inside(request, Owner, OwnerCharacter, Token)
    if action == "snapshot":
        from django.core import serializers

        pairs = OwnerCharacter.objects.values_list(
            "character_ownership__user_id", "character_ownership__character__character_id"
        )
        from django.db.models import Q

        selection = Q(pk__in=[])
        for user_id, character_id in pairs:
            selection |= Q(user_id=user_id, character_id=character_id)
        return {
            "created": dt.datetime.now(dt.timezone.utc).isoformat(),
            "owner_rows": json.loads(serializers.serialize("json", Owner.objects.all())),
            "character_rows": json.loads(
                serializers.serialize("json", OwnerCharacter.objects.all())
            ),
            "token_rows": json.loads(
                serializers.serialize("json", Token.objects.filter(selection))
            ),
            "summary": inspect_characters(Owner, Token),
        }
    if action == "rollback_flags":
        from django.db import transaction

        restored = []
        with transaction.atomic():
            for row in request["snapshot"]["character_rows"]:
                if row["fields"]["owner"] in request["owner_ids"]:
                    fields = row["fields"]
                    OwnerCharacter.objects.filter(pk=row["pk"]).update(
                        is_enabled=fields["is_enabled"],
                        disabled_reason=fields["disabled_reason"],
                        error_count=fields["error_count"],
                    )
                    restored.append(row["pk"])
            for row in request["snapshot"]["owner_rows"]:
                if row["pk"] in request["owner_ids"]:
                    Owner.objects.filter(pk=row["pk"]).update(
                        is_alliance_main=row["fields"]["is_alliance_main"]
                    )
        return {"restored_character_flags": restored, "tokens_restored": False}
    if action != "apply":
        raise ValueError("Unsupported inner action")
    from structures import providers, app_settings
    from django.db import transaction
    from allianceauth.authentication.models import CharacterOwnership

    outcomes = []
    for owner_id in request["owner_ids"]:
        owner = Owner.objects.get(pk=owner_id)
        rows = [
            row for row in inspect_characters(Owner, Token) if row["owner_id"] == owner_id
        ]
        candidates = [
            row
            for row in rows
            if eligible_character(row, request["owner_ids"]) and row["owner_active"]
        ]
        if len(candidates) != 1:
            outcomes.append(
                {
                    "owner_id": owner_id,
                    "result": "skipped",
                    "reason": "Expected exactly one eligible allowed token-disabled character",
                }
            )
            continue
        row = candidates[0]
        character = OwnerCharacter.objects.select_related(
            "character_ownership__character", "character_ownership__user"
        ).get(pk=row["character_pk"])
        ownership = character.character_ownership
        validated_ownership = ownership_authorization(ownership)
        validated_corporation_id = owner.corporation.corporation_id
        expected_token_authorization = (
            validated_ownership[1],
            validated_ownership[2],
            validated_ownership[4],
        )
        try:
            # This is the upstream validity/refresh API. It may delete unusable
            # expired token rows; the host snapshot was saved before this step.
            token = (
                Token.objects.filter(
                    user=ownership.user,
                    character_id=ownership.character.character_id,
                    character_owner_hash=ownership.owner_hash,
                )
                .require_scopes(Owner.esi_scopes())
                .require_valid()
                .first()
            )
            if token is None:
                outcomes.append({"owner_id": owner_id, "result": "needs_reauthorization"})
                continue
            if token_authorization(token) != expected_token_authorization:
                outcomes.append({"owner_id": owner_id, "result": "authorization_changed"})
                continue
            validated_token_pk = token.pk
            # Director/CEO check is a read-only ESI request and stores no data.
            # A 403 is reported and leaves the disabled character unchanged.
            if app_settings.STRUCTURES_FEATURE_STARBASES:
                providers.esi.client.Corporation.GetCorporationsCorporationIdStarbases(
                    corporation_id=owner.corporation.corporation_id, token=token
                ).results(use_etag=False)
            else:
                providers.esi.client.Corporation.GetCorporationsCorporationIdStructures(
                    corporation_id=owner.corporation.corporation_id, token=token
                ).results(use_etag=False)
            with transaction.atomic():
                owner = (
                    Owner.objects.select_for_update()
                    .select_related("corporation")
                    .get(pk=owner_id)
                )
                locked = OwnerCharacter.objects.select_for_update().get(pk=character.pk)
                if locked.is_enabled or locked.disabled_reason != TOKEN_DISABLED_REASON:
                    outcomes.append(
                        {"owner_id": owner_id, "result": "changed_since_inspection"}
                    )
                    continue
                # Re-check ownership and permission immediately before enabling.
                if locked.character_ownership_id != validated_ownership[0]:
                    outcomes.append({"owner_id": owner_id, "result": "ownership_changed"})
                    continue
                # Lock and reload the actual ownership row. A user or owner hash
                # may change in place while the network probe is outstanding.
                ownership = (
                    CharacterOwnership.objects.select_for_update()
                    .select_related("character", "user")
                    .get(pk=locked.character_ownership_id)
                )
                if (
                    ownership_authorization(ownership) != validated_ownership
                    or not owner.is_active
                    or owner.corporation.corporation_id != validated_corporation_id
                    or ownership.character.corporation_id
                    != owner.corporation.corporation_id
                    or not ownership.user.has_perm("structures.add_structure_owner")
                ):
                    outcomes.append(
                        {"owner_id": owner_id, "result": "authorization_changed"}
                    )
                    continue
                # Validate the same token row under lock without refreshing in
                # this transaction. Deleted, transferred or scope-revoked tokens
                # must not enable a character using an earlier successful probe.
                locked_token = (
                    Token.objects.select_for_update()
                    .filter(
                        pk=validated_token_pk,
                        user_id=ownership.user_id,
                        character_id=ownership.character.character_id,
                        character_owner_hash=ownership.owner_hash,
                    )
                    .require_scopes(Owner.esi_scopes())
                    .first()
                )
                if (
                    locked_token is None
                    or token_authorization(locked_token) != expected_token_authorization
                ):
                    outcomes.append(
                        {"owner_id": owner_id, "result": "authorization_changed"}
                    )
                    continue
                owner.add_character(ownership)
                if request.get("alliance_main_owner_id") == owner_id:
                    if (
                        Owner.objects.filter(is_active=True, is_alliance_main=True)
                        .exclude(pk=owner_id)
                        .exists()
                    ):
                        raise RuntimeError("Existing alliance-main owner requires review")
                    owner.is_alliance_main = True
                    owner.save(update_fields=["is_alliance_main"])
            outcomes.append(
                {
                    "owner_id": owner_id,
                    "result": "restored",
                    "character_pk": character.pk,
                    "task_ids": [],
                }
            )
        except Exception as exception:
            status = getattr(exception, "status_code", None)
            outcomes.append(
                {
                    "owner_id": owner_id,
                    "result": "role_denied" if status == 403 else "error",
                    "exception_type": type(exception).__name__,
                    "http_status": status,
                }
            )
    return {"outcomes": outcomes, "characters": inspect_characters(Owner, Token)}


def prepare_dispatch(character: dict) -> dict:
    return {
        "recovery_id": uuid.uuid4().hex,
        "character_pk": character["character_pk"],
        "authorization_fingerprint": character["authorization_fingerprint"],
        "enablement": "prepared",
        "enablement_attempt_id": uuid.uuid4().hex,
        "tasks": {
            name: {"task_id": uuid.uuid4().hex, "state": "prepared", "attempts": 0}
            for name in ("structures", "notifications")
        },
    }


def current_pending_character(pending: dict, characters: list[dict], owner_id: int):
    return next(
        (
            row
            for row in characters
            if row["owner_id"] == owner_id
            and row["character_pk"] == pending["character_pk"]
            and row["authorization_fingerprint"] == pending["authorization_fingerprint"]
            and row["owner_active"]
            and row["membership_ok"]
            and row["permission_ok"]
            and row["scope_complete_tokens"] > 0
        ),
        None,
    )


def dispatch_pending(
    args, pending: dict, characters: list[dict], owner_ids: list[int], now: dt.datetime
) -> list[dict]:
    """Persist each acknowledgement; ambiguous publication is never auto-replayed."""
    path = args.state_dir / "pending-dispatch.json"
    outcomes = []
    for owner_id in owner_ids:
        record = pending.get(str(owner_id))
        if not record:
            continue
        for task in record["tasks"].values():
            if task["state"] == "publishing":
                task["state"] = "uncertain"
                private_write(path, pending)
        character = current_pending_character(record, characters, owner_id)
        if record["enablement"] == "prepared" and character and character["enabled"]:
            # Commit may have succeeded before the enabling call's response was
            # retained. An intent alone is not proof that this recovery enabled it.
            record["enablement"] = "uncertain"
            private_write(path, pending)
        if record["enablement"] != "confirmed":
            outcomes.append(
                {
                    "owner_id": owner_id,
                    "result": "dispatch_unresolved",
                    "enablement": record["enablement"],
                }
            )
            continue
        if not character or not character["enabled"] or character["disabled_reason"]:
            outcomes.append({"owner_id": owner_id, "result": "dispatch_blocked"})
            continue
        for name, task in record["tasks"].items():
            if task["state"] == "accepted":
                continue
            if task["state"] == "uncertain":
                outcomes.append(
                    {
                        "owner_id": owner_id,
                        "task": name,
                        "result": "dispatch_uncertain",
                        "task_id": task["task_id"],
                    }
                )
                continue
            if not within_retry_budget(
                task, now, args.cooldown_hours, args.max_owner_attempts
            ):
                outcomes.append(
                    {
                        "owner_id": owner_id,
                        "task": name,
                        "result": "dispatch_budget_exhausted",
                    }
                )
                continue
            task.update(
                state="publishing",
                attempts=task.get("attempts", 0) + 1,
                last_attempt=now.isoformat(),
                publication_id=uuid.uuid4().hex,
            )
            private_write(path, pending)
            try:
                response = container_call(
                    args.container,
                    "dispatch",
                    {
                        "owner_id": owner_id,
                        "pending": record,
                        "task": name,
                        "task_id": task["task_id"],
                    },
                )
                if (
                    response.get("result") == "accepted"
                    and response.get("task_id") == task["task_id"]
                ):
                    task["state"] = "accepted"
                elif response.get("result") == "not_submitted":
                    task["state"] = "prepared"
                else:
                    task["state"] = "uncertain"
            except Exception:
                # Invocation/acknowledgement loss cannot prove that nothing was
                # queued. Preserve the intent for reviewed reconciliation.
                task["state"] = "uncertain"
            private_write(path, pending)
            outcomes.append(
                {
                    "owner_id": owner_id,
                    "task": name,
                    "result": "dispatch_" + task["state"],
                    "task_id": task["task_id"],
                }
            )
        if all(task["state"] == "accepted" for task in record["tasks"].values()):
            # Persist a completed receipt before clearing the pending entry.
            private_write(
                args.state_dir / (record["recovery_id"] + "-dispatch-receipt.json"), record
            )
            del pending[str(owner_id)]
            private_write(path, pending)
    return outcomes


def reconcile_dispatch(args, owner_ids: list[int], pending: dict, now: dt.datetime) -> dict:
    if len(owner_ids) != 1 or not args.dispatch_evidence_file or not args.dispatch_task:
        raise ValueError(
            "Reconciliation requires one owner, a task and a private evidence file"
        )
    source = args.dispatch_evidence_file
    details = source.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise ValueError(
            "Reconciliation evidence must be a root-owned private regular file"
        )
    raw = source.read_text(encoding="utf-8")
    evidence = json.loads(raw)
    owner_id = owner_ids[0]
    record = pending[str(owner_id)]
    target = (
        record
        if args.dispatch_task == "enablement"
        else record["tasks"][args.dispatch_task]
    )
    expected_id = (
        record["recovery_id"] if args.dispatch_task == "enablement" else target["task_id"]
    )
    expected_attempt = (
        record["enablement_attempt_id"]
        if args.dispatch_task == "enablement"
        else target.get("publication_id")
    )
    evidence_digest = hashlib.sha256(raw.encode()).hexdigest()
    prior_reviews = record.get("reconciliations", []) + [
        review
        for task in record["tasks"].values()
        for review in task.get("reconciliations", [])
    ]
    if (
        evidence.get("owner_id") != owner_id
        or evidence.get("task_id") != expected_id
        or evidence.get("recovery_id") != record["recovery_id"]
        or not expected_attempt
        or evidence.get("publication_id") != expected_attempt
        or evidence.get("decision") not in {"accepted", "not_submitted"}
        or not isinstance(evidence.get("evidence"), str)
        or not evidence["evidence"].strip()
        or any(review["evidence_sha256"] == evidence_digest for review in prior_reviews)
    ):
        raise ValueError("Reconciliation evidence does not match the pending recovery")
    if args.dispatch_task == "enablement":
        if record["enablement"] not in {"prepared", "uncertain"}:
            raise ValueError("Enablement is already confirmed")
        if evidence["decision"] != "accepted":
            raise ValueError(
                "Unconfirmed enablement must be retried through the normal authorization budget"
            )
        characters = container_call(args.container, "inspect", {})["characters"]
        current = current_pending_character(record, characters, owner_id)
        if not current or not current["enabled"] or current["disabled_reason"]:
            raise ValueError("Current enablement does not match the reviewed recovery")
        record["enablement"] = "confirmed"
    else:
        if target["state"] not in {"publishing", "uncertain"}:
            raise ValueError("Only uncertain publication can be reconciled")
        target["state"] = "accepted" if evidence["decision"] == "accepted" else "prepared"
        if target["state"] == "prepared":
            # A new, explicit reviewed non-submission finding authorizes a
            # bounded retry; it never silently resets an automatic retry budget.
            target.update(attempts=0, last_attempt=None)
    target.setdefault("reconciliations", []).append(
        {
            "at": now.isoformat(),
            "decision": evidence["decision"],
            "evidence_sha256": evidence_digest,
            "publication_id": expected_attempt,
            "recovery_id": record["recovery_id"],
        }
    )
    private_write(args.state_dir / "pending-dispatch.json", pending)
    return {
        "owner_id": owner_id,
        "task": args.dispatch_task,
        "result": "reconciled",
        "decision": evidence["decision"],
    }


def run_host(args, parser) -> int:
    if args.action == "inspect":
        result = container_call(args.container, "inspect", {})
        pending_path = args.state_dir / "pending-dispatch.json"
        result["pending_dispatch"] = (
            json.loads(pending_path.read_text()) if pending_path.exists() else {}
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.action in {"snapshot", "dispatch"}:
        parser.error("This is an internal action")
    try:
        owner_ids = (
            validate_owner_ids(args.owner_ids)
            if args.owner_ids
            else load_owner_ids(args.owners_file)
        )
    except (ValueError, OSError) as exception:
        parser.error(str(exception))
    if args.cooldown_hours < 1 or args.max_owner_attempts < 1:
        parser.error("Recovery cooldown and attempt cap must be positive")
    if (
        args.alliance_main_owner_id is not None
        and args.alliance_main_owner_id not in owner_ids
    ):
        parser.error("Alliance-main owner must be in the explicit owner selection")
    if args.action == "rollback_flags":
        if not args.snapshot:
            parser.error("rollback_flags requires --snapshot")
        result = container_call(
            args.container,
            "rollback_flags",
            {"snapshot": json.loads(args.snapshot.read_text()), "owner_ids": owner_ids},
        )
        print(json.dumps(result, indent=2))
        return 0
    now = dt.datetime.now(dt.timezone.utc)
    budget_path = args.state_dir / "retry-budget.json"
    pending_path = args.state_dir / "pending-dispatch.json"
    budget = json.loads(budget_path.read_text()) if budget_path.exists() else {}
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
    if args.action == "reconcile_dispatch":
        print(json.dumps(reconcile_dispatch(args, owner_ids, pending, now), indent=2))
        return 0
    current = container_call(args.container, "inspect", {})["characters"]
    candidates = {}
    for row in current:
        if eligible_character(row, owner_ids) and row["owner_active"]:
            candidates.setdefault(row["owner_id"], []).append(row)
    selected = []
    for owner_id, rows in candidates.items():
        if len(rows) != 1:
            continue
        row = rows[0]
        key = str(owner_id)
        budget[key] = authorization_budget(budget.get(key, {}), row)
        existing = pending.get(key)
        if existing and (
            existing["character_pk"] != row["character_pk"]
            or existing["authorization_fingerprint"] != row["authorization_fingerprint"]
        ):
            continue
        if args.action == "apply" or within_retry_budget(
            budget[key], now, args.cooldown_hours, args.max_owner_attempts
        ):
            selected.append(owner_id)
            if args.schedule and key not in pending:
                pending[key] = prepare_dispatch(row)
    # Retain observations even if cap/cooldown prevented another mutation.
    if budget:
        private_write(budget_path, budget)
    result = {"outcomes": [], "characters": current}
    snapshot_path = None
    if selected:
        snapshot = container_call(args.container, "snapshot", {})
        snapshot_path = args.state_dir / (
            now.strftime("%Y%m%dT%H%M%S%fZ") + "-private-snapshot.json"
        )
        private_write(snapshot_path, snapshot)
        # Reserve retry and task intents before any enabling/refresh operation.
        for owner_id in selected:
            record = budget[str(owner_id)]
            record["attempts"] = record.get("attempts", 0) + 1
            record["last_attempt"] = now.isoformat()
        private_write(budget_path, budget)
        if args.schedule:
            for owner_id in selected:
                record = pending[str(owner_id)]
                record["enablement"] = "prepared"
                record["enablement_attempt_id"] = uuid.uuid4().hex
            private_write(pending_path, pending)
        result = container_call(
            args.container,
            "apply",
            {
                "owner_ids": selected,
                "alliance_main_owner_id": args.alliance_main_owner_id,
            },
        )
        for outcome in result["outcomes"]:
            record = pending.get(str(outcome["owner_id"]))
            if (
                record
                and outcome["result"] == "restored"
                and outcome["character_pk"] == record["character_pk"]
            ):
                record["enablement"] = "confirmed"
            elif record and outcome["result"] in {
                "changed_since_inspection",
                "ownership_changed",
                "authorization_changed",
            }:
                record["enablement"] = "uncertain"
        if pending:
            private_write(pending_path, pending)
    if args.schedule and pending:
        result["outcomes"].extend(
            dispatch_pending(args, pending, result["characters"], owner_ids, now)
        )
    if snapshot_path:
        private_write(
            snapshot_path.with_name(
                snapshot_path.name.replace("private-snapshot", "result")
            ),
            result,
        )
    print(
        json.dumps(
            {"snapshot": str(snapshot_path) if snapshot_path else None, **result}, indent=2
        )
    )
    return int(
        any(
            outcome["result"] not in {"restored", "skipped", "dispatch_accepted"}
            for outcome in result["outcomes"]
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "action",
        choices=[
            "inspect",
            "snapshot",
            "apply",
            "recover",
            "rollback_flags",
            "dispatch",
            "reconcile_dispatch",
        ],
    )
    parser.add_argument("--container", default="allianceauth_gunicorn")
    parser.add_argument(
        "--state-dir", type=Path, default=Path("/var/lib/buh-auth-recovery/structures")
    )
    parser.add_argument("--owner-id", type=int, action="append", dest="owner_ids")
    parser.add_argument("--owners-file", type=Path, default=DEFAULT_OWNERS_FILE)
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--alliance-main-owner-id", type=int)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--cooldown-hours", type=int, default=24)
    parser.add_argument("--max-owner-attempts", type=int, default=2)
    parser.add_argument(
        "--dispatch-task", choices=["enablement", "structures", "notifications"]
    )
    parser.add_argument("--dispatch-evidence-file", type=Path)
    args = parser.parse_args()
    if args.inside:
        result = inside(args.action, json.loads(sys.stdin.read()))
        print(RESULT_MARKER + json.dumps(result, default=str))
        return 0
    if os.geteuid() != 0:
        parser.error("Run on the Docker host as root to protect snapshots")
    with state_lock(args.state_dir):
        return run_host(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
