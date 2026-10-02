"""Exercise existing-token repair on the real pinned production models and native sync methods."""
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import EveCharacterFactory, EveCorporationInfoFactory
from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.utils.timezone import now
from esi.errors import TokenInvalidError
from esi.managers import TokenManager, TokenQueryset
from esi.models import Token
from memberaudit.models import Character, CharacterUpdateStatus
from oauthlib.oauth2 import InvalidGrantError
from requests.exceptions import Timeout
from requests_oauthlib import OAuth2Session
from structures.models import Notification, Owner, OwnerCharacter
from structures.models import owners as owners_module

from ops.incidents import structures_recovery as recovery
from ops.incidents.run_structures_pilot import parse_result


class StructuresRecoveryTests(NoSocketsTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("synthetic-recovery-owner")
        self.user.user_permissions.add(Permission.objects.get(codename="add_structure_owner"))
        self.corp = EveCorporationInfoFactory()
        self.character = EveCharacterFactory(corporation=self.corp)
        self.link = CharacterOwnership.objects.create(
            character=self.character, user=self.user, owner_hash="synthetic-recovery-hash",
        )
        self.owner = Owner.objects.create(corporation=self.corp, is_active=True, is_up=False)
        self.token = Token.objects.create(
            user=self.user, character_id=self.character.character_id,
            character_name=self.character.character_name,
            character_owner_hash=self.link.owner_hash,
            access_token="synthetic-access-secret-before", refresh_token="synthetic-refresh-secret-before",
        )
        from esi.models import Scope
        for scope in Owner.esi_scopes():
            self.token.scopes.add(Scope.objects.get_or_create(name=scope)[0])
        self.selector = OwnerCharacter.objects.create(
            owner=self.owner, character_ownership=self.link, is_enabled=False,
            disabled_reason=recovery.DISABLE_REASON, error_count=3,
        )
        member = Character.objects.create(eve_character=self.character)
        self.ma_status = CharacterUpdateStatus.objects.create(
            character=member, section="location", has_token_error=True,
            is_success=False, error_message="TokenDoesNotExist",
        )
        self.retained = Notification.objects.create(
            owner=self.owner, notification_id=123456,
            notif_type="synthetic-retained-history",
            timestamp=now(), last_updated=now(), text="history: retained",
        )
        self.retained_values = list(Notification.objects.filter(pk=self.retained.pk).values())
        self.target = {
            "owner_pk": self.owner.pk, "owner_character_pk": self.selector.pk,
            "auth_link_pk": self.link.pk, "character_id": self.character.character_id,
            "token_pk": self.token.pk,
        }
        self.client = MagicMock()
        self.client.Character.GetCharactersCharacterId.return_value.results.return_value = SimpleNamespace(
            corporation_id=self.corp.corporation_id
        )
        self.client.Corporation.GetCorporationsCorporationIdStructures.return_value.results.return_value = []
        self.client.Assets.GetCorporationsCorporationIdAssets.return_value.results.return_value = []
        self.client.Character.GetCharactersCharacterIdNotifications.return_value.results.return_value = []
        self.claims = {"sub": f"CHARACTER:EVE:{self.character.character_id}",
                       "owner": self.link.owner_hash, "scp": Owner.esi_scopes()}
        self.grant = {"access_token": "synthetic-access-secret-after",
                      "refresh_token": "synthetic-refresh-secret-after"}

    def invoke(self, *, apply=True, oauth_error=None, extra=None):
        with (patch.object(OAuth2Session, "refresh_token", return_value=self.grant,
                           side_effect=oauth_error) as oauth,
              patch.object(TokenManager, "validate_access_token", return_value=self.claims),
              patch.object(owners_module, "esi", SimpleNamespace(client=self.client)),
              patch.object(owners_module, "STRUCTURES_FEATURE_CUSTOMS_OFFICES", False),
              patch.object(owners_module, "STRUCTURES_FEATURE_STARBASES", False),
              patch.object(owners_module, "STRUCTURES_FEATURE_SKYHOOKS", False),
              patch.object(TokenQueryset, "require_valid", side_effect=AssertionError("no token cleanup")),
              patch.object(Token, "refresh_or_delete", side_effect=AssertionError("no token deletion"))):
            if extra:
                with extra:
                    result = recovery.run(self.target, apply=apply)
            else:
                result = recovery.run(self.target, apply=apply)
            return result, oauth.call_count

    def assert_preserved(self):
        self.assertTrue(CharacterOwnership.objects.filter(pk=self.link.pk).exists())
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())
        self.assertEqual(list(Token.objects.filter(character_id=self.character.character_id)
                              .values_list("pk", flat=True)), [self.token.pk])
        self.ma_status.refresh_from_db()
        self.assertTrue(self.ma_status.has_token_error)
        self.assertEqual(list(Notification.objects.filter(pk=self.retained.pk).values()), self.retained_values)

    def test_native_sync_success_keeps_existing_token_and_link_then_reenables_only_selector(self):
        started = now()
        result, calls = self.invoke()
        self.assertEqual(calls, 1)
        self.assertTrue(result["recovered"], result)
        self.assertEqual(result["category"], "recovered_existing_token")
        self.assertTrue(result["same_token_pk"])
        self.assertTrue(result["same_auth_link_pk"])
        self.assertTrue(result["same_token_inventory"])
        self.selector.refresh_from_db()
        self.owner.refresh_from_db()
        self.token.refresh_from_db()
        self.assertTrue(self.selector.is_enabled)
        self.assertEqual(self.selector.disabled_reason, "")
        self.assertEqual(self.selector.error_count, 0)
        for field in recovery.SYNC_FIELDS:
            self.assertGreaterEqual(getattr(self.owner, field), started)
        self.assertEqual(self.token.refresh_token, self.grant["refresh_token"])
        self.assertEqual(len(result["steps"]), 3)
        self.assertTrue(all(step["committed"] for step in result["steps"]))
        self.assert_preserved()
        encoded = json.dumps(result)
        for secret in (*self.grant.values(), "synthetic-refresh-secret-before", self.link.owner_hash):
            self.assertNotIn(secret, encoded)

    def test_dry_report_performs_no_refresh_sync_or_mutation(self):
        before = (list(Token.objects.values()), list(Owner.objects.values()),
                  list(OwnerCharacter.objects.values()), list(CharacterOwnership.objects.values()),
                  list(CharacterUpdateStatus.objects.values()))
        result, calls = self.invoke(apply=False)
        self.assertEqual(calls, 0)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["refresh_attempted"])
        self.assertEqual(before, (list(Token.objects.values()), list(Owner.objects.values()),
                                 list(OwnerCharacter.objects.values()), list(CharacterOwnership.objects.values()),
                                 list(CharacterUpdateStatus.objects.values())))
        self.client.Corporation.GetCorporationsCorporationIdStructures.assert_not_called()

    def test_temporary_transport_failure_preserves_token_and_disabled_state_then_rerun_recovers(self):
        result, _ = self.invoke(oauth_error=Timeout("synthetic-secret-must-not-print"))
        self.assertFalse(result["recovered"])
        self.assertEqual(result["category"], "retryable_or_unclassified_failure")
        self.selector.refresh_from_db()
        self.token.refresh_from_db()
        self.assertFalse(self.selector.is_enabled)
        self.assertEqual(self.token.refresh_token, "synthetic-refresh-secret-before")
        self.assertNotIn("synthetic-secret-must-not-print", json.dumps(result))
        self.assert_preserved()
        recovered, _ = self.invoke()
        self.assertTrue(recovered["recovered"], recovered)
        self.assert_preserved()

    def test_fresh_invalid_grant_is_reported_without_deleting_or_clearing_it(self):
        result, calls = self.invoke(oauth_error=InvalidGrantError(description="synthetic-secret"))
        self.assertEqual(calls, 1)
        self.assertFalse(result["recovered"])
        self.assertEqual(result["category"], "permanent_invalid_grant")
        self.selector.refresh_from_db()
        self.assertFalse(self.selector.is_enabled)
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assert_preserved()

    def test_token_error_without_permanent_oauth_evidence_is_not_labelled_revoked(self):
        result, _ = self.invoke(oauth_error=TokenInvalidError())
        self.assertEqual(result["category"], "retryable_or_unclassified_failure")
        self.assertFalse(result["recovered"])
        self.assert_preserved()

    def test_missing_scopes_are_detected_before_refresh_or_clear(self):
        self.token.scopes.clear()
        result, calls = self.invoke()
        self.assertEqual(calls, 0)
        self.assertEqual(result["category"], "missing_required_scopes")
        self.assert_preserved()

    def test_fresh_identity_claim_mismatch_never_reenables_selector(self):
        self.claims["sub"] = "CHARACTER:EVE:999999999"
        result, _ = self.invoke()
        self.assertEqual(result["category"], "fresh_token_ownership_mismatch")
        self.selector.refresh_from_db()
        self.assertFalse(self.selector.is_enabled)
        self.assert_preserved()

    def test_new_scope_loss_is_distinguished_from_stored_scope_metadata(self):
        self.claims["scp"] = []
        result, _ = self.invoke()
        self.assertEqual(result["category"], "fresh_token_missing_required_scopes")
        self.assertFalse(result["recovered"])
        self.assert_preserved()

    def test_current_corporation_change_stays_fail_closed(self):
        self.client.Character.GetCharactersCharacterId.return_value.results.return_value = SimpleNamespace(
            corporation_id=self.corp.corporation_id + 1
        )
        result, _ = self.invoke()
        self.assertEqual(result["category"], "current_corporation_changed")
        self.assertFalse(result["recovered"])
        self.assert_preserved()

    def test_failed_later_sync_rolls_back_native_updates_but_keeps_rotated_refresh_grant(self):
        self.client.Assets.GetCorporationsCorporationIdAssets.return_value.results.side_effect = Timeout()
        result, _ = self.invoke()
        self.assertFalse(result["recovered"])
        self.owner.refresh_from_db()
        self.selector.refresh_from_db()
        self.token.refresh_from_db()
        self.assertIsNone(self.owner.structures_last_update_at)
        self.assertFalse(self.selector.is_enabled)
        self.assertEqual(self.token.refresh_token, self.grant["refresh_token"])
        self.assertFalse(result["steps"][0]["committed"])
        self.assert_preserved()

    def test_protected_deletion_is_blocked_and_rolls_back_sync(self):
        def destructive_sync(owner, *args, **kwargs):
            Token.objects.filter(pk=self.token.pk).delete()
        result, _ = self.invoke(extra=patch.object(Owner, "update_structures_esi", destructive_sync))
        self.assertEqual(result["category"], "protected_record_deletion_blocked")
        self.assertFalse(result["recovered"])
        self.assert_preserved()

    def test_raw_history_deletion_is_blocked_even_without_django_signals(self):
        def destructive_sync(owner, *args, **kwargs):
            Notification.objects.filter(owner=owner)._raw_delete("default")
        result, _ = self.invoke(extra=patch.object(Owner, "update_structures_esi", destructive_sync))
        self.assertEqual(result["category"], "protected_record_deletion_blocked")
        self.assertFalse(result["recovered"])
        self.assert_preserved()

    def test_incoming_asset_bound_stops_before_bulk_persistence(self):
        with patch.object(Owner, "_fetch_owner_assets_from_esi", return_value=dict.fromkeys(range(50001))):
            result, _ = self.invoke()
        self.assertEqual(result["category"], "assets_exceed_pilot_bound")
        self.assertFalse(result["recovered"])
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.structures_last_update_at)
        self.assert_preserved()

    def test_missing_existing_token_is_reported_without_replacement(self):
        Token.objects.filter(pk=self.token.pk).delete()
        result, calls = self.invoke()
        self.assertEqual(calls, 0)
        self.assertEqual(result["category"], "existing_token_record_missing")
        self.assertEqual(Token.objects.filter(character_id=self.character.character_id).count(), 0)

    def test_rerun_uses_same_record_and_does_not_create_duplicates(self):
        first, _ = self.invoke()
        second, _ = self.invoke()
        self.assertTrue(first["recovered"], first)
        self.assertTrue(second["recovered"], second)
        self.assert_preserved()

    def test_actual_production_shell_invocation_emits_bounded_result(self):
        from pathlib import Path
        source = Path(recovery.__file__).read_text(encoding="utf-8")
        code = "exec(compile(" + repr(source) + ", '<buh-structures-recovery>', 'exec'));emit(" + repr(self.target) + ",apply=False)"
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            call_command("shell", command=code, verbosity=0, no_imports=True)
        result = parse_result(output.getvalue())
        self.assertTrue(result["read_only"])
        self.assertFalse(result["refresh_attempted"])
        self.assert_preserved()
