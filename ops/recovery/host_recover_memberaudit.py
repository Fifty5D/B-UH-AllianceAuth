#!/usr/bin/env python3
"""Host-side reviewed recovery, with durable private evidence and retry budgets.

Runs the adjacent source in a temporary directory inside a fresh Auth Python
process. Installed packages, mounted settings, containers, and services remain
unchanged. Snapshot evidence reaches durable host storage before apply begins.
"""

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from retry_budget import choose_retries


CONTAINER_BOOTSTRAP = r'''
import contextlib, importlib.util, io, json, logging, os, pathlib, sys, tempfile, types
logging.disable(logging.CRITICAL)
payload = json.loads(PAYLOAD)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myauth.settings.local')
import django
django.setup()
from django.core.management.base import OutputWrapper
with tempfile.TemporaryDirectory(prefix='buh-memberaudit-recovery-') as directory:
    root = pathlib.Path(directory)
    for name, source in payload['sources'].items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding='utf-8')
    def load(name, relative):
        spec = importlib.util.spec_from_file_location(name, root / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    recovery = load('buh_memberaudit_autoreg.recovery', 'recovery.py')
    command_module = load('buh_memberaudit_recovery_command', 'management/commands/buh_memberaudit_recover.py')
    output = io.StringIO()
    command = command_module.Command(stdout=OutputWrapper(output))
    snapshot_path = root / 'snapshot.json'
    command.handle(apply=False, snapshot=str(snapshot_path), limit=0,
                   expect_characters=None, expect_sections=None)
    preview = json.loads(output.getvalue())
    result = {'preview': preview, 'snapshot': json.loads(snapshot_path.read_text()), 'results': []}
    if payload['apply']:
        current = {row['character_pk']: set(row['sections']) for row in preview['results'] if row['status'] == 'planned'}
        for row in payload['selection']:
            if not set(row['sections']).issubset(current.get(row['character_pk'], set())):
                raise SystemExit('Recovery selection changed; preview again')
        result['results'] = [recovery.recover_character(row['character_pk'], apply=True, sections=row['sections'])
                             for row in payload['selection']]
    print(json.dumps(result, default=str))
'''


def private_json(path, data, *, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = path.with_name(path.name + f".tmp-{os.getpid()}") if replace else path
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(target, path)


def container_run(container, source_root, *, apply=False, selection=()):
    files = ("recovery.py", "management/commands/buh_memberaudit_recover.py")
    payload = json.dumps({
        "sources": {name: (source_root / name).read_text(encoding="utf-8") for name in files},
        "apply": apply,
        "selection": list(selection),
    })
    program = "PAYLOAD = " + repr(payload) + "\n" + CONTAINER_BOOTSTRAP
    completed = subprocess.run(
        ["docker", "exec", "-i", container, "python"],
        input=program, text=True, encoding="utf-8", capture_output=True, timeout=300,
    )
    if completed.returncode:
        # Do not forward raw application output or exceptions that may contain credentials.
        raise RuntimeError(f"Auth recovery process failed (exit {completed.returncode})")
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("Auth recovery process returned invalid safe metadata") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--periodic", action="store_true", help="Enforce persistent cooldown/attempt limits")
    parser.add_argument("--container", default="allianceauth_gunicorn")
    parser.add_argument("--snapshot-dir", default="/var/lib/buh-auth-recovery/memberaudit")
    parser.add_argument("--expect-characters", type=int)
    parser.add_argument("--expect-sections", type=int)
    parser.add_argument("--cooldown", type=int, default=3600)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--limit", type=int, default=50, help="Maximum sections per periodic run")
    options = parser.parse_args(argv)
    if options.cooldown < 300 or not 1 <= options.max_attempts <= 10 or not 1 <= options.limit <= 100:
        parser.error("cooldown >=300, max-attempts 1..10, and limit 1..100 required")
    tool_root = Path(__file__).resolve().parent
    source_root = tool_root / "src" / "buh_memberaudit_autoreg"
    if not source_root.is_dir():
        source_root = (
            tool_root.parents[1] / "apps" / "memberaudit-autoreg"
            / "src" / "buh_memberaudit_autoreg"
        )
    evidence = Path(options.snapshot_dir)
    evidence.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = evidence / "recovery.lock"
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = container_run(options.container, source_root)
        plans = [row for row in before["preview"]["results"] if row["status"] == "planned"]
        summary = before["preview"]["summary"]
        for field, expected in (("actionable_characters", options.expect_characters),
                                ("actionable_sections", options.expect_sections)):
            if expected is not None and summary[field] != expected:
                raise RuntimeError(f"{field} changed: expected {expected}, found {summary[field]}")
        if not options.apply:
            print(json.dumps(before["preview"], indent=2))
            return 0
        state_path = evidence / "retry-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        selection, next_state = choose_retries(
            plans, state, now=time.time(), cooldown=options.cooldown,
            max_attempts=options.max_attempts, limit=options.limit,
            override=not options.periodic,
        )
        if not selection:
            private_json(state_path, next_state, replace=True)
            print(json.dumps({"summary": summary, "scheduled": 0, "reason": "no_budgeted_retries"}))
            return 0
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        prefix = evidence / stamp
        private_json(str(prefix) + ".before.json", before["snapshot"])
        # Persist attempt reservations before dispatch so interruption cannot create a loop.
        private_json(state_path, next_state, replace=True)
        result = container_run(options.container, source_root, apply=True, selection=selection)
        private_json(str(prefix) + ".result.json", result)
        failures = [row for row in result["results"] if row["status"] not in
                    {"queued", "already_queued", "no_failed_sections"}]
        print(json.dumps({"summary": summary, "results": result["results"],
                          "snapshot": str(prefix) + ".before.json"}, indent=2))
        return 2 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Recovery stopped: {exc}", file=sys.stderr)
        sys.exit(1)
