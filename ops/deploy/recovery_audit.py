"""Read-only aggregate diagnosis from staged source; never installs a receiver.

This is a diagnostic report, not a recovery receipt or cleanup authorization.
It preserves the exact original recovery interval and fatal-error policy.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from . import worker_recovery as recovery
from .collect_worker_recovery import read_file
from .contracts import (
    DeploymentError, ReceiverConfig, extract_archive, load_validated_bundle,
)
from .docker_host import DockerHost, LogScanError, _safe_report_text
from .receiver import _open_lock

INSTALLED_RUNTIME = "5891086d9656114f2245bed9eea0870f0e8c07c0cd54a8afeaeaef95e702c38c"
ARCHIVE_SHA256 = "0d1dec44c94a5cad8c6568b195a206954d3f8b660c304af418de4f6e12273ad0"
LOG_SINCE = "2026-09-12T19:09:32+00:00"


def run_checks(checks) -> list[dict]:
    """Finish independent probes even after a failure; redact and bound output."""
    results = []
    for name, action in checks:
        result = {"check": name, "result": "passed"}
        try:
            detail = action()
            if name == "retained-interval-logs":
                result["allowed_findings"] = list(detail or ())
        except (DeploymentError, OSError, ValueError) as error:
            result.update(
                result="failed",
                error=_safe_report_text(str(error), 500)
                if isinstance(error, DeploymentError) else "Bounded read-only probe failed",
            )
            if isinstance(error, LogScanError):
                result.update(
                    findings=list(error.findings),
                    scan_complete=error.scan_complete,
                    findings_truncated=error.findings_truncated,
                )
        results.append(result)
    return results


def diagnose(host, value, bundle) -> dict:
    host.log_since = LOG_SINCE
    results = run_checks([
        ("retained-recovery-context", lambda: recovery.prepare_verification(host, value, bundle))
    ])
    if results[0]["result"] == "passed":
        results.extend(run_checks(recovery.restored_file_checks(host)))
    else:
        # Do not grant historical owner exceptions on a partially checked baseline.
        host.recovery_baseline_verified = False
        results.append({"check": "restored-files-and-slots", "result": "blocked-by-context"})
    results.extend(run_checks(host.restored_health_checks(bundle, set())))
    return {
        "schema_version": 1,
        "result": "diagnostic-only",
        "attempt_id": recovery.ATTEMPT,
        "log_since": LOG_SINCE,
        "checks": results,
        "all_checks_passed": all(item["result"] == "passed" for item in results),
        "preserve_resources": True,
        "authorizes_cleanup": False,
        "authorizes_deployment": False,
        "verification_receipt_created": False,
        "interpretation": "Current functional probes and retained historical log findings are separate evidence. A historical task error is not proof of a current outage or permission to ignore it.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-sha256", required=True, help="Hash of staged diagnostic docker_host.py")
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    lock = None
    started = datetime.now(timezone.utc).isoformat()
    try:
        if os.geteuid() != 0:
            raise DeploymentError("Root-owned reviewed diagnostic source is required")
        # Installed and staged code have separate identities. No file is installed.
        recovery.verify_pins(args.runtime_sha256, installed_sha256=INSTALLED_RUNTIME)
        recovery.verify_classifier_repair(INSTALLED_RUNTIME)
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        lock = _open_lock(config)
        loaded = DockerHost.load_incomplete_plan(config)
        if loaded is None:
            raise DeploymentError("Retained recovery plan is missing")
        metadata, archive = read_file(args.archive, maximum=8 * 1024 * 1024)
        if archive is None or metadata.get("sha256") != ARCHIVE_SHA256:
            raise DeploymentError("Immutable verification archive changed")
        # Scratch extraction only; no writes to the receiver, recovery journal or backups.
        with tempfile.TemporaryDirectory(prefix="buh-readonly-audit-") as temporary:
            payload = Path(temporary) / "payload"
            extract_archive(archive, payload)
            bundle = load_validated_bundle(payload, config)
            host, value = loaded
            report = diagnose(host, value, bundle)
        recovery.verify_pins(args.runtime_sha256, installed_sha256=INSTALLED_RUNTIME)
        recovery.verify_classifier_repair(INSTALLED_RUNTIME)
        report.update(
            started_at=started, finished_at=datetime.now(timezone.utc).isoformat(),
            installed_runtime_sha256=INSTALLED_RUNTIME,
            diagnostic_runtime_sha256=args.runtime_sha256,
            archive_sha256=ARCHIVE_SHA256,
        )
        print(json.dumps(report, sort_keys=True))
        return 0 if report["all_checks_passed"] else 1
    except (OSError, ValueError, DeploymentError) as error:
        print(json.dumps({
            "result": "diagnostic-blocked", "preserve_resources": True,
            "authorizes_cleanup": False, "authorizes_deployment": False,
            "error": _safe_report_text(str(error), 500)
            if isinstance(error, DeploymentError) else "Diagnostic prerequisites unavailable",
        }))
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
