"""Backfill Auth characters that already have full-scope ESI tokens."""

from collections import Counter

from django.core.management.base import BaseCommand

from esi.models import Token

from memberaudit.models import Character

from buh_memberaudit_autoreg.services import register_token


class Command(BaseCommand):
    help = "Register existing full-scope Alliance Auth characters in Member Audit."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without modifying Member Audit.",
        )
        parser.add_argument(
            "--no-update",
            action="store_true",
            help="Register characters without queuing immediate Member Audit updates.",
        )

    def handle(self, *args, **options):  # noqa: ARG002
        dry_run = options["dry_run"]
        queue_updates = not options["no_update"]
        required_scopes = Character.esi_scopes()
        tokens = (
            Token.objects.filter(user__isnull=False)
            .require_scopes(required_scopes)
            .select_related("user")
            .order_by("user_id", "character_id", "-created")
        )

        counts = Counter()
        seen = set()
        for token in tokens.iterator():
            key = (token.user_id, token.character_id)
            if key in seen:
                continue
            seen.add(key)
            result = register_token(
                token,
                dry_run=dry_run,
                queue_updates=queue_updates,
            )
            counts[result.status.value] += 1

        prefix = "Dry run: " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                prefix
                + ", ".join(
                    [
                        f"registered={counts['registered']}",
                        f"re-enabled={counts['reenabled']}",
                        f"already registered={counts['already_registered']}",
                        f"missing ownership={counts['missing_ownership']}",
                    ]
                )
            )
        )
        if not seen:
            self.stdout.write(
                self.style.WARNING(
                    "No full-scope tokens were found. Existing publicData-only "
                    "characters must authorize again through Add Character."
                )
            )

