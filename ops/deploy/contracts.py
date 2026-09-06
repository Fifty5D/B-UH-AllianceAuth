"""Strict, secret-free contracts used by the Platform v2 receiver."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping
from urllib.parse import urlsplit


CONTRACT_SCHEMA_VERSION = 1
RECEIVER_CONFIG_SCHEMA_VERSION = 2
MAX_REQUEST_BYTES = 16 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 132
MAX_FILE_BYTES = 64 * 1024 * 1024

REQUIRED_SMOKE_CHECK_STATUSES = {
    "/": (200, 302),
    "/account/login/": (200, 302),
    "/moon-tax/": (200, 302, 403),
    "/structure-operations/": (200, 302, 403),
    "/mining-analytics/": (200, 302, 403),
}

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
SAFE_RELATIVE_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9._/-]{0,254}$")
SAFE_REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"
)
IMAGE_DIGEST_RE = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"
    r"@sha256:[0-9a-f]{64}$"
)
FATAL_LOG_ALLOWLIST_RE = re.compile(
    r"^component=([a-z][a-z0-9_.-]{2,23})\|"
    r"exception=([A-Z][A-Za-z0-9_.]{2,47})\|"
    r"message=([A-Za-z0-9][^\x00-\x1f\x7f|]{19,71})$"
)
EXACT_DISCORD_PERMISSION_MESSAGE = (
    "403 Forbidden (error code: 50013): Missing Permissions"
)
SENSITIVE_KEY_RE = (
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"set[_-]?cookie|client[_-]?secret|refresh[_-]?token|access[_-]?token|"
    r"private[_-]?key|session(?:id)?|csrf(?:token)?)"
)
SENSITIVE_IDENTIFIER_RE = (
    rf"(?:[A-Za-z0-9_.-]*{SENSITIVE_KEY_RE}[A-Za-z0-9_.-]*)"
)
SENSITIVE_TEXT_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\bBot\s+[A-Za-z0-9._~+/=-]{12,}"), "Bot <redacted>"),
    (
        re.compile(
            rf"(?i)([\"']?{SENSITIVE_IDENTIFIER_RE}[\"']?\s*[:=]\s*)"
            r"([\"'])(.*?)\2"
        ),
        r"\1\2<redacted>\2",
    ),
    (
        re.compile(
            rf"(?i)([\"']?{SENSITIVE_IDENTIFIER_RE}[\"']?)"
            r"(\s*[:=]\s*)([^\s,;}}]+)"
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|token|key|api_key|secret|"
            r"signature|sig|auth|sentry_key)=)[^&\s\"']+"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
        r"\1<redacted>:<redacted>@",
    ),
    (
        re.compile(
            r"https://(?:canary\.)?discord(?:app)?\.com/api/webhooks/\S+",
            re.IGNORECASE,
        ),
        "https://discord.com/api/webhooks/<redacted>",
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."
            r"[A-Za-z0-9_-]{10,}\b"
        ),
        "<redacted-jwt>",
    ),
)


class DeploymentError(RuntimeError):
    """Raised when a deployment input or host invariant fails closed."""


def redact_sensitive_text(text: str) -> str:
    """Redact credential-shaped retained text to a bounded fixed point."""

    for _ in range(8):
        updated = text
        for pattern, replacement in SENSITIVE_TEXT_PATTERNS:
            updated = pattern.sub(replacement, updated)
        if updated == text:
            return updated
        text = updated
    return text


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_keys(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    context: str,
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - allowed
    if missing or unknown:
        raise DeploymentError(
            f"Invalid {context} fields; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )


def _load_json(path: Path, *, max_bytes: int, context: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise DeploymentError(f"{context} is not a regular file")
    data = path.read_bytes()
    if not data or len(data) > max_bytes:
        raise DeploymentError(f"{context} has an unsafe size")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"{context} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentError(f"{context} must be an object")
    if data != canonical_json_bytes(value):
        raise DeploymentError(f"{context} must use canonical JSON")
    return value


def _absolute_directory(value: Any, context: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise DeploymentError(f"{context} must be an absolute path")
    path = Path(value)
    if path == Path("/") or ".." in path.parts or len(value) > 255:
        raise DeploymentError(f"{context} is unsafe")
    return path


def _relative_path(value: Any, context: str) -> Path:
    pure = PurePosixPath(value) if isinstance(value, str) else None
    if (
        not isinstance(value, str)
        or not SAFE_RELATIVE_RE.fullmatch(value)
        or value.startswith("/")
        or pure is None
        or not pure.parts
        or ".." in pure.parts
        or "//" in value
    ):
        raise DeploymentError(f"{context} is unsafe")
    return Path(value)


def _service(value: Any, context: str) -> str:
    if not isinstance(value, str) or not SERVICE_RE.fullmatch(value):
        raise DeploymentError(f"{context} is not a safe service name")
    return value


def _safe_argument(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or any(ord(character) < 32 for character in value)
    ):
        raise DeploymentError(f"{context} is not a safe argument")
    return value


@dataclass(frozen=True)
class SmokeCheck:
    url: str
    statuses: tuple[int, ...]


@dataclass(frozen=True)
class ReceiverConfig:
    schema_version: int
    repository: str
    app_dir: Path
    state_dir: Path
    backup_dir: Path
    compose_file: Path
    env_file: Path
    custom_dockerfile: Path
    local_settings: Path
    manage_py: str
    auth_services: tuple[str, ...]
    gunicorn_service: str
    worker_service: str
    database_service: str
    redis_service: str
    proxy_service: str
    beat_service: str
    nginx_upstream_file: Path
    nginx_upstream_container_file: str
    gunicorn_port: int
    setup_arguments: Mapping[str, tuple[str, ...]]
    smoke_checks: tuple[SmokeCheck, ...]
    application_health_commands: tuple[str, ...]
    required_celery_queues: tuple[str, ...]
    required_celery_tasks: tuple[str, ...]
    fatal_log_allowlist: tuple[str, ...]
    legacy_platform_version: str
    legacy_release_commit: str
    command_timeout_seconds: int
    health_attempts: int
    health_interval_seconds: int
    stabilization_seconds: int
    stabilization_interval_seconds: int
    restore_tmpfs_mb: int
    max_archive_bytes: int

    @classmethod
    def load(cls, path: Path) -> "ReceiverConfig":
        data = _load_json(path, max_bytes=64 * 1024, context="receiver config")
        v1_fields = {
            "schema_version",
            "repository",
            "app_dir",
            "state_dir",
            "backup_dir",
            "compose_file",
            "env_file",
            "custom_dockerfile",
            "local_settings",
            "manage_py",
            "auth_services",
            "gunicorn_service",
            "worker_service",
            "database_service",
            "redis_service",
            "proxy_service",
            "setup_arguments",
            "smoke_checks",
            "legacy_platform_version",
            "legacy_release_commit",
            "command_timeout_seconds",
            "health_attempts",
            "health_interval_seconds",
            "restore_tmpfs_mb",
            "max_archive_bytes",
        }
        v2_fields = v1_fields | {
            "beat_service",
            "nginx_upstream_file",
            "nginx_upstream_container_file",
            "gunicorn_port",
            "application_health_commands",
            "required_celery_queues",
            "required_celery_tasks",
            "fatal_log_allowlist",
            "stabilization_seconds",
            "stabilization_interval_seconds",
        }
        schema_version = data.get("schema_version")
        if schema_version == CONTRACT_SCHEMA_VERSION:
            fields = v1_fields
        elif schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            fields = v2_fields
        else:
            raise DeploymentError("Unsupported receiver config schema")
        _strict_keys(data, allowed=fields, required=fields, context="receiver config")
        repository = data["repository"]
        if not isinstance(repository, str) or not SAFE_REPOSITORY_RE.fullmatch(repository):
            raise DeploymentError("Invalid repository in receiver config")
        manage_py = data["manage_py"]
        if (
            not isinstance(manage_py, str)
            or not manage_py.startswith("/")
            or ".." in PurePosixPath(manage_py).parts
            or not SAFE_RELATIVE_RE.fullmatch(manage_py.lstrip("/"))
        ):
            raise DeploymentError("Invalid in-container manage.py path")

        services = data["auth_services"]
        if (
            not isinstance(services, list)
            or not services
            or len(services) > 16
            or len(services) != len(set(services))
        ):
            raise DeploymentError("auth_services must be a unique, non-empty list")
        auth_services = tuple(
            _service(value, f"auth service {index}")
            for index, value in enumerate(services)
        )
        named_services = {
            name: _service(data[name], name)
            for name in (
                "gunicorn_service",
                "worker_service",
                "database_service",
                "redis_service",
                "proxy_service",
            )
        }
        if named_services["gunicorn_service"] not in auth_services:
            raise DeploymentError("gunicorn_service must occur in auth_services")
        if named_services["worker_service"] not in auth_services:
            raise DeploymentError("worker_service must occur in auth_services")
        if schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            beat_service = _service(data["beat_service"], "beat_service")
        else:
            beat_candidates = tuple(
                service for service in auth_services if service.lower().endswith("beat")
            )
            if len(beat_candidates) != 1:
                raise DeploymentError(
                    "Schema-v1 receiver config must identify exactly one *beat service"
                )
            beat_service = beat_candidates[0]
        if beat_service not in auth_services:
            raise DeploymentError("beat_service must occur in auth_services")
        if beat_service in {
            named_services["gunicorn_service"],
            named_services["worker_service"],
        }:
            raise DeploymentError("Gunicorn, worker, and beat services must be distinct")
        infrastructure = {
            named_services["database_service"],
            named_services["redis_service"],
            named_services["proxy_service"],
        }
        if len(infrastructure) != 3 or infrastructure.intersection(auth_services):
            raise DeploymentError(
                "Database, Redis, and proxy services must be distinct from Auth services"
            )

        setup_raw = data["setup_arguments"]
        if not isinstance(setup_raw, dict) or len(setup_raw) > 32:
            raise DeploymentError("setup_arguments must be an object")
        setup_arguments: dict[str, tuple[str, ...]] = {}
        command_re = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
        for command, arguments in setup_raw.items():
            if not command_re.fullmatch(command):
                raise DeploymentError("setup_arguments contains an unsafe command")
            if not isinstance(arguments, list) or len(arguments) > 32:
                raise DeploymentError(f"Invalid arguments for {command}")
            setup_arguments[command] = tuple(
                _safe_argument(argument, f"argument {index} for {command}")
                for index, argument in enumerate(arguments)
            )

        checks_raw = data["smoke_checks"]
        if not isinstance(checks_raw, list) or not 1 <= len(checks_raw) <= 16:
            raise DeploymentError("smoke_checks must be a bounded, non-empty list")
        smoke_checks: list[SmokeCheck] = []
        for index, check in enumerate(checks_raw):
            if not isinstance(check, dict):
                raise DeploymentError(f"Smoke check {index} must be an object")
            _strict_keys(
                check,
                allowed={"url", "statuses"},
                required={"url", "statuses"},
                context=f"smoke check {index}",
            )
            url = check["url"]
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                parsed_port = parsed.port if parsed is not None else None
            except ValueError:
                parsed = None
                parsed_port = None
            if (
                not isinstance(url, str)
                or not url.startswith("https://")
                or len(url) > 500
                or any(ord(character) < 32 for character in url)
                or parsed is None
                or parsed.scheme != "https"
                or parsed.netloc != "auth.b-uh.com"
                or parsed.hostname != "auth.b-uh.com"
                or parsed_port is not None
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query)
                or bool(parsed.fragment)
                or not parsed.path.startswith("/")
                or "//" in parsed.path
                or any(part in {".", ".."} for part in parsed.path.split("/"))
                or re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]*", parsed.path)
                is None
                or url != f"https://auth.b-uh.com{parsed.path}"
            ):
                raise DeploymentError(
                    f"Smoke check {index} must use the production HTTPS origin"
                )
            statuses = check["statuses"]
            if (
                not isinstance(statuses, list)
                or not statuses
                or len(statuses) > 8
                or len(statuses) != len(set(statuses))
                or not all(
                    type(status) is int and 100 <= status <= 499
                    for status in statuses
                )
            ):
                raise DeploymentError(f"Smoke check {index} has invalid statuses")
            smoke_checks.append(SmokeCheck(url=url, statuses=tuple(statuses)))
        paths = [urlsplit(check.url).path for check in smoke_checks]
        if len(paths) != len(set(paths)):
            raise DeploymentError("smoke_checks must use unique route paths")
        required_checks = REQUIRED_SMOKE_CHECK_STATUSES
        missing_paths = set(required_checks) - set(paths)
        if schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            if missing_paths:
                raise DeploymentError(
                    "smoke_checks must uniquely cover public, login, Moon Tax, "
                    "Structure Ops, and Mining Analytics routes"
                )
            for check in smoke_checks:
                route = urlsplit(check.url).path
                expected_statuses = required_checks.get(route)
                if expected_statuses is not None and set(check.statuses) != set(
                    expected_statuses
                ):
                    raise DeploymentError(
                        f"Smoke check for {route} must use its fixed status policy"
                    )
        else:
            # Installed schema-v1 configs predate the complete functional gate.
            # Preserve every configured check and synthesize only the fixed,
            # production-origin minimums so a receiver code upgrade cannot
            # silently omit login or application health coverage.
            for check in smoke_checks:
                route = urlsplit(check.url).path
                expected_statuses = required_checks.get(route)
                if expected_statuses is not None and not set(check.statuses) <= set(
                    expected_statuses
                ):
                    raise DeploymentError(
                        f"Legacy smoke check for {route} exceeds its fixed status policy"
                    )
            for route, statuses in required_checks.items():
                if route in missing_paths:
                    smoke_checks.append(
                        SmokeCheck(
                            url=f"https://auth.b-uh.com{route}",
                            statuses=statuses,
                        )
                    )

        command_re = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
        contract_name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")

        def bounded_strings(
            name: str,
            *,
            maximum: int,
            minimum: int,
            pattern: re.Pattern[str],
            defaults: tuple[str, ...],
        ) -> tuple[str, ...]:
            raw = data[name] if schema_version == RECEIVER_CONFIG_SCHEMA_VERSION else list(defaults)
            if (
                not isinstance(raw, list)
                or not minimum <= len(raw) <= maximum
                or len(raw) != len(set(raw))
                or any(not isinstance(item, str) or not pattern.fullmatch(item) for item in raw)
            ):
                raise DeploymentError(f"{name} must be a bounded list of safe unique strings")
            return tuple(raw)

        application_health_commands = bounded_strings(
            "application_health_commands",
            maximum=16,
            minimum=1,
            pattern=command_re,
            defaults=(
                "buh_moon_tax_status",
                "buh_structure_ops_status",
                "buh_mining_check",
            ),
        )
        required_celery_queues = bounded_strings(
            "required_celery_queues",
            maximum=32,
            minimum=1,
            pattern=contract_name_re,
            defaults=("celery", "services"),
        )
        required_celery_tasks = bounded_strings(
            "required_celery_tasks",
            maximum=64,
            minimum=1,
            pattern=contract_name_re,
            defaults=(
                "buh_moon_tax.tasks.run_scheduled_audit",
                "buh_structure_ops.tasks.capture_and_evaluate",
            ),
        )
        allowlist_raw = (
            data["fatal_log_allowlist"]
            if schema_version == RECEIVER_CONFIG_SCHEMA_VERSION
            else []
        )
        if (
            not isinstance(allowlist_raw, list)
            or len(allowlist_raw) > 32
            or len(allowlist_raw) != len(set(allowlist_raw))
            or any(
                not isinstance(item, str)
                or FATAL_LOG_ALLOWLIST_RE.fullmatch(item) is None
                for item in allowlist_raw
            )
        ):
            raise DeploymentError(
                "fatal_log_allowlist must contain bounded structured signatures"
            )
        for signature in allowlist_raw:
            match = FATAL_LOG_ALLOWLIST_RE.fullmatch(signature)
            assert match is not None
            _component, exception, message = match.groups()
            exact_discord_permission = (
                exception in {"Forbidden", "DiscordForbidden"}
                and message == EXACT_DISCORD_PERMISSION_MESSAGE
            )
            words = {
                word.casefold()
                for word in re.findall(r"[A-Za-z0-9]{3,}", message)
            }
            if (
                exception in {"Error", "Exception", "HTTPError"}
                or len(words) < 4
                or (
                    not exact_discord_permission
                    and re.search(
                        r"(?i)(?:\b403\b|\b50013\b|permissions?|"
                        r"permission denied|\berrors?\b)",
                        signature,
                    )
                )
            ):
                raise DeploymentError(
                    "fatal_log_allowlist contains a broad or forbidden signature"
                )
        fatal_log_allowlist = tuple(allowlist_raw)

        def bounded_int(name: str, minimum: int, maximum: int) -> int:
            value = data[name]
            if type(value) is not int or not minimum <= value <= maximum:
                raise DeploymentError(f"{name} is outside its safe range")
            return value

        if not isinstance(data["legacy_platform_version"], str) or not SEMVER_RE.fullmatch(
            data["legacy_platform_version"]
        ):
            raise DeploymentError("Invalid legacy platform version")
        if not isinstance(data["legacy_release_commit"], str) or not COMMIT_RE.fullmatch(
            data["legacy_release_commit"]
        ):
            raise DeploymentError("Invalid legacy release commit")

        directories = {
            "app_dir": _absolute_directory(data["app_dir"], "app_dir"),
            "state_dir": _absolute_directory(data["state_dir"], "state_dir"),
            "backup_dir": _absolute_directory(data["backup_dir"], "backup_dir"),
        }
        if len(set(directories.values())) != len(directories):
            raise DeploymentError("Application, state, and backup directories must differ")

        if schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            nginx_upstream_file = _relative_path(
                data["nginx_upstream_file"], "nginx_upstream_file"
            )
            nginx_upstream_container_file = data["nginx_upstream_container_file"]
            if (
                not isinstance(nginx_upstream_container_file, str)
                or not nginx_upstream_container_file.startswith("/")
                or ".." in PurePosixPath(nginx_upstream_container_file).parts
                or not SAFE_RELATIVE_RE.fullmatch(
                    nginx_upstream_container_file.lstrip("/")
                )
            ):
                raise DeploymentError("Invalid in-container Nginx upstream path")
            gunicorn_port = bounded_int("gunicorn_port", 1024, 65535)
            stabilization_seconds = bounded_int(
                "stabilization_seconds", 300, 1800
            )
            stabilization_interval_seconds = bounded_int(
                "stabilization_interval_seconds", 5, 60
            )
            if stabilization_interval_seconds > stabilization_seconds:
                raise DeploymentError(
                    "stabilization_interval_seconds exceeds stabilization_seconds"
                )
        else:
            nginx_upstream_file = Path("conf/buh-platform-v2/nginx/upstream.conf")
            nginx_upstream_container_file = "/etc/nginx/buh-platform-v2/upstream.conf"
            gunicorn_port = 8000
            stabilization_seconds = 300
            stabilization_interval_seconds = 15

        return cls(
            schema_version=schema_version,
            repository=repository,
            **directories,
            compose_file=_relative_path(data["compose_file"], "compose_file"),
            env_file=_relative_path(data["env_file"], "env_file"),
            custom_dockerfile=_relative_path(
                data["custom_dockerfile"], "custom_dockerfile"
            ),
            local_settings=_relative_path(data["local_settings"], "local_settings"),
            manage_py=manage_py,
            auth_services=auth_services,
            setup_arguments=setup_arguments,
            smoke_checks=tuple(smoke_checks),
            beat_service=beat_service,
            nginx_upstream_file=nginx_upstream_file,
            nginx_upstream_container_file=nginx_upstream_container_file,
            gunicorn_port=gunicorn_port,
            application_health_commands=application_health_commands,
            required_celery_queues=required_celery_queues,
            required_celery_tasks=required_celery_tasks,
            fatal_log_allowlist=fatal_log_allowlist,
            legacy_platform_version=data["legacy_platform_version"],
            legacy_release_commit=data["legacy_release_commit"],
            command_timeout_seconds=bounded_int(
                "command_timeout_seconds", 30, 3600
            ),
            health_attempts=bounded_int("health_attempts", 1, 120),
            health_interval_seconds=bounded_int(
                "health_interval_seconds", 1, 60
            ),
            stabilization_seconds=stabilization_seconds,
            stabilization_interval_seconds=stabilization_interval_seconds,
            restore_tmpfs_mb=bounded_int("restore_tmpfs_mb", 256, 16384),
            max_archive_bytes=bounded_int(
                "max_archive_bytes", 1024 * 1024, MAX_ARCHIVE_BYTES
            ),
            **named_services,
        )


@dataclass(frozen=True)
class DeploymentRequest:
    mode: str
    repository: str
    release_commit: str
    release_ref: str
    platform_version: str
    manifest_sha256: str
    workflow_run_id: str
    workflow_run_attempt: int

    @property
    def attempt_id(self) -> str:
        return f"gh-{self.workflow_run_id}-{self.workflow_run_attempt}"

    @classmethod
    def load(cls, path: Path) -> "DeploymentRequest":
        data = _load_json(path, max_bytes=MAX_REQUEST_BYTES, context="deployment request")
        fields = {
            "schema_version",
            "mode",
            "repository",
            "release_commit",
            "release_ref",
            "platform_version",
            "manifest_sha256",
            "workflow_run_id",
            "workflow_run_attempt",
        }
        _strict_keys(data, allowed=fields, required=fields, context="deployment request")
        if data["schema_version"] != CONTRACT_SCHEMA_VERSION:
            raise DeploymentError("Unsupported deployment request schema")
        if data["mode"] not in {"preflight", "deploy"}:
            raise DeploymentError("Unsupported deployment mode")
        if not isinstance(data["repository"], str) or not SAFE_REPOSITORY_RE.fullmatch(
            data["repository"]
        ):
            raise DeploymentError("Invalid deployment repository")
        if not isinstance(data["release_commit"], str) or not COMMIT_RE.fullmatch(
            data["release_commit"]
        ):
            raise DeploymentError("Invalid release commit")
        version = data["platform_version"]
        if not isinstance(version, str) or not SEMVER_RE.fullmatch(version):
            raise DeploymentError("Invalid platform version")
        if data["release_ref"] != f"release/platform-v{version}":
            raise DeploymentError("Release ref and platform version disagree")
        if not isinstance(data["manifest_sha256"], str) or not SHA256_RE.fullmatch(
            data["manifest_sha256"]
        ):
            raise DeploymentError("Invalid release manifest digest")
        run_id = data["workflow_run_id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
            raise DeploymentError("Invalid workflow run id")
        attempt = data["workflow_run_attempt"]
        if type(attempt) is not int or not 1 <= attempt <= 1000:
            raise DeploymentError("Invalid workflow run attempt")
        return cls(
            mode=data["mode"],
            repository=data["repository"],
            release_commit=data["release_commit"],
            release_ref=data["release_ref"],
            platform_version=version,
            manifest_sha256=data["manifest_sha256"],
            workflow_run_id=run_id,
            workflow_run_attempt=attempt,
        )


@dataclass(frozen=True)
class ValidatedBundle:
    root: Path
    release_dir: Path
    request: DeploymentRequest
    manifest: Mapping[str, Any]
    install_plan: Mapping[str, Any]


def read_bounded_stream(stream: BinaryIO, maximum: int) -> bytes:
    data = stream.read(maximum + 1)
    if not data:
        raise DeploymentError("Deployment archive is empty")
    if len(data) > maximum:
        raise DeploymentError("Deployment archive exceeds the configured limit")
    return data


def extract_archive(data: bytes, destination: Path) -> None:
    """Extract the tiny USTAR payload without trusting tarfile.extract()."""

    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise DeploymentError("Deployment archive is not a valid gzip tar") from exc
    with archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise DeploymentError("Deployment archive has an unsafe member count")
        seen: set[str] = set()
        expanded = 0
        for member in members:
            if member.pax_headers:
                raise DeploymentError("Extended tar headers are not allowed")
            name = member.name
            pure = PurePosixPath(name)
            normalized = unicodedata.normalize("NFC", name)
            folded = normalized.casefold()
            if (
                normalized != name
                or name.startswith("/")
                or "\\" in name
                or ".." in pure.parts
                or not pure.parts
                or folded in seen
                or len(name) > 300
            ):
                raise DeploymentError(f"Unsafe archive member: {name!r}")
            seen.add(folded)
            if pure.parts[0] not in {"REQUEST.json", "release"}:
                raise DeploymentError(f"Undeclared archive root: {name!r}")
            if pure.parts[0] == "REQUEST.json" and len(pure.parts) != 1:
                raise DeploymentError("REQUEST.json must be at the archive root")
            if pure.parts[0] == "release" and len(pure.parts) > 2:
                raise DeploymentError("Nested release paths are not allowed")
            if member.isdir():
                if pure.parts != ("release",):
                    raise DeploymentError("Only the release directory may be a directory")
                continue
            if not member.isreg() or member.issym() or member.islnk():
                raise DeploymentError(f"Archive member is not a regular file: {name}")
            if member.size <= 0 or member.size > MAX_FILE_BYTES:
                raise DeploymentError(f"Archive member has an unsafe size: {name}")
            expanded += member.size
            if expanded > MAX_EXPANDED_BYTES:
                raise DeploymentError("Deployment archive expands beyond its safe limit")
            source = archive.extractfile(member)
            if source is None:
                raise DeploymentError(f"Could not read archive member: {name}")
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise DeploymentError(f"Archive member ended early: {name}")
                    output.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise DeploymentError(f"Archive member exceeds its declared size: {name}")
            target.chmod(0o600 if name == "REQUEST.json" else 0o644)

    if not (destination / "REQUEST.json").is_file():
        raise DeploymentError("Deployment archive lacks REQUEST.json")
    if not (destination / "release").is_dir():
        raise DeploymentError("Deployment archive lacks its release directory")


def load_validated_bundle(root: Path, config: ReceiverConfig) -> ValidatedBundle:
    request = DeploymentRequest.load(root / "REQUEST.json")
    if request.repository != config.repository:
        raise DeploymentError("Deployment request targets the wrong repository")
    release_dir = root / "release"

    # This module is installed alongside the trusted v1 release validator. The
    # archive never supplies executable validation code.
    try:
        from ops.release import buh_release
    except ImportError as exc:  # pragma: no cover - host installation failure
        raise DeploymentError("Trusted release validator is unavailable") from exc
    try:
        manifest = buh_release.verify_release_dir(release_dir)
    except Exception as exc:
        raise DeploymentError(f"Release bundle validation failed: {exc}") from exc
    if sha256_file(release_dir / "RELEASE.json") != request.manifest_sha256:
        raise DeploymentError("Deployment request manifest digest is incorrect")
    if manifest.get("platform_version") != request.platform_version:
        raise DeploymentError("Request and release platform versions disagree")
    if manifest.get("platform_id") != "buh-allianceauth":
        raise DeploymentError("Release manifest has the wrong platform id")
    try:
        install_plan = json.loads((release_dir / "INSTALL_PLAN.json").read_text("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("Could not load the verified install plan") from exc
    return ValidatedBundle(
        root=root,
        release_dir=release_dir,
        request=request,
        manifest=manifest,
        install_plan=install_plan,
    )
