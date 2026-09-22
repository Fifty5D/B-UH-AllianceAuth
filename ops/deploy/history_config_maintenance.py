"""Add the ESI History setup command to the receiver after recovery.

The default operation is a read-only plan.  Apply changes one configuration
field, records the exact prior recovery provenance, and never starts or stops a
service, deploys an application, or cleans recovery evidence.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import worker_recovery as recovery
from .contracts import DeploymentError, ReceiverConfig, canonical_json_bytes
from .docker_host import _atomic_bytes
from .receiver import _open_lock, _verify_root_owned_ancestors


CONFIG = Path("/etc/buh-platform-v2/receiver.json")
INSTALL = Path("/etc/buh-platform-v2/INSTALL.json")
PARENT_RECEIPT = recovery.EXHAUSTION_REPAIR_RECEIPT
RECEIPT = Path("/etc/buh-platform-v2/HISTORY-CONFIG-MAINTENANCE.json")
RUNTIME = recovery.LIBRARY / "ops/deploy/docker_host.py"
COMPLETION_NAME = f"worker-recovery-{recovery.ATTEMPT}.json"
BACKUP_ROOT = Path("/var/backups/buh-receiver-upgrade")
EXPECTED_ARGUMENTS = ["--no-color"]
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RECOVERY_CHECKS = (
    "restored-file-custom.dockerfile",
    "restored-file-local.py",
    "restored-file-CURRENT.json",
    "restored-file-current.json",
    "previous-static-fallback",
    "previous-static-manifests",
    "candidate-safety-slots",
    "proxy-upstream",
    "nginx-configuration",
    "recovered-data-syncs",
    "services-and-restarts",
    "installed-images",
    "django",
    "migration-state",
    "redis",
    "celery-workers-queues-tasks",
    "internal-http",
    "static-asset",
    "application-buh_moon_tax_status",
    "application-buh_structure_ops_status",
    "application-buh_mining_check",
    "public-http",
    "retained-interval-logs",
)


@dataclass(frozen=True)
class MaintenancePaths:
    config: Path = CONFIG
    install: Path = INSTALL
    parent_receipt: Path = PARENT_RECEIPT
    receipt: Path = RECEIPT
    runtime: Path = RUNTIME
    backup_root: Path = BACKUP_ROOT


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_private(path: Path, *, maximum: int = 1024 * 1024) -> tuple[dict, bytes]:
    _verify_root_owned_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeploymentError("Required receiver provenance is unavailable") from exc
    with os.fdopen(descriptor, "rb") as stream:
        details = os.fstat(stream.fileno())
        mode = stat.S_IMODE(details.st_mode)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != 0
            or mode & 0o022
            or details.st_size <= 0
            or details.st_size > maximum
        ):
            raise DeploymentError("Required receiver provenance is unsafe")
        data = stream.read(maximum + 1)
        if len(data) != details.st_size:
            raise DeploymentError("Required receiver provenance changed while reading")
    return {
        "path": str(path),
        "sha256": _digest(data),
        "uid": details.st_uid,
        "gid": details.st_gid,
        "mode": f"{mode:04o}",
        "size": details.st_size,
    }, data


def _json_object(data: bytes, context: str) -> dict:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"{context} is invalid") from exc
    if not isinstance(value, dict):
        raise DeploymentError(f"{context} is invalid")
    return value


def _validate_install(value: dict, current_config_sha256: str) -> None:
    if (
        set(value) != {"config_sha256", "files", "schema_version", "source_commit"}
        or value.get("schema_version") != 1
        or HASH_RE.fullmatch(str(value.get("config_sha256", ""))) is None
        or COMMIT_RE.fullmatch(str(value.get("source_commit", ""))) is None
        or not isinstance(value.get("files"), dict)
        or value["config_sha256"] != current_config_sha256
    ):
        raise DeploymentError("Current receiver install provenance does not match config")


def _validate_completion(value: dict, runtime_sha256: str) -> None:
    required = {
        "schema_version": 1,
        "attempt_id": recovery.ATTEMPT,
        "result": "restored-production-verified-with-reviewed-history",
        "cleanup": "passed",
        "deployment_performed": False,
        "database_restored": False,
        "migrations_reversed": False,
        "retained_plan_sha256": recovery.PLAN_SHA256,
        "original_journal_sha256": recovery.JOURNAL_SHA256,
        "runtime_sha256": runtime_sha256,
        "installed_runtime_sha256": runtime_sha256,
        "historical_interval_clean": False,
    }
    if any(value.get(key) != expected for key, expected in required.items()):
        raise DeploymentError("Reviewed recovery completion receipt is invalid")
    checks = value.get("checks")
    if (
        not isinstance(checks, list)
        or tuple(item.get("check") if isinstance(item, dict) else None for item in checks)
        != RECOVERY_CHECKS
        or any(item.get("result") != "passed" for item in checks)
        or not isinstance(value.get("recovery"), str)
        or not value["recovery"].strip()
    ):
        raise DeploymentError("Reviewed recovery completion receipt is incomplete")
    for key in ("archive_sha256", "review_sha256"):
        if HASH_RE.fullmatch(str(value.get(key, ""))) is None:
            raise DeploymentError("Reviewed recovery completion receipt is invalid")


def _candidate(current: dict) -> dict:
    setup = current.get("setup_arguments")
    if not isinstance(setup, dict):
        raise DeploymentError("Receiver setup arguments are invalid")
    if "buh_archive_setup" in setup:
        if setup["buh_archive_setup"] != EXPECTED_ARGUMENTS:
            raise DeploymentError("Existing ESI History setup arguments differ")
        return current
    updated = json.loads(json.dumps(current))
    updated["setup_arguments"]["buh_archive_setup"] = list(EXPECTED_ARGUMENTS)
    if {
        key: value
        for key, value in updated["setup_arguments"].items()
        if key != "buh_archive_setup"
    } != setup:
        raise DeploymentError("Receiver configuration update changed unrelated setup")
    return updated


def _validate_candidate(data: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="buh-history-config-") as directory:
        candidate = Path(directory) / "receiver.json"
        candidate.write_bytes(data)
        ReceiverConfig.load(candidate)


def _provenance_digest(value: dict) -> str:
    return _digest(canonical_json_bytes(value))


def _receipt_record(plan: dict, backup: Path) -> dict:
    provenance = plan["provenance"]
    return {
        "schema_version": 1,
        "result": "history-setup-command-configured",
        "path": plan["path"],
        "previous_sha256": plan["old_config_sha256"],
        "sha256": plan["new_config_sha256"],
        "arguments": list(EXPECTED_ARGUMENTS),
        "backup_path": str(backup),
        "base_install_path": provenance["install_path"],
        "base_install_sha256": provenance["install_sha256"],
        "base_install_config_sha256": provenance["install_config_sha256"],
        "parent_receipt_path": provenance["parent_receipt_path"],
        "parent_receipt_sha256": provenance["parent_receipt_sha256"],
        "completion_receipt_path": provenance["completion_receipt_path"],
        "completion_receipt_sha256": provenance["completion_receipt_sha256"],
        "receiver_runtime_path": provenance["runtime_path"],
        "receiver_runtime_sha256": provenance["runtime_sha256"],
        "provenance_sha256": plan["provenance_sha256"],
        "deployment_performed": False,
        "services_restarted": False,
        "recovery_cleanup_performed": False,
    }


def _verify_backup(record: dict) -> None:
    backup = Path(record["backup_path"])
    expected = {
        "receiver.json": record["previous_sha256"],
        "INSTALL.json": record["base_install_sha256"],
        "parent-repair.json": record["parent_receipt_sha256"],
        "recovery-completion.json": record["completion_receipt_sha256"],
    }
    try:
        details = backup.lstat()
        names = {entry.name for entry in backup.iterdir()}
    except OSError as exc:
        raise DeploymentError("History configuration backup is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or backup.is_symlink()
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) != 0o700
        or names != set(expected)
    ):
        raise DeploymentError("History configuration backup is unsafe")
    for name, digest in expected.items():
        metadata = _read_private(backup / name)[0]
        if metadata["mode"] != "0600" or metadata["sha256"] != digest:
            raise DeploymentError("History configuration backup differs")


def _verify_applied(
    paths: MaintenancePaths,
    current_sha256: str,
    receipt_data: bytes,
    *,
    install: dict,
    install_meta: dict,
    parent_meta: dict,
    completion: Path,
    completion_meta: dict,
    runtime_meta: dict,
) -> dict:
    record = _json_object(receipt_data, "History configuration receipt")
    previous = record.get("previous_sha256")
    if HASH_RE.fullmatch(str(previous or "")) is None:
        raise DeploymentError("History configuration receipt is invalid")
    _validate_install(install, previous)
    provenance = {
        "install_path": str(paths.install),
        "install_sha256": install_meta["sha256"],
        "install_config_sha256": install["config_sha256"],
        "parent_receipt_path": str(paths.parent_receipt),
        "parent_receipt_sha256": parent_meta["sha256"],
        "completion_receipt_path": str(completion),
        "completion_receipt_sha256": completion_meta["sha256"],
        "runtime_path": str(paths.runtime),
        "runtime_sha256": runtime_meta["sha256"],
    }
    plan = {
        "path": str(paths.config),
        "old_config_sha256": previous,
        "new_config_sha256": current_sha256,
        "provenance": provenance,
        "provenance_sha256": _provenance_digest(provenance),
    }
    expected = _receipt_record(
        plan,
        paths.backup_root / f"history-config-{current_sha256[:12]}",
    )
    if record != expected:
        raise DeploymentError("History configuration receipt does not match config")
    _verify_backup(record)
    return record


def build_plan(
    paths: MaintenancePaths = MaintenancePaths(), *, verify_repairs: bool = True
) -> dict:
    config_meta, config_data = _read_private(paths.config, maximum=64 * 1024)
    current = _json_object(config_data, "Receiver configuration")
    # Completion lives in the state directory declared by the validated config.
    loaded_config = ReceiverConfig.load(paths.config)
    active = loaded_config.state_dir / "active-recovery.json"
    try:
        active.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DeploymentError("Recovery state is unreadable") from exc
    else:
        raise DeploymentError("Recovery is still active; configuration is held")
    completion = loaded_config.state_dir / COMPLETION_NAME

    install_meta, install_data = _read_private(paths.install)
    install = _json_object(install_data, "Receiver install receipt")
    parent_meta, _parent_data = _read_private(paths.parent_receipt)
    runtime_meta, _runtime_data = _read_private(paths.runtime, maximum=8 * 1024 * 1024)
    completion_meta, completion_data = _read_private(completion, maximum=8 * 1024 * 1024)
    completion_value = _json_object(completion_data, "Recovery completion receipt")
    _validate_completion(completion_value, runtime_meta["sha256"])
    if verify_repairs:
        recovery.verify_exhaustion_repair(runtime_meta["sha256"])

    candidate = _candidate(current)
    candidate_data = canonical_json_bytes(candidate)
    _validate_candidate(candidate_data)

    if candidate == current:
        try:
            receipt_meta, receipt_data = _read_private(paths.receipt)
        except DeploymentError as exc:
            raise DeploymentError(
                "ESI History setup is present without its maintenance receipt"
            ) from exc
        record = _verify_applied(
            paths,
            config_meta["sha256"],
            receipt_data,
            install=install,
            install_meta=install_meta,
            parent_meta=parent_meta,
            completion=completion,
            completion_meta=completion_meta,
            runtime_meta=runtime_meta,
        )
        return {
            "schema_version": 1,
            "result": "already-applied",
            "path": str(paths.config),
            "old_config_sha256": record["previous_sha256"],
            "new_config_sha256": record["sha256"],
            "provenance_sha256": record["provenance_sha256"],
            "receipt_sha256": receipt_meta["sha256"],
            "deployment_performed": False,
            "services_restarted": False,
            "recovery_cleanup_performed": False,
        }

    if paths.receipt.exists() or paths.receipt.is_symlink():
        raise DeploymentError("History configuration receipt exists before activation")
    _validate_install(install, config_meta["sha256"])
    provenance = {
        "install_path": str(paths.install),
        "install_sha256": install_meta["sha256"],
        "install_config_sha256": install["config_sha256"],
        "parent_receipt_path": str(paths.parent_receipt),
        "parent_receipt_sha256": parent_meta["sha256"],
        "completion_receipt_path": str(completion),
        "completion_receipt_sha256": completion_meta["sha256"],
        "runtime_path": str(paths.runtime),
        "runtime_sha256": runtime_meta["sha256"],
    }
    return {
        "schema_version": 1,
        "result": "planned",
        "path": str(paths.config),
        "old_config_sha256": config_meta["sha256"],
        "new_config_sha256": _digest(candidate_data),
        "provenance": provenance,
        "provenance_sha256": _provenance_digest(provenance),
        "change": {"setup_arguments.buh_archive_setup": EXPECTED_ARGUMENTS},
        "deployment_performed": False,
        "services_restarted": False,
        "recovery_cleanup_performed": False,
    }


def _create_backup(paths: MaintenancePaths, plan: dict, old_config: bytes) -> Path:
    backup = paths.backup_root / ("history-config-" + plan["new_config_sha256"][:12])
    _verify_root_owned_ancestors(backup)
    try:
        details = paths.backup_root.lstat()
    except OSError as exc:
        raise DeploymentError("Receiver backup root is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or paths.backup_root.is_symlink()
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise DeploymentError("Receiver backup root is unsafe")
    sources = {
        "receiver.json": old_config,
        "INSTALL.json": _read_private(paths.install)[1],
        "parent-repair.json": _read_private(paths.parent_receipt)[1],
        "recovery-completion.json": _read_private(
            Path(plan["provenance"]["completion_receipt_path"])
        )[1],
    }
    expected = {name: _digest(data) for name, data in sources.items()}
    planned = {
        "receiver.json": plan["old_config_sha256"],
        "INSTALL.json": plan["provenance"]["install_sha256"],
        "parent-repair.json": plan["provenance"]["parent_receipt_sha256"],
        "recovery-completion.json": plan["provenance"]["completion_receipt_sha256"],
    }
    if expected != planned:
        raise DeploymentError("History configuration provenance changed before backup")
    try:
        backup.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        try:
            details = backup.lstat()
        except OSError as exc:
            raise DeploymentError("History configuration backup is unavailable") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or backup.is_symlink()
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) != 0o700
            or {entry.name for entry in backup.iterdir()} != set(expected)
            or any(
                _read_private(backup / name)[0]["sha256"] != digest
                for name, digest in expected.items()
            )
        ):
            raise DeploymentError("Existing history configuration backup differs")
        return backup
    except OSError as exc:
        raise DeploymentError("History configuration backup creation failed") from exc
    for name, data in sources.items():
        _atomic_bytes(backup / name, data, 0o600, owner=(0, 0))
    return backup


def apply(
    *,
    old_config_sha256: str | None,
    new_config_sha256: str | None,
    provenance_sha256: str | None,
    paths: MaintenancePaths = MaintenancePaths(),
    verify_repairs: bool = True,
) -> dict:
    if os.geteuid() != 0:
        raise DeploymentError("History configuration maintenance must run as root")
    if any(
        HASH_RE.fullmatch(value or "") is None
        for value in (old_config_sha256, new_config_sha256, provenance_sha256)
    ):
        raise DeploymentError("Exact plan hashes are required for apply")

    initial = ReceiverConfig.load(paths.config)
    lock = _open_lock(initial)
    try:
        plan = build_plan(paths, verify_repairs=verify_repairs)
        if plan["result"] == "already-applied":
            if (
                plan["old_config_sha256"] != old_config_sha256
                or plan["new_config_sha256"] != new_config_sha256
                or plan["provenance_sha256"] != provenance_sha256
            ):
                raise DeploymentError("Applied history configuration differs from pins")
            return plan
        if (
            plan["old_config_sha256"] != old_config_sha256
            or plan["new_config_sha256"] != new_config_sha256
            or plan["provenance_sha256"] != provenance_sha256
        ):
            raise DeploymentError("History configuration plan differs from exact pins")

        _meta, old_config = _read_private(paths.config, maximum=64 * 1024)
        current = _json_object(old_config, "Receiver configuration")
        candidate_data = canonical_json_bytes(_candidate(current))
        if _digest(candidate_data) != new_config_sha256:
            raise DeploymentError("History configuration candidate changed")
        _validate_candidate(candidate_data)
        backup = _create_backup(paths, plan, old_config)
        receipt = _receipt_record(plan, backup)
        receipt_data = (json.dumps(receipt, sort_keys=True) + "\n").encode("ascii")
        try:
            _atomic_bytes(paths.config, candidate_data, 0o600, owner=(0, 0))
            if (
                _read_private(paths.config, maximum=64 * 1024)[0]["sha256"]
                != new_config_sha256
            ):
                raise DeploymentError("History configuration activation differs")
            ReceiverConfig.load(paths.config)
            _atomic_bytes(paths.receipt, receipt_data, 0o600, owner=(0, 0))
            receipt_meta, installed_receipt = _read_private(paths.receipt)
            if installed_receipt != receipt_data:
                raise DeploymentError("History configuration receipt differs")
            installed_plan = build_plan(paths, verify_repairs=verify_repairs)
            if installed_plan["result"] != "already-applied":
                raise DeploymentError("History configuration activation is incomplete")
        except BaseException as exc:
            try:
                _atomic_bytes(paths.config, old_config, 0o600, owner=(0, 0))
                paths.receipt.unlink(missing_ok=True)
                restored = _read_private(paths.config, maximum=64 * 1024)[0]["sha256"]
            except BaseException as restore_exc:
                raise DeploymentError(
                    "History configuration activation and rollback failed; preserve backup"
                ) from restore_exc
            if restored != old_config_sha256:
                raise DeploymentError(
                    "History configuration rollback differs; preserve backup"
                ) from exc
            raise DeploymentError(
                "History configuration activation failed; prior config restored"
            ) from exc
        return {
            **receipt,
            "receipt_path": str(paths.receipt),
            "receipt_sha256": receipt_meta["sha256"],
        }
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", choices=("plan", "apply"), default="plan")
    parser.add_argument("--old-config-sha256")
    parser.add_argument("--new-config-sha256")
    parser.add_argument("--provenance-sha256")
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "apply":
            result = apply(
                old_config_sha256=arguments.old_config_sha256,
                new_config_sha256=arguments.new_config_sha256,
                provenance_sha256=arguments.provenance_sha256,
            )
        else:
            result = build_plan()
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError, TypeError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "result": "failed",
                    "error": str(exc)[:500]
                    if isinstance(exc, DeploymentError)
                    else "Bounded history configuration maintenance failed",
                    "deployment_performed": False,
                    "services_restarted": False,
                    "recovery_cleanup_performed": False,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
