#!/usr/bin/env python3
"""Bounded, read-only owner evidence for gh-34713182347-1. Never runs recovery."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

ATTEMPT = "gh-34713182347-1"
STATE = Path("/var/lib/buh-platform-v2")
BACKUP = Path("/var/backups/buh-platform-v2") / ATTEMPT
LIBRARY = Path("/usr/local/lib/buh-platform-v2")
MAX_BYTES = 1024 * 1024


def read_file(path: Path, *, maximum: int = MAX_BYTES) -> tuple[dict, bytes | None]:
    """No following symlinks; content never appears in output by default."""
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe evidence ancestor")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"present": False}, None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe evidence file")
        record = {
            "present": True,
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mode": oct(stat.S_IMODE(info.st_mode)),
            "size": info.st_size,
        }
        if info.st_size > maximum:
            record["content"] = "not-read-size-bound"
            return record, None
        data = stream.read(maximum + 1)
        if len(data) != info.st_size:
            raise ValueError("evidence changed while reading")
        record["sha256"] = hashlib.sha256(data).hexdigest()
        return record, data


def collect() -> dict:
    if os.geteuid() != 0:
        raise ValueError("owner/root read-only collection is required")
    fixed = (
        Path("/etc/buh-platform-v2/receiver.json"),
        Path("/etc/buh-platform-v2/INSTALL.json"),
        LIBRARY / "ops/deploy/docker_host.py",
        LIBRARY / "ops/deploy/receiver.py",
        LIBRARY / "ops/deploy/engine.py",
        STATE / "active-recovery.json",
        STATE / "current.json",
        STATE / "attempts" / f"{ATTEMPT}.json",
        BACKUP / "RECOVERY.json",
        BACKUP / "BACKUP.json",
        Path("/opt/aa-docker/conf/buh-platform-v2/nginx/upstream.conf"),
    )
    files = {}
    for path in fixed:
        record, raw = read_file(path)
        files[str(path)] = record
        if raw is None:
            continue
        if path.name == "INSTALL.json":
            value = json.loads(raw).get("source_commit", "")
            if re.fullmatch(r"[0-9a-f]{40}", value):
                record["source_commit"] = value
        if path in {STATE / "active-recovery.json", BACKUP / "RECOVERY.json"}:
            value = json.loads(raw)
            if value.get("attempt_id") != ATTEMPT or value.get("status") != "active":
                raise ValueError("active recovery attempt identity changed")
            if value.get("backup_path") != str(BACKUP):
                raise ValueError("retained backup identity changed")
            flags = value.get("flags")
            if not isinstance(flags, dict) or any(
                type(item) is not bool for item in flags.values()
            ):
                raise ValueError("recovery flags are malformed")
            record["flags"] = flags
            record["phase"] = (
                value["phase"]
                if re.fullmatch(r"[a-z0-9-]{1,64}", value["phase"])
                else "invalid"
            )
            record["backup_path"] = str(BACKUP)
            for field in ("candidate_web_slots", "previous_web_slots"):
                slots = value.get(field)
                if (
                    not isinstance(slots, list)
                    or len(slots) > 8
                    or any(
                        not isinstance(slot, str)
                        or not re.fullmatch(
                            rf"buh-web-(candidate|previous)-{ATTEMPT}-[1-8]", slot
                        )
                        for slot in slots
                    )
                ):
                    raise ValueError("retained web slot identity changed")
                record[field] = slots
    # Names/metadata/hashes only. Never print local.py, SQL, .env or backup bytes.
    backups = {}
    entries = sorted(BACKUP.iterdir())
    if len(entries) > 24:
        raise ValueError("backup directory exceeds entry bound")
    for path in entries:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", path.name):
            raise ValueError("backup entry name is unsafe")
        backups[path.name] = read_file(path)[0]
    return {
        "schema_version": 1,
        "attempt_id": ATTEMPT,
        "read_only": True,
        "files": files,
        "backup_files": backups,
        "warning": "This inventory is not verified rollback or cleanup authorization.",
    }


def main() -> int:
    try:
        result = collect()
        encoded = json.dumps(result, sort_keys=True)
        if len(encoded) > 32 * 1024:
            raise ValueError("report exceeds output bound")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        # No paths, file contents, subprocess output or exception values.
        print(
            json.dumps(
                {
                    "read_only": True,
                    "result": "incomplete",
                    "error_type": type(exc).__name__,
                }
            )
        )
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
