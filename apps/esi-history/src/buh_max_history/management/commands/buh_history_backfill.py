"""Backfill the maximum character history currently exposed through CCP ESI."""

from django.core.management.base import BaseCommand, CommandError
from memberaudit import app_settings, tasks
from memberaudit.models import Character

from buh_max_history.services import (
    backfill_character_mail,
    configure_runtime_mail_limit,
    queue_non_mail_sections,
)


class Command(BaseCommand):
    """Run a resumable full-history backfill outside the live web workers."""

    help = str(__doc__)

    def add_arguments(self, parser):
        parser.add_argument("--mail-limit", type=int, default=2_147_483_647)
        parser.add_argument("--body-delay", type=float, default=0.20)
        parser.add_argument("--max-retries", type=int, default=3)

    def handle(self, *args, **options):
        mail_limit = options["mail_limit"]
        body_delay = options["body_delay"]
        max_retries = options["max_retries"]

        if app_settings.MEMBERAUDIT_DATA_RETENTION_LIMIT is not None:
            raise CommandError(
                "Refusing to backfill while MEMBERAUDIT_DATA_RETENTION_LIMIT is not None."
            )
        if mail_limit < 250:
            raise CommandError("--mail-limit must be at least 250")
        if body_delay < 0 or body_delay > 30:
            raise CommandError("--body-delay must be between 0 and 30 seconds")
        if max_retries < 1 or max_retries > 10:
            raise CommandError("--max-retries must be between 1 and 10")

        previous_limit = configure_runtime_mail_limit(mail_limit)
        characters = list(
            Character.objects.filter(
                is_disabled=False,
                eve_character__character_ownership__isnull=False,
            )
            .select_related("eve_character")
            .order_by("eve_character__character_name")
            .distinct()
        )

        self.stdout.write("B-UH Maximum History backfill")
        self.stdout.write(f"Updateable characters: {len(characters):,}")
        self.stdout.write(f"Temporary mail ceiling in this process: {mail_limit:,}")
        self.stdout.write(f"Normal recurring mail ceiling remains: {previous_limit:,}")
        self.stdout.write("Local Member Audit retention: unlimited")
        self.stdout.write("")

        if not characters:
            self.stdout.write(
                self.style.WARNING("No updateable Member Audit characters found.")
            )
            return

        tasks.update_market_prices.apply_async(
            priority=tasks.MEMBERAUDIT_TASKS_LOW_PRIORITY
        )
        total_section_tasks = 0
        for character in characters:
            queued = queue_non_mail_sections(character)
            total_section_tasks += queued
            self.stdout.write(
                f"Queued {queued} non-mail sections for {character.eve_character.character_name}."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {total_section_tasks:,} non-mail section refresh tasks."
            )
        )
        self.stdout.write("")

        successful = 0
        failed = 0
        for index, character in enumerate(characters, start=1):
            name = character.eve_character.character_name
            self.stdout.write(f"[{index}/{len(characters)}] {name}")
            result = backfill_character_mail(
                character=character,
                body_delay=body_delay,
                max_retries=max_retries,
                progress=self.stdout.write,
            )
            if result.is_success:
                successful += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Complete: headers={result.header_count:,}, "
                        f"bodies loaded={result.bodies_loaded:,}"
                    )
                )
            else:
                failed += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  Incomplete: headers={result.header_count:,}, "
                        f"body failures={result.bodies_failed:,}, "
                        f"token_error={result.token_error}"
                    )
                )
            self.stdout.write("")

        self.stdout.write("Backfill summary")
        self.stdout.write(f"  Successful characters: {successful:,}")
        self.stdout.write(f"  Incomplete characters: {failed:,}")
        self.stdout.write(
            "  Other Member Audit sections continue in the normal low-priority Celery queue."
        )
        if failed:
            raise CommandError(
                "One or more characters were incomplete. Reauthorize token-error characters "
                "and run the launcher again; already stored data will be reused."
            )

        self.stdout.write(self.style.SUCCESS("Maximum-history mail backfill completed."))
