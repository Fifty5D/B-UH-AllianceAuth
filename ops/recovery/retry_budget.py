"""Pure retry reservation policy; contains no credentials or service calls."""

from copy import deepcopy
import math


def _ids(values):
    result = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Authorization IDs must be nonnegative integers")
        result.add(value)
    return sorted(result)


def choose_retries(
    plans, state, *, now, cooldown=3600, max_attempts=3, limit=50, override=False
):
    """Reserve a fair, bounded set of section retries and return fresh state.

    ``plans`` should describe all failed eligible sections for fair selection.
    Absence from a plan never proves success: prior records are retained until
    a new matching authorization explicitly renews the same section's budget.
    The caller must persist the returned state before dispatching selected work.
    Authorization IDs identify database rows; refreshing the same row cannot
    renew its retry budget. No input object is modified.
    """
    if not isinstance(state, dict):
        raise ValueError("Retry state must be a mapping")
    now = float(now)
    cooldown = float(cooldown)
    if not math.isfinite(now) or not math.isfinite(cooldown) or cooldown < 0:
        raise ValueError("Retry timestamps and cooldown must be finite")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 0:
        raise ValueError("Maximum attempts must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("Section limit must be a nonnegative integer")

    current = deepcopy(state)
    seen = set()
    candidates = []
    templates = {}
    for plan in plans:
        character_pk = plan["character_pk"]
        if isinstance(character_pk, bool) or not isinstance(character_pk, int) or character_pk < 0:
            raise ValueError("Character IDs must be nonnegative integers")
        authorization_ids = _ids(plan.get("authorization_ids", []))
        if character_pk in templates:
            if _ids(templates[character_pk].get("authorization_ids", [])) != authorization_ids:
                raise ValueError("One character cannot have conflicting authorization sets")
        else:
            templates[character_pk] = plan
        for section in plan.get("sections", []):
            if not isinstance(section, str) or not section or ":" in section:
                raise ValueError("Section names must be nonempty strings without colons")
            key = f"{character_pk}:{section}"
            if key in seen:
                continue
            seen.add(key)
            previous = state.get(key, {})
            attempts = previous.get("attempts", 0)
            last_attempt = float(previous.get("last_attempt", 0))
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise ValueError("Stored attempt counts must be nonnegative integers")
            if not math.isfinite(last_attempt):
                raise ValueError("Stored retry timestamps must be finite")
            if plan.get("status") != "planned" or not authorization_ids:
                current.setdefault(key, {
                    "attempts": attempts,
                    "last_attempt": last_attempt,
                    "authorization_ids": _ids(previous.get("authorization_ids", [])),
                })
                continue
            prior_authorizations = _ids(previous.get("authorization_ids", []))
            if set(authorization_ids) - set(prior_authorizations):
                attempts = 0
                last_attempt = 0
            elif prior_authorizations:
                # Deleting an old token is not new consent. Retain its seen
                # identity so removal/reappearance cannot renew the budget.
                authorization_ids = prior_authorizations
            entry = {
                "attempts": attempts,
                "last_attempt": last_attempt,
                "authorization_ids": authorization_ids[:],
            }
            current[key] = entry
            if override or (
                attempts < max_attempts
                and (attempts == 0 or now - last_attempt >= cooldown)
            ):
                candidates.append((attempts, last_attempt, character_pk, section, key))

    selected = {}
    for _, _, character_pk, section, key in sorted(candidates)[:limit]:
        entry = current[key]
        entry["attempts"] += 1
        entry["last_attempt"] = now
        if character_pk not in selected:
            selected[character_pk] = deepcopy(templates[character_pk])
            selected[character_pk]["sections"] = []
            selected[character_pk]["authorization_ids"] = _ids(
                templates[character_pk].get("authorization_ids", [])
            )
        selected[character_pk]["sections"].append(section)
    return list(selected.values()), current
