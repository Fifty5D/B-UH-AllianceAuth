
"""Reviewed, single-file receiver repair followed by the existing recovery API.

No application installation, token refresh, Member Audit update or new deploy.
The original installation record, failed-attempt evidence and database backup stay.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat

from ops.deploy.contracts import DeploymentError, ReceiverConfig
from ops.deploy.docker_host import (
    NGINX_UPSTREAM_NAME, _atomic_bytes, _parse_nginx_configuration,
    _verify_nginx_production_route, _walk_nginx_nodes,
)
from ops.deploy.receiver import _open_lock, _verify_root_owned_ancestors

BASE_COMMIT = "714a3407b7e84b70110162e22bf61f289773b23b"
BASE_DOCKER_SHA256 = "e24a7a7d92fa49be03fdc46e626bd61ca342ca72b89a163f38ea15d6e89d70a8"
BRIDGE_SHA256 = "907808212614a5db98c0e8cac013e8ea694556a08d8d160f729aecee3cdd8929"
SELECTION_SHA256 = "08c183989f7ea6d7ba06f8a3d4d4c4524886800fffd913035a637808553996ed"
LIBRARY = Path("/usr/local/lib/buh-platform-v2")
RECEIPT = Path("/etc/buh-platform-v2/VERIFIER-REPAIR.json")
SOURCE_NAMES = (
    "ops/__init__.py", "ops/deploy/__init__.py", "ops/deploy/contracts.py",
    "ops/deploy/docker_host.py", "ops/deploy/engine.py", "ops/deploy/receiver.py",
    "ops/release/__init__.py", "ops/release/buh_release.py", "ops/release/recovery_policy.py",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_read(path, maximum=2*1024*1024):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise DeploymentError("Private repair path is invalid")
    current = Path("/")
    for part in path.parent.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise DeploymentError("Private repair path ancestor changed")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or stat.S_IMODE(before.st_mode) & 0o022 or before.st_size > maximum):
            raise DeploymentError("Private repair file is unsafe")
        raw = stream.read(maximum+1)
        after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns):
            raise DeploymentError("Private repair file changed while reading")
        return raw


def load_source(path, expected, name):
    raw = private_read(path)
    if digest(raw) != expected:
        raise DeploymentError("Reviewed tooling source digest differs")
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader=None))
    module.__file__ = str(path)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def installed_identity(expected_docker=BASE_DOCKER_SHA256):
    record = json.loads(private_read("/etc/buh-platform-v2/INSTALL.json"))
    if record.get("source_commit") != BASE_COMMIT or not isinstance(record.get("files"), dict):
        raise DeploymentError("Installed receiver record identity changed")
    for name in SOURCE_NAMES:
        expected = expected_docker if name == "ops/deploy/docker_host.py" else record["files"].get(name)
        if digest(private_read(LIBRARY / name)) != expected:
            raise DeploymentError("Installed receiver source changed")
    return {"base_source_commit": BASE_COMMIT, "runtime_files_verified": len(SOURCE_NAMES),
            "docker_host_sha256": expected_docker,
            "install_record_sha256": digest(private_read("/etc/buh-platform-v2/INSTALL.json"))}


def verify_retained_plan(host, plan, expected_digest):
    """Bind the loaded object and both retained files to the reviewed bytes."""
    raw = private_read(host.config.state_dir / "active-recovery.json")
    if (digest(raw) != expected_digest or json.loads(raw) != plan
            or digest(private_read(host.backup_path / "RECOVERY.json")) != expected_digest):
        raise DeploymentError("Retained attempt/plan evidence changed")


def verify_retained_container(host, target, row, *, name=None):
    """Re-read identity and runtime; never establish a baseline from this read."""
    if (not isinstance(row["container_id"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["container_id"]) is None
            or not isinstance(row["image_id"], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", row["image_id"]) is None
            or type(row["restart_count"]) is not int or not 0 <= row["restart_count"] <= 2**31 - 1
            or not isinstance(row["started_at"], str)
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z", row["started_at"]) is None):
        raise DeploymentError("Reviewed previous runtime evidence is malformed")
    actual = host._run(
        ["docker", "inspect", "--format", "{{.Id}}|{{.Name}}|{{.Image}}|{{.State.Status}}|"
         "{{.State.OOMKilled}}|{{.RestartCount}}|{{.State.StartedAt}}|"
         '{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}', target],
        context="Retained previous runtime identity verification").strip().split("|")
    if (len(actual) != 8 or actual[0] != row["container_id"]
            or name is not None and actual[1] != "/" + name
            or actual[2:5] != [row["image_id"], "running", "false"]
            or actual[7] not in {"none", "healthy"}):
        raise DeploymentError("Retained previous runtime identity/image/restart changed")
    if actual[5:7] != [str(row["restart_count"]), row["started_at"]]:
        if not actual[5].isdigit():
            raise DeploymentError("Retained previous runtime restart count is invalid")
        try:
            host._verify_retained_worker_recycles(row["container_id"], int(actual[5]), actual[6])
        except DeploymentError as error:
            host.blocked_worker_recycles[row["container_id"]] = str(error)[:200]
            raise
        host.blocked_worker_recycles.pop(row["container_id"], None)


def verify_previous_slot_routing(host, plan, assessment, rows):
    """Admit the static-safety switch only with independent, bound host proof."""
    proof = assessment.get("previous_slot_routing")
    gunicorn = host.config.gunicorn_service
    previous = tuple(plan["previous_web_slots"])
    expected_slots = tuple(f"buh-web-previous-{plan['attempt_id']}-{index+1}"
                           for index in range(plan["auth_replica_counts"][gunicorn]))
    if (plan["phase"] != "candidate-slot-start-1" or not previous or previous != expected_slots
            or tuple(host.previous_web_slots) != previous
            or not isinstance(proof, dict)
            or set(proof) != {"schema_version", "attempt_id", "plan_sha256", "host_evidence_sha256",
                              "slots", "targets", "backup_targets", "upstream_sha256"}
            or type(proof["schema_version"]) is not int or proof["schema_version"] != 1
            or proof["attempt_id"] != plan["attempt_id"]
            or proof["plan_sha256"] != assessment["hold_sha256"]
            or proof["host_evidence_sha256"] != assessment["host_evidence_sha256"]
            or not isinstance(proof["slots"], list) or len(proof["slots"]) != len(previous)
            or not isinstance(proof["targets"], list) or not isinstance(proof["backup_targets"], list)):
        raise DeploymentError("Reviewed previous-slot routing evidence differs")
    # These are the existing static-protection / traffic-first rollback route
    # and the restored original route, including its final no-backup form.
    # Every selectable endpoint must still have the independently reviewed old image.
    route = (tuple(proof["targets"]), tuple(proof["backup_targets"]))
    if route not in ((previous, (gunicorn,)), ((gunicorn,), previous), ((gunicorn,), ())):
        raise DeploymentError("Retained traffic is not exclusively on a supported previous-version route")
    identities = {row["container_id"] for row in rows}
    for name, row in zip(previous, proof["slots"]):
        if (not isinstance(row, dict)
                or set(row) != {"name", "container_id", "image_id", "restart_count", "started_at"}
                or row["name"] != name or not isinstance(row["container_id"], str)
                or row["container_id"] in identities
                or row["image_id"] != plan["previous_images"][gunicorn][0]
                or type(row["restart_count"]) is not int or row["restart_count"] != 0):
            raise DeploymentError("Reviewed previous slot identity/image differs")
        verify_retained_container(host, name, row, name=name)
        identities.add(row["container_id"])
    required = {**plan["auth_replica_counts"], host.config.database_service: 1,
                host.config.redis_service: 1, host.config.proxy_service: 1}
    for role, count in required.items():
        retained = [row for row in rows if row["service"] == role]
        if (len(retained) != count
                or role in plan["previous_images"] and any(
                    row["image_id"] != plan["previous_images"][role][0] for row in retained)
                or set(host._running_service_containers(role, context="Retained previous service discovery"))
                != {row["container_id"] for row in retained}):
            raise DeploymentError("Retained previous service topology/image changed")
        for row in retained:
            verify_retained_container(host, row["container_id"], row)
    host._verify_previous_static_fallback()
    expected = host._render_upstream(route[0], backup_targets=route[1])
    path = host._upstream_path()
    if (proof["upstream_sha256"] != digest(expected.encode())
            or not path.is_file() or path.is_symlink() or path.stat().st_size != len(expected.encode())
            or path.read_bytes() != expected.encode()):
        raise DeploymentError("Retained previous-version upstream bytes changed")
    host._verify_proxy_upstream_bytes(expected, context="Reviewed previous-version route")
    configuration = host._proxy_exec("nginx", "-T", bounded_output=True,
                                     context="Reviewed previous-version production route discovery")
    _verify_nginx_production_route(configuration)
    upstreams = [node for node in _walk_nginx_nodes(_parse_nginx_configuration(configuration))
                 if node[0] == ("upstream", NGINX_UPSTREAM_NAME)]
    if upstreams != list(_parse_nginx_configuration(expected)):
        raise DeploymentError("Nginx does not exclusively select the reviewed previous endpoints")
    verify_retained_plan(host, plan, assessment["hold_sha256"])


def build_review(host, plan, assessment, baseline, prior):
    """Counts come from the reviewed host evidence, never the current inspector."""
    if (assessment["attempt_id"] != plan["attempt_id"]
            or assessment["hold_sha256"] != digest(private_read(host.config.state_dir / "active-recovery.json"))
            or prior.get("attempt_id") != plan["attempt_id"]
            or prior.get("supported_recovery_completed") is not False
            or prior.get("deployment_attempted") is not False
            or prior.get("before_host", {}).get("hold_sha256") != assessment["hold_sha256"]
            or any(plan["flags"][name] for name in ("workers_replacement_started",
                                                    "gunicorn_replacement_started"))):
        raise DeploymentError("Retained pre-replacement attempt identity changed")
    verify_retained_plan(host, plan, assessment["hold_sha256"])
    required = {*host.config.auth_services, host.config.database_service,
                host.config.redis_service, host.config.proxy_service}
    retained = {row["id"]: row for row in baseline["containers"]}
    rows, seen = [], set()
    for row in assessment["host_containers"]:
        role, prefix = row["service"], row["id"]
        if role not in required:
            continue
        matches = [old for identity, old in retained.items() if identity.startswith(prefix)]
        if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{12}", prefix):
            raise DeploymentError("Reviewed host container is not the retained identity")
        old = matches[0]
        if old["id"] in seen:
            raise DeploymentError("Reviewed host container is duplicated")
        if role in host.config.auth_services:
            previous = prior["before_host"]["live_auth_services"].get(role, [])
            if sum(item["container_id"] == old["id"] and item["image_id"] == old["image_id"]
                   for item in previous) != 1:
                raise DeploymentError("Retained service/image evidence differs")
        if (row["state"] != "running" or row["oom_killed"] is not False
                or row["health"] not in {None, "healthy"}):
            raise DeploymentError("Reviewed host state was not healthy")
        seen.add(old["id"])
        rows.append({"container_id": old["id"], "service": role, "image_id": old["image_id"],
                     "restart_count": row["restarts"], "started_at": row["started_at"]})
    review = {"schema_version": 1, "attempt_id": plan["attempt_id"],
            "plan_sha256": assessment["hold_sha256"],
            "host_evidence_sha256": assessment["host_evidence_sha256"],
            "host_observed_at": assessment["host_observed_at"],
            "containers": rows, "discord_429": assessment["discord_429"],
            "verification_since": prior["started_at"]}
    if "worker_memory_recycles" in assessment:
        review.update(schema_version=2, worker_memory_recycles=assessment["worker_memory_recycles"])
    if "retained_log_findings" in assessment:
        if "worker_memory_recycles" not in assessment:
            raise DeploymentError("Retained log proof requires the reviewed restart policy")
        review.update(schema_version=3, retained_log_findings=assessment["retained_log_findings"])
    if review["schema_version"] >= 2:
        host._apply_retained_verifier_review(review, plan, assessment["hold_sha256"])
    if plan["flags"]["traffic_switch_started"]:
        verify_previous_slot_routing(host, plan, assessment, rows)
    return review


def memberaudit_refs(completed, targets):
    if (completed.get("recovery_complete") is not True
            or completed.get("recovery_source_commit") != "c6ba1ead084a1c6c49a48dc85d8edaa80a287a24"
            or completed.get("counts", {}).get("recovered_with_existing_tokens") != 87
            or completed["counts"].get("sections_successfully_recovered") != 348):
        raise DeploymentError("Completed Member Audit evidence changed")
    by_id = {row["memberaudit_character_pk"]: row for row in completed["results"]}
    if len(by_id) != 87 or len(targets) != 87:
        raise DeploymentError("Completed Member Audit population differs")
    refs = []
    for target in targets:
        row = by_id[target["memberaudit_character_pk"]]
        if any(row[key] != target[key] for key in (
                "character_id", "user_id", "auth_link_pk", "memberaudit_character_pk")):
            raise DeploymentError("Member Audit identity evidence differs")
        statuses = [{"status_pk": section["status"]["status_pk"],
                     "section": section["section"],
                     "finished_at": section["status"]["run_finished_at"]}
                    for section in row["sections"] if section.get("verified") is True]
        if {s["status_pk"] for s in statuses} != {s["status_pk"] for s in target["sections"]}:
            raise DeploymentError("Completed Member Audit section evidence differs")
        refs.append({**{key: row[key] for key in ("character_id", "user_id", "auth_link_pk",
                                                 "memberaudit_character_pk")},
                     "token_pk": row["validated_token_pk"],
                     "token_inventory": row["after_token_inventory"],
                     "auth_digest": row["after_auth_identity_sha256"], "sections": statuses})
    return refs


MA_READ_CODE = """
from datetime import datetime
import hashlib
import json
from allianceauth.authentication.models import CharacterOwnership
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from esi.models import Token
from memberaudit.models import Character, CharacterUpdateStatus

def evidence_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, cls=DjangoJSONEncoder,
                                    separators=(',', ':')).encode()).hexdigest()

def status_projection(status):
    return {'status_pk': status.pk, 'section': status.section,
            'has_token_error': status.has_token_error, 'is_success': status.is_success,
            'run_started_at': status.run_started_at.isoformat() if status.run_started_at else None,
            'run_finished_at': status.run_finished_at.isoformat() if status.run_finished_at else None,
            'update_started_at': status.update_started_at.isoformat() if status.update_started_at else None,
            'update_finished_at': status.update_finished_at.isoformat() if status.update_finished_at else None,
            'content_hashes': [status.content_hash_1, status.content_hash_2, status.content_hash_3],
            'error_sha256': hashlib.sha256(status.error_message.encode()).hexdigest()}

def emit_preserved(refs, excluded, expected_excluded):
    characters = sections = 0
    # Database enforcement prevents an accidental write by this read-only check.
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
    with transaction.atomic():
        for ref in refs:
            ownership = CharacterOwnership.objects.filter(pk=ref['auth_link_pk']).values(
                'pk', 'user_id', 'character_id', 'owner_hash').get()
            if evidence_digest(ownership) != ref['auth_digest']:
                raise ValueError('Member Audit Auth identity changed')
            member = Character.objects.select_related('eve_character').get(pk=ref['memberaudit_character_pk'])
            if member.is_disabled or member.eve_character.character_id != ref['character_id']:
                raise ValueError('Member Audit character changed')
            ids = list(Token.objects.filter(character_id=ref['character_id']).order_by('pk').values_list('pk', flat=True))
            if ids != ref['token_inventory']:
                raise ValueError('Member Audit token inventory changed')
            token = Token.objects.get(pk=ref['token_pk'], character_id=ref['character_id'], user_id=ref['user_id'])
            if token.character_owner_hash != ownership['owner_hash']:
                raise ValueError('Member Audit token ownership changed')
            for selected in ref['sections']:
                status = CharacterUpdateStatus.objects.get(pk=selected['status_pk'], character=member, section=selected['section'])
                if (status.has_token_error or status.is_success is not True or not status.content_hash_1
                        or not status.run_finished_at
                        or status.run_finished_at < datetime.fromisoformat(selected['finished_at'])):
                    raise ValueError('Recovered Member Audit section is not successful')
                sections += 1
            characters += 1
        older = [status_projection(CharacterUpdateStatus.objects.get(pk=item['status_pk'],
                 character_id=item['memberaudit_character_pk'])) for item in excluded]
        if len(older) != 61 or evidence_digest(older) != expected_excluded:
            raise ValueError('Unrelated older Member Audit state changed')
    print('BUH_PRESERVED_MEMBERAUDIT_BEGIN')
    print(json.dumps({'read_only': True, 'characters': characters, 'successful_sections': sections,
                      'auth_and_token_inventory_preserved': True,
                      'older_sections_unchanged': True, 'older_section_count': len(older)}))
    print('BUH_PRESERVED_MEMBERAUDIT_END')
"""


def verify_memberaudit(host, refs, excluded, completed):
    code = (MA_READ_CODE + "\nemit_preserved(" + repr(refs) + "," + repr(excluded) + ","
            + repr(completed["excluded_after"]["states_sha256"]) + ")")
    if len(code.encode()) > 100 * 1024:
        raise DeploymentError("Read-only Member Audit check exceeds its argument bound")
    raw = host._manage_live("shell", "--no-imports", "-c", code,
                            context="Read-only preservation of completed Member Audit recovery")
    begin, end = "BUH_PRESERVED_MEMBERAUDIT_BEGIN\n", "\nBUH_PRESERVED_MEMBERAUDIT_END"
    if raw.count(begin) != 1 or raw.count(end) != 1:
        raise DeploymentError("Read-only Member Audit evidence is incomplete")
    result = json.loads(raw.split(begin, 1)[1].split(end, 1)[0])
    if result.get("read_only") is not True or result.get("characters") != 87 or result.get("successful_sections") != 348:
        raise DeploymentError("Completed Member Audit state is not preserved")
    return result


def all_checks(host):
    results, warnings = [], []
    for name, action in host.restored_health_checks(None, set()):
        row = {"check": name, "passed": False}
        try:
            value = action()
            row["passed"] = True
            if name == "retained-interval-logs":
                warnings.extend(value)
        except Exception as error:
            row.update(error_type=type(error).__name__, error_sha256=digest(str(error).encode()))
            if isinstance(error, DeploymentError):
                row["guard_reason"] = str(error)[:200]
        results.append(row)
    return results, warnings


def install_single_file(host, candidate, source_hash, review_path, commit):
    """Use the established additive single-file repair/atomic-restore pattern."""
    target = LIBRARY / "ops/deploy/docker_host.py"
    current = private_read(target)
    reviewed = private_read(candidate)
    if digest(current) != BASE_DOCKER_SHA256 or digest(reviewed) != source_hash:
        raise DeploymentError("Single-file repair payload identity differs")
    if RECEIPT.exists() or RECEIPT.is_symlink():
        raise DeploymentError("Verifier repair receipt exists; do not repeat activation")
    compile(reviewed, "reviewed-docker-host.py", "exec")
    backup = Path("/var/backups/buh-receiver-upgrade") / (
        "retained-verifier-" + source_hash[:12])
    _verify_root_owned_ancestors(backup)
    backup.mkdir(mode=0o700, exist_ok=False)
    original_install = private_read("/etc/buh-platform-v2/INSTALL.json")
    _atomic_bytes(backup / "docker_host.py", current, 0o600, owner=(0, 0))
    _atomic_bytes(backup / "INSTALL.json", original_install, 0o600, owner=(0, 0))
    review = private_read(review_path)
    _atomic_bytes(backup / "VERIFIER-REVIEW.json", review, 0o600, owner=(0, 0))
    result = {"schema_version": 1, "result": "receiver-verifier-file-repaired",
              "source_commit": commit, "base_source_commit": BASE_COMMIT,
              "base_install_sha256": digest(original_install), "path": str(target),
              "previous_sha256": digest(current), "sha256": source_hash,
              "review_sha256": digest(review), "backup_path": str(backup),
              "attempt_id": host.retained_verifier_review["attempt_id"],
              "deployment_performed": False, "memberaudit_mutation_performed": False}
    try:
        _atomic_bytes(host.backup_path / "VERIFIER-REVIEW.json", review, 0o600, owner=(0, 0))
        _atomic_bytes(target, reviewed, 0o644, owner=(0, 0))
        installed_identity(source_hash)
        _atomic_bytes(RECEIPT, (json.dumps(result, sort_keys=True) + "\n").encode(), 0o600, owner=(0, 0))
    except BaseException:
        _atomic_bytes(target, current, 0o644, owner=(0, 0))
        if digest(private_read(target)) != BASE_DOCKER_SHA256:
            raise DeploymentError("Single-file verifier repair restore failed; preserve evidence")
        raise
    return result



def reconciliation_host(BaseHost, memberaudit_gate, structures_gate, recycle_evidence_gate=None):
    """Add preservation reads inside the supported pre-cleanup verification."""
    class ScopedReconciliationHost(BaseHost):
        def _verify_restored(self, bundle, replaced_services):
            super()._verify_restored(bundle, replaced_services)
            memberaudit_gate(self)
            structures_gate(self)
            if recycle_evidence_gate is not None:
                recycle_evidence_gate(self)
    return ScopedReconciliationHost


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--assessment", type=Path, required=True)
    parser.add_argument("--assessment-sha256", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--pilot-directory", type=Path, required=True)
    parser.add_argument("--ma-directory", type=Path, required=True)
    parser.add_argument("--operation", choices=("verify", "install-and-recover"), default="verify")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "scan_complete": False,
              "read_only": args.operation == "verify",
              "started_at": datetime.now(timezone.utc).isoformat(), "source_commit": args.commit,
              "deployment_attempted": False, "memberaudit_mutation_attempted": False,
              "supported_recovery_attempted": False}
    lock, host, phase = None, None, "validate_source_and_assessment"
    try:
        if (os.geteuid() != 0 or re.fullmatch(r"[0-9a-f]{40}", args.commit) is None
                or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in
                       (args.candidate_sha256, args.assessment_sha256))):
            raise DeploymentError("Reviewed root repair arguments are invalid")
        raw = private_read(args.assessment, 64*1024)
        if digest(raw) != args.assessment_sha256:
            raise DeploymentError("Reviewed private assessment differs")
        assessment = json.loads(raw)
        bridge = load_source(args.bridge, BRIDGE_SHA256, "qualified_recovery_bridge")
        module = load_source(args.candidate, args.candidate_sha256, "ops.deploy.reviewed_verifier_candidate")
        pilot, owner_source = bridge.qualified_pilot(args.pilot_directory)
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        lock = _open_lock(config)
        phase = "verify_installed_and_retained_state"
        report["installed_before"] = installed_identity()
        loaded = module.DockerHost.load_incomplete_plan(config)
        if loaded is None:
            raise DeploymentError("No retained attempt; do not repeat recovery")
        host, plan = loaded
        report["before_host"] = pilot.verify_host(host, config, assessment["attempt_id"])
        _, baseline, baseline_digest = pilot.latest_report()
        if baseline_digest != assessment["baseline_report_sha256"]:
            raise DeploymentError("Retained qualified incident evidence differs")
        prior_raw = private_read(Path(assessment["prior_recovery_report"]), 256*1024)
        prior = json.loads(prior_raw)
        report["original_failed_recovery_report_sha256"] = digest(prior_raw)
        review = build_review(host, plan, assessment, baseline, prior)
        if plan["flags"]["traffic_switch_started"]:
            report["previous_slot_routing"] = {
                "verified": True, "attempt_id": plan["attempt_id"], "plan_sha256": assessment["hold_sha256"],
                "evidence_sha256": digest(json.dumps(assessment["previous_slot_routing"], sort_keys=True).encode()),
            }
        review_path = args.assessment.parent / "VERIFIER-REVIEW.json"
        _atomic_bytes(review_path, (json.dumps(review, sort_keys=True) + "\n").encode(), 0o600, owner=(0, 0))
        host._load_retained_verifier_review(plan, assessment["hold_sha256"], review_path=review_path)
        report["restart_baseline"] = {"source": "reviewed-retained-host-evidence",
                                      "containers": len(host.restart_baselines), "review_sha256": digest(private_read(review_path)),
                                      "host_evidence_sha256": review["host_evidence_sha256"],
                                      "observed_at": review["host_observed_at"]}
        phase = "read_only_application_preservation"
        target, roster = pilot.select_pilot(baseline, "Fifty5D"), pilot.snapshot_targets(baseline)
        report["before_owners"] = bridge.snapshot(host, pilot, owner_source, target, roster)
        bridge.require_healthy_owners(report["before_owners"], roster)
        selection = load_source(args.ma_directory / "memberaudit_selection.py", SELECTION_SHA256, "qualified_ma_selection")
        completed = json.loads(private_read(args.ma_directory / "memberaudit-report.json"))
        targets, excluded = selection.select_outage(baseline)
        refs = memberaudit_refs(completed, targets)
        report["memberaudit_before"] = verify_memberaudit(host, refs, excluded, completed)
        report["retained_database_backup"] = bridge.verify_backup(host, pilot)
        phase = "all_read_only_verifier_checks"
        report["pre_install_checks"], report["recovered_log_warnings"] = all_checks(host)
        report["proven_worker_recycles"] = host.accepted_worker_recycles
        report["blocked_worker_recycles"] = host.blocked_worker_recycles
        report["reviewed_restart_baseline_unchanged"] = host.restart_baselines == {
            row["container_id"]: row["restart_count"] for row in review["containers"]}
        if not report["pre_install_checks"] or any(not row["passed"] for row in report["pre_install_checks"]):
            raise DeploymentError("Current verification checks failed; no tooling activation or cleanup")
        if not any("recovered warning: Discord nickname HTTP 429 -> HTTP 204" in warning
                   for warning in report["recovered_log_warnings"]):
            raise DeploymentError("The exact original Discord recovery warning was not retained")
        if "retained_log_findings" in review:
            if any(not any(prefix in warning for warning in report["recovered_log_warnings"]) for prefix in (
                    "recovered warning: exact historical asset-name Invalid IDs 404",
                    "recovered warning: exact handled DEBUG CallbackRedirect lookup")):
                raise DeploymentError("Exact historical finding recovery warnings were not retained")
            report["retained_log_findings"] = {
                "report_sha256": review["retained_log_findings"]["report_sha256"],
                "report_path": review["retained_log_findings"]["report_path"],
                "exact_records": sum(len(rows) for rows in host.recovered_retained_log_records.values()),
                "current_assets_and_callback_source_verified": True}
        if args.operation == "verify":
            report["scan_complete"] = True
            report["ready_for_single_file_activation"] = True
            return report
        phase = "install_single_file_correction"
        report["tooling_installation"] = install_single_file(host, args.candidate, args.candidate_sha256,
                                                           review_path, args.commit)
        phase = "supported_retained_recovery"
        report["supported_recovery_attempted"] = True
        def memberaudit_gate(current):
            report["memberaudit_before_cleanup"] = verify_memberaudit(current, refs, excluded, completed)

        def structures_gate(current):
            report["owners_before_cleanup"] = bridge.snapshot(current, pilot, owner_source, target, roster)
            bridge.require_healthy_owners(report["owners_before_cleanup"], roster)

        def recycle_evidence_gate(current):
            report["proven_worker_recycles_before_cleanup"] = current.accepted_worker_recycles
            record = {"schema_version": 1, "attempt_id": plan["attempt_id"],
                      "plan_sha256": assessment["hold_sha256"], "review_sha256": digest(private_read(review_path)),
                      "reviewed_baseline_unchanged": current.restart_baselines == {
                          row["container_id"]: row["restart_count"] for row in review["containers"]},
                      "proven_worker_recycles": current.accepted_worker_recycles}
            if record["reviewed_baseline_unchanged"] is not True:
                raise DeploymentError("Original reviewed restart baseline changed")
            _atomic_bytes(current.backup_path / "WORKER-RECYCLES.json",
                          (json.dumps(record, sort_keys=True) + "\n").encode(), 0o600, owner=(0, 0))
            if "retained_log_findings" in review:
                findings = {"schema_version": 1, "attempt_id": plan["attempt_id"],
                    "plan_sha256": assessment["hold_sha256"], "review_sha256": digest(private_read(review_path)),
                    "proof": review["retained_log_findings"],
                    "recovered_warnings": current._verified_retained_log_findings(),
                    "records": [{"source": source, "header_sha256": header,
                                 "ordered_original_line_sha256": rows}
                                for (source, header), rows in sorted(current.recovered_retained_log_records.items())]}
                _atomic_bytes(current.backup_path / "RETAINED-LOG-FINDINGS.json",
                              (json.dumps(findings, sort_keys=True) + "\n").encode(), 0o600, owner=(0, 0))
                report["retained_log_findings_before_cleanup"] = {
                    "report_sha256": findings["proof"]["report_sha256"], "warnings": findings["recovered_warnings"],
                    "evidence_path": str(current.backup_path / "RETAINED-LOG-FINDINGS.json")}

        ScopedReconciliationHost = reconciliation_host(
            module.DockerHost, memberaudit_gate, structures_gate, recycle_evidence_gate)
        report["recovery"] = bridge.reconcile(config, ScopedReconciliationHost, pilot, owner_source,
                                              assessment["attempt_id"], assessment["hold_sha256"], "Fifty5D", True)
        if report["recovery"].get("scan_complete") is not True:
            raise DeploymentError("Supported retained recovery did not complete; do not deploy")
        phase = "verify_completion_and_preservation"
        report["memberaudit_after"] = verify_memberaudit(host, refs, excluded, completed)
        report["installed_after"] = installed_identity(args.candidate_sha256)
        bridge.require_backup_unchanged(host, report["retained_database_backup"])
        report["scan_complete"] = True
        report["supported_recovery_completed"] = True
        report["hold_retired"] = module.DockerHost.load_incomplete_plan(config) is None
        report["immutable_release_changed"] = False
    except Exception as error:
        report["failure_phase"] = phase
        report["error_type"] = type(error).__name__
        report["error_sha256"] = digest(str(error).encode())
        if isinstance(error, DeploymentError):
            report["guard_reason"] = str(error)[:200]
        if host is not None:
            report["proven_worker_recycles"] = host.accepted_worker_recycles
            report["blocked_worker_recycles"] = host.blocked_worker_recycles
        report["preserve_recovery_resources"] = True
    finally:
        if lock is not None:
            os.close(lock)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


if __name__ == "__main__":
    value = main()
    print(json.dumps(value, indent=2, sort_keys=True))
    raise SystemExit(0 if value.get("scan_complete") else 1)
