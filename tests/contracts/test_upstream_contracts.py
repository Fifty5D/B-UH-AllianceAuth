"""Guard only the upstream surface that B-UH applications actually consume.

These are upgrade gates, not snapshots of entire third-party applications.  A
dependency upgrade should change a contract here only after the corresponding
B-UH call site has been reviewed.
"""

from __future__ import annotations

import inspect
import tomllib
from importlib.metadata import version
from pathlib import Path

from allianceauth import hooks
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from allianceauth.groupmanagement.models import AuthGroup, ReservedGroupName
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.notifications import notify
from allianceauth.services.hooks import UrlHook
from allianceauth.services.tasks import QueueOnce
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import IntegerField
from django.test import SimpleTestCase
from django.urls import reverse
from eveuniverse.models import (
    EveConstellation,
    EveEntity,
    EveMarketPrice,
    EveRegion,
    EveSolarSystem,
    EveType,
)
from memberaudit import tasks as memberaudit_tasks
from memberaudit.models import (
    Character,
    CharacterContract,
    CharacterMiningLedgerEntry,
    CharacterUpdateStatus,
    CharacterWalletJournalEntry,
)
from moonmining import tasks as moonmining_tasks
from moonmining.models import Extraction, MiningLedgerRecord, Moon, Refinery
from moonmining.models import Owner as MoonOwner
from structures import tasks as structures_tasks
from structures.core.notification_types import NotificationType
from structures.models import Notification as StructureNotification
from structures.models import Owner as StructureOwner
from structures.models import Structure, StructureItem, StructureService


PLATFORM_ROOT = Path(__file__).resolve().parents[2]


class ContractAssertions:
    """Small subset assertions that produce actionable upgrade failures."""

    def assert_model_fields(self, model, *expected: str) -> None:
        actual = {field.name for field in model._meta.get_fields()}
        missing = set(expected) - actual
        self.assertFalse(
            missing,
            f"{model._meta.label} removed fields consumed by B-UH: "
            f"{sorted(missing)}",
        )

    def assert_relation_path(self, model, path: str) -> None:
        current = model
        for index, part in enumerate(path.split("__")):
            try:
                field = current._meta.get_field(part)
            except Exception as exc:  # pragma: no cover - assertion context
                self.fail(
                    f"{model._meta.label} no longer supports consumed ORM path "
                    f"{path!r}: {exc}"
                )
            if index < len(path.split("__")) - 1:
                self.assertIsNotNone(
                    field.related_model,
                    f"{current._meta.label}.{part} is no longer a relation in {path!r}",
                )
                current = field.related_model

    def assert_class_attributes(self, owner, *expected: str) -> None:
        missing = [
            name
            for name in expected
            if inspect.getattr_static(owner, name, None) is None
        ]
        self.assertFalse(
            missing,
            f"{owner.__module__}.{owner.__name__} removed APIs consumed by B-UH: "
            f"{missing}",
        )

    def assert_task(self, task, expected_name: str) -> None:
        self.assertEqual(task.name, expected_name)
        self.assertTrue(callable(task.delay))
        self.assertTrue(callable(task.apply_async))


class VersionContracts(ContractAssertions, SimpleTestCase):
    """Keep the runtime aligned with the one reviewed compatibility manifest."""

    def test_installed_upstream_versions_match_manifest(self):
        with (PLATFORM_ROOT / "platform" / "compatibility.toml").open("rb") as stream:
            compatibility = tomllib.load(stream)

        expected = {
            "allianceauth": compatibility["runtime"]["allianceauth"],
            "django-esi": compatibility["runtime"]["django_esi"],
            "aa-memberaudit": compatibility["runtime"]["memberaudit"],
            "aa-structures": compatibility["third_party"]["aa_structures"],
            "aa-moonmining": compatibility["third_party"]["aa_moonmining"],
        }
        for distribution, expected_version in expected.items():
            with self.subTest(distribution=distribution):
                self.assertEqual(version(distribution), expected_version)

    def test_required_upstream_django_apps_are_installed(self):
        for app_name in ("esi", "structures", "moonmining", "memberaudit"):
            with self.subTest(app=app_name):
                self.assertTrue(apps.is_installed(app_name))


class AllianceAuthContracts(ContractAssertions, SimpleTestCase):
    """Alliance Auth 5.2 APIs and model paths shared by all three B-UH apps."""

    def test_hook_notification_and_queue_apis_are_importable(self):
        from buh_structure_ops.tasks import capture_and_evaluate, queue_source_refreshes

        self.assertTrue(callable(hooks.register))
        self.assertTrue(callable(notify))
        self.assertTrue(callable(MenuItemHook))
        self.assertTrue(callable(UrlHook))
        self.assertIsInstance(capture_and_evaluate, QueueOnce)
        self.assertIsInstance(queue_source_refreshes, QueueOnce)

    def test_character_and_corporation_fields(self):
        self.assert_model_fields(
            EveCharacter,
            "character_id",
            "character_name",
            "corporation_id",
            "corporation_name",
            "character_ownership",
        )
        self.assert_model_fields(
            EveCorporationInfo,
            "corporation_id",
            "corporation_name",
        )
        self.assert_model_fields(CharacterOwnership, "character", "user")

    def test_auth_account_and_group_relation_paths(self):
        self.assert_relation_path(get_user_model(), "profile__main_character")
        self.assert_relation_path(Group, "authgroup__group_leaders")
        self.assert_relation_path(AuthGroup, "group_leader_groups")
        self.assert_relation_path(AuthGroup, "states")
        self.assert_model_fields(ReservedGroupName, "name")

    def test_buh_hook_implementations_still_fit_allianceauth(self):
        from buh_mining_analytics.auth_hooks import (
            MiningAnalyticsMenuItem,
            register_urls as analytics_urls,
        )
        from buh_moon_tax.auth_hooks import (
            MoonTaxMenuItem,
            register_urls as moon_tax_urls,
        )
        from buh_structure_ops.auth_hooks import (
            StructureOpsMenuItem,
            StructureOpsScheduleMenuItem,
            register_urls as structure_ops_urls,
        )

        menu_items = (
            MiningAnalyticsMenuItem(),
            MoonTaxMenuItem(),
            StructureOpsMenuItem(),
            StructureOpsScheduleMenuItem(),
        )
        self.assertTrue(all(isinstance(item, MenuItemHook) for item in menu_items))
        url_hooks = (analytics_urls(), moon_tax_urls(), structure_ops_urls())
        self.assertTrue(all(isinstance(item, UrlHook) for item in url_hooks))


class StructuresContracts(ContractAssertions, SimpleTestCase):
    """aa-structures 4.0.3 surface used by Structure Operations."""

    def test_structure_models_keep_consumed_fields(self):
        self.assert_model_fields(
            Structure,
            "owner",
            "name",
            "eve_type",
            "eve_solar_system",
            "fuel_expires_at",
            "state_timer_end",
            "unanchors_at",
            "next_reinforce_apply",
            "last_updated_at",
            "items",
            "services",
            "tags",
        )
        self.assert_class_attributes(
            Structure,
            "structure_fuel_quantity",
            "structure_fuel_usage",
            "is_reinforced",
            "get_state_display",
            "get_power_mode_display",
        )
        self.assert_model_fields(
            StructureOwner,
            "corporation",
            "characters",
            "is_active",
            "is_up",
            "structures_last_update_at",
        )
        self.assertEqual(StructureOwner._meta.pk.name, "corporation")
        self.assertIsInstance(Structure._meta.pk, IntegerField)

    def test_structure_item_service_and_notification_fields(self):
        self.assert_model_fields(
            StructureItem,
            "structure",
            "eve_type",
            "location_flag",
            "quantity",
            "last_updated_at",
        )
        self.assert_model_fields(StructureService, "structure", "name", "state")
        self.assert_model_fields(
            StructureNotification,
            "owner",
            "notification_id",
            "notif_type",
            "text",
            "timestamp",
            "structures",
        )
        self.assertEqual(
            StructureItem.LocationFlag.STRUCTURE_FUEL.value,
            "StructureFuel",
        )
        self.assertEqual(StructureService.State.ONLINE.value, 2)

    def test_structure_relation_paths(self):
        for path in (
            "owner__corporation",
            "eve_type__eve_group__eve_category",
            "items__eve_type",
            "services",
        ):
            with self.subTest(path=path):
                self.assert_relation_path(Structure, path)
        self.assert_relation_path(
            StructureOwner,
            "characters__character_ownership__character",
        )
        self.assert_relation_path(StructureNotification, "owner__corporation")

    def test_notification_type_lookup_used_for_alert_labels(self):
        notification_type = NotificationType("StructureUnderAttack")
        self.assertTrue(notification_type.label)

    def test_structure_permissions_consumed_by_shared_roles_exist(self):
        expected = {
            "basic_access",
            "view_all_structures",
            "view_structure_fit",
            "add_structure_owner",
        }
        actual = {
            codename
            for model in apps.get_app_config("structures").get_models()
            for codename, _label in model._meta.permissions
        }
        self.assertTrue(
            expected <= actual,
            f"Missing Structures permissions: {expected - actual}",
        )

    def test_structure_tasks_keep_celery_names(self):
        self.assert_task(
            structures_tasks.update_all_structures,
            "structures.tasks.update_all_structures",
        )
        self.assert_task(
            structures_tasks.fetch_all_notifications,
            "structures.tasks.fetch_all_notifications",
        )


class MoonMiningContracts(ContractAssertions, SimpleTestCase):
    """aa-moonmining 3.1.0.post1 surface used by Structure Ops and Moon Tax."""

    def test_extraction_fields_manager_and_status_semantics(self):
        self.assert_model_fields(
            Extraction,
            "refinery",
            "started_at",
            "chunk_arrival_at",
            "auto_fracture_at",
            "fractured_at",
            "status",
        )
        self.assert_class_attributes(Extraction, "get_status_display")
        self.assertTrue(callable(Extraction.objects.selected_related_defaults))
        self.assertEqual(
            set(Extraction.Status.considered_active()),
            {Extraction.Status.STARTED, Extraction.Status.READY},
        )
        self.assertEqual(Extraction.Status.STARTED.value, "ST")
        self.assertEqual(Extraction.Status.READY.value, "RD")
        self.assertEqual(Extraction.Status.CANCELED.value, "CN")

    def test_ledger_owner_refinery_and_moon_fields(self):
        self.assert_model_fields(
            MiningLedgerRecord,
            "refinery",
            "character",
            "corporation",
            "ore_type",
            "user",
            "day",
            "quantity",
        )
        self.assert_model_fields(
            MoonOwner,
            "corporation",
            "character_ownership",
            "is_enabled",
            "last_update_at",
            "last_update_ok",
            "refineries",
        )
        self.assertEqual(MoonOwner._meta.pk.name, "corporation")
        self.assert_model_fields(Refinery, "owner", "moon", "name")
        self.assertIsInstance(Refinery._meta.pk, IntegerField)
        self.assert_model_fields(Moon, "eve_moon")
        self.assert_class_attributes(Moon, "name")

    def test_moonmining_relation_paths(self):
        for model, path in (
            (Extraction, "refinery__owner__corporation"),
            (Extraction, "refinery__moon__eve_moon__eve_planet__eve_solar_system"),
            (MiningLedgerRecord, "ore_type"),
            (MiningLedgerRecord, "character"),
            (MiningLedgerRecord, "corporation"),
            (MoonOwner, "character_ownership__character"),
            (Refinery, "owner__corporation"),
        ):
            with self.subTest(model=model.__name__, path=path):
                self.assert_relation_path(model, path)

    def test_moonmining_permissions_consumed_by_shared_roles_exist(self):
        expected = {
            "basic_access",
            "extractions_access",
            "reports_access",
            "view_all_moons",
            "upload_moon_scan",
            "add_refinery_owner",
            "view_moon_ledgers",
            "view_extraction_amounts",
            "view_extraction_details",
            "view_moon_details",
            "view_moon_values",
        }
        actual = {
            codename
            for model in apps.get_app_config("moonmining").get_models()
            for codename, _label in model._meta.permissions
        }
        self.assertTrue(
            expected <= actual,
            f"Missing Moon Mining permissions: {expected - actual}",
        )

    def test_moonmining_tasks_keep_celery_names(self):
        self.assert_task(
            moonmining_tasks.run_regular_updates,
            "moonmining.tasks.run_regular_updates",
        )
        self.assert_task(
            moonmining_tasks.run_report_updates,
            "moonmining.tasks.run_report_updates",
        )


class MemberAuditContracts(ContractAssertions, SimpleTestCase):
    """aa-memberaudit 5.0.4 data and task surface used by two B-UH apps."""

    def test_character_fields_and_cached_properties(self):
        self.assert_model_fields(
            Character,
            "eve_character",
            "is_disabled",
            "contracts",
            "wallet_journal",
        )
        self.assert_class_attributes(
            Character,
            "user",
            "name",
            "character_id",
            "UpdateSection",
        )
        self.assertEqual(
            Character.UpdateSection.MINING_LEDGER.value,
            "mining_ledger",
        )

    def test_mining_ledger_and_update_status_fields(self):
        self.assert_model_fields(
            CharacterMiningLedgerEntry,
            "character",
            "date",
            "eve_solar_system",
            "eve_type",
            "quantity",
        )
        self.assert_model_fields(
            CharacterUpdateStatus,
            "character",
            "section",
            "run_finished_at",
            "update_finished_at",
            "is_success",
            "has_token_error",
        )

    def test_payment_evidence_fields_and_constants(self):
        self.assert_model_fields(
            CharacterContract,
            "contract_id",
            "status",
            "items",
            "issuer",
            "assignee",
            "acceptor",
            "reward",
            "price",
            "date_completed",
            "date_accepted",
            "title",
        )
        self.assertEqual(
            {
                CharacterContract.STATUS_FINISHED,
                CharacterContract.STATUS_FINISHED_CONTRACTOR,
                CharacterContract.STATUS_FINISHED_ISSUER,
            },
            {"FS", "FC", "FI"},
        )
        self.assert_model_fields(
            CharacterWalletJournalEntry,
            "entry_id",
            "amount",
            "context_id",
            "context_id_type",
            "date",
            "description",
            "reason",
            "ref_type",
            "second_party",
        )
        self.assertEqual(
            CharacterWalletJournalEntry.CONTEXT_ID_TYPE_CONTRACT_ID,
            "CNT",
        )

    def test_memberaudit_relation_paths(self):
        for model, path in (
            (Character, "eve_character__character_ownership__user__profile"),
            (CharacterMiningLedgerEntry, "character__eve_character"),
            (CharacterMiningLedgerEntry, "eve_type__market_price"),
            (
                CharacterMiningLedgerEntry,
                "eve_solar_system__eve_constellation__eve_region",
            ),
            (CharacterWalletJournalEntry, "second_party"),
        ):
            with self.subTest(model=model.__name__, path=path):
                self.assert_relation_path(model, path)

    def test_memberaudit_tasks_keep_celery_names(self):
        expected = (
            (
                memberaudit_tasks.update_character_mining_ledger,
                "memberaudit.tasks.update_character_mining_ledger",
            ),
            (
                memberaudit_tasks.update_character_wallet_journal,
                "memberaudit.tasks.update_character_wallet_journal",
            ),
            (
                memberaudit_tasks.update_character_contracts,
                "memberaudit.tasks.update_character_contracts",
            ),
        )
        for task, name in expected:
            with self.subTest(task=name):
                self.assert_task(task, name)

    def test_memberaudit_navigation_permission_exists(self):
        actual = {
            codename
            for model in apps.get_app_config("memberaudit").get_models()
            for codename, _label in model._meta.permissions
        }
        self.assertIn("basic_access", actual)


class EveUniverseTransitiveContracts(ContractAssertions, SimpleTestCase):
    """Fields reached through Member Audit and Moon Mining relations."""

    def test_pricing_location_and_entity_fields(self):
        self.assert_model_fields(EveType, "name", "volume", "published", "market_price")
        self.assert_model_fields(EveMarketPrice, "average_price")
        self.assert_model_fields(EveSolarSystem, "name", "eve_constellation")
        self.assert_model_fields(EveConstellation, "eve_region")
        self.assert_model_fields(EveRegion, "name")
        self.assert_model_fields(EveEntity, "name")


class NamedUrlContracts(ContractAssertions, SimpleTestCase):
    """Names reversed by B-UH runtime code and privacy integration tests."""

    def test_consumed_upstream_named_urls_reverse(self):
        routes = (
            ("structures:index", ()),
            ("structures:add_structure_owner", ()),
            ("structures:structure_details", (1,)),
            ("moonmining:add_owner", ()),
            ("moonmining:extractions", ()),
            ("moonmining:extraction_details", (1,)),
            ("moonmining:extractions_data", ("upcoming",)),
            ("moonmining:moon_details", (1,)),
            ("memberaudit:index", ()),
            ("admin:groupmanagement_group_change", (1,)),
        )
        for name, args in routes:
            with self.subTest(name=name):
                self.assertTrue(reverse(name, args=args).startswith("/"))

    def test_payment_audit_esi_scopes_are_configured(self):
        self.assertTrue(
            {
                "esi-wallet.read_character_wallet.v1",
                "esi-contracts.read_character_contracts.v1",
            }
            <= set(settings.LOGIN_TOKEN_SCOPES)
        )
