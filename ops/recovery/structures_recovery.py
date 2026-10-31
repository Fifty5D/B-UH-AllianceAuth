#!/usr/bin/env python3
"""Host-side, private-state recovery of explicitly allowed Structures token-disabled owners.

Run on the Docker host as root. Never print the private snapshot: it includes
token material needed to preserve pre-refresh state. No installed wheel changes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile

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


def inside(action: str, request: dict) -> dict:
    Owner, OwnerCharacter, Token = django_context()
    if action == "inspect":
        return {"characters": inspect_characters(Owner, Token)}
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
    from structures import providers, app_settings, tasks
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
            task_ids = []
            if request.get("schedule"):
                task_ids.append(tasks.update_structures_for_owner.delay(owner.pk).id)
                task_ids.append(tasks.process_notifications_for_owner.delay(owner.pk).id)
            outcomes.append(
                {
                    "owner_id": owner_id,
                    "result": "restored",
                    "character_pk": character.pk,
                    "task_ids": task_ids,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "action", choices=["inspect", "snapshot", "apply", "recover", "rollback_flags"]
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
    args = parser.parse_args()
    if args.inside:
        result = inside(args.action, json.loads(sys.stdin.read()))
        print(RESULT_MARKER + json.dumps(result, default=str))
        return 0
    if os.geteuid() != 0:
        parser.error("Run on the Docker host as root to protect snapshots")
    if args.action == "inspect":
        print(json.dumps(container_call(args.container, "inspect", {}), indent=2))
        return 0
    if args.action == "snapshot":
        parser.error("snapshot is an internal action")
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
    budget = json.loads(budget_path.read_text()) if budget_path.exists() else {}
    if args.action == "recover":
        current = container_call(args.container, "inspect", {})["characters"]
        eligible_ids = {
            row["owner_id"]
            for row in current
            if eligible_character(row, owner_ids) and row["owner_active"]
        }
        owner_ids = [
            owner_id
            for owner_id in owner_ids
            if owner_id in eligible_ids
            and within_retry_budget(
                budget.get(str(owner_id), {}),
                now,
                args.cooldown_hours,
                args.max_owner_attempts,
            )
        ]
        if not owner_ids:
            print(
                json.dumps(
                    {"outcomes": [], "reason": "No eligible owners within retry budget"}
                )
            )
            return 0
    snapshot = container_call(args.container, "snapshot", {})
    snapshot_path = args.state_dir / (
        now.strftime("%Y%m%dT%H%M%S%fZ") + "-private-snapshot.json"
    )
    private_write(snapshot_path, snapshot)
    # Persist budget before mutation to remain bounded across interruption.
    for owner_id in owner_ids:
        record = budget.setdefault(str(owner_id), {"attempts": 0})
        record["attempts"] += 1
        record["last_attempt"] = now.isoformat()
    private_write(budget_path, budget)
    result = container_call(
        args.container,
        "apply",
        {
            "owner_ids": owner_ids,
            "schedule": args.schedule,
            "alliance_main_owner_id": args.alliance_main_owner_id,
        },
    )
    private_write(
        snapshot_path.with_name(snapshot_path.name.replace("private-snapshot", "result")),
        result,
    )
    print(json.dumps({"snapshot": str(snapshot_path), **result}, indent=2))
    return int(
        any(
            outcome["result"] in {"error", "role_denied", "needs_reauthorization"}
            for outcome in result["outcomes"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
