"""Explicit, bounded verification/completion of the migrated worker-check failure.

No deployment entry point, full receiver upgrade, database restoration or retry.
Run only from the exact reviewed root-owned source after separate Work approval.
The default operation performs health reads and retains every recovery resource.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from .collect_worker_recovery import ATTEMPT, BACKUP, LIBRARY, STATE, read_file
from .contracts import (
    DeploymentError,
    ReceiverConfig,
    extract_archive,
    load_validated_bundle,
    sha256_file,
)
from .docker_host import DockerHost, _atomic_bytes
from .engine import _atomic_write

RELEASE = "6074b965cbd2e6ab2630cd539ee455b8d419aef6"
PLAN_SHA256 = "359eba2816cb0af9c7bda07739f64f14d74be115b0b418db33d0d6297e81afc8"
JOURNAL_SHA256 = "fb0db30cb18da821ab4cb7b4c3ab89f9848707eb7bbf7dc0a739452953eec50b"
ORIGINAL_RUNTIME = "0aa449968b98038fd68aca1b093640bea76d3889c185dce298775943881f20fe"
INSTALLED_WORKER_RUNTIME = (
    "c33fe29ed735e18f0c62bbbb31aecd7a6f3ee49a175a8c1c2ad8f33b90134ef8"
)
REPAIR_RECEIPT = Path("/etc/buh-platform-v2/WORKER-REPAIR.json")
LOG_REPAIR_RECEIPT = Path("/etc/buh-platform-v2/RETAINED-LOG-REPAIR.json")
PINS = {
    Path(
        "/etc/buh-platform-v2/receiver.json"
    ): "358cd57023d6afc26964fba4d5fa29a87e926aff02af097e28ed0e5a94a51de4",
    Path(
        "/etc/buh-platform-v2/INSTALL.json"
    ): "d7b5c208f223720bfaf33525aff3bb3f116054b654015cbc7298e648fc959379",
    LIBRARY
    / "ops/deploy/receiver.py": "69ab17cfb54a5af4d081c377752bcea9ccdd2d727ce3985ec67c65edd6e448ab",
    LIBRARY
    / "ops/deploy/engine.py": "dfe39593848cbb50b81a71ddd6d9b3d0807496cacf6972b01f923173e38bf0d4",
    STATE / "active-recovery.json": PLAN_SHA256,
    BACKUP / "RECOVERY.json": PLAN_SHA256,
    BACKUP
    / "BACKUP.json": "f85878cfe5164dbe012010048fe4f392719109756c0a753d97a857a4873ab1b9",
    STATE / "attempts" / f"{ATTEMPT}.json": JOURNAL_SHA256,
    Path(
        "/opt/aa-docker/conf/buh-platform-v2/nginx/upstream.conf"
    ): "272c362f1c1a7265c637edec86bad0112fd919438d005e231e2e649c25db1d9d",
}


def verify_pins(runtime_sha256: str, *, installed_sha256: str | None = None) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", runtime_sha256) is None:
        raise DeploymentError("Reviewed receiver file hash is invalid")
    if Path(__file__).with_name("docker_host.py") == LIBRARY / "ops/deploy/docker_host.py":
        raise DeploymentError(
            "Run the helper from the separately reviewed root-owned source"
        )
    for path, expected in {
        **PINS,
        LIBRARY / "ops/deploy/docker_host.py": installed_sha256 or runtime_sha256,
        Path(__file__).with_name("docker_host.py"): runtime_sha256,
    }.items():
        metadata, _raw = read_file(path)
        if metadata.get("sha256") != expected:
            raise DeploymentError("Worker recovery pinned host/source evidence changed")


def _repair_backup(runtime_sha256: str) -> Path:
    return Path("/var/backups/buh-receiver-upgrade") / f"worker-check-{runtime_sha256[:12]}"


def _log_repair_backup(runtime_sha256: str) -> Path:
    return (
        Path("/var/backups/buh-receiver-upgrade") / f"retained-logs-{runtime_sha256[:12]}"
    )


def _verify_worker_repair() -> str:
    """Verify, never replace, PR #61's already-installed receipt and backup."""
    metadata, raw = read_file(REPAIR_RECEIPT)
    original_install = PINS[Path("/etc/buh-platform-v2/INSTALL.json")]
    backup = _repair_backup(INSTALLED_WORKER_RUNTIME)
    expected = {
        "schema_version": 1,
        "result": "receiver-file-repaired",
        "base_source_commit": "fc0229b71c50c1bcb15d37f3625189fd9a7cb495",
        "base_install_sha256": original_install,
        "path": str(LIBRARY / "ops/deploy/docker_host.py"),
        "previous_sha256": ORIGINAL_RUNTIME,
        "sha256": INSTALLED_WORKER_RUNTIME,
        "backup_path": str(backup),
        "deployment_performed": False,
    }
    if raw != (json.dumps(expected, sort_keys=True) + "\n").encode():
        raise DeploymentError(
            "Already-installed worker repair receipt changed or is missing"
        )
    for path, digest in (
        (backup / "docker_host.py", ORIGINAL_RUNTIME),
        (backup / "INSTALL.json", original_install),
    ):
        if read_file(path)[0].get("sha256") != digest:
            raise DeploymentError(
                "Already-installed worker repair backup changed or is missing"
            )
    return metadata["sha256"]


def _log_repair_record(runtime_sha256: str, parent_receipt: str) -> dict:
    return {
        "schema_version": 1,
        "result": "receiver-log-reader-repaired",
        "base_source_commit": "fc0229b71c50c1bcb15d37f3625189fd9a7cb495",
        "base_install_sha256": PINS[Path("/etc/buh-platform-v2/INSTALL.json")],
        "path": str(LIBRARY / "ops/deploy/docker_host.py"),
        "previous_sha256": INSTALLED_WORKER_RUNTIME,
        "sha256": runtime_sha256,
        "backup_path": str(_log_repair_backup(runtime_sha256)),
        "parent_receipt_sha256": parent_receipt,
        "parent_receipt_path": str(REPAIR_RECEIPT),
        "deployment_performed": False,
    }


def verify_log_repair(runtime_sha256: str) -> None:
    parent = _verify_worker_repair()
    expected = _log_repair_record(runtime_sha256, parent)
    if (
        read_file(LOG_REPAIR_RECEIPT)[1]
        != (json.dumps(expected, sort_keys=True) + "\n").encode()
    ):
        raise DeploymentError("Retained-log repair receipt changed or is missing")
    backup = _log_repair_backup(runtime_sha256)
    for path, digest in (
        (backup / "docker_host.py", INSTALLED_WORKER_RUNTIME),
        (backup / "INSTALL.json", expected["base_install_sha256"]),
        (backup / "WORKER-REPAIR.json", parent),
    ):
        if read_file(path)[0].get("sha256") != digest:
            raise DeploymentError("Retained-log repair backup changed or is missing")


def install(runtime_sha256: str, *, confirmation: str) -> dict:
    """Historical PR #61 activation; its old baseline prevents replay on this host."""
    return _install_file(runtime_sha256, confirmation=confirmation, log_reader=False)


def install_log_reader(runtime_sha256: str, *, confirmation: str) -> dict:
    return _install_file(runtime_sha256, confirmation=confirmation, log_reader=True)


def _install_file(runtime_sha256: str, *, confirmation: str, log_reader: bool) -> dict:
    """One fixed-file receiver repair, not the historical receiver/VPS upgrade.

    Work must bind the file hash to its reviewed Git commit before owner execution.
    No service operation or receiver/preflight invocation occurs during activation.
    The original INSTALL.json stays truthful as the base; an additive receipt
    records the sole override. A failure restores the exact original file.
    """
    marker = "INSTALL RETAINED LOG READER" if log_reader else "INSTALL WORKER CHECK"
    previous = INSTALLED_WORKER_RUNTIME if log_reader else ORIGINAL_RUNTIME
    receipt = LOG_REPAIR_RECEIPT if log_reader else REPAIR_RECEIPT
    if confirmation != f"{marker} {runtime_sha256}":
        raise DeploymentError("Separate exact receiver-repair approval is required")
    verify_pins(runtime_sha256, installed_sha256=previous)
    parent_receipt = _verify_worker_repair() if log_reader else None
    if runtime_sha256 == previous:
        raise DeploymentError("Receiver repair payload is unchanged")
    if receipt.exists() or receipt.is_symlink():
        raise DeploymentError("Receiver repair receipt already exists; do not repeat")
    target = LIBRARY / "ops/deploy/docker_host.py"
    old_meta, old = read_file(target)
    _meta, new = read_file(Path(__file__).with_name("docker_host.py"))
    if old is None or new is None or old_meta.get("mode") != "0o644":
        raise DeploymentError("Receiver repair input identity is invalid")
    compile(new, "reviewed-docker_host.py", "exec")
    backup = (
        _log_repair_backup(runtime_sha256) if log_reader else _repair_backup(runtime_sha256)
    )
    # Validate the existing root-only parent, never mkdir through a replaceable path.
    from .receiver import _verify_root_owned_ancestors

    _verify_root_owned_ancestors(backup)
    backup.mkdir(mode=0o700, exist_ok=False)
    _atomic_bytes(backup / "docker_host.py", old, 0o600, owner=(0, 0))
    install_meta, install_bytes = read_file(Path("/etc/buh-platform-v2/INSTALL.json"))
    _atomic_bytes(backup / "INSTALL.json", install_bytes, 0o600, owner=(0, 0))
    if log_reader:
        _atomic_bytes(
            backup / "WORKER-REPAIR.json", read_file(REPAIR_RECEIPT)[1], 0o600, owner=(0, 0)
        )
    result = {
        "schema_version": 1,
        "result": "receiver-file-repaired",
        "base_source_commit": "fc0229b71c50c1bcb15d37f3625189fd9a7cb495",
        "base_install_sha256": install_meta["sha256"],
        "path": str(target),
        "previous_sha256": ORIGINAL_RUNTIME,
        "sha256": runtime_sha256,
        "backup_path": str(backup),
        "deployment_performed": False,
    }
    if log_reader:
        result = _log_repair_record(runtime_sha256, parent_receipt)
    try:
        _atomic_bytes(target, new, 0o644, owner=(0, 0))
        verify_pins(runtime_sha256)
        if log_reader and _verify_worker_repair() != parent_receipt:
            raise DeploymentError("Worker repair receipt changed during activation")
        _atomic_write(receipt, (json.dumps(result, sort_keys=True) + "\n").encode())
    except BaseException:
        _atomic_bytes(target, old, 0o644, owner=(0, 0))
        if read_file(target)[0].get("sha256") != previous:
            raise DeploymentError("Receiver repair restore failed; preserve its backup")
        raise
    return result


def verify_restored(host: DockerHost, value: dict, bundle) -> dict:
    """Reestablish only the confirmed unchanged, zero-restart production baseline.

    Never turn a newly observed nonzero count into a historical success baseline.
    The immutable archive supplies validated migration lineage/owner context, not
    deployment authorization. This path cannot migrate or replace any service.
    """
    if (
        value["attempt_id"] != ATTEMPT
        or bundle.request.attempt_id != ATTEMPT
        or bundle.request.release_commit != RELEASE
        or bundle.request.mode != "deploy"
        or bundle.request.recovery_sha256
        != "c88638c6c47c97c1bc62413fb4843e8668a2f260cb046aca31ec4def4d7946eb"
        or value["flags"]
        != {
            "gunicorn_replacement_started": False,
            "migration_started": True,
            "platform_current_write_started": False,
            "static_collection_started": True,
            "traffic_switch_started": True,
            "workers_replacement_started": False,
        }
        or host.previous_web_slots != (f"buh-web-previous-{ATTEMPT}-1",)
        or host.candidate_web_slots != (f"buh-web-candidate-{ATTEMPT}-1",)
    ):
        raise DeploymentError("Worker recovery attempt or mutation state changed")
    images, counts = dict(host.previous_images), dict(host.auth_replica_counts)
    host._capture_live_images()
    host._capture_infrastructure_restart_baselines()
    if (
        host.previous_images != images
        or host.auth_replica_counts != counts
        or not host.restart_baselines
        or any(host.restart_baselines.values())
    ):
        raise DeploymentError(
            "Retained production images, topology or zero restarts changed"
        )
    # Revalidate exact original v0.5.6 labels, Compose overlays, Nginx/proxy and
    # guild/owner association. Do not simply set the owner-transition flag.
    host._validate_recovery_host_baseline(bundle)
    host.log_since = "2026-09-12T19:09:32+00:00"
    for saved, live in (
        (host.original_dockerfile, host.config.app_dir / host.config.custom_dockerfile),
        (host.original_local_settings, host.config.app_dir / host.config.local_settings),
        (host.original_platform_current, host._platform_current_path()),
        (host.original_deployment_current, host.config.state_dir / "current.json"),
    ):
        if saved is None or sha256_file(saved) != sha256_file(live):
            raise DeploymentError("Retained rollback file restoration is incomplete")
    host._verify_previous_static_fallback()
    for container in host._container_names_for_service(host.config.gunicorn_service):
        host._verify_previous_static_manifest(container)
    host._wait_for_slots(
        host.candidate_web_slots,
        "sha256:46bbe055aa71f67e4f2c23d4f9aa89c352a5bfc115da8302d60f954bd7558a2d",
        context="Retained candidate safety slot identity",
    )
    host._verify_proxy_upstream_bytes(
        host._upstream_path().read_text(encoding="utf-8"), context="Retained upstream"
    )
    host._proxy_exec("nginx", "-t", context="Read-only retained Nginx validation")
    host._verify_restored(bundle, set())
    return {
        "schema_version": 1,
        "attempt_id": ATTEMPT,
        "release_commit": RELEASE,
        "result": "restored-production-verified",
        "cleanup": "not-run",
        "database_restored": False,
        "migrations_reversed": False,
        "workers": sorted(host._expected_celery_nodes()),
        "retained_plan_sha256": PLAN_SHA256,
        "original_journal_sha256": JOURNAL_SHA256,
        "warnings": list(host._transition_log_findings),
    }


def complete(host, value, bundle, *, confirmation: str) -> dict:
    if confirmation != f"COMPLETE ROLLBACK {ATTEMPT}":
        raise DeploymentError("Separate exact rollback-completion approval is required")
    result = verify_restored(host, value, bundle)
    # Flags are independently pinned false for all live replacements. The normal
    # traffic-first rollback rechecks health before cleanup; no retry/deploy/DB op.
    result["recovery"] = host.rollback(bundle, value["phase"])
    result["cleanup"] = "passed"
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=("install", "install-logs", "verify", "complete")
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--archive-sha256")
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    lock = None
    try:
        from .receiver import _open_lock

        if os.geteuid() != 0:
            raise DeploymentError("Root-only reviewed recovery is required")
        verify_pins(
            args.runtime_sha256,
            installed_sha256={
                "install": ORIGINAL_RUNTIME,
                "install-logs": INSTALLED_WORKER_RUNTIME,
            }.get(args.operation),
        )
        if args.operation == "install-logs":
            _verify_worker_repair()
        elif args.operation != "install":
            verify_log_repair(args.runtime_sha256)
        config = ReceiverConfig.load(Path("/etc/buh-platform-v2/receiver.json"))
        lock = _open_lock(config)
        if args.operation in {"install", "install-logs"}:
            installer = install_log_reader if args.operation == "install-logs" else install
            print(
                json.dumps(
                    installer(args.runtime_sha256, confirmation=args.confirm),
                    sort_keys=True,
                )
            )
            return 0
        verify_pins(args.runtime_sha256)
        verify_log_repair(args.runtime_sha256)
        loaded = DockerHost.load_incomplete_plan(config)
        if loaded is None:
            raise DeploymentError(
                "Pinned recovery plan is missing; do not repeat completion"
            )
        if args.archive is None or not args.archive_sha256:
            raise DeploymentError("Exact immutable verification archive is required")
        meta, archive = read_file(args.archive, maximum=8 * 1024 * 1024)
        if archive is None or meta.get("sha256") != args.archive_sha256:
            raise DeploymentError("Reviewed immutable recovery archive changed")
        receipt = config.state_dir / f"worker-recovery-{ATTEMPT}.json"
        if args.operation == "complete" and (receipt.exists() or receipt.is_symlink()):
            raise DeploymentError("Completion receipt exists; do not repeat recovery")
        with tempfile.TemporaryDirectory(
            prefix="worker-evidence-", dir=config.state_dir
        ) as tmp:
            payload = Path(tmp) / "payload"
            extract_archive(archive, payload)
            bundle = load_validated_bundle(payload, config)
            host, value = loaded
            if args.operation == "complete":
                result = complete(host, value, bundle, confirmation=args.confirm)
                result["runtime_sha256"] = args.runtime_sha256
                result["archive_sha256"] = args.archive_sha256
                _atomic_write(receipt, (json.dumps(result, sort_keys=True) + "\n").encode())
            else:
                result = verify_restored(host, value, bundle)
        result["runtime_sha256"] = args.runtime_sha256
        result["archive_sha256"] = hashlib.sha256(archive).hexdigest()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, DeploymentError) as exc:
        # No response bodies, Docker output, configuration values or tracebacks.
        print(
            json.dumps(
                {
                    "result": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500]
                    if isinstance(exc, DeploymentError)
                    else "bounded operation failed",
                    "cleanup": "not-established",
                    "preserve_resources": True,
                }
            )
        )
        return 1
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
