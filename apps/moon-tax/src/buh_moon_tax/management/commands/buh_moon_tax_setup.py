"""Create safe defaults, discover compression rules, and apply shared role presets."""

from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils.timezone import now
from eveuniverse.models import EveType
from moonmining.models import MiningLedgerRecord, Owner, Refinery

from buh_moon_tax.access_roles import apply_role_presets
from buh_moon_tax.models import (
    CompressionRule,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxConfiguration,
)


class Command(BaseCommand):
    help = "Configure Moon Tax defaults and discover raw-to-compressed ore mappings."
    V030_DIRECTOR_CODES = ("manage_bill_adjustments", "preview_member_view")

    def add_arguments(self, parser):
        parser.add_argument("--payment-character", default="Fifty5D")
        parser.add_argument(
            "--payment-corporation", default="Bureau of Unified Harvesting"
        )
        parser.add_argument(
            "--repair-default-access",
            action="store_true",
            help=(
                "Explicitly reapply the starter Member/Director permissions. "
                "Normal setup preserves the customized permission matrix."
            ),
        )
        parser.add_argument(
            "--reset-payment-defaults",
            action="store_true",
            help=(
                "Explicitly replace the installation-wide legacy payment names and "
                "default recipient checklist with the two command-line destinations. "
                "Normal setup preserves director-customized policy."
            ),
        )
        parser.add_argument("--dry-run", action="store_true")

    @staticmethod
    def _character(name):
        return EveCharacter.objects.filter(character_name__iexact=name).first()

    @staticmethod
    def _corporation(name):
        return EveCorporationInfo.objects.filter(corporation_name__iexact=name).first()

    def _discover_rules(self):
        created = 0
        missing = []
        ore_types = (
            MiningLedgerRecord.objects.select_related("ore_type")
            .values_list("ore_type_id", "ore_type__name")
            .distinct()
        )
        for raw_type_id, raw_name in ore_types:
            if CompressionRule.objects.filter(raw_type_id=raw_type_id).exists():
                continue
            compressed = EveType.objects.filter(
                name__iexact=f"Compressed {raw_name}", published=True
            ).first()
            if not compressed:
                missing.append(f"{raw_name} ({raw_type_id})")
                continue
            CompressionRule.objects.create(
                raw_type_id=raw_type_id,
                raw_type_name=raw_name,
                compressed_type_id=compressed.id,
                compressed_type_name=compressed.name,
                # Since the March 2022 compression redesign, compressed ore
                # keeps the same item count as its uncompressed counterpart.
                # The advertised 100:1 ratio describes volume, not units.
                raw_units=1,
                compressed_units=1,
                notes=(
                    "Auto-discovered by exact EVE type name using the post-2022 "
                    "1:1 item-count conversion; editable by directors."
                ),
            )
            created += 1
        return created, missing

    def _discover_recipients(self):
        """Refresh the director checklist from Auth-owned characters/corporations."""

        discovered = 0
        linked_characters = EveCharacter.objects.filter(
            character_ownership__isnull=False
        ).distinct()
        corporations = {}
        for character in linked_characters:
            _, created = PaymentRecipient.objects.update_or_create(
                kind=PaymentRecipient.Kind.CHARACTER,
                entity_id=character.character_id,
                defaults={
                    "name": character.character_name,
                    "enabled": True,
                    "discovered_from_auth": True,
                },
            )
            if character.corporation_id:
                corporations[character.corporation_id] = (
                    character.corporation_name or str(character.corporation_id)
                )
            discovered += int(created)
        for owner in Owner.objects.select_related("corporation"):
            corporations[owner.corporation_id] = owner.corporation.corporation_name
        for corporation_id, corporation_name in corporations.items():
            _, created = PaymentRecipient.objects.update_or_create(
                kind=PaymentRecipient.Kind.CORPORATION,
                entity_id=corporation_id,
                defaults={
                    "name": corporation_name,
                    "enabled": True,
                    "discovered_from_auth": True,
                },
            )
            discovered += int(created)
        return discovered

    @staticmethod
    def _ensure_refinery_policies(config):
        created = 0
        for refinery in Refinery.objects.select_related(
            "owner", "owner__corporation"
        ):
            if StructureTaxPolicy.objects.filter(structure_id=refinery.id).exists():
                continue
            policy = StructureTaxPolicy.objects.create(
                structure_id=refinery.id,
                structure_name=refinery.name,
                corporation_id=refinery.owner.corporation.corporation_id,
                corporation_name=refinery.owner.corporation.corporation_name,
                tax_rate=config.default_tax_rate,
                effective_from=now(),
                notes="Initial per-Athanor policy; edit rate and recipient checklist as needed.",
            )
            policy.payment_recipients.set(config.default_payment_recipients.all())
            created += 1
        return created

    @transaction.atomic
    def handle(self, *args, **options):
        del args
        dry_run = options["dry_run"]
        try:
            member, director = apply_role_presets(
                force=options["repair_default_access"]
            )
        except Permission.DoesNotExist as exc:
            raise CommandError(
                f"Moon Tax permissions are unavailable: {exc}. Run migrations first."
            ) from exc

        character = self._character(options["payment_character"])
        corporation = self._corporation(options["payment_corporation"])
        config, config_created = TaxConfiguration.objects.get_or_create(singleton_id=1)
        if config.permission_schema_version == "0.2.2":
            new_permissions = Permission.objects.filter(
                content_type__app_label="buh_moon_tax",
                codename__in=self.V030_DIRECTOR_CODES,
            )
            if new_permissions.count() != len(self.V030_DIRECTOR_CODES):
                raise CommandError(
                    "Moon Tax v0.3 permissions are missing. Run migrations first."
                )
            director.permissions.add(*new_permissions)
            config.permission_schema_version = "0.3.0"
        reset_payment_defaults = config_created or options["reset_payment_defaults"]
        if reset_payment_defaults:
            config.payment_character_name = options["payment_character"]
            config.payment_corporation_name = options["payment_corporation"]
            config.payment_character_id = character.character_id if character else 0
            if corporation:
                config.payment_corporation_id = corporation.corporation_id
            else:
                represented_corporation_id = (
                    EveCharacter.objects.filter(
                        corporation_name__iexact=options["payment_corporation"],
                        character_ownership__isnull=False,
                    )
                    .values_list("corporation_id", flat=True)
                    .first()
                )
                config.payment_corporation_id = represented_corporation_id or 0
        if config_created:
            config.enforcement_enabled = False
        config.save()

        recipient_count = self._discover_recipients()
        default_recipients = PaymentRecipient.objects.filter(
            Q(
                kind=PaymentRecipient.Kind.CHARACTER,
                entity_id=config.payment_character_id,
            )
            | Q(
                kind=PaymentRecipient.Kind.CORPORATION,
                entity_id=config.payment_corporation_id,
            ),
            enabled=True,
        )
        if reset_payment_defaults:
            config.default_payment_recipients.set(default_recipients)
        policy_count = self._ensure_refinery_policies(config)

        created, missing = self._discover_rules()
        self.stdout.write(
            f'Applied member defaults to shared role "{member.name}" and director defaults to "{director.name}".'
        )
        self.stdout.write(
            f"Payment character: {config.payment_character_name} "
            f"({config.payment_character_id or 'ID NOT FOUND'})"
        )
        self.stdout.write(
            f"Payment corporation: {config.payment_corporation_name} "
            f"({config.payment_corporation_id or 'ID NOT FOUND'})"
        )
        self.stdout.write(f"Created {created} compression mapping(s).")
        self.stdout.write(
            f"Discovered {recipient_count} new linked payment recipient(s); "
            f"{PaymentRecipient.objects.count()} are available in the checklists."
        )
        self.stdout.write(f"Created {policy_count} new per-Athanor policy row(s).")
        if missing:
            self.stdout.write(
                self.style.WARNING(
                    "Mappings need director review in admin: " + ", ".join(missing)
                )
            )
        if not config.payment_character_id or not config.payment_corporation_id:
            self.stdout.write(
                self.style.WARNING(
                    "One or more payment destination IDs were not found in Alliance Auth. "
                    "Edit Moon Tax configuration before approving payments."
                )
            )
        if dry_run:
            transaction.set_rollback(True)
            self.stdout.write(self.style.WARNING("Dry run complete. No changes were made."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "Moon Tax defaults are ready. Enforcement is "
                    f"{'enabled' if config.enforcement_enabled else 'disabled'}."
                )
            )
