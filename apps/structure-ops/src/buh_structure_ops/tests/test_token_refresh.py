"""Exercise real token/queryset code with synthetic rotating SSO credentials."""

from datetime import timedelta
from unittest.mock import Mock, patch

from app_utils.testing import NoSocketsTestCase
from app_utils.testdata_factories import EveCharacterFactory
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from esi.errors import IncompleteResponseError, TokenInvalidError
from esi.managers import TokenManager, TokenQueryset
from esi.models import Scope, Token
from oauthlib.oauth2.rfc6749.errors import InvalidGrantError, MissingTokenError

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
            self.assertIsNone(Token.objects.filter(pk=self.token.pk).require_valid().first())
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
        methods = (Token.refresh, Token.refresh_or_delete, TokenQueryset.bulk_refresh)
        self.assertFalse(install_token_refresh_guard())
        with patch("buh_structure_ops.token_refresh.esi.__version__", "9.6.1"):
            self.assertFalse(install_token_refresh_guard())
        self.assertEqual(methods, (Token.refresh, Token.refresh_or_delete, TokenQueryset.bulk_refresh))
