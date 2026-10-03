"""Owner-operated reconciliation of one already-retained deployment attempt.

Uses the installed supported recovery API. No release deployment, new migrations,
token refresh, Member Audit reset, manual notification forwarding or custom cleanup.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import SimpleNamespace

PILOT_COMMIT = "badc74fd5314800eaff9f0bfa0dbaec332240e15"
PILOT_HASHES = {
    "run_structures_pilot.py": "47eef33d8c3f5bb1714d87d3d3267c5305dedd4a784fd057342d10862865a836",
    "structures_recovery.py": "ce35433fc7b2816683df45a5579e9ffed29caa11db9f15623197018d93f59329",
}
INSTALLED_COMMIT = "714a3407b7e84b70110162e22bf61f289773b23b"
RUNTIME_SOURCES = (
    "ops/__init__.py", "ops/deploy/__init__.py", "ops/deploy/contracts.py",
    "ops/deploy/docker_host.py", "ops/deploy/engine.py", "ops/deploy/receiver.py",
    "ops/release/__init__.py", "ops/release/buh_release.py", "ops/release/recovery_policy.py",
)


class RecoveryGate(ValueError):
    def __init__(self, category):
        self.category = category
        super().__init__(category)


def private_read(path, maximum=1024 * 1024):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RecoveryGate("unsafe_private_path")
    current = Path("/")
    for part in path.parent.parts[1:]:
        current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise RecoveryGate("unsafe_private_ancestor")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or stat.S_IMODE(before.st_mode) & 0o022 or before.st_size > maximum):
            raise RecoveryGate("unsafe_private_file")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if len(data) != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RecoveryGate("private_file_changed")
    return data


def qualified_pilot(directory):
    directory = Path(directory)
    if not re.fullmatch(r"/root/buh-structures-pilot-badc74fd-[A-Za-z0-9_-]+", str(directory)):
        raise RecoveryGate("unexpected_pilot_source_directory")
    sources = {}
    for name, expected in PILOT_HASHES.items():
        data = private_read(directory / name)
        if hashlib.sha256(data).hexdigest() != expected:
            raise RecoveryGate("qualified_pilot_source_changed")
        sources[name] = data
    namespace = {"__name__": "qualified_incident_pilot", "__file__": str(directory / "run_structures_pilot.py")}
    exec(compile(sources["run_structures_pilot.py"], namespace["__file__"], "exec"), namespace)
    return SimpleNamespace(**namespace), sources["structures_recovery.py"].decode("utf-8")


def verify_installed():
    receipt = json.loads(private_read("/etc/buh-platform-v2/INSTALL.json"))
    if (receipt.get("schema_version") != 1 or receipt.get("source_commit") != INSTALLED_COMMIT
            or not isinstance(receipt.get("files"), dict)):
        raise RecoveryGate("installed_receiver_receipt_changed")
    for relative in RUNTIME_SOURCES:
        expected = receipt["files"].get(relative)
        actual = private_read(Path("/usr/local/lib/buh-platform-v2") / relative)
        if not isinstance(expected, str) or hashlib.sha256(actual).hexdigest() != expected:
            raise RecoveryGate("installed_receiver_file_changed")
    return {"source_commit": INSTALLED_COMMIT, "required_runtime_files_verified": len(RUNTIME_SOURCES)}


def snapshot(host, pilot, source, target, roster):
    code = ("exec(compile(" + repr(source) + ", '<buh-structures-recovery>', 'exec'));emit("
            + repr(target) + ",apply=False,snapshot_targets=" + repr(roster) + ")")
    raw = host._manage_live("shell", "--no-imports", "-c", code,
                            context="Read-only Structures recovery completion gate")
    result = pilot.parse_result(raw)
    value = result.get("outage_snapshot") or {}
    if value.get("read_only") is not True:
        raise RecoveryGate("current_owner_snapshot_incomplete")
    return value


def require_healthy_owners(value, roster):
    rows = value.get("owners") or []
    expected = {(r["owner_pk"], r["owner_character_pk"], r["character_id"], r["token_pk"]) for r in roster}
    if (not expected or len(expected) != len(roster) or len(roster) > 16 or len(rows) != len(roster)
            or {(r.get("owner_pk"), r.get("owner_character_pk"), r.get("character_id"), r.get("token_pk")) for r in rows} != expected):
        raise RecoveryGate("incident_owner_roster_changed")
    for row in rows:
        state = row.get("state") or {}
        if (any(row.get(key) is not True for key in ("same_auth_link_pk", "same_owner_pk", "same_character_id"))
                or state.get("owner_active") is not True or state.get("enabled") is not True
                or state.get("disabled_for_no_valid_token") is not False or state.get("owner_is_up") is not True
                or (state.get("freshness") or {}).get("all") is not True
                or state.get("token_identity_matches_auth") is not True
                or state.get("token_has_refresh_credential") is not True
                or state.get("required_missing_scopes") != []):
            raise RecoveryGate("active_owner_current_health_not_proven")


def verify_backup(host, pilot):
    path = host.backup_path / "database.sql.gz"
    manifest = json.loads(pilot.private_bytes(host.backup_path / "BACKUP.json", 64 * 1024))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077
                or not 1 <= info.st_size <= 1024 ** 3 or info.st_size != manifest.get("size")
                or manifest.get("filename") != "database.sql.gz"):
            raise RecoveryGate("retained_database_backup_changed")
        digest = hashlib.sha256()
        remaining = info.st_size
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise RecoveryGate("retained_database_backup_truncated")
            digest.update(block)
            remaining -= len(block)
        after = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RecoveryGate("retained_database_backup_changed")
    if digest.hexdigest() != manifest.get("sha256"):
        raise RecoveryGate("retained_database_backup_hash_mismatch")
    return {"filename": "database.sql.gz", "size": info.st_size, "sha256": digest.hexdigest(),
            "device": info.st_dev, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns}


def require_backup_unchanged(host, before):
    info = (host.backup_path / "database.sql.gz").lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077
            or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            != (before["device"], before["inode"], before["size"], before["mtime_ns"])):
        raise RecoveryGate("retained_database_backup_changed")


def verify_after(host, pilot, before):
    config = host.config
    marker = json.loads(pilot.private_bytes(config.app_dir / "conf/buh-platform-v2/CURRENT.json", 16 * 1024))
    deployed = json.loads(pilot.private_bytes(config.state_dir / "current.json", 16 * 1024))
    if (marker.get("platform_version") != "0.8.2" or marker.get("release_commit") != pilot.OLD_RELEASE
            or marker.get("manifest_sha256") != pilot.OLD_MANIFEST
            or deployed.get("platform_version") != "0.8.2" or deployed.get("release_commit") != pilot.OLD_RELEASE
            or deployed.get("manifest_sha256") != pilot.OLD_MANIFEST or deployed.get("source_commit") != pilot.OLD_SOURCE):
        raise RecoveryGate("restored_application_identity_changed")
    host._require_live_images({role: image[0] for role, image in host.previous_images.items()})
    live = {}
    for role in config.auth_services:
        ids = host._running_service_containers(role, context="Verified restored runtime counts")
        if len(ids) != host.auth_replica_counts[role]:
            raise RecoveryGate("restored_replica_count_changed")
        live[role] = list(ids)
    for role, ids in live.items():
        expected = {row["container_id"] for row in before["live_auth_services"][role]}
        resolved = []
        for identity in ids:
            matches = [item for item in expected if item.startswith(identity)]
            if len(matches) != 1:
                raise RecoveryGate("unchanged_runtime_identity_not_proven")
            resolved.append(matches[0])
        if set(resolved) != expected or len(resolved) != len(expected):
            raise RecoveryGate("unchanged_runtime_identity_not_proven")
        live[role] = sorted(resolved)
    route = pilot.private_bytes(config.app_dir / config.nginx_upstream_file, 64 * 1024)
    actual = host._proxy_exec("cat", config.nginx_upstream_container_file, bounded_output=True,
                             context="Verified restored traffic configuration")
    if hashlib.sha256(route).digest() != hashlib.sha256(actual.encode()).digest():
        raise RecoveryGate("restored_proxy_route_differs")
    targets = re.findall(rb"\bserver[ \t]+([A-Za-z0-9_.-]+):([0-9]{1,5})[ \t;]", route)
    if len(targets) != 1 or targets[0] != (config.gunicorn_service.encode(), str(config.gunicorn_port).encode()):
        raise RecoveryGate("final_route_is_not_exclusively_restored_service")
    resources = pilot.host_resources()
    pilot.guard_resources(resources)
    usage = os.statvfs(config.app_dir)
    free, total = usage.f_bavail * usage.f_frsize, usage.f_blocks * usage.f_frsize
    if free < max(10 * 1024 ** 3, total // 10):
        raise RecoveryGate("restored_disk_safety_threshold")
    host._public_smoke_checks()
    for name in (*host.previous_web_slots, *host.candidate_web_slots):
        remaining = host._run(["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
                              bounded_output=True, context="Supported safety container cleanup evidence")
        if remaining.strip():
            raise RecoveryGate("supported_safety_slot_cleanup_incomplete")
    for pin in host.previous_image_pins.values():
        remaining = host._run(["docker", "image", "ls", "--filter", f"reference={pin}", "--format", "{{.ID}}"],
                              bounded_output=True, context="Supported rollback pin cleanup evidence")
        if remaining.strip():
            raise RecoveryGate("supported_image_pin_cleanup_incomplete")
    return {"platform_version": "0.8.2", "release_commit": marker["release_commit"],
            "live_service_container_ids": live, "active_upstream_sha256": hashlib.sha256(route).hexdigest(),
            "traffic_only_verified_previous_image": True, "public_smoke_passed": True,
            "supported_safety_slots_removed": True, "supported_image_pins_removed": True,
            "disk": {"free_bytes": free, "total_bytes": total}, **resources}


def failed_recovery_checks(host):
    """Read every existing restoration probe after a failed recovery, without retrying it."""
    checks = []
    try:
        for name, action in host.restored_health_checks(None, set()):
            row = {"check": name if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", name) else "unclassified", "passed": False}
            try:
                action()
                row["passed"] = True
            except Exception as error:
                text = str(error)
                row.update(error_type=type(error).__name__, message_length=len(text),
                           message_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
                shapes = {
                    "structures_selection": "No valid character found for sync",
                    "fatal_logs": "New fatal AllianceAuth log pattern",
                    "pending_migrations": "pending migrations",
                    "replica_or_image": "container",
                    "provider_throttling": "429",
                }
                row["known_error_shapes"] = [key for key, shape in shapes.items() if shape in text]
                if hasattr(error, "scan_complete"):
                    row["log_scan_complete"] = bool(error.scan_complete)
            checks.append(row)
    except Exception as error:
        checks.append({"check": "diagnostic_iteration", "passed": False, "error_type": type(error).__name__})
    return checks


def reconcile(config, Host, pilot, source, attempt, expected_hold, pilot_name, recover):
    output = {"schema_version": 1, "read_only": not recover, "attempt_id": attempt,
              "started_at": datetime.now(timezone.utc).isoformat(), "scan_complete": False,
              "deployment_attempted": False, "memberaudit_mutation_attempted": False,
              "supported_recovery_attempted": False, "supported_recovery_completed": False}
    phase = "validate_existing_plan"
    try:
        loaded = Host.load_incomplete_plan(config)
        if loaded is None:
            raise RecoveryGate("no_active_incident_plan_do_not_repeat_cleanup")
        host, plan = loaded
        if plan.get("attempt_id") != attempt or plan.get("phase") != "candidate-slot-start-1":
            raise RecoveryGate("retained_attempt_changed")
        output["before_host"] = pilot.verify_host(host, config, attempt)
        if output["before_host"]["hold_sha256"] != expected_hold:
            raise RecoveryGate("retained_hold_digest_changed")
        _, baseline, digest = pilot.latest_report()
        if baseline.get("attempt_id") != attempt:
            raise RecoveryGate("qualified_baseline_attempt_changed")
        output["baseline_report_sha256"] = digest
        target, roster = pilot.select_pilot(baseline, pilot_name), pilot.snapshot_targets(baseline)
        phase = "verify_current_owners"
        output["before_owners"] = snapshot(host, pilot, source, target, roster)
        require_healthy_owners(output["before_owners"], roster)
        phase = "verify_retained_database_backup"
        output["retained_database_backup"] = verify_backup(host, pilot)
        output["retained_safety_slots"] = list(host.previous_web_slots) + list(host.candidate_web_slots)
        if not recover:
            output["ready_for_supported_recovery"] = True
            output["scan_complete"] = True
            return output
        phase = "supported_recovery"
        output["supported_recovery_attempted"] = True
        outcome = Host.recover_incomplete_plan(config)
        if not isinstance(outcome, str) or not outcome:
            raise RecoveryGate("supported_recovery_result_incomplete")
        output["supported_recovery_completed"] = True
        phase = "verify_completed_recovery"
        if Host.load_incomplete_plan(config) is not None:
            raise RecoveryGate("active_incident_plan_not_retired")
        output["after_host"] = verify_after(host, pilot, output["before_host"])
        require_backup_unchanged(host, output["retained_database_backup"])
        output["after_owners"] = snapshot(host, pilot, source, target, roster)
        require_healthy_owners(output["after_owners"], roster)
        def inventories(value):
            return {row["owner_pk"]: (row["state"]["auth_link_pk"], row["state"]["existing_token_ids"])
                    for row in value["owners"]}
        if inventories(output["before_owners"]) != inventories(output["after_owners"]):
            raise RecoveryGate("token_or_auth_inventory_changed")
        output["token_and_auth_inventory_preserved"] = True
        output["database_backup_preserved"] = True
        output["recovery_completion_is_proven"] = True
        output["scan_complete"] = True
        phase = "save_completion_receipt"
        output["completion_verified_at"] = datetime.now(timezone.utc).isoformat()
        receipt = host.backup_path / "STRUCTURES-INCIDENT-RECOVERY.json"
        output["completion_receipt_path"] = str(receipt)
        descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(output, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as error:
        output["scan_complete"] = False
        output["error_type"] = type(error).__name__
        output["guard_reason"] = error.category if isinstance(error, RecoveryGate) else None
        output["failure_phase"] = phase
        if phase == "supported_recovery" and not output["supported_recovery_completed"]:
            output["read_only_failure_checks"] = failed_recovery_checks(host)
        output["failure_sites"] = []
        trace = error.__traceback__
        while trace is not None and len(output["failure_sites"]) < 12:
            frame = trace.tb_frame
            component = frame.f_globals.get("__name__", "")
            if frame.f_code.co_filename == __file__ or re.fullmatch(r"ops\.deploy(?:\.[A-Za-z_][A-Za-z0-9_]*)*", component):
                output["failure_sites"].append({"component": "recovery_bridge" if frame.f_code.co_filename == __file__ else component,
                                                "function": frame.f_code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
    finally:
        output["finished_at"] = datetime.now(timezone.utc).isoformat()
    return output


def main():
    output = {"schema_version": 1, "scan_complete": False, "deployment_attempted": False,
              "memberaudit_mutation_attempted": False, "supported_recovery_attempted": False}
    descriptor = None
    try:
        directory, attempt, expected_hold, name, mode = sys.argv[1:]
        if (os.geteuid() != 0 or mode not in {"report", "recover"}
                or not re.fullmatch(r"gh-[1-9][0-9]*-[1-9][0-9]*", attempt)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hold)
                or not 1 <= len(name) <= 100 or any(ord(c) < 32 for c in name)):
            raise RecoveryGate("invalid_recovery_invocation")
        output["read_only"] = mode == "report"
        pilot, source = qualified_pilot(directory)
        installed = verify_installed()
        sys.path.insert(0, "/usr/local/lib/buh-platform-v2")
        from ops.deploy.contracts import ReceiverConfig
        from ops.deploy.docker_host import DockerHost
        from ops.deploy.receiver import _open_lock
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        descriptor = _open_lock(config)
        output = reconcile(config, DockerHost, pilot, source, attempt, expected_hold, name, mode == "recover")
        output["installed_receiver"] = installed
    except Exception as error:
        output.update(error_type=type(error).__name__, failure_phase="host_setup",
                      guard_reason=error.category if isinstance(error, RecoveryGate) else None)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output.get("scan_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
