"""Targeted read-only finding collection keeps evidence complete and secret-free."""
import hashlib
from types import SimpleNamespace
import unittest
from unittest import mock

from ops.deploy import collect_retained_log_findings as collector
from ops.deploy.contracts import DeploymentError, redact_sensitive_text
from ops.deploy.docker_host import ALLIANCEAUTH_LOG_HEADER_RE, CELERY_LOG_HEADER_RE


class LogEvidenceTests(unittest.TestCase):
    def target(self):
        return {'character_name': 'FixtureCharacter', 'character_id': 1000001,
                'memberaudit_character_pk': 9, 'token_pk': 12,
                'original_assets_task_id': '00000000-0000-0000-0000-000000000001',
                'asset_error_at': '2026-10-04T00:00:01.000Z'}

    def module(self):
        return SimpleNamespace(DeploymentError=DeploymentError,
            ALLIANCEAUTH_LOG_HEADER_RE=ALLIANCEAUTH_LOG_HEADER_RE,
            CELERY_LOG_HEADER_RE=CELERY_LOG_HEADER_RE,
            redact_sensitive_text=redact_sensitive_text)

    def host(self, lines):
        return SimpleNamespace(config=SimpleNamespace(command_timeout_seconds=120),
            _stream_log_lines=lambda *args, **kwargs: (line for line in lines))

    def collect(self, lines, web=True):
        return collector.collect_stream(self.host(lines), self.module(), 'fixture/' + 'a' * 64,
            ['docker', 'logs', 'fixture'], web=web, since='2026-10-04T00:00:00+00:00',
            until='2026-10-04T00:10:00+00:00', target=self.target())

    def test_callback_session_correlates_without_exposing_session_or_query(self):
        lines = [
            '2026-10-04T00:00:01.000Z [04/Oct/2026 00:00:01] DEBUG [esi.decorators:39] No callback for AnonymousUser session test123',
            '2026-10-04T00:00:02.000Z [04/Oct/2026 00:00:02] DEBUG [esi.views:91] Processed callback for AnonymousUser session test123. Redirecting to /sso/login?code=private-code&state=private-state',
        ]
        result = self.collect(lines)
        self.assertTrue(result['complete'])
        self.assertEqual(result['rows'][0]['session_sha256'], result['rows'][1]['session_sha256'])
        self.assertEqual(result['rows'][0]['raw_sha256'], hashlib.sha256(lines[0].partition(' ')[2].encode()).hexdigest())
        for row in result['rows']:
            self.assertNotIn('test123', row['text'])
            self.assertNotIn('private-code', row['text'])
            self.assertNotIn('private-state', row['text'])

    def test_complete_debug_traceback_is_retained(self):
        result = self.collect([
            '2026-10-04T00:00:01.000Z [04/Oct/2026 00:00:01] DEBUG [esi.decorators:39] No callback for AnonymousUser session fixture',
            '2026-10-04T00:00:01.001Z Traceback (most recent call last):',
            '2026-10-04T00:00:01.002Z   File "/installed/esi/decorators.py", line 29, in _check_callback',
            '2026-10-04T00:00:01.003Z esi.models.CallbackRedirect.DoesNotExist: CallbackRedirect matching query does not exist.',
            '2026-10-04T00:00:02.000Z [04/Oct/2026 00:00:02] DEBUG [other.logger:1] irrelevant record',
            '2026-10-04T00:00:02.001Z irrelevant continuation',
        ])
        self.assertEqual(result['selected_lines'], 4)
        self.assertIn('in _check_callback', result['rows'][2]['text'])
        self.assertIn('DoesNotExist', result['rows'][3]['text'])

    def test_selected_traceback_and_exact_task_are_selected_not_other_characters(self):
        result = self.collect([
            '2026-10-04T00:00:01.000Z [04/Oct/2026 00:00:01] ERROR [memberaudit.models.characters:551] FixtureCharacter (ID:9): assets: Invalid IDs in the request',
            '2026-10-04T00:00:01.001Z Traceback (most recent call last):',
            '2026-10-04T00:00:01.002Z unexpected frame retained for review',
            '2026-10-04T00:00:02.000Z [04/Oct/2026 00:00:02] INFO [memberaudit.models.characters:489] OtherCharacter (ID:99): assets: updated',
            '2026-10-04T00:00:03.000Z [2026-10-04 00:00:03,000: ERROR/MainProcess] Task memberaudit.tasks.assets_build_list_from_esi[00000000-0000-0000-0000-000000000001] raised unexpected: HTTPError()',
            '2026-10-04T00:00:03.001Z Traceback (most recent call last):',
        ], web=False)
        self.assertEqual(result['selected_lines'], 5)
        self.assertFalse(any('OtherCharacter' in row['text'] for row in result['rows']))
        self.assertTrue(any('unexpected frame retained' in row['text'] for row in result['rows']))

    def test_unknown_timestamp_or_partial_stream_cannot_claim_complete(self):
        with self.assertRaisesRegex(DeploymentError, 'incomplete'):
            self.collect(['not-a-timestamp Traceback (most recent call last):'])

    def test_raw_token_claims_are_not_exported(self):
        result = self.collect([
            '2026-10-04T00:00:01.000Z [04/Oct/2026 00:00:01] DEBUG [esi.views:73] Received callback for AnonymousUser session fixture',
            "2026-10-04T00:00:01.001Z {'scp': ['publicData'], 'jti': 'private-claim'}",
            '2026-10-04T00:00:01.002Z access_token=private-token',
        ])
        self.assertEqual(result['selected_lines'], 1)

    def test_failure_does_not_authorize_recovery_or_claim_current_health(self):
        with mock.patch.object(collector.os, 'geteuid', return_value=1234):
            result = collector.run(self.target())
        self.assertFalse(result['scan_complete'])
        self.assertTrue(result['read_only'])
        for key in ('tooling_installation_attempted', 'deployment_attempted', 'supported_recovery_attempted',
                    'token_refresh_attempted', 'memberaudit_mutation_attempted', 'structures_mutation_attempted'):
            self.assertFalse(result[key])
        self.assertTrue(result['preserve_recovery_resources'])

    def test_embedded_database_query_compiles_and_only_returns_evidence(self):
        compile(collector.ASSET_READ_CODE, '<read-only asset query>', 'exec')

    def test_invalid_target_stops_before_production_access(self):
        for change in ({'token_pk': 0}, {'token_pk': True}, {'character_name': 'bad\nname'},
                       {'original_assets_task_id': '--bad'}, {'asset_error_at': 'not-a-time'}):
            with self.subTest(change=change):
                target = self.target() | change
                with self.assertRaises(ValueError):
                    collector.validate_target(target)


if __name__ == '__main__':
    unittest.main()
