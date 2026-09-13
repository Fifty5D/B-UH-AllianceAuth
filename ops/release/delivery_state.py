"""One read-only delivery status; publication is never installed-state evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

REPOSITORY = "Fifty5D/B-UH-AllianceAuth"
HOLD_PATH = "ops/release/recovery-hold.json"
LEGACY_HOLD_PATH = "ops/release/published-release-recovery-v0.6.2.json"


def status(root: Path) -> dict:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=10
    ).strip()
    hold = None
    path = root / HOLD_PATH
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            raise ValueError("Recovery hold is not a bounded regular file")
        hold = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(hold, dict)
            or set(hold) != {"schema_version", "repository", "state", "attempt_id",
                            "target_release", "reason", "next_action"}
            or hold["schema_version"] != 1
            or hold["repository"] != REPOSITORY
            or hold["state"] != "active"
            or not isinstance(hold["attempt_id"], str)
            or not re.fullmatch(r"gh-[0-9]+-[0-9]+", hold["attempt_id"])
            or not isinstance(hold["target_release"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", hold["target_release"])
            or any(not isinstance(hold[k], str) or not 1 <= len(hold[k]) <= 500
                   or any(ord(c) < 32 for c in hold[k]) for k in ("reason", "next_action"))
        ):
            raise ValueError("Recovery hold is invalid; production remains blocked")
    elif (root / LEGACY_HOLD_PATH).exists():
        hold = {"state": "active", "reason": "Historical recovery lock remains active.",
                "next_action": "Review and complete recovery before retiring its lock."}
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "source_commit": commit,
        "feature_work": "allowed",
        "release_preparation": "held" if hold else "eligible-after-required-checks",
        "production": "blocked-by-recovery" if hold else "requires-exact-production-approval",
        "installed_release": None,
        "installed_release_authority": "Verified host current pointer and transaction journal; never inferred from GitHub",
        "recovery_hold": hold,
        "next_action": hold["next_action"] if hold else "Review the qualified candidate and request one exact production approval.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--require-clear", action="store_true")
    args = parser.parse_args()
    try:
        report = status(args.root)
        encoded = json.dumps(report, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        else:
            print(encoded, end="")
        if args.summary:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n### Delivery status\n\n")
                stream.write(f"Source: `{report['source_commit']}`\n\n")
                stream.write(f"Feature work: **{report['feature_work']}**. Production: **{report['production']}**.\n\n")
                stream.write(report["next_action"] + "\n")
                stream.write("\nPublished releases do not establish the installed production version.\n")
        return 2 if args.require_clear and report["recovery_hold"] else 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print("Delivery state is unavailable or invalid; production remains blocked.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
