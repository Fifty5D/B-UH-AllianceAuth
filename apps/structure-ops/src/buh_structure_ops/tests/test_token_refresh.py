"""Exercise real token/queryset code with synthetic rotating SSO credentials."""

from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import Mock, patch

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import EveCharacterFactory
from django.apps import apps
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from esi.errors import IncompleteResponseError, TokenInvalidError
from esi.managers import TokenManager, TokenQueryset
from esi.models import Scope, Token
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError, MissingTokenError
from requests.exceptions import ConnectionError as SSOConnectionError
from requests.exceptions import Timeout as SSOTimeout

from memberaudit import tasks as memberaudit_tasks
from memberaudit.core import esi_status
from memberaudit.helpers import UpdateSectionResult
from memberaudit.models import Character
from memberaudit.tests.testdata.factories_2 import CharacterFactory

from buh_structure_ops.token_refresh import install_token_refresh_guard


class TokenRefreshTests(NoSocketsTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("synthetic-token-owner")
        EveCharacterFactory(character_id=90000001)
        self.token = Token.objects.create(
            user=self.user, character_id=90000001, character_name="Synthetic",
            character_owner_hash="synthetic-owner", access_token="old-access",
            refresh_token="old-refresh",
        )
        self.token.scopes.add(Scope.objects.get_or_create(name="esi-skills.read_skills.v1")[0])
        Token.objects.filter(pk=self.token.pk).update(created=now() - timedelta(hours=1))
        self.token.refresh_from_db()
        self.session = Mock()
        self.session.refresh_token.return_value = {
            "access_token": "fresh-access", "refresh_token": "rotated-refresh",
        }
        self.validation = patch.object(
            TokenManager, "validate_access_token", return_value={"owner": "synthetic-owner"}
        )
        self.validation.start()
        self.addCleanup(self.validation.stop)

    def test_stale_refresher_reuses_rotated_token_instead_of_reusing_old_grant(self):
        sibling = Token.objects.get(pk=self.token.pk)
        self.token.refresh(session=self.session)
        self.session.refresh_token.side_effect = InvalidGrantError()

        sibling.refresh(session=self.session)

        self.session.refresh_token.assert_called_once()
        self.assertEqual(sibling.access_token, "fresh-access")
        self.assertEqual(sibling.refresh_token, "rotated-refresh")
        self.assertEqual(sibling.created, self.token.created)

    def test_stale_cleanup_does_not_delete_a_siblings_successful_refresh(self):
        stale = Token.objects.get(pk=self.token.pk)
        self.token.refresh(session=self.session)
        with patch("esi.models.OAuth2Session", return_value=self.session):
            self.session.refresh_token.side_effect = InvalidGrantError()
            stale.refresh_or_delete()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())
        self.assertEqual(self.token.scopes.count(), 1)
        self.session.refresh_token.assert_called_once()

    def test_unpatched_cleanup_reproduces_deletion_of_a_renewed_grant(self):
        stale = Token.objects.get(pk=self.token.pk)
        self.token.refresh(session=self.session)
        original_refresh = Token.refresh.__wrapped__
        original_cleanup = Token.refresh_or_delete.__wrapped__
        with (
            patch.object(Token, "refresh", original_refresh),
            patch("esi.models.OAuth2Session", return_value=self.session),
        ):
            self.session.refresh_token.side_effect = InvalidGrantError()
            original_cleanup(stale)
        self.assertFalse(Token.objects.filter(pk=self.token.pk).exists())

    def test_require_valid_preserves_user_and_scope_filters_after_refresh(self):
        outsider = get_user_model().objects.create_user("other-token-owner")
        with patch("esi.models.OAuth2Session", return_value=self.session):
            found = (
                Token.objects.filter(user=self.user, character_id=self.token.character_id)
                .require_scopes(["esi-skills.read_skills.v1"]).require_valid().first()
            )
        self.assertEqual(found.pk, self.token.pk)
        self.assertEqual(found.access_token, "fresh-access")
        self.assertFalse(Token.objects.filter(user=outsider).require_valid().exists())
        self.assertFalse(Token.objects.all().require_scopes(["missing-scope"]).require_valid().exists())

    def test_rejected_grant_is_still_removed_and_not_returned(self):
        self.session.refresh_token.side_effect = InvalidGrantError()
        with patch("esi.models.OAuth2Session", return_value=self.session):
            self.assertIsNone(Token.objects.filter(pk=self.token.pk).require_valid().first())
        self.assertFalse(Token.objects.filter(pk=self.token.pk).exists())

    def test_incomplete_reply_preserves_grant_but_does_not_return_expired_token(self):
        self.session.refresh_token.side_effect = MissingTokenError()
        with patch("esi.models.OAuth2Session", return_value=self.session):
            with self.assertRaises(IncompleteResponseError):
                Token.objects.filter(pk=self.token.pk).require_valid().first()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_stale_identity_cannot_refresh_or_delete_reassigned_token(self):
        Token.objects.filter(pk=self.token.pk).update(character_owner_hash="different-owner")
        with self.assertRaises(IncompleteResponseError):
            self.token.refresh(session=self.session)
        with self.assertRaises(IncompleteResponseError):
            self.token.refresh_or_delete()
        self.session.refresh_token.assert_not_called()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_deleted_row_is_not_resurrected_by_a_stale_refresher(self):
        Token.objects.filter(pk=self.token.pk).delete()
        with self.assertRaises(Token.DoesNotExist):
            self.token.refresh(session=self.session)
        self.token.refresh_or_delete()
        self.assertEqual(Token.objects.count(), 0)
        self.session.refresh_token.assert_not_called()

    def test_expired_nonrefreshable_grant_is_removed(self):
        Token.objects.filter(pk=self.token.pk).update(refresh_token=None)
        self.assertFalse(Token.objects.all().require_valid().exists())
        self.assertFalse(Token.objects.filter(pk=self.token.pk).exists())

    def test_owner_change_from_sso_still_fails(self):
        with patch.object(TokenManager, "validate_access_token", return_value={"owner": "new-owner"}):
            with self.assertRaises(TokenInvalidError):
                self.token.refresh(session=self.session)

    def test_installation_is_version_scoped_and_idempotent(self):
        methods = (
            Token.refresh, Token.refresh_or_delete,
            TokenQueryset.bulk_refresh, TokenQueryset.require_valid,
        )
        self.assertFalse(install_token_refresh_guard())
        with patch("buh_structure_ops.token_refresh.esi.__version__", "9.6.1"):
            self.assertFalse(install_token_refresh_guard())
        self.assertEqual(
            methods,
            (Token.refresh, Token.refresh_or_delete, TokenQueryset.bulk_refresh,
             TokenQueryset.require_valid),
        )

    def test_fresh_matching_token_remains_usable_when_expired_sibling_is_incomplete(self):
        sibling = Token.objects.create(
            user=self.user, character_id=self.token.character_id,
            character_name="Synthetic", character_owner_hash="synthetic-owner",
            access_token="already-fresh", refresh_token="sibling-refresh",
        )
        sibling.scopes.set(self.token.scopes.all())
        self.session.refresh_token.side_effect = MissingTokenError()
        with patch("esi.models.OAuth2Session", return_value=self.session):
            found = Token.objects.filter(
                user=self.user, character_id=self.token.character_id
            ).require_scopes(["esi-skills.read_skills.v1"]).require_valid().first()
        self.assertEqual(found.pk, sibling.pk)
        self.assertEqual(found.access_token, "already-fresh")
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_direct_bulk_incomplete_response_raises_without_removing_grant(self):
        self.session.refresh_token.side_effect = MissingTokenError()
        with (
            patch("esi.models.OAuth2Session", return_value=self.session),
            self.assertRaises(IncompleteResponseError),
        ):
            Token.objects.filter(pk=self.token.pk).bulk_refresh()
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_transport_failure_does_not_delete_token_or_leak_incomplete_context(self):
        for failure in (SSOTimeout("synthetic timeout"), SSOConnectionError("synthetic connection")):
            with self.subTest(failure=type(failure).__name__):
                self.session.refresh_token.side_effect = failure
                with (
                    patch("esi.models.OAuth2Session", return_value=self.session),
                    self.assertRaises(type(failure)),
                ):
                    Token.objects.filter(pk=self.token.pk).require_valid()
                self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())
                # A later unrelated, genuinely empty query is still empty.
                self.assertFalse(Token.objects.filter(pk=-1).require_valid().exists())


class MemberAuditSSOOutageTests(NoSocketsTestCase):
    """A transient SSO outage must not latch Member Audit's permanent token flag."""

    def setUp(self):
        with ExitStack() as stack:
            if apps.is_installed("buh_memberaudit_autoreg"):
                from buh_memberaudit_autoreg.services import (
                    RegistrationResult, RegistrationStatus,
                )
                stack.enter_context(patch(
                    "buh_memberaudit_autoreg.signals.register_token",
                    return_value=RegistrationResult(
                        RegistrationStatus.MISSING_SCOPES, 0, None
                    ),
                ))
            self.character = CharacterFactory()
        self.token = Token.objects.filter(
            user=self.character.user, character_id=self.character.character_id
        ).require_scopes(Character.esi_scopes()).first()
        self.assertIsNotNone(self.token)
        self.token_ids = set(Token.objects.filter(
            user=self.character.user, character_id=self.character.character_id
        ).values_list("pk", flat=True))
        self.ownership_pk = self.character.character_ownership.pk
        self.section = Character.UpdateSection.SKILLS
        self.session = Mock()
        self.session.refresh_token.return_value = {
            "access_token": "recovered-access", "refresh_token": "rotated-existing-grant",
        }
        self.session_patch = patch("esi.models.OAuth2Session", return_value=self.session)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        validation = patch.object(
            TokenManager, "validate_access_token",
            return_value={"owner": self.token.character_owner_hash},
        )
        validation.start()
        self.addCleanup(validation.stop)
        self.addCleanup(esi_status.clear_cache)
        self._expire_existing_tokens()

    def _expire_existing_tokens(self):
        Token.objects.filter(pk__in=self.token_ids).update(
            access_token="expired-access", refresh_token="existing-grant",
            created=now() - timedelta(hours=1),
        )

    def _pull_with_existing_token(self, **_kwargs):
        token = self.character.fetch_token(scopes=["esi-skills.read_skills.v1"])
        self.assertIn(token.pk, self.token_ids)
        self.assertEqual(token.access_token, "recovered-access")
        return UpdateSectionResult(
            is_changed=False, is_updated=True, data={"synthetic": True}
        )

    def _run_native_section(self):
        with patch.object(
            Character, "update_skills", side_effect=self._pull_with_existing_token
        ):
            memberaudit_tasks._update_character_section(
                character_pk=self.character.pk, section=self.section,
                force_update=False,
            )

    def test_outage_state_same_token_recovery_and_automatic_memberaudit_dispatch(self):
        self.session.refresh_token.side_effect = MissingTokenError()
        with self.assertRaises(IncompleteResponseError):
            self._run_native_section()
        status = self.character.update_status_for_section(self.section)
        self.assertFalse(status.is_success)
        self.assertFalse(status.has_token_error)
        self.assertIn("IncompleteResponseError", status.error_message)
        self.assertTrue(status.is_update_needed())
        self.character.refresh_from_db()
        self.assertIsNone(self.character.token_error_notified_at)

        # The normal scheduler dispatches the failed section without ignore_stale
        # or a new token. Mock only task submission and ESI route availability.
        with (
            patch.object(esi_status, "unavailable_sections", return_value=set()),
            ExitStack() as stack,
        ):
            queued = {
                section: stack.enter_context(patch.object(
                    memberaudit_tasks, f"update_character_{section.value}"
                ))
                for section in Character.UpdateSection.enabled_sections()
            }
            self.assertTrue(memberaudit_tasks.update_character(self.character.pk))
        queued[self.section].apply_async.assert_called_once()

        self.session.refresh_token.side_effect = None
        self._run_native_section()
        status.refresh_from_db()
        self.assertTrue(status.is_success)
        self.assertFalse(status.has_token_error)
        self.assertEqual(status.error_message, "")
        self.assertIsNotNone(status.update_finished_at)
        self.character.refresh_from_db()
        self.assertEqual(self.character.character_ownership.pk, self.ownership_pk)
        self.assertEqual(
            set(Token.objects.filter(
                user=self.character.user, character_id=self.character.character_id
            ).values_list("pk", flat=True)),
            self.token_ids,
        )

    def test_permanent_rejection_remains_blocked_and_is_not_automatically_requeued(self):
        from memberaudit.errors import TokenDoesNotExist

        self.session.refresh_token.side_effect = InvalidGrantError()
        with self.assertRaises(TokenDoesNotExist):
            self._run_native_section()
        status = self.character.update_status_for_section(self.section)
        self.assertFalse(status.is_success)
        self.assertTrue(status.has_token_error)
        self.assertFalse(status.is_update_needed())
        with (
            patch.object(esi_status, "unavailable_sections", return_value=set()),
            ExitStack() as stack,
        ):
            queued = {
                section: stack.enter_context(patch.object(
                    memberaudit_tasks, f"update_character_{section.value}"
                ))
                for section in Character.UpdateSection.enabled_sections()
            }
            memberaudit_tasks.update_character(self.character.pk)
        queued[self.section].apply_async.assert_not_called()
