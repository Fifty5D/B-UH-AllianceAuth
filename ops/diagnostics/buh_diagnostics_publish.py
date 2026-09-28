#!/usr/bin/env python3
"""Publish only redacted, retained diagnostics to a private Git branch."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import zlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

HISTORY = Path(os.environ.get("BUH_DIAGNOSTICS_DIR", "/var/lib/buh-diagnostics"))
STAGING = Path(os.environ.get("BUH_DIAGNOSTICS_PUBLISH_DIR", "/var/lib/buh-diagnostics-publish"))
REMOTE = os.environ.get("BUH_DIAGNOSTICS_REMOTE", "git@github.com:Fifty5D/B-UH-Diagnostics.git")
KEY = Path(os.environ.get("BUH_DIAGNOSTICS_DEPLOY_KEY", "/etc/buh-diagnostics/deploy-key"))
KNOWN_HOSTS = Path(os.environ.get("BUH_DIAGNOSTICS_KNOWN_HOSTS", "/etc/buh-diagnostics/known_hosts"))
MAX_PART_BYTES = 384 * 1024
MINIMUM_FREE_BYTES = 25 * 1024**3
RETENTION = timedelta(days=30)


def utcnow():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp needs a timezone")
    return parsed.astimezone(UTC)


def hour_start(value):
    return value.replace(minute=0, second=0, microsecond=0)


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".buh-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_hour(db, repo, hour, cutoff=None):
    if shutil.disk_usage(repo).free < MINIMUM_FREE_BYTES:
        raise RuntimeError("Private evidence export paused by free-space guard")
    path = repo / "evidence" / hour.strftime("%Y-%m-%d") / hour.strftime("%H")
    path.mkdir(parents=True, exist_ok=True)
    for previous in path.glob("part-*.jsonl"):
        previous.unlink()
    part, size, count = 0, 0, 0
    output = None
    services = Counter()
    errors = Counter()
    first = last = None
    try:
        for source, service, at, blob, error_key in db.execute(
            "SELECT source,service,at,message,error_key FROM logs "
            "WHERE at>=? AND at<? ORDER BY at,id",
            (stamp(max(hour, cutoff)) if cutoff else stamp(hour),
             stamp(hour + timedelta(hours=1))),
        ):
            record = {"at": at, "source": source, "service": service,
                      "message": zlib.decompress(blob).decode("utf-8")}
            line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
            if len(line) > MAX_PART_BYTES:
                raise RuntimeError("Stored log record exceeds private evidence shard limit")
            if output is None or size + len(line) > MAX_PART_BYTES:
                if output is not None:
                    output.close()
                output = (path / f"part-{part:03d}.jsonl").open("wb")
                part += 1
                size = 0
            output.write(line)
            size += len(line)
            count += 1
            services[service] += 1
            if error_key:
                errors[service] += 1
            first = first or at
            last = at
    finally:
        if output is not None:
            output.close()
    if count == 0:
        (path / "index.json").unlink(missing_ok=True)
        path.rmdir()
        return None
    summary = {
        "from_utc": stamp(hour), "to_utc": stamp(hour + timedelta(hours=1)),
        "first_at": first, "last_at": last, "records": count,
        "services": dict(sorted(services.items())),
        "errors_by_service": dict(sorted(errors.items())),
        "parts": [f"evidence/{hour:%Y-%m-%d}/{hour:%H}/part-{i:03d}.jsonl"
                  for i in range(part)],
    }
    atomic_json(path / "index.json", summary)
    return summary


def prune_hours(repo, cutoff):
    evidence = repo / "evidence"
    if not evidence.exists():
        return False
    threshold = hour_start(cutoff)
    removed = False
    for day in evidence.iterdir():
        if not day.is_dir():
            continue
        for hour in day.iterdir():
            if not hour.is_dir():
                continue
            try:
                when = datetime.strptime(day.name + hour.name, "%Y-%m-%d%H").replace(
                    tzinfo=UTC
                )
            except ValueError:
                raise RuntimeError("Unexpected diagnostics evidence path") from None
            if when < threshold:
                if not hour.resolve().is_relative_to(evidence.resolve()):
                    raise RuntimeError("Unsafe diagnostics evidence path")
                shutil.rmtree(hour)
                removed = True
        if not any(day.iterdir()):
            day.rmdir()
    return removed


def build_index(repo, generated):
    days = {}
    for path in sorted((repo / "evidence").glob("*/??/index.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))
        day = path.parent.parent.name
        current = days.setdefault(day, {"index": f"evidence/{day}/index.json",
                                        "records": 0, "first_at": None,
                                        "last_at": None, "hours": []})
        current["records"] += summary["records"]
        current["first_at"] = current["first_at"] or summary["first_at"]
        current["last_at"] = summary["last_at"] or current["last_at"]
        current["hours"].append(path.parent.name)
    for day, value in days.items():
        hours = [json.loads((repo / "evidence" / day / hour / "index.json").read_text())
                 for hour in value.pop("hours")]
        atomic_json(repo / "evidence" / day / "index.json", {"date_utc": day, "hours": hours})
    atomic_json(repo / "evidence-index.json", {"schema_version": 1,
                "updated_at": stamp(generated), "retention_days": 30,
                "days": days})


def git_command(repo, *args, env=None, timeout=180):
    result = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Private diagnostics Git transport failed: " + " ".join(args[:2]))
    return result.stdout.strip()


def push_snapshot(repo, *, clean_objects):
    if not (repo / ".git").is_dir():
        git_command(repo, "init", "-q")
    git_command(repo, "add", "-A")
    tree = git_command(repo, "write-tree")
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": "B-UH diagnostics", "GIT_AUTHOR_EMAIL": "diagnostics@b-uh.invalid",
                "GIT_COMMITTER_NAME": "B-UH diagnostics",
                "GIT_COMMITTER_EMAIL": "diagnostics@b-uh.invalid"})
    commit = git_command(repo, "commit-tree", tree, "-m", "Update private redacted diagnostics", env=env)
    if REMOTE.startswith("git@"):
        if not KEY.is_file() or not KNOWN_HOSTS.is_file():
            raise RuntimeError("Restricted deploy key or pinned host key is missing")
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {shlex.quote(KEY.as_posix())} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={shlex.quote(KNOWN_HOSTS.as_posix())} "
            f"-o BatchMode=yes"
        )
    git_command(repo, "push", "--force", REMOTE, f"{commit}:refs/heads/data", env=env)
    # The pushed commit is an orphan snapshot. Retain only the current local
    # snapshot; old commits and payloads must not build up in the host cache.
    git_command(repo, "update-ref", "refs/heads/data", commit)
    if clean_objects:
        git_command(repo, "reflog", "expire", "--expire=now", "--all")
        git_command(repo, "gc", "--prune=now", "--quiet", timeout=900)


def insertion_hours(db, after_id, cutoff, generated):
    return {
        datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=UTC)
        for (value,) in db.execute(
            "SELECT DISTINCT substr(at,1,13) FROM logs "
            "WHERE id>? AND at>=? AND at<=?",
            (after_id, stamp(cutoff), stamp(generated)),
        )
    }


def publish():
    generated = utcnow()
    report_path = HISTORY / "buh-diagnostics-latest.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1:
        raise RuntimeError("Collector report has an unsupported schema")
    latest_at = parse_stamp(report["generated_at"])
    report["report_stale"] = bool(report.get("report_stale")) or generated - latest_at > timedelta(minutes=10)
    if report["report_stale"] and report.get("status") != "not_installed":
        report["status"] = "unknown"
    report["published_at"] = stamp(generated)
    report["evidence"] = {
        "index": "evidence-index.json", "ref": "data",
        "format": "redacted JSONL shards grouped by UTC date and hour",
    }
    STAGING.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(STAGING).free < MINIMUM_FREE_BYTES:
        raise RuntimeError("Private evidence publication paused by free-space guard")
    repo = STAGING / "repo"
    repo.mkdir(mode=0o700, exist_ok=True)
    state = STAGING / "last-published.json"
    db = sqlite3.connect(f"file:{(HISTORY / 'history.sqlite3').as_posix()}?mode=ro", uri=True)
    try:
        cutoff = generated - RETENTION
        oldest = db.execute("SELECT MIN(at) FROM logs WHERE at>=?",
                            (stamp(cutoff),)).fetchone()[0]
        sequence = db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='logs'"
        ).fetchone()
        last_log_id = sequence[0] if sequence else 0
        store = db.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()
        if not store:
            raise RuntimeError("Diagnostics store identity is missing")
        store_id = store[0]
        previous_state = json.loads(state.read_text()) if state.exists() else {}
        old_id = previous_state.get("last_log_id")
        rebuild = (not isinstance(old_id, int) or old_id > last_log_id or
                   previous_state.get("store_id") != store_id)
        if rebuild:
            evidence = repo / "evidence"
            if evidence.is_symlink():
                raise RuntimeError("Unsafe diagnostics evidence path")
            if evidence.exists():
                shutil.rmtree(evidence)
            earliest = max(cutoff, parse_stamp(oldest)) if oldest else generated
        else:
            previous = parse_stamp(previous_state["at"])
            earliest = max(cutoff, previous - timedelta(hours=1))
        # The boundary hour is rewritten each run so rolling retention cannot
        # leave old records visible in the private mirror.
        start = hour_start(earliest)
        boundary = hour_start(cutoff)
        hours = {boundary}
        hour = start
        while hour <= generated:
            hours.add(hour)
            hour += timedelta(hours=1)
        if not rebuild:
            hours.update(insertion_hours(db, old_id, cutoff, generated))
        for hour in sorted(hours):
            export_hour(db, repo, hour, cutoff)
    finally:
        db.close()
    removed_hours = prune_hours(repo, generated - RETENTION)
    build_index(repo, generated)
    atomic_json(repo / "buh-diagnostics-latest.json", report)
    last_gc = previous_state.get("last_gc_at")
    clean_objects = (removed_hours or not last_gc or
                     generated - parse_stamp(last_gc) >= timedelta(days=1))
    push_snapshot(repo, clean_objects=clean_objects)
    atomic_json(state, {"at": stamp(generated), "last_log_id": last_log_id,
                        "store_id": store_id,
                        "last_gc_at": stamp(generated) if clean_objects else last_gc})


if __name__ == "__main__":
    publish()
