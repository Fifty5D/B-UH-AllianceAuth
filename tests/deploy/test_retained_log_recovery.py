"""Exact historical records qualify only with byte evidence and successful continuation."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import (
    ASSET_ERROR_FRAMES, ASSET_TASK_FRAMES, CALLBACK_LOOKUP_SHA256,
    DockerHost, LogScanError, WORKER_MEMORY_SCRIPT_SHA256, _prove_retained_findings,
)
from tests.deploy.test_docker_host import make_config
from tests.deploy import test_verifier_corrections as rehydration


def row(text, at="2026-10-04T00:00:01.000Z", *, session=None):
    return {"text": text, "at": at, "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "session_sha256": [session] if session else []}


def finding_case(config):
    """Synthetic counterparts of the captured full asset stack and caught DEBUG lookup."""
    worker, web, proxy = "1" * 64, "2" * 64, "3" * 64
    task = "00000000-0000-0000-0000-000000000001"
    since = "2026-10-03T18:56:59+00:00"
    review = {"attempt_id": "gh-123-1", "plan_sha256": "a" * 64, "verification_since": since,
              "containers": [{"container_id": identity, "service": service}
                for identity, service in ((worker, config.worker_service), (web, config.gunicorn_service),
                                          (proxy, config.proxy_service))]}
    proof = {"report_path": "/root/fixture/report.json", "report_sha256": "b" * 64,
             "asset_task_id": task, "asset_error_at": "2026-10-04T00:00:01.000Z"}
    asset_message = ("FixtureCharacter (ID:9): assets: Error occurred: HTTPClientError: "
        "<HTTPClientError 404 details=None error='Invalid IDs in the request' Headers("
        "[('x-esi-request-id', '00000000-0000-0000-0000-000000000002')])>")
    headers = ["[04/Oct/2026 00:00:01] ERROR [memberaudit.models.characters:551] " + asset_message,
               "[2026-10-04 00:00:01,001: ERROR/MainProcess] " + asset_message,
               "[2026-10-04 00:00:01,010: ERROR/MainProcess] Task memberaudit.tasks.assets_build_list_from_esi["
               + task + "] raised unexpected: HTTPError()"]
    worker_rows = []
    for index, header in enumerate(headers):
        frames = ASSET_ERROR_FRAMES if index < 2 else ASSET_TASK_FRAMES
        lines = [header, "Traceback (most recent call last):"]
        lines += [f'  File "/installed/site-packages/{path}", line 123, in {function}' for path, function in frames]
        lines += ["aiopenapi3.errors.HTTPError"]
        worker_rows += [row(line) for line in lines]
    session = "c" * 64
    callback_lines = [
        "[04/Oct/2026 00:00:01] DEBUG [esi.decorators:39] No callback for AnonymousUser session sha256:" + session,
        "Traceback (most recent call last):",
        '  File "/installed/esi/decorators.py", line 29, in _check_callback',
        "    CallbackRedirect.objects.get(session_key=redacted)", "    ^^^^^^^^^^^",
        '  File "/usr/local/lib/python3.12/site-packages/django/db/models/manager.py", line 87, in manager_method',
        "    return getattr(self.get_queryset(), name)(*args, **kwargs)", "    ^^^^^^^^^^^",
        '  File "/usr/local/lib/python3.12/site-packages/django/db/models/query.py", line 635, in get',
        "    raise self.model.DoesNotExist(",
        "esi.models.CallbackRedirect.DoesNotExist: CallbackRedirect matching query does not exist."]
    web_rows = [row(line, session=session if index == 0 else None) for index, line in enumerate(callback_lines)]
    web_rows += [row("[04/Oct/2026 00:00:01] DEBUG [esi.decorators:60] Redirecting AnonymousUser session sha256:"
                    + session + " to SSO.", "2026-10-04T00:00:01.010Z", session=session),
                 row("[04/Oct/2026 00:00:09] DEBUG [esi.views:91] Processed callback for AnonymousUser session sha256:"
                    + session + ". Redirecting to /sso/login", "2026-10-04T00:00:09.500Z", session=session),
                 row("[04/Oct/2026 00:00:10] DEBUG [esi.decorators:52] Got new token from AnonymousUser session sha256:"
                    + session + ". Returning to view.", "2026-10-04T00:00:10.000Z", session=session)]
    preserved = {"characters": 87, "successful_sections": 348, "auth_and_token_inventory_preserved": True,
                 "older_sections_unchanged": True, "older_section_count": 61}
    boundary = {"hold_sha256": review["plan_sha256"], "platform_version": "0.8.2",
                "public_smoke_passed": True, "traffic_only_verified_previous_image": True}
    report = {"schema_version": 1, "attempt_id": review["attempt_id"], "scan_complete": True,
              "read_only": True, "diagnostic_only": True, "errors": [], "protected_state_unchanged": True,
              "before_host": boundary, "after_host": copy.deepcopy(boundary),
              "memberaudit_preserved": preserved, "memberaudit_preserved_after": copy.deepcopy(preserved),
              "selected_character": {"scan_complete": True, "errors": [], "later_successful_assets_run": True,
                  "identity": {"character_name": "FixtureCharacter", "character_id": 1000001,
                      "memberaudit_character_pk": 9, "token_pk": 12, "auth_link_pk": 7, "user_id": 3,
                      "token_inventory": [12], "is_disabled": False, "same_token_character": True,
                      "same_token_owner": True, "same_token_user": True, "required_assets_scope_present": True},
                  "assets_status": {"pk": 44, "section": "assets", "is_success": True, "has_token_error": False,
                      "error_length": 0, "content_hashes": ["d" * 32],
                      "run_finished_at": "2026-10-04T01:00:00+00:00", "update_finished_at": "2026-10-04T00:10:00+00:00"},
                  "current_assets": {"all_rows_metadata_valid": True, "relationship_issues": {"invalid_item_ids": 0}},
                  "historical_asset_payloads": [{"actual_hash_matches": True, "actual_size_matches": True, "format_valid": True}],
                  "later_asset_payloads": [{"actual_hash_matches": True, "actual_size_matches": True, "format_valid": True}],
                  "versions": {"django-esi": "9.6.0"}, "installed_sources": {"callback_lookup": {
                      "filename": "/installed/esi/decorators.py", "sha256": CALLBACK_LOOKUP_SHA256}}},
              "logs": [{"source": config.worker_service + "/" + worker, "complete": True, "since": since, "rows": worker_rows},
                       {"source": config.gunicorn_service + "/" + web, "complete": True, "since": since, "rows": web_rows}],
              "login_http": [{"complete": True, "container_id": proxy, "rows": [
                  {"path": "/sso/login", "status": 302, "at": "2026-10-04T00:00:01.100Z"},
                  {"path": "/sso/callback/", "status": 302, "at": "2026-10-04T00:00:10.100Z"},
                  {"path": "/dashboard/", "status": 200, "at": "2026-10-04T00:00:11.100Z"}]}]}
    for key in ("deployment_attempted", "tooling_installation_attempted", "supported_recovery_attempted",
                "token_refresh_attempted", "memberaudit_mutation_attempted", "structures_mutation_attempted"):
        report[key] = False
    return report, review, proof


class RetainedFindingProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = make_config(Path(self.temp.name))
        self.report, self.review, self.proof = finding_case(self.config)

    def prove(self):
        return _prove_retained_findings(self.report, self.review, self.config, self.proof)

    def test_exact_recovered_asset_records_and_handled_callback_retain_original_evidence(self):
        records, warnings = self.prove()
        self.assertEqual(sum(map(len, records.values())), 4)
        self.assertEqual(len(warnings), 2)
        self.assertIn(self.proof['asset_error_at'], warnings[0])
        self.assertIn(self.report['logs'][1]['rows'][0]['raw_sha256'], warnings[1])
        self.assertTrue(all('/' in source for source, header in records))

    def test_asset_identity_state_byte_or_actual_update_mismatch_blocks(self):
        original = copy.deepcopy(self.report)
        for section, key, value in (
                ('identity', 'same_token_owner', False), ('identity', 'required_assets_scope_present', False),
                ('assets_status', 'is_success', False), ('assets_status', 'has_token_error', True),
                ('assets_status', 'error_length', 1), ('assets_status', 'update_finished_at', self.proof['asset_error_at']),
                ('current_assets', 'all_rows_metadata_valid', False),
                ('current_assets', 'relationship_issues', {'missing_types': 1})):
            with self.subTest(key=key):
                self.report = copy.deepcopy(original)
                self.report['selected_character'][section][key] = value
                with self.assertRaises(DeploymentError):
                    self.prove()
        for group, key in (('historical_asset_payloads', 'actual_hash_matches'), ('later_asset_payloads', 'format_valid')):
            self.report = copy.deepcopy(original)
            self.report['selected_character'][group][0][key] = False
            with self.assertRaises(DeploymentError):
                self.prove()

    def test_asset_other_character_request_task_frame_or_container_is_not_recovered(self):
        original = copy.deepcopy(self.report)
        for old, new in (('FixtureCharacter', 'DifferentCharacter'), ('ID:9', 'ID:99'),
                         ("00000000-0000-0000-0000-000000000002", "different-request"),
                         ('in _fetching_asset_names_from_esi', 'in unexpected_function')):
            self.report = copy.deepcopy(original)
            self.report['logs'][0]['rows'][0 if 'in ' not in old else 20]['text'] = (
                self.report['logs'][0]['rows'][0 if 'in ' not in old else 20]['text'].replace(old, new))
            if 'in ' in old:
                for entry in self.report['logs'][0]['rows']:
                    if old in entry['text']:
                        entry['text'] = entry['text'].replace(old, new)
                        break
            with self.subTest(old=old), self.assertRaises(DeploymentError):
                self.prove()
        self.report = copy.deepcopy(original)
        self.proof['asset_task_id'] = '00000000-0000-0000-0000-000000000099'
        with self.assertRaises(DeploymentError):
            self.prove()
        self.proof['asset_task_id'] = '00000000-0000-0000-0000-000000000001'
        self.report['logs'][0]['source'] = self.config.worker_service + '/' + 'f' * 64
        with self.assertRaises(DeploymentError):
            self.prove()

    def test_callback_severity_frame_exception_source_or_continuation_near_misses_block(self):
        original = copy.deepcopy(self.report)
        cases = [(0, 'DEBUG', 'ERROR'), (0, 'esi.decorators:39', 'esi.decorators:40'),
                 (2, '_check_callback', '_unexpected_callback'), (8, 'line 635', 'line 636'),
                 (10, 'CallbackRedirect.DoesNotExist', 'Token.DoesNotExist')]
        for index, old, new in cases:
            self.report = copy.deepcopy(original)
            self.report['logs'][1]['rows'][index]['text'] = self.report['logs'][1]['rows'][index]['text'].replace(old, new)
            with self.subTest(index=index), self.assertRaises(DeploymentError):
                self.prove()
        self.report = copy.deepcopy(original)
        self.report['logs'][1]['rows'][-1]['session_sha256'] = ['f' * 64]
        with self.assertRaises(DeploymentError):
            self.prove()
        self.report = copy.deepcopy(original)
        self.report['login_http'][0]['rows'][-1]['status'] = 500
        with self.assertRaises(DeploymentError):
            self.prove()
        self.report = copy.deepcopy(original)
        self.report['selected_character']['installed_sources']['callback_lookup']['sha256'] = 'f' * 64
        with self.assertRaises(DeploymentError):
            self.prove()

    def test_delayed_same_session_requires_immediate_normal_redirect_and_later_authentication(self):
        self.report['logs'][1]['rows'][-1]['at'] = '2026-10-04T03:35:00.000Z'
        self.report['logs'][1]['rows'][-2]['at'] = '2026-10-04T03:34:59.500Z'
        self.report['login_http'][0]['rows'][1]['at'] = '2026-10-04T03:35:00.100Z'
        self.report['login_http'][0]['rows'][2]['at'] = '2026-10-04T03:35:01.100Z'
        self.assertTrue(self.prove()[0])
        self.report['login_http'][0]['rows'].pop(0)
        with self.assertRaises(DeploymentError):
            self.prove()

    def test_existing_token_selection_requires_successful_choice_view_and_http_completion(self):
        stream = self.report['logs'][1]
        session = stream['rows'][0]['session_sha256'][0]
        stream['rows'] = stream['rows'][:11] + [
            row('[04/Oct/2026 00:00:01] DEBUG [esi.decorators:86] Retrieved 2 tokens for AnonymousUser session sha256:'
                + session, '2026-10-04T00:00:01.020Z', session=session),
            row('[04/Oct/2026 00:00:02] DEBUG [esi.decorators:142] AnonymousUser has selected token 12',
                '2026-10-04T00:00:02.000Z'),
            row('[04/Oct/2026 00:00:02] DEBUG [esi.decorators:159] Selected token fulfills requirements of view. Returning.',
                '2026-10-04T00:00:02.010Z'),
            row('[04/Oct/2026 00:00:02] INFO [allianceauth.authentication.views:172] Changed user AnonymousUser main character to Fixture',
                '2026-10-04T00:00:02.020Z')]
        self.assertTrue(self.prove()[0])
        stream['rows'].pop()
        with self.assertRaises(DeploymentError):
            self.prove()

    def test_incomplete_stream_or_changed_preservation_or_attempt_blocks(self):
        original = copy.deepcopy(self.report)
        for key, value in (('attempt_id', 'gh-999-1'), ('protected_state_unchanged', False), ('scan_complete', False)):
            self.report = copy.deepcopy(original)
            self.report[key] = value
            with self.assertRaises(DeploymentError):
                self.prove()
        self.report = copy.deepcopy(original)
        self.report['logs'][0]['complete'] = False
        with self.assertRaises(DeploymentError):
            self.prove()
        self.report = copy.deepcopy(original)
        self.report['memberaudit_preserved_after']['older_sections_unchanged'] = False
        with self.assertRaises(DeploymentError):
            self.prove()

    def test_live_scan_requires_whole_exact_records_and_limits_duplicate_occurrences(self):
        host = DockerHost(self.config)
        host.recovered_retained_log_records, warnings = self.prove()
        for stream in self.report['logs']:
            text = '\n'.join(entry['text'] for entry in stream['rows'])
            self.assertEqual(host._classify_log_batch([(stream['source'], text)], set(), None), ())
        callback = self.report['logs'][1]
        text = '\n'.join(entry['text'] for entry in callback['rows'][:11])
        with self.assertRaises(LogScanError):
            host._classify_log_batch([(callback['source'], text)], set(), None)
        host.recovered_retained_log_seen = {}
        with self.assertRaises(LogScanError):
            host._classify_log_batch([(callback['source'], text.replace('line 29', 'line 30'))], set(), None)
        with self.assertRaises(LogScanError):
            host._classify_log_batch([(self.config.gunicorn_service + '/' + 'f' * 64, text)], set(), None)
        with self.assertRaises(LogScanError):
            host._classify_log_batch([(callback['source'], text + '\nTraceback unexpected appended failure')], set(), None)
        self.assertEqual(len(warnings), 2)

    def test_identical_same_second_records_are_counted_not_globally_ignored(self):
        stream = self.report['logs'][1]
        duplicate = copy.deepcopy(stream['rows'][:11])
        duplicate[0]['at'] = '2026-10-04T00:00:01.001Z'
        stream['rows'][11:11] = duplicate
        host = DockerHost(self.config)
        host.recovered_retained_log_records, _ = self.prove()
        text = '\n'.join(entry['text'] for entry in stream['rows'][:11])
        self.assertEqual(host._classify_log_batch([(stream['source'], text + '\n' + text)], set(), None), ())
        with self.assertRaises(LogScanError):
            host._classify_log_batch([(stream['source'], text)], set(), None)

    def test_large_log_batches_preserve_whole_callback_record(self):
        host = DockerHost(self.config)
        host.recovered_retained_log_records, _ = self.prove()
        stream = self.report['logs'][1]
        lines = ['[04/Oct/2026 00:00:00] INFO [fixture:1] ' + 'x' * 60000]
        lines += [entry['text'] for entry in stream['rows'][:11]]
        lines += ['[04/Oct/2026 00:00:02] INFO [fixture:1] ' + 'x' * 10000]
        for batch in host._log_batches(iter(lines), owner_scoped=True, deadline=time.monotonic()+30):
            self.assertEqual(host._classify_log_batch([(stream['source'], batch)], set(), None), ())

    def test_runtime_report_digest_and_current_success_gate_before_any_exception_is_active(self):
        host = DockerHost(self.config)
        raw = json.dumps(self.report).encode()
        self.proof['report_sha256'] = hashlib.sha256(raw).hexdigest()
        host.retained_verifier_review = {**self.review, 'retained_log_findings': self.proof}
        good = {'read_only': True, 'assets_successful': True, 'assets_relationships_valid': True,
                'identity_and_token_inventory_preserved': True, 'callback_source_verified': True}
        def output(value):
            return 'BUH_RETAINED_FINDINGS_CURRENT_BEGIN\n'+json.dumps(value)+'\nBUH_RETAINED_FINDINGS_CURRENT_END'
        with mock.patch.object(host, '_worker_recycle_private_bytes', return_value=raw), mock.patch.object(
                host, '_manage_live', return_value=output(good)) as current:
            self.assertEqual(len(host._verified_retained_log_findings()), 2)
            self.assertIn('SET TRANSACTION READ ONLY', current.call_args.args[-1])
        with mock.patch.object(host, '_worker_recycle_private_bytes', return_value=raw), mock.patch.object(
                host, '_manage_live', return_value=output({**good, 'assets_successful': False})):
            with self.assertRaises(DeploymentError):
                host._verified_retained_log_findings()
            self.assertEqual(host.recovered_retained_log_records, {})
        with mock.patch.object(host, '_worker_recycle_private_bytes', return_value=raw+b' '), mock.patch.object(
                host, '_manage_live') as current:
            with self.assertRaises(DeploymentError):
                host._verified_retained_log_findings()
            current.assert_not_called()


class RetainedFindingReviewTests(unittest.TestCase):
    setUp = rehydration.RestartRehydrationTests.setUp
    legacy = rehydration.RestartRehydrationTests.legacy
    review = rehydration.RestartRehydrationTests.review
    save_review = rehydration.RestartRehydrationTests.save_review
    health = rehydration.RestartRehydrationTests.health

    def test_schema_three_preserves_attempt_bound_restart_baseline_and_rejects_unproven_changes(self):
        plan = self.legacy()
        review = self.review(plan)
        _, _, proof = finding_case(self.original.config)
        policy = {"schema_version": 1, "reviewed_evidence_sha256": "a" * 64,
                  "script_sha256": WORKER_MEMORY_SCRIPT_SHA256, "host_boot_id": "b" * 32,
                  "diagnostics_store_id": "e" * 32,
                  "workers": {row["container_id"]: "fixture-worker" for row in review["containers"]
                              if row["service"] == self.original.config.worker_service},
                  "daemons": {unit: {"MainPID": "915", "ExecMainStartTimestampMonotonic": "6116073"}
                              for unit in ("docker.service", "containerd.service")}}
        review.update(schema_version=3, worker_memory_recycles=policy, retained_log_findings=proof)
        path = self.save_review(review)
        with rehydration.simulated_root_owned_lstat(path):
            loaded, _ = DockerHost.load_incomplete_plan(self.original.config)
        self.assertEqual(loaded.restart_baselines, self.original.restart_baselines)
        self.assertEqual(loaded.retained_verifier_review['retained_log_findings'], proof)
        self.assertTrue(self.health(loaded))
        self.assertFalse(self.health(loaded, restart_delta=1))
        self.assertFalse(self.health(loaded, identity_change=True))
        self.assertFalse(self.health(loaded, image_change=True))
        original = copy.deepcopy(review)
        for key, value in (('attempt_id', 'gh-999-1'), ('plan_sha256', 'f' * 64), ('worker_memory_recycles', None)):
            review = copy.deepcopy(original)
            review[key] = value
            path = self.save_review(review)
            with rehydration.simulated_root_owned_lstat(path), self.assertRaises(DeploymentError):
                DockerHost.load_incomplete_plan(self.original.config)


if __name__ == '__main__':
    unittest.main()
