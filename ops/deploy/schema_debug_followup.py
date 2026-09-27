"""One-time receiver follow-up for the isolated Celery ESI schema DEBUG line.

The first checker repair and both failed deployment attempts remain immutable.
This changes only the installed checker file under the production lock.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from . import schema_debug_repair as prior
from .collect_worker_recovery import read_file
from .contracts import DeploymentError, ReceiverConfig
from .docker_host import _atomic_bytes
from .receiver import _open_lock


TARGET = prior.TARGET
CONFIG = prior.CONFIG
BACKUP_ROOT = prior.BACKUP_ROOT
RECEIPT = Path("/etc/buh-platform-v2/ESI-SCHEMA-MIRROR-REPAIR.json")
FAILED_ATTEMPT = "gh-36318906058-1"
PREVIOUS_SHA256 = "7d6ee7885d743e624ffbbd0b4c216b2dd258336522e5651e984c5ea62336bc8f"
PINS = {
    **prior.PINS,
    prior.RECEIPT: "ce918ef49943ae6f80d2e57b4cb748711bceb7b2ded065c4fc6129fe1389614e",
    Path("/var/lib/buh-platform-v2/attempts") / f"{FAILED_ATTEMPT}.json":
        "42774c786245613a84132ecbfde96177ed77272bc79d3b2b25d2bc31aaead56f",
}


def _baseline() -> bytes:
    if os.geteuid() != 0:
        raise DeploymentError("Receiver follow-up requires root")
    if Path(__file__).with_name("docker_host.py") == TARGET:
        raise DeploymentError("Run only from separately staged reviewed source")
    for path, digest in PINS.items():
        prior._require_sha(path, digest)
    previous = prior._require_sha(TARGET, PREVIOUS_SHA256, mode="0o644")
    if read_file(Path("/var/lib/buh-platform-v2/active-recovery.json"))[1] is not None:
        raise DeploymentError("Active recovery returned")
    if RECEIPT.exists() or RECEIPT.is_symlink():
        raise DeploymentError("This one-time receiver follow-up already has a receipt")
    attempt_path = Path("/var/lib/buh-platform-v2/attempts") / f"{FAILED_ATTEMPT}.json"
    attempt = json.loads(prior._require_sha(attempt_path, PINS[attempt_path]))
    if attempt.get("result") != "failed" or attempt.get("rollback", {}).get("result") != "passed":
        raise DeploymentError("Second failed attempt rollback is not verified")
    first = json.loads(prior._require_sha(prior.RECEIPT, PINS[prior.RECEIPT]))
    if first.get("sha256") != PREVIOUS_SHA256 or first.get("deployment_performed") is not False:
        raise DeploymentError("First checker repair receipt changed")
    return previous


def run(*, mode: str, replacement_sha256: str, source_commit: str, confirmation: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", replacement_sha256):
        raise DeploymentError("Replacement hash is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise DeploymentError("Reviewed source commit is invalid")
    if mode == "install" and confirmation != f"INSTALL ESI SCHEMA MIRROR CHECKER {replacement_sha256}":
        raise DeploymentError("Exact receiver follow-up confirmation is required")
    if mode not in {"plan", "install"}:
        raise DeploymentError("Unknown receiver follow-up operation")
    staged = Path(__file__).with_name("docker_host.py")
    new = prior._require_sha(staged, replacement_sha256)
    compile(new, str(staged), "exec")
    if replacement_sha256 == PREVIOUS_SHA256:
        raise DeploymentError("Replacement is identical to installed receiver")
    config = ReceiverConfig.load(CONFIG)
    lock = _open_lock(config)
    try:
        previous = _baseline()
        prior._verify_backup_root()
        backup = BACKUP_ROOT / f"esi-schema-mirror-{replacement_sha256[:12]}"
        if backup.exists() or backup.is_symlink():
            raise DeploymentError("Receiver follow-up backup already exists")
        record = {
            "schema_version": 1,
            "result": "esi-schema-mirror-checker-repaired",
            "source_commit": source_commit,
            "path": str(TARGET),
            "previous_sha256": PREVIOUS_SHA256,
            "sha256": replacement_sha256,
            "backup_path": str(backup),
            "failed_attempt": FAILED_ATTEMPT,
            "parent_receipt_path": str(prior.RECEIPT),
            "parent_receipt_sha256": PINS[prior.RECEIPT],
            "deployment_performed": False,
        }
        if mode == "plan":
            return {**record, "result": "ready-to-install"}
        backup.mkdir(mode=0o700, exist_ok=False)
        _atomic_bytes(backup / "docker_host.py", previous, 0o600, owner=(0, 0))
        _atomic_bytes(backup / "replacement.py", new, 0o600, owner=(0, 0))
        prior._require_sha(backup / "docker_host.py", PREVIOUS_SHA256, mode="0o600")
        prior._require_sha(backup / "replacement.py", replacement_sha256, mode="0o600")
        _baseline()
        try:
            _atomic_bytes(TARGET, new, 0o644, owner=(0, 0))
            prior._require_sha(TARGET, replacement_sha256, mode="0o644")
            _atomic_bytes(
                RECEIPT, (json.dumps(record, sort_keys=True) + "\n").encode("ascii"),
                0o600, owner=(0, 0),
            )
        except BaseException:
            _atomic_bytes(TARGET, previous, 0o644, owner=(0, 0))
            prior._require_sha(TARGET, PREVIOUS_SHA256, mode="0o644")
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
