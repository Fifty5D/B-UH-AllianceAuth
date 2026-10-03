"""Existing-token outage recovery on pinned MA models and native managers."""
from datetime import datetime
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import EveCharacterFactory, EveCorporationInfoFactory
from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils.timezone import now
from esi.errors import IncompleteResponseError
from esi.managers import TokenManager, TokenQueryset
from esi.models import Scope, Token
from eveuniverse.models import EveSolarSystem, EveType
from memberaudit import models
from memberaudit.managers import character_sections_1 as manager1
from memberaudit.managers import character_sections_2 as manager2
from memberaudit.managers import character_sections_3 as manager3
from memberaudit.managers import general as general_manager
from memberaudit.models import Character, CharacterUpdateStatus, Location
from oauthlib.oauth2 import InvalidGrantError
from requests import Response
from requests.exceptions import Timeout
from requests_oauthlib import OAuth2Session
from structures.models import Owner
from structures.tests.testdata.factories import StructureFactory

from ops.incidents import memberaudit_recovery as recovery


class MemberAuditRecoveryTests(NoSocketsTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("synthetic-ma-owner")
        self.character = EveCharacterFactory(corporation=EveCorporationInfoFactory())
        self.link = CharacterOwnership.objects.create(
            character=self.character, user=self.user, owner_hash="synthetic-owner-hash",
        )
        self.member = Character.objects.create(eve_character=self.character)
        self.token = self.make_token()
        reference = StructureFactory(owner=Owner.objects.create(corporation=EveCorporationInfoFactory()))
        self.system, self.ship_type = reference.eve_solar_system, reference.eve_type
        self.location = Location.objects.create(
            id=self.system.pk, name="Synthetic solar location", eve_solar_system=self.system,
        )
        self.payloads = {
            "location": {"solar_system_id": self.system.pk},
            "online_status": {"online": False, "last_login": now(), "last_logout": now(), "logins": 23},
            "ship": {"ship_item_id": 123456789012, "ship_type_id": self.ship_type.pk,
                     "ship_name": "Synthetic ship, never export"},
            "skill_queue": [{"queue_position": 0, "skill_id": self.ship_type.pk,
                             "finished_level": 2, "start_date": now(), "finish_date": now(),
                             "level_end_sp": 500, "level_start_sp": 100, "training_start_sp": 100}],
        }
        self.original = datetime.fromisoformat("2026-10-01T10:40:00+00:00")
        self.target = {
            "memberaudit_character_pk": self.member.pk, "character_id": self.character.character_id,
            "character_name": self.character.character_name, "user_id": self.user.pk,
            "auth_link_pk": self.link.pk, "token_ids": [self.token.pk], "sections": [],
        }
        for section in self.payloads:
            self.add_section(section)
        self.client = SimpleNamespace(
            Location=SimpleNamespace(
                GetCharactersCharacterIdLocation=self.operation(self.payloads["location"]),
                GetCharactersCharacterIdOnline=self.operation(self.payloads["online_status"]),
                GetCharactersCharacterIdShip=self.operation(self.payloads["ship"]),
            ),
            Skills=SimpleNamespace(GetCharactersCharacterIdSkillqueue=self.operation(self.payloads["skill_queue"])),
            Contacts=SimpleNamespace(
                GetCharactersCharacterIdContactsLabels=self.operation([]),
                GetCharactersCharacterIdContacts=self.operation([]),
            ),
            Mail=SimpleNamespace(
                GetCharactersCharacterIdMailLists=self.operation([]),
                GetCharactersCharacterIdMailLabels=MagicMock(),
                GetCharactersCharacterIdMail=self.operation([]),
                GetCharactersCharacterIdMailMailId=MagicMock(),
            ),
            Wallet=SimpleNamespace(GetCharactersCharacterIdWalletTransactions=self.operation([])),
        )
        self.client.Mail.GetCharactersCharacterIdMailLabels.return_value.result.return_value = (
            SimpleNamespace(labels=[], total_unread_count=0)
        )
        self.claims = {"sub": f"CHARACTER:EVE:{self.character.character_id}",
                       "owner": self.link.owner_hash, "scp": Character.esi_scopes()}
        self.grant = {"access_token": "synthetic-access-after", "refresh_token": "synthetic-refresh-after"}

    def make_token(self):
        token = Token.objects.create(
            user=self.user, character_id=self.character.character_id,
            character_name=self.character.character_name, character_owner_hash=self.link.owner_hash,
            access_token="synthetic-access-before", refresh_token="synthetic-refresh-before",
        )
        for scope in Character.esi_scopes():
            token.scopes.add(Scope.objects.get_or_create(name=scope)[0])
        return token

    def add_section(self, name, member=None):
        member = member or self.member
        status = CharacterUpdateStatus.objects.create(
            character=member, section=name, has_token_error=True, is_success=False,
            error_message="TokenDoesNotExist: synthetic-private-message",
            run_finished_at=self.original, run_started_at=self.original,
        )
        item = {"status_pk": status.pk, "section": name, "run_finished_at": self.original.isoformat(),
                "error_sha256": recovery.status_evidence(status)["error_sha256"]}
        if member is self.member:
            self.target["sections"].append(item)
        return status

    @staticmethod
    def operation(data):
        operation = MagicMock()
        if isinstance(data, list):
            value = [SimpleNamespace(model_dump=lambda row=row: row.copy()) if isinstance(row, dict) else row
                     for row in data]
        elif isinstance(data, dict):
            value = SimpleNamespace(model_dump=lambda: data.copy())
        else:
            value = data
        operation.return_value.result.return_value = value
        operation.return_value.results.return_value = value
        return operation

    def invoke(self, error=None, apply=True, extra=None):
        with (patch.object(OAuth2Session, "refresh_token", return_value=self.grant, side_effect=error) as oauth,
              patch.object(TokenManager, "validate_access_token", return_value=self.claims),
              patch.object(manager1, "esi", SimpleNamespace(client=self.client)),
              patch.object(manager2, "esi", SimpleNamespace(client=self.client)),
              patch.object(manager3, "esi", SimpleNamespace(client=self.client)),
              patch.object(general_manager, "esi", SimpleNamespace(client=self.client)),
              patch.object(EveSolarSystem.objects, "get_or_create_esi", return_value=(self.system, False)),
              patch.object(EveType.objects, "get_or_create_esi", return_value=(self.ship_type, False)),
              patch.object(Location.objects, "get_or_create_esi", return_value=(self.location, False)),
              patch.object(TokenQueryset, "require_valid", side_effect=AssertionError("no cleanup APIs")),
              patch.object(Token, "refresh_or_delete", side_effect=AssertionError("no token deletion"))):
            if extra:
                with extra:
                    value = recovery.run(self.target, apply=apply)
            else:
                value = recovery.run(self.target, apply=apply)
            return value, oauth.call_count

    def assert_preserved(self, result):
        self.assertTrue(result["same_auth_link"], result)
        self.assertTrue(result["same_token_inventory"], result)
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())
        self.assertTrue(CharacterOwnership.objects.filter(pk=self.link.pk, user=self.user).exists())
        output = json.dumps(result)
        for secret in ("synthetic-access-before", "synthetic-refresh-before", "synthetic-owner-hash",
                       "synthetic-access-after", "synthetic-refresh-after",
                       "synthetic-private-message", "Synthetic ship, never export"):
            self.assertNotIn(secret, output)

    def test_outage_then_same_grant_native_four_sections_success_and_persisted_rows(self):
        initial = list(CharacterUpdateStatus.objects.filter(character=self.member).values())
        failed, calls = self.invoke(error=IncompleteResponseError("synthetic-private-message"))
        self.assertFalse(failed["recovered"])
        self.assertEqual(calls, 1)
        self.assertEqual(initial, list(CharacterUpdateStatus.objects.filter(character=self.member).values()))
        result, calls = self.invoke()
        self.assertTrue(result["recovered"], result)
        self.assertEqual(calls, 1)
        self.assertEqual(result["validated_token_pk"], self.token.pk)
        self.assertEqual(len(result["sections"]), 4)
        for section in result["sections"]:
            self.assertTrue(section["verified"])
            self.assertTrue(section["transaction_committed"])
            row = CharacterUpdateStatus.objects.get(pk=section["status"]["status_pk"])
            self.assertFalse(row.has_token_error)
            self.assertTrue(row.is_success)
            self.assertEqual(row.error_message, "")
            self.assertGreater(row.update_finished_at, self.original)
        self.assertEqual(models.CharacterShip.objects.get(character=self.member).item_id, 123456789012)
        self.assertEqual(models.CharacterOnlineStatus.objects.get(character=self.member).logins, 23)
        self.assertEqual(models.CharacterSkillqueueEntry.objects.filter(character=self.member).count(), 1)
        self.assert_preserved(result)

    def test_report_mode_has_no_refresh_or_status_mutation(self):
        before = list(CharacterUpdateStatus.objects.values())
        result, calls = self.invoke(apply=False)
        self.assertEqual(calls, 0)
        self.assertEqual(result["category"], "eligible_existing_token_not_yet_validated")
        self.assertEqual(before, list(CharacterUpdateStatus.objects.values()))

    def test_permanent_grant_rejection_preserves_flags_and_existing_token(self):
        result, calls = self.invoke(error=InvalidGrantError())
        self.assertEqual(calls, 1)
        self.assertEqual(result["category"], "permanent_invalid_grant", result)
        self.assertTrue(result["requires_reauthorization"])
        self.assertFalse(result["recovered"])
        self.assertEqual(CharacterUpdateStatus.objects.filter(character=self.member, has_token_error=True).count(), 4)
        self.assert_preserved(result)

    def test_valid_alternative_existing_grant_avoids_unnecessary_reauthorization(self):
        second = self.make_token()
        self.target["token_ids"].append(second.pk)
        result, calls = self.invoke(error=[InvalidGrantError(), self.grant])
        self.assertTrue(result["recovered"], result)
        self.assertEqual(result["validated_token_pk"], second.pk)
        self.assertEqual(calls, 2)
        self.assertFalse(result["requires_reauthorization"])
        self.assert_preserved(result)

    def test_fresh_signed_scope_rejection_preserves_rotation_but_not_false_success(self):
        self.claims["scp"] = ["esi-location.read_location.v1"]
        result, _ = self.invoke()
        self.assertEqual(result["category"], "missing_required_scopes", result)
        self.token.refresh_from_db()
        self.assertEqual(self.token.refresh_token, self.grant["refresh_token"])
        self.assertTrue(result["requires_reauthorization"])
        self.assertEqual(CharacterUpdateStatus.objects.filter(character=self.member, has_token_error=True).count(), 4)

    def test_changed_outage_error_is_not_cleared_even_with_matching_character(self):
        selected = self.target["sections"][0]
        CharacterUpdateStatus.objects.filter(pk=selected["status_pk"]).update(error_message="InvalidGrantError")
        result, calls = self.invoke()
        self.assertEqual(calls, 0)
        self.assertEqual(result["category"], "outage_status_changed_do_not_clear", result)

    def test_later_section_failure_keeps_rotated_grant_and_successful_prior_section(self):
        self.client.Location.GetCharactersCharacterIdOnline.return_value.result.side_effect = Timeout()
        result, _ = self.invoke()
        self.assertFalse(result["recovered"])
        self.assertEqual(result["category"], "transient_failure", result)
        self.assertEqual(len(result["sections"]), 1)
        self.assertTrue(CharacterUpdateStatus.objects.get(character=self.member, section="location").is_success)
        self.assertTrue(CharacterUpdateStatus.objects.get(character=self.member, section="online_status").has_token_error)
        self.token.refresh_from_db()
        self.assertEqual(self.token.refresh_token, self.grant["refresh_token"])
        self.assert_preserved(result)

    def test_idempotent_rerun_proves_success_without_repeating_native_sections(self):
        first, _ = self.invoke()
        self.assertTrue(first["recovered"], first)
        values = list(CharacterUpdateStatus.objects.filter(character=self.member).values())
        again, _ = self.invoke()
        self.assertTrue(again["recovered"], again)
        self.assertEqual(values, list(CharacterUpdateStatus.objects.filter(character=self.member).values()))
        self.assertTrue(all(s["category"] == "already_completed_after_outage" for s in again["sections"]))

    def test_empty_queue_is_real_success_and_replaces_selected_current_queue_only(self):
        first, _ = self.invoke()
        self.assertTrue(first["recovered"], first)
        selected = self.target["sections"][-1]
        CharacterUpdateStatus.objects.filter(pk=selected["status_pk"]).update(
            has_token_error=True, is_success=False, run_finished_at=self.original,
            error_message="TokenDoesNotExist: synthetic-private-message",
        )
        self.client.Skills.GetCharactersCharacterIdSkillqueue.return_value.result.return_value = []
        result, _ = self.invoke()
        self.assertTrue(result["recovered"], result)
        self.assertFalse(models.CharacterSkillqueueEntry.objects.filter(character=self.member).exists())
        self.assert_preserved(result)

    def test_empty_contact_and_mail_chains_have_real_persisted_subsection_hashes(self):
        self.add_section("contacts")
        self.add_section("mails")
        result, _ = self.invoke()
        self.assertTrue(result["recovered"], result)
        mail = CharacterUpdateStatus.objects.get(character=self.member, section="mails")
        contact = CharacterUpdateStatus.objects.get(character=self.member, section="contacts")
        self.assertTrue(mail.content_hash_1 and mail.content_hash_2 and mail.content_hash_3)
        self.assertTrue(contact.content_hash_1 and contact.content_hash_2)
        self.assert_preserved(result)

    def test_history_pruning_blocked_while_native_mail_update_completes(self):
        self.add_section("mails")
        old_mail = models.CharacterMail.objects.create(
            character=self.member, mail_id=7654321, subject="private retained mail",
            timestamp=datetime.fromisoformat("2020-01-01T00:00:00+00:00"), body="private retained body",
        )
        before = list(models.CharacterMail.objects.filter(pk=old_mail.pk).values())
        result, _ = self.invoke(extra=patch.object(manager2, "data_retention_cutoff", return_value=now()))
        self.assertTrue(result["recovered"], result)
        self.assertEqual(before, list(models.CharacterMail.objects.filter(pk=old_mail.pk).values()))
        self.assertGreater(result["sections"][-1]["retention_deletions_prevented"], 0)
        self.assertNotIn("private retained body", json.dumps(result))

    def test_outside_older_disabled_character_states_are_unchanged(self):
        older = Character.objects.create(eve_character=EveCharacterFactory(), is_disabled=True)
        self.add_section("location", member=older)
        before = list(CharacterUpdateStatus.objects.filter(character=older).values())
        result, _ = self.invoke()
        self.assertTrue(result["recovered"], result)
        self.assertEqual(before, list(CharacterUpdateStatus.objects.filter(character=older).values()))

    def test_missing_link_and_token_are_distinct_and_do_not_refresh(self):
        Token.objects.filter(pk=self.token.pk).delete()
        result, calls = self.invoke()
        self.assertEqual(result["category"], "missing_token_record", result)
        self.assertEqual(calls, 0)
        self.assertFalse(result["record_presence"]["matching_baseline_token_exists"])

    def test_missing_auth_link_is_reported_with_existing_token_presence(self):
        CharacterOwnership.objects.filter(pk=self.link.pk).delete()
        result, calls = self.invoke()
        self.assertEqual(result["category"], "missing_auth_link", result)
        self.assertTrue(result["record_presence"]["matching_baseline_token_exists"])
        self.assertEqual(calls, 0)

    def test_missing_stored_scope_does_not_clear_outage_state(self):
        self.token.scopes.clear()
        result, calls = self.invoke()
        self.assertEqual(result["category"], "missing_required_scopes", result)
        self.assertEqual(calls, 0)
        self.assertFalse(result["recovered"])

    def test_raw_token_delete_and_auth_update_are_blocked(self):
        with transaction.atomic(), recovery.credential_guard(self.token, recovery.identity(self.token)):
            with self.assertRaises(recovery.RecoveryStop):
                Token.objects.filter(pk=self.token.pk).delete()
            with self.assertRaises(recovery.RecoveryStop):
                CharacterOwnership.objects.filter(pk=self.link.pk).update(user_id=self.user.pk)
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_other_character_current_rows_and_any_history_raw_delete_are_blocked(self):
        other = Character.objects.create(eve_character=EveCharacterFactory())
        row = models.CharacterSkillqueueEntry.objects.create(
            character=other, queue_position=0, finished_level=1, eve_type=self.ship_type,
        )
        with transaction.atomic(), recovery.preserve_history(self.member):
            with self.assertRaises(recovery.RecoveryStop):
                models.CharacterSkillqueueEntry.objects.filter(pk=row.pk).delete()
            with self.assertRaises(recovery.RecoveryStop):
                with connection.cursor() as cursor:
                    cursor.execute("DELETE FROM " + connection.ops.quote_name(models.CharacterMail._meta.db_table))
        self.assertTrue(models.CharacterSkillqueueEntry.objects.filter(pk=row.pk).exists())

    def test_native_server_error_fallback_cannot_clear_sticky_state(self):
        class ServerFailure(Exception):
            status_code = 500
        self.client.Location.GetCharactersCharacterIdLocation.return_value.result.side_effect = ServerFailure()
        result, _ = self.invoke()
        self.assertEqual(result["category"], "provider_server_failure", result)
        self.assertFalse(result["recovered"])
        self.assertEqual(CharacterUpdateStatus.objects.filter(character=self.member, has_token_error=True).count(), 4)
        self.token.refresh_from_db()
        self.assertEqual(self.token.refresh_token, self.grant["refresh_token"])

    def test_real_prepared_http_throttle_stops_with_no_secret_response_export(self):
        response = Response()
        response.status_code = 429
        response.raw = io.BytesIO(b"synthetic-secret-provider-body")
        import requests
        with patch.object(requests.sessions.Session, "send", return_value=response):
            with recovery.bounded_requests() as network:
                with self.assertRaises(recovery.RecoveryStop) as failure:
                    requests.get("https://example.invalid/synthetic")
        self.assertEqual(failure.exception.category, "provider_throttling")
        self.assertEqual(network["status_counts"], {"429": 1})
        self.assertNotIn("synthetic-secret-provider-body", json.dumps(network))
