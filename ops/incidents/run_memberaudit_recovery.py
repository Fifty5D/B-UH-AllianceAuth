"""Owner host orchestration: v0.8.2 MA recovery only; retained deployment stays held."""
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

if __package__:
    from .memberaudit_selection import SelectionStop, pilot_order, select_outage, summarize
else:
    from memberaudit_selection import SelectionStop, pilot_order, select_outage, summarize

BASELINE_SHA = "58812a8133ba794dbd45d3ac9431dddaca96322e9cace36eaad1287b1d2d22b4"
HOLD_SHA = "ec04921a8707437ef8c0a05460581ea05c72de21d19ab5d602b6478aaf5597dd"
PILOT_HOST_SHA = "47eef33d8c3f5bb1714d87d3d3267c5305dedd4a784fd057342d10862865a836"


class RecoveryGate(ValueError):
    pass


def qualified_host(path):
    path = Path(path)
    # The existing read-only host checks are reused from the successful Structures stage.
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != PILOT_HOST_SHA:
        raise RecoveryGate("readonly_host_helper_hash_mismatch")
    spec = importlib.util.spec_from_file_location("buh_incident_host_readonly", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.private_bytes(path) != data:
        raise RecoveryGate("readonly_host_helper_changed")
    return module


def checkpoint(path, report):
    """Atomic private progress evidence survives operator disconnects."""
    data = json.dumps(report, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(".next")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def retained_fingerprint(pilot, host, config, attempt):
    """Read safety evidence only. No recovery API, pruning, image/container commands."""
    paths = [
        config.state_dir / "active-recovery.json", config.state_dir / "attempts" / (attempt + ".json"),
        config.state_dir / "current.json", config.app_dir / "conf/buh-platform-v2/CURRENT.json",
    ]
    result = {"state_sha256": {str(p): hashlib.sha256(pilot.private_bytes(p, 256 * 1024)).hexdigest()
                              for p in paths}, "backup_files": []}
    root = config.backup_dir / attempt
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if path.is_file() and not path.is_symlink():
            result["backup_files"].append({
                "relative_path": str(path.relative_to(root)), "bytes": info.st_size,
                "mtime_ns": info.st_mtime_ns, "inode": info.st_ino,
            })
        elif not path.is_dir() or path.is_symlink():
            raise RecoveryGate("unexpected_retained_backup_object")
        if len(result["backup_files"]) > 500:
            raise RecoveryGate("retained_backup_inventory_exceeds_bound")
    ids = host._run(["docker", "ps", "-aq"], bounded_output=True, context="Read retained incident resource identities").split()
    if not ids or len(ids) > 128 or any(not re.fullmatch(r"[0-9a-f]{12,64}", i) for i in ids):
        raise RecoveryGate("retained_container_inventory_bound")
    containers = json.loads(host._run(["docker", "inspect", *ids], bounded_output=True,
                                      context="Read retained incident resources"))
    # Exact existing container inventory is preserved; uptime/task counters are not identities.
    result["containers"] = sorted(
        [{"id": c["Id"], "image": c["Image"], "name": c["Name"],
          "running": c["State"]["Running"]} for c in containers],
        key=lambda c: c["id"],
    )
    return result


def parse_result(output):
    begin, end = "BUH_MEMBERAUDIT_BEGIN\n", "\nBUH_MEMBERAUDIT_END"
    if output.count(begin) != 1 or output.count(end) != 1:
        raise RecoveryGate("missing_or_ambiguous_memberaudit_result")
    value = json.loads(output.split(begin, 1)[1].split(end, 1)[0])
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or type(value.get("recovered")) is not bool):
        raise RecoveryGate("invalid_memberaudit_result")
    return value


def same_host(before, after, check_growth=True):
    keys = ("hold_sha256", "active_upstream_sha256", "live_auth_services",
            "runtime_container_id", "active_upstream_container_ids")
    if any(before[k] != after[k] for k in keys):
        raise RecoveryGate("retained_host_identity_changed")
    if check_growth and before["disk"]["free_bytes"] - after["disk"]["free_bytes"] > 512 * 1024 ** 2:
        raise RecoveryGate("per_character_disk_growth_exceeds_bound")


def recover(attempt, pilot_name, mode, old_stage):
    stage = Path(__file__).parent
    report_path = stage / "memberaudit-report.json"
    output = {
        "schema_version": 1, "read_only": mode == "report", "scope": "memberaudit_october_outage",
        "attempt_id": attempt, "platform_required": "0.8.2", "deployment_attempted": False,
        "retained_recovery_changed": False, "preserve_recovery_resources": True,
        "scan_complete": False, "recovery_complete": False, "results": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    descriptor = None
    targets = []
    try:
        if (os.geteuid() != 0 or mode not in {"apply", "report"}
                or not re.fullmatch(r"gh-[0-9]+-[0-9]+", attempt)):
            raise RecoveryGate("invalid_recovery_invocation")
        pilot = qualified_host(Path(old_stage) / "run_structures_pilot.py")
        sys.path.insert(0, "/usr/local/lib/buh-platform-v2")
        from ops.deploy.contracts import ReceiverConfig
        from ops.deploy.docker_host import DockerHost
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        host = DockerHost(config)
        descriptor = os.open(config.state_dir / "deploy.lock", os.O_RDONLY | os.O_NOFOLLOW)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path, baseline, digest = pilot.latest_report()
        if digest != BASELINE_SHA or baseline["attempt_id"] != attempt:
            raise RecoveryGate("qualified_baseline_changed")
        targets, excluded = select_outage(baseline)
        if (len(targets) != 87 or sum(len(t["sections"]) for t in targets) != 348
                or len(excluded) != 61 or len({t["memberaudit_character_pk"] for t in excluded}) != 12):
            raise RecoveryGate("incident_totals_changed")
        targets = pilot_order(targets, pilot_name)
        output["baseline_report_sha256"], output["baseline_report_path"] = digest, str(path)
        output["before_host"] = pilot.verify_host(host, config, attempt)
        if output["before_host"]["hold_sha256"] != HOLD_SHA:
            raise RecoveryGate("retained_hold_changed")
        retained = retained_fingerprint(pilot, host, config, attempt)
        output["before_retained_resources"] = retained
        source = (stage / "memberaudit_recovery.py").read_text(encoding="utf-8")
        output["bounded_policy"] = {
            "pilot_characters": 1, "representative_characters": 3,
            "initial_batch_size": 5, "maximum_batch_size": 10, "maximum_characters": 87,
            "maximum_seconds": 2700, "per_character_seconds": 360,
            "per_character_requests": 256, "per_character_mail_bodies": 128,
            "maximum_disk_growth_bytes": 2 * 1024 ** 3,
        }

        def invoke(target, apply):
            code = ("exec(compile(" + repr(source) + ", '<buh-memberaudit-recovery>', 'exec'));emit("
                    + repr(target) + ",apply=" + repr(apply) + ",excluded=" + repr(excluded) + ")")
            return parse_result(host._compose(
                "exec", "-T", config.gunicorn_service, "python3", config.manage_py,
                "shell", "--no-imports", "-c", code, timeout=420, bounded_output=True,
                context="Bounded existing-token Member Audit recovery",
            ))

        # One bounded read-only eligibility check captures older sticky states without refreshing them.
        initial = invoke(targets[0], False)
        output["initial"] = initial
        if initial["category"] != "eligible_existing_token_not_yet_validated" or initial.get("preservation_failure"):
            raise RecoveryGate("pilot_preflight_not_eligible")
        excluded_before = initial["excluded_snapshot"]
        output["excluded_before"] = excluded_before
        checkpoint(report_path, output)
        started = time.monotonic()
        output["batches"] = []
        groups = [targets[:1], targets[1:4], targets[4:9]]
        rest = targets[9:]
        groups += [rest[i:i + 10] for i in range(0, len(rest), 10)]
        stop = False
        for number, group in enumerate(groups, 1):
            if mode == "report":
                break
            output["batches"].append({"number": number, "size": len(group), "completed": 0})
            for target in group:
                if time.monotonic() - started > 2700:
                    output["stop_reason"] = "bounded_runtime_reached"
                    stop = True
                    break
                before = pilot.verify_host(host, config, attempt)
                same_host(output["before_host"], before, check_growth=False)
                result = invoke(target, True)
                output["results"].append(result)
                output["batches"][-1]["completed"] += 1
                output["counts"] = summarize(targets, output["results"])
                checkpoint(report_path, output)
                after = pilot.verify_host(host, config, attempt)
                same_host(before, after)
                output["latest_host"], output["excluded_after"] = after, result["excluded_snapshot"]
                if result["excluded_snapshot"] != excluded_before:
                    raise RecoveryGate("older_unrelated_sticky_states_changed")
                if output["before_host"]["disk"]["free_bytes"] - after["disk"]["free_bytes"] > 2 * 1024 ** 3:
                    raise RecoveryGate("total_disk_growth_exceeds_bound")
                checkpoint(report_path, output)
                if not result["recovered"]:
                    output["stop_reason"] = result["category"]
                    stop = True
                    break
            if stop:
                break
        output["after_host"] = pilot.verify_host(host, config, attempt)
        same_host(output["before_host"], output["after_host"], check_growth=False)
        output["after_retained_resources"] = retained_fingerprint(pilot, host, config, attempt)
        if output["after_retained_resources"] != retained:
            raise RecoveryGate("retained_resources_changed_do_not_reconcile")
        output["retained_resources_unchanged_verified"] = True
        output["counts"] = summarize(targets, output["results"])
        output["scan_complete"] = True
        output["recovery_complete"] = output["counts"]["recovered_with_existing_tokens"] == 87
        output["observed_disk_growth_bytes"] = output["before_host"]["disk"]["free_bytes"] - output["after_host"]["disk"]["free_bytes"]
    except Exception as error:
        output["error_type"] = type(error).__name__
        if isinstance(error, (RecoveryGate, SelectionStop)):
            output["guard_reason"] = str(error)
        if targets:
            output["counts"] = summarize(targets, output["results"])
        output["stop_reason"] = output.get("stop_reason") or "guard_or_unknown_result_stop"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        output["finished_at"] = datetime.now(timezone.utc).isoformat()
        checkpoint(report_path, output)
    return output


def main():
    try:
        attempt, pilot_name, mode, old_stage = sys.argv[1:]
        output = recover(attempt, pilot_name, mode, old_stage)
    except Exception as error:
        output = {"schema_version": 1, "scope": "memberaudit_october_outage",
                  "scan_complete": False, "recovery_complete": False, "read_only": False,
                  "error_type": type(error).__name__, "preserve_recovery_resources": True,
                  "deployment_attempted": False, "retained_recovery_changed": False}
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output.get("scan_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
