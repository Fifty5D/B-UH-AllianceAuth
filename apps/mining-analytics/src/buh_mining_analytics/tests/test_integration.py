"""Exact-version integration tests for access, views, and Member Audit models."""

import datetime as dt
from io import StringIO
from unittest.mock import patch

from app_utils.testdata_factories import (
    EveCharacterFactory,
    EveCorporationInfoFactory,
    UserMainFactory,
)
from app_utils.testing import add_character_to_user
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from eveuniverse.tests.testdata.factories_2 import (
    EveMarketPriceFactory,
    EveSolarSystemFactory,
    EveTypeFactory,
)
from memberaudit.models import Character, CharacterMiningLedgerEntry
from memberaudit.tests.testdata.factories_2 import (
    CharacterMiningLedgerEntryFactory,
    CharacterUpdateStatusFactory,
)

from buh_mining_analytics.access import (
    SCOPE_ALL,
    SCOPE_CORP_CHARACTERS,
    SCOPE_CORP_MEMBERS,
    SCOPE_MINE,
    characters_for_scope,
)
from buh_mining_analytics.services import (
    apply_selection,
    build_dashboard_payload,
    resolve_date_range,
    selector_options,
)


def make_user(name, corporation, permissions):
    main = EveCharacterFactory(character_name=name, corporation=corporation)
    user = UserMainFactory(
        username=name.replace(" ", "_"),
        main_character__character=main,
        permissions=permissions,
    )
    character = Character.objects.create(eve_character=main)
    return user, character


def add_alt(user, name, corporation):
    eve_character = EveCharacterFactory(character_name=name, corporation=corporation)
    add_character_to_user(user, eve_character, is_main=False)
    return Character.objects.create(eve_character=eve_character)


class MiningIntegrationBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.corp = EveCorporationInfoFactory(
            corporation_id=98_765_001,
            corporation_name="Bureau of Unified Harvesting",
            corporation_ticker="B-UH",
        )
        cls.other_corp = EveCorporationInfoFactory(
            corporation_id=98_765_002,
            corporation_name="Other Rocks Inc",
            corporation_ticker="OREX",
        )
        cls.member, cls.member_main = make_user(
            "Fifty5D",
            cls.corp,
            [
                "buh_mining_analytics.basic_access",
                "buh_mining_analytics.export_data",
            ],
        )
        cls.member_alt = add_alt(cls.member, "Sixty6D", cls.other_corp)
        cls.corp_mate, cls.corp_mate_main = make_user(
            "Twenty2D",
            cls.corp,
            ["buh_mining_analytics.basic_access"],
        )
        cls.outsider, cls.outsider_main = make_user(
            "Outside Pilot",
            cls.other_corp,
            ["buh_mining_analytics.basic_access"],
        )
        cls.admin, cls.admin_main = make_user(
            "Mining Admin",
            cls.corp,
            [
                "buh_mining_analytics.basic_access",
                "buh_mining_analytics.view_corporation",
                "buh_mining_analytics.export_data",
            ],
        )
        cls.site_admin, cls.site_admin_main = make_user(
            "Site Mining Admin",
            cls.corp,
            [
                "buh_mining_analytics.basic_access",
                "buh_mining_analytics.view_all",
            ],
        )

        cls.ore = EveTypeFactory(id=46_600, name="Bitumens", volume=0.1)
        EveMarketPriceFactory(eve_type=cls.ore, average_price=500.0)
        cls.system = EveSolarSystemFactory(name="Badivefi")
        cls.today = timezone.now().date()
        CharacterMiningLedgerEntryFactory(
            character=cls.member_main,
            date=cls.today,
            eve_type=cls.ore,
            eve_solar_system=cls.system,
            quantity=1_000,
        )
        CharacterMiningLedgerEntryFactory(
            character=cls.member_alt,
            date=cls.today - dt.timedelta(days=1),
            eve_type=cls.ore,
            eve_solar_system=cls.system,
            quantity=2_000,
        )
        CharacterMiningLedgerEntryFactory(
            character=cls.corp_mate_main,
            date=cls.today,
            eve_type=cls.ore,
            eve_solar_system=cls.system,
            quantity=3_000,
        )
        CharacterMiningLedgerEntryFactory(
            character=cls.outsider_main,
            date=cls.today,
            eve_type=cls.ore,
            eve_solar_system=cls.system,
            quantity=9_000,
        )
        CharacterUpdateStatusFactory(
            character=cls.member_main,
            section=Character.UpdateSection.MINING_LEDGER,
            is_success=True,
            has_token_error=False,
            run_finished_at=timezone.now(),
        )


class TestAccessScopes(MiningIntegrationBase):
    def test_member_only_sees_owned_characters(self):
        result = set(
            characters_for_scope(self.member, SCOPE_MINE).values_list("pk", flat=True)
        )
        self.assertEqual(result, {self.member_main.pk, self.member_alt.pk})

    def test_corp_member_scope_includes_out_of_corp_alts(self):
        result = set(
            characters_for_scope(self.admin, SCOPE_CORP_MEMBERS).values_list(
                "pk", flat=True
            )
        )
        self.assertIn(self.member_alt.pk, result)
        self.assertIn(self.corp_mate_main.pk, result)
        self.assertNotIn(self.outsider_main.pk, result)

    def test_in_corp_scope_filters_on_character_affiliation(self):
        result = set(
            characters_for_scope(self.admin, SCOPE_CORP_CHARACTERS).values_list(
                "pk", flat=True
            )
        )
        self.assertIn(self.member_main.pk, result)
        self.assertNotIn(self.member_alt.pk, result)
        self.assertNotIn(self.outsider_main.pk, result)

    def test_member_cannot_request_corp_scope(self):
        with self.assertRaises(PermissionDenied):
            characters_for_scope(self.member, SCOPE_CORP_MEMBERS)

    def test_site_admin_sees_every_registered_character(self):
        result = set(
            characters_for_scope(self.site_admin, SCOPE_ALL).values_list(
                "pk", flat=True
            )
        )
        self.assertEqual(result, set(Character.objects.values_list("pk", flat=True)))

    def test_requested_ids_are_intersected_with_scope(self):
        selection = apply_selection(
            self.admin,
            SCOPE_CORP_MEMBERS,
            account_ids=None,
            character_ids=[self.outsider_main.pk],
        )
        self.assertEqual(selection.character_count, 0)


class TestDashboardServices(MiningIntegrationBase):
    def test_selector_groups_characters_by_auth_owner(self):
        options = selector_options(self.member, SCOPE_MINE)
        self.assertEqual(len(options["accounts"]), 1)
        self.assertEqual(options["accounts"][0]["name"], "Fifty5D")
        self.assertEqual(
            {item["name"] for item in options["characters"]}, {"Fifty5D", "Sixty6D"}
        )

    def test_payload_aggregates_multi_character_selection(self):
        selection = apply_selection(
            self.member,
            SCOPE_MINE,
            account_ids=None,
            character_ids=[self.member_main.pk, self.member_alt.pk],
        )
        dates = resolve_date_range(selection, "30d")
        payload = build_dashboard_payload(
            self.member, SCOPE_MINE, selection, dates, comparison="character"
        )

        self.assertEqual(payload["kpis"]["total"]["units"], 3_000)
        self.assertEqual(payload["kpis"]["total"]["volume"], 300.0)
        self.assertEqual(payload["kpis"]["total"]["isk"], 1_500_000.0)
        self.assertEqual(payload["meta"]["selected_character_count"], 2)
        self.assertEqual(payload["trend"]["comparison"], "character")


class TestDashboardViews(MiningIntegrationBase):
    def test_dashboard_renders_for_member(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("buh_mining_analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mining Analytics")
        self.assertContains(response, "mining-initial-options")

    def test_data_api_rejects_cross_scope_character_injection(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("buh_mining_analytics:data_api"),
            {
                "scope": SCOPE_CORP_MEMBERS,
                "characters": str(self.outsider_main.pk),
                "preset": "30d",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["kpis"]["total"]["units"], 0)
        self.assertEqual(response.json()["meta"]["selected_character_count"], 0)

    def test_member_corp_request_returns_403(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("buh_mining_analytics:data_api"),
            {"scope": SCOPE_CORP_MEMBERS},
        )
        self.assertEqual(response.status_code, 403)

    def test_export_respects_selection(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("buh_mining_analytics:export_csv"),
            {
                "scope": SCOPE_MINE,
                "characters": str(self.member_main.pk),
                "preset": "30d",
            },
        )
        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Fifty5D", content)
        self.assertNotIn("Sixty6D", content)
        self.assertNotIn("Outside Pilot", content)

    @patch(
        "buh_mining_analytics.views.memberaudit_tasks.update_character_mining_ledger.delay"
    )
    def test_refresh_queues_only_allowed_selection(self, delay):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("buh_mining_analytics:refresh_api"),
            {
                "scope": SCOPE_MINE,
                "characters": f"{self.member_main.pk},{self.outsider_main.pk}",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queued"], 1)
        delay.assert_called_once_with(self.member_main.pk, False)


class TestManagementCommands(MiningIntegrationBase):
    def test_setup_grants_expected_permissions_and_admin_membership(self):
        member_group = Group.objects.create(name="Member")
        output = StringIO()
        call_command(
            "buh_mining_setup",
            member_group="Member",
            admin_group="Mining Analytics Admins",
            admin_character="Fifty5D",
            stdout=output,
        )

        member_codes = set(
            member_group.permissions.filter(
                content_type__app_label="buh_mining_analytics"
            ).values_list("codename", flat=True)
        )
        admin_group = Group.objects.get(name="Mining Analytics Admins")
        admin_codes = set(
            admin_group.permissions.filter(
                content_type__app_label="buh_mining_analytics"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(member_codes, {"basic_access", "export_data"})
        self.assertEqual(
            admin_codes,
            {
                "basic_access",
                "view_corporation",
                "view_all",
                "export_data",
                "manage_access",
            },
        )
        self.assertTrue(self.member.groups.filter(pk=admin_group.pk).exists())

    def test_setup_dry_run_does_not_create_admin_group(self):
        output = StringIO()
        call_command(
            "buh_mining_setup",
            admin_group="Dry Run Mining Admins",
            dry_run=True,
            stdout=output,
        )
        self.assertFalse(Group.objects.filter(name="Dry Run Mining Admins").exists())

    def test_diagnostic_command_is_read_only_and_completes(self):
        before = CharacterMiningLedgerEntry.objects.count()
        output = StringIO()
        call_command("buh_mining_check", days=30, stdout=output)
        after = CharacterMiningLedgerEntry.objects.count()
        self.assertEqual(before, after)
        self.assertIn("Check complete. No data was changed.", output.getvalue())
