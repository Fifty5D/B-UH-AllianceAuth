"""Pure selection/result bounds for the owner-operated October SSO recovery."""
from datetime import datetime
import re


class SelectionStop(ValueError):
    pass


START = datetime.fromisoformat("2026-10-01T10:38:40+00:00")
END = datetime.fromisoformat("2026-10-01T10:55:00+00:00")
SECTIONS = frozenset({
    "location", "online_status", "ship", "skill_queue", "contacts", "mails",
    "implants", "jump_clones", "planets", "standings", "mining_ledger",
    "roles", "skills", "wallet_balance", "wallet_transactions",
})


def select_outage(report):
    """Freeze IDs, error hashes and times from the existing qualified private report."""
    targets, excluded = [], []
    for row in report["database"]["memberaudit"]:
        sticky = [s for s in row["sections"] if s["has_token_error"]]
        if not sticky:
            continue
        person = row.get("identity") or {}
        inside = [
            s for s in sticky if s["run_finished_at"]
            and START <= datetime.fromisoformat(s["run_finished_at"]) <= END
        ]
        if not inside:
            excluded.extend({"status_pk": s["status_pk"],
                             "memberaudit_character_pk": row["memberaudit_character_pk"]}
                            for s in sticky)
            continue
        if (len(inside) != len(sticky) or not row["auth_link_exists"]
                or row["disabled"] or not person.get("user_active")):
            raise SelectionStop("mixed_or_changed_outage_roster")
        token_ids = sorted(
            t["id"] for t in person["tokens"]
            if t["user_id"] == person["user_id"]
            and t["character_id"] == row["character_id"]
            and t["has_refresh_credential"] and not t["missing_scopes"]
            and t["owner_identity_matches_auth_link"] is True
        )
        if not token_ids:
            raise SelectionStop("baseline_matching_existing_grant_missing")
        selected = []
        for s in inside:
            error = s["error"]
            if (s["section"] not in SECTIONS or s["is_success"] is not False
                    or error["classes"] != ["TokenDoesNotExist"]
                    or error["oauth_codes"]
                    or not re.fullmatch(r"[0-9a-f]{64}", error["message_sha256"])):
                raise SelectionStop("outage_error_shape_changed")
            selected.append({
                "status_pk": s["status_pk"], "section": s["section"],
                "run_finished_at": s["run_finished_at"],
                "error_sha256": error["message_sha256"],
            })
        targets.append({
            "memberaudit_character_pk": row["memberaudit_character_pk"],
            "character_id": row["character_id"], "character_name": row["character_name"],
            "auth_link_pk": person["ownership_id"], "user_id": person["user_id"],
            "token_ids": token_ids, "sections": selected,
        })
    if len(targets) > 100 or sum(len(t["sections"]) for t in targets) > 400:
        raise SelectionStop("outage_bound_exceeded")
    if len({t["memberaudit_character_pk"] for t in targets}) != len(targets):
        raise SelectionStop("duplicate_outage_character")
    if len({s["status_pk"] for t in targets for s in t["sections"]}) != sum(len(t["sections"]) for t in targets):
        raise SelectionStop("duplicate_outage_status")
    return sorted(targets, key=lambda t: t["memberaudit_character_pk"]), excluded


def pilot_order(targets, pilot_name):
    first = [t for t in targets if t["character_name"] == pilot_name]
    if len(first) != 1 or {s["section"] for s in first[0]["sections"]} != {
        "location", "online_status", "ship", "skill_queue",
    }:
        raise SelectionStop("pilot_is_not_the_four_section_incident")
    remaining = [t for t in targets if t is not first[0]]
    # Cover the largest different section group and additional owners, without a public roster.
    chosen = sorted(remaining, key=lambda t: (-len(t["sections"]), t["memberaudit_character_pk"]))
    representatives, users = [], {first[0]["user_id"]}
    for target in chosen:
        if target["user_id"] not in users:
            representatives.append(target)
            users.add(target["user_id"])
        if len(representatives) == 3:
            break
    if len(representatives) != 3:
        raise SelectionStop("representative_owners_missing")
    ids = {t["memberaudit_character_pk"] for t in representatives}
    return first + representatives + [t for t in remaining if t["memberaudit_character_pk"] not in ids]


def summarize(targets, results):
    by_id = {r["memberaudit_character_pk"]: r for r in results}
    counts = {
        "outage_characters_total": len(targets),
        "outage_sections_total": sum(len(t["sections"]) for t in targets),
        "recovered_with_existing_tokens": 0, "sections_successfully_recovered": 0,
        "still_retryable_transient": 0, "permanently_invalid_revoked": 0,
        "missing_required_scopes": 0, "missing_tokens": 0,
        "missing_auth_links": 0, "other_blocker": 0, "not_attempted": 0,
    }
    needs_action = set()
    for target in targets:
        result = by_id.get(target["memberaudit_character_pk"])
        if result is None:
            counts["not_attempted"] += 1
            continue
        counts["sections_successfully_recovered"] += sum(
            bool(s.get("verified")) for s in result.get("sections", [])
        )
        if result.get("recovered"):
            counts["recovered_with_existing_tokens"] += 1
        else:
            category = result["category"]
            key = {
                "permanent_invalid_grant": "permanently_invalid_revoked",
                "permanent_ownership_mismatch": "permanently_invalid_revoked",
                "missing_required_scopes": "missing_required_scopes",
                "missing_token_record": "missing_tokens",
                "missing_auth_link": "missing_auth_links",
                "transient_failure": "still_retryable_transient",
                "provider_throttling": "still_retryable_transient",
                "provider_server_failure": "still_retryable_transient",
                "provider_error_budget_low": "still_retryable_transient",
                "operation_deadline_exceeded": "still_retryable_transient",
            }.get(category, "other_blocker")
            counts[key] += 1
            if result.get("requires_reauthorization") is True:
                needs_action.add(target["user_id"])
    counts["users_requiring_reauthorization"] = len(needs_action)
    counts["characters_requiring_reauthorization"] = sum(
        r.get("requires_reauthorization") is True for r in results
    )
    counts["characters_remaining"] = len(targets) - counts["recovered_with_existing_tokens"]
    counts["sections_remaining"] = counts["outage_sections_total"] - counts["sections_successfully_recovered"]
    return counts
