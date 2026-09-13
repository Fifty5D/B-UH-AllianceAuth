"""Report maximum-history settings, stored ranges, and Member Audit update health."""

from datetime import date, datetime
from importlib.metadata import version

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import Count, Max, Min, Q
from django.utils.timezone import now
from memberaudit import app_settings
from memberaudit.models import Character


def _stamp(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _range(queryset, field: str) -> dict:
    return queryset.aggregate(count=Count("pk"), oldest=Min(field), newest=Max(field))


def _format_range(label: str, values: dict, extra: str = "") -> str:
    suffix = f", {extra}" if extra else ""
    return (
        f"  {label:<19} count={values['count']:,}, "
        f"oldest={_stamp(values['oldest'])}, newest={_stamp(values['newest'])}{suffix}"
    )


class Command(BaseCommand):
    """Produce a safe report containing no passwords, tokens, or mail contents."""

    help = str(__doc__)

    def handle(self, *args, **options):
        characters = Character.objects.select_related("eve_character").order_by(
            "eve_character__character_name"
        )
        enabled_sections = Character.UpdateSection.enabled_sections()
        updateable = Character.objects.filter(
            is_disabled=False,
            eve_character__character_ownership__isnull=False,
        ).distinct()

        self.stdout.write("=" * 78)
        self.stdout.write("B-UH MAXIMUM HISTORY REPORT")
        self.stdout.write("=" * 78)
        self.stdout.write(f"Generated (server time): {_stamp(now())}")
        self.stdout.write(f"aa-buh-max-history: {version('aa-buh-max-history')}")
        self.stdout.write(f"Registered characters: {characters.count():,}")
        self.stdout.write(f"Updateable characters: {updateable.count():,}")
        self.stdout.write(f"Enabled Member Audit sections: {len(enabled_sections):,}")
        retention = app_settings.MEMBERAUDIT_DATA_RETENTION_LIMIT
        retention_text = "UNLIMITED" if retention is None else f"{retention} days"
        self.stdout.write(f"Stored historical-data retention: {retention_text}")
        self.stdout.write(
            f"Normal recurring mail ceiling: {app_settings.MEMBERAUDIT_MAX_MAILS:,}"
        )
        self.stdout.write(
            "Full-mail backfills temporarily remove that ceiling only in their own container."
        )

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(SUM(data_length + index_length), 0) "
                    "FROM information_schema.tables WHERE table_schema = DATABASE()"
                )
                database_bytes = int(cursor.fetchone()[0])
            self.stdout.write(
                f"Alliance Auth database size: {database_bytes / 1024 / 1024:.1f} MiB"
            )
        except Exception:  # noqa: BLE001 - optional diagnostic only
            self.stdout.write("Alliance Auth database size: unavailable")

        self.stdout.write("")
        self.stdout.write("PER-CHARACTER HISTORY")

        issue_characters = 0
        for character in characters:
            statuses = character.update_status_set.filter(section__in=enabled_sections)
            status_counts = statuses.aggregate(
                ok=Count("pk", filter=Q(is_success=True, has_token_error=False)),
                errors=Count("pk", filter=Q(is_success=False)),
                token_errors=Count("pk", filter=Q(has_token_error=True)),
                running=Count(
                    "pk",
                    filter=Q(run_started_at__isnull=False, run_finished_at__isnull=True),
                ),
                last_finished=Max("run_finished_at"),
            )
            missing_sections = len(enabled_sections) - statuses.count()
            if (
                status_counts["errors"]
                or status_counts["token_errors"]
                or missing_sections
                or character.is_disabled
            ):
                issue_characters += 1

            name = character.eve_character.character_name.replace("\n", " ")
            state = "DISABLED" if character.is_disabled else "enabled"
            self.stdout.write("")
            self.stdout.write(f"{name} [audit_pk={character.pk}, {state}]")
            self.stdout.write(
                "  sections            "
                f"ok={status_counts['ok']}, errors={status_counts['errors']}, "
                f"token_errors={status_counts['token_errors']}, "
                f"running={status_counts['running']}, missing={missing_sections}, "
                f"last={_stamp(status_counts['last_finished'])}"
            )

            mails = _range(character.mails.all(), "timestamp")
            missing_bodies = character.mails.filter(body="").count()
            mining = _range(character.mining_ledger.all(), "date")
            journal = _range(character.wallet_journal.all(), "date")
            transactions = _range(character.wallet_transactions.all(), "date")
            contracts = _range(character.contracts.all(), "date_issued")
            corp_history = _range(character.corporation_history.all(), "start_date")

            self.stdout.write(
                _format_range("mail", mails, f"blank bodies={missing_bodies:,}")
            )
            self.stdout.write(_format_range("mining ledger", mining))
            self.stdout.write(_format_range("wallet journal", journal))
            self.stdout.write(_format_range("wallet transactions", transactions))
            self.stdout.write(_format_range("contracts", contracts))
            self.stdout.write(_format_range("corporation history", corp_history))

            problems = statuses.filter(
                Q(is_success=False) | Q(has_token_error=True)
            ).order_by("section")
            for status in problems:
                detail = (status.error_message or "").replace("\n", " ")[:180]
                kind = "TOKEN" if status.has_token_error else "ERROR"
                self.stdout.write(f"  problem {status.section}: {kind} {detail}")

        self.stdout.write("")
        self.stdout.write("SUMMARY")
        self.stdout.write(f"Characters needing attention: {issue_characters:,}")
        self.stdout.write(
            "Blank mail bodies can include genuinely empty EVE mails; reruns are safe and resumable."
        )
        self.stdout.write(
            "CCP ESI history limits still apply. Current-state endpoints cannot provide past snapshots."
        )
