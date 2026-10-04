"""Targeted read-only finding collection keeps evidence complete and secret-free."""
import ast
import hashlib
import json
from pathlib import Path
import stat
from types import ModuleType, SimpleNamespace
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


class CollectorOrchestrationTests(unittest.TestCase):
    """Exercise the collector's real sequencing, not only log selection helpers."""

    target = LogEvidenceTests.target
    module = LogEvidenceTests.module

    def setUp(self):
        self.asset_target = self.target()
        self.structures_target = {'owner_pk': 11, 'sync_character_pk': 21}
        self.asset_selections = []
        self.diagnostic_selections = []
        self.assets_report = {'scan_complete': True, 'read_only': True, 'errors': []}
        config = SimpleNamespace(state_dir=Path('/fixture-state'), gunicorn_service='fixture_web',
                                 worker_service='fixture_worker', proxy_service='fixture_proxy',
                                 command_timeout_seconds=10)
        self.ids = {'fixture_web': ('a' * 64,), 'fixture_worker': ('b' * 64, 'c' * 64),
                    'fixture_proxy': ('d' * 64,)}
        self.host_stub = SimpleNamespace(config=config, auth_replica_counts={key: len(value)
                                        for key, value in self.ids.items()},
            _manage_live=mock.Mock(side_effect=self.asset_query),
            _running_service_containers=mock.Mock(side_effect=lambda role, **kwargs: self.ids[role]),
            _stream_log_lines=self.log_lines)
        module = self.module()
        module.DockerHost = SimpleNamespace(load_incomplete_plan=mock.Mock(
            return_value=(self.host_stub, {'attempt_id': collector.ATTEMPT})))
        self.baseline = {'fixture': 'already-verified'}
        self.roster = [{'owner_pk': 11}]
        self.pilot = SimpleNamespace(verify_host=mock.Mock(return_value={'platform_version': '0.8.2'}),
            latest_report=mock.Mock(return_value=(None, self.baseline, 'baseline-digest')),
            select_pilot=mock.Mock(return_value=self.structures_target),
            snapshot_targets=mock.Mock(return_value=self.roster))
        self.bridge = SimpleNamespace(qualified_pilot=mock.Mock(return_value=(self.pilot, 'owner-source')),
            snapshot=mock.Mock(return_value={'healthy': 12}), require_healthy_owners=mock.Mock())
        self.selection = SimpleNamespace(select_outage=mock.Mock(return_value=(['selected'], ['excluded'])))
        self.assessment = {'attempt_id': collector.ATTEMPT, 'hold_sha256': collector.PLAN_HASH,
            'bridge_source': '/fixture-bridge.py', 'pilot_directory': '/fixture-pilot',
            'baseline_report_sha256': 'baseline-digest', 'prior_recovery_report': '/fixture-prior.json',
            'memberaudit_directory': '/fixture-memberaudit'}
        self.helper = ModuleType('fixture_readonly_helper')
        self.helper.installed_identity = mock.Mock(return_value={'unchanged': True})
        self.helper.private_read = mock.Mock(side_effect=self.private_read)
        self.helper.load_source = mock.Mock(side_effect=lambda path, digest, name: {
            'ops.deploy.readonly_log_host': module, 'buh_readonly_log_bridge': self.bridge,
            'buh_readonly_log_selection': self.selection}[name])
        self.helper.ReceiverConfig = SimpleNamespace(load=mock.Mock(return_value=config))
        self.helper.verify_retained_plan = mock.Mock()
        self.helper.memberaudit_refs = mock.Mock(return_value=['fixture-ref'])
        self.helper.verify_memberaudit = mock.Mock(return_value={'characters': 87, 'successful_sections': 348,
            'older_section_count': 61, 'older_sections_unchanged': True})
        self.helper.DeploymentError = DeploymentError
        self.helper.BRIDGE_SHA256 = 'bridge-digest'
        self.helper.SELECTION_SHA256 = 'selection-digest'
        raw = b'pass\n'
        self.enterContext(mock.patch.object(collector.os, 'geteuid', return_value=0))
        self.enterContext(mock.patch.object(collector.Path, 'lstat', autospec=True, side_effect=lambda path:
            SimpleNamespace(st_mode=(stat.S_IFDIR | 0o700) if path == collector.STAGE else (stat.S_IFREG | 0o600),
                            st_uid=0, st_size=len(raw))))
        self.enterContext(mock.patch.object(collector.Path, 'read_bytes', return_value=raw))
        self.enterContext(mock.patch.object(collector, 'HELPER_HASH', collector.sha(raw)))
        self.enterContext(mock.patch.object(collector.importlib.util, 'module_from_spec', return_value=self.helper))
        self.enterContext(mock.patch.object(collector.sys, 'path', list(collector.sys.path)))
        self.enterContext(mock.patch.object(collector.sys, 'dont_write_bytecode', True))
        self.enterContext(mock.patch.object(collector.os, 'open', return_value=31))
        self.enterContext(mock.patch.object(collector.os, 'fstat', return_value=
            SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0)))
        self.flock = self.enterContext(mock.patch.object(collector.fcntl, 'flock'))
        self.close = self.enterContext(mock.patch.object(collector.os, 'close'))
        self.protection = self.enterContext(mock.patch.object(collector, 'protection', return_value={'unchanged': True}))
        self.enterContext(mock.patch.object(collector, 'collect_diagnostic_copy', side_effect=self.diagnostic_copy))

    def private_read(self, path, *args):
        value = {'assessment.json': self.assessment, 'fixture-prior.json': {
            'started_at': '2026-10-04T00:00:00+00:00'}, 'memberaudit-report.json': {'completed': True}}[path.name]
        return json.dumps(value).encode()

    def asset_query(self, *args, **kwargs):
        selected = ast.literal_eval(args[-1].rsplit('\ncapture_assets(', 1)[1][:-1])
        self.asset_selections.append(selected)
        # Reproduce the production KeyError if an unrelated Structures target
        # is accidentally supplied to the selected-character database query.
        selected['memberaudit_character_pk']
        return 'BUH_RETAINED_ASSETS_BEGIN\n' + json.dumps(self.assets_report) + '\nBUH_RETAINED_ASSETS_END'

    def log_lines(self, command, **kwargs):
        stamp = '2026-10-04T00:00:02.000Z '
        if command[-1] == self.ids['fixture_web'][0]:
            yield stamp + '[04/Oct/2026 00:00:02] DEBUG [esi.decorators:39] No callback for AnonymousUser session fixture'
        elif command[-1] in self.ids['fixture_worker']:
            yield stamp + '[04/Oct/2026 00:00:02] INFO [memberaudit.models.characters:489] FixtureCharacter (ID:9): assets: updated'
        else:
            yield stamp + '127.0.0.1 - - [04/Oct/2026:00:00:02 +0000] "GET /dashboard/ HTTP/1.1" 200 100'

    def diagnostic_copy(self, host, module, assessment, since, until, target):
        self.diagnostic_selections.append(target.copy())
        return {'read_only': True, 'complete': True, 'streams': []}

    def test_structures_snapshot_keeps_selected_assets_identity_through_final_preservation(self):
        original_target = self.asset_target.copy()
        result = collector.run(self.asset_target)
        self.assertTrue(result['scan_complete'], result.get('errors'))
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.asset_target, original_target)
        self.assertEqual(self.asset_selections, [original_target])
        self.assertEqual(self.diagnostic_selections, [original_target])
        self.bridge.snapshot.assert_called_once_with(
            self.host_stub, self.pilot, 'owner-source', self.structures_target, self.roster)
        self.assertEqual([row['selected_lines'] for row in result['logs']], [1, 1, 1])
        self.assertEqual(result['login_http'][0]['rows'][0]['status'], 200)
        self.assertEqual(result['memberaudit_preserved'], result['memberaudit_preserved_after'])
        self.assertTrue(result['protected_state_unchanged'])
        self.protection.assert_has_calls([mock.call(self.host_stub, self.helper)] * 2)
        self.close.assert_called_once_with(31)
        for key in ('tooling_installation_attempted', 'deployment_attempted', 'supported_recovery_attempted',
                    'token_refresh_attempted', 'memberaudit_mutation_attempted', 'structures_mutation_attempted'):
            self.assertFalse(result[key])

    def test_incomplete_assets_evidence_still_collects_logs_http_and_preservation_without_passing(self):
        self.assets_report = {'scan_complete': False, 'read_only': True, 'errors': [{'error_type': 'KeyError'}]}
        result = collector.run(self.asset_target)
        self.assertFalse(result['scan_complete'])
        self.assertEqual(len(result['logs']), 3)
        self.assertTrue(result['diagnostic_log_copy']['complete'])
        self.assertTrue(result['login_http'][0]['complete'])
        self.assertTrue(result['protected_state_unchanged'])
        self.helper.verify_memberaudit.assert_has_calls([
            mock.call(self.host_stub, ['fixture-ref'], ['excluded'], {'completed': True})] * 2)

    def test_worker_discovery_failure_records_a_site_keeps_resources_and_cannot_pass(self):
        self.host_stub._running_service_containers.side_effect = lambda role, **kwargs: (
            () if role == 'fixture_worker' else self.ids[role])
        result = collector.run(self.asset_target)
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['failure_phase'], 'read_only_exact_retained_logs')
        self.assertEqual(result['errors'][0]['error_type'], 'RuntimeError')
        self.assertEqual(result['errors'][0]['failure_sites'][-1]['function'], 'run')
        self.assertTrue(result['preserve_recovery_resources'])
        self.assertFalse(result['supported_recovery_attempted'])
        self.assertFalse(result['deployment_attempted'])
        self.close.assert_called_once_with(31)


if __name__ == '__main__':
    unittest.main()
