"""Recovery decisions need complete, same-operation, later and current proof."""
from copy import deepcopy
import ast
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import (
    DockerHost,
    LogScanError,
    RetainedSemanticAnalyzer,
    SemanticRecoveryLedger,
    _semantic_log_records,
    prove_semantic_recovery,
    prove_systemic_provider_recovery,
)


def source(text, name):
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "filename": "/installed/" + name.replace(".", "/") + ".py"}


SOURCES = {
    "esi.openapi_clients": source('''
from aiopenapi3.errors import HTTPServerError as base_HTTPServerError
from esi.exceptions import HTTPServerError
from aiopenapi3.errors import HTTPClientError as base_HTTPClientError
from esi.exceptions import HTTPClientError
def result():
    try:
        request()
    except base_HTTPServerError as e:
        raise HTTPServerError(status_code=e.status_code, headers=e.headers, data=e.data)
    except base_HTTPClientError as e:
        raise HTTPClientError(status_code=e.status_code, headers=e.headers, data=e.data)
''', "esi.openapi_clients"),
    "moonmining.tasks": source('''
def update_owner(owner_pk):
    return chain(update_refineries_from_esi_for_owner.si(owner_pk),
                 fetch_notifications_from_esi_for_owner.si(owner_pk),
                 update_extractions_for_owner.si(owner_pk),
                 mark_successful_update_for_owner.si(owner_pk))
def mark_successful_update_for_owner(owner_pk):
    owner.last_update_ok = True
    owner.save()
''', "moonmining.tasks"),
    "structures.models.owners": source('''
def fetch_notifications_esi(self):
    notifications = self._fetch_notifications_from_esi(token)
    self._store_notifications(notifications)
    self.notifications_last_update_at = now()
    self.save(update_fields=["notifications_last_update_at"])
''', "structures.models.owners"),
    "esi.decorators": source('''
def _check_callback(request):
    try:
        model = CallbackRedirect.objects.get(session_key=request.session.session_key)
    except (CallbackRedirect.DoesNotExist, Token.DoesNotExist):
        logger.debug("No callback for %s session %s", request.user, request.session.session_key, exc_info=True)
        return None
''', "esi.decorators"),
}


def record(text, at="2026-01-01T00:01:00Z", worker="a"):
    return {"source": "worker/" + worker * 64, "first_at": at, "record_complete": True,
            "rows": [{"text": line} for line in text.splitlines()]}


def provider_record(task, number, second=0, worker="a"):
    function = task.split(".tasks.")[1]
    app = task.split(".")[0]
    line = next(node.lineno for node in ast.walk(ast.parse(SOURCES['esi.openapi_clients']['text']))
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'HTTPServerError')
    return record(
        "[2026-01-01 00:01:00,001: ERROR/MainProcess] Task " + task
        + f"[00000000-0000-0000-0000-{number:012d}] raised unexpected: HTTPError()\n"
        + 'Traceback (most recent call last):\n'
        + f'  File "/installed/{app}/tasks.py", line 1, in {function}\n'
        + '    owner.update()\n'
        + f'  File "/installed/esi/openapi_clients.py", line {line}, in result\n'
        + '    raise HTTPServerError(\n'
        + 'aiopenapi3.errors.HTTPError', at=f"2026-01-01T00:01:{second:02d}Z", worker=worker)


def operation(provider="esi", subsystem="memberaudit", family="location", identity="7"):
    return {"provider": provider, "subsystem": subsystem, "family": family, "identity": identity}


def case(kind="memberaudit_transient"):
    op = operation()
    return {"contract": kind, "complete": True,
            "record": record("[01/Jan/2026 00:01:00] ERROR [memberaudit.models.characters:12] "
                             "Fixture (ID:7): location: Error occurred: TokenDoesNotExist"),
            "failure": {"at": "2026-01-01T00:01:00Z", "operation": op},
            "success": {"operation": deepcopy(op), "finished_at": "2026-01-01T00:02:00Z", "persisted": True},
            "current": {"operation": deepcopy(op), "observed_at": "2026-01-01T00:03:00Z", "healthy": True,
                        "identity_preserved": True, "credentials_valid": True, "latest_failure_at": None,
                        "has_token_error": False, "error_length": 0, "is_success": True},
            "section": "location", "persisted_section": "location", "character_pk": 7,
            "persisted_character_pk": 7, "transient_exception": "IncompleteResponseError",
            "token_pk": 9, "failed_refresh_token_pk": 9, "later_refresh_token_pk": 9,
            "required_scopes": ["esi-location.read_location.v1"],
            "validated_required_scopes": ["esi-location.read_location.v1"]}


class NativeRecoveryTests(unittest.TestCase):
    def test_retained_operand_proves_only_its_same_task_and_owner(self):
        item = case('owner_transient')
        task = 'moonmining.tasks.update_refineries_from_esi_for_owner'
        item['record'] = provider_record(task, 1)
        op = operation('esi', 'moonmining', task, '7')
        for side in ('failure', 'success', 'current'):
            item[side]['operation'] = deepcopy(op)
        item.update(owner_pk=7, owner_operand_source='task_args',
                    operand_task_id='00000000-0000-0000-0000-000000000001',
                    operand_source=item['record']['source'])
        self.assertTrue(prove_semantic_recovery(item, SOURCES))
        for key, value in (('owner_pk', 8), ('owner_operand_source', 'timing_guess'),
                           ('operand_task_id', '00000000-0000-0000-0000-000000000002'),
                           ('operand_source', 'another-worker')):
            other = deepcopy(item)
            other[key] = value
            self.assertFalse(prove_semantic_recovery(other, SOURCES))

    def test_same_section_and_existing_token_recovery(self):
        self.assertTrue(prove_semantic_recovery(case(), SOURCES))

    def test_different_owner_or_operation_success_cannot_recover(self):
        for field, value in (("identity", "8"), ("family", "ship"), ("subsystem", "structures")):
            item = case()
            item["success"]["operation"][field] = value
            with self.subTest(field=field):
                self.assertFalse(prove_semantic_recovery(item, SOURCES))

    def test_no_later_persisted_success_or_current_unhealthy(self):
        for side, key, value in (("success", "persisted", False), ("current", "healthy", False),
                                 ("current", "identity_preserved", False), ("current", "credentials_valid", False),
                                 ("current", "has_token_error", True), ("current", "is_success", False),
                                 ("success", "finished_at", "2026-01-01T00:00:00Z"),
                                 ("current", "latest_failure_at", "2026-01-01T00:02:01Z")):
            item = case()
            item[side][key] = value
            with self.subTest(side=side, key=key):
                self.assertFalse(prove_semantic_recovery(item, SOURCES))

    def test_scope_permanent_permission_unknown_and_malformed_fail_closed(self):
        for message in ("401 Unauthorized", "HTTPClientError 403", "Missing Permissions", "50013",
                        "invalid_grant", "InvalidTokenError", "MissingScopes", "MemoryError"):
            item = case()
            item["record"]["rows"].append({"text": message})
            with self.subTest(message=message):
                self.assertFalse(prove_semantic_recovery(item, SOURCES))
        for key, value in (("contract", "new_unknown_exception"), ("complete", False),
                           ("required_scopes", None), ("failed_refresh_token_pk", 10),
                           ("persisted_section", "ship")):
            item = case()
            item[key] = value
            self.assertFalse(prove_semantic_recovery(item, SOURCES))
        item = case()
        item["record"]["record_complete"] = False
        self.assertFalse(prove_semantic_recovery(item, SOURCES))
        item["record"]["rows"] = None
        self.assertFalse(prove_semantic_recovery(item, SOURCES))


class ProviderRecoveryTests(unittest.TestCase):
    def fixture(self):
        records = [provider_record("moonmining.tasks.update_refineries_from_esi_for_owner", 1),
                   provider_record("moonmining.tasks.fetch_notifications_from_esi_for_owner", 2, 1, "b"),
                   provider_record("structures.tasks.fetch_notification_for_owner", 3, 2, "b")]
        owners = [{"subsystem": app, "owner_pk": number, "corporation_id": 100 + number,
                   "character_id": 200 + number, "auth_link_pk": 300 + number, "token_pk": 400 + number,
                   "healthy": True, "credentials_valid": True, "native_success": True,
                   "finished_at": "2026-01-01T00:02:00Z", "observed_at": "2026-01-01T00:03:00Z",
                   "latest_failure_at": None} for number, app in enumerate(("moonmining", "structures"), 1)]
        return records, deepcopy(owners), owners

    def prove(self, records, before, current, complete=True, sources=None):
        return prove_systemic_provider_recovery(records, sources or SOURCES, before, current, complete=complete)

    def test_concurrent_known_server_route_and_full_later_native_inventory(self):
        self.assertTrue(self.prove(*self.fixture()))

    def test_isolated_or_copies_cannot_establish_systemic_event(self):
        records, before, current = self.fixture()
        self.assertFalse(self.prove(records[:1], before, current))
        self.assertFalse(self.prove([records[0]] * 20, before, current))
        for item in records:
            item["source"] = records[0]["source"]
        self.assertFalse(self.prove(records, before, current))

    def test_incomplete_changed_missing_bad_or_stale_owner_remains_blocking(self):
        for mutation in ("missing", "changed", "bad", "no_success", "new_failure"):
            records, before, current = self.fixture()
            if mutation == "missing":
                current.pop()
            elif mutation == "changed":
                current[0]["token_pk"] += 1
            elif mutation == "bad":
                current[0]["healthy"] = False
            elif mutation == "no_success":
                current[0]["finished_at"] = "2026-01-01T00:01:00Z"
            else:
                current[0]["latest_failure_at"] = "2026-01-01T00:02:01Z"
            with self.subTest(mutation=mutation):
                self.assertFalse(self.prove(records, before, current))
        self.assertFalse(self.prove(*self.fixture(), complete=False))

    def test_client_error_route_unknown_exception_or_changed_source_is_blocked(self):
        for replacement in ("HTTPClientError(", "ValueError("):
            records, before, current = self.fixture()
            records[0]["rows"][-2]["text"] = "    raise " + replacement
            self.assertFalse(self.prove(records, before, current))
        sources = deepcopy(SOURCES)
        sources["esi.openapi_clients"]["text"] += "# unexpected change"
        with self.assertRaises(DeploymentError):
            self.prove(*self.fixture(), sources=sources)


class CompleteAccountingTests(unittest.TestCase):
    def test_display_cap_never_caps_decisions_or_unknown_families(self):
        ledger = SemanticRecoveryLedger(display_limit=2)
        for number in range(100):
            ledger.add("known", "recovered", evidence_id=str(number))
        ledger.add("last_unknown_family", "unknown", evidence_id="last")
        result = ledger.summary()
        self.assertEqual(result["total_candidate_findings"], 101)
        self.assertEqual(result["recovered_count"], 100)
        self.assertEqual(result["unknown_count"], 1)
        self.assertTrue(result["analysis_complete"])
        self.assertTrue(result["examples_truncated"])
        with self.assertRaises(LogScanError) as raised:
            ledger.require_complete_recovery()
        self.assertEqual(raised.exception.analysis["unknown_count"], 1)

    def test_unreadable_stream_cannot_become_complete_analysis(self):
        ledger = SemanticRecoveryLedger(complete_scan=False, analysis_complete=False)
        with self.assertRaises(LogScanError):
            ledger.require_complete_recovery()


class AssetRecoveryTests(unittest.TestCase):
    def asset_case(self):
        item = case('asset_names')
        op = operation('esi', 'memberaudit', 'assets', '7')
        for side in ('failure', 'success', 'current'):
            item[side]['operation'] = deepcopy(op)
        line = next(node.lineno for node in ast.walk(ast.parse(SOURCES['esi.openapi_clients']['text']))
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'HTTPClientError')
        item['record'] = record('[01/Jan/2026 00:01:00] ERROR [memberaudit.models.characters:12] '
                                'Fixture (ID:7): assets: Error occurred: HTTPClientError: <HTTPClientError 404>\n'
                                'Traceback (most recent call last):\n'
                                'aiopenapi3.errors.HTTPClientError: Invalid IDs in the request\n'
                                'During handling of the above exception, another exception occurred:\n'
                                'Traceback (most recent call last):\n'
                                '  File "/installed/memberaudit/models/characters.py", line 1, in _fetching_asset_names_from_esi\n'
                                '    result()\n'
                                f'  File "/installed/esi/openapi_clients.py", line {line}, in result\n'
                                '    raise HTTPClientError(\n'
                                'esi.exceptions.HTTPClientError: <HTTPClientError 404: Invalid IDs in the request>')
        item.update(section='assets', payload_bytes_verified=True, relationships_valid=True)
        return item

    def test_complete_native_404_requires_real_payload_and_same_later_success(self):
        self.assertTrue(prove_semantic_recovery(self.asset_case(), SOURCES))
        for key in ('payload_bytes_verified', 'relationships_valid'):
            other = self.asset_case()
            other[key] = False
            self.assertFalse(prove_semantic_recovery(other, SOURCES))
        for terminal in ('ValueError: unrelated exception', 'esi.exceptions.HTTPClientError: <HTTPClientError 403 Forbidden>'):
            other = self.asset_case()
            other['record']['rows'][-1]['text'] = terminal
            self.assertFalse(prove_semantic_recovery(other, SOURCES))
        other = self.asset_case()
        other['success']['operation']['identity'] = '8'
        self.assertFalse(prove_semantic_recovery(other, SOURCES))


class CompleteReceiverScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.sources = {role: str(number) * 64 for number, role in enumerate(('worker', 'db', 'redis', 'proxy'), 1)}
        self.state = {'sources': SOURCES, 'inventory_before': [], 'owners': [], 'worker_service': 'worker',
                      'observed_at': '2026-01-01T00:03:00Z',
                      'interval': {'since': '2026-01-01T00:00:00Z', 'until': '2026-01-01T00:02:00Z'}}
        row = record('[01/Jan/2026 00:01:00] DEBUG [esi.aiopenapi3.plugins:121]  - Error', worker='1')
        raw = (json.dumps(row) + '\n').encode()
        compressed = gzip.compress(raw)
        (self.path / 'reviewed.ndjson.gz').write_bytes(compressed)
        self.proof = {'state': self.state, 'retained_captures': [], 'reviewed_summary': {},
                      'original_report_sha256': 'a' * 64,
                      'source_exports': [{'source': role + '/' + identity} for role, identity in self.sources.items()],
                      'signal_archive': {'filename': 'reviewed.ndjson.gz', 'stored_bytes': len(compressed),
                                         'sha256': hashlib.sha256(compressed).hexdigest(), 'source_bytes': len(raw),
                                         'record_count': 1, 'uncompressed_sha256': hashlib.sha256(raw).hexdigest()}}
        self.host = DockerHost.__new__(DockerHost)
        self.host.config = SimpleNamespace(auth_services=('worker',), database_service='db', redis_service='redis',
                                           proxy_service='proxy', command_timeout_seconds=5)
        self.host.retained_verifier_review = {'containers': [{'service': role, 'container_id': identity}
                                                            for role, identity in self.sources.items()],
                                              'semantic_recovery': {'report_path': str(self.path / 'proof.json')}}
        self.host._semantic_recovery_proof = mock.Mock(return_value=self.proof)
        self.host._worker_recycle_private_bytes = mock.Mock(side_effect=lambda path, limit: path.read_bytes())
        self.host._running_service_containers = mock.Mock(side_effect=lambda service, **kwargs: [self.sources[service]])
        self.host._semantic_read_current = mock.Mock(return_value=self.state)
        self.host._stream_log_lines = mock.Mock(side_effect=self.empty_stream)

    @staticmethod
    def empty_stream(*args, **kwargs):
        yield from ()

    def scan(self):
        return self.host._scan_semantic_logs(tuple(self.sources))

    def test_complete_history_plus_every_current_source_reaches_eof(self):
        self.scan()
        result = self.host.semantic_log_analysis
        self.assertTrue(result['complete_scan'])
        self.assertTrue(result['analysis_complete'])
        self.assertEqual(result['total_candidate_findings'], 1)
        self.assertEqual(self.host._stream_log_lines.call_count, 4)
        self.assertTrue(all(row['read_complete'] for row in result['source_coverage'].values()))

    def test_corrupted_or_incomplete_history_is_reported_and_cannot_pass(self):
        (self.path / 'reviewed.ndjson.gz').write_bytes(b'invalid')
        archive = self.proof['signal_archive']
        archive.update(stored_bytes=7, sha256=hashlib.sha256(b'invalid').hexdigest())
        with self.assertRaises(LogScanError) as raised:
            self.scan()
        self.assertFalse(raised.exception.analysis['complete_scan'])
        self.assertFalse(raised.exception.analysis['analysis_complete'])
        self.assertEqual(raised.exception.analysis['unknown_count'], 1)
        self.assertEqual(self.host._stream_log_lines.call_count, 4)

    def test_current_native_failure_has_complete_counted_blocking_report(self):
        self.host._semantic_read_current.side_effect = DeploymentError('current unhealthy')
        with self.assertRaises(LogScanError) as raised:
            self.scan()
        result = raised.exception.analysis
        self.assertTrue(result['complete_scan'])
        self.assertFalse(result['analysis_complete'])
        self.assertEqual(result['unresolved_count'], 1)
        self.assertEqual(result['total_candidate_findings'], 2)

    def test_one_failed_source_never_skips_the_other_sources(self):
        def stream(args, **kwargs):
            if args[-1] == self.sources['worker']:
                raise DeploymentError('source truncated')
            yield from ()
        self.host._stream_log_lines.side_effect = stream
        with self.assertRaises(LogScanError) as raised:
            self.scan()
        self.assertEqual(self.host._stream_log_lines.call_count, 4)
        self.assertFalse(raised.exception.analysis['complete_scan'])
        self.assertEqual(raised.exception.analysis['unknown_count'], 1)

    def test_unknown_delta_after_recovered_history_is_never_hidden(self):
        def stream(args, **kwargs):
            if args[-1] == self.sources['worker']:
                yield '2026-01-01T00:02:30Z [01/Jan/2026 00:02:30] ERROR [unknown:1] new failure'
        self.host._stream_log_lines.side_effect = stream
        with self.assertRaises(LogScanError) as raised:
            self.scan()
        result = raised.exception.analysis
        self.assertTrue(result['complete_scan'])
        self.assertTrue(result['analysis_complete'])
        self.assertEqual(result['unknown_count'], 1)
        self.assertEqual(result['total_candidate_findings'], 2)


class DiscordRecoveryTests(unittest.TestCase):
    def retry(self):
        item=case('discord_retry')
        op=operation('discord','discord','update_nickname','1234567/7654321')
        item['record']=record('[01/Jan/2026 00:01:00] ERROR [allianceauth.services.modules.discord.discord_client.client:100] '
                              '[Discord Service] '+ 'a'*32 +': Discord API returned error code 429')
        for side in ('failure','success','current'):
            item[side]['operation']=deepcopy(op)
        item.update(http_status=429,method='PATCH',success_method='PATCH',guild_id='1234567',success_guild_id='1234567',
                    member_id='7654321',success_member_id='7654321',request_id='a'*32,success_request_id='b'*32,
                    success_status=204,native_operation_completed=True,backoff_ms=60_000)
        return item

    def test_same_request_target_requires_later_native_success_and_current_health(self):
        self.assertTrue(prove_semantic_recovery(self.retry(),SOURCES))
        for key,value in (('success_guild_id','1234568'),('success_member_id','7654322'),('success_method','DELETE'),
                          ('success_status',403),('native_operation_completed',False),('backoff_ms',None),
                          ('success_request_id','a'*32)):
            item=self.retry()
            item[key]=value
            with self.subTest(key=key):
                self.assertFalse(prove_semantic_recovery(item,SOURCES))
        item=self.retry()
        item['success']['finished_at']='2026-01-01T00:20:00Z'
        item['current']['observed_at']='2026-01-01T00:30:00Z'
        self.assertFalse(prove_semantic_recovery(item,SOURCES))

    def test_unknown_member_is_a_separate_delete_and_absence_contract(self):
        item=self.retry()
        item['contract']='discord_unknown_member'
        item['record']=record('[01/Jan/2026 00:01:00] ERROR [discord.client:1] '
                              '[Discord Service] API error {"message": "Unknown Member", "code": 10007}')
        item.update(http_status=404,discord_code=10007,delete_status=204,delete_guild_id='1234567',
                    current_guild_id='1234567',delete_member_id='7654321',current_member_id='7654321',
                    binding_present=False,current_http_status=404,current_discord_code=10007)
        self.assertTrue(prove_semantic_recovery(item,SOURCES))
        for key,value in (('binding_present',True),('current_guild_id','1234568'),('delete_status',403),
                          ('current_discord_code',50013),('current_http_status',401)):
            altered=deepcopy(item)
            altered[key]=value
            self.assertFalse(prove_semantic_recovery(altered,SOURCES))
        item['record']['rows'][-1]['text']+=' Missing Permissions 50013'
        self.assertFalse(prove_semantic_recovery(item,SOURCES))


class StreamingSemanticTests(unittest.TestCase):
    def test_client_cancellation_cannot_hide_unknown_text_or_another_service(self):
        state={'sources':SOURCES,'inventory_before':[],'owners':[],'worker_service':'worker','proxy_service':'nginx'}
        line='192.0.2.1 - - [01/Jan/2026:00:01:00 +0000] "GET /counts/ HTTP/1.1" 499 0 "-" "fixture-browser"'
        original=record(line)
        original['source']='nginx/'+'a'*64
        for mutation in ('normal','unknown_tail','worker_source','unknown_prefix'):
            item=deepcopy(original)
            if mutation=='unknown_tail':
                item['rows'].append({'text':'ERROR: unknown failure without a traceback'})
            elif mutation=='worker_source':
                item['source']='worker/'+'a'*64
            elif mutation=='unknown_prefix':
                item['rows'][0]['text']='ERROR: '+line
            with self.subTest(mutation=mutation),RetainedSemanticAnalyzer(state) as analyzer:
                analyzer.feed(item)
                result=analyzer.finish()
                self.assertEqual(result['unknown_count'],0 if mutation=='normal' else 1)
                if mutation!='normal':
                    with self.assertRaises(LogScanError):
                        analyzer.ledger.require_complete_recovery()

    def test_caught_callback_requires_same_session_native_fallback_and_http_completion(self):
        item=case('handled_callback')
        session='a'*64
        op=operation('application','esi','callback_lookup',session)
        for side in ('failure','success','current'):
            item[side]['operation']=deepcopy(op)
        item['record']=record('[01/Jan/2026 00:01:00] DEBUG [esi.decorators:7] No callback for AnonymousUser session sha256:'+session+'\n'
                              'Traceback (most recent call last):\n'
                              '  File "/installed/esi/decorators.py", line 4, in _check_callback\n'
                              '    model = get()\n'
                              '  File "/django/manager.py", line 1, in manager_method\n'
                              '    return get()\n'
                              '  File "/django/query.py", line 1, in get\n'
                              '    raise DoesNotExist()\n'
                              'esi.models.CallbackRedirect.DoesNotExist: CallbackRedirect matching query does not exist.')
        item.update(session_sha256=session,success_session_sha256=session,native_fallback_completed=True,
                    authentication_completed=True,http_completion_status=200)
        # The log call's line is derived from this source rather than a deployed
        # application's fixed line number.
        import ast
        tree=ast.parse(SOURCES['esi.decorators']['text'])
        line=next(node.lineno for node in ast.walk(tree) if isinstance(node,ast.Call)
                  and isinstance(node.func,ast.Attribute) and node.func.attr=='debug')
        item['record']['rows'][0]['text']=item['record']['rows'][0]['text'].replace('esi.decorators:7',f'esi.decorators:{line}')
        self.assertTrue(prove_semantic_recovery(item,SOURCES))
        for key,value in (('success_session_sha256','b'*64),('native_fallback_completed',False),
                          ('authentication_completed',False),('http_completion_status',500)):
            other=deepcopy(item)
            other[key]=value
            self.assertFalse(prove_semantic_recovery(other,SOURCES))
        item['record']['rows'].append({'text':'RuntimeError: unhandled new failure'})
        self.assertFalse(prove_semantic_recovery(item,SOURCES))

    def test_complete_records_keep_terminal_causes_hash_sessions_and_do_not_expose_secrets(self):
        session='q'*32
        lines=['2026-01-01T00:01:00.001Z [01/Jan/2026 00:01:00] ERROR [example:1] failure session '+session,
               '2026-01-01T00:01:00.002Z Traceback (most recent call last):',
               '2026-01-01T00:01:00.003Z   File "/example.py", line 2, in example',
               '2026-01-01T00:01:00.004Z     raise RuntimeError()',
               '2026-01-01T00:01:00.005Z RuntimeError: complete terminal cause',
               '2026-01-01T00:01:01.000Z [01/Jan/2026 00:01:01] INFO [example:1] done']
        stats={'physical_lines':0}
        rows=list(_semantic_log_records(iter(lines),'worker/'+'a'*64,stats=stats,deadline=time.monotonic()+5))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['rows'][-1]['text'],'RuntimeError: complete terminal cause')
        self.assertNotIn(session,str(rows))
        self.assertEqual(rows[0]['rows'][0]['session_sha256'],[hashlib.sha256(session.encode()).hexdigest()])
        self.assertEqual(stats['physical_lines'],6)

    def test_malformed_truncated_and_oversized_streams_cannot_pass(self):
        for lines in (['unknown transport output'],['2026-01-01T00:01:00Z \ufffd damaged'],
                      ['2026-01-01T00:01:00Z [01/Jan/2026 00:01:00] ERROR [example:1] failure']
                       +['2026-01-01T00:01:00Z   continuation']*129):
            with self.subTest(lines=len(lines)),self.assertRaises(DeploymentError):
                list(_semantic_log_records(iter(lines),'worker/'+'a'*64,stats={'physical_lines':0},deadline=time.monotonic()+5))

    def test_unknown_after_many_schema_messages_remains_visible_and_blocks(self):
        state={'sources':SOURCES,'inventory_before':[],'owners':[],'worker_service':'worker'}
        with RetainedSemanticAnalyzer(state,display_limit=1) as analyzer:
            for number in range(70):
                analyzer.feed(record('[01/Jan/2026 00:01:00] DEBUG [esi.aiopenapi3.plugins:121]  - Error'))
            analyzer.feed(record('[01/Jan/2026 00:01:00] ERROR [unknown:1] unrecognized failure'))
            result=analyzer.finish()
            self.assertEqual(result['total_candidate_findings'],71)
            self.assertEqual(result['unknown_count'],1)
            self.assertTrue(result['examples_truncated'])
            with self.assertRaises(LogScanError):
                analyzer.ledger.require_complete_recovery()


if __name__ == "__main__":
    unittest.main()
