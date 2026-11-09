"""Preview or apply a bounded recovery after preserving private status evidence."""

import datetime as dt
import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from memberaudit.models import Character, CharacterUpdateStatus

from buh_memberaudit_autoreg.recovery import recover_character


def private_snapshot(path, data):
    """Write safe metadata once, without credentials, under private permissions."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, default=str, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class Command(BaseCommand):
    help = "Recover failed enabled Member Audit sections; dry run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--snapshot", help="New private JSON path required for apply")
        parser.add_argument("--limit", type=int, default=50, help="Character limit; 0 inventories all during preview")
        parser.add_argument("--expect-characters", type=int)
        parser.add_argument("--expect-sections", type=int)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 0 <= limit <= 100 or (options["apply"] and limit == 0):
            raise CommandError("--limit must be 1..100 for apply, or 0 for an unlimited preview")
        if options["apply"] and not options["snapshot"]:
            raise CommandError("--apply requires a new --snapshot path")

        flagged = CharacterUpdateStatus.objects.filter(has_token_error=True)
        candidate_query = (
            Character.objects.filter(
                is_disabled=False, update_status_set__has_token_error=True
            )
            .order_by("pk")
            .values_list("pk", flat=True)
            .distinct()
        )
        candidates = list(candidate_query[:limit] if limit else candidate_query)
        plans = [recover_character(pk) for pk in candidates]
        actionable = [row for row in plans if row["status"] == "planned"]
        sections = sum(len(row["sections"]) for row in actionable)
        summary = {
            "mode": "apply" if options["apply"] else "preview",
            "flagged_characters": flagged.values("character_id").distinct().count(),
            "flagged_sections": flagged.count(),
            "disabled_characters_retained": flagged.filter(
                character__is_disabled=True
            ).values("character_id").distinct().count(),
            "disabled_sections_retained": flagged.filter(
                character__is_disabled=True
            ).count(),
            "actionable_characters": len(actionable),
            "actionable_sections": sections,
        }
        for argument, actual in (
            ("expect_characters", len(actionable)),
            ("expect_sections", sections),
        ):
            expected = options[argument]
            if expected is not None and expected != actual:
                raise CommandError(
                    f"{argument} changed: expected {expected}, found {actual}; preview again"
                )

        snapshot = {
            "schema_version": 1,
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "summary": summary,
            "plans": plans,
            "status_rows": list(flagged.values(
                "pk", "character_id", "character__eve_character__character_name",
                "character__is_disabled", "section", "has_token_error",
                "is_success", "run_finished_at", "update_finished_at",
            )),
            "section_counts": list(flagged.values("section").annotate(rows=Count("pk"))),
        }
        if options["snapshot"]:
            try:
                private_snapshot(options["snapshot"], snapshot)
            except OSError as exc:
                raise CommandError(f"Snapshot failed ({type(exc).__name__}); nothing queued") from None
        results = plans
        if options["apply"]:
            results = [
                recover_character(row["character_pk"], apply=True, sections=row["sections"])
                for row in actionable
            ]
        self.stdout.write(json.dumps({"summary": summary, "results": results}, indent=2))
        if options["apply"] and any(
            row["status"] not in {"queued", "already_queued", "no_failed_sections"}
            for row in results
        ):
            raise CommandError("Recovery is incomplete; inspect the safe results above")
