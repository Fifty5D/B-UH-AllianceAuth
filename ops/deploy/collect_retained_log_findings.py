"""Read only the two unresolved retained-log findings, never run recovery.

Uses the already-reviewed staged verifier and the existing production lock.
No token refresh, update task, verifier installation, rollback or cleanup occurs.
The resulting evidence cannot grant a log exception or authorize a deployment.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace
import zlib

STAGE = Path("/root/buh-verifier-repair-5e45cd91-6z2lbw22")
HEAD = "5e45cd91591ac49dceac7ac293f7313071f95db9"
HELPER_HASH = "937ea9dde4337b15c91ce69dd2dfbb7e64a25a440bfb3f9d81c7149ddb4a6654"
DOCKER_HASH = "343e794a9e5f450c5e2f0130113b93fb343c7a7463e3011dae8125385dcf032b"
ATTEMPT = "gh-36955595351-1"
PLAN_HASH = "ec04921a8707437ef8c0a05460581ea05c72de21d19ab5d602b6478aaf5597dd"
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_LOG_ROWS = 12000
MAX_LOG_BYTES = 8 * 1024 * 1024


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_log(line: str, redact) -> dict:
    """Preserve complete traceback structure; hash session and request secrets."""
    original = line
    # These loggers include short session identifiers. Preserve correlation by
    # hashing even these partial IDs rather than copying them to public output.
    sessions = re.findall(r"\bsession ([A-Za-z0-9]{1,128})", line)
    line = re.sub(r"\bsession ([A-Za-z0-9]{1,128})",
                  lambda match: "session sha256:" + sha(match[1].encode()), line)
    # Access logs can contain an SSO code or state in their query string.
    line = re.sub(r"(https?://[^\s\"'<>?]+|/[A-Za-z0-9_./%+-]+)\?[^\s\"'<>]*",
                  r"\1?<query-redacted>", line)
    line = redact(line)
    return {"text": line, "raw_sha256": sha(original.encode()),
            "session_sha256": sorted({sha(value.encode()) for value in sessions})}


def log_line_selected(line: str, *, web: bool, active_record: bool, target: dict) -> bool:
    """Select full relevant records, never emit token claims or credentials."""
    if active_record:
        return True
    if web:
        return bool(re.search(
            r"\[esi\.(?:decorators|views):[0-9]+\]|CallbackRedirect|"
            r"\[allianceauth\.authentication\.(?:backends|views):[0-9]+\]|"
            r"\[allianceauth\.hooks:[0-9]+\].*to dashboard|"
            r"(?:ERROR|CRITICAL)|(?:^| )Traceback", line))
    return bool(re.search(
        re.escape(target['character_name']) + r" \(ID:" + str(target['memberaudit_character_pk']) + r"\):|"
        + re.escape(target['character_name']) + r": Using token |"
        + r"Successfully refreshed <Token\(id=" + str(target['token_pk']) + r"\):|"
        + r"Task memberaudit\.tasks\.assets_build_list_from_esi"
        r"\[" + re.escape(target['original_assets_task_id']) + r"\]", line))


ASSET_READ_CODE = r'''
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import stat
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import Q
from allianceauth.authentication.models import CharacterOwnership
from esi.models import Token
from memberaudit.models import Character, CharacterAsset, CharacterUpdateStatus
from memberaudit.managers.character_sections_1 import CharacterAssetManagerBase
import esi.decorators
from buh_max_history.capture import archive_root
from buh_max_history.models import ArchiveStream, ArchiveObservation

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, cls=DjangoJSONEncoder,
                                   separators=(',', ':')).encode()).hexdigest()

def status_row(obj):
    return {'pk': obj.pk, 'section': obj.section, 'is_success': obj.is_success,
            'has_token_error': obj.has_token_error,
            'run_started_at': obj.run_started_at, 'run_finished_at': obj.run_finished_at,
            'update_started_at': obj.update_started_at, 'update_finished_at': obj.update_finished_at,
            'content_hashes': [obj.content_hash_1, obj.content_hash_2, obj.content_hash_3],
            'error_sha256': hashlib.sha256(obj.error_message.encode()).hexdigest(),
            'error_length': len(obj.error_message),
            'invalid_ids_404': 'Invalid IDs in the request' in obj.error_message}

def snapshot_payload(snapshot, stream):
    relative = Path(snapshot.relative_path)
    root = archive_root().resolve(strict=True)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] != 'private':
        raise ValueError('Private asset snapshot path is outside the expected area')
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('Private asset snapshot path contains a link')
    path.resolve(strict=True).relative_to(root)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_size != snapshot.stored_bytes
                or before.st_size > 16 * 1024 * 1024 or snapshot.source_bytes > 32 * 1024 * 1024):
            raise ValueError('Private asset snapshot size or file type differs')
        with gzip.GzipFile(fileobj=source) as compressed:
            raw = compressed.read(32 * 1024 * 1024 + 1)
        after = os.fstat(source.fileno())
        if (len(raw) != snapshot.source_bytes or hashlib.sha256(raw).hexdigest() != snapshot.payload_sha256
                or (before.st_ino, before.st_mtime_ns, before.st_size) !=
                   (after.st_ino, after.st_mtime_ns, after.st_size)):
            raise ValueError('Private asset snapshot bytes do not match their revision')
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) > 25000:
        raise ValueError('Private asset payload exceeds the bounded collection')
    ids = []
    for row in data:
        if (not isinstance(row, dict) or type(row.get('item_id')) is not int
                or row['item_id'] <= 0 or type(row.get('quantity')) is not int
                or row['quantity'] < 0 or type(row.get('type_id')) is not int
                or row['type_id'] <= 0):
            raise ValueError('Private asset payload has a malformed asset')
        ids.append(row['item_id'])
    return {'snapshot_pk': snapshot.pk, 'stream_pk': stream.pk,
            'operation_id': stream.operation_id, 'payload_sha256': snapshot.payload_sha256,
            'relative_path': snapshot.relative_path, 'source_bytes': len(raw),
            'stored_bytes': snapshot.stored_bytes, 'actual_hash_matches': True,
            'actual_size_matches': True, 'format_valid': True, 'item_count': len(ids),
            'item_ids_in_response_order': ids, 'asset_rows_sha256': digest(data)}

def emit_assets(target):
    result = {'read_only': True, 'scan_complete': False, 'errors': []}
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
    with transaction.atomic():
        member = Character.objects.select_related('eve_character').get(pk=target['memberaudit_character_pk'])
        if (member.eve_character.character_id != target['character_id']
                or member.eve_character.character_name != target['character_name']):
            raise ValueError('The stable selected character identity differs')
        ownership = CharacterOwnership.objects.get(character=member.eve_character)
        token = Token.objects.get(pk=target['token_pk'])
        inventory = list(Token.objects.filter(character_id=target['character_id']).order_by('pk').values_list('pk', flat=True))
        scope_names = sorted(token.scopes.values_list('name', flat=True))
        status = CharacterUpdateStatus.objects.get(character=member, section=Character.UpdateSection.ASSETS)
        result['identity'] = {'character_name': target['character_name'], 'character_id': target['character_id'],
                              'memberaudit_character_pk': member.pk, 'auth_link_pk': ownership.pk,
                              'user_id': ownership.user_id, 'token_pk': token.pk,
                              'token_inventory': inventory, 'is_disabled': member.is_disabled,
                              'same_token_user': token.user_id == ownership.user_id,
                              'same_token_character': token.character_id == target['character_id'],
                              'same_token_owner': token.character_owner_hash == ownership.owner_hash,
                              'required_assets_scope_present': 'esi-assets.read_assets.v1' in scope_names}
        result['assets_status'] = status_row(status)
        rows = list(CharacterAsset.objects.filter(character=member).order_by('pk').values(
            'pk', 'item_id', 'eve_type_id', 'quantity', 'parent_id', 'location_id',
            'is_singleton', 'is_blueprint_copy', 'location_flag')[:25001])
        if len(rows) > 25000:
            raise ValueError('Current selected asset rows exceed the bounded collection')
        pk_map = {row['pk']: row for row in rows}
        item_ids = [row['item_id'] for row in rows]
        parent_ids = {row['parent_id'] for row in rows if row['parent_id'] is not None}
        type_ids = {row['eve_type_id'] for row in rows}
        location_ids = {row['location_id'] for row in rows if row['location_id'] is not None}
        # One character only; query required FK sets in batches to stay below
        # database parameter limits. These are ordinary read-only QuerySets.
        def present(model, selected):
            ordered = sorted(selected)
            found = set()
            for offset in range(0, len(ordered), 500):
                found.update(model.objects.filter(pk__in=ordered[offset:offset+500]).values_list('pk', flat=True))
            return found
        type_model = CharacterAsset._meta.get_field('eve_type').remote_field.model
        location_model = CharacterAsset._meta.get_field('location').remote_field.model
        issues = {
            'duplicate_item_ids': len(item_ids) - len(set(item_ids)),
            'invalid_item_ids': sum(type(row['item_id']) is not int or row['item_id'] <= 0 for row in rows),
            'invalid_quantities': sum(type(row['quantity']) is not int or row['quantity'] < 0 for row in rows),
            'missing_or_other_character_parents': len(parent_ids - set(pk_map)),
            'missing_types': len(type_ids - present(type_model, type_ids)),
            'missing_locations': len(location_ids - present(location_model, location_ids)),
            'self_parent_rows': sum(row['parent_id'] == row['pk'] for row in rows),
        }
        result['current_assets'] = {'row_count': len(rows), 'rows_sha256': digest(rows),
                                   'item_ids': item_ids, 'relationship_issues': issues,
                                   'examples': rows[:20], 'all_rows_metadata_valid': not any(issues.values())}
        for key, func in (
                ('asset_name_lookup', CharacterAssetManagerBase._fetching_asset_names_from_esi),
                ('asset_fetch', CharacterAssetManagerBase._fetch_data_from_esi),
                ('callback_lookup', esi.decorators._check_callback)):
            source = inspect.getsource(func)
            if len(source.encode()) > 20000:
                raise ValueError('Installed source excerpt exceeds the bound')
            result.setdefault('installed_sources', {})[key] = {
                'filename': inspect.getsourcefile(func), 'first_line': inspect.getsourcelines(func)[1],
                'sha256': hashlib.sha256(source.encode()).hexdigest(), 'source': source}
        result['versions'] = {name: importlib.metadata.version(name) for name in ('aa-memberaudit', 'django-esi', 'allianceauth')}
        error_at = datetime.fromisoformat(target['asset_error_at'].replace('Z', '+00:00'))
        streams = list(ArchiveStream.objects.filter(character_id=target['character_id']).filter(
            Q(operation_id__icontains='Assets') | Q(url_template__icontains='/assets')).order_by('pk')[:101])
        if len(streams) > 100:
            raise ValueError('Selected asset history streams exceed the bound')
        result['asset_history_streams'] = [{'pk': stream.pk, 'operation_id': stream.operation_id,
            'method': stream.method, 'url_template': stream.url_template.split('?')[0],
            'character_id': stream.character_id, 'auth_user_id': stream.auth_user_id,
            'request_count': stream.request_count, 'snapshot_count': stream.snapshot_count,
            'last_status_code': stream.last_status_code, 'last_seen_at': stream.last_seen_at,
            'page': stream.safe_parameters.get('page'), 'current_payload_sha256': stream.current_payload_sha256}
            for stream in streams]
        result['historical_asset_payloads'] = []
        result['later_asset_payloads'] = []
        inspected = set()
        payload_cache = {}
        for stream in streams:
            if stream.method != 'GET' or '/names' in stream.url_template:
                continue
            observations = ArchiveObservation.objects.filter(stream=stream).select_related('snapshot')
            near = list(observations.filter(first_observed_at__lte=error_at,
                        last_observed_at__gte=error_at-timedelta(seconds=30)).order_by('-last_observed_at')[:6])
            later = list(observations.filter(last_observed_at__gt=error_at).order_by('-last_observed_at')[:1])
            for group, selected in (('historical_asset_payloads', near), ('later_asset_payloads', later)):
                for observation in selected:
                    if observation.snapshot_id is None:
                        result['errors'].append({'section': group, 'error_type': 'MissingSnapshot'})
                        continue
                    snapshot = observation.snapshot
                    if snapshot.stream_id != stream.pk or not 200 <= snapshot.status_code < 300:
                        result['errors'].append({'section': group, 'error_type': 'SnapshotRelationshipMismatch'})
                        continue
                    row = {'observation_pk': observation.pk, 'first_observed_at': observation.first_observed_at,
                           'last_observed_at': observation.last_observed_at, 'page': stream.safe_parameters.get('page')}
                    try:
                        if snapshot.pk not in payload_cache:
                            if len(payload_cache) >= 16:
                                raise ValueError('Distinct selected payload reads exceed the bound')
                            payload_cache[snapshot.pk] = snapshot_payload(snapshot, stream)
                        row.update(payload_cache[snapshot.pk])
                        inspected.add(snapshot.pk)
                    except Exception as error:
                        row.update(error_type=type(error).__name__, error_sha256=hashlib.sha256(str(error).encode()).hexdigest())
                        result['errors'].append({'section': group, 'error_type': type(error).__name__})
                    result[group].append(row)
        result['asset_post_bodies_retained'] = False
        result['item_id_evidence_limitation'] = 'Capture stores successful GET payloads, not failed POST bodies; response IDs preserve order, but invalid IDs are not identified by ESI.'
        result['distinct_payloads_read'] = len(inspected)
        result['later_successful_assets_run'] = bool(status.is_success is True and not status.has_token_error
            and status.run_finished_at and status.run_finished_at > error_at
            and status.run_started_at and status.run_finished_at >= status.run_started_at
            and status.content_hash_1 and not status.error_message)
        result['scan_complete'] = not result['errors']
    print('BUH_RETAINED_ASSETS_BEGIN')
    print(json.dumps(result, sort_keys=True, cls=DjangoJSONEncoder))
    print('BUH_RETAINED_ASSETS_END')

def capture_assets(target):
    try:
        emit_assets(target)
    except Exception as error:
        import traceback
        sites = [{'file': Path(row.filename).name, 'function': row.name, 'line': row.lineno}
                 for row in traceback.extract_tb(error.__traceback__)[-8:]]
        print('BUH_RETAINED_ASSETS_BEGIN')
        print(json.dumps({'read_only': True, 'scan_complete': False,
              'errors': [{'error_type': type(error).__name__, 'failure_sites': sites,
                          'error_sha256': hashlib.sha256(str(error).encode()).hexdigest()}]}))
        print('BUH_RETAINED_ASSETS_END')
'''


def collect_stream(host, module, name, command, *, web, since, until, target):
    rows = []
    total = consumed = selected_bytes = 0
    record_selected = False
    deadline = time.monotonic() + min(180, host.config.command_timeout_seconds)
    stream = host._stream_log_lines(command, deadline=deadline, context="Read-only exact retained log findings")
    try:
        for raw in stream:
            total += 1
            consumed += len(raw.encode())
            stamp, separator, message = raw.partition(' ')
            if not separator or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z', stamp) is None:
                raise module.DeploymentError('Timestamped retained finding stream is incomplete')
            header = module.ALLIANCEAUTH_LOG_HEADER_RE.match(message) or module.CELERY_LOG_HEADER_RE.match(message)
            if header:
                record_selected = log_line_selected(message, web=web, active_record=False, target=target)
            if not log_line_selected(message, web=web, active_record=record_selected, target=target):
                continue
            # Never retain claims, response headers, raw codes or access tokens,
            # even when an unexpectedly interleaved record matches selection.
            if re.search(r"\b(?:jti|scp|azp|authorization|refresh_token|access_token)\b", message, re.I):
                continue
            selected_bytes += len(message.encode())
            if len(rows) >= MAX_LOG_ROWS or selected_bytes > MAX_LOG_BYTES:
                raise module.DeploymentError('Exact retained log findings exceed the bounded export')
            safe = safe_log(message, module.redact_sensitive_text)
            rows.append({'at': stamp, **safe})
    finally:
        stream.close()
    return {'source': name, 'since': since, 'until': until, 'complete': True,
            'scanned_lines': total, 'scanned_bytes': consumed, 'selected_lines': len(rows), 'rows': rows}


def protection(host, helper):
    paths = [Path('/etc/buh-platform-v2/INSTALL.json'), helper.LIBRARY / 'ops/deploy/docker_host.py',
             host.config.state_dir / 'active-recovery.json', host.backup_path / 'RECOVERY.json']
    return {'file_sha256': {str(path): sha(helper.private_read(path)) for path in paths},
            'repair_receipt_present': helper.RECEIPT.exists() or helper.RECEIPT.is_symlink(),
            'safety_slots': {slot: host._run(['docker', 'inspect', '--format',
                '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.RestartCount}}|{{.State.StartedAt}}', slot],
                context='Read-only retained safety identity').strip()
                for slot in (*host.previous_web_slots, *host.candidate_web_slots)}}


def collect_diagnostic_copy(host, module, assessment, since, until, target):
    """Indexed read-only evidence also covers records rotated out of Docker.

    The worker queries cover only the original asset request's 30-second window.
    Only the web source spans the retained interval to correlate login records.
    Neither events nor restart counts are compared or modified here.
    """
    path = module.WORKER_RESTART_STORE
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o600):
        raise module.DeploymentError('Read-only retained log store identity differs')
    result = {'read_only': True, 'complete': False, 'streams': []}
    deadline = time.monotonic() + 150
    error_at = datetime.fromisoformat(target['asset_error_at'].replace('Z', '+00:00'))
    start = (error_at - timedelta(seconds=15)).isoformat().replace('+00:00', 'Z')
    end = (error_at + timedelta(seconds=15)).isoformat().replace('+00:00', 'Z')
    since_z = datetime.fromisoformat(since.replace('Z', '+00:00')).isoformat().replace('+00:00', 'Z')
    until_z = datetime.fromisoformat(until.replace('Z', '+00:00')).isoformat().replace('+00:00', 'Z')
    policy = assessment['worker_memory_recycles']
    queries = [(host.config.gunicorn_service, True, since_z, until_z)]
    queries.extend((name, False, start, end) for name in policy['workers'].values())
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        db.execute('BEGIN')
        metadata = dict(db.execute("SELECT key,value FROM metadata WHERE key IN ('store_id','source:docker:last_success')"))
        if metadata.get('store_id') != policy['diagnostics_store_id']:
            raise module.DeploymentError('Read-only retained log store differs from reviewed evidence')
        result['store_id'] = metadata['store_id']
        result['docker_capture_last_success'] = metadata.get('source:docker:last_success')
        for name, web, first, last in queries:
            def lines(*args, **kwargs):
                count = consumed = 0
                for at, body in db.execute("SELECT at,message FROM logs WHERE source='docker' AND service=? "
                                          "AND at>=? AND at<=? ORDER BY at,id LIMIT 100001", (name, first, last)):
                    if time.monotonic() >= deadline:
                        raise module.DeploymentError('Read-only retained log store timed out')
                    count += 1
                    decoder = zlib.decompressobj()
                    raw = decoder.decompress(body, 65537)
                    consumed += len(raw)
                    if (len(raw) > 65536 or not decoder.eof or decoder.unused_data
                            or count > 100000 or consumed > 64 * 1024 * 1024):
                        raise module.DeploymentError('Read-only retained log store exceeds complete-read bounds')
                    yield at + ' ' + raw.decode('utf-8', errors='strict')
            fake_host = SimpleNamespace(config=host.config, _stream_log_lines=lines)
            result['streams'].append(collect_stream(fake_host, module, name,
                ['read-only-diagnostic-store'], web=web, since=first, until=last, target=target))
        gaps = list(db.execute('SELECT at,source,reason FROM gaps WHERE at>=? AND at<=? ORDER BY at LIMIT 101',
                               (since_z, until_z)))
        if len(gaps) > 100:
            raise module.DeploymentError('Retained log coverage gaps exceed the report bound')
        result['coverage_gaps'] = [{'at': at, 'source': source, 'reason_sha256': sha(reason.encode())}
                                   for at, source, reason in gaps]
    after = path.lstat()
    if (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev):
        raise module.DeploymentError('Retained log store changed identity during read')
    result['complete'] = True
    return result


def validate_target(target):
    keys = {'character_name', 'character_id', 'memberaudit_character_pk', 'token_pk',
            'original_assets_task_id', 'asset_error_at'}
    if (not isinstance(target, dict) or set(target) != keys
            or not isinstance(target['character_name'], str) or not 1 <= len(target['character_name']) <= 100
            or any(ord(char) < 32 or ord(char) == 127 for char in target['character_name'])
            or any(type(target[key]) is not int or not 1 <= target[key] < 2**63
                   for key in ('character_id', 'memberaudit_character_pk', 'token_pk'))
            or not isinstance(target['original_assets_task_id'], str)
            or re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', target['original_assets_task_id']) is None
            or not isinstance(target['asset_error_at'], str)
            or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z', target['asset_error_at']) is None):
        raise ValueError('Read-only character selection is invalid')
    datetime.fromisoformat(target['asset_error_at'].replace('Z', '+00:00'))


def run(target):
    report = {'schema_version': 1, 'read_only': True, 'diagnostic_only': True, 'scan_complete': False,
              'started_at': datetime.now(timezone.utc).isoformat(), 'attempt_id': ATTEMPT,
              'staged_source_commit': HEAD, 'tooling_installation_attempted': False,
              'deployment_attempted': False, 'supported_recovery_attempted': False,
              'token_refresh_attempted': False, 'memberaudit_mutation_attempted': False,
              'structures_mutation_attempted': False, 'preserve_recovery_resources': True,
              'errors': []}
    lock = helper = host = None
    phase = 'validate_exact_staged_tooling'
    try:
        validate_target(target)
        if os.geteuid() != 0:
            raise RuntimeError('root_required')
        info = STAGE.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('staged_directory_identity_changed')
        path = STAGE / 'verifier_repair.py'
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 65536):
            raise RuntimeError('staged_helper_identity_changed')
        raw = path.read_bytes()
        if sha(raw) != HELPER_HASH:
            raise RuntimeError('staged_helper_checksum_changed')
        sys.dont_write_bytecode = True
        sys.path.insert(0, '/usr/local/lib/buh-platform-v2')
        helper = importlib.util.module_from_spec(importlib.util.spec_from_loader('buh_readonly_log_proof', loader=None))
        helper.__file__ = str(path)
        exec(compile(raw, str(path), 'exec'), helper.__dict__)
        report['installed_before'] = helper.installed_identity()
        assessment = json.loads(helper.private_read(STAGE / 'assessment.json', 65536))
        if assessment['attempt_id'] != ATTEMPT or assessment['hold_sha256'] != PLAN_HASH:
            raise RuntimeError('retained_assessment_identity_changed')
        module = helper.load_source(STAGE / 'docker_host.py', DOCKER_HASH, 'ops.deploy.readonly_log_host')
        config = helper.ReceiverConfig.load(Path('/etc/buh-platform-v2/receiver.json'))
        lock = os.open(config.state_dir / 'deploy.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError('existing_deployment_lock_identity_changed')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        loaded = module.DockerHost.load_incomplete_plan(config)
        if loaded is None:
            raise RuntimeError('retained_attempt_missing')
        host, plan = loaded
        helper.verify_retained_plan(host, plan, PLAN_HASH)
        report['protected_before'] = protection(host, helper)
        bridge = helper.load_source(Path(assessment['bridge_source']), helper.BRIDGE_SHA256, 'buh_readonly_log_bridge')
        pilot, owner_source = bridge.qualified_pilot(Path(assessment['pilot_directory']))
        report['before_host'] = pilot.verify_host(host, config, ATTEMPT)
        _, baseline, baseline_digest = pilot.latest_report()
        if baseline_digest != assessment['baseline_report_sha256']:
            raise RuntimeError('qualified_baseline_changed')
        prior = json.loads(helper.private_read(Path(assessment['prior_recovery_report']), 256 * 1024))
        since = prior['started_at']
        until = datetime.now(timezone.utc).isoformat()
        # The prior reviewed boundary is reused; no moving restart baseline and
        # no activation of the staged verifier occurs in this diagnostic.
        phase = 'read_only_current_application_evidence'
        structures_target = pilot.select_pilot(baseline, 'Fifty5D')
        roster = pilot.snapshot_targets(baseline)
        report['structures'] = bridge.snapshot(host, pilot, owner_source, structures_target, roster)
        bridge.require_healthy_owners(report['structures'], roster)
        selection = helper.load_source(Path(assessment['memberaudit_directory']) / 'memberaudit_selection.py',
            helper.SELECTION_SHA256, 'buh_readonly_log_selection')
        completed = json.loads(helper.private_read(Path(assessment['memberaudit_directory']) / 'memberaudit-report.json'))
        targets, excluded = selection.select_outage(baseline)
        refs = helper.memberaudit_refs(completed, targets)
        report['memberaudit_preserved'] = helper.verify_memberaudit(host, refs, excluded, completed)
        code = ASSET_READ_CODE + '\ncapture_assets(' + repr(target) + ')'
        raw = host._manage_live('shell', '--no-imports', '-c', code,
                               context='Read-only selected assets and installed callback source')
        begin, end = 'BUH_RETAINED_ASSETS_BEGIN\n', '\nBUH_RETAINED_ASSETS_END'
        if raw.count(begin) != 1 or raw.count(end) != 1:
            raise RuntimeError('bounded_assets_report_incomplete')
        report['selected_character'] = json.loads(raw.split(begin, 1)[1].split(end, 1)[0])
        phase = 'read_only_exact_retained_logs'
        report['logs'] = []
        for service, web in ((config.gunicorn_service, True), (config.worker_service, False)):
            ids = host._running_service_containers(service, context='Read-only exact finding source discovery')
            if not ids or len(ids) != host.auth_replica_counts[service]:
                raise RuntimeError('retained_log_service_topology_changed')
            for identity in ids:
                report['logs'].append(collect_stream(host, module, service + '/' + identity,
                    ['docker', 'logs', '--timestamps', '--since=' + since, '--until=' + until, identity],
                    web=web, since=since, until=until, target=target))
        try:
            report['diagnostic_log_copy'] = collect_diagnostic_copy(host, module, assessment, since, until, target)
        except Exception as error:
            report['diagnostic_log_copy'] = {'read_only': True, 'complete': False,
                'error_type': type(error).__name__, 'error_sha256': sha(str(error).encode())}
        # HTTP status evidence is selected without exposing query strings,
        # credentials, client IPs or other users' request bodies.
        ids = host._running_service_containers(config.proxy_service, context='Read-only login HTTP source discovery')
        report['login_http'] = []
        for identity in ids:
            deadline = time.monotonic() + min(120, config.command_timeout_seconds)
            lines = host._stream_log_lines(['docker', 'logs', '--timestamps', '--since=' + since,
                '--until=' + until, identity], deadline=deadline, context='Read-only retained login HTTP statuses')
            matched = []
            try:
                for line in lines:
                    stamp, _, body = line.partition(' ')
                    match = re.search(r'"(GET|POST) (/[^ ]*) HTTP/[0-9.]+" ([0-9]{3}) ', body)
                    if not match:
                        continue
                    path = match[2].split('?', 1)[0]
                    status = int(match[3])
                    if path not in {'/sso/callback/', '/sso/callback', '/sso/login', '/sso/login/',
                                    '/account/login/', '/dashboard/', '/dashboard'} and status < 500:
                        continue
                    if len(matched) >= 4000:
                        raise RuntimeError('login_http_evidence_exceeds_bound')
                    matched.append({'at': stamp, 'method': match[1], 'path': path, 'status': status,
                        'client_sha256': sha(body.split(' ', 1)[0].encode()), 'raw_sha256': sha(line.encode())})
            finally:
                lines.close()
            report['login_http'].append({'container_id': identity, 'complete': True, 'rows': matched})
        phase = 'read_only_final_preservation'
        report['after_host'] = pilot.verify_host(host, config, ATTEMPT)
        report['memberaudit_preserved_after'] = helper.verify_memberaudit(host, refs, excluded, completed)
        report['protected_after'] = protection(host, helper)
        report['protected_state_unchanged'] = report['protected_before'] == report['protected_after']
        if not report['protected_state_unchanged']:
            raise RuntimeError('retained_state_changed_during_read_only_scan')
        report['scan_complete'] = report['selected_character'].get('scan_complete') is True
    except Exception as error:
        report['failure_phase'] = phase
        import traceback
        sites = [{'file': Path(row.filename).name, 'function': row.name, 'line': row.lineno}
                 for row in traceback.extract_tb(error.__traceback__)[-8:]]
        report['errors'].append({'error_type': type(error).__name__, 'error_sha256': sha(str(error).encode()),
                                 'failure_sites': sites})
        if helper is not None and isinstance(error, helper.DeploymentError):
            report['guard_reason'] = str(error)[:200]
    finally:
        if lock is not None:
            os.close(lock)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-json', required=True)
    args = parser.parse_args(argv)
    result = run(json.loads(args.target_json))
    raw = json.dumps(result, sort_keys=True, indent=2)
    if len(raw.encode()) > MAX_REPORT_BYTES:
        # Keep the complete report private on the caller's stdout only within
        # the bound; never silently truncate evidence and claim completion.
        result = {key: value for key, value in result.items() if key not in {'logs', 'login_http', 'diagnostic_log_copy'}}
        result.update(scan_complete=False, failure_phase='report_size_bound')
        result['errors'].append({'error_type': 'ReportSizeLimit', 'complete_report_sha256': sha(raw.encode())})
        raw = json.dumps(result, sort_keys=True, indent=2)
    print(raw, flush=True)
    return 0 if result['scan_complete'] else 1


if __name__ == '__main__':
    sys.exit(main())
