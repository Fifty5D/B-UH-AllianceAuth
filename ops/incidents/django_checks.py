"""Use the installed AA/Structures/Member Audit models with synthetic data."""
import io
import json
from pathlib import Path
from unittest.mock import patch

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import EveCharacterFactory, EveCorporationInfoFactory
from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import connection, transaction
from django.db.utils import load_backend
from esi.managers import TokenQueryset
from esi.models import Scope, Token
from memberaudit.models import Character, CharacterUpdateStatus
from structures.models import Owner, OwnerCharacter

from ops.incidents import database_report as reporter
from ops.incidents import collect_sso_incident as host_reporter


class DatabaseReportTests(NoSocketsTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("synthetic-incident-owner")
        self.user.user_permissions.add(Permission.objects.get(codename="add_structure_owner"))
        self.corp = EveCorporationInfoFactory()
        self.character = EveCharacterFactory(corporation=self.corp)
        self.ownership = CharacterOwnership.objects.create(
            character=self.character, user=self.user, owner_hash="synthetic-owner-hash",
        )
        self.owner = Owner.objects.create(corporation=self.corp, is_active=True)
        self.token = Token.objects.create(
            user=self.user, character_id=self.character.character_id,
            character_name=self.character.character_name,
            character_owner_hash="synthetic-owner-hash",
            access_token="synthetic-access-must-not-appear",
            refresh_token="synthetic-refresh-must-not-appear",
        )
        for scope in Owner.esi_scopes():
            self.token.scopes.add(Scope.objects.get_or_create(name=scope)[0])
        self.member = Character.objects.create(eve_character=self.character)
        self.status = CharacterUpdateStatus.objects.create(
            character=self.member, section="location", has_token_error=True,
            is_success=False, error_message="TokenDoesNotExist secret=never-export",
        )
        self.assertTrue(CharacterOwnership.objects.filter(pk=self.ownership.pk).exists(),
                        "Synthetic token must preserve the matching Auth owner identity")
        self.owner_character = OwnerCharacter.objects.create(
            owner=self.owner, character_ownership=self.ownership,
            is_enabled=False, disabled_reason="No valid token found for character",
        )
        self.assertTrue(OwnerCharacter.objects.filter(pk=self.owner_character.pk).exists())

    def report(self):
        with (patch.object(Token, "refresh", side_effect=AssertionError("must not refresh")),
              patch.object(TokenQueryset, "require_valid", side_effect=AssertionError("must not cleanup"))):
            return reporter.collect()

    def owner_row(self, result):
        return next(row for row in result["structures"] if row["owner_pk"] == self.owner.pk)

    def member_row(self, result):
        return next(row for row in result["memberaudit"]
                    if row["memberaudit_character_pk"] == self.member.pk)

    def test_sticky_selection_has_evidence_and_does_not_imply_invalid_token(self):
        result = self.report()
        self.assertTrue(result["scan_complete"])
        owner = self.owner_row(result)
        self.assertTrue(owner["enabled"])
        selected = owner["configured_characters"][0]
        self.assertFalse(selected["enabled"])
        self.assertTrue(selected["disabled_for_no_valid_token"])
        token = selected["identity"]["tokens"][0]
        self.assertEqual(token["id"], self.token.pk)
        self.assertEqual(token["missing_scopes"], [])
        self.assertFalse(token["permanent_failure_proven"])
        self.assertEqual(token["fresh_refresh_result"], "not_attempted_read_only")
        self.assertEqual(result["summary"]["memberaudit_sticky_sections"], 1)
        self.assertEqual(result["summary"]["memberaudit_sticky_characters"], 1)
        self.assertIsNone(result["summary"]["permanently_invalid_proven"])

    def test_report_preserves_actual_tokens_links_disabled_state_and_status(self):
        before = (
            list(Token.objects.values()), list(CharacterOwnership.objects.values()),
            list(OwnerCharacter.objects.values()), list(CharacterUpdateStatus.objects.values()),
        )
        result = self.report()
        after = (
            list(Token.objects.values()), list(CharacterOwnership.objects.values()),
            list(OwnerCharacter.objects.values()), list(CharacterUpdateStatus.objects.values()),
        )
        self.assertEqual(before, after)
        output = json.dumps(result)
        for secret in ("synthetic-access-must-not-appear", "synthetic-refresh-must-not-appear",
                       "synthetic-owner-hash", "never-export"):
            self.assertNotIn(secret, output)

    def test_scope_problem_is_separate_from_refreshability(self):
        self.token.scopes.clear()
        result = self.report()
        token = self.owner_row(result)["configured_characters"][0]["identity"]["tokens"][0]
        self.assertEqual(token["missing_scopes"], sorted(Owner.esi_scopes()))
        self.assertFalse(token["permanent_failure_proven"])

    def test_foreign_user_token_does_not_count_as_matching_auth_credentials(self):
        outsider = get_user_model().objects.create_user("synthetic-other-owner")
        Token.objects.filter(pk=self.token.pk).update(user=outsider)
        identity = self.owner_row(self.report())["configured_characters"][0]["identity"]
        self.assertEqual(identity["matching_user_token_ids"], [])
        self.assertEqual(identity["tokens"][0]["user_id"], outsider.pk)

    def test_missing_auth_link_is_distinguishable_from_missing_token(self):
        CharacterOwnership.objects.filter(character=self.character).delete()
        row = self.member_row(self.report())
        self.assertFalse(row["auth_link_exists"])
        self.assertEqual(row["orphaned_token_records"][0]["id"], self.token.pk)
        self.assertFalse(row["orphaned_token_records"][0]["permanent_failure_proven"])

    def test_read_only_guard_blocks_accidental_real_orm_mutation(self):
        with patch.object(reporter, "_collect", side_effect=lambda: Token.objects.all().delete()):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    reporter.collect()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_actual_django_shell_program_emits_the_same_read_only_inventory(self):
        source = Path(reporter.__file__).read_text(encoding="utf-8")
        before = (list(Token.objects.values()), list(CharacterOwnership.objects.values()),
                  list(OwnerCharacter.objects.values()), list(CharacterUpdateStatus.objects.values()))
        with (patch("sys.stdout", new_callable=io.StringIO) as output,
              patch.object(Token, "refresh", side_effect=AssertionError("must not refresh")),
              patch.object(TokenQueryset, "require_valid", side_effect=AssertionError("must not cleanup"))):
            call_command("shell", command=host_reporter.database_program(source),
                         verbosity=0, no_imports=True)
        result = host_reporter.parse_database_output(output.getvalue())
        self.assertTrue(result["scan_complete"], result.get("report_frames"))
        self.assertEqual(self.owner_row(result)["owner_pk"], self.owner.pk)
        self.assertEqual(before, (list(Token.objects.values()),
                                 list(CharacterOwnership.objects.values()),
                                 list(OwnerCharacter.objects.values()),
                                 list(CharacterUpdateStatus.objects.values())))

    def test_fresh_mysql_session_setup_precedes_guarded_report_queries(self):
        if connection.vendor != "mysql":
            self.skipTest("Fresh session SET regression needs actual MariaDB/MySQL")
        backend = load_backend(connection.settings_dict["ENGINE"])
        fresh = backend.DatabaseWrapper(connection.settings_dict.copy(), alias="incident-fresh")
        try:
            # Reproduce the original fresh-shell failure without touching data.
            with self.assertRaises(RuntimeError):
                with fresh.execute_wrapper(reporter.reject_writes):
                    with fresh.cursor() as cursor:
                        cursor.execute("SELECT 1")
            fresh.close()

            def selected():
                with fresh.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    return {"selected": cursor.fetchone()[0]}

            with (patch("django.db.connection", fresh),
                  patch.object(reporter, "_collect", side_effect=selected)):
                self.assertEqual(reporter.collect(), {"selected": 1})
            with fresh.execute_wrapper(reporter.reject_writes):
                with self.assertRaises(RuntimeError):
                    with fresh.cursor() as cursor:
                        cursor.execute("UPDATE esi_token SET refresh_token='never-run'")
        finally:
            fresh.close()
