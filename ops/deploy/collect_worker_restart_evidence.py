"""Bounded, read-only restart evidence; never installs, retries, or rebases."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import zlib


INSPECT_FORMAT = (
    '[{{json .Id}},{{json .Name}},{{json .Image}},{{json .RestartCount}},'
    '{{json .State}},{{json .Config.Labels}},'
    '{{json (index .Config "Healthcheck")}},{{json .HostConfig.RestartPolicy}},'
    '{{json .Mounts}}]'
)
MAX_ROWS = 30000
MAX_RECORD = 65536
MAX_WINDOW_BYTES = 32 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def decode_inspection(raw):
    """Missing Health remains absent; malformed/present health fails closed."""
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 9:
        raise ValueError("invalid_inspection_shape")
    identity, name, image, restarts, state, labels, check, policy, mounts = value
    if (not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None
            or not isinstance(image, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None
            or not isinstance(name, str) or not name.startswith("/")
            or type(restarts) is not int or not 0 <= restarts <= 2**31 - 1
            or not isinstance(state, dict) or type(state.get("OOMKilled")) is not bool
            or type(state.get("ExitCode")) is not int
            or state.get("Status") not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
            or not isinstance(state.get("StartedAt"), str)
            or not isinstance(state.get("FinishedAt"), str)
            or labels is not None and not isinstance(labels, dict)
            or check is not None and not isinstance(check, dict)
            or not isinstance(policy, dict) or not isinstance(mounts, list)):
        raise ValueError("invalid_inspection_state")
    present = "Health" in state
    health = state.get("Health")
    if present and (not isinstance(health, dict)
                    or health.get("Status") not in {"starting", "healthy", "unhealthy"}):
        raise ValueError("invalid_optional_health_metadata")
    result = {
        "container_id": identity, "name": name, "image_id": image,
        "restart_count": restarts, "status": state["Status"],
        "oom_killed": state["OOMKilled"], "exit_code": state["ExitCode"],
        "started_at": state["StartedAt"], "finished_at": state["FinishedAt"],
        "state_error_present": bool(state.get("Error")),
        "state_error_sha256": digest(str(state.get("Error", "")).encode()),
        "service": (labels or {}).get("com.docker.compose.service"),
        "health_metadata_present": present,
        "health": health["Status"] if present else None,
        "restart_policy": {key: policy.get(key) for key in ("Name", "MaximumRetryCount")},
        "memory_check_mounts": [{key: item.get(key) for key in ("Source", "Destination", "RW")}
                                for item in mounts if item.get("Destination") == "/memory_check.sh"],
    }
    if (isinstance(check, dict) and isinstance(check.get("Test"), list)
            and len(check["Test"]) == 3 and check["Test"][:2] == ["CMD", "/memory_check.sh"]
            and isinstance(check["Test"][2], str) and check["Test"][2].isdigit()):
        result["memory_healthcheck"] = {key: check.get(key) for key in
                                       ("Test", "Interval", "Timeout", "StartPeriod", "Retries")}
    else:
        result["memory_healthcheck"] = None
    result["recent_memory_check_outputs"] = []
    for entry in (health or {}).get("Log", []):
        if not isinstance(entry, dict):
            raise ValueError("invalid_health_log_metadata")
        output = str(entry.get("Output", ""))
        safe_lines = [line for line in output.splitlines() if re.fullmatch(
            r"(?:/tmp/health\.stat (?:exists\.|does not exist\. Creating)|Testing Mem: \d+ / \d+|"
            r"All Ok|Un-healthy! Check #\d+|Starting a restart of this the container\.\.\.)", line)]
        result["recent_memory_check_outputs"].append({
            "start": entry.get("Start"), "end": entry.get("End"),
            "exit_code": entry.get("ExitCode"), "output_sha256": digest(output.encode()),
            "known_memory_lines": safe_lines,
        })
    return result


def command(argv, maximum=4 * 1024 * 1024, timeout=20):
    process = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=False, timeout=timeout)
    if process.returncode or len(process.stdout) > maximum:
        raise RuntimeError("bounded_read_command_failed_" + digest(process.stderr))
    return process.stdout


def inspect(target):
    return decode_inspection(command(["docker", "inspect", "--format", INSPECT_FORMAT, target], 65536))


def private_read(path, maximum=2 * 1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != 0
                or details.st_mode & 0o022 or details.st_size > maximum):
            raise RuntimeError("unsafe_read_only_evidence_file")
        raw = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (details.st_ino, details.st_size, details.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != details.st_size:
            raise RuntimeError("evidence_changed_during_read")
        return raw


def decode_message(raw):
    decoder = zlib.decompressobj()
    message = decoder.decompress(raw, MAX_RECORD + 1)
    if len(message) > MAX_RECORD or not decoder.eof:
        raise RuntimeError("oversized_stored_evidence_record")
    return message.decode("utf-8")


def retained_window(connection, name, start, end):
    cursor = connection.execute(
        "SELECT id,source,service,at,message FROM logs WHERE at >= ? AND at <= ? "
        "AND (service=? OR source='journal') ORDER BY at,id LIMIT ?",
        (start, end, name, MAX_ROWS + 1),
    )
    retained, total, consumed, categories = [], 0, 0, {}
    for identity, source, service, at, blob in cursor:
        total += 1
        consumed += len(blob)
        if total > MAX_ROWS or consumed > MAX_WINDOW_BYTES:
            raise RuntimeError("bounded_evidence_window_incomplete")
        message = decode_message(blob)
        kind, safe = None, None
        if source == "docker-event":
            if re.fullmatch(r"exec_(?:create|start): /memory_check\.sh \d+", message):
                kind, safe = "memory_check_execution", message
            elif message in {"exec_die", "health_status: unhealthy", "health_status: healthy", "die", "start"}:
                kind, safe = "container_lifecycle", message
            else:
                kind = "other_container_event"
        elif source == "docker" and re.fullmatch(r"worker: Warm shutdown \(MainProcess\)", message):
            kind, safe = "celery_warm_shutdown", message
        elif source == "docker" and re.search(r"\b(?:SIGTERM|SIGINT|SIGKILL|CRITICAL|FATAL)\b|^Traceback|Cold shutdown", message):
            kind = "worker_signal_or_fatal_record"
        elif source == "journal":
            restart = re.search(r'msg="restarting container" container=([0-9a-f]{64}) '
                                r'exitCode=(\d+) exitedAt="([^"]+)" manualRestart=(true|false) '
                                r'restartCount=(\d+) restartPolicy="\{([^}]*)\}"', message)
            if restart:
                kind = "docker_automatic_restart"
                safe = {"container_id": restart[1], "exit_code": int(restart[2]),
                        "exited_at": restart[3], "manual_restart": restart[4] == "true",
                        "restart_count": int(restart[5]), "restart_policy": restart[6]}
            elif re.search(r"restarting container|task-delete|connecting to shim", message):
                kind = "docker_lifecycle_record"
            elif re.search(r"\b(?:oom|SIGTERM|SIGINT|SIGKILL|killed process|out of memory|reboot|shutdown|"
                           r"Starting Docker|Stopping Docker|Stopped Docker|daemon shutdown|API listen|"
                           r"unattended|logrotate|sudo|cron|compose)\b", message, re.I):
                kind = "host_operation_or_resource_record"
        if kind:
            categories[kind] = categories.get(kind, 0) + 1
            row = {"log_id": identity, "source": source, "service": service, "at": at,
                   "category": kind, "message_sha256": digest(message.encode())}
            if safe is not None:
                row["evidence"] = safe
            retained.append(row)
    gaps = [{"at": at, "source": source, "reason_sha256": digest(reason.encode()),
             "oversized_record": reason == "Oversized log record truncated"}
            for at, source, reason in connection.execute(
                "SELECT at,source,reason FROM gaps WHERE at >= ? AND at <= ? ORDER BY at LIMIT 101",
                (start, end))]
    if len(gaps) > 100:
        raise RuntimeError("too_many_evidence_gaps")
    return {"start": start, "end": end, "rows_scanned": total, "compressed_bytes_read": consumed,
            "categories": categories, "records": retained, "coverage_gaps": gaps}


def host_journal_window(start, end):
    """Include info-level host automation without exporting arbitrary commands."""
    start_arg = timestamp(start).strftime("%Y-%m-%d %H:%M:%S UTC")
    end_arg = timestamp(end).strftime("%Y-%m-%d %H:%M:%S UTC")
    raw = command(["journalctl", "--since", start_arg, "--until", end_arg,
                   "--no-pager", "--output=json"], 8 * 1024 * 1024)
    records, total, boots = [], 0, set()
    for line in raw.splitlines():
        total += 1
        if total > MAX_ROWS:
            raise RuntimeError("bounded_host_journal_incomplete")
        entry = json.loads(line)
        message = entry.get("MESSAGE")
        if not isinstance(message, str):
            raise RuntimeError("unreadable_host_journal_record")
        if isinstance(entry.get("_BOOT_ID"), str):
            boots.add(entry["_BOOT_ID"])
        unit = str(entry.get("_SYSTEMD_UNIT") or "")
        identifier = str(entry.get("SYSLOG_IDENTIFIER") or "")
        if (unit in {"docker.service", "containerd.service", "cron.service", "auditd.service"}
                or identifier in {"CRON", "sudo", "kernel", "audit"}
                or re.search(r"buh-|docker-[0-9a-f]{64}\.scope|Docker|Containerd|logrotate|unattended", message)):
            tags = []
            for tag, pattern in (
                    ("docker_or_compose_control", r"\b(?:docker(?:-compose|\s+compose)?\s+(?:restart|stop|kill|up|down|rm)|compose\s+(?:restart|up|down))\b"),
                    ("signal_or_kill", r"\bSIG(?:TERM|INT|KILL)\b|\bkill(?:all)?\s+-"),
                    ("daemon_or_host_shutdown", r"\b(?:reboot|shutting down|daemon shutdown|Stopping Docker|Starting Docker|Stopped Docker)\b"),
                    ("memory_or_oom", r"\b(?:oom|out of memory|killed process|memory pressure)\b"),
                    ("buh_operation", r"\bbuh-|\brecovery|\bdiagnostic|\bdeployment")):
                if re.search(pattern, message, re.I):
                    tags.append(tag)
            records.append({"at": datetime.fromtimestamp(int(entry["__REALTIME_TIMESTAMP"]) / 1e6,
                                                         timezone.utc).isoformat(),
                            "unit": unit[:100], "identifier": identifier[:100],
                            "pid": str(entry.get("_PID") or ""), "boot_id": entry.get("_BOOT_ID"),
                            "priority": entry.get("PRIORITY"), "tags": tags,
                            "message_sha256": digest(message.encode())})
    return {"rows_scanned": total, "boot_ids": sorted(boots), "records": records,
            "source_bytes": len(raw)}


def host_agent_actions(windows):
    root = Path("/opt/aa-docker/conf/buh-vps-health/run/actions")
    result = {"path": str(root), "exists": root.exists(), "matching_actions": []}
    if not root.exists():
        return result
    paths = list(root.glob("*.json"))
    if len(paths) > 2000:
        raise RuntimeError("bounded_host_agent_inventory_incomplete")
    for path in paths:
        raw = private_read(path, 65536)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError("unreadable_host_agent_action")
        at = next((value.get(key) for key in ("at", "started_at", "requested_at", "created_at")
                   if isinstance(value.get(key), str)), None)
        if at is None:
            continue
        point = timestamp(at)
        if any(timestamp(window["start"]) <= point <= timestamp(window["end"]) for window in windows):
            result["matching_actions"].append({"at": at, "sha256": digest(raw),
                                                "bytes": len(raw), "action_requires_review": True})
    return result


def collect(expected):
    report = {"schema_version": 1, "read_only": True, "scan_complete": False,
              "scope": "post-baseline-worker-restart-causality", "recovery_eligible": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "errors": [],
              "installation_attempted": False, "supported_recovery_attempted": False,
              "deployment_attempted": False, "memberaudit_mutation_attempted": False,
              "structures_mutation_attempted": False, "preserve_recovery_resources": True}
    lock, connection, phase = None, None, "retained_attempt_binding"
    try:
        if os.geteuid() != 0:
            raise RuntimeError("root_read_only_inspection_required")
        runtime = private_read(Path("/usr/local/lib/buh-platform-v2/ops/deploy/docker_host.py"))
        if digest(runtime) != expected["installed_docker_host_sha256"]:
            raise RuntimeError("installed_receiver_identity_changed")
        sys.path.insert(0, "/usr/local/lib/buh-platform-v2")
        from ops.deploy.contracts import ReceiverConfig
        from ops.deploy.docker_host import (
            DockerHost, NGINX_UPSTREAM_NAME, _parse_nginx_configuration,
            _verify_nginx_production_route, _walk_nginx_nodes,
        )
        from ops.deploy.receiver import _open_lock
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        lock = _open_lock(config)
        loaded = DockerHost.load_incomplete_plan(config)
        if loaded is None:
            raise RuntimeError("retained_attempt_missing")
        host, plan = loaded
        raw_plan = private_read(config.state_dir / "active-recovery.json")
        if (plan["attempt_id"] != expected["attempt_id"]
                or digest(raw_plan) != expected["hold_sha256"]
                or private_read(host.backup_path / "RECOVERY.json") != raw_plan):
            raise RuntimeError("retained_attempt_changed")
        report.update({"attempt_id": plan["attempt_id"], "hold_sha256": digest(raw_plan),
                       "hold_phase": plan["phase"], "flags": plan["flags"],
                       "installed_docker_host_sha256": digest(runtime)})
        phase = "current_runtime_identity"
        current = [inspect(row["container_id"]) for row in expected["containers"]]
        comparisons, changes = [], []
        for baseline, live in zip(expected["containers"], current):
            fixed = all(live[key] == baseline[key] for key in ("container_id", "image_id", "service"))
            health_ok = (live["status"] == "running" and live["oom_killed"] is False
                         and not live["state_error_present"]
                         and (live["health"] == "healthy" if baseline["health"] == "healthy"
                              else live["health"] in {None, "healthy"}))
            comparisons.append({"baseline": baseline, "current": live,
                                "identity_image_service_match": fixed, "runtime_healthy": health_ok,
                                "baseline_count_and_start_match": live["restart_count"] == baseline["restart_count"]
                                and live["started_at"] == baseline["started_at"]})
            if not fixed or not health_ok:
                raise RuntimeError("current_identity_or_health_changed")
            if live["restart_count"] != baseline["restart_count"]:
                changes.append({"baseline": baseline, "current": live})
        report["service_comparisons"] = comparisons
        report["additional_post_baseline_restarts"] = changes
        phase = "installed_memory_healthcheck_identity"
        script_path = Path(expected["memory_check_path"])
        script = private_read(script_path, 4096)
        report["memory_check_source"] = {"path": str(script_path), "bytes": len(script),
                                         "sha256": digest(script),
                                         "matches_reviewed_script": digest(script) == expected["memory_check_sha256"],
                                         "configured_sigterm_one": b"kill -SIGTERM 1" in script}
        if digest(script) != expected["memory_check_sha256"]:
            raise RuntimeError("memory_healthcheck_source_changed")
        phase = "retained_causal_windows"
        database_path = Path("/var/lib/buh-diagnostics/history.sqlite3")
        details = database_path.lstat()
        if (not stat.S_ISREG(details.st_mode) or database_path.is_symlink()
                or details.st_uid != 0 or stat.S_IMODE(details.st_mode) != 0o600):
            raise RuntimeError("unsafe_read_only_diagnostics_database")
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        store = connection.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()
        report["diagnostics_store_id"] = store[0] if store else None
        report["restart_windows"] = []
        for change in changes:
            live, baseline = change["current"], change["baseline"]
            if live["service"] != config.worker_service or not 0 < live["restart_count"] - baseline["restart_count"] <= 20:
                raise RuntimeError("unreviewed_nonworker_or_unbounded_restart_delta")
            point = timestamp(live["started_at"])
            start, end = point - timedelta(minutes=5), point + timedelta(minutes=2)
            window = retained_window(connection, live["name"].lstrip("/"),
                                     start.isoformat().replace("+00:00", "Z"),
                                     end.isoformat().replace("+00:00", "Z"))
            window.update({"container_id": live["container_id"], "image_id": live["image_id"],
                           "baseline_restart_count": baseline["restart_count"],
                           "current_restart_count": live["restart_count"], "started_at": live["started_at"],
                           "additional_restart_count": live["restart_count"] - baseline["restart_count"],
                           "all_additional_restarts_in_this_window": live["restart_count"] - baseline["restart_count"] == 1})
            window["host_resources"] = [{"at": at, "free_bytes": free, "available_memory_bytes": available,
                                         "load_1m": load} for at, free, available, load in connection.execute(
                "SELECT at,free_bytes,available_memory_bytes,load_1m FROM resources "
                "WHERE at >= ? AND at <= ? ORDER BY at LIMIT 10", (window["start"], window["end"]))]
            window["host_journal"] = host_journal_window(window["start"], window["end"])
            report["restart_windows"].append(window)
        report["host_boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        report["matches_reviewed_host_boot_id"] = report["host_boot_id"] == expected["host_boot_id"]
        report["uptime_seconds"] = float(Path("/proc/uptime").read_text().split()[0])
        properties = ("MainPID", "ExecMainStartTimestamp", "ExecMainStartTimestampMonotonic",
                      "ActiveEnterTimestamp", "ExecMainCode", "ExecMainStatus", "ActiveState", "SubState")
        report["host_daemons"] = {}
        for unit in ("docker.service", "containerd.service"):
            raw = command(["systemctl", "show", unit, "--property=" + ",".join(properties)], 8192)
            report["host_daemons"][unit] = {key: value for key, value in
                (line.split("=", 1) for line in raw.decode().splitlines() if "=" in line) if key in properties}
        report["host_agent_actions"] = host_agent_actions(report["restart_windows"])
        report["evidence_limits"] = [
            "The existing diagnostic store keeps Docker event actions without exec IDs or exec exit codes.",
            "Historic healthcheck stdout is available only if retained in recent Docker Health.Log entries.",
            "This collector does not infer a cause, accept a restart, or replace any reviewed baseline.",
        ]
        phase = "retained_previous_slot_route_evidence"
        slots = [inspect(name) for name in host.previous_web_slots]
        old_web_image = host.previous_images[config.gunicorn_service][0]
        if not slots or any(row["image_id"] != old_web_image or row["restart_count"] != 0
                            or row["status"] != "running" or row["oom_killed"]
                            or row["state_error_present"] or row["health"] not in {None, "healthy"}
                            for row in slots):
            raise RuntimeError("retained_previous_slot_identity_or_health_changed")
        upstream = private_read(host._upstream_path(), 65536)
        choices = [(tuple(host.previous_web_slots), (config.gunicorn_service,)),
                   ((config.gunicorn_service,), tuple(host.previous_web_slots)),
                   ((config.gunicorn_service,), ())]
        matching = [(targets, backups) for targets, backups in choices
                    if host._render_upstream(targets, backup_targets=backups).encode() == upstream]
        if len(matching) != 1:
            raise RuntimeError("current_route_is_not_a_supported_previous_version_route")
        host._verify_proxy_upstream_bytes(upstream.decode(), context="Read-only retained route evidence")
        configuration = host._proxy_exec("nginx", "-T", bounded_output=True,
                                         context="Read-only retained effective route evidence")
        _verify_nginx_production_route(configuration)
        nodes = [node for node in _walk_nginx_nodes(_parse_nginx_configuration(configuration))
                 if node[0] == ("upstream", NGINX_UPSTREAM_NAME)]
        if nodes != list(_parse_nginx_configuration(upstream.decode())):
            raise RuntimeError("effective_route_differs_from_managed_previous_version_route")
        targets, backups = matching[0]
        report["previous_slot_routing"] = {
            "schema_version": 1, "attempt_id": plan["attempt_id"], "plan_sha256": digest(raw_plan),
            "host_evidence_sha256": expected["host_evidence_sha256"],
            "slots": [{"name": name, **{key: row[key] for key in
                       ("container_id", "image_id", "restart_count", "started_at")}}
                      for name, row in zip(host.previous_web_slots, slots)],
            "targets": list(targets), "backup_targets": list(backups), "upstream_sha256": digest(upstream),
        }
        report["effective_previous_version_route_verified"] = True
        phase = "evidence_stability"
        fields = ("container_id", "image_id", "service", "restart_count", "started_at", "finished_at",
                  "status", "health", "oom_killed", "exit_code", "state_error_present",
                  "memory_healthcheck", "restart_policy", "memory_check_mounts")
        final_current = [inspect(live["container_id"]) for live in current]
        report["unchanged_during_collection"] = (private_read(config.state_dir / "active-recovery.json") == raw_plan
            and private_read(host.backup_path / "RECOVERY.json") == raw_plan
            and private_read(script_path, 4096) == script
            and private_read(host._upstream_path(), 65536) == upstream
            and all(all(after[key] == before[key] for key in fields)
                    for before, after in zip(slots, [inspect(name) for name in host.previous_web_slots]))
            and all(all(after[key] == before[key] for key in fields)
                    for before, after in zip(current, final_current)))
        if not report["unchanged_during_collection"]:
            raise RuntimeError("new_restart_or_identity_change_during_read")
        report["scan_complete"] = True
        report["requires_causal_review"] = True
    except Exception as error:
        report["failure_phase"] = phase
        report["errors"].append({"error_type": type(error).__name__, "error_sha256": digest(str(error).encode())})
        if type(error) is RuntimeError:
            report["guard_reason"] = str(error)[:160]
    finally:
        if connection is not None:
            connection.close()
        if lock is not None:
            os.close(lock)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


if __name__ == "__main__":
    expectation = json.loads(base64.b64decode(sys.argv[1], validate=True))
    result = collect(expectation)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["scan_complete"] else 1)
