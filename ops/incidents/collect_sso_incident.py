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
    "started_at", "finished_at", "updated_at", "result", "operation",
}


def read_file(path, maximum=1024 * 1024, *, allow_directory=False):
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
        directory = stat.S_ISDIR(details.st_mode)
        if (not (stat.S_ISREG(details.st_mode) or (allow_directory and directory))
                or details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022):
            raise ValueError("unsafe evidence file")
        metadata = {"exists": True, "size": details.st_size,
                    "mode": oct(stat.S_IMODE(details.st_mode)),
                    "uid": details.st_uid, "gid": details.st_gid,
                    "mtime": datetime.fromtimestamp(details.st_mtime, timezone.utc).isoformat()}
        metadata["kind"] = "directory" if directory else "file"
        if directory:
            metadata["hash_status"] = "not_applicable_directory"
            metadata["content_audit"] = "not_recursively_verified"
            return metadata, None
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


def backup_inventory(directory, expected):
    """Inventory every entry; preserve and report the normal static directory."""
    details = directory.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o077):
        raise ValueError("unsafe backup directory")
    paths = sorted(directory.iterdir())
    if len(paths) > 64:
        raise ValueError("backup inventory exceeds bound")
    entries, errors = [], []
    for path in paths:
        try:
            metadata, _ = read_file(
                path, maximum=1024 * 1024,
                allow_directory=path.name == "previous-static-root",
            )
            metadata["name"] = path.name
            if path.name in expected:
                metadata["expected_sha256"] = expected[path.name]
                metadata["matches_expected_sha256"] = (
                    metadata.get("sha256") == expected[path.name]
                    if "sha256" in metadata else None
                )
            entries.append(metadata)
        except Exception as exc:
            errors.append({"section": "backup_inventory", "entry": path.name,
                           "error_type": type(exc).__name__})
            entries.append({"name": path.name, "error_type": type(exc).__name__,
                            "payload_read": False})
    return entries, errors


def database_program(code):
    """Use a named source frame so a safe failure site survives Django shell."""
    return "exec(compile(" + repr(code) + ", '<buh-database-report>', 'exec'));emit()"


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



def public_identity_inventory(database, owner_errors, *, opener=None):
    """Bounded public ESI GETs, without stored credentials or database writes."""
    import urllib.error
    import urllib.request
    if opener is None:
        opener = urllib.request.urlopen
    selected = [
        owner for owner in database["structures"]
        if str(owner["owner_pk"]) in owner_errors
        or (owner["enabled"] and (
            not owner["configured_characters"]
            or any(not item["enabled"] for item in owner["configured_characters"])
        ))
    ]
    wanted = []
    for owner in selected:
        wanted.append(("corporations", owner["corporation_id"]))
        for item in owner["configured_characters"]:
            identity = item.get("identity")
            if identity:
                wanted.append(("characters", identity["character_id"]))
        for identity in owner["other_linked_characters_in_stored_corporation"]:
            if identity["user_has_structures_owner_permission"] and any(
                not token["missing_scopes"] and token["has_refresh_credential"]
                and token["user_id"] == identity["user_id"] for token in identity["tokens"]
            ):
                wanted.append(("characters", identity["character_id"]))
    for character in database["memberaudit"]:
        if character["character_name"].casefold() == "fifty5d":
            wanted.append(("characters", character["character_id"]))
    wanted = list(dict.fromkeys(wanted))
    result = {"read_only_public_requests": True, "request_bound": 64,
              "selected_owners": [owner["owner_pk"] for owner in selected],
              "records": [], "coverage_complete": len(wanted) <= 64}
    transient_streak = 0
    stop_reason = None
    for kind, identity in wanted[:64]:
        row = {"kind": kind, "id": identity}
        if stop_reason:
            row["result"] = stop_reason
            result["coverage_complete"] = False
            result["records"].append(row)
            continue
        if type(identity) is not int or identity <= 0:
            raise ValueError("invalid public identity")
        request = urllib.request.Request(
            f"https://esi.evetech.net/latest/{kind}/{identity}/?datasource=tranquility",
            headers={"User-Agent": "B-UH-read-only-incident-diagnostics"},
        )
        try:
            with opener(request, timeout=8) as response:
                row["http_status"] = response.status
                data = response.read(16 * 1024 + 1)
                if len(data) > 16 * 1024:
                    raise ValueError("public identity response exceeds bound")
                payload = json.loads(data)
                allowed = {"corporation_id", "alliance_id", "name"} if kind == "characters" else {
                    "ceo_id", "creator_id", "alliance_id", "name", "member_count",
                }
                row["identity"] = projection(payload, allowed)
                row["result"] = "success"
                remaining = response.headers.get("X-Esi-Error-Limit-Remain", "")
                if remaining.isdigit() and int(remaining) <= 20:
                    stop_reason = "skipped_provider_error_budget_low"
                transient_streak = 0
        except urllib.error.HTTPError as exc:
            row["http_status"] = exc.code
            row["result"] = "public_http_rejection"
            if exc.code in {420, 429}:
                stop_reason = "skipped_provider_throttling"
            if exc.code >= 500:
                transient_streak += 1
            else:
                transient_streak = 0
        except Exception as exc:
            row["result"] = "public_transport_or_response_failure"
            row["error_type"] = type(exc).__name__
            transient_streak += 1
        row["checked_at"] = datetime.now(timezone.utc).isoformat()
        result["records"].append(row)
        if transient_streak >= 3:
            stop_reason = "skipped_provider_unavailable"
    result["stop_reason"] = stop_reason
    return result

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
        elif name in {"deployment_current", "platform_current", "journal", "receiver_install", "backup_manifest"}:
            value = section(name + "_json", lambda data=data: json.loads(data))
            if value is not None:
                report[name] = projection(value, MARKER_FIELDS)
                if name == "backup_manifest":
                    report[name] = projection(value, {"schema_version", "filename", "sha256",
                                                     "size", "database_image_id",
                                                     "evidence_tables", "evidence_rows"})
                if name == "journal":
                    report[name]["transition_states"] = [
                        projection(item, {"state", "at", "timestamp"})
                        for item in value.get("history", [])[:64]
                    ]
                    for field in ("rollback", "cleanup", "verification"):
                        item = value.get(field)
                        report[name][field] = (projection(item, {"result", "filename", "sha256", "iterations"})
                                               if isinstance(item, dict) else None)
                    report[name]["structures_selection_failure_reported"] = (
                        "No valid character found for sync" in str(value.get("failure_detail", "")))
        elif name == "upstream":
            report["upstream_targets"] = re.findall(
                rb"\bserver[ \t]+([A-Za-z0-9_.-]+:[0-9]{1,5})[ \t;]",
                data,
            )
            report["upstream_targets"] = [item.decode("ascii") for item in report["upstream_targets"]]

    def backups():
        directory = config.backup_dir / ATTEMPT
        expected = (report.get("recovery_hold") or {}).get("backup_files", {})
        entries, errors = backup_inventory(directory, expected)
        report["backup_inventory"] = entries
        if errors:
            report["scan_complete"] = False
            report["errors"].extend(errors)
    section("backup_inventory", backups)
    report["configuration_matches_retained_backup"] = {}
    for current, previous in (("local_settings", "local.py"), ("dockerfile", "custom.dockerfile"),
                              ("upstream", "nginx-upstream.conf"),
                              ("platform_current", "platform-CURRENT.json"),
                              ("deployment_current", "deployment-current.json")):
        metadata = report["files"].get(current, {})
        saved = next((item for item in report["backup_inventory"] if item["name"] == previous), {})
        report["configuration_matches_retained_backup"][current] = (
            metadata["sha256"] == saved["sha256"]
            if "sha256" in metadata and "sha256" in saved else None)
    report["recovery_completion_is_proven"] = False

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
        program = database_program(code)
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
        for role in config.auth_services:
            if role == config.gunicorn_service:
                continue
            for container in live.get(role, []):
                output = host._run(
                    ["docker", "logs", "--timestamps", "--since", since, container],
                    timeout=120, bounded_output=True, context="Read-only owner-specific current log evidence",
                )
                report["owner_errors_by_container"][container] = log_owner_evidence(output, owners)
    if report["database"] and report["database"].get("scan_complete"):
        section("current_owner_logs", logs)
        owners_with_errors = {
            owner_id for values in report.get("owner_errors_by_container", {}).values()
            for owner_id in values
        }
        report["current_public_identities"] = section(
            "public_identity_esi",
            lambda: public_identity_inventory(report["database"], owners_with_errors),
        )
    def prior_database_failures():
        # Only the known earlier read-only collector's private, retained reports.
        paths = sorted(Path("/root").glob(
            "buh-sso-incident-e635c833-*/incident-report.json"))
        if len(paths) > 8:
            raise ValueError("prior report inventory exceeds bound")
        values = []
        for path in paths:
            metadata, data = read_file(path)
            if data is None:
                values.append({"report_sha256": metadata.get("sha256"),
                               "read_result": "not_read_over_bound"})
                continue
            previous = json.loads(data)
            if (previous.get("read_only") is not True or
                    previous.get("diagnostic_source_commit") !=
                    "e635c83359492c06309ad588e55fb1a630f261da"):
                raise ValueError("unexpected prior diagnostic source")
            error_type = (previous.get("database") or {}).get("error_type")
            if error_type is not None and not re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
                raise ValueError("unexpected diagnostic category")
            values.append({"report_sha256": metadata.get("sha256"),
                           "database_scan_complete":
                               (previous.get("database") or {}).get("scan_complete"),
                           "database_error_type": error_type})
        return values
    report["prior_database_failures"] = section(
        "prior_database_failures", prior_database_failures)

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
