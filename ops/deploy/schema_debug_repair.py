"""One-file receiver repair for the verified ESI schema DEBUG false positive.

Run from an independently staged, reviewed Git tree. This operation never
starts a deployment, changes application containers, or alters recovery files.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat

from .collect_worker_recovery import read_file
from .contracts import DeploymentError, ReceiverConfig
from .docker_host import _atomic_bytes
from .receiver import _open_lock, _verify_root_owned_ancestors


TARGET = Path("/usr/local/lib/buh-platform-v2/ops/deploy/docker_host.py")
CONFIG = Path("/etc/buh-platform-v2/receiver.json")
RECEIPT = Path("/etc/buh-platform-v2/ESI-SCHEMA-LOG-REPAIR.json")
BACKUP_ROOT = Path("/var/backups/buh-receiver-upgrade")
FAILED_ATTEMPT = "gh-36317022181-1"
PREVIOUS_SHA256 = "7d91f2fc88e99a4a59a651046451829711fbba60d9f9b014ef68945f3156a03b"
PINS = {
    CONFIG: "28d37ca1a149a81405c036c4668a235952182681f274847067ebb13d7f594d4e",
    Path("/etc/buh-platform-v2/INSTALL.json"): "d7b5c208f223720bfaf33525aff3bb3f116054b654015cbc7298e648fc959379",
    Path("/etc/buh-platform-v2/OWNER-EXHAUSTION-REPAIR.json"): "aa18cfe65495e4360131f39417ed68934d8a240c1819b294b9af1cb3abe0dca4",
    Path("/etc/buh-platform-v2/HISTORY-CONFIG-MAINTENANCE.json"): "ed4b8392c91da345286bb18bf503e606c7c2abab91df8b0358f149a8c20dd958",
    Path("/var/lib/buh-platform-v2/worker-recovery-gh-34713182347-1.json"): "95bf1453f9386af40ff1cad4af57a5e28145097431e6dcdde327bdcb19486d53",
    Path("/var/lib/buh-platform-v2/current.json"): "d1d7ecb4256e6e450f684110819e1641f519f56bd6d45058422e794fd3624ce5",
    Path("/var/lib/buh-platform-v2/attempts/gh-36317022181-1.json"): "f0bd6beb3c99eb4ac3f13f7db3bfc484d88b433471d134cb21b84902a86c3528",
}


def _require_sha(path: Path, expected: str, *, mode: str | None = None) -> bytes:
    meta, raw = read_file(path)
    if raw is None or meta.get("sha256") != expected or (mode and meta.get("mode") != mode):
        raise DeploymentError("Pinned receiver or host evidence changed")
    return raw


def _baseline() -> bytes:
    if os.geteuid() != 0:
        raise DeploymentError("Receiver repair requires root")
    if Path(__file__).with_name("docker_host.py") == TARGET:
        raise DeploymentError("Run only from separately staged reviewed source")
    for path, digest in PINS.items():
        _require_sha(path, digest)
    previous = _require_sha(TARGET, PREVIOUS_SHA256, mode="0o644")
    if read_file(Path("/var/lib/buh-platform-v2/active-recovery.json"))[1] is not None:
        raise DeploymentError("Active recovery returned")
    if RECEIPT.exists() or RECEIPT.is_symlink():
        raise DeploymentError("This one-time receiver repair already has a receipt")
    attempt = json.loads(
        _require_sha(
            Path("/var/lib/buh-platform-v2/attempts") / f"{FAILED_ATTEMPT}.json",
            PINS[Path("/var/lib/buh-platform-v2/attempts") / f"{FAILED_ATTEMPT}.json"],
        )
    )
    if attempt.get("result") != "failed" or attempt.get("rollback", {}).get("result") != "passed":
        raise DeploymentError("Failed attempt rollback is not verified")
    return previous


def _verify_backup_root() -> None:
    _verify_root_owned_ancestors(BACKUP_ROOT / "new-backup")
    details = BACKUP_ROOT.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise DeploymentError("Receiver backup root is unsafe")


def run(*, mode: str, replacement_sha256: str, source_commit: str, confirmation: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", replacement_sha256):
        raise DeploymentError("Replacement hash is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise DeploymentError("Reviewed source commit is invalid")
    if mode == "install" and confirmation != f"INSTALL ESI SCHEMA DEBUG CHECKER {replacement_sha256}":
        raise DeploymentError("Exact receiver-repair confirmation is required")
    if mode not in {"plan", "install"}:
        raise DeploymentError("Unknown receiver-repair operation")
    staged = Path(__file__).with_name("docker_host.py")
    new = _require_sha(staged, replacement_sha256)
    compile(new, str(staged), "exec")
    if replacement_sha256 == PREVIOUS_SHA256:
        raise DeploymentError("Replacement is identical to installed receiver")
    config = ReceiverConfig.load(CONFIG)
    lock = _open_lock(config)
    try:
        previous = _baseline()
        _verify_backup_root()
        backup = BACKUP_ROOT / f"esi-schema-debug-{replacement_sha256[:12]}"
        if backup.exists() or backup.is_symlink():
            raise DeploymentError("Receiver-repair backup already exists")
        record = {
            "schema_version": 1,
            "result": "esi-schema-log-checker-repaired",
            "source_commit": source_commit,
            "path": str(TARGET),
            "previous_sha256": PREVIOUS_SHA256,
            "sha256": replacement_sha256,
            "backup_path": str(backup),
            "failed_attempt": FAILED_ATTEMPT,
            "deployment_performed": False,
        }
        if mode == "plan":
            return {**record, "result": "ready-to-install"}
        backup.mkdir(mode=0o700, exist_ok=False)
        _atomic_bytes(backup / "docker_host.py", previous, 0o600, owner=(0, 0))
        _atomic_bytes(backup / "replacement.py", new, 0o600, owner=(0, 0))
        _require_sha(backup / "docker_host.py", PREVIOUS_SHA256, mode="0o600")
        _require_sha(backup / "replacement.py", replacement_sha256, mode="0o600")
        _baseline()
        try:
            _atomic_bytes(TARGET, new, 0o644, owner=(0, 0))
            _require_sha(TARGET, replacement_sha256, mode="0o644")
            _atomic_bytes(
                RECEIPT, (json.dumps(record, sort_keys=True) + "\n").encode("ascii"),
                0o600, owner=(0, 0),
            )
        except BaseException:
            _atomic_bytes(TARGET, previous, 0o644, owner=(0, 0))
            _require_sha(TARGET, PREVIOUS_SHA256, mode="0o644")
            raise
        return record
    finally:
        os.close(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "install"))
    parser.add_argument("--replacement-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    try:
        report = run(
            mode=args.mode, replacement_sha256=args.replacement_sha256,
            source_commit=args.source_commit, confirmation=args.confirmation,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except (DeploymentError, OSError, ValueError) as error:
        print(json.dumps({"result": "blocked", "error": str(error)[:200]}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
