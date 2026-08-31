"""Read-only diagnostics for Mining Analytics and Member Audit data."""

import datetime as dt

from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand
from django.db.models import Count, Max, Min, Q
from django.utils import timezone
from memberaudit.models import (
    Character,
    CharacterMiningLedgerEntry,
    CharacterUpdateStatus,
)


class Command(BaseCommand):
    help = "Check Mining Analytics permissions, coverage, dates, and Member Audit update health."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--corporation-id", type=int)

    def handle(self, *args, **options):
        del args
        days = max(1, options["days"])
        cutoff = timezone.now().date() - dt.timedelta(days=days - 1)
        corporation_id = options.get("corporation_id")

        characters = Character.objects.all()
        if corporation_id:
            characters = characters.filter(
                Q(eve_character__corporation_id=corporation_id)
                | Q(
                    eve_character__character_ownership__user__profile__main_character__corporation_id=corporation_id
                )
            ).distinct()
        character_ids = characters.values_list("pk", flat=True)

        entries = CharacterMiningLedgerEntry.objects.filter(
            character_id__in=character_ids
        )
        recent = entries.filter(date__gte=cutoff)
        date_stats = entries.aggregate(oldest=Min("date"), newest=Max("date"))
        recent_stats = recent.aggregate(
            rows=Count("pk"),
            characters=Count("character_id", distinct=True),
            ores=Count("eve_type_id", distinct=True),
            systems=Count("eve_solar_system_id", distinct=True),
        )
        statuses = CharacterUpdateStatus.objects.filter(
            character_id__in=character_ids,
            section=Character.UpdateSection.MINING_LEDGER,
        )

        self.stdout.write("B-UH MINING ANALYTICS CHECK")
        self.stdout.write(f"Selected Member Audit characters: {characters.count():,}")
        self.stdout.write(
            f"Recent window: {cutoff} through {timezone.now().date()} ({days} days)"
        )
        self.stdout.write(f"Recent ledger rows: {recent_stats['rows'] or 0:,}")
        self.stdout.write(
            f"Characters with recent mining: {recent_stats['characters'] or 0:,}"
        )
        self.stdout.write(f"Recent ore types: {recent_stats['ores'] or 0:,}")
        self.stdout.write(f"Recent systems: {recent_stats['systems'] or 0:,}")
        self.stdout.write(f"Oldest stored mining date: {date_stats['oldest'] or '-'}")
        self.stdout.write(f"Newest stored mining date: {date_stats['newest'] or '-'}")

        missing_price = recent.filter(
            eve_type__market_price__average_price__isnull=True
        ).count()
        missing_volume = recent.filter(eve_type__volume__isnull=True).count()
        self.stdout.write(f"Recent rows missing an average price: {missing_price:,}")
        self.stdout.write(f"Recent rows missing type volume: {missing_volume:,}")

        self.stdout.write("\nMINING UPDATE HEALTH")
        self.stdout.write(
            f"Successful: {statuses.filter(is_success=True, has_token_error=False).count():,}"
        )
        self.stdout.write(f"Failed: {statuses.filter(is_success=False).count():,}")
        self.stdout.write(
            f"Token errors: {statuses.filter(has_token_error=True).count():,}"
        )
        self.stdout.write(
            f"Never attempted: {max(0, characters.count() - statuses.count()):,}"
        )
        last_attempt = statuses.aggregate(value=Max("run_finished_at"))["value"]
        self.stdout.write(f"Latest attempt: {last_attempt or '-'}")

        self.stdout.write("\nPERMISSIONS")
        for permission in Permission.objects.filter(
            content_type__app_label="buh_mining_analytics"
        ).prefetch_related("group_set"):
            groups = (
                ", ".join(permission.group_set.values_list("name", flat=True)) or "none"
            )
            self.stdout.write(f"{permission.codename}: {groups}")

        self.stdout.write(
            "\nSource limit: CCP's character mining endpoint returns the previous 30 days. "
            "Member Audit accumulates imported mining dates instead of backfilling pre-registration history."
        )
        self.stdout.write(self.style.SUCCESS("Check complete. No data was changed."))
