"""Recover sticky Member Audit section errors without changing ownership or consent.

Planning never refreshes tokens or modifies statuses. Applying uses django-esi's
supported validity check (which may refresh/delete expired tokens), then lets
Member Audit's own section tasks record the actual result.
"""

from allianceauth.authentication.models import CharacterOwnership
from esi.models import Token
from memberaudit import tasks
from memberaudit.app_settings import MEMBERAUDIT_TASKS_NORMAL_PRIORITY
from memberaudit.models import Character


def recover_character(character_pk, *, apply=False, sections=None):
    """Plan or queue only failed enabled sections for an enabled, owned character."""
    report = {
        "character_pk": character_pk,
        "status": "not_found",
        "sections": [],
        "scheduled": [],
        "token_checked": False,
        "authorization_ids": [],
    }
    character = Character.objects.filter(pk=character_pk).first()
    if character is None:
        return report
    if character.is_disabled:
        report["status"] = "disabled"
        return report
    ownership = CharacterOwnership.objects.filter(
        character_id=character.eve_character_id
    ).first()
    if ownership is None:
        report["status"] = "orphan"
        return report

    enabled = {str(section) for section in Character.UpdateSection.enabled_sections()}
    failed = set(
        character.update_status_set.filter(has_token_error=True).values_list(
            "section", flat=True
        )
    )
    requested = set(sections) if sections is not None else failed
    selected = sorted(failed & requested & enabled)
    report["sections"] = selected
    if not selected:
        report["status"] = "no_failed_sections"
        return report

    tokens = Token.objects.filter(
        character_id=character.eve_character.character_id,
        user_id=ownership.user_id,
        character_owner_hash=ownership.owner_hash,
    ).require_scopes(Character.esi_scopes())
    if not tokens.exists():
        report["status"] = "authorization_required"
        return report
    report["authorization_ids"] = sorted(tokens.values_list("pk", flat=True))
    if not apply:
        report["status"] = "planned"
        return report

    # require_valid can refresh expired credentials and remove invalid ones.
    # Do not invoke it from the dry run or clear the section's old error first.
    try:
        valid = tokens.require_valid().exists()
    except Exception as exc:  # report the class, never credential-bearing text
        report["status"] = "validation_failed"
        report["error_type"] = type(exc).__name__
        return report
    report["token_checked"] = True
    if not valid:
        report["status"] = "authorization_required"
        return report

    unavailable = tasks.esi_status.unavailable_sections()
    if unavailable is None:
        report["status"] = "esi_status_unavailable"
        return report
    selected = [s for s in selected if s not in {str(x) for x in unavailable}]
    if not selected:
        report["status"] = "esi_sections_unavailable"
        return report

    # Consent/ownership or disable state may change during an SSO request.
    character.refresh_from_db(fields=["is_disabled"])
    if character.is_disabled or not CharacterOwnership.objects.filter(
        pk=ownership.pk,
        character_id=character.eve_character_id,
        user_id=ownership.user_id,
        owner_hash=ownership.owner_hash,
    ).exists():
        report["status"] = "ownership_changed"
        return report

    for section in selected:
        task = getattr(tasks, f"update_character_{section}")
        try:
            result = task.apply_async(
                kwargs={"character_pk": character_pk, "force_update": False},
                priority=MEMBERAUDIT_TASKS_NORMAL_PRIORITY,
            )
        except Exception as exc:
            report["status"] = "queue_failed"
            report["error_type"] = type(exc).__name__
            return report
        # QueueOnce returns None when an equivalent task is already queued.
        if result is not None:
            report["scheduled"].append(section)
    report["status"] = "queued" if report["scheduled"] else "already_queued"
    return report
