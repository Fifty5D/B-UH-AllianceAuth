"""Owner-operated, single-character Structures pilot during a retained attempt.

Uses installed receiver read APIs only. Does not recover/clean up the deployment,
switch traffic, install source, replace workers, change MA, or delete credentials.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

QUALIFIED_REPORT_COMMIT = "9d5c92c9e068da8b2eb6c10be9c7716225d55e82"
MAX_REPORT_BYTES = 8 * 1024 * 1024
OLD_RELEASE = "ff36151aa2065b0914a6cc2858a2284523fb098f"
OLD_SOURCE = "a8f60c83ddecf883cdefdd21f90dc426726bf5d3"
OLD_MANIFEST = "ee1120131935b78ba583072ad6c3b35ffb5585c98472f524214281174856f860"


class PilotStop(ValueError):
    pass


def private_bytes(path, maximum=MAX_REPORT_BYTES):
    """No-follow bounded root-owned files, including every ancestor."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise PilotStop("unsafe path")
    current = Path("/")
    for part in path.parent.parts[1:]:
        current /= part
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise PilotStop("unsafe ancestor")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > maximum):
            raise PilotStop("unsafe file")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if (len(data) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns):
            raise PilotStop("file changed")
    return data


def latest_report():
    paths = list(Path("/root").glob("buh-sso-incident-9d5c92c9-*/incident-report.json"))
    if not paths or len(paths) > 8:
        raise PilotStop("qualified report not uniquely bounded")
    values = []
    for path in paths:
        data = private_bytes(path)
        value = json.loads(data)
        if (value.get("diagnostic_source_commit") != QUALIFIED_REPORT_COMMIT
                or value.get("read_only") is not True):
            raise PilotStop("unexpected prior report")
        if value.get("scan_complete") and (value.get("database") or {}).get("scan_complete"):
            values.append((datetime.fromisoformat(value["finished_at"]), path, value, data))
    if not values:
        raise PilotStop("no complete qualified report")
    _, path, value, data = max(values, key=lambda item: item[0])
    return path, value, hashlib.sha256(data).hexdigest()


def select_pilot(report, name):
    targets = []
    for owner in report["database"]["structures"]:
        if not owner["enabled"]:
            continue
        for character in owner["configured_characters"]:
            person = character.get("identity") or {}
            if person.get("character_name") != name:
                continue
            if (not character["auth_link_exists"]
                    or (not character["enabled"] and not character["disabled_for_no_valid_token"])
                    or not character["stored_corporation_matches_owner"]
                    or not person["user_active"]
                    or not person["user_has_structures_owner_permission"]):
                raise PilotStop("pilot has a different incident state")
            tokens = [
                token for token in person["tokens"]
                if token["user_id"] == person["user_id"]
                and token["character_id"] == person["character_id"]
                and token["has_refresh_credential"] and not token["missing_scopes"]
                and token["owner_identity_matches_auth_link"] is True
            ]
            if len(tokens) != 1:
                raise PilotStop("pilot needs one unambiguous existing token")
            targets.append({
                "owner_pk": owner["owner_pk"],
                "owner_character_pk": character["owner_character_id"],
                "auth_link_pk": person["ownership_id"],
                "character_id": person["character_id"], "token_pk": tokens[0]["id"],
            })
    if len(targets) != 1:
        raise PilotStop("pilot selection is not exactly one configured owner")
    return targets[0]


def parse_result(output):
    begin, end = "BUH_STRUCTURES_RECOVERY_BEGIN\n", "\nBUH_STRUCTURES_RECOVERY_END"
    if output.count(begin) != 1 or output.count(end) != 1:
        raise PilotStop("missing or ambiguous recovery evidence")
    result = json.loads(output.split(begin, 1)[1].split(end, 1)[0])
    if (not isinstance(result, dict) or result.get("schema_version") != 1
            or type(result.get("recovered")) is not bool):
        raise PilotStop("invalid recovery evidence")
    return result


def assert_previous_image(container, expected):
    labels = container["Config"].get("Labels") or {}
    if (not container["State"]["Running"]
            or container["Image"] != expected
            or labels.get("com.b-uh.platform.version") != "0.8.2"
            or labels.get("com.b-uh.platform.release") != OLD_RELEASE
            or labels.get("com.b-uh.platform.source") != OLD_SOURCE
            or labels.get("com.b-uh.platform.manifest") != OLD_MANIFEST):
        raise PilotStop("runtime is not the verified previous image")


def verify_host(host, config, attempt):
    hold_data = private_bytes(config.state_dir / "active-recovery.json", 64 * 1024)
    hold = json.loads(hold_data)
    journal = json.loads(private_bytes(config.state_dir / "attempts" / (attempt + ".json"), 256 * 1024))
    if (hold.get("attempt_id") != attempt or hold.get("status") != "active"
            or hold.get("phase") != "candidate-slot-start-1"
            or journal.get("attempt_id") != attempt or journal.get("state") != "migrated"
            or journal.get("result") != "failed"):
        raise PilotStop("retained attempt changed; no independent attempt allowed")
    current = json.loads(private_bytes(config.app_dir / "conf/buh-platform-v2/CURRENT.json", 16 * 1024))
    if current.get("platform_version") != "0.8.2" or current.get("release_commit") != OLD_RELEASE:
        raise PilotStop("production baseline changed")
    for path, backup_name in (
        (config.app_dir / config.local_settings, "local.py"),
        (config.app_dir / config.custom_dockerfile, "custom.dockerfile"),
    ):
        data = private_bytes(path, 1024 * 1024)
        saved = private_bytes(config.backup_dir / attempt / backup_name, 1024 * 1024)
        if hashlib.sha256(data).digest() != hashlib.sha256(saved).digest():
            raise PilotStop("configuration differs from retained previous baseline")
    ids = host._run(["docker", "ps", "-aq"], bounded_output=True,
                    context="Bounded pilot container metadata").split()
    if not ids or len(ids) > 128 or any(not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in ids):
        raise PilotStop("container bound")
    containers = json.loads(host._run(["docker", "inspect", *ids], bounded_output=True,
                                      context="Bounded pilot runtime identities"))
    by_id = {item["Id"]: item for item in containers}
    expected_image = hold["previous_images"][config.gunicorn_service][0]
    live = list(host._running_service_containers(config.gunicorn_service,
                                                 context="Existing pilot Django runtime"))
    if len(live) != 1:
        raise PilotStop("pilot needs one current Django runtime")
    selected = next((item for key, item in by_id.items() if key.startswith(live[0])), None)
    if not selected:
        raise PilotStop("live container identity missing")
    assert_previous_image(selected, expected_image)
    services = {}
    for role in config.auth_services:
        role_ids = list(host._running_service_containers(
            role, context="Retained previous runtime identity"))
        if len(role_ids) != hold["auth_replica_counts"][role]:
            raise PilotStop("live service count differs from retained previous state")
        services[role] = []
        for short_id in role_ids:
            item = next((value for key, value in by_id.items() if key.startswith(short_id)), None)
            if item is None:
                raise PilotStop("live service identity missing")
            assert_previous_image(item, hold["previous_images"][role][0])
            services[role].append({"container_id": item["Id"], "image_id": item["Image"]})
    route = private_bytes(config.app_dir / config.nginx_upstream_file, 64 * 1024)
    proxy_route = host._proxy_exec("cat", config.nginx_upstream_container_file,
                                  bounded_output=True, context="Current pilot traffic identity")
    if hashlib.sha256(route).digest() != hashlib.sha256(proxy_route.encode()).digest():
        raise PilotStop("active upstream differs from host")
    targets = re.findall(rb"\bserver[ \t]+([A-Za-z0-9_.-]+):([0-9]{1,5})[ \t;]", route)
    if not targets:
        raise PilotStop("no bounded upstream identity")
    route_ids = []
    for hostname, port in targets:
        if int(port) != config.gunicorn_port:
            raise PilotStop("unexpected upstream port")
        matches = []
        for container in containers:
            if not container["State"]["Running"]:
                continue
            names = {container["Name"].lstrip("/"), container["Id"], container["Id"][:12]}
            for network in container["NetworkSettings"]["Networks"].values():
                names.update(network.get("Aliases") or [])
                names.add(network.get("IPAddress"))
            if hostname.decode("ascii") in names:
                matches.append(container)
        if not matches:
            raise PilotStop("upstream target not bound to a runtime")
        for container in matches:
            assert_previous_image(container, expected_image)
            route_ids.append(container["Id"])
    usage = os.statvfs(config.app_dir)
    free_bytes, total_bytes = usage.f_bavail * usage.f_frsize, usage.f_blocks * usage.f_frsize
    if free_bytes < max(10 * 1024 ** 3, total_bytes // 10):
        raise PilotStop("disk safety threshold")
    host._public_smoke_checks()
    return {"hold_sha256": hashlib.sha256(hold_data).hexdigest(),
            "attempt_id": attempt, "hold_phase": hold["phase"], "journal_state": journal["state"],
            "runtime_container_id": selected["Id"], "runtime_image_id": selected["Image"],
            "platform_version": "0.8.2", "live_auth_services": services,
            "host_load_average": list(os.getloadavg()), "active_upstream_container_ids": sorted(set(route_ids)),
            "active_upstream_sha256": hashlib.sha256(route).hexdigest(),
            "traffic_only_verified_previous_image": True, "public_smoke_passed": True,
            "disk": {"free_bytes": free_bytes, "total_bytes": total_bytes}}


def collect(attempt, name, apply):
    if os.geteuid() != 0 or not re.fullmatch(r"gh-[0-9]+-[0-9]+", attempt):
        raise PilotStop("invalid incident invocation")
    if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
        raise PilotStop("invalid pilot name")
    sys.path.insert(0, "/usr/local/lib/buh-platform-v2")
    from ops.deploy.contracts import ReceiverConfig
    from ops.deploy.docker_host import DockerHost
    config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
    host = DockerHost(config)
    output = {"schema_version": 1, "read_only": not apply, "single_character_pilot": True,
              "attempt_id": attempt, "deployment_attempted": False,
              "retained_recovery_changed": False, "preserve_recovery_resources": True,
              "started_at": datetime.now(timezone.utc).isoformat(), "scan_complete": False}
    descriptor = os.open(config.state_dir / "deploy.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path, report, digest = latest_report()
        if report["attempt_id"] != attempt:
            raise PilotStop("qualified report belongs to another attempt")
        target = select_pilot(report, name)
        output["baseline_report_path"], output["baseline_report_sha256"] = str(path), digest
        output["before_host"] = verify_host(host, config, attempt)
        output["target"] = target
        source = Path(__file__).with_name("structures_recovery.py").read_text(encoding="utf-8")
        code = "exec(compile(" + repr(source) + ", '<buh-structures-recovery>', 'exec'));emit(" + repr(target) + ",apply=" + repr(apply) + ")"
        returned = host._manage_live("shell", "--no-imports", "-c", code,
                                    context="Bounded existing-token Structures pilot")
        output["result"] = parse_result(returned)
        output["after_host"] = verify_host(host, config, attempt)
        if (output["before_host"]["hold_sha256"] != output["after_host"]["hold_sha256"]
                or output["before_host"]["active_upstream_sha256"] != output["after_host"]["active_upstream_sha256"]
                or output["before_host"]["runtime_container_id"] != output["after_host"]["runtime_container_id"]):
            raise PilotStop("host changed during pilot")
        output["observed_disk_growth_bytes"] = (
            output["before_host"]["disk"]["free_bytes"] - output["after_host"]["disk"]["free_bytes"])
        output["scan_complete"] = True
    except Exception as error:
        output["error_type"] = type(error).__name__
        if isinstance(error, PilotStop):
            output["guard_reason"] = str(error)
        output["failure_sites"] = []
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                output["failure_sites"].append({"function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
    finally:
        os.close(descriptor)
        output["finished_at"] = datetime.now(timezone.utc).isoformat()
    return output


def main():
    attempt, name, mode = sys.argv[1:]
    if mode not in {"apply", "report"}:
        raise PilotStop("invalid mode")
    output = collect(attempt, name, mode == "apply")
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output.get("scan_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
