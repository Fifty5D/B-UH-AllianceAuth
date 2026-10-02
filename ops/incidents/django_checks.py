"""Use the installed AA/Structures/Member Audit models with synthetic data."""
import json
from unittest.mock import patch

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import EveCharacterFactory, EveCorporationInfoFactory
from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from esi.managers import TokenQueryset
from esi.models import Scope, Token
from memberaudit.models import Character, CharacterUpdateStatus
from structures.models import Owner, OwnerCharacter

from ops.incidents import database_report as reporter


class DatabaseReportTests(NoSocketsTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("synthetic-incident-owner")
        self.user.user_permissions.add(Permission.objects.get(codename="add_structure_owner"))
        self.corp = EveCorporationInfoFactory()
        self.character = EveCharacterFactory(corporation=self.corp)
        self.ownership = CharacterOwnership.objects.create(character=self.character, user=self.user)
        self.owner = Owner.objects.create(corporation=self.corp, is_active=True)
        self.owner_character = OwnerCharacter.objects.create(
            owner=self.owner, character_ownership=self.ownership,
            is_enabled=False, disabled_reason="No valid token found for character",
        )
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

    def report(self):
        with (patch.object(Token, "refresh", side_effect=AssertionError("must not refresh")),
              patch.object(TokenQueryset, "require_valid", side_effect=AssertionError("must not cleanup"))):
            return reporter.collect()

    def test_sticky_selection_has_evidence_and_does_not_imply_invalid_token(self):
        result = self.report()
        self.assertTrue(result["scan_complete"])
        owner = result["structures"][0]
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
        token = result["structures"][0]["configured_characters"][0]["identity"]["tokens"][0]
        self.assertEqual(token["missing_scopes"], sorted(Owner.esi_scopes()))
        self.assertFalse(token["permanent_failure_proven"])

    def test_foreign_user_token_does_not_count_as_matching_auth_credentials(self):
        outsider = get_user_model().objects.create_user("synthetic-other-owner")
        Token.objects.filter(pk=self.token.pk).update(user=outsider)
        identity = self.report()["structures"][0]["configured_characters"][0]["identity"]
        self.assertEqual(identity["matching_user_token_ids"], [])
        self.assertEqual(identity["tokens"][0]["user_id"], outsider.pk)

    def test_missing_auth_link_is_distinguishable_from_missing_token(self):
        self.ownership.delete()
        row = self.report()["memberaudit"][0]
        self.assertFalse(row["auth_link_exists"])
        self.assertEqual(row["orphaned_token_records"][0]["id"], self.token.pk)
        self.assertFalse(row["orphaned_token_records"][0]["permanent_failure_proven"])

    def test_read_only_guard_blocks_accidental_real_orm_mutation(self):
        with patch.object(reporter, "_collect", side_effect=lambda: Token.objects.all().delete()):
            with self.assertRaises(RuntimeError):
                reporter.collect()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())
