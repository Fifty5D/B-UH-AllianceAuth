"""Collect token-selection and retained-attempt evidence without installing code."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ATTEMPT = "gh-36955595351-1"
INSTALLED = Path("/usr/local/lib/buh-platform-v2")
PROVENANCE = {
    "com.b-uh.platform.version", "com.b-uh.platform.source",
    "com.b-uh.platform.release", "com.b-uh.platform.manifest",
    "com.b-uh.platform.base-digest", "com.docker.compose.project",
    "com.docker.compose.service", "com.docker.compose.container-number",
}
PLAN_FIELDS = {
    "schema_version", "status", "attempt_id", "phase", "backup_path",
    "previous_images", "previous_image_pins", "auth_replica_counts",
    "previous_web_slots", "candidate_web_slots", "backup_files", "flags",
    "platform_current_existed", "deployment_current_existed",
    "static_manifest_sha256", "static_assets_sha256", "static_assets_count",
    "static_assets_bytes", "static_assets_backup_sha256",
}
MARKER_FIELDS = {
    "schema_version", "platform_version", "release_commit", "source_commit",
    "manifest_sha256", "verified_at", "attempt_id", "status", "state",
    "started_at", "finished_at",
}


def read_file(path, maximum=1024 * 1024):
    """No-follow bounded metadata/hash; never export file bytes by default."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe path")
    current = Path("/")
    for part in path.parent.parts[1:]:
        current /= part
        details = current.lstat()
        if (not stat.S_ISDIR(details.st_mode) or details.st_uid != 0
                or stat.S_IMODE(details.st_mode) & 0o022):
            raise ValueError("unsafe evidence ancestor")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"exists": False}, None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
            raise ValueError("unsafe evidence file")
        metadata = {"exists": True, "size": details.st_size,
                    "mode": oct(stat.S_IMODE(details.st_mode)),
                    "uid": details.st_uid, "gid": details.st_gid,
                    "mtime": datetime.fromtimestamp(details.st_mtime, timezone.utc).isoformat()}
        if details.st_size > maximum:
            metadata["hash_status"] = "not_hashed_over_bound"
            return metadata, None
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        if (len(data) != details.st_size or after.st_size != details.st_size
                or after.st_mtime_ns != details.st_mtime_ns):
            raise ValueError("evidence changed during read")
        metadata["sha256"] = hashlib.sha256(data).hexdigest()
        return metadata, data
    finally:
        os.close(descriptor)


def projection(value, fields):
    return {key: value[key] for key in fields if key in value}


def parse_database_output(output):
    begin, end = "BUH_INCIDENT_REPORT_BEGIN\n", "\nBUH_INCIDENT_REPORT_END"
    if output.count(begin) != 1 or output.count(end) != 1:
        raise ValueError("missing or ambiguous database report")
    value = json.loads(output.split(begin, 1)[1].split(end, 1)[0])
    if (not isinstance(value, dict) or value.get("read_only") is not True
            or type(value.get("scan_complete")) is not bool):
        raise ValueError("invalid database report")
    return value


def container_projection(value):
    return {
        "id": value["Id"], "name": value["Name"].lstrip("/"), "image_id": value["Image"],
        "image_reference": value["Config"]["Image"],
        "labels": projection(value["Config"].get("Labels") or {}, PROVENANCE),
        "state": projection(value["State"], {"Status", "Running", "StartedAt", "FinishedAt"}),
        "restart_count": value["RestartCount"],
        "networks": sorted(value.get("NetworkSettings", {}).get("Networks", {})),
        "mounts": [projection(mount, {"Type", "Source", "Destination", "RW"})
                   for mount in value.get("Mounts", [])],
    }


def log_owner_evidence(output, owners):
    """Only an exact owner-bound error becomes evidence; raw logs are discarded."""
    found = {}
    for line in output.splitlines():
        match = re.match(r"^(\d{4}-\d\d-\d\dT[0-9:.]+Z)\s", line)
        if not match:
            continue
        for owner in owners:
            shape = owner["corporation_name"] + ": No valid character found for sync. Service down for this owner."
            pattern = (r"(?:^|\b(?:ERROR|WARNING|CRITICAL)\s+|[\"']|:\s+|\]\s+)"
                       + re.escape(shape))
            if re.search(pattern, line[match.end():]):
                found.setdefault(str(owner["owner_pk"]), []).append(match[1])
    return {key: {"count": len(values), "first": min(values), "last": max(values),
                  "shape": "OWNER: No valid character found for sync. Service down for this owner."}
            for key, values in found.items()}


def collect():
    if os.geteuid() != 0:
        raise RuntimeError("root required for read-only host evidence")
    # Use the receiver already installed on the host, never the staged candidate.
    sys.path.insert(0, str(INSTALLED))
    from ops.deploy.contracts import ReceiverConfig
    from ops.deploy.docker_host import DockerHost

    config_path = Path("/etc/buh-platform-v2/receiver.json")
    config_metadata, _ = read_file(config_path)
    config = ReceiverConfig.load(config_path)
    host = DockerHost(config)
    report = {
        "schema_version": 1, "read_only": True, "scan_complete": True,
        "attempt_id": ATTEMPT, "started_at": datetime.now(timezone.utc).isoformat(),
        "preserve_recovery_resources": True, "errors": [],
        "receiver_config": config_metadata, "files": {}, "containers": [],
        "backup_inventory": [],
    }

    def section(name, function):
        try:
            return function()
        except Exception as exc:
            report["scan_complete"] = False
            report["errors"].append({"section": name, "error_type": type(exc).__name__})
            return None

    paths = {
        "receiver_install": Path("/etc/buh-platform-v2/INSTALL.json"),
        "recovery_hold": config.state_dir / "active-recovery.json",
        "deployment_current": config.state_dir / "current.json",
        "platform_current": config.app_dir / "conf/buh-platform-v2/CURRENT.json",
        "journal": config.state_dir / "attempts" / (ATTEMPT + ".json"),
        "backup_recovery": config.backup_dir / ATTEMPT / "RECOVERY.json",
        "backup_manifest": config.backup_dir / ATTEMPT / "BACKUP.json",
        "upstream": config.app_dir / config.nginx_upstream_file,
        "installed_engine": INSTALLED / "ops/deploy/engine.py",
        "installed_host": INSTALLED / "ops/deploy/docker_host.py",
        "installed_receiver": INSTALLED / "ops/deploy/receiver.py",
        "local_settings": config.app_dir / config.local_settings,
        "dockerfile": config.app_dir / config.custom_dockerfile,
        "deployment_lock": config.state_dir / "deploy.lock",
    }
    for name, path in paths.items():
        item = section(name, lambda path=path: read_file(path))
        if item is None:
            continue
        metadata, data = item
        report["files"][name] = metadata
        if data is None:
            continue
        if name in {"recovery_hold", "backup_recovery"}:
            value = section(name + "_json", lambda data=data: json.loads(data))
            if value is not None:
                report[name] = projection(value, PLAN_FIELDS)
                if value.get("attempt_id") != ATTEMPT:
                    report["scan_complete"] = False
                    report["errors"].append({"section": name, "error_type": "UnexpectedAttempt"})
        elif name in {"deployment_current", "platform_current", "journal", "receiver_install"}:
            value = section(name + "_json", lambda data=data: json.loads(data))
            if value is not None:
                report[name] = projection(value, MARKER_FIELDS)
                if name == "journal":
                    report[name]["transition_states"] = [
                        projection(item, {"state", "at", "timestamp"})
                        for item in value.get("transitions", [])[:64]
                    ]
        elif name == "upstream":
            report["upstream_targets"] = re.findall(
                rb"\bserver[ \t]+([A-Za-z0-9_.-]+:[0-9]{1,5})[ \t;]",
                data,
            )
            report["upstream_targets"] = [item.decode("ascii") for item in report["upstream_targets"]]

    def backups():
        directory = config.backup_dir / ATTEMPT
        details = directory.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("unsafe backup directory")
        paths = sorted(directory.iterdir())
        if len(paths) > 64:
            raise ValueError("backup inventory exceeds bound")
        expected = (report.get("recovery_hold") or {}).get("backup_files", {})
        for path in paths:
            metadata, _ = read_file(path, maximum=1024 * 1024)
            metadata["name"] = path.name
            if path.name in expected:
                metadata["expected_sha256"] = expected[path.name]
                metadata["matches_expected_sha256"] = (
                    metadata.get("sha256") == expected[path.name]
                    if "sha256" in metadata else None
                )
            report["backup_inventory"].append(metadata)
    section("backup_inventory", backups)

    def containers():
        ids = host._run(["docker", "ps", "-aq"], bounded_output=True,
                        context="Read-only incident container inventory").split()
        if not ids or len(ids) > 128 or any(not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in ids):
            raise ValueError("container inventory exceeds bound")
        raw = host._run(["docker", "inspect", *ids], bounded_output=True,
                        context="Read-only incident container metadata")
        report["containers"] = [container_projection(item) for item in json.loads(raw)]
        role_ids = {
            role: list(host._running_service_containers(role, context="Read-only incident live service"))
            for role in config.auth_services
        }
        report["live_service_container_ids"] = role_ids
        proxy = host._proxy_exec("cat", config.nginx_upstream_container_file,
                                bounded_output=True, context="Read-only active nginx upstream")
        report["active_upstream_sha256"] = hashlib.sha256(proxy.encode()).hexdigest()
        report["active_upstream_matches_host"] = (
            report["active_upstream_sha256"] == report["files"]["upstream"].get("sha256")
        )
    section("containers_and_traffic", containers)

    def database():
        code = (Path(__file__).with_name("database_report.py")).read_text(encoding="utf-8")
        program = "exec(" + repr(code) + ");emit()"
        output = host._manage_live("shell", "-c", program, context="Read-only token incident database evidence")
        value = parse_database_output(output)
        if not value["scan_complete"]:
            report["scan_complete"] = False
        return value
    report["database"] = section("database", database)

    def logs():
        since = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        report["logs_since"] = since
        report["owner_errors_by_container"] = {}
        owners = report["database"]["structures"]
        live = report["live_service_container_ids"]
        for role in (config.worker_service, config.beat_service):
            for container in live.get(role, []):
                output = host._run(
                    ["docker", "logs", "--timestamps", "--since", since, container],
                    timeout=120, bounded_output=True, context="Read-only owner-specific current log evidence",
                )
                report["owner_errors_by_container"][container] = log_owner_evidence(output, owners)
    if report["database"] and report["database"].get("scan_complete"):
        section("current_owner_logs", logs)
    def lock_state():
        import fcntl
        descriptor = os.open(config.state_dir / "deploy.lock", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"busy": True, "checked_at": datetime.now(timezone.utc).isoformat()}
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return {"busy": False, "checked_at": datetime.now(timezone.utc).isoformat()}
        finally:
            os.close(descriptor)
    report["deployment_lock_state"] = section("deployment_lock", lock_state)
    usage = os.statvfs(config.app_dir)
    report["disk"] = {"free_bytes": usage.f_bavail * usage.f_frsize,
                      "total_bytes": usage.f_blocks * usage.f_frsize}
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    try:
        value = collect()
    except Exception as exc:
        value = {"read_only": True, "scan_complete": False,
                 "preserve_recovery_resources": True, "error_type": type(exc).__name__}
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0 if value.get("scan_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
