#!/usr/bin/env python3
"""Independent, private B-UH log store and compact health report.

The collector only reads Docker, journald, and Django metadata. Its SQLite
database survives container replacement and deployments. No network listener
or Auth/Celery task is needed to serve the stored report.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import zlib
import uuid
from datetime import UTC, date, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(os.environ.get("BUH_DIAGNOSTICS_DIR", "/var/lib/buh-diagnostics"))
REDACTOR = Path(
    os.environ.get("BUH_DIAGNOSTICS_REDACTOR", "/usr/local/libexec/buh-redact-diagnostics")
)
RELEASE = Path("/var/lib/buh-platform-v2/current.json")
RETENTION = timedelta(days=30)
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_LOG_BATCH_BYTES = 512 * 1024 * 1024
MAX_MESSAGE_BYTES = 16 * 1024
MINIMUM_FREE_BYTES = 25 * 1024**3
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s(.*)$", re.S)
ERROR = re.compile(r"(?i)\b(error|exception|traceback|failed|fatal|oom|out of memory)\b")
VARIABLE = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f-]{27,}|\d{4,}|0x[0-9a-f]+)\b", re.I)
EXPECTED_SERVICES_FILE = Path(
    os.environ.get("BUH_DIAGNOSTICS_EXPECTED_SERVICES", "/etc/buh-diagnostics/expected-services.json")
)
AUTH_SERVICES = frozenset(("allianceauth_gunicorn", "allianceauth_worker",
                           "allianceauth_worker_services", "allianceauth_beat"))
INFRASTRUCTURE_SERVICES = frozenset(("auth_mysql", "redis", "nginx", "proxy", "grafana"))
APP_MARKER = "BUH_DIAGNOSTICS_JSON:"

APP_PROBE = r'''
import importlib.metadata
import json
from datetime import timedelta
from django.db.models import Count, Max
from django.utils.timezone import now
from django_celery_beat.models import PeriodicTask
from buh_max_history.models import (
    ArchiveCollectionTarget, ArchiveJob, PublicCatalogIndex, PublicArchiveFile,
    PublicDataset,
)
from buh_moon_tax.models import AuditRun
from buh_structure_ops.models import StructureSnapshot

names = ("allianceauth", "Django", "django-esi", "aa-memberaudit", "aa-structures",
         "aa-moonmining", "aa-buh-max-history", "aa-buh-moon-tax",
         "aa-buh-structure-ops", "aa-buh-mining-analytics")
versions = {}
for name in names:
    try:
        versions[name] = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        versions[name] = None
scheduled = []
units = {"microseconds": 0.000001, "seconds": 1, "minutes": 60,
         "hours": 3600, "days": 86400}
for task in PeriodicTask.objects.filter(enabled=True).order_by("name").values(
        "name", "task", "last_run_at", "total_run_count",
        "interval__every", "interval__period"):
    if "buh" in task["name"].lower() or "buh" in task["task"].lower():
        cadence = (task["interval__every"] * units[task["interval__period"]]
                   if task["interval__period"] in units and task["interval__every"]
                   else None)
        lag = (now() - task["last_run_at"]).total_seconds() if task["last_run_at"] else None
        scheduled.append({"name": task["name"], "task": task["task"],
                          "last_run_at": task["last_run_at"].isoformat()
                          if task["last_run_at"] else None,
                          "total_run_count": task["total_run_count"],
                          "cadence_seconds": cadence,
                          "overdue": lag > max(2 * cadence, cadence + 600)
                          if lag is not None and cadence else None,
                          "never_run": task["last_run_at"] is None})
jobs = []
for job in ArchiveJob.objects.order_by("-requested_at").values(
        "kind", "status", "requested_at", "finished_at", "result")[:12]:
    result = job["result"] if isinstance(job["result"], dict) else {}
    catalog = result.get("catalog") if isinstance(result.get("catalog"), dict) else {}
    jobs.append({"kind": job["kind"], "status": job["status"],
                 "requested_at": job["requested_at"].isoformat(),
                 "finished_at": job["finished_at"].isoformat()
                 if job["finished_at"] else None,
                 "downloaded_files": result.get("downloaded_files"),
                 "errors": len(result.get("errors") or []),
                 "provider_gaps": sum(x.get("unavailable_indexes", 0)
                                      for x in catalog.get("datasets", [])
                                      if isinstance(x, dict))})
last_archive_success = {}
for kind in ("CATALOG", "SYNC", "VERIFY"):
    value = ArchiveJob.objects.filter(kind=kind, status="SUCCEEDED").aggregate(
        value=Max("finished_at"))["value"]
    last_archive_success[kind] = value.isoformat() if value else None
counts = dict(PublicArchiveFile.objects.values("status").annotate(n=Count("pk"))
              .values_list("status", "n"))
capture_targets = {
    "status_counts": dict(ArchiveCollectionTarget.objects.values("status")
                          .annotate(n=Count("pk")).values_list("status", "n")),
    "last_success_at": ArchiveCollectionTarget.objects.aggregate(
        value=Max("last_success_at"))["value"],
    "last_attempt_at": ArchiveCollectionTarget.objects.aggregate(
        value=Max("last_attempt_at"))["value"],
    "due_failed": ArchiveCollectionTarget.objects.filter(
        status="failed", next_attempt_at__lt=now()).count(),
}
for key in ("last_success_at", "last_attempt_at"):
    value = capture_targets[key]
    capture_targets[key] = value.isoformat() if value else None
recent_audits = [
    {"status": row["status"], "queued_at": row["queued_at"].isoformat(),
     "finished_at": row["finished_at"].isoformat()
     if row["finished_at"] else None}
    for row in AuditRun.objects.order_by("-queued_at").values(
        "status", "queued_at", "finished_at")[:8]
]
moon_tax_last_complete = AuditRun.objects.filter(status="COMPLETE").aggregate(
    value=Max("finished_at"))["value"]
moon_tax_last_failed = AuditRun.objects.filter(status="FAILED").aggregate(
    value=Max("finished_at"))["value"]
last_structure = StructureSnapshot.objects.aggregate(value=Max("captured_at"))["value"]
datasets = [
    {"slug": row["slug"],
     "last_sync_at": row["last_sync_at"].isoformat() if row["last_sync_at"] else None,
     "last_catalog_at": row["last_catalog_at"].isoformat()
     if row["last_catalog_at"] else None}
    for row in PublicDataset.objects.filter(enabled=True).order_by("slug").values(
        "slug", "last_sync_at", "last_catalog_at")[:50]
]
payload = {"at": now().isoformat(), "versions": versions, "scheduled": scheduled,
           "archive_jobs": jobs, "archive_files": counts,
           "archive_last_success_at": last_archive_success,
           "archive_capture_targets": capture_targets,
           "public_datasets": datasets,
           "moon_tax_recent_audits": recent_audits,
           "moon_tax_last_complete_at": moon_tax_last_complete.isoformat()
           if moon_tax_last_complete else None,
           "moon_tax_last_failed_at": moon_tax_last_failed.isoformat()
           if moon_tax_last_failed else None,
           "structure_last_snapshot_at": last_structure.isoformat()
           if last_structure else None,
           "unavailable_indexes": PublicCatalogIndex.objects.filter(
               status="UNAVAILABLE").count(),
           "stalled_jobs": ArchiveJob.objects.filter(
               status="RUNNING", requested_at__lt=now()-timedelta(minutes=35)).count()}
try:
    from django_celery_results.models import TaskResult
    payload["task_results"] = [
        {"task": row["task_name"], "status": row["status"], "count": row["n"],
         "last_at": row["last_at"].isoformat() if row["last_at"] else None}
        for row in TaskResult.objects.filter(date_done__gte=now()-timedelta(hours=24))
        .values("task_name", "status").annotate(n=Count("pk"), last_at=Max("date_done"))
        .order_by("task_name", "status")[:100]
    ]
    payload["task_results_note"] = (
        "No task-result rows in the last 24 hours; use application records and worker logs."
        if not payload["task_results"] else None
    )
except (ImportError, LookupError):
    payload["task_results"] = None
    payload["task_results_note"] = "Task-result model unavailable."
print("BUH_DIAGNOSTICS_JSON:" + json.dumps(payload, sort_keys=True))
'''


def utcnow() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("A timezone is required")
    return parsed.astimezone(UTC)


def redactor():
    loader = SourceFileLoader("buh_diagnostics_redactor", str(REDACTOR))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError("Redactor is unavailable")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def database():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    path = ROOT / "history.sqlite3"
    connection = sqlite3.connect(path, timeout=15)
    os.chmod(path, 0o600)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=15000")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, service TEXT NOT NULL,
            at TEXT NOT NULL, message BLOB NOT NULL, error_key TEXT,
            fingerprint TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS logs_time_service ON logs(at, service);
        CREATE INDEX IF NOT EXISTS logs_error_time ON logs(error_key, at);
        CREATE TABLE IF NOT EXISTS gaps (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, source TEXT NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resources (
            at TEXT PRIMARY KEY, free_bytes INTEGER NOT NULL,
            available_memory_bytes INTEGER, load_1m REAL
        );
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    if get_meta(connection, "store_id") is None:
        set_meta(connection, "store_id", uuid.uuid4().hex)
    connection.commit()
    return connection


def readonly_database():
    path = ROOT / "history.sqlite3"
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute(
        "INSERT INTO metadata(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value))
    )


def gap(db, source, reason):
    db.execute(
        "INSERT INTO gaps(at,source,reason) VALUES (?,?,?)",
        (stamp(utcnow()), source, reason[:250]),
    )


def insert_log(db, source, service, at, message, redact):
    encoded = message.encode("utf-8", errors="replace")
    if len(encoded) > MAX_MESSAGE_BYTES:
        message = encoded[:MAX_MESSAGE_BYTES].decode("utf-8", errors="replace")
        gap(db, source, "Oversized log record truncated")
    cleaned = redact.redact(message)
    if redact.contains_credential_shape(cleaned):
        cleaned = "[Credential-shaped log record omitted]"
        gap(db, source, "Credential-shaped log record omitted")
    key = hashlib.sha256(f"{source}\0{service}\0{at}\0{cleaned}".encode()).hexdigest()
    error_key = None
    if ERROR.search(cleaned):
        error_key = VARIABLE.sub("<variable>", cleaned.lower())[:250]
    db.execute(
        "INSERT OR IGNORE INTO logs(source,service,at,message,error_key,fingerprint) "
        "VALUES (?,?,?,?,?,?)",
        (source, service, at, zlib.compress(cleaned.encode()), error_key, key),
    )


def read_command(argv, *, input_text=None, timeout=120):
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    expired = threading.Event()

    def stop():
        if process.poll() is None:
            expired.set()
            process.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    try:
        if input_text is not None:
            process.stdin.write(input_text.encode())
            process.stdin.close()
        output = process.stdout.read(MAX_SOURCE_BYTES + 1)
        if len(output) > MAX_SOURCE_BYTES:
            process.kill()
            raise RuntimeError(f"{argv[0]} output exceeded the collection limit")
        process.wait()
        if expired.is_set():
            raise subprocess.TimeoutExpired(argv, timeout)
        if process.returncode:
            raise RuntimeError(f"{argv[0]} exited {process.returncode}")
        return output.decode("utf-8", errors="replace")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()


def iter_command_lines(argv, *, timeout=300):
    """Stream a bounded source without holding its retained history in RAM."""
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    expired = threading.Event()

    def stop():
        if process.poll() is None:
            expired.set()
            process.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    size = 0
    try:
        for raw in process.stdout:
            size += len(raw)
            if size > MAX_LOG_BATCH_BYTES:
                raise RuntimeError(f"{argv[0]} log batch exceeded 512 MiB")
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
        process.wait()
        if expired.is_set():
            raise subprocess.TimeoutExpired(argv, timeout)
        if process.returncode:
            raise RuntimeError(f"{argv[0]} exited {process.returncode}")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()


def docker_containers():
    expected = expected_services()
    lines = read_command(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=25)
    candidates = []
    for line in lines.splitlines():
        item = json.loads(line)
        name = item.get("Names", "")
        # Inspect Compose labels rather than guessing the name of the beat
        # container. Include exact legacy names while they are still in use.
        if name in expected["services"] or name.startswith(expected["compose_project"] + "-"):
            candidates.append({"id": item["ID"], "name": name,
                               "state": item.get("State"), "status": item.get("Status")})
    result = []
    if candidates:
        details = read_command(
            ["docker", "inspect", "--format",
             "{{json .Config.Labels}}|{{json .State}}|{{.RestartCount}}",
             *(item["id"] for item in candidates)], timeout=25,
        ).splitlines()
        if len(details) != len(candidates):
            raise RuntimeError("Docker inspection omitted a configured container")
        for item, line in zip(candidates, details):
            state_text, separator, restart_text = line.rpartition("|")
            if not separator:
                raise RuntimeError("Docker inspection returned malformed state")
            labels_text, separator, state_text = state_text.rpartition("|")
            if not separator:
                raise RuntimeError("Docker inspection returned malformed labels")
            labels = json.loads(labels_text) or {}
            service = labels.get("com.docker.compose.service")
            project = labels.get("com.docker.compose.project")
            if project != expected["compose_project"] or service not in expected["services"]:
                if project is not None or item["name"] not in expected["services"]:
                    continue
                service = item["name"]
            state = json.loads(state_text)
            health = state.get("Health") or {}
            item.update({
                "service": service,
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "oom_killed": bool(state.get("OOMKilled")),
                "exit_code": state.get("ExitCode"),
                "health": health.get("Status"),
                "restarts": int(restart_text),
            })
            result.append(item)
    return result


def expected_services():
    value = json.loads(EXPECTED_SERVICES_FILE.read_text(encoding="utf-8"))
    services = value.get("services")
    project = value.get("compose_project")
    if (value.get("schema_version") != 1 or not isinstance(project, str)
            or re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", project) is None
            or not isinstance(services, dict)
            or not AUTH_SERVICES <= services.keys()
            or not {"auth_mysql", "redis", "nginx"} <= services.keys()
            or not services.keys() <= AUTH_SERVICES | INFRASTRUCTURE_SERVICES
            or any(type(count) is not int or not 1 <= count <= 128
                   for count in services.values())):
        raise ValueError("Invalid diagnostics service inventory contract")
    return value


def collect_docker(db, containers, redact, end):
    seen = set()
    for container in containers:
        name, identity = container["name"], container["id"]
        seen.add(identity)
        key = "docker:" + identity
        since = get_meta(db, key, stamp(end - RETENTION))
        try:
            lines = iter_command_lines(
                ["docker", "logs", "--timestamps", "--since", since,
                 "--until", stamp(end), identity], timeout=300,
            )
            for line in lines:
                match = STAMP.match(line)
                if match:
                    insert_log(db, "docker", name, match[1], match[2], redact)
                elif line.strip():
                    gap(db, name, "Docker returned an unparseable timestamped line")
            set_meta(db, key, stamp(end - timedelta(seconds=2)))
            set_meta(db, "source:docker:last_success", stamp(end))
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            gap(db, name, "Docker log capture failed: " + type(exc).__name__)
    previous = set(json.loads(get_meta(db, "docker:seen", "[]")))
    for identity in previous - seen:
        gap(db, "docker", "Container disappeared before its final log read: " + identity[:16])
    set_meta(db, "docker:seen", json.dumps(sorted(seen)))


def collect_journal(db, redact, end):
    since = get_meta(db, "journal:cursor", stamp(end - RETENTION))
    commands = (
        ["journalctl", "--since", since, "--until", stamp(end),
         "--no-pager", "-o", "json", "-p", "warning..emerg"],
        ["journalctl", "--since", since, "--until", stamp(end),
         "--no-pager", "-o", "json", "-u", "docker.service", "-u", "containerd.service"],
    )
    try:
        for command in commands:
            for line in iter_command_lines(command, timeout=300):
                item = json.loads(line)
                microseconds = int(item["__REALTIME_TIMESTAMP"])
                at = stamp(datetime.fromtimestamp(microseconds / 1_000_000, UTC))
                service = str(item.get("_SYSTEMD_UNIT") or item.get("SYSLOG_IDENTIFIER")
                              or "host")[:100]
                message = item.get("MESSAGE", "")
                if isinstance(message, str):
                    insert_log(db, "journal", service, at, message, redact)
        set_meta(db, "journal:cursor", stamp(end - timedelta(seconds=2)))
        set_meta(db, "source:journal:last_success", stamp(end))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        gap(db, "journal", "Journal capture failed: " + type(exc).__name__)


def collect_events(db, redact, end):
    since = get_meta(db, "events:cursor", stamp(end - RETENTION))
    try:
        expected = expected_services()
        lines = iter_command_lines(
            ["docker", "events", "--since", since, "--until", stamp(end),
             "--filter", "type=container", "--format", "{{json .}}"], timeout=300,
        )
        for line in lines:
            event = json.loads(line)
            when = datetime.fromtimestamp(int(event.get("timeNano", 0)) / 1e9, UTC)
            actor = event.get("Actor") or {}
            attributes = actor.get("Attributes") or {}
            name = str(attributes.get("name") or "container")[:100]
            if name in expected["services"] or (
                    attributes.get("com.docker.compose.project") == expected["compose_project"]
                    and attributes.get("com.docker.compose.service") in expected["services"]):
                insert_log(db, "docker-event", name, stamp(when),
                           str(event.get("Action") or "unknown"), redact)
        set_meta(db, "events:cursor", stamp(end - timedelta(seconds=2)))
        set_meta(db, "source:docker-event:last_success", stamp(end))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        gap(db, "docker-event", "Docker event capture failed: " + type(exc).__name__)


def collect_app(db, end):
    try:
        output = read_command(
            ["docker", "exec", "-i", "allianceauth_gunicorn", "python", "-B",
             "manage.py", "shell"], input_text=APP_PROBE, timeout=45,
        )
        line = next(line for line in reversed(output.splitlines())
                    if line.startswith(APP_MARKER))
        data = json.loads(line[len(APP_MARKER):])
        if not isinstance(data, dict) or not isinstance(data.get("versions"), dict):
            raise ValueError("Invalid application probe")
        set_meta(db, "app:probe", json.dumps(data, sort_keys=True))
        set_meta(db, "source:app:last_success", stamp(end))
    except (OSError, ValueError, RuntimeError, StopIteration,
            subprocess.TimeoutExpired) as exc:
        gap(db, "app", "Application metadata probe failed: " + type(exc).__name__)


def collect_resources(db, end):
    free = shutil.disk_usage("/").free
    memory = None
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            memory = int(line.split()[1]) * 1024
            break
    load = os.getloadavg()[0]
    db.execute(
        "INSERT OR REPLACE INTO resources(at,free_bytes,available_memory_bytes,load_1m) "
        "VALUES (?,?,?,?)", (stamp(end), free, memory, load)
    )
    set_meta(db, "source:resources:last_success", stamp(end))


def release_state():
    try:
        data = json.loads(RELEASE.read_text(encoding="utf-8"))
        return {key: data.get(key) for key in (
            "platform_version", "source_commit", "release_commit", "manifest_sha256"
        )}
    except (OSError, ValueError):
        return None


def create_report(db, containers, generated):
    try:
        expected = expected_services()["services"]
        inventory_error = None
    except (OSError, ValueError):
        expected = {}
        inventory_error = "Required service inventory is unavailable or invalid"
    observed = {service: 0 for service in expected}
    for container in containers:
        service = container.get("service")
        if service in observed and container.get("state") == "running":
            observed[service] += 1
    mismatched = {service: {"expected": count, "running": observed[service]}
                  for service, count in expected.items() if observed[service] != count}
    cutoff = stamp(generated - RETENTION)
    source_rows = db.execute(
        "SELECT source,MIN(at),MAX(at),COUNT(*) FROM logs WHERE at>=? GROUP BY source",
        (cutoff,),
    ).fetchall()
    sources = {
        name: {"first_at": first, "last_at": last, "records": count,
               "last_collection_at": get_meta(db, "source:" + name + ":last_success")}
        for name, first, last, count in source_rows
    }
    for name in ("docker", "journal", "docker-event", "app", "resources"):
        sources.setdefault(name, {"first_at": None, "last_at": None, "records": 0,
                                  "last_collection_at": get_meta(
                                      db, "source:" + name + ":last_success")})
    for item in sources.values():
        last = item["last_collection_at"]
        item["stale"] = last is None or generated - parse_stamp(last) > timedelta(minutes=10)
    gaps = [
        {"at": at, "source": source, "reason": reason}
        for at, source, reason in db.execute(
            "SELECT at,source,reason FROM gaps WHERE at>=? ORDER BY at DESC LIMIT 50",
            (cutoff,),
        )
    ]
    gap_count = db.execute(
        "SELECT COUNT(*) FROM gaps WHERE at>=?", (cutoff,)
    ).fetchone()[0]
    first_log = db.execute(
        "SELECT MIN(at) FROM logs WHERE at>=?", (cutoff,)
    ).fetchone()[0]
    errors = []
    for service, key, count, first_at, last_at in db.execute(
        "SELECT service,error_key,COUNT(*),MIN(at),MAX(at) FROM logs "
        "WHERE error_key IS NOT NULL AND at>=? GROUP BY service,error_key "
        "ORDER BY COUNT(*) DESC LIMIT 25", (cutoff,),
    ):
        sample = db.execute(
            "SELECT message FROM logs WHERE service=? AND error_key=? "
            "ORDER BY at DESC LIMIT 1", (service, key),
        ).fetchone()
        context = [
            zlib.decompress(row[0]).decode("utf-8")[:300]
            for row in db.execute(
                "SELECT message FROM logs WHERE service=? AND at>=? AND at<=? "
                "ORDER BY at,id LIMIT 8",
                (service, last_at, stamp(parse_stamp(last_at) + timedelta(seconds=5))),
            )
        ]
        errors.append({
            "service": service, "count": count, "first_at": first_at,
            "last_at": last_at,
            "sample": zlib.decompress(sample[0]).decode("utf-8")[:1200]
            if sample else "",
            "context": context,
            "evidence": {"from_utc": last_at,
                         "to_utc": stamp(parse_stamp(last_at) + timedelta(seconds=5)),
                         "service": service},
        })
    errors_24h_total = db.execute(
        "SELECT COUNT(*) FROM logs WHERE error_key IS NOT NULL AND at>=?",
        (stamp(generated - timedelta(hours=24)),),
    ).fetchone()[0]
    latest_resource = db.execute(
        "SELECT at,free_bytes,available_memory_bytes,load_1m FROM resources "
        "ORDER BY at DESC LIMIT 1"
    ).fetchone()
    resource_history = db.execute(
        "SELECT MIN(free_bytes),MIN(available_memory_bytes),MAX(load_1m) "
        "FROM resources WHERE at>=?",
        (stamp(generated - timedelta(hours=24)),),
    ).fetchone()
    resource_days = [
        {"date_utc": day, "samples": samples,
         "minimum_free_bytes": free, "minimum_available_memory_bytes": memory,
         "maximum_load_1m": load}
        for day, samples, free, memory, load in db.execute(
            "SELECT substr(at,1,10),COUNT(*),MIN(free_bytes),"
            "MIN(available_memory_bytes),MAX(load_1m) FROM resources "
            "WHERE at>=? GROUP BY substr(at,1,10) ORDER BY substr(at,1,10)",
            (cutoff,),
        )
    ]
    app = json.loads(get_meta(db, "app:probe", "null"))
    stale = any(item["stale"] for item in sources.values())
    unhealthy_container = any(
        item.get("state") != "running" or item.get("oom_killed")
        or item.get("health") == "unhealthy" for item in containers
    )
    recent_failed_job = bool(app and app.get("archive_jobs")
                             and app["archive_jobs"][0]["status"] == "FAILED")
    report = {
        "schema_version": 1,
        "generated_at": stamp(generated),
        "status": "unknown" if stale or inventory_error else (
            "degraded" if unhealthy_container or mismatched or gaps or recent_failed_job else "healthy"
        ),
        "service_inventory": {"expected": expected, "running": observed,
                              "mismatched": mismatched, "error": inventory_error},
        "history_retention_days": 30,
        "collector_started_at": get_meta(db, "collector:started_at"),
        "earliest_retained_log_at": first_log,
        "sources": sources,
        "coverage_gaps": gaps,
        "coverage_gap_count": gap_count,
        "release": release_state(),
        "application": app,
        "containers": containers,
        "resources": dict(zip(("at", "free_bytes", "available_memory_bytes", "load_1m"),
                              latest_resource)) if latest_resource else None,
        "resource_extremes_24h": dict(zip(
            ("minimum_free_bytes", "minimum_available_memory_bytes", "maximum_load_1m"),
            resource_history,
        )) if resource_history else None,
        "resource_days_30d": resource_days,
        "errors_24h_total": errors_24h_total,
        "errors_30d": errors,
        "evidence": {"format": "redacted JSONL", "query":
                     "Use the private 30-day daily evidence artifact index by UTC date."},
    }
    if len(json.dumps(report).encode()) > 256 * 1024:
        raise RuntimeError("Diagnostic report exceeded its compact size limit")
    return report


def atomic_report(value):
    fd, filename = tempfile.mkstemp(dir=ROOT, prefix=".latest-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            if hasattr(os, "fchmod"):
                os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(filename, ROOT / "buh-diagnostics-latest.json")
    finally:
        Path(filename).unlink(missing_ok=True)


def prune_history(db, cutoff):
    db.execute("DELETE FROM logs WHERE at<?", (cutoff,))
    db.execute("DELETE FROM gaps WHERE at<?", (cutoff,))
    db.execute("DELETE FROM resources WHERE at<?", (cutoff,))


def collect():
    import fcntl

    with (ROOT / ".collect.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = database()
        end = utcnow()
        try:
            redact = redactor()
            if get_meta(db, "collector:started_at") is None:
                set_meta(db, "collector:started_at", stamp(end))
                gap(db, "historical-coverage", "Logs that rotated before collector installation cannot be recovered; verify each source's earliest retained record")
            if shutil.disk_usage(ROOT).free < MINIMUM_FREE_BYTES:
                containers = []
                gap(db, "storage", "Diagnostic log capture paused by free-space guard")
            else:
                try:
                    containers = docker_containers()
                    collect_docker(db, containers, redact, end)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    containers = []
                    gap(db, "docker", "Container inventory failed: " + type(exc).__name__)
                collect_journal(db, redact, end)
                collect_events(db, redact, end)
            collect_app(db, end)
            try:
                collect_resources(db, end)
            except (OSError, ValueError) as exc:
                gap(db, "resources", "Resource sampling failed: " + type(exc).__name__)
            cutoff = stamp(end - RETENTION)
            prune_history(db, cutoff)
            db.commit()
            last_vacuum = get_meta(db, "database:last_vacuum")
            database_size = (ROOT / "history.sqlite3").stat().st_size
            safe_to_vacuum = shutil.disk_usage(ROOT).free > (
                MINIMUM_FREE_BYTES + 2 * database_size
            )
            if safe_to_vacuum and (
                last_vacuum is None
                or end - parse_stamp(last_vacuum) >= timedelta(days=7)
            ):
                db.execute("VACUUM")
                set_meta(db, "database:last_vacuum", stamp(end))
                db.commit()
            else:
                db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            atomic_report(create_report(db, containers, end))
        finally:
            db.close()


def query(db, start, end, service, contains, limit):
    statement = "SELECT source,service,at,message FROM logs WHERE at>=? AND at<?"
    args = [stamp(start), stamp(end)]
    if service:
        statement += " AND service=?"
        args.append(service)
    statement += " ORDER BY at,id"
    matched = 0
    scanned = 0
    for source, service, at, blob in db.execute(statement, args):
        scanned += 1
        if scanned > 1_000_000:
            raise ValueError("Query exceeded its scan limit; narrow the time or service")
        message = zlib.decompress(blob).decode("utf-8")
        if not contains or contains.lower() in message.lower():
            matched += 1
            if matched > limit:
                raise ValueError("Query exceeded its row limit; narrow the time or service")
            yield {"source": source, "service": service, "at": at, "message": message}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect")
    sub.add_parser("latest")
    search = sub.add_parser("query")
    search.add_argument("--from-utc", required=True)
    search.add_argument("--to-utc", required=True)
    search.add_argument("--service")
    search.add_argument("--contains")
    search.add_argument("--limit", type=int, default=2000)
    day = sub.add_parser("export-day")
    day.add_argument("date")
    args = parser.parse_args(argv)
    if args.command == "collect":
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        collect()
        return 0
    if args.command == "latest":
        path = ROOT / "buh-diagnostics-latest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if utcnow() - parse_stamp(payload["generated_at"]) > timedelta(minutes=10):
            payload["report_stale"] = True
        else:
            payload["report_stale"] = False
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    if args.command == "export-day":
        requested = date.fromisoformat(args.date)
        start = datetime.combine(requested, datetime.min.time(), UTC)
        end = start + timedelta(days=1)
        limit = 1_000_000
        service = contains = None
    else:
        start = parse_stamp(args.from_utc)
        end = parse_stamp(args.to_utc)
        if not timedelta(0) < end - start <= timedelta(days=1):
            parser.error("Query window must be within one day")
        if not 1 <= args.limit <= 5000:
            parser.error("Limit must be 1-5000")
        if args.service and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", args.service) is None:
            parser.error("Invalid service")
        if args.contains and len(args.contains) > 100:
            parser.error("Search text is too long")
        service, contains, limit = args.service, args.contains, args.limit
    db = readonly_database()
    try:
        written = 0
        for item in query(db, start, end, service, contains, limit):
            line = json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
            written += len(line.encode())
            if written > 64 * 1024 * 1024:
                raise ValueError("Evidence export exceeded 64 MiB; narrow the query")
            sys.stdout.write(line)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
