from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase

from buh_moon_tax.access_roles import apply_role_presets
from buh_moon_tax.models import PaymentRecipient, TaxConfiguration


class RolePresetSafetyTests(TestCase):
    def test_normal_setup_preserves_removed_permission(self):
        member, _ = apply_role_presets(force=True)
        permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="view_mining_values",
        )
        member.permissions.remove(permission)

        apply_role_presets()

        self.assertFalse(member.permissions.filter(pk=permission.pk).exists())

    def test_explicit_repair_reapplies_starter_permissions(self):
        member, _ = apply_role_presets(force=True)
        permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="view_mining_values",
        )
        member.permissions.remove(permission)

        apply_role_presets(force=True)

        self.assertTrue(member.permissions.filter(pk=permission.pk).exists())

    def test_upgrade_grants_new_director_permissions_only_once(self):
        _, director = apply_role_presets(force=True)
        permission = Permission.objects.get(
            content_type__app_label="buh_moon_tax",
            codename="manage_bill_adjustments",
        )
        director.permissions.remove(permission)
        config = TaxConfiguration.objects.create(permission_schema_version="0.2.2")

        call_command("buh_moon_tax_setup", verbosity=0)

        config.refresh_from_db()
        self.assertEqual(config.permission_schema_version, "0.3.0")
        self.assertTrue(director.permissions.filter(pk=permission.pk).exists())

        director.permissions.remove(permission)
        call_command("buh_moon_tax_setup", verbosity=0)

        self.assertFalse(director.permissions.filter(pk=permission.pk).exists())

    def test_normal_setup_preserves_customized_payment_and_enforcement_policy(self):
        apply_role_presets(force=True)
        recipient = PaymentRecipient.objects.create(
            kind=PaymentRecipient.Kind.CHARACTER,
            entity_id=90000001,
            name="Member Investor",
        )
        config = TaxConfiguration.objects.create(
            permission_schema_version="0.3.0",
            payment_character_id=90000001,
            payment_character_name="Member Investor",
            payment_corporation_id=98000001,
            payment_corporation_name="Investment Holding",
            enforcement_enabled=True,
        )
        config.default_payment_recipients.add(recipient)

        call_command("buh_moon_tax_setup", verbosity=0)

        config.refresh_from_db()
        self.assertEqual(config.payment_character_id, 90000001)
        self.assertEqual(config.payment_character_name, "Member Investor")
        self.assertEqual(config.payment_corporation_id, 98000001)
        self.assertEqual(config.payment_corporation_name, "Investment Holding")
        self.assertTrue(config.enforcement_enabled)
        self.assertEqual(
            list(config.default_payment_recipients.values_list("pk", flat=True)),
            [recipient.pk],
        )
