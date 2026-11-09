"""Execute the actual scoped native program with controlled persistence/provider seams."""
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from ops.deploy.docker_host import EXISTING_LOCATION_RECOVERY_CODE


class ExistingLocationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.scopes = ['esi-location.read_location.v1']
        self.expected = {'identity': {'token_pk': 9, 'memberaudit_character_pk': 7,
                         'auth_link_pk': 8, 'user_id': 6, 'character_id': 123,
                         'owner_hash_sha256': hashlib.sha256(b'fixture-owner').hexdigest(),
                         'token_inventory': [9], 'required_scopes': self.scopes},
                         'status': {'id': 10, 'original_error_message_sha256': hashlib.sha256(b'stale').hexdigest(),
                                    'run_finished_at': '2026-01-01T00:00:00.123Z'}}
        self.fields = [SimpleNamespace(name=name, attname=name) for name in
                       ('pk', 'character_id', 'user_id', 'character_owner_hash', 'access_token', 'refresh_token', 'created', 'sso_version')]
        self.token = SimpleNamespace(pk=9, character_id=123, user_id=6,
                        character_owner_hash='fixture-owner', access_token='old-private-access',
                        refresh_token='old-private-refresh', created=None, sso_version=2,
                        scopes=SimpleNamespace(values_list=lambda *args, **kwargs: self.scopes),
                        refresh_from_db=lambda: None)
        self.ownership = SimpleNamespace(pk=8, owner_hash='fixture-owner', user_id=6,
                                        character_id=11, refresh_from_db=lambda: None)
        self.member = SimpleNamespace(pk=7, is_disabled=False, eve_character_id=11,
                                      eve_character=SimpleNamespace(character_id=123))
        self.status = SimpleNamespace(pk=10, character_id=7, section='location', is_success=False,
                        has_token_error=True, error_message='stale',
                        run_finished_at=datetime(2026, 1, 1, microsecond=123000, tzinfo=timezone.utc),
                        run_started_at=None, update_started_at=None, update_finished_at=None,
                        content_hash_1='', content_hash_2='', content_hash_3='', refresh_from_db=lambda: None)
        self.location = SimpleNamespace(pk=12, eve_solar_system_id=30000123,
                                        _meta=SimpleNamespace(fields=[]))
        self.native_update = mock.Mock(side_effect=self.persist_location)
        self.provider_refresh = mock.Mock(return_value={'access_token': 'new-private-access',
                                                       'refresh_token': 'new-private-refresh'})
        self.validate = mock.Mock(return_value={'character_id': 123, 'owner': 'fixture-owner', 'scp': self.scopes})
        self.refresh_calls = mock.Mock()
        self.saved_tokens = []
        self.signals = []
        class Signal:
            def connect(inner, callback, sender, **kwargs):
                self.signals.append((inner, sender, callback))
            def disconnect(inner, callback, sender):
                self.signals.remove((inner, sender, callback))
        self.pre_save, self.pre_delete = Signal(), Signal()
        query = SimpleNamespace(order_by=lambda *args: SimpleNamespace(values_list=lambda *args, **kwargs: [9]))
        def model(item):
            manager = SimpleNamespace(get=mock.Mock(return_value=item), filter=mock.Mock(return_value=query))
            manager.select_related = lambda *args: manager
            manager.select_for_update = lambda: manager
            return SimpleNamespace(objects=manager)
        self.token_model = model(self.token)
        self.token_model._meta = SimpleNamespace(concrete_fields=self.fields)
        self.auth_model = model(self.ownership)
        self.char_model = model(self.member)
        self.char_model.UpdateSection = SimpleNamespace(LOCATION='location')
        self.status_model = model(self.status)
        self.location_model = model(self.location)
        test = self
        class NativeSession:
            def __init__(inner, *args, **kwargs):
                pass
            def __enter__(inner):
                return inner
            def __exit__(inner, *args):
                pass
            def refresh_token(inner, *args, **kwargs):
                return test.provider_refresh(*args, **kwargs)
        def native_refresh(*, session, auth):
            self.refresh_calls()
            payload = session.refresh_token('fixture-provider', refresh_token=self.token.refresh_token, auth=auth)
            self.token.access_token, self.token.refresh_token = payload['access_token'], payload['refresh_token']
            for event, sender, callback in list(self.signals):
                if event is self.pre_save and sender is self.token_model:
                    callback(sender=sender, instance=self.token)
            self.saved_tokens.append(self.token.pk)
        self.token.refresh = native_refresh
        self.modules = {}
        def module(name, **fields):
            item = ModuleType(name)
            item.__dict__.update(fields)
            self.modules[name] = item
            if '.' in name:
                parent, attr = name.rsplit('.', 1)
                if parent not in self.modules:
                    module(parent)
                setattr(self.modules[parent], attr, item)
            return item
        module('signal', SIGALRM=14, signal=mock.Mock(), alarm=mock.Mock())
        module('django.core.serializers.json', DjangoJSONEncoder=Encoder)
        module('django.db', transaction=SimpleNamespace(atomic=nullcontext))
        module('django.db.models.signals', pre_save=self.pre_save, pre_delete=self.pre_delete)
        module('allianceauth.authentication.models', CharacterOwnership=self.auth_model)
        module('esi.app_settings', ESI_SSO_CLIENT_ID='fixture-client', ESI_SSO_CLIENT_SECRET='fixture-private-secret')
        module('esi.managers', TokenManager=SimpleNamespace(validate_access_token=self.validate),
               requests=SimpleNamespace(get=mock.Mock()))
        module('esi.models', Token=self.token_model)
        module('memberaudit.models', Character=self.char_model, CharacterLocation=self.location_model,
               CharacterUpdateStatus=self.status_model)
        module('memberaudit.tasks', _update_character_section=self.native_update)
        module('requests.auth', HTTPBasicAuth=lambda *args: SimpleNamespace())
        module('requests_oauthlib', OAuth2Session=NativeSession)

    def persist_location(self, character_pk, section, *, force_update):
        self.assertEqual((character_pk, section, force_update), (7, 'location', True))
        now = datetime.now(timezone.utc) + timedelta(milliseconds=1)
        self.status.is_success, self.status.has_token_error = True, False
        self.status.error_message, self.status.content_hash_1 = '', 'persisted-location-hash'
        self.status.run_started_at = self.status.update_started_at = now
        self.status.run_finished_at = self.status.update_finished_at = now

    def run_program(self, expected=None):
        output = io.StringIO()
        namespace = {}
        with mock.patch.dict('sys.modules', self.modules), mock.patch.object(logging, 'disable'), redirect_stdout(output):
            exec(compile(EXISTING_LOCATION_RECOVERY_CODE, '<scoped-native-recovery>', 'exec'), namespace)
            namespace['recover_existing_location'](expected or self.expected)
        self.assertEqual(self.signals, [])
        text = output.getvalue()
        self.assertNotIn('private-access', text)
        self.assertNotIn('private-refresh', text)
        self.assertNotIn('private-secret', text)
        return json.loads(text.split('BUH_EXISTING_LOCATION_RECOVERY_BEGIN\n')[1].split('\nBUH_EXISTING_LOCATION_RECOVERY_END')[0])

    def test_existing_validated_refresh_and_actual_native_location_persist_once(self):
        result = self.run_program()
        self.assertTrue(result['complete'])
        self.assertEqual(self.refresh_calls.call_count, 1)
        self.assertEqual(self.provider_refresh.call_count, 1)
        self.assertEqual(self.saved_tokens, [9])
        self.native_update.assert_called_once_with(7, 'location', force_update=True)
        self.assertFalse(result['has_token_error'])
        self.assertTrue(result['is_success'])

    def test_wrong_identity_or_missing_scope_never_saves_or_updates(self):
        for claims in ({'character_id': 124, 'owner': 'fixture-owner', 'scp': self.scopes},
                       {'character_id': 123, 'owner': 'another-owner', 'scp': self.scopes},
                       {'character_id': 123, 'owner': 'fixture-owner', 'scp': []}, None):
            with self.subTest(claims=claims):
                self.validate.return_value = claims
                result = self.run_program()
                self.assertFalse(result['complete'])
                self.assertEqual(self.saved_tokens, [])
                self.native_update.assert_not_called()

    def test_incomplete_provider_refresh_preserves_existing_token_and_stops(self):
        for payload in ({'access_token': 'new-private-access'}, {'refresh_token': 'new-private-refresh'}, None):
            with self.subTest(payload=payload):
                self.provider_refresh.return_value = payload
                result = self.run_program()
                self.assertFalse(result['complete'])
                self.assertEqual(self.token.access_token, 'old-private-access')
                self.assertEqual(self.saved_tokens, [])
                self.native_update.assert_not_called()

    def test_native_permanent_rejection_keeps_exact_safe_reason_and_never_updates(self):
        rejection = RuntimeError('private provider response')
        rejection.error = 'invalid_grant'
        wrapped = RuntimeError('native TokenInvalidError')
        wrapped.__cause__ = rejection
        self.provider_refresh.side_effect = wrapped
        result = self.run_program()
        self.assertFalse(result['complete'])
        self.assertEqual(result['permanent_oauth_reason'], 'invalid_grant')
        self.assertEqual(self.saved_tokens, [])
        self.native_update.assert_not_called()

    def test_changed_stale_status_stops_before_refresh(self):
        self.status.error_message = 'different current defect'
        result = self.run_program()
        self.assertFalse(result['complete'])
        self.refresh_calls.assert_not_called()
        self.native_update.assert_not_called()

    def test_current_scope_or_inventory_change_stops_before_refresh(self):
        changed = deepcopy(self.expected)
        changed['identity']['token_inventory'] = [9, 13]
        result = self.run_program(changed)
        self.assertFalse(result['complete'])
        self.refresh_calls.assert_not_called()
        self.native_update.assert_not_called()
        self.scopes = []
        result = self.run_program()
        self.assertFalse(result['complete'])
        self.refresh_calls.assert_not_called()

    def test_other_section_success_does_not_substitute_for_location(self):
        self.native_update.side_effect = None
        result = self.run_program()
        self.assertFalse(result['complete'])
        self.assertEqual(self.native_update.call_count, 1)
        self.assertTrue(self.status.has_token_error)

    def test_bad_persisted_location_stops_without_retry(self):
        self.location.eve_solar_system_id = None
        result = self.run_program()
        self.assertFalse(result['complete'])
        self.assertEqual(self.native_update.call_count, 1)
        self.assertEqual(self.refresh_calls.call_count, 1)


class Encoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, datetime):
            return value.isoformat()
        return super().default(value)
