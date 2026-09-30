#!/usr/bin/env python3
"""Root-side, narrowly scoped telemetry and Alliance Auth control agent."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

APP_DIR = Path(os.environ.get("BUH_VPS_HEALTH_APP_DIR", "/opt/aa-docker"))
RUNTIME_DIR = Path(
    os.environ.get(
        "BUH_VPS_HEALTH_RUNTIME_DIR",
        str(APP_DIR / "conf" / "buh-vps-health" / "run"),
    )
)
SOCKET_PATH = RUNTIME_DIR / "agent.sock"
TOKEN_PATH = RUNTIME_DIR / "token"
ACTIONS_DIR = RUNTIME_DIR / "actions"
CLIENT_GID = int(os.environ.get("BUH_VPS_HEALTH_CLIENT_GID", "0"))
COLLECT_SECONDS = 5
MAX_REQUEST_BYTES = 64 * 1024
ACTION_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
PERCENT_RE = re.compile(r"[^0-9.+-]")
ENV_COUNT_RE = re.compile(r"(?m)^BUH_AUTH_WORKER_COUNT=.*$")

AUTH_RESTART_SERVICES = (
    "allianceauth_gunicorn",
    "allianceauth_worker",
    "allianceauth_worker_services",
    "allianceauth_beat",
    "nginx",
)
AUTH_RESTART_SERVICE_RE = re.compile(
    r"^(?:allianceauth_gunicorn|allianceauth_beat|allianceauth_worker(?:_[a-z0-9_-]+)?)$"
)

metrics_lock = threading.Lock()
action_lock = threading.Lock()
cached_metrics: dict = {}
last_action_time = {"restart_auth": 0.0, "set_workers": 0.0, "check_updates": 0.0}
previous_cpu: tuple[int, int] | None = None
previous_network: tuple[float, int, int] | None = None
active_action_id: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o640)
        os.chown(path, 0, CLIENT_GID)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(command: list[str], timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=APP_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
        env={
            **os.environ,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        },
    )


def compose(*arguments: str, timeout: int = 45) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "--env-file=.env", *arguments], timeout=timeout)


def percent(value) -> float:
    try:
        return float(PERCENT_RE.sub("", str(value)))
    except (TypeError, ValueError):
        return 0.0


def human_bytes(value) -> int:
    if value is None:
        return 0
    raw = str(value).strip().split("/")[0].strip().replace(" ", "")
    match = re.match(r"^([0-9.]+)([KMGTPE]?i?B)?$", raw, re.IGNORECASE)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = (match.group(2) or "B").upper()
    powers = {
        "B": 0,
        "KB": 1,
        "KIB": 1,
        "MB": 2,
        "MIB": 2,
        "GB": 3,
        "GIB": 3,
        "TB": 4,
        "TIB": 4,
        "PB": 5,
        "PIB": 5,
    }
    return int(amount * (1024 ** powers.get(unit, 0)))


def read_cpu() -> tuple[float, int]:
    global previous_cpu
    values = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    ticks = [int(value) for value in values]
    idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    total = sum(ticks)
    usage = 0.0
    if previous_cpu:
        total_delta = total - previous_cpu[0]
        idle_delta = idle - previous_cpu[1]
        if total_delta > 0:
            usage = 100 * (1 - idle_delta / total_delta)
    previous_cpu = (total, idle)
    return round(max(0.0, min(100.0, usage)), 2), os.cpu_count() or 1


def read_memory() -> dict:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = values.get("SwapTotal", 0)
    swap_used = max(0, swap_total - values.get("SwapFree", 0))
    return {
        "memory_total_bytes": total,
        "memory_used_bytes": used,
        "memory_percent": round(100 * used / total, 2) if total else 0,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_percent": round(100 * swap_used / swap_total, 2) if swap_total else 0,
    }


def read_network() -> tuple[float, float]:
    global previous_network
    rx = tx = 0
    for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
        interface, raw = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        fields = raw.split()
        rx += int(fields[0])
        tx += int(fields[8])
    current = time.monotonic()
    rx_rate = tx_rate = 0.0
    if previous_network:
        elapsed = max(0.001, current - previous_network[0])
        rx_rate = max(0.0, (rx - previous_network[1]) / elapsed)
        tx_rate = max(0.0, (tx - previous_network[2]) / elapsed)
    previous_network = (current, rx, tx)
    return round(rx_rate, 2), round(tx_rate, 2)


def host_metrics() -> dict:
    cpu, cpu_count = read_cpu()
    memory = read_memory()
    disk = shutil.disk_usage("/")
    rx_rate, tx_rate = read_network()
    load_1, load_5, load_15 = os.getloadavg()
    uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    return {
        "cpu_percent": cpu,
        "cpu_count": cpu_count,
        "load_1": round(load_1, 2),
        "load_5": round(load_5, 2),
        "load_15": round(load_15, 2),
        **memory,
        "disk_total_bytes": disk.total,
        "disk_used_bytes": disk.used,
        "disk_percent": round(100 * disk.used / disk.total, 2) if disk.total else 0,
        "network_rx_bytes_per_second": rx_rate,
        "network_tx_bytes_per_second": tx_rate,
        "uptime_seconds": round(uptime),
    }


def parse_json_lines(raw: str) -> list[dict]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except json.JSONDecodeError:
        result = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except json.JSONDecodeError:
                continue
        return result


def docker_metrics() -> tuple[dict, dict]:
    ps = compose("ps", "-a", "--format", "json", timeout=20)
    ps_rows = parse_json_lines(ps.stdout) if ps.returncode == 0 else []
    stats_result = run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=20
    )
    stats_rows = (
        parse_json_lines(stats_result.stdout) if stats_result.returncode == 0 else []
    )
    stats_by_name = {
        str(row.get("Name") or row.get("Container") or ""): row for row in stats_rows
    }
    names = [
        str(row.get("Name") or row.get("Names") or "")
        for row in ps_rows
        if row.get("Name") or row.get("Names")
    ]
    restart_counts = {}
    if names:
        inspected = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Name}}|{{.RestartCount}}",
                *names,
            ],
            timeout=20,
        )
        if inspected.returncode == 0:
            for line in inspected.stdout.splitlines():
                name, _, count = line.partition("|")
                try:
                    restart_counts[name.lstrip("/")] = int(count)
                except ValueError:
                    pass
    containers = []
    for row in ps_rows:
        name = str(row.get("Name") or row.get("Names") or "unknown")
        service = str(row.get("Service") or "")
        state = str(row.get("State") or "")
        status = str(row.get("Status") or state or "unknown")
        health = str(row.get("Health") or "")
        running = state.lower() == "running" or status.lower().startswith("up")
        healthy = (
            running
            and health.lower() != "unhealthy"
            and not re.search(
                r"restarting|exited|dead|unhealthy", status, re.IGNORECASE
            )
        )
        stats = stats_by_name.get(name, {})
        containers.append(
            {
                "name": name,
                "service": service,
                "status": status,
                "healthy": healthy,
                "cpu_percent": percent(stats.get("CPUPerc")),
                "memory_percent": percent(stats.get("MemPerc")),
                "memory_used_bytes": human_bytes(stats.get("MemUsage")),
                "restart_count": restart_counts.get(name, 0),
            }
        )
    unhealthy = sum(1 for item in containers if not item["healthy"])
    desired = desired_workers()
    online = sum(
        1
        for item in containers
        if item["service"] == "allianceauth_worker" and item["healthy"]
    )
    return (
        {
            "total": len(containers),
            "healthy": len(containers) - unhealthy,
            "unhealthy": unhealthy,
            "containers": sorted(containers, key=lambda item: item["name"]),
            "error": "" if ps.returncode == 0 else ps.stdout.strip()[-500:],
        },
        {"desired": desired, "online": online},
    )


def desired_workers() -> int:
    try:
        text = (APP_DIR / ".env").read_text(encoding="utf-8")
    except OSError:
        return 0
    match = ENV_COUNT_RE.search(text)
    if not match:
        return 2
    try:
        return max(1, min(8, int(match.group(0).split("=", 1)[1].strip())))
    except ValueError:
        return 2


def restart_service_names() -> tuple[str, ...]:
    """Discover future Auth worker services without admitting data services."""
    configured = compose("config", "--services", timeout=20)
    if configured.returncode != 0:
        return AUTH_RESTART_SERVICES
    names = {
        line.strip()
        for line in configured.stdout.splitlines()
        if AUTH_RESTART_SERVICE_RE.fullmatch(line.strip())
    }
    names.add("nginx")
    required = {"allianceauth_gunicorn", "allianceauth_worker", "allianceauth_beat"}
    return tuple(sorted(names)) if required.issubset(names) else AUTH_RESTART_SERVICES


def collect_metrics() -> dict:
    host = host_metrics()
    docker, workers = docker_metrics()
    return {
        "generated_at": utc_now(),
        "host": host,
        "docker": docker,
        "workers": workers,
        "agent": {"version": "0.2.0", "collect_seconds": COLLECT_SECONDS},
    }


def collector_loop() -> None:
    while True:
        try:
            result = collect_metrics()
        except Exception as exc:  # noqa: BLE001 - controls must survive collection errors
            result = {
                "generated_at": utc_now(),
                "host": {},
                "docker": {
                    "total": 0,
                    "healthy": 0,
                    "unhealthy": 0,
                    "containers": [],
                    "error": str(exc),
                },
                "workers": {"desired": desired_workers(), "online": 0},
                "agent": {
                    "version": "0.2.0",
                    "collect_seconds": COLLECT_SECONDS,
                    "error": str(exc),
                },
            }
        with metrics_lock:
            cached_metrics.clear()
            cached_metrics.update(result)
        time.sleep(COLLECT_SECONDS)


def read_action(action_id: str) -> dict | None:
    if not ACTION_ID_RE.fullmatch(action_id):
        return None
    try:
        return json.loads(
            (ACTIONS_DIR / f"{action_id}.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None


def write_worker_count(count: int) -> None:
    env_path = APP_DIR / ".env"
    text = env_path.read_text(encoding="utf-8")
    line = f"BUH_AUTH_WORKER_COUNT={count}"
    if ENV_COUNT_RE.search(text):
        updated = ENV_COUNT_RE.sub(line, text, count=1)
    else:
        updated = text.rstrip() + "\n" + line + "\n"
    stat = env_path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".env.buh-vps-health.", dir=APP_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.st_mode)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, env_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_json_output(raw: str, fallback):
    """Parse command JSON while tolerating harmless notices around it."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        starts = [position for position in (raw.find("["), raw.find("{")) if position >= 0]
        if not starts:
            return fallback
        decoder = json.JSONDecoder()
        try:
            value, _end = decoder.raw_decode(raw[min(starts):])
            return value
        except json.JSONDecodeError:
            return fallback


def host_package_updates() -> dict:
    """Refresh APT metadata in an isolated temp tree and simulate an upgrade."""
    updates: list[dict] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="buh-update-check-") as directory:
        lists = Path(directory) / "lists"
        (lists / "partial").mkdir(parents=True)
        option = f"Dir::State::lists={lists}"
        refreshed = run(
            ["apt-get", "-o", option, "-o", "APT::Get::List-Cleanup=0", "update"],
            timeout=300,
        )
        if refreshed.returncode != 0:
            errors.append(
                "Temporary APT metadata refresh failed: "
                + (refreshed.stdout.strip()[-1200:] or "unknown APT error")
            )
            simulated = run(["apt-get", "-s", "upgrade"], timeout=180)
        else:
            simulated = run(["apt-get", "-o", option, "-s", "upgrade"], timeout=180)
        if simulated.returncode != 0:
            errors.append(
                "APT upgrade simulation failed: "
                + (simulated.stdout.strip()[-1200:] or "unknown APT error")
            )
        else:
            pattern = re.compile(
                r"^Inst\s+(?P<name>\S+)\s+(?:\[(?P<current>[^\]]+)\]\s+)?"
                r"\((?P<candidate>\S+)(?:\s+[^)]*)?\)",
                re.MULTILINE,
            )
            for match in pattern.finditer(simulated.stdout):
                updates.append(
                    {
                        "name": match.group("name")[:180],
                        "current": (match.group("current") or "unknown")[:120],
                        "latest": match.group("candidate")[:120],
                    }
                )
    return {"updates": updates[:500], "count": len(updates), "errors": errors}


def python_package_updates() -> dict:
    command = (
        "import importlib.metadata as m,json; "
        "print(json.dumps(sorted([{'name':d.metadata.get('Name',d.name),'version':d.version} "
        "for d in m.distributions() if (d.metadata.get('Name',d.name) or '').lower().startswith(('aa-','allianceauth','django-esi'))], "
        "key=lambda x:x['name'].lower())))"
    )
    installed_result = compose(
        "exec", "-T", "allianceauth_gunicorn", "python3", "-c", command, timeout=60
    )
    installed = (
        parse_json_output(installed_result.stdout, [])
        if installed_result.returncode == 0
        else []
    )
    outdated_result = compose(
        "exec",
        "-T",
        "allianceauth_gunicorn",
        "python3",
        "-m",
        "pip",
        "list",
        "--outdated",
        "--format=json",
        "--disable-pip-version-check",
        timeout=300,
    )
    outdated = (
        parse_json_output(outdated_result.stdout, [])
        if outdated_result.returncode == 0
        else []
    )
    if not isinstance(outdated, list):
        outdated = []
    updates = [
        {
            "name": str(item.get("name") or "unknown")[:180],
            "current": str(item.get("version") or "unknown")[:120],
            "latest": str(item.get("latest_version") or "unknown")[:120],
            "type": str(item.get("latest_filetype") or "")[:40],
        }
        for item in outdated[:500]
        if isinstance(item, dict)
    ]
    errors = []
    if installed_result.returncode != 0:
        errors.append("Could not inventory Auth Python packages: " + installed_result.stdout[-1000:])
    if outdated_result.returncode != 0:
        errors.append("PyPI update check failed: " + outdated_result.stdout[-1000:])
    return {
        "installed_buh": installed if isinstance(installed, list) else [],
        "updates": updates,
        "count": len(updates),
        "errors": errors,
    }


def local_wheel_updates(installed_buh: list[dict]) -> dict:
    def version_key(value: str) -> tuple:
        """Natural-sort versions without depending on host Python packages."""
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in re.findall(r"\d+|[A-Za-z]+", value)
        )

    installed = {
        re.sub(r"[-_.]+", "-", str(item.get("name") or "")).lower(): str(
            item.get("version") or ""
        )
        for item in installed_buh
        if isinstance(item, dict)
    }
    wheel_pattern = re.compile(
        r"^(?P<name>(?:aa|allianceauth)[A-Za-z0-9_.-]*?)-(?P<version>\d[^-]*)-py\d-none-any\.whl$",
        re.IGNORECASE,
    )
    local: dict[str, dict] = {}
    for path in (APP_DIR / "conf").glob("*.whl"):
        match = wheel_pattern.match(path.name)
        if not match:
            continue
        normalized = re.sub(r"[-_.]+", "-", match.group("name")).lower()
        candidate = {
            "name": normalized,
            "version": match.group("version"),
            "file": path.name,
        }
        current = local.get(normalized)
        if not current or version_key(candidate["version"]) > version_key(
            current["version"]
        ):
            local[normalized] = candidate
    mismatches = []
    for name, candidate in sorted(local.items()):
        current = installed.get(name)
        if current and current != candidate["version"]:
            mismatches.append(
                {
                    "name": name,
                    "current": current,
                    "latest": candidate["version"],
                    "file": candidate["file"],
                }
            )
    return {
        "local_wheels": list(local.values())[:300],
        "updates": mismatches,
        "count": len(mismatches),
        "errors": [],
    }


def docker_image_updates() -> dict:
    config_result = compose("config", "--format", "json", timeout=45)
    if config_result.returncode != 0:
        return {
            "images": [],
            "count": 0,
            "errors": ["Docker Compose image inventory failed: " + config_result.stdout[-1200:]],
        }
    config = parse_json_output(config_result.stdout, {})
    services = config.get("services", {}) if isinstance(config, dict) else {}
    images = []
    errors = []
    seen: set[str] = set()
    for service in services.values():
        if not isinstance(service, dict) or service.get("build"):
            continue
        image = str(service.get("image") or "").strip()
        if not image or image in seen:
            continue
        seen.add(image)
        local_result = run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
            timeout=30,
        )
        local_digests = set()
        if local_result.returncode == 0:
            values = parse_json_output(local_result.stdout, [])
            if isinstance(values, list):
                local_digests = {
                    str(value).rsplit("@", 1)[-1] for value in values if "@" in str(value)
                }
        remote_result = run(
            [
                "docker",
                "buildx",
                "imagetools",
                "inspect",
                image,
                "--format",
                "{{json .Manifest.Digest}}",
            ],
            timeout=120,
        )
        remote_digest = ""
        if remote_result.returncode == 0:
            remote_digest = remote_result.stdout.strip().strip('"')
        else:
            fallback = run(["docker", "manifest", "inspect", "--verbose", image], timeout=120)
            payload = parse_json_output(fallback.stdout, {}) if fallback.returncode == 0 else {}
            if isinstance(payload, dict):
                remote_digest = str((payload.get("Descriptor") or {}).get("digest") or "")
            if not remote_digest:
                errors.append(f"Could not check registry digest for {image}: {(remote_result.stdout or fallback.stdout)[-600:]}")
        update_available = bool(remote_digest and local_digests and remote_digest not in local_digests)
        images.append(
            {
                "image": image[:300],
                "services": sorted(
                    str(name)
                    for name, data in services.items()
                    if isinstance(data, dict) and data.get("image") == image
                ),
                "local_digests": sorted(local_digests)[:5],
                "remote_digest": remote_digest[:120],
                "update_available": update_available,
                "comparison": "exact" if remote_digest and local_digests else "unavailable",
            }
        )
    return {
        "images": images,
        "count": sum(1 for item in images if item["update_available"]),
        "errors": errors,
    }


def run_update_scan() -> dict:
    started = time.monotonic()
    host = host_package_updates()
    python = python_package_updates()
    local = local_wheel_updates(python.get("installed_buh") or [])
    docker = docker_image_updates()
    errors = host["errors"] + python["errors"] + local["errors"] + docker["errors"]
    total = host["count"] + python["count"] + local["count"] + docker["count"]
    return {
        "generated_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 2),
        "host": {
            "platform": platform.platform()[:300],
            "updates": host["updates"],
            "count": host["count"],
        },
        "python": python,
        "local_wheels": local,
        "docker": docker,
        "summary": {
            "total_updates": total,
            "host_packages": host["count"],
            "python_packages": python["count"],
            "local_wheels": local["count"],
            "docker_images": docker["count"],
            "errors": len(errors),
        },
        "errors": errors[:50],
    }


def perform_action(action: dict) -> None:
    global active_action_id
    action_id = action["id"]
    path = ACTIONS_DIR / f"{action_id}.json"
    # Give the requesting Gunicorn worker enough time to return HTTP 202.
    time.sleep(2)
    action["status"] = "running"
    action["started_at"] = utc_now()
    action["message"] = "The host agent is applying the guarded operation."
    atomic_json(path, action)
    try:
        if action["action"] == "restart_auth":
            result = compose("restart", *restart_service_names(), timeout=180)
            success_message = (
                "Alliance Auth application services restarted successfully."
            )
        elif action["action"] == "set_workers":
            count = int(action["worker_count"])
            if not 1 <= count <= 8:
                raise RuntimeError("Worker count is outside the hard 1-8 safety range.")
            write_worker_count(count)
            result = compose(
                "up",
                "-d",
                "--no-deps",
                "--scale",
                f"allianceauth_worker={count}",
                "allianceauth_worker",
                timeout=180,
            )
            success_message = f"Alliance Auth worker count set to {count}."
        elif action["action"] == "check_updates":
            action["result"] = run_update_scan()
            result = subprocess.CompletedProcess([], 0, stdout="")
            total = action["result"]["summary"]["total_updates"]
            errors = action["result"]["summary"]["errors"]
            success_message = (
                f"Update scan completed: {total} update(s) found"
                + (f" with {errors} check warning(s)." if errors else ".")
            )
        else:
            raise RuntimeError("Unsupported action.")
        if result.returncode != 0:
            raise RuntimeError(
                result.stdout.strip()[-2000:] or "Docker Compose failed."
            )
        action["status"] = "succeeded"
        action["message"] = success_message
    except Exception as exc:  # noqa: BLE001 - every action outcome must be persisted
        action["status"] = "failed"
        action["message"] = str(exc)[:4000]
    action["finished_at"] = utc_now()
    atomic_json(path, action)
    with action_lock:
        if active_action_id == action_id:
            active_action_id = None


def accept_action(payload: dict) -> dict:
    global active_action_id
    action_id = str(payload.get("action_id") or "")
    action_name = str(payload.get("action") or "")
    if not ACTION_ID_RE.fullmatch(action_id):
        raise ValueError("A valid UUID action ID is required.")
    try:
        uuid.UUID(action_id)
    except ValueError as exc:
        raise ValueError("A valid UUID action ID is required.") from exc
    if action_name not in {"restart_auth", "set_workers", "check_updates"}:
        raise ValueError("This action is not in the host-agent allowlist.")
    existing = read_action(action_id)
    if existing:
        return existing
    cooldown = {"restart_auth": 120, "set_workers": 20, "check_updates": 300}[action_name]
    with action_lock:
        if active_action_id:
            raise ValueError(
                f"Another guarded operation ({active_action_id}) is still running."
            )
        remaining = cooldown - (time.monotonic() - last_action_time[action_name])
        if remaining > 0:
            raise ValueError(
                f"Action cooldown is active for another {int(remaining) + 1}s."
            )
        worker_count = payload.get("worker_count")
        if action_name == "set_workers":
            try:
                worker_count = int(worker_count)
            except (TypeError, ValueError) as exc:
                raise ValueError("Worker count must be an integer.") from exc
            if not 1 <= worker_count <= 8:
                raise ValueError("Worker count is outside the hard 1-8 safety range.")
        else:
            worker_count = None
        action = {
            "id": action_id,
            "action": action_name,
            "worker_count": worker_count,
            "requester": str(payload.get("requester") or "")[:150],
            "status": "accepted",
            "message": "Action accepted by the guarded host agent.",
            "accepted_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "result": {},
        }
        atomic_json(ACTIONS_DIR / f"{action_id}.json", action)
        last_action_time[action_name] = time.monotonic()
        active_action_id = action_id
        threading.Thread(target=perform_action, args=(action,), daemon=True).start()
        return action


def handle_request(payload: dict) -> dict:
    try:
        expected = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return {"ok": False, "error": "Agent token is unavailable."}
    supplied = str(payload.get("token") or "")
    if (
        not expected
        or not supplied
        or not __import__("hmac").compare_digest(expected, supplied)
    ):
        return {"ok": False, "error": "Authentication failed."}
    operation = payload.get("operation")
    try:
        if operation == "metrics":
            with metrics_lock:
                metrics = dict(cached_metrics)
            if not metrics:
                metrics = collect_metrics()
            return {"ok": True, "metrics": metrics}
        if operation == "action":
            return {"ok": True, "action": accept_action(payload)}
        if operation == "action_status":
            action = read_action(str(payload.get("action_id") or ""))
            return (
                {"ok": True, "action": action}
                if action
                else {"ok": False, "error": "Action was not found."}
            )
        return {"ok": False, "error": "Unknown operation."}
    except (ValueError, OSError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            response = {"ok": False, "error": "Request exceeded the safety limit."}
        else:
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise TypeError
            except (json.JSONDecodeError, TypeError):
                response = {"ok": False, "error": "Invalid JSON request."}
            else:
                response = handle_request(payload)
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())


class ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def prepare_runtime() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(RUNTIME_DIR, 0, CLIENT_GID)
    os.chown(ACTIONS_DIR, 0, CLIENT_GID)
    os.chmod(RUNTIME_DIR, 0o750)
    os.chmod(ACTIONS_DIR, 0o750)
    for path in ACTIONS_DIR.glob("*.json"):
        try:
            action = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if action.get("status") in {"accepted", "running"}:
            action["status"] = "failed"
            action["message"] = (
                "The host agent restarted before this operation completed."
            )
            action["finished_at"] = utc_now()
            atomic_json(path, action)
    if SOCKET_PATH.exists() or SOCKET_PATH.is_socket():
        SOCKET_PATH.unlink()


def main() -> None:
    prepare_runtime()
    threading.Thread(target=collector_loop, daemon=True).start()
    with ThreadingUnixServer(str(SOCKET_PATH), RequestHandler) as server:
        os.chown(SOCKET_PATH, 0, CLIENT_GID)
        os.chmod(SOCKET_PATH, 0o660)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
