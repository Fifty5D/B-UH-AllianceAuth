"""Integration tests against Alliance Auth and Member Audit models."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from esi.models import Scope, Token

from app_utils.testdata_factories import UserMainFactory
from memberaudit.models import Character

from buh_memberaudit_autoreg.scopes import (
    LOGIN_TOKEN_SCOPES,
    MEMBERAUDIT_ESI_SCOPES,
)
from buh_memberaudit_autoreg.checks import check_configuration


MODULE_PATH = "buh_memberaudit_autoreg.services.tasks"


class AutoRegistrationTests(TestCase):
    def setUp(self):
        self.user = UserMainFactory()
        self.token = self.user.token_set.first()
        Character.objects.filter(
            eve_character=self.user.profile.main_character
        ).delete()

    def _scopes(self, names):
        return [
            Scope.objects.get_or_create(name=name, defaults={"help_text": name})[0]
            for name in names
        ]

    def test_static_scope_list_matches_memberaudit_5(self):
        self.assertEqual(
            set(MEMBERAUDIT_ESI_SCOPES), set(Character.esi_scopes())
        )
        self.assertIn("publicData", LOGIN_TOKEN_SCOPES)

    def test_full_configuration_passes_addon_check(self):
        self.assertEqual(check_configuration(None), [])

    @override_settings(LOGIN_TOKEN_SCOPES=["publicData"])
    def test_missing_login_scopes_fail_addon_check(self):
        errors = check_configuration(None)

        self.assertEqual([error.id for error in errors], ["buh_memberaudit_autoreg.E002"])

    @patch(MODULE_PATH + ".update_compliance_groups_for_user.apply_async")
    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_full_scope_token_registers_character(self, update_character, compliance):
        with self.captureOnCommitCallbacks(execute=True):
            self.token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES))

        character = Character.objects.get(
            eve_character=self.user.profile.main_character
        )
        self.assertFalse(character.is_disabled)
        update_character.assert_called_once()
        # No compliance designation exists in this test database.
        compliance.assert_not_called()

    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_partial_scope_token_is_ignored(self, update_character):
        self.token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES[:-1]))

        self.assertFalse(
            Character.objects.filter(
                eve_character=self.user.profile.main_character
            ).exists()
        )
        update_character.assert_not_called()

    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_mismatched_owner_hash_is_ignored(self, update_character):
        self.token.character_owner_hash = "not-the-owned-account"
        self.token.save(update_fields=["character_owner_hash"])
        self.token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES))

        self.assertFalse(
            Character.objects.filter(
                eve_character=self.user.profile.main_character
            ).exists()
        )
        update_character.assert_not_called()

    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_existing_registration_is_idempotent(self, update_character):
        Character.objects.create(
            eve_character=self.user.profile.main_character,
            is_disabled=False,
        )

        self.token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES))

        self.assertEqual(
            Character.objects.filter(
                eve_character=self.user.profile.main_character
            ).count(),
            1,
        )
        update_character.assert_not_called()

    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_disabled_character_is_reenabled(self, update_character):
        character = Character.objects.create(
            eve_character=self.user.profile.main_character,
            is_disabled=True,
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES))

        character.refresh_from_db()
        self.assertFalse(character.is_disabled)
        update_character.assert_called_once()

    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_registration_finishes_when_sso_attaches_user(self, update_character):
        pending_token = Token.objects.create(
            character_id=self.token.character_id,
            character_name=self.token.character_name,
            character_owner_hash=self.token.character_owner_hash,
            access_token="pending-access-token",
            refresh_token="pending-refresh-token",
            user=None,
        )
        pending_token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES))
        self.assertFalse(
            Character.objects.filter(
                eve_character=self.user.profile.main_character
            ).exists()
        )

        pending_token.user = self.user
        with self.captureOnCommitCallbacks(execute=True):
            pending_token.save(update_fields=["user"])

        self.assertTrue(
            Character.objects.filter(
                eve_character=self.user.profile.main_character,
                is_disabled=False,
            ).exists()
        )
        update_character.assert_called_once()

    @patch(MODULE_PATH + ".update_character.apply_async")
    def test_backfill_dry_run_does_not_reenable_character(self, update_character):
        with self.captureOnCommitCallbacks(execute=True):
            self.token.scopes.add(*self._scopes(MEMBERAUDIT_ESI_SCOPES))
        character = Character.objects.get(
            eve_character=self.user.profile.main_character
        )
        character.is_disabled = True
        character.save(update_fields=["is_disabled"])
        update_character.reset_mock()
        output = StringIO()

        call_command("buh_autoreg_backfill", "--dry-run", stdout=output)

        character.refresh_from_db()
        self.assertTrue(character.is_disabled)
        self.assertIn("re-enabled=1", output.getvalue())
        update_character.assert_not_called()
