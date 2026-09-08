"""Docker Compose host adapter for an AllianceAuth Platform v2 deployment."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import select
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

from ops.release import recovery_policy

from .contracts import (
    FATAL_LOG_ALLOWLIST_RE,
    IMAGE_DIGEST_RE,
    RECEIVER_CONFIG_SCHEMA_VERSION,
    SHA256_RE,
    DeploymentError,
    ReceiverConfig,
    ValidatedBundle,
    canonical_json_bytes,
    redact_sensitive_text,
    sha256_file,
)


BEGIN_V2 = "# BEGIN B-UH PLATFORM V2"
END_V2 = "# END B-UH PLATFORM V2"
BEGIN_LEGACY = "# BEGIN B-UH MOON TAX PLATFORM"
END_LEGACY = "# END B-UH MOON TAX PLATFORM"
FATAL_LOG_RE = re.compile(
    r"(?:^|\W)(?:ERROR|CRITICAL|Traceback|PermissionError|permission denied)"
    r"(?:\W|$)|ModuleNotFoundError|Worker failed to boot|ImproperlyConfigured|"
    r"ImportError:|SyntaxError:|django\.db\.migrations\.exceptions|"
    r"(?:\b403\s+Forbidden\b|\berror\s+code:\s*50013\b|"
    r"\bMissing\s+Permissions\b)|"
    r"(?:restart(?:ing|ed)?\s+(?:too\s+)?(?:often|repeatedly))",
    re.IGNORECASE,
)
SAFE_DATABASE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SAFE_COMPOSE_PATH_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9._/-]{0,254}$")
SAFE_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_IMAGE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,511}$")
MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
MAX_COMPOSE_FILES = 8
MAX_SERVICE_REPLICAS = 128
BACKUP_LOCK_SECONDS = 120
BACKUP_LOCK_WAIT_SECONDS = 15
NGINX_UPSTREAM_NAME = "buh_platform_v2_active"
NGINX_PRODUCTION_HOST = "auth.b-uh.com"
STATIC_HEALTH_ASSET = "admin/css/base.css"
MAX_RETAINED_LOG_FINDINGS = 32
MAX_RETAINED_LOG_FINDING_CHARS = 200
MAX_STATIC_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_STATIC_MANIFEST_ENTRIES = 100_000
MAX_STATIC_ASSET_BYTES = 512 * 1024 * 1024
MAX_STATIC_FILE_BYTES = 64 * 1024 * 1024
MAX_STATIC_ASSET_BACKUP_BYTES = (
    MAX_STATIC_ASSET_BYTES + (MAX_STATIC_MANIFEST_ENTRIES + 2) * 2048
)
ALLOWLISTED_LOG_LEVEL_RE = (
    r"(?:(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9:.+-]{8,31}Z?)[ \t]+)?"
    r"(?:\[(?:WARNING|ERROR|CRITICAL)\]|(?:WARNING|ERROR|CRITICAL))"
    r"(?:[ \t]+|:[ \t]*)"
)

IMAGE_PROVENANCE_LABELS = {
    "platform_version": "com.b-uh.platform.version",
    "source_commit": "com.b-uh.platform.source",
    "release_commit": "com.b-uh.platform.release",
    "manifest_sha256": "com.b-uh.platform.manifest",
    "base_digest": "com.b-uh.platform.base-digest",
}
RECOVERY_PLAN_SCHEMA_VERSION = 1
RECOVERY_PLAN_NAME = "active-recovery.json"
MAX_RECOVERY_PLAN_BYTES = 256 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _tokenize_nginx_configuration(configuration: str) -> tuple[str, ...]:
    """Tokenize enough of validated ``nginx -T`` output to inspect scopes."""

    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    comment = False

    def flush() -> None:
        if current:
            tokens.append("".join(current))
            current.clear()

    for character in configuration:
        if comment:
            if character in "\r\n":
                comment = False
            continue
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote is not None:
            if character == quote:
                quote = None
            else:
                current.append(character)
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "#":
            flush()
            comment = True
            continue
        if character.isspace():
            flush()
            continue
        if character in "{};":
            flush()
            tokens.append(character)
            continue
        current.append(character)
    if quote is not None or escaped:
        raise DeploymentError("Nginx configuration tokenization was incomplete")
    flush()
    return tuple(tokens)


def _parse_nginx_configuration(
    configuration: str,
) -> tuple[tuple[tuple[str, ...], tuple[Any, ...] | None], ...]:
    """Build a small directive tree from syntax already accepted by Nginx."""

    tokens = _tokenize_nginx_configuration(configuration)

    def parse_scope(
        index: int, *, nested: bool
    ) -> tuple[list[tuple[tuple[str, ...], tuple[Any, ...] | None]], int]:
        nodes: list[tuple[tuple[str, ...], tuple[Any, ...] | None]] = []
        header: list[str] = []
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token == ";":
                if header:
                    nodes.append((tuple(header), None))
                    header.clear()
                continue
            if token == "{":
                if not header:
                    raise DeploymentError("Nginx configuration has an anonymous scope")
                children, index = parse_scope(index, nested=True)
                nodes.append((tuple(header), tuple(children)))
                header.clear()
                continue
            if token == "}":
                if not nested or header:
                    raise DeploymentError("Nginx configuration scopes are malformed")
                return nodes, index
            header.append(token)
        if nested:
            raise DeploymentError("Nginx configuration contains an unterminated scope")
        # ``nginx -T`` writes its final success diagnostic to stderr without a
        # semicolon. Since stderr is deliberately merged, ignore only that
        # trailing, non-directive text at top level.
        return nodes, index

    nodes, _index = parse_scope(0, nested=False)
    return tuple(nodes)


def _walk_nginx_nodes(
    nodes: Sequence[tuple[tuple[str, ...], tuple[Any, ...] | None]],
) -> Iterator[tuple[tuple[str, ...], tuple[Any, ...] | None]]:
    for node in nodes:
        yield node
        if node[1] is not None:
            yield from _walk_nginx_nodes(node[1])


def _verify_nginx_production_route(configuration: str) -> None:
    """Prove the production virtual host's catch-all uses the managed upstream."""

    nodes = _parse_nginx_configuration(configuration)
    upstream_header = ("upstream", NGINX_UPSTREAM_NAME)
    expected_proxy = ("proxy_pass", f"http://{NGINX_UPSTREAM_NAME}")
    all_nodes = tuple(_walk_nginx_nodes(nodes))
    if sum(node[0] == upstream_header for node in all_nodes) != 1:
        raise DeploymentError("Nginx does not define exactly one managed upstream")
    if sum(node[0] == expected_proxy for node in all_nodes) != 1:
        raise DeploymentError("Nginx does not use exactly one managed proxy_pass")

    auth_servers = []
    for header, children in all_nodes:
        if header != ("server",) or children is None:
            continue
        names = {
            name
            for child_header, child_children in children
            if child_children is None
            and child_header[:1] == ("server_name",)
            for name in child_header[1:]
        }
        if NGINX_PRODUCTION_HOST in names:
            auth_servers.append(children)
    if not auth_servers:
        raise DeploymentError("Nginx has no server_name for the production Auth host")

    managed_root_locations = 0
    production_proxies: list[tuple[str, ...]] = []
    for children in auth_servers:
        production_proxies.extend(
            header
            for header, child_children in _walk_nginx_nodes(children)
            if child_children is None and header[:1] == ("proxy_pass",)
        )
        for location_header, location_children in children:
            if location_children is None or location_header not in {
                ("location", "/"),
                ("location", "^~", "/"),
            }:
                continue
            direct_proxies = [
                header
                for header, child_children in location_children
                if child_children is None and header[:1] == ("proxy_pass",)
            ]
            if direct_proxies == [expected_proxy]:
                managed_root_locations += 1
    if production_proxies != [expected_proxy] or managed_root_locations != 1:
        raise DeploymentError(
            "The production Auth route does not exclusively use the managed upstream"
        )


def _semver(value: str) -> tuple[int, int, int]:
    try:
        result = tuple(int(part) for part in value.split("."))
    except (AttributeError, ValueError) as exc:
        raise DeploymentError(f"Invalid platform version: {value!r}") from exc
    if len(result) != 3:
        raise DeploymentError(f"Invalid platform version: {value!r}")
    return result  # type: ignore[return-value]


def _safe_error_output(output: str) -> str:
    output = redact_sensitive_text(output)
    lines = []
    for raw_line in output.splitlines()[-80:]:
        line = raw_line[:500]
        lines.append(line)
    return "\n".join(lines)


def _safe_report_text(output: str, maximum: int) -> str:
    """Return one bounded, redacted ASCII field for retained JSON evidence."""

    safe = _safe_error_output(output).replace("\n", " ")
    printable = "".join(
        character if 32 <= ord(character) <= 126 else "?" for character in safe
    )
    return printable[:maximum]


def _atomic_bytes(
    path: Path,
    data: bytes,
    mode: int,
    *,
    owner: tuple[int, int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        if owner is not None and hasattr(os, "fchown"):
            details = os.fstat(descriptor)
            if (details.st_uid, details.st_gid) != owner:
                os.fchown(descriptor, *owner)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:  # pragma: no cover - Windows-only local contract tests
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_text(
    path: Path,
    text: str,
    mode: int,
    *,
    owner: tuple[int, int] | None = None,
) -> None:
    _atomic_bytes(path, text.encode("utf-8"), mode, owner=owner)


class DockerHost:
    """Apply a verified release to the existing `/opt/aa-docker` layout."""

    def __init__(self, config: ReceiverConfig):
        self.config = config
        self.backup_path: Path | None = None
        self.original_dockerfile: Path | None = None
        self.original_local_settings: Path | None = None
        self.original_local_settings_metadata: tuple[int, int, int] | None = None
        self.staged_release: Path | None = None
        self.previous_images: dict[str, tuple[str, str]] = {}
        self.previous_image_pins: dict[str, str] = {}
        self.candidate_image_ids: dict[str, str] = {}
        self.auth_replica_counts: dict[str, int] = {}
        self.restart_baselines: dict[str, int] = {}
        self.live_replacement_started = False
        self.original_upstream: Path | None = None
        self.original_upstream_metadata: tuple[int, int, int] | None = None
        self.original_platform_current: Path | None = None
        self.original_platform_current_metadata: tuple[int, int, int] | None = None
        self.platform_current_existed: bool | None = None
        self.original_deployment_current: Path | None = None
        self.original_deployment_current_metadata: tuple[int, int, int] | None = None
        self.deployment_current_existed: bool | None = None
        self.platform_current_write_started = False
        self.static_manifest_path: str | None = None
        self.static_manifest_backup: Path | None = None
        self.static_manifest_sha256: str | None = None
        self.static_manifest_entries: int | None = None
        self.static_manifest_metadata: tuple[int, int, int] | None = None
        self.static_assets_sha256: str | None = None
        self.static_assets_count: int | None = None
        self.static_assets_bytes: int | None = None
        self.static_root_path: str | None = None
        self.static_assets_backup: Path | None = None
        self.static_assets_backup_sha256: str | None = None
        self.static_collection_started = False
        self.candidate_web_slots: tuple[str, ...] = ()
        self.previous_web_slots: tuple[str, ...] = ()
        self.traffic_switch_started = False
        self.workers_replacement_started = False
        self.gunicorn_replacement_started = False
        self.migration_started = False
        self.log_since = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._last_stabilization_evidence: Mapping[str, Any] | None = None
        self.recovery_plan_armed = False
        self.recovery_baseline_verified = False

    def _recovery_plan_value(self, attempt_id: str, phase: str) -> dict[str, Any]:
        if (
            self.backup_path is None
            or self.original_dockerfile is None
            or self.original_local_settings is None
            or self.original_local_settings_metadata is None
            or not re.fullmatch(r"gh-[1-9][0-9]{0,19}-[1-9][0-9]{0,3}", attempt_id)
        ):
            raise DeploymentError("Deployment recovery evidence is incomplete")

        backup_files = {
            path.name: sha256_file(path)
            for path in (
                self.original_dockerfile,
                self.original_local_settings,
                self.original_upstream,
                self.original_platform_current,
                self.original_deployment_current,
                self.static_manifest_backup,
                self.static_assets_backup,
            )
            if path is not None
        }
        return {
            "schema_version": RECOVERY_PLAN_SCHEMA_VERSION,
            "status": "active",
            "attempt_id": attempt_id,
            "phase": phase,
            "backup_path": str(self.backup_path),
            "backup_files": backup_files,
            "previous_images": {
                service: [image, reference]
                for service, (image, reference) in self.previous_images.items()
            },
            "previous_image_pins": dict(self.previous_image_pins),
            "auth_replica_counts": dict(self.auth_replica_counts),
            "previous_web_slots": list(self.previous_web_slots),
            "candidate_web_slots": list(self.candidate_web_slots),
            "original_local_settings_metadata": list(
                self.original_local_settings_metadata
            ),
            "original_upstream_metadata": (
                list(self.original_upstream_metadata)
                if self.original_upstream_metadata is not None
                else None
            ),
            "platform_current_existed": self.platform_current_existed,
            "original_platform_current_metadata": (
                list(self.original_platform_current_metadata)
                if self.original_platform_current_metadata is not None
                else None
            ),
            "deployment_current_existed": self.deployment_current_existed,
            "original_deployment_current_metadata": (
                list(self.original_deployment_current_metadata)
                if self.original_deployment_current_metadata is not None
                else None
            ),
            "static_root_path": self.static_root_path,
            "static_manifest_path": self.static_manifest_path,
            "static_manifest_sha256": self.static_manifest_sha256,
            "static_manifest_entries": self.static_manifest_entries,
            "static_manifest_metadata": (
                list(self.static_manifest_metadata)
                if self.static_manifest_metadata is not None
                else None
            ),
            "static_assets_sha256": self.static_assets_sha256,
            "static_assets_count": self.static_assets_count,
            "static_assets_bytes": self.static_assets_bytes,
            "static_assets_backup_sha256": self.static_assets_backup_sha256,
            "flags": {
                "static_collection_started": self.static_collection_started,
                "traffic_switch_started": self.traffic_switch_started,
                "workers_replacement_started": self.workers_replacement_started,
                "gunicorn_replacement_started": self.gunicorn_replacement_started,
                "migration_started": self.migration_started,
                "platform_current_write_started": self.platform_current_write_started,
            },
        }

    def _save_recovery_plan(self, attempt_id: str, phase: str) -> None:
        """Durably arm restart recovery before the next mutable operation."""

        value = self._recovery_plan_value(attempt_id, phase)
        encoded = canonical_json_bytes(value)
        if len(encoded) > MAX_RECOVERY_PLAN_BYTES:
            raise DeploymentError("Deployment recovery plan exceeds its safe limit")
        active = self.config.state_dir / RECOVERY_PLAN_NAME
        _atomic_bytes(active, encoded, 0o600)
        assert self.backup_path is not None
        _atomic_bytes(self.backup_path / "RECOVERY.json", encoded, 0o600)
        self.recovery_plan_armed = True

    def _update_recovery_plan(self, phase: str) -> None:
        if not self.recovery_plan_armed:
            return
        active = self.config.state_dir / RECOVERY_PLAN_NAME
        if not active.is_file() or active.is_symlink():
            raise DeploymentError("Active deployment recovery plan is unavailable")
        try:
            value = json.loads(active.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("Active deployment recovery plan is unreadable") from exc
        attempt_id = value.get("attempt_id") if isinstance(value, dict) else None
        if not isinstance(attempt_id, str):
            raise DeploymentError("Active deployment recovery plan is malformed")
        self._save_recovery_plan(attempt_id, phase)

    def complete_recovery_plan(self) -> None:
        """Disarm restart recovery only after restore or verified publication."""

        active = self.config.state_dir / RECOVERY_PLAN_NAME
        try:
            details = active.lstat()
        except FileNotFoundError:
            self.recovery_plan_armed = False
            return
        if not stat.S_ISREG(details.st_mode) or active.is_symlink():
            raise DeploymentError("Active deployment recovery plan became unsafe")
        active.unlink()
        self.recovery_plan_armed = False
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(active.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    @classmethod
    def recover_incomplete_plan(cls, config: ReceiverConfig) -> str | None:
        """Consume one durable plan after a prior receiver died mid-attempt."""

        active = config.state_dir / RECOVERY_PLAN_NAME
        try:
            details = active.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(details.st_mode)
            or active.is_symlink()
            or details.st_size > MAX_RECOVERY_PLAN_BYTES
            or (os.name != "nt" and (details.st_uid != 0 or stat.S_IMODE(details.st_mode) != 0o600))
        ):
            raise DeploymentError("Active deployment recovery plan is unsafe")
        try:
            value = json.loads(active.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("Active deployment recovery plan is unreadable") from exc
        expected_keys = {
            "schema_version", "status", "attempt_id", "phase", "backup_path",
            "backup_files", "previous_images", "previous_image_pins",
            "auth_replica_counts", "previous_web_slots", "candidate_web_slots",
            "original_local_settings_metadata", "original_upstream_metadata",
            "platform_current_existed", "original_platform_current_metadata",
            "deployment_current_existed", "original_deployment_current_metadata",
            "static_root_path", "static_manifest_path", "static_manifest_sha256",
            "static_manifest_entries", "static_manifest_metadata",
            "static_assets_sha256", "static_assets_count", "static_assets_bytes",
            "static_assets_backup_sha256", "flags",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected_keys
            or value["schema_version"] != RECOVERY_PLAN_SCHEMA_VERSION
            or value["status"] != "active"
            or not isinstance(value["attempt_id"], str)
            or re.fullmatch(r"gh-[1-9][0-9]{0,19}-[1-9][0-9]{0,3}", value["attempt_id"])
            is None
            or not isinstance(value["phase"], str)
        ):
            raise DeploymentError("Active deployment recovery plan is malformed")
        attempt_id = value["attempt_id"]
        backup = Path(value["backup_path"])
        if backup != config.backup_dir / attempt_id or not backup.is_dir() or backup.is_symlink():
            raise DeploymentError("Deployment recovery backup identity is invalid")
        filenames = {
            "custom.dockerfile", "local.py", "nginx-upstream.conf",
            "platform-CURRENT.json", "deployment-current.json",
            "staticfiles.previous.json", "static-assets.previous.tar",
        }
        backup_files = value["backup_files"]
        required_backup_files = {
            "custom.dockerfile",
            "local.py",
            "staticfiles.previous.json",
            "static-assets.previous.tar",
        }
        if config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            required_backup_files.add("nginx-upstream.conf")
        if (
            not isinstance(backup_files, dict)
            or not required_backup_files <= set(backup_files)
            or not set(backup_files) <= filenames
            or any(
                not isinstance(digest, str)
                or SHA256_RE.fullmatch(digest) is None
                or not (backup / name).is_file()
                or (backup / name).is_symlink()
                or sha256_file(backup / name) != digest
                for name, digest in backup_files.items()
            )
        ):
            raise DeploymentError("Deployment recovery backup evidence is invalid")
        services = set(config.auth_services)
        previous_images = value["previous_images"]
        pins = value["previous_image_pins"]
        counts = value["auth_replica_counts"]
        if (
            not isinstance(previous_images, dict)
            or set(previous_images) != services
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or not SAFE_IMAGE_ID_RE.fullmatch(item[0])
                or not SAFE_IMAGE_REFERENCE_RE.fullmatch(item[1])
                for item in previous_images.values()
            )
            or not isinstance(pins, dict)
            or set(pins) != services
            or any(not SAFE_IMAGE_REFERENCE_RE.fullmatch(item) for item in pins.values())
            or not isinstance(counts, dict)
            or set(counts) != services
            or any(type(item) is not int or not 1 <= item <= MAX_SERVICE_REPLICAS for item in counts.values())
        ):
            raise DeploymentError("Deployment recovery topology is invalid")
        slots: dict[str, tuple[str, ...]] = {}
        for field in ("previous_web_slots", "candidate_web_slots"):
            items = value[field]
            if (
                not isinstance(items, list)
                or len(items) > MAX_SERVICE_REPLICAS
                or any(not isinstance(item, str) for item in items)
            ):
                raise DeploymentError("Deployment recovery web slots are invalid")
            slots[field] = tuple(cls._safe_container_name(item) for item in items)
        flags = value["flags"]
        flag_names = {
            "static_collection_started", "traffic_switch_started",
            "workers_replacement_started", "gunicorn_replacement_started",
            "migration_started", "platform_current_write_started",
        }
        if not isinstance(flags, dict) or set(flags) != flag_names or any(type(item) is not bool for item in flags.values()):
            raise DeploymentError("Deployment recovery flags are invalid")
        if (
            type(value["platform_current_existed"]) is not bool
            or type(value["deployment_current_existed"]) is not bool
            or not isinstance(value["static_root_path"], str)
            or not re.fullmatch(r"/[A-Za-z0-9._/-]{1,254}", value["static_root_path"])
            or ".." in PurePosixPath(value["static_root_path"]).parts
            or not isinstance(value["static_manifest_path"], str)
            or not value["static_manifest_path"].startswith(
                value["static_root_path"].rstrip("/") + "/"
            )
            or not isinstance(value["static_manifest_sha256"], str)
            or SHA256_RE.fullmatch(value["static_manifest_sha256"]) is None
            or value["static_manifest_sha256"]
            != backup_files.get("staticfiles.previous.json")
            or not isinstance(value["static_assets_sha256"], str)
            or SHA256_RE.fullmatch(value["static_assets_sha256"]) is None
            or value["static_assets_backup_sha256"]
            != backup_files.get("static-assets.previous.tar")
            or type(value["static_manifest_entries"]) is not int
            or not 1 <= value["static_manifest_entries"] <= MAX_STATIC_MANIFEST_ENTRIES
            or type(value["static_assets_count"]) is not int
            or not 1 <= value["static_assets_count"] <= value["static_manifest_entries"]
            or type(value["static_assets_bytes"]) is not int
            or not 0 <= value["static_assets_bytes"] <= MAX_STATIC_ASSET_BYTES
        ):
            raise DeploymentError("Deployment recovery static evidence is invalid")

        def metadata(name: str) -> tuple[int, int, int] | None:
            item = value[name]
            if item is None:
                return None
            if (
                not isinstance(item, list)
                or len(item) != 3
                or any(type(part) is not int or part < 0 for part in item)
                or item[0] > 2**31 - 1
                or item[1] > 2**31 - 1
                or item[2] > 0o777
            ):
                raise DeploymentError("Deployment recovery metadata is invalid")
            return tuple(item)  # type: ignore[return-value]

        local_metadata = metadata("original_local_settings_metadata")
        upstream_metadata = metadata("original_upstream_metadata")
        platform_metadata = metadata("original_platform_current_metadata")
        deployment_metadata = metadata("original_deployment_current_metadata")
        static_metadata = metadata("static_manifest_metadata")
        if (
            local_metadata is None
            or static_metadata is None
            or (
                config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION
                and upstream_metadata is None
            )
            or value["platform_current_existed"]
            != ("platform-CURRENT.json" in backup_files)
            or value["platform_current_existed"] != (platform_metadata is not None)
            or value["deployment_current_existed"]
            != ("deployment-current.json" in backup_files)
            or value["deployment_current_existed"] != (deployment_metadata is not None)
        ):
            raise DeploymentError("Deployment recovery file metadata is incomplete")

        host = cls(config)
        host.recovery_plan_armed = True
        host.backup_path = backup
        host.original_dockerfile = backup / "custom.dockerfile"
        host.original_local_settings = backup / "local.py"
        host.original_local_settings_metadata = local_metadata
        if "nginx-upstream.conf" in backup_files:
            host.original_upstream = backup / "nginx-upstream.conf"
        host.original_upstream_metadata = upstream_metadata
        host.platform_current_existed = value["platform_current_existed"]
        if "platform-CURRENT.json" in backup_files:
            host.original_platform_current = backup / "platform-CURRENT.json"
        host.original_platform_current_metadata = platform_metadata
        host.deployment_current_existed = value["deployment_current_existed"]
        if "deployment-current.json" in backup_files:
            host.original_deployment_current = backup / "deployment-current.json"
        host.original_deployment_current_metadata = deployment_metadata
        host.previous_images = {name: tuple(item) for name, item in previous_images.items()}  # type: ignore[misc]
        host.previous_image_pins = dict(pins)
        host.auth_replica_counts = dict(counts)
        host.previous_web_slots = slots["previous_web_slots"]
        host.candidate_web_slots = slots["candidate_web_slots"]
        host.static_root_path = value["static_root_path"]
        host.static_manifest_path = value["static_manifest_path"]
        host.static_manifest_sha256 = value["static_manifest_sha256"]
        host.static_manifest_entries = value["static_manifest_entries"]
        host.static_manifest_metadata = static_metadata
        host.static_assets_sha256 = value["static_assets_sha256"]
        host.static_assets_count = value["static_assets_count"]
        host.static_assets_bytes = value["static_assets_bytes"]
        host.static_assets_backup_sha256 = value["static_assets_backup_sha256"]
        if "staticfiles.previous.json" in backup_files:
            host.static_manifest_backup = backup / "staticfiles.previous.json"
        if "static-assets.previous.tar" in backup_files:
            host.static_assets_backup = backup / "static-assets.previous.tar"
        for name, item in flags.items():
            setattr(host, name, item)

        attempt_path = config.state_dir / "attempts" / f"{attempt_id}.json"
        if attempt_path.is_file() and not attempt_path.is_symlink():
            try:
                attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                attempt = None
            if (
                isinstance(attempt, dict)
                and attempt.get("result") == "success"
                and attempt.get("state") == "verified"
            ):
                host.complete_recovery_plan()
                return "A verified prior attempt left only a stale recovery marker."
        recovery = host.rollback(None, value["phase"])  # type: ignore[arg-type]
        return "Recovered an incomplete prior deployment before accepting new input: " + recovery

    @property
    def compose_prefix(self) -> list[str]:
        prefix = [
            "docker",
            "compose",
            "--env-file",
            str(self.config.env_file),
        ]
        for compose_file in self._compose_files():
            prefix.extend(("-f", str(compose_file)))
        return prefix

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: int | None = None,
        bounded_output: bool = False,
        context: str,
    ) -> str:
        if bounded_output:
            if input_bytes is not None:
                raise DeploymentError(f"{context} cannot use bounded output with input")
            return self._run_bounded_output(arguments, timeout=timeout, context=context)
        try:
            result = subprocess.run(
                list(arguments),
                cwd=self.config.app_dir,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout or self.config.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"{context} could not complete") from exc
        output_bytes = result.stdout or b""
        if len(output_bytes) > MAX_COMMAND_OUTPUT:
            output_bytes = output_bytes[-MAX_COMMAND_OUTPUT:]
        output = output_bytes.decode("utf-8", errors="replace")
        if result.returncode != 0:
            safe = _safe_error_output(output)
            suffix = f"\n{safe}" if safe else ""
            raise DeploymentError(f"{context} failed with exit {result.returncode}{suffix}")
        return output

    def _run_bounded_output(
        self,
        arguments: Sequence[str],
        *,
        timeout: int | None,
        context: str,
    ) -> str:
        """Consume output incrementally and fail instead of dropping early bytes."""

        try:
            process = subprocess.Popen(
                list(arguments),
                cwd=self.config.app_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise DeploymentError(f"{context} could not start") from exc
        assert process.stdout is not None
        chunks: list[bytes] = []
        size = 0
        overflow = threading.Event()
        read_error: list[OSError] = []

        def consume() -> None:
            nonlocal size
            try:
                while chunk := process.stdout.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_COMMAND_OUTPUT:
                        overflow.set()
                        process.kill()
                        return
                    chunks.append(chunk)
            except OSError as exc:
                read_error.append(exc)

        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        try:
            returncode = process.wait(
                timeout=timeout or self.config.command_timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=5)
            raise DeploymentError(f"{context} could not complete") from exc
        finally:
            reader.join(timeout=5)
            if reader.is_alive():
                process.kill()
                process.stdout.close()
                reader.join(timeout=5)
        if reader.is_alive() or read_error:
            raise DeploymentError(f"{context} output could not be read safely")
        if overflow.is_set():
            raise DeploymentError(f"{context} exceeded the bounded output limit")
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if returncode != 0:
            safe = _safe_error_output(output)
            suffix = f"\n{safe}" if safe else ""
            raise DeploymentError(f"{context} failed with exit {returncode}{suffix}")
        return output

    def _run_to_file(
        self, arguments: Sequence[str], destination: Path, *, context: str
    ) -> None:
        try:
            with destination.open("xb") as output:
                result = subprocess.run(
                    list(arguments),
                    cwd=self.config.app_dir,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=self.config.command_timeout_seconds,
                    check=False,
                )
                output.flush()
                os.fsync(output.fileno())
        except (OSError, subprocess.TimeoutExpired) as exc:
            destination.unlink(missing_ok=True)
            raise DeploymentError(f"{context} could not complete") from exc
        if result.returncode != 0:
            destination.unlink(missing_ok=True)
            error = _safe_error_output(
                (result.stderr or b"").decode("utf-8", errors="replace")
            )
            raise DeploymentError(
                f"{context} failed with exit {result.returncode}"
                + (f"\n{error}" if error else "")
            )

    def _run_from_file(
        self, arguments: Sequence[str], source: Path, *, context: str
    ) -> None:
        try:
            with source.open("rb") as input_stream:
                result = subprocess.run(
                    list(arguments),
                    cwd=self.config.app_dir,
                    stdin=input_stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.config.command_timeout_seconds,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"{context} could not complete") from exc
        if result.returncode != 0:
            output = _safe_error_output(
                (result.stdout or b"").decode("utf-8", errors="replace")
            )
            raise DeploymentError(
                f"{context} failed with exit {result.returncode}"
                + (f"\n{output}" if output else "")
            )

    def _compose(
        self,
        *arguments: str,
        context: str,
        timeout: int | None = None,
        bounded_output: bool = False,
    ) -> str:
        return self._run(
            [*self.compose_prefix, *arguments],
            timeout=timeout,
            bounded_output=bounded_output,
            context=context,
        )

    def _proxy_exec(
        self, *arguments: str, context: str, bounded_output: bool = False
    ) -> str:
        return self._compose(
            "exec",
            "-T",
            self.config.proxy_service,
            *arguments,
            bounded_output=bounded_output,
            context=context,
        )

    @staticmethod
    def _safe_container_name(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise DeploymentError("Generated web-slot name is unsafe")
        return value

    def _upstream_path(self) -> Path:
        return self.config.app_dir / self.config.nginx_upstream_file

    def _platform_current_path(self) -> Path:
        return self.config.app_dir / "conf" / "buh-platform-v2" / "CURRENT.json"

    def _capture_platform_current(self) -> None:
        if self.backup_path is None:
            raise DeploymentError("Configuration backup was not prepared")
        path = self._platform_current_path()
        try:
            details = path.lstat()
        except FileNotFoundError:
            self.platform_current_existed = False
            self.original_platform_current = None
            self.original_platform_current_metadata = None
            return
        if (
            not stat.S_ISREG(details.st_mode)
            or path.is_symlink()
            or details.st_size > 16 * 1024
        ):
            raise DeploymentError("Platform CURRENT marker is unsafe")
        backup = self.backup_path / "platform-CURRENT.json"
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        self.platform_current_existed = True
        self.original_platform_current = backup
        self.original_platform_current_metadata = (
            details.st_uid,
            details.st_gid,
            stat.S_IMODE(details.st_mode),
        )

    def _restore_platform_current(self) -> None:
        if (
            not self.platform_current_write_started
            or self.platform_current_existed is None
        ):
            return
        path = self._platform_current_path()
        if self.platform_current_existed:
            if (
                self.original_platform_current is None
                or self.original_platform_current_metadata is None
            ):
                raise DeploymentError("Original Platform CURRENT marker was not captured")
            data = self.original_platform_current.read_bytes()
            owner = self.original_platform_current_metadata[:2]
            mode = self.original_platform_current_metadata[2]
            _atomic_bytes(path, data, mode, owner=owner)
            return
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode) or path.is_symlink():
            raise DeploymentError("New Platform CURRENT marker is unsafe to remove")
        path.unlink()
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _capture_deployment_current(self) -> None:
        """Capture the lineage pointer in case journal publication is uncertain."""

        if self.backup_path is None:
            raise DeploymentError("Configuration backup was not prepared")
        path = self.config.state_dir / "current.json"
        try:
            details = path.lstat()
        except FileNotFoundError:
            self.deployment_current_existed = False
            self.original_deployment_current = None
            self.original_deployment_current_metadata = None
            return
        if (
            not stat.S_ISREG(details.st_mode)
            or path.is_symlink()
            or not 1 <= details.st_size <= 16 * 1024
        ):
            raise DeploymentError("Deployment current marker is unsafe")
        backup = self.backup_path / "deployment-current.json"
        shutil.copy2(path, backup)
        backup.chmod(0o600)
        self.deployment_current_existed = True
        self.original_deployment_current = backup
        self.original_deployment_current_metadata = (
            details.st_uid,
            details.st_gid,
            stat.S_IMODE(details.st_mode),
        )

    def _restore_deployment_current(self) -> None:
        if (
            not self.platform_current_write_started
            or self.deployment_current_existed is None
        ):
            return
        path = self.config.state_dir / "current.json"
        if self.deployment_current_existed:
            if (
                self.original_deployment_current is None
                or self.original_deployment_current_metadata is None
            ):
                raise DeploymentError(
                    "Original deployment current marker was not captured"
                )
            data = self.original_deployment_current.read_bytes()
            owner = self.original_deployment_current_metadata[:2]
            mode = self.original_deployment_current_metadata[2]
            try:
                details = path.lstat()
            except FileNotFoundError:
                details = None
            if details is not None and (
                not stat.S_ISREG(details.st_mode) or path.is_symlink()
            ):
                raise DeploymentError("Deployment current marker became unsafe")
            if (
                details is not None
                and path.read_bytes() == data
                and (details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode))
                == (*owner, mode)
            ):
                return
            _atomic_bytes(path, data, mode, owner=owner)
            return
        try:
            details = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(details.st_mode) or path.is_symlink():
            raise DeploymentError("New deployment current marker is unsafe to remove")
        path.unlink()
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _render_upstream(
        self,
        targets: Sequence[str],
        *,
        backup_targets: Sequence[str] = (),
    ) -> str:
        if (
            not targets
            or len(targets) + len(backup_targets) > MAX_SERVICE_REPLICAS
        ):
            raise DeploymentError("Nginx upstream must contain a bounded target set")
        safe_targets = tuple(self._safe_container_name(target) for target in targets)
        safe_backups = tuple(
            self._safe_container_name(target) for target in backup_targets
        )
        all_targets = (*safe_targets, *safe_backups)
        if len(set(all_targets)) != len(all_targets):
            raise DeploymentError("Nginx upstream contains duplicate targets")
        lines = [
            "# Managed by the B-UH Platform v2 receiver.",
            f"upstream {NGINX_UPSTREAM_NAME} {{",
            "    least_conn;",
        ]
        lines.extend(
            f"    server {target}:{self.config.gunicorn_port} "
            "max_fails=1 fail_timeout=5s;"
            for target in safe_targets
        )
        lines.extend(
            f"    server {target}:{self.config.gunicorn_port} "
            "max_fails=1 fail_timeout=5s backup;"
            for target in safe_backups
        )
        lines.extend(("}", ""))
        return "\n".join(lines)

    def _verify_proxy_contract(self) -> None:
        """Prove Nginx sees the managed include and uses only its named upstream."""

        path = self._upstream_path()
        if not path.is_file() or path.is_symlink():
            raise DeploymentError("Managed Nginx upstream is not a regular file")
        if path.read_text(encoding="utf-8") != self._render_upstream(
            (self.config.gunicorn_service,)
        ):
            raise DeploymentError(
                "Managed Nginx upstream does not select the active Gunicorn service"
            )
        self._verify_proxy_upstream_bytes(
            path.read_text(encoding="utf-8"),
            context="Managed Nginx upstream",
        )
        # The successful dump remains process-local and is never retained in the
        # deployment journal. Parse scopes instead of grepping: a decoy server
        # block must not satisfy the contract while auth.b-uh.com still points
        # directly at the mutable Compose Gunicorn service.
        configuration = self._proxy_exec(
            "nginx",
            "-T",
            bounded_output=True,
            context="Nginx production route discovery",
        )
        _verify_nginx_production_route(configuration)

    def _nginx_test_and_reload(self, context: str) -> None:
        self._proxy_exec("nginx", "-t", context=f"{context} configuration test")
        self._proxy_exec(
            "nginx", "-s", "reload", context=f"{context} atomic reload"
        )

    def _verify_proxy_upstream_bytes(self, expected: str, *, context: str) -> None:
        """Prove Nginx's mount resolves the exact host inode contents."""

        expected_digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
        output = self._proxy_exec(
            "sha256sum",
            self.config.nginx_upstream_container_file,
            context=f"{context} container-visible upstream verification",
        ).strip()
        if output.split(maxsplit=1)[0] != expected_digest:
            raise DeploymentError(
                f"{context} is not visible as exact bytes inside Nginx"
            )

    def _activate_upstream(
        self,
        targets: Sequence[str],
        *,
        backup_targets: Sequence[str] = (),
        context: str,
    ) -> None:
        """Validate and atomically reload one target set, restoring on uncertainty."""

        path = self._upstream_path()
        if not path.is_file() or path.is_symlink():
            raise DeploymentError("Managed Nginx upstream disappeared")
        details = path.stat()
        previous = path.read_text(encoding="utf-8")
        updated = self._render_upstream(
            targets, backup_targets=backup_targets
        )
        if updated == previous:
            # The on-disk bytes can already match while the running Nginx
            # master still has the preceding candidate/rollback route loaded.
            # Always reload at a traffic boundary; equality only lets us skip
            # the atomic file replacement, never the live configuration swap.
            self._verify_proxy_upstream_bytes(updated, context=context)
            self._nginx_test_and_reload(context)
            return
        _atomic_text(
            path,
            updated,
            stat.S_IMODE(details.st_mode),
            owner=(details.st_uid, details.st_gid),
        )
        try:
            self._verify_proxy_upstream_bytes(updated, context=context)
            self._nginx_test_and_reload(context)
        except BaseException as switch_error:
            # The old master keeps serving its last valid config until a reload.
            # Restore the prior bytes and reload them before reporting failure,
            # covering an uncertain signal-delivery result without a raw 502.
            _atomic_text(
                path,
                previous,
                stat.S_IMODE(details.st_mode),
                owner=(details.st_uid, details.st_gid),
            )
            try:
                self._verify_proxy_upstream_bytes(
                    previous, context=f"{context} restoration"
                )
                self._nginx_test_and_reload(f"{context} restoration")
            except BaseException as restore_error:
                raise DeploymentError(
                    f"{context} failed and the prior Nginx route could not be reloaded"
                ) from restore_error
            if isinstance(switch_error, DeploymentError):
                raise switch_error
            raise DeploymentError(f"{context} failed") from switch_error

    def _running_service_containers(self, service: str, *, context: str) -> tuple[str, ...]:
        """Return every running Compose container for a service, fail closed."""

        containers = tuple(
            line.strip()
            for line in self._compose("ps", "-q", service, context=context).splitlines()
            if line.strip()
        )
        if not containers:
            raise DeploymentError(f"{context} found no running containers for {service}")
        if (
            len(containers) > MAX_SERVICE_REPLICAS
            or len(set(containers)) != len(containers)
            or any(not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in containers)
        ):
            raise DeploymentError(
                f"{context} returned invalid container identities for {service}"
            )
        return containers

    def _auth_scale_arguments(self) -> tuple[str, ...]:
        """Pin the live Auth replica topology during replacement and rollback."""

        if set(self.auth_replica_counts) != set(self.config.auth_services):
            raise DeploymentError("Live AllianceAuth replica counts were not captured")
        arguments: list[str] = []
        for service in self.config.auth_services:
            count = self.auth_replica_counts[service]
            if not 1 <= count <= MAX_SERVICE_REPLICAS:
                raise DeploymentError(
                    f"Live service {service} has an invalid replica count"
                )
            arguments.extend(("--scale", f"{service}={count}"))
        return tuple(arguments)

    def _manage_image(self, *arguments: str, context: str) -> str:
        return self._compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python3",
            self.config.gunicorn_service,
            self.config.manage_py,
            *arguments,
            context=context,
        )

    def _manage_live(self, *arguments: str, context: str) -> str:
        return self._compose(
            "exec",
            "-T",
            self.config.gunicorn_service,
            "python3",
            self.config.manage_py,
            *arguments,
            context=context,
        )

    def _load_current(self) -> Mapping[str, Any] | None:
        path = self.config.state_dir / "current.json"
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024:
            raise DeploymentError("Current deployment state is not a safe file")
        try:
            data = json.loads(path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("Current deployment state is unreadable") from exc
        required = {
            "schema_version",
            "platform_version",
            "release_commit",
            "source_commit",
            "manifest_sha256",
            "verified_at",
        }
        if not isinstance(data, dict) or set(data) != required or data["schema_version"] != 1:
            raise DeploymentError("Current deployment state has an unsupported schema")
        if canonical_json_bytes(data) != path.read_bytes():
            raise DeploymentError("Current deployment state is not canonical")
        if not all(
            isinstance(data[key], str)
            for key in required - {"schema_version"}
        ):
            raise DeploymentError("Current deployment state has invalid values")
        for key in ("release_commit", "source_commit"):
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", data[key]):
                raise DeploymentError(f"Current deployment state has an invalid {key}")
        if not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00",
            data["verified_at"],
        ):
            raise DeploymentError("Current deployment state has an invalid timestamp")
        if not SHA256_RE.fullmatch(data["manifest_sha256"]):
            raise DeploymentError("Current deployment state has an invalid manifest digest")
        return data

    @staticmethod
    def _secure_private_directory(path: Path, context: str) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
            raise DeploymentError(f"{context} is not a real directory")
        if details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o077:
            raise DeploymentError(f"{context} must be root-owned with mode 0700")

    def _read_optional_env_value(self, key: str) -> str | None:
        path = self.config.app_dir / self.config.env_file
        if not path.is_file() or path.is_symlink():
            raise DeploymentError("Environment file is not a regular file")
        try:
            contents = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DeploymentError("Environment file is unreadable") from exc
        found: str | None = None
        for raw_line in contents.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if separator and name.strip() == key:
                if found is not None:
                    raise DeploymentError(f"{key} occurs more than once in the environment")
                found = value.strip()
                if (
                    len(found) >= 2
                    and found[0] == found[-1]
                    and found[0] in {"'", '"'}
                ):
                    found = found[1:-1]
        return found

    def _read_env_value(self, key: str) -> str:
        found = self._read_optional_env_value(key)
        if not found:
            raise DeploymentError(f"{key} is not configured")
        return found

    def _compose_files(self) -> tuple[Path, ...]:
        """Resolve and validate the exact host Compose stack, fail closed."""

        configured = self._read_optional_env_value("COMPOSE_FILE")
        if configured is None:
            values = (self.config.compose_file.as_posix(),)
        else:
            separator = self._read_optional_env_value("COMPOSE_PATH_SEPARATOR")
            if separator not in {None, ":"}:
                raise DeploymentError(
                    "COMPOSE_PATH_SEPARATOR must be ':' on the production host"
                )
            values = tuple(configured.split(":"))

        if not values or len(values) > MAX_COMPOSE_FILES or any(not item for item in values):
            raise DeploymentError("COMPOSE_FILE contains an invalid number of files")

        relative_files: list[Path] = []
        for value in values:
            pure = PurePosixPath(value)
            if (
                not SAFE_COMPOSE_PATH_RE.fullmatch(value)
                or value.startswith("/")
                or not pure.parts
                or ".." in pure.parts
                or "//" in value
            ):
                raise DeploymentError("COMPOSE_FILE contains an unsafe path")
            relative_files.append(Path(pure.as_posix()))

        compose_files = tuple(relative_files)
        if len(set(compose_files)) != len(compose_files):
            raise DeploymentError("COMPOSE_FILE contains duplicate files")
        if self.config.compose_file not in compose_files:
            raise DeploymentError("COMPOSE_FILE does not include the configured base file")

        app_dir = self.config.app_dir.resolve()
        for relative in compose_files:
            path = self.config.app_dir / relative
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(app_dir)
            ):
                raise DeploymentError(
                    f"Compose file {relative.as_posix()} is not a regular in-tree file"
                )
        return compose_files

    def _validate_release_transition(self, bundle: ValidatedBundle) -> None:
        current = self._load_current()
        previous = bundle.manifest["previous_release"]
        if current is None:
            baseline = bundle.manifest["compatibility"]["values"].get(
                "production_baseline"
            )
            policy = bundle.manifest["compatibility"]["values"].get("policy")
            expected = {
                "platform_version": self.config.legacy_platform_version,
                "release_commit": self.config.legacy_release_commit,
                "deployment_generation": "legacy-v1",
            }
            if not isinstance(baseline, dict) or any(
                baseline.get(key) != value for key, value in expected.items()
            ):
                raise DeploymentError("First Platform v2 release targets the wrong legacy baseline")
            if _semver(bundle.request.platform_version) <= _semver(
                self.config.legacy_platform_version
            ):
                raise DeploymentError(
                    "First Platform v2 release must be newer than the legacy baseline"
                )
            if previous is not None and not (
                isinstance(policy, dict)
                and policy.get(
                    "legacy_bootstrap_may_skip_uninstalled_v2_releases"
                )
                is True
            ):
                raise DeploymentError(
                    "First Platform v2 release with a predecessor is not authorized "
                    "for legacy bootstrap"
                )
            return
        if _semver(bundle.request.platform_version) <= _semver(
            current["platform_version"]
        ):
            raise DeploymentError("Platform versions must increase monotonically")
        if not isinstance(previous, dict):
            raise DeploymentError("An established Platform v2 host requires previous_release")
        expected_previous = {
            "platform_version": current["platform_version"],
            "source_commit": current["source_commit"],
            "manifest_sha256": current["manifest_sha256"],
        }
        if previous == expected_previous:
            if bundle.request.recovery_transition is not None:
                raise DeploymentError(
                    "A direct release must not carry stale recovery authorization"
                )
            return
        transition = bundle.request.recovery_transition
        if transition is None:
            raise DeploymentError("Release predecessor does not match the verified live state")
        baseline = transition["releases"][0]
        expected_baseline = {
            "platform_version": current["platform_version"],
            "release_commit": current["release_commit"],
            "source_commit": current["source_commit"],
            "manifest_sha256": current["manifest_sha256"],
        }
        if any(baseline[key] != value for key, value in expected_baseline.items()):
            raise DeploymentError(
                "Recovery baseline does not match the verified live state"
            )

    def _validate_recovery_host_baseline(self, bundle: ValidatedBundle) -> None:
        transition = bundle.request.recovery_transition
        if transition is None:
            return
        try:
            policy = recovery_policy.load_policy()
        except recovery_policy.RecoveryPolicyError as exc:
            raise DeploymentError(str(exc)) from exc
        expected = policy["host_baseline"]
        compose_files = [path.as_posix() for path in self._compose_files()]
        compose_key = (
            "compose_files_before_activation"
            if transition["purpose"] == "receiver-upgrade-preflight"
            else "compose_files_after_activation"
        )
        if compose_files != expected[compose_key]:
            raise DeploymentError(
                "Recovery Compose file set changed from the confirmed transition phase"
            )
        expected_services = expected["auth_services"]
        if set(expected_services) != set(self.config.auth_services):
            raise DeploymentError("Recovery Auth service set changed")
        for service, identity in expected_services.items():
            captured = self.previous_images.get(service)
            if (
                captured is None
                or captured[0] != identity["image_id"]
                or self.auth_replica_counts.get(service) != identity["replicas"]
            ):
                raise DeploymentError(
                    f"Recovery live image or replica baseline changed for {service}"
                )
            for container in self._running_service_containers(
                service, context=f"Recovery provenance discovery for {service}"
            ):
                raw = self._run(
                    ["docker", "inspect", "--format", "{{json .Config.Labels}}", container],
                    context=f"Recovery provenance verification for {service}",
                ).strip()
                try:
                    labels = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DeploymentError("Recovery live provenance is malformed") from exc
                baseline = transition["releases"][0]
                if (
                    not isinstance(labels, dict)
                    or labels.get(IMAGE_PROVENANCE_LABELS["platform_version"])
                    != baseline["platform_version"]
                    or labels.get(IMAGE_PROVENANCE_LABELS["source_commit"])
                    != baseline["source_commit"]
                ):
                    raise DeploymentError(
                        f"Recovery live provenance changed for {service}"
                    )
        local = self.config.app_dir / self.config.local_settings
        details = local.stat()
        local_expected = expected["local_settings"]
        if (
            self.config.local_settings.as_posix() != local_expected["path"]
            or details.st_uid != local_expected["uid"]
            or details.st_gid != local_expected["gid"]
            or f"{stat.S_IMODE(details.st_mode):04o}" != local_expected["mode"]
        ):
            raise DeploymentError("Recovery local-settings identity changed")
        marker_path = self._platform_current_path()
        try:
            marker = json.loads(marker_path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("Recovery application marker is unreadable") from exc
        baseline = transition["releases"][0]
        if marker != {
            "manifest_sha256": baseline["manifest_sha256"],
            "platform_version": baseline["platform_version"],
            "release_commit": baseline["release_commit"],
            "schema_version": 1,
        }:
            raise DeploymentError("Recovery application marker changed")
        for key in ("nginx", "front_proxy"):
            service = expected[key]["service"]
            containers = self._running_service_containers(
                service, context=f"Recovery {key} discovery"
            )
            if len(containers) != 1:
                raise DeploymentError(f"Recovery {key} topology changed")
            state = self._run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Name}}|{{.Image}}|{{.State.Status}}|{{.RestartCount}}",
                    containers[0],
                ],
                context=f"Recovery {key} identity verification",
            ).strip().split("|")
            if state != [
                f"/{expected[key]['container']}",
                expected[key]["image_id"],
                "running",
                "0",
            ]:
                raise DeploymentError(f"Recovery {key} identity changed")
        owner = expected["discord_owner"]
        probe = (
            "import json;from django.conf import settings;"
            "from allianceauth.services.modules.discord.models import DiscordUser;"
            f"item=DiscordUser.objects.get(uid='{owner['discord_user_id']}');"
            "print(json.dumps({'configured':str(getattr(settings,"
            "'BUH_DISCORD_GUILD_OWNER_ID','')),'uid':str(item.uid),"
            "'username':item.user.username},sort_keys=True,separators=(',',':')))"
        )
        output = self._manage_image(
            "shell",
            "-c",
            probe,
            context="Recovery Discord owner association verification",
        )
        expected_owner = {
            "configured": owner["discord_user_id"],
            "uid": owner["discord_user_id"],
            "username": owner["auth_username"],
        }
        parsed: Any = None
        for line in reversed(output.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                break
        if parsed != expected_owner:
            raise DeploymentError("Recovery Discord owner association changed")
        self.recovery_baseline_verified = True

    def _validate_runtime_image(self, bundle: ValidatedBundle) -> None:
        values = bundle.manifest["compatibility"]["values"]
        runtime = values.get("production_runtime")
        if not isinstance(runtime, dict) or set(runtime) != {"base_image"}:
            raise DeploymentError(
                "Release compatibility does not record one production base image"
            )
        expected = runtime["base_image"]
        if not isinstance(expected, str) or not IMAGE_DIGEST_RE.fullmatch(expected):
            raise DeploymentError("Release production base image is not digest pinned")
        configured = self._read_env_value("AA_DOCKER_TAG")
        if configured != expected:
            raise DeploymentError(
                "The host AA_DOCKER_TAG does not match the release compatibility contract"
            )

    def _verify_compose_build_contract(self) -> None:
        """Require every Auth service to build the reviewed custom Dockerfile."""

        output = self._compose(
            "config",
            "--no-interpolate",
            "--no-env-resolution",
            "--format",
            "json",
            bounded_output=True,
            context="Docker Compose build contract discovery",
        )
        document: Any = None
        decoder = json.JSONDecoder()
        for offset, character in enumerate(output):
            if character != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(output[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("services"), dict):
                document = candidate
                break
        if not isinstance(document, dict):
            raise DeploymentError("Docker Compose build contract is not valid JSON")
        services = document["services"]
        app_dir = self.config.app_dir.resolve()
        expected_dockerfile = (app_dir / self.config.custom_dockerfile).resolve()
        for service in self.config.auth_services:
            service_config = services.get(service)
            build = (
                service_config.get("build")
                if isinstance(service_config, dict)
                else None
            )
            if not isinstance(build, dict) or "dockerfile_inline" in build:
                raise DeploymentError(
                    f"Auth service {service} does not use a file-backed build"
                )
            context_value = build.get("context")
            dockerfile_value = build.get("dockerfile")
            if (
                not isinstance(context_value, str)
                or not context_value
                or any(ord(character) < 32 for character in context_value)
                or "://" in context_value
                or not isinstance(dockerfile_value, str)
                or not dockerfile_value
                or any(ord(character) < 32 for character in dockerfile_value)
                or "://" in dockerfile_value
            ):
                raise DeploymentError(
                    f"Auth service {service} has an unsafe build definition"
                )
            context_path = Path(context_value)
            if not context_path.is_absolute():
                context_path = app_dir / context_path
            context_path = context_path.resolve()
            dockerfile_path = Path(dockerfile_value)
            if not dockerfile_path.is_absolute():
                dockerfile_path = context_path / dockerfile_path
            if context_path != app_dir or dockerfile_path.resolve() != expected_dockerfile:
                raise DeploymentError(
                    f"Auth service {service} does not build the reviewed custom Dockerfile"
                )

    @staticmethod
    def _candidate_provenance_labels(bundle: ValidatedBundle) -> Mapping[str, str]:
        base_image = bundle.manifest["compatibility"]["values"][
            "production_runtime"
        ]["base_image"]
        if not isinstance(base_image, str) or not IMAGE_DIGEST_RE.fullmatch(base_image):
            raise DeploymentError("Candidate base image provenance is invalid")
        base_digest = base_image.rsplit("@", 1)[1]
        values = {
            "platform_version": bundle.request.platform_version,
            "source_commit": bundle.manifest["source_commit"],
            "release_commit": bundle.request.release_commit,
            "manifest_sha256": bundle.request.manifest_sha256,
            "base_digest": base_digest,
        }
        return {
            IMAGE_PROVENANCE_LABELS[name]: value for name, value in values.items()
        }

    def validate(self, bundle: ValidatedBundle) -> None:
        if os.geteuid() != 0:
            raise DeploymentError("Platform v2 receiver must run as root")
        if (
            bundle.request.mode == "deploy"
            and self.config.schema_version != RECEIVER_CONFIG_SCHEMA_VERSION
        ):
            raise DeploymentError(
                "Production deployment requires receiver configuration schema v2 "
                "and its managed Nginx web-switch prerequisite"
            )
        app_dir = self.config.app_dir
        if not app_dir.is_dir() or app_dir.is_symlink():
            raise DeploymentError("AllianceAuth application directory is unavailable")
        required_files = [
            (self.config.env_file, "environment file"),
            (self.config.custom_dockerfile, "custom Dockerfile"),
            (self.config.local_settings, "local settings"),
        ]
        if self.config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            required_files.append(
                (self.config.nginx_upstream_file, "managed Nginx upstream")
            )
        for relative, name in required_files:
            path = app_dir / relative
            if not path.is_file() or path.is_symlink():
                raise DeploymentError(f"{name} is not a regular file")
        self._compose_files()
        if self.config.state_dir.resolve().is_relative_to(app_dir.resolve()):
            raise DeploymentError("Deployment state must live outside the build context")
        if self.config.backup_dir.resolve().is_relative_to(app_dir.resolve()):
            raise DeploymentError("Database backups must live outside the build context")
        self._secure_private_directory(self.config.state_dir, "Deployment state directory")
        self._secure_private_directory(self.config.backup_dir, "Backup directory")
        if shutil.disk_usage(app_dir).free < 1024 * 1024 * 1024:
            raise DeploymentError("Less than 1 GiB is free in the AllianceAuth filesystem")
        self._validate_release_transition(bundle)
        self._validate_runtime_image(bundle)

        setup_commands = bundle.install_plan["setup_commands"]
        missing_setup = set(setup_commands) - set(self.config.setup_arguments)
        if missing_setup:
            raise DeploymentError(
                f"Receiver config lacks setup arguments for: {sorted(missing_setup)}"
            )
        local_text = (app_dir / self.config.local_settings).read_text(encoding="utf-8")
        missing_apps = [
            app
            for app in bundle.install_plan["django_apps"]
            if f'"{app}"' not in local_text and f"'{app}'" not in local_text
        ]
        if missing_apps:
            raise DeploymentError(
                f"local.py does not declare release applications: {missing_apps}"
            )
        services = {
            line.strip()
            for line in self._compose(
                "config", "--services", context="Docker Compose validation"
            ).splitlines()
            if line.strip()
        }
        required_services = set(self.config.auth_services) | {
            self.config.database_service,
            self.config.redis_service,
            self.config.proxy_service,
        }
        if not required_services <= services:
            raise DeploymentError(
                f"Docker Compose lacks required services: {sorted(required_services - services)}"
            )
        if self.config.beat_service not in self.config.auth_services:
            raise DeploymentError("Configured beat service is not an Auth service")
        self._verify_compose_build_contract()
        if self.config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            self._verify_proxy_contract()

    def _stage_release(self, bundle: ValidatedBundle) -> Path:
        release_root = (
            self.config.app_dir / "conf" / "buh-platform-v2" / "releases"
        )
        release_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        destination = release_root / f"v{bundle.request.platform_version}"
        if destination.exists():
            if not destination.is_dir() or destination.is_symlink():
                raise DeploymentError("Staged release path is not a regular directory")
            source_files = {path.name: sha256_file(path) for path in bundle.release_dir.iterdir()}
            staged_files = {path.name: sha256_file(path) for path in destination.iterdir()}
            if source_files != staged_files:
                raise DeploymentError("Existing staged release has different bytes")
            return destination
        temporary = release_root / f".v{bundle.request.platform_version}.{os.getpid()}"
        if temporary.exists():
            raise DeploymentError("Temporary staged release path already exists")
        try:
            shutil.copytree(bundle.release_dir, temporary)
            for path in temporary.iterdir():
                if not path.is_file() or path.is_symlink():
                    raise DeploymentError("Staged release contains a non-regular file")
                path.chmod(0o644)
            os.replace(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return destination

    def _dockerfile_block(self, bundle: ValidatedBundle) -> str:
        artifacts = {item["filename"]: item for item in bundle.manifest["artifacts"]}
        ordered = sorted(
            artifacts.values(),
            key=lambda item: (
                item["origin"] == "built",
                item["kind"] == "owned",
                item["component"],
            ),
        )
        relative_root = (
            f"conf/buh-platform-v2/releases/v{bundle.request.platform_version}"
        )
        lines = [BEGIN_V2]
        for artifact in ordered:
            filename = artifact["filename"]
            digest = artifact["sha256"]
            lines.extend(
                [
                    f"COPY {relative_root}/{filename} /tmp/buh-platform-v2/{filename}",
                    "RUN printf '%s  %s\\n' \\",
                    f"      '{digest}' '/tmp/buh-platform-v2/{filename}' \\",
                    "      | sha256sum --check --strict - \\",
                    "    && python3 -m pip install --no-cache-dir --no-deps \\",
                    "      --force-reinstall \\",
                    f"      /tmp/buh-platform-v2/{filename}",
                ]
            )
        labels = self._candidate_provenance_labels(bundle)
        lines.append(
            "LABEL "
            + " \\\n    ".join(
                f"{name}={json.dumps(value)}" for name, value in labels.items()
            )
        )
        lines.append(END_V2)
        return "\n".join(lines) + "\n"

    def _write_candidate_dockerfile(self, bundle: ValidatedBundle) -> None:
        path = self.config.app_dir / self.config.custom_dockerfile
        text = path.read_text(encoding="utf-8")
        for begin, end in ((BEGIN_V2, END_V2), (BEGIN_LEGACY, END_LEGACY)):
            if text.count(begin) != text.count(end) or text.count(begin) > 1:
                raise DeploymentError("Custom Dockerfile release markers are malformed")
        if BEGIN_V2 in text and BEGIN_LEGACY in text:
            raise DeploymentError("Custom Dockerfile contains both deployment generations")
        block = self._dockerfile_block(bundle)
        if BEGIN_V2 in text:
            pattern = re.compile(
                rf"(?ms)^{re.escape(BEGIN_V2)}\n.*?^{re.escape(END_V2)}\n?"
            )
            # A callable replacement preserves Dockerfile backslashes verbatim.
            # Passing ``block`` directly makes ``re.sub`` interpret sequences
            # such as ``\\n`` inside the generated shell command.
            updated = pattern.sub(lambda _match: block, text)
        elif BEGIN_LEGACY in text:
            pattern = re.compile(
                rf"(?ms)^{re.escape(BEGIN_LEGACY)}\n.*?^{re.escape(END_LEGACY)}\n?"
            )
            updated = pattern.sub(lambda _match: block, text)
        else:
            updated = text.rstrip() + "\n\n" + block
        details = path.stat()
        _atomic_text(
            path,
            updated,
            stat.S_IMODE(details.st_mode),
            owner=(details.st_uid, details.st_gid),
        )

    def _expected_versions(self, bundle: ValidatedBundle) -> dict[str, str]:
        return {
            wheel["distribution"]: wheel["version"]
            for wheel in bundle.install_plan["wheels"]
        }

    def _version_probe(self, service: str, bundle: ValidatedBundle, *, live: bool) -> None:
        expected = self._expected_versions(bundle)
        program = (
            "import importlib.metadata,json;"
            f"names={json.dumps(sorted(expected))};"
            "print(json.dumps({name:importlib.metadata.version(name) for name in names},"
            "sort_keys=True,separators=(',',':')))"
        )
        if live:
            output = self._compose(
                "exec",
                "-T",
                service,
                "python3",
                "-c",
                program,
                context=f"Live package verification in {service}",
            )
        else:
            output = self._compose(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "python3",
                service,
                "-c",
                program,
                context=f"Candidate package verification in {service}",
            )
        actual: Any = None
        for line in reversed(output.splitlines()):
            try:
                actual = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(actual, dict):
                break
        if actual != expected:
            raise DeploymentError(f"Package versions differ in {service}")

    def _require_live_images(self, expected_images: Mapping[str, str]) -> None:
        if not expected_images or not set(expected_images) <= set(self.config.auth_services):
            raise DeploymentError(
                "Expected AllianceAuth service images were not captured"
            )
        for service in expected_images:
            expected_image = expected_images[service]
            if not SAFE_IMAGE_ID_RE.fullmatch(expected_image):
                raise DeploymentError(
                    f"Expected image identity for {service} is unsafe"
                )
            containers = self._running_service_containers(
                service, context=f"Live image container discovery for {service}"
            )
            expected = self.auth_replica_counts.get(service)
            if expected is not None and len(containers) != expected:
                raise DeploymentError(f"Live service {service} changed replica count")
            for container in containers:
                image_id = self._run(
                    ["docker", "inspect", "--format", "{{.Image}}", container],
                    context=f"Live image identity for {service}",
                ).strip()
                if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
                    raise DeploymentError(
                        f"Live service {service} returned an unsafe image identity"
                    )
                if image_id != expected_image:
                    raise DeploymentError(
                        f"Live service {service} does not use its expected image"
                    )

    def _capture_live_images(self) -> None:
        """Remember each service image, reference, and live replica count."""

        service_images: dict[str, tuple[str, str]] = {}
        reference_images: dict[str, str] = {}
        replica_counts: dict[str, int] = {}
        restart_baselines: dict[str, int] = {}
        seen_containers: set[str] = set()
        for service in self.config.auth_services:
            image_ids: set[str] = set()
            image_references: set[str] = set()
            containers = self._running_service_containers(
                service, context=f"Live container discovery for {service}"
            )
            if seen_containers.intersection(containers):
                raise DeploymentError(
                    "A live container resolved to more than one Auth service"
                )
            seen_containers.update(containers)
            replica_counts[service] = len(containers)
            for container in containers:
                details = self._run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Status}}|{{.Image}}|{{.Config.Image}}|"
                        "{{.RestartCount}}",
                        container,
                    ],
                    context=f"Live image discovery for {service}",
                ).strip()
                parts = details.split("|")
                if (
                    len(parts) != 4
                    or parts[0] != "running"
                    or not parts[3].isdigit()
                ):
                    raise DeploymentError(f"Live service {service} has an unstable replica")
                image_id, reference = parts[1:3]
                if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
                    raise DeploymentError(
                        f"Live service {service} returned an unsafe image identity"
                    )
                if not SAFE_IMAGE_REFERENCE_RE.fullmatch(reference) or "@" in reference:
                    raise DeploymentError(
                        f"Live service {service} returned an unsafe image reference"
                    )
                image_ids.add(image_id)
                image_references.add(reference)
                restart_baselines[container] = int(parts[3])
            if len(image_ids) != 1:
                raise DeploymentError(
                    f"Live replicas for service {service} do not share one image"
                )
            if len(image_references) != 1:
                raise DeploymentError(
                    f"Live replicas for service {service} do not share one image reference"
                )
            image_id = image_ids.pop()
            reference = image_references.pop()
            prior_image = reference_images.setdefault(reference, image_id)
            if prior_image != image_id:
                raise DeploymentError(
                    "Live AllianceAuth services use one image reference for different images"
                )
            service_images[service] = (image_id, reference)
        self.previous_images = service_images
        self.auth_replica_counts = replica_counts
        self.restart_baselines = restart_baselines

    def _capture_infrastructure_restart_baselines(self) -> None:
        baselines = dict(self.restart_baselines)
        for service in (
            self.config.database_service,
            self.config.redis_service,
            self.config.proxy_service,
        ):
            containers = self._running_service_containers(
                service, context=f"Infrastructure baseline discovery for {service}"
            )
            if len(containers) != 1 or any(container in baselines for container in containers):
                raise DeploymentError(
                    f"Infrastructure service {service} does not have one unique replica"
                )
            state = self._run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}|{{.RestartCount}}",
                    containers[0],
                ],
                context=f"Infrastructure restart baseline for {service}",
            ).strip()
            parts = state.split("|")
            if len(parts) != 2 or parts[0] != "running" or not parts[1].isdigit():
                raise DeploymentError(
                    f"Infrastructure service {service} is not stable"
                )
            baselines[containers[0]] = int(parts[1])
        self.restart_baselines = baselines

    def _capture_candidate_images(self, bundle: ValidatedBundle) -> None:
        if set(self.previous_images) != set(self.config.auth_services):
            raise DeploymentError("Prior service image references were not captured")
        expected_labels = self._candidate_provenance_labels(bundle)
        candidate_images: dict[str, str] = {}
        for service in self.config.auth_services:
            previous_image, reference = self.previous_images[service]
            details = self._run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}|{{json .Config.Labels}}",
                    reference,
                ],
                context=f"Candidate image provenance verification for {service}",
            ).strip()
            image_id, separator, labels_json = details.partition("|")
            if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
                raise DeploymentError(
                    f"Candidate image identity for {service} is unsafe"
                )
            if image_id == previous_image:
                raise DeploymentError(
                    f"Candidate build for {service} left the previous image unchanged"
                )
            try:
                labels = json.loads(labels_json) if separator else None
            except json.JSONDecodeError as exc:
                raise DeploymentError(
                    f"Candidate image labels for {service} are malformed"
                ) from exc
            if not isinstance(labels, dict) or any(
                labels.get(name) != value for name, value in expected_labels.items()
            ):
                raise DeploymentError(
                    f"Candidate image provenance labels for {service} do not match the release"
                )
            candidate_images[service] = image_id
        self.candidate_image_ids = candidate_images

    def _pin_previous_images(self, bundle: ValidatedBundle) -> None:
        """Keep each live image addressable while Compose replaces its normal tag."""

        if set(self.previous_images) != set(self.config.auth_services):
            raise DeploymentError("Previous service images were not captured")
        expected_pins = {
            service: f"buh-platform-v2-rollback:{bundle.request.attempt_id}-{index}"
            for index, service in enumerate(self.config.auth_services)
        }
        if not self.previous_image_pins:
            self.previous_image_pins = expected_pins
        elif self.previous_image_pins != expected_pins:
            raise DeploymentError("Previous image recovery references changed")
        for index, service in enumerate(self.config.auth_services):
            image_id, _reference = self.previous_images[service]
            pin = self.previous_image_pins[service]
            if not SAFE_IMAGE_REFERENCE_RE.fullmatch(pin) or "@" in pin:
                raise DeploymentError("Generated rollback image reference is unsafe")
            self._run(
                ["docker", "image", "tag", image_id, pin],
                context=f"Previous image retention for {service}",
            )
            resolved = self._run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", pin],
                context=f"Previous image retention verification for {service}",
            ).strip()
            if resolved != image_id:
                raise DeploymentError(
                    f"Retained previous image for {service} has the wrong identity"
                )

    def _discard_previous_image_pins(self) -> None:
        """Remove temporary tags after the normal Compose references are verified."""

        pins = tuple(dict.fromkeys(self.previous_image_pins.values()))
        for pin in pins:
            try:
                self._run(
                    ["docker", "image", "rm", pin],
                    context="Temporary rollback image tag cleanup",
                )
            except DeploymentError:
                # The verified Compose references now retain these image IDs.
                # A leftover private tag is harmless and aids manual recovery.
                continue
        self.previous_image_pins = {}

    def _restore_candidate_configuration(self) -> bool:
        """Restore host files and retag the exact pre-attempt image without a restart."""

        if (
            self.original_dockerfile is None
            or self.original_local_settings is None
            or self.original_local_settings_metadata is None
            or self.backup_path is None
        ):
            return False
        dockerfile = self.config.app_dir / self.config.custom_dockerfile
        local_settings = self.config.app_dir / self.config.local_settings
        upstream = self._upstream_path()
        shutil.copy2(self.original_dockerfile, dockerfile)
        if sha256_file(self.original_local_settings) != sha256_file(local_settings):
            raise DeploymentError(
                "Local settings changed during the guarded deployment attempt"
            )
        local_details = local_settings.stat()
        local_metadata = (
            local_details.st_uid,
            local_details.st_gid,
            stat.S_IMODE(local_details.st_mode),
        )
        if local_metadata != self.original_local_settings_metadata:
            raise DeploymentError(
                "Local settings ownership or permissions changed during the guarded "
                "deployment attempt"
            )
        if self.config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            if (
                self.original_upstream is None
                or self.original_upstream_metadata is None
            ):
                return False
            original_upstream = self.original_upstream.read_text(encoding="utf-8")
            upstream_owner = self.original_upstream_metadata[:2]
            upstream_mode = self.original_upstream_metadata[2]
            if upstream.read_text(encoding="utf-8") != original_upstream:
                _atomic_text(
                    upstream,
                    original_upstream,
                    upstream_mode,
                    owner=upstream_owner,
                )
        self._restore_platform_current()
        self._restore_deployment_current()
        self._restore_static_manifest()
        if self.previous_images:
            if set(self.previous_images) != set(self.config.auth_services):
                raise DeploymentError(
                    "The previous service images are incomplete"
                )
            restored_references: dict[str, str] = {}
            for service in self.config.auth_services:
                image_id, reference = self.previous_images[service]
                restored_image = restored_references.get(reference)
                if restored_image is not None:
                    if restored_image != image_id:
                        raise DeploymentError(
                            "One previous image reference resolves to different service images"
                        )
                    continue
                restored_references[reference] = image_id
                source = self.previous_image_pins.get(service, image_id)
                self._run(
                    ["docker", "image", "tag", source, reference],
                    context=f"Previous image reference restoration for {service}",
                )
                restored = self._run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        reference,
                    ],
                    context=f"Previous image reference verification for {service}",
                ).strip()
                if restored != image_id:
                    raise DeploymentError(
                        f"Previous image reference for {service} was not restored"
                    )
        self._discard_previous_image_pins()
        return True

    def prepare_candidate(self, bundle: ValidatedBundle) -> None:
        self.log_since = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.backup_path = self.config.backup_dir / bundle.request.attempt_id
        self.backup_path.mkdir(mode=0o700, parents=True, exist_ok=False)
        dockerfile = self.config.app_dir / self.config.custom_dockerfile
        local_settings = self.config.app_dir / self.config.local_settings
        self.original_dockerfile = self.backup_path / "custom.dockerfile"
        self.original_local_settings = self.backup_path / "local.py"
        if self.config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            self.original_upstream = self.backup_path / "nginx-upstream.conf"
        local_details = local_settings.stat()
        self.original_local_settings_metadata = (
            local_details.st_uid,
            local_details.st_gid,
            stat.S_IMODE(local_details.st_mode),
        )
        shutil.copy2(dockerfile, self.original_dockerfile)
        shutil.copy2(local_settings, self.original_local_settings)
        if self.config.schema_version == RECEIVER_CONFIG_SCHEMA_VERSION:
            upstream = self._upstream_path()
            upstream_details = upstream.stat()
            self.original_upstream_metadata = (
                upstream_details.st_uid,
                upstream_details.st_gid,
                stat.S_IMODE(upstream_details.st_mode),
            )
            assert self.original_upstream is not None
            shutil.copy2(upstream, self.original_upstream)
        self.original_local_settings.chmod(0o600)
        if self.original_upstream is not None:
            self.original_upstream.chmod(0o600)
        self._capture_platform_current()
        self._capture_deployment_current()

        self._capture_live_images()
        self._validate_recovery_host_baseline(bundle)
        self._capture_infrastructure_restart_baselines()
        self._capture_static_manifest()
        self.previous_image_pins = {
            service: f"buh-platform-v2-rollback:{bundle.request.attempt_id}-{index}"
            for index, service in enumerate(self.config.auth_services)
        }
        self._save_recovery_plan(bundle.request.attempt_id, "prepared")
        self._pin_previous_images(bundle)
        # Start old-image slots while the Compose image reference and shared
        # static manifest still describe the live release. Their read-only
        # manifest bind remains immutable through candidate collectstatic.
        self._start_previous_web_slots(bundle)
        self.staged_release = self._stage_release(bundle)
        self._write_candidate_dockerfile(bundle)
        self._compose(
            "config", "--quiet", context="Candidate Docker Compose validation"
        )
        self._compose(
            "build",
            *self.config.auth_services,
            context="Cached candidate image build",
            timeout=self.config.command_timeout_seconds,
        )
        self._capture_candidate_images(bundle)
        for service in self.config.auth_services:
            self._version_probe(service, bundle, live=False)
        self._manage_image("check", "--no-color", context="Candidate Django checks")
        self._manage_image(
            "migrate", "--plan", "--no-color", context="Candidate migration plan"
        )
        static_strategy = (
            "from django.contrib.staticfiles.storage import staticfiles_storage as s;"
            "assert hasattr(s,'hashed_name') and hasattr(s,'manifest_name');"
            "print(s.manifest_name)"
        )
        self._manage_image(
            "shell",
            "-c",
            static_strategy,
            context="Content-hashed static asset strategy verification",
        )

    def _database_container(self) -> str:
        container = self._compose(
            "ps",
            "-q",
            self.config.database_service,
            context="Database container discovery",
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise DeploymentError("Database service did not resolve to one container")
        return container

    def _container_environment(self, container: str) -> dict[str, str]:
        output = self._run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                container,
            ],
            context="Database environment discovery",
        )
        result: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                result[key] = value
        return result

    @staticmethod
    def _database_shell() -> str:
        return (
            'password="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
            'test -n "$password"; '
            'export MYSQL_PWD="$password"; exec mariadb --user=root --batch '
            '--skip-column-names --execute="$1"'
        )

    def _database_query(self, container: str, sql: str) -> str:
        return self._run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-ceu",
                self._database_shell(),
                "buh-db-query",
                sql,
            ],
            context="Database verification query",
        )

    def _application_database(
        self, container: str, environment: Mapping[str, str]
    ) -> str:
        declared = {
            value.strip()
            for name in ("MARIADB_DATABASE", "MYSQL_DATABASE")
            if (value := environment.get(name, "")).strip()
        }
        if any(not SAFE_DATABASE_RE.fullmatch(name) for name in declared):
            raise DeploymentError("Database container declares an unsafe database name")
        if len(declared) > 1:
            raise DeploymentError("Database container declares conflicting database names")

        query = (
            "SELECT DISTINCT TABLE_SCHEMA FROM information_schema.TABLES "
            "WHERE TABLE_NAME='django_migrations' AND TABLE_SCHEMA NOT IN "
            "('information_schema','mysql','performance_schema','sys') "
            "ORDER BY TABLE_SCHEMA"
        )
        candidates = [
            line.strip()
            for line in self._database_query(container, query).splitlines()
            if line.strip()
        ]
        if (
            len(candidates) > 128
            or len(candidates) != len(set(candidates))
            or any(not SAFE_DATABASE_RE.fullmatch(name) for name in candidates)
        ):
            raise DeploymentError("Application database discovery returned unsafe results")

        if declared:
            database = next(iter(declared))
            if database not in candidates:
                raise DeploymentError(
                    "Declared application database has no Django migration history"
                )
            return database
        if len(candidates) != 1:
            raise DeploymentError(
                "Database container did not resolve to exactly one Django database"
            )
        return candidates[0]

    @staticmethod
    def _database_dump_shell() -> str:
        return (
            'database="$1"; '
            'password="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
            'test -n "$database"; test -n "$password"; '
            'export MYSQL_PWD="$password"; exec mariadb-dump --user=root '
            '--single-transaction --quick --routines --triggers --events --hex-blob '
            '"$database"'
        )

    def _evidence_counts(self, container: str, database: str) -> dict[str, int]:
        if not SAFE_DATABASE_RE.fullmatch(database):
            raise DeploymentError("Database name is unsafe")
        table_sql = (
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{database}' AND "
            "(TABLE_NAME='django_migrations' OR TABLE_NAME LIKE 'buh\\_%') "
            "ORDER BY TABLE_NAME"
        )
        tables = [line.strip() for line in self._database_query(container, table_sql).splitlines()]
        if (
            not tables
            or len(tables) > 1000
            or any(not SAFE_DATABASE_RE.fullmatch(table) for table in tables)
        ):
            raise DeploymentError("Database evidence table set is invalid")
        counts: dict[str, int] = {}
        for table in tables:
            output = self._database_query(
                container, f"SELECT COUNT(*) FROM `{database}`.`{table}`"
            ).strip()
            if not re.fullmatch(r"[0-9]+", output):
                raise DeploymentError(f"Could not count database evidence table {table}")
            counts[table] = int(output)
        return counts

    @contextmanager
    def _database_read_lock(self, container: str) -> Iterator[None]:
        """Keep the dump and its evidence counts on the same database state.

        The watchdog runs inside the database container, so even a terminated
        receiver or Docker client cannot leave the database locked indefinitely.
        Reconnection is disabled because a new session would lose the lock.
        """
        script = (
            'password="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
            'test -n "$password"; export MYSQL_PWD="$password"; '
            'command -v timeout >/dev/null; '
            f'exec timeout --signal=TERM --kill-after=5s {BACKUP_LOCK_SECONDS}s '
            'mariadb --user=root --batch --skip-column-names --unbuffered '
            '--skip-reconnect'
        )
        process = subprocess.Popen(
            ["docker", "exec", "-i", container, "sh", "-ceu", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(
                (
                    f"SET SESSION lock_wait_timeout={BACKUP_LOCK_WAIT_SECONDS};\n"
                    "FLUSH TABLES WITH READ LOCK;\n"
                    "SELECT 'BUH_BACKUP_LOCKED';\n"
                ).encode("ascii")
            )
            process.stdin.flush()
            deadline = time.monotonic() + BACKUP_LOCK_WAIT_SECONDS + 5
            response = b""
            while b"\n" not in response and len(response) < 128:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise DeploymentError("Database backup read lock timed out")
                chunk = os.read(process.stdout.fileno(), 128 - len(response))
                if not chunk:
                    break
                response += chunk
            if response != b"BUH_BACKUP_LOCKED\n" or process.poll() is not None:
                raise DeploymentError("Database backup read lock was not acquired")
            yield
            if process.poll() is not None:
                raise DeploymentError("Database backup read lock expired or disconnected")
            process.stdin.write(b"UNLOCK TABLES;\n")
            process.stdin.flush()
            process.stdin.close()
            if process.wait(timeout=10) != 0:
                raise DeploymentError("Database backup read lock did not release cleanly")
        except (OSError, subprocess.SubprocessError) as error:
            raise DeploymentError("Database backup read lock connection failed") from error
        finally:
            # EOF also closes the session and releases its global read lock.
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                # The in-container watchdog still bounds an orphaned session.
            if process.stdout is not None:
                process.stdout.close()

    def backup(self, bundle: ValidatedBundle) -> Mapping[str, Any]:
        if self.backup_path is None:
            raise DeploymentError("Configuration backup was not prepared")
        container = self._database_container()
        environment = self._container_environment(container)
        database = self._application_database(container, environment)
        image_id = self._run(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            context="Database image discovery",
        ).strip()
        if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
            raise DeploymentError("Database container image is not content addressed")

        raw_path = self.backup_path / "database.sql"
        # A transaction-consistent dump cannot be compared with later live
        # counts: scheduled capture, retention and user writes may change them.
        # Hold one bounded read lock through both reads, then release it before
        # the slower independent restore rehearsal.
        with self._database_read_lock(container):
            self._run_to_file(
                [
                    "docker",
                    "exec",
                    container,
                    "sh",
                    "-ceu",
                    self._database_dump_shell(),
                    "buh-db-dump",
                    database,
                ],
                raw_path,
                context="Pre-migration database backup",
            )
            if raw_path.stat().st_size < 128:
                raise DeploymentError("Database backup is unexpectedly small")
            source_counts = self._evidence_counts(container, database)

        restore_name = f"buh-restore-{bundle.request.workflow_run_id}-{os.getpid()}"
        restore_password = secrets.token_urlsafe(32)
        self._run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                restore_name,
                "--network",
                "none",
                "--tmpfs",
                f"/var/lib/mysql:rw,noexec,nosuid,size={self.config.restore_tmpfs_mb}m",
                "--env",
                f"MARIADB_ROOT_PASSWORD={restore_password}",
                "--env",
                f"MARIADB_DATABASE={database}",
                image_id,
            ],
            context="Ephemeral database restore container startup",
        )
        restore_error: BaseException | None = None
        try:
            ready = False
            for _ in range(self.config.health_attempts):
                try:
                    self._run(
                        [
                            "docker",
                            "exec",
                            restore_name,
                            "healthcheck.sh",
                            "--connect",
                            "--innodb_initialized",
                        ],
                        timeout=30,
                        context="Ephemeral database readiness",
                    )
                except DeploymentError:
                    time.sleep(self.config.health_interval_seconds)
                    continue
                ready = True
                break
            if not ready:
                raise DeploymentError("Ephemeral database restore container was not ready")
            restore_script = (
                'password="${MARIADB_ROOT_PASSWORD:-}"; test -n "$password"; '
                'export MYSQL_PWD="$password"; '
                'exec mariadb --user=root "$MARIADB_DATABASE"'
            )
            self._run_from_file(
                ["docker", "exec", "-i", restore_name, "sh", "-ceu", restore_script],
                raw_path,
                context="Ephemeral database restoration",
            )
            restored_counts = self._evidence_counts(restore_name, database)
            if restored_counts != source_counts:
                raise DeploymentError("Restored accounting evidence differs from the source")
        except BaseException as exc:
            restore_error = exc
        try:
            self._run(
                ["docker", "rm", "--force", restore_name],
                timeout=60,
                context="Ephemeral restore cleanup",
            )
        except DeploymentError as cleanup_error:
            if restore_error is not None:
                raise DeploymentError(
                    "Ephemeral database restore rehearsal failed and its isolated "
                    "container could not be removed"
                ) from restore_error
            raise DeploymentError(
                "Ephemeral database restore container could not be removed"
            ) from cleanup_error
        if restore_error is not None:
            raise restore_error

        compressed_path = self.backup_path / "database.sql.gz"
        with raw_path.open("rb") as source, compressed_path.open("xb") as raw_output:
            with gzip.GzipFile(
                filename="database.sql", mode="wb", fileobj=raw_output, mtime=0
            ) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        compressed_path.chmod(0o600)
        raw_path.unlink()
        metadata = {
            "schema_version": 1,
            "filename": compressed_path.name,
            "sha256": sha256_file(compressed_path),
            "size": compressed_path.stat().st_size,
            "database_image_id": image_id,
            "evidence_tables": len(source_counts),
            "evidence_rows": sum(source_counts.values()),
        }
        metadata_path = self.backup_path / "BACKUP.json"
        _atomic_text(
            metadata_path,
            canonical_json_bytes(metadata).decode("ascii"),
            0o600,
        )
        return metadata

    def migrate(self, bundle: ValidatedBundle) -> None:
        policy = bundle.manifest["compatibility"]["values"].get("policy", {})
        if policy.get("database_migrations_must_be_rollback_compatible") is not True:
            raise DeploymentError("Release does not require rollback-compatible migrations")
        self.migration_started = True
        self._update_recovery_plan("migration-started")
        self._manage_image(
            "migrate", "--noinput", "--no-color", context="Database migration"
        )
        self._manage_image(
            "migrate", "--noinput", "--no-color", context="Idempotent migration check"
        )
        self._protect_static_collection_traffic()
        self.static_collection_started = True
        self._update_recovery_plan("static-collection-started")
        self._manage_image(
            "collectstatic",
            "--noinput",
            "--no-color",
            context="Static asset collection",
        )
        self._verify_previous_static_fallback()
        for command in bundle.install_plan["setup_commands"]:
            self._manage_image(
                command,
                *self.config.setup_arguments[command],
                context=f"Setup command {command}",
            )

    @staticmethod
    def _safe_static_relative_path(value: Any) -> bool:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 512
            or "\\" in value
            or any(ord(character) < 32 or ord(character) > 126 for character in value)
        ):
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and ".." not in path.parts

    def _static_asset_archive_evidence(
        self, archive_path: Path, expected_paths: set[str]
    ) -> Mapping[str, Any]:
        try:
            details = archive_path.lstat()
        except FileNotFoundError as exc:
            raise DeploymentError("Previous static asset archive was not captured") from exc
        if (
            not stat.S_ISREG(details.st_mode)
            or archive_path.is_symlink()
            or not 1 <= details.st_size <= MAX_STATIC_ASSET_BACKUP_BYTES
        ):
            raise DeploymentError("Previous static asset archive is unsafe")
        aggregate = hashlib.sha256()
        total = 0
        names: set[str] = set()
        try:
            with tarfile.open(archive_path, mode="r:") as archive:
                members = archive.getmembers()
                if not 1 <= len(members) <= MAX_STATIC_MANIFEST_ENTRIES:
                    raise DeploymentError(
                        "Previous static asset archive has an unsafe member count"
                    )
                for member in members:
                    if (
                        not member.isfile()
                        or not self._safe_static_relative_path(member.name)
                        or member.name in names
                        or not 0 <= member.uid <= 2**31 - 1
                        or not 0 <= member.gid <= 2**31 - 1
                        or member.mode < 0
                        or member.mode > 0o777
                        or not 0 <= member.size <= MAX_STATIC_FILE_BYTES
                    ):
                        raise DeploymentError(
                            "Previous static asset archive member is unsafe"
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise DeploymentError(
                            "Previous static asset archive member is unreadable"
                        )
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > member.size or total > MAX_STATIC_ASSET_BYTES:
                            raise DeploymentError(
                                "Previous static asset archive exceeds its limits"
                            )
                        digest.update(chunk)
                    if size != member.size:
                        raise DeploymentError(
                            "Previous static asset archive member is truncated"
                        )
                    names.add(member.name)
                    aggregate.update(
                        canonical_json_bytes(
                            {
                                "path": member.name,
                                "sha256": digest.hexdigest(),
                                "size": size,
                            }
                        )
                    )
        except (OSError, tarfile.TarError) as exc:
            raise DeploymentError("Previous static asset archive is unreadable") from exc
        if names != expected_paths:
            raise DeploymentError(
                "Previous static asset archive does not contain the exact manifest paths"
            )
        return {
            "assets": len(names),
            "asset_bytes": total,
            "assets_sha256": aggregate.hexdigest(),
        }

    def _capture_static_assets(
        self, container: str, expected_paths: set[str]
    ) -> None:
        if (
            self.backup_path is None
            or self.static_root_path is None
            or self.static_manifest_path is None
            or self.static_assets_count is None
            or self.static_assets_bytes is None
            or self.static_assets_sha256 is None
        ):
            raise DeploymentError("Previous static asset evidence is incomplete")
        backup = self.backup_path / "static-assets.previous.tar"
        program = "\n".join(
            (
                "import json,os,stat,sys,tarfile",
                "root,manifest_path=sys.argv[1:]",
                "root=os.path.abspath(root)",
                "root_stat=os.lstat(root)",
                "assert stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISLNK(root_stat.st_mode)",
                "with open(manifest_path,'rb') as stream: data=json.load(stream)",
                "assets=sorted(set(data['paths'].values()))",
                f"assert 1 <= len(assets) <= {MAX_STATIC_MANIFEST_ENTRIES}",
                "total=0",
                "with tarfile.open(fileobj=sys.stdout.buffer,mode='w|',format=tarfile.PAX_FORMAT) as output:",
                " for asset in assets:",
                "  parts=asset.split('/')",
                "  assert asset and all(part not in ('','..') for part in parts)",
                "  path=os.path.join(root,*parts)",
                "  assert os.path.commonpath((root,os.path.realpath(path))) == root",
                "  before=os.lstat(path)",
                "  assert stat.S_ISREG(before.st_mode) and not stat.S_ISLNK(before.st_mode)",
                f"  assert before.st_size <= {MAX_STATIC_FILE_BYTES}",
                "  total+=before.st_size",
                f"  assert total <= {MAX_STATIC_ASSET_BYTES}",
                "  descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))",
                "  current=os.fstat(descriptor)",
                "  assert (current.st_dev,current.st_ino,current.st_size)==(before.st_dev,before.st_ino,before.st_size)",
                "  info=tarfile.TarInfo(asset)",
                "  info.size=current.st_size;info.mode=stat.S_IMODE(current.st_mode)",
                "  info.uid=current.st_uid;info.gid=current.st_gid;info.mtime=0",
                "  with os.fdopen(descriptor,'rb') as stream: output.addfile(info,stream)",
            )
        )
        try:
            self._run_to_file(
                [
                    "docker",
                    "exec",
                    container,
                    "python3",
                    "-c",
                    program,
                    self.static_root_path,
                    self.static_manifest_path,
                ],
                backup,
                context="Previous static asset archive capture",
            )
            evidence = self._static_asset_archive_evidence(backup, expected_paths)
            if evidence != {
                "assets": self.static_assets_count,
                "asset_bytes": self.static_assets_bytes,
                "assets_sha256": self.static_assets_sha256,
            }:
                raise DeploymentError(
                    "Previous static asset archive differs from its fingerprint"
                )
            backup.chmod(0o400)
            self.static_assets_backup = backup
            self.static_assets_backup_sha256 = sha256_file(backup)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise

    def _capture_static_manifest(self) -> None:
        """Snapshot the exact old manifest before candidate collection begins."""

        if self.backup_path is None:
            raise DeploymentError("Configuration backup was not prepared")
        output = self._manage_live(
            "shell",
            "-c",
            "import json;"
            "from django.contrib.staticfiles.storage import staticfiles_storage as s;"
            "print(json.dumps({'location':str(s.location),"
            "'manifest_name':s.manifest_name},sort_keys=True,separators=(',',':')))",
            context="Previous static manifest location discovery",
        )
        details: Any = None
        for line in reversed(output.splitlines()):
            try:
                details = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(details, dict):
                break
        if not isinstance(details, dict) or set(details) != {
            "location",
            "manifest_name",
        }:
            raise DeploymentError("Static manifest location evidence is malformed")
        location = details["location"]
        manifest_name = details["manifest_name"]
        if (
            not isinstance(location, str)
            or not re.fullmatch(r"/[A-Za-z0-9._/-]{1,254}", location)
            or ".." in PurePosixPath(location).parts
            or not self._safe_static_relative_path(manifest_name)
        ):
            raise DeploymentError("Static manifest location is unsafe")
        manifest_path = (
            PurePosixPath(location) / PurePosixPath(manifest_name)
        ).as_posix()
        container = self._running_service_containers(
            self.config.gunicorn_service,
            context="Previous static manifest container discovery",
        )[0]
        metadata = self._run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-ceu",
                'test -f "$1"; test ! -L "$1"; stat -c "%u:%g:%a" "$1"',
                "buh-static-manifest-metadata",
                manifest_path,
            ],
            context="Previous static manifest metadata capture",
        ).strip()
        match = re.fullmatch(
            r"([0-9]{1,10}):([0-9]{1,10}):([0-7]{3,4})", metadata
        )
        if match is None:
            raise DeploymentError("Previous static manifest metadata is malformed")
        uid, gid, mode = (
            int(value, base)
            for value, base in zip(match.groups(), (10, 10, 8))
        )
        if uid > 2**31 - 1 or gid > 2**31 - 1 or mode > 0o777:
            raise DeploymentError("Previous static manifest metadata is unsafe")
        backup = self.backup_path / "staticfiles.previous.json"
        self._run(
            ["docker", "cp", f"{container}:{manifest_path}", str(backup)],
            context="Previous static manifest capture",
        )
        try:
            backup_details = backup.lstat()
        except FileNotFoundError as exc:
            raise DeploymentError("Previous static manifest was not captured") from exc
        if (
            not stat.S_ISREG(backup_details.st_mode)
            or backup.is_symlink()
            or not 1 <= backup_details.st_size <= MAX_STATIC_MANIFEST_BYTES
        ):
            raise DeploymentError("Previous static manifest capture is unsafe")
        try:
            manifest = json.loads(backup.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("Previous static manifest is unreadable") from exc
        paths = manifest.get("paths") if isinstance(manifest, dict) else None
        if (
            not isinstance(paths, dict)
            or set(manifest) != {"paths", "version", "hash"}
            or manifest.get("version") != "1.1"
            or not isinstance(manifest.get("hash"), str)
            or re.fullmatch(r"[0-9a-f]{12}", manifest["hash"]) is None
            or not 1 <= len(paths) <= MAX_STATIC_MANIFEST_ENTRIES
            or any(
                not self._safe_static_relative_path(source)
                or not self._safe_static_relative_path(stored)
                for source, stored in paths.items()
            )
        ):
            raise DeploymentError("Previous static manifest mapping is unsafe")
        # Docker bind-mounts the file itself into the root-only backup
        # directory. World-readability on that mount is necessary for an
        # unprivileged Gunicorn process; the manifest contains public paths and
        # no secrets, and the enclosing host directory remains mode 0700.
        backup.chmod(0o444)
        self.static_root_path = location
        self.static_manifest_path = manifest_path
        self.static_manifest_backup = backup
        self.static_manifest_sha256 = sha256_file(backup)
        self.static_manifest_entries = len(paths)
        self.static_manifest_metadata = (uid, gid, mode)
        evidence = self._static_manifest_evidence(container)
        expected_assets = len(set(paths.values()))
        if (
            evidence.get("manifest_sha256") != self.static_manifest_sha256
            or evidence.get("entries") != self.static_manifest_entries
            or evidence.get("assets") != expected_assets
            or evidence.get("missing") != 0
        ):
            raise DeploymentError(
                "Previous static manifest assets could not be fingerprinted exactly"
            )
        self.static_assets_sha256 = evidence["assets_sha256"]
        self.static_assets_count = evidence["assets"]
        self.static_assets_bytes = evidence["asset_bytes"]
        self._capture_static_assets(container, set(paths.values()))

    def _static_manifest_evidence(self, container: str) -> Mapping[str, Any]:
        if self.static_manifest_path is None:
            raise DeploymentError("Previous static manifest path was not captured")
        program = (
            "import hashlib,json;"
            "from django.contrib.staticfiles.storage import staticfiles_storage as s;"
            f"path={self.static_manifest_path!r};"
            "raw=open(path,'rb').read();data=json.loads(raw);paths=data.get('paths');"
            "assets=sorted(set(paths.values()));total=0;missing=0;"
            "aggregate=hashlib.sha256();"
            f"max_assets={MAX_STATIC_MANIFEST_ENTRIES};"
            f"max_total={MAX_STATIC_ASSET_BYTES};"
            f"max_file={MAX_STATIC_FILE_BYTES};"
            "assert len(assets)<=max_assets;"
            "\nfor asset in assets:"
            "\n if not s.exists(asset):missing+=1;continue"
            "\n digest=hashlib.sha256();size=0"
            "\n with s.open(asset,'rb') as stream:"
            "\n  while True:"
            "\n   chunk=stream.read(1048576)"
            "\n   if not chunk:break"
            "\n   size+=len(chunk);total+=len(chunk)"
            "\n   if size>max_file or total>max_total:raise RuntimeError('static asset fingerprint limit exceeded')"
            "\n   digest.update(chunk)"
            "\n record=json.dumps({'path':asset,'sha256':digest.hexdigest(),"
            "'size':size},sort_keys=True,separators=(',',':')).encode('ascii')"
            "\n aggregate.update(record+b'\\n')"
            "\nprint(json.dumps({'manifest_sha256':hashlib.sha256(raw).hexdigest(),"
            "'entries':len(paths),'assets':len(assets),'asset_bytes':total,"
            "'assets_sha256':aggregate.hexdigest(),'missing':missing},"
            "sort_keys=True,separators=(',',':')))"
        )
        output = self._manage_container(
            container,
            "shell",
            "-c",
            program,
            context=f"Previous static asset fingerprint in {container}",
        )
        evidence: Any = None
        for line in reversed(output.splitlines()):
            try:
                evidence = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evidence, dict):
                break
        if (
            not isinstance(evidence, dict)
            or set(evidence)
            != {
                "manifest_sha256",
                "entries",
                "assets",
                "asset_bytes",
                "assets_sha256",
                "missing",
            }
            or not isinstance(evidence.get("manifest_sha256"), str)
            or SHA256_RE.fullmatch(evidence["manifest_sha256"]) is None
            or not isinstance(evidence.get("assets_sha256"), str)
            or SHA256_RE.fullmatch(evidence["assets_sha256"]) is None
            or any(
                type(evidence.get(field)) is not int
                for field in ("entries", "assets", "asset_bytes", "missing")
            )
            or not 1 <= evidence["entries"] <= MAX_STATIC_MANIFEST_ENTRIES
            or not 1 <= evidence["assets"] <= evidence["entries"]
            or not 0 <= evidence["asset_bytes"] <= MAX_STATIC_ASSET_BYTES
            or not 0 <= evidence["missing"] <= evidence["assets"]
        ):
            raise DeploymentError("Previous static asset evidence is malformed")
        return evidence

    def _verify_previous_static_manifest(self, container: str) -> None:
        if (
            self.static_manifest_path is None
            or self.static_manifest_sha256 is None
            or self.static_manifest_entries is None
            or self.static_assets_sha256 is None
            or self.static_assets_count is None
            or self.static_assets_bytes is None
        ):
            raise DeploymentError("Previous static manifest evidence was not captured")
        evidence = self._static_manifest_evidence(container)
        if evidence != {
            "manifest_sha256": self.static_manifest_sha256,
            "entries": self.static_manifest_entries,
            "assets": self.static_assets_count,
            "asset_bytes": self.static_assets_bytes,
            "assets_sha256": self.static_assets_sha256,
            "missing": 0,
        }:
            raise DeploymentError(
                "Previous static manifest or referenced hashed asset bytes changed"
            )

    def _verify_previous_static_fallback(self) -> None:
        if not self.previous_web_slots:
            raise DeploymentError("Previous static-safe web slots are unavailable")
        expected = self.previous_images[self.config.gunicorn_service][0]
        self._wait_for_slots(
            self.previous_web_slots,
            expected,
            context="Previous static-safe web slots",
        )
        for container in self.previous_web_slots:
            self._verify_previous_static_manifest(container)
            self._internal_http_checks(container)

    def _protect_static_collection_traffic(self) -> None:
        """Move users onto old slots whose manifest cannot change."""

        if not self.previous_web_slots:
            raise DeploymentError("Previous static-safe web slots are unavailable")
        self._verify_previous_static_fallback()
        # Mark the traffic side effect before touching the upstream. An
        # uncertain Nginx reload must enter the traffic-first rollback path.
        self.traffic_switch_started = True
        self._update_recovery_plan("static-traffic-switch-started")
        self._activate_upstream(
            self.previous_web_slots,
            backup_targets=(self.config.gunicorn_service,),
            context="Pre-collection static-safe traffic switch",
        )
        self._public_smoke_checks()

    def _restore_static_manifest(self) -> None:
        """Atomically restore the old shared manifest before old Gunicorn restarts."""

        if not self.static_collection_started or self.static_manifest_path is None:
            return
        if (
            self.static_manifest_backup is None
            or self.static_manifest_sha256 is None
            or self.static_manifest_metadata is None
            or self.static_assets_backup is None
            or self.static_assets_backup_sha256 is None
            or self.static_root_path is None
            or self.static_assets_count is None
            or self.static_assets_bytes is None
            or self.static_assets_sha256 is None
        ):
            raise DeploymentError("Previous static asset backup is incomplete")
        targets = list(self.candidate_web_slots)
        try:
            targets.extend(
                self._container_names_for_service(self.config.gunicorn_service)
            )
        except DeploymentError:
            pass
        targets = list(dict.fromkeys(targets))
        if not targets:
            raise DeploymentError("No writable container can restore the static manifest")
        asset_container = self._restore_static_assets(targets)
        targets = [asset_container, *(item for item in targets if item != asset_container)]
        temporary = self.static_manifest_path + ".buh-rollback"
        uid, gid, mode = self.static_manifest_metadata
        script = (
            'target="$1"; staged="$2"; expected="$3"; '
            'uid="$4"; gid="$5"; mode="$6"; '
            'test -f "$target"; test ! -L "$target"; '
            'test -f "$staged"; test ! -L "$staged"; '
            'test "${target%/*}" = "${staged%/*}"; '
            'test "$(stat -c "%d" "$target")" = "$(stat -c "%d" "$staged")"; '
            'chown "$uid:$gid" "$staged"; chmod "$mode" "$staged"; '
            'mv -f "$staged" "$target"; '
            'actual="$(sha256sum "$target")"; test "${actual%% *}" = "$expected"; '
            'test "$(stat -c "%u:%g:%a" "$target")" = "$uid:$gid:$mode"'
        )
        for container in targets:
            try:
                self._run(
                    [
                        "docker",
                        "cp",
                        str(self.static_manifest_backup),
                        f"{container}:{temporary}",
                    ],
                    context="Previous static manifest restoration staging",
                )
                self._run(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "0",
                        container,
                        "sh",
                        "-ceu",
                        script,
                        "buh-static-rollback",
                        self.static_manifest_path,
                        temporary,
                        self.static_manifest_sha256,
                        str(uid),
                        str(gid),
                        format(mode, "o"),
                    ],
                    context="Previous static manifest atomic restoration",
                )
                self._verify_previous_static_manifest(container)
                return
            except DeploymentError:
                continue
        raise DeploymentError("Previous static manifest could not be restored atomically")

    def _restore_static_assets(self, targets: Sequence[str]) -> str:
        """Atomically restore every asset referenced by the old manifest."""

        if (
            self.static_assets_backup is None
            or self.static_assets_backup_sha256 is None
            or self.static_root_path is None
        ):
            raise DeploymentError("Previous static asset backup is incomplete")
        if sha256_file(self.static_assets_backup) != self.static_assets_backup_sha256:
            raise DeploymentError("Previous static asset backup changed after capture")
        remote_archive = f"/tmp/.buh-static-{secrets.token_hex(16)}.tar"
        program = "\n".join(
            (
                "import hashlib,os,stat,sys,tarfile,tempfile",
                "root,archive_path,expected_hash=sys.argv[1:]",
                "root=os.path.abspath(root)",
                "root_stat=os.lstat(root)",
                "assert stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISLNK(root_stat.st_mode)",
                "digest=hashlib.sha256()",
                "with open(archive_path,'rb') as stream:",
                " while True:",
                "  chunk=stream.read(1048576)",
                "  if not chunk: break",
                "  digest.update(chunk)",
                "assert digest.hexdigest()==expected_hash",
                "with tarfile.open(archive_path,'r:') as archive:",
                " members=archive.getmembers()",
                f" assert 1 <= len(members) <= {MAX_STATIC_MANIFEST_ENTRIES}",
                " names=set();total=0",
                " for member in members:",
                "  parts=member.name.split('/')",
                "  assert member.isfile() and member.name not in names",
                "  assert member.name and all(part not in ('','..') for part in parts)",
                f"  assert 0 <= member.size <= {MAX_STATIC_FILE_BYTES}",
                f"  total+=member.size;assert total <= {MAX_STATIC_ASSET_BYTES}",
                "  assert 0 <= member.uid <= 2**31-1 and 0 <= member.gid <= 2**31-1",
                "  assert 0 <= member.mode <= 0o777",
                "  names.add(member.name)",
                "  parent=root",
                "  for part in parts[:-1]:",
                "   candidate=os.path.join(parent,part)",
                "   try:",
                "    details=os.lstat(candidate)",
                "    assert stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode)",
                "   except FileNotFoundError:",
                "    os.mkdir(candidate,0o755)",
                "    directory=os.open(parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))",
                "    try: os.fsync(directory)",
                "    finally: os.close(directory)",
                "   parent=candidate",
                "  target=os.path.join(parent,parts[-1])",
                "  assert os.path.commonpath((root,os.path.abspath(target)))==root",
                "  try:",
                "   current=os.lstat(target)",
                "   assert stat.S_ISREG(current.st_mode) and not stat.S_ISLNK(current.st_mode)",
                "  except FileNotFoundError: pass",
                "  source=archive.extractfile(member)",
                "  assert source is not None",
                "  descriptor,temporary=tempfile.mkstemp(prefix='.buh-rollback-',dir=parent)",
                "  try:",
                "   written=0",
                "   with os.fdopen(descriptor,'wb',closefd=True) as output:",
                "    while True:",
                "     chunk=source.read(1048576)",
                "     if not chunk: break",
                "     written+=len(chunk);assert written <= member.size",
                "     output.write(chunk)",
                "    assert written==member.size",
                "    output.flush();os.fsync(output.fileno())",
                "    os.fchmod(output.fileno(),member.mode)",
                "    os.fchown(output.fileno(),member.uid,member.gid)",
                "    os.fsync(output.fileno())",
                "   os.replace(temporary,target)",
                "   directory=os.open(parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))",
                "   try: os.fsync(directory)",
                "   finally: os.close(directory)",
                "  finally:",
                "   try: os.unlink(temporary)",
                "   except FileNotFoundError: pass",
                "os.unlink(archive_path)",
            )
        )
        for container in targets:
            try:
                self._run(
                    [
                        "docker",
                        "cp",
                        str(self.static_assets_backup),
                        f"{container}:{remote_archive}",
                    ],
                    context="Previous static asset restoration staging",
                )
                self._run(
                    [
                        "docker",
                        "exec",
                        "--user",
                        "0",
                        container,
                        "python3",
                        "-c",
                        program,
                        self.static_root_path,
                        remote_archive,
                        self.static_assets_backup_sha256,
                    ],
                    context="Previous static asset atomic restoration",
                )
                return container
            except DeploymentError:
                continue
        raise DeploymentError("Previous static assets could not be restored atomically")

    def _web_slot_names(
        self, bundle: ValidatedBundle, role: str
    ) -> tuple[str, ...]:
        count = self.auth_replica_counts.get(self.config.gunicorn_service)
        if count is None or not 1 <= count <= MAX_SERVICE_REPLICAS:
            raise DeploymentError("Gunicorn replica count was not captured")
        return tuple(
            self._safe_container_name(
                f"buh-web-{role}-{bundle.request.attempt_id}-{index + 1}"
            )
            for index in range(count)
        )

    def _slot_state_is_ready(self, name: str, expected_image: str) -> bool:
        try:
            details = self._run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}}|{{.RestartCount}}|{{.Image}}",
                    name,
                ],
                context=f"Web slot state for {name}",
            ).strip()
        except DeploymentError:
            return False
        parts = details.split("|")
        return (
            len(parts) == 3
            and parts[0] == "running"
            and parts[1] == "0"
            and parts[2] == expected_image
        )

    def _wait_for_slots(
        self, names: Sequence[str], expected_image: str, *, context: str
    ) -> None:
        for _ in range(self.config.health_attempts):
            if all(
                self._slot_state_is_ready(name, expected_image) for name in names
            ):
                return
            time.sleep(self.config.health_interval_seconds)
        raise DeploymentError(f"{context} did not become stable")

    def _start_web_slots(
        self,
        bundle: ValidatedBundle,
        *,
        role: str,
        expected_image: str,
    ) -> tuple[str, ...]:
        names = self._web_slot_names(bundle, role)
        started: list[str] = []
        for name in names:
            started.append(name)
            if role == "candidate":
                self.candidate_web_slots = tuple(started)
            else:
                self.previous_web_slots = tuple(started)
            self._update_recovery_plan(f"{role}-slot-start-{len(started)}")
            arguments = [
                "run",
                "--detach",
                "--no-deps",
                "--name",
                name,
            ]
            if role == "previous":
                if (
                    self.static_manifest_backup is None
                    or self.static_manifest_path is None
                ):
                    raise DeploymentError(
                        "Previous web slot lacks an isolated static manifest"
                    )
                arguments.extend(
                    (
                        "--volume",
                        f"{self.static_manifest_backup}:{self.static_manifest_path}:ro",
                    )
                )
            arguments.append(self.config.gunicorn_service)
            self._compose(
                *arguments,
                context=f"{role.title()} web slot startup",
            )
        self._wait_for_slots(names, expected_image, context=f"{role.title()} web slots")
        return names

    def _container_version_probe(
        self, container: str, bundle: ValidatedBundle
    ) -> None:
        expected = self._expected_versions(bundle)
        program = (
            "import importlib.metadata,json;"
            f"names={json.dumps(sorted(expected))};"
            "print(json.dumps({name:importlib.metadata.version(name) for name in names},"
            "sort_keys=True,separators=(',',':')))"
        )
        output = self._run(
            ["docker", "exec", container, "python3", "-c", program],
            context=f"Package verification in web slot {container}",
        )
        actual: Any = None
        for line in reversed(output.splitlines()):
            try:
                actual = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(actual, dict):
                break
        if actual != expected:
            raise DeploymentError(f"Package versions differ in web slot {container}")

    def _manage_container(
        self, container: str, *arguments: str, context: str
    ) -> str:
        return self._run(
            [
                "docker",
                "exec",
                container,
                "python3",
                self.config.manage_py,
                *arguments,
            ],
            context=context,
        )

    def _verify_static_asset(self, container: str | None) -> None:
        """Prove collectstatic published a retained content-addressed asset."""

        program = (
            "from django.contrib.staticfiles.storage import staticfiles_storage as s;"
            f"source={STATIC_HEALTH_ASSET!r};"
            "stored=s.stored_name(source);"
            "assert stored != source and s.exists(stored);"
            "print(stored)"
        )
        arguments = ("shell", "-c", program)
        if container is None:
            self._manage_live(
                *arguments, context="Active content-hashed static asset verification"
            )
        else:
            self._manage_container(
                container,
                *arguments,
                context="Candidate content-hashed static asset verification",
            )

    def _internal_http_checks(self, container: str) -> None:
        checks = [
            {
                "path": urlsplit(check.url).path
                + (f"?{urlsplit(check.url).query}" if urlsplit(check.url).query else ""),
                "statuses": list(check.statuses),
            }
            for check in self.config.smoke_checks
        ]
        program = (
            "import http.client,json;"
            f"checks=json.loads({json.dumps(json.dumps(checks))});"
            "actual=[];"
            "\nfor check in checks:"
            "\n c=http.client.HTTPConnection('127.0.0.1',"
            f"{self.config.gunicorn_port},timeout=15);"
            "\n c.request('GET',check['path'],headers={'Host':'auth.b-uh.com',"
            "'User-Agent':'B-UH-Platform-v2-internal-health/1'});"
            "\n r=c.getresponse();actual.append([check['path'],r.status,r.getheader('Location')]);"
            "\n r.read();c.close();"
            "\nprint(json.dumps(actual,separators=(',',':')))"
        )
        output = self._run(
            ["docker", "exec", container, "python3", "-c", program],
            context=f"Internal HTTP checks in {container}",
        )
        actual: Any = None
        for line in reversed(output.splitlines()):
            try:
                actual = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(actual, list):
                break
        expected_paths = {item["path"]: set(item["statuses"]) for item in checks}
        if (
            not isinstance(actual, list)
            or len(actual) != len(checks)
            or any(
                not isinstance(item, list)
                or len(item) != 3
                or item[0] not in expected_paths
                or item[1] not in expected_paths[item[0]]
                or (
                    300 <= item[1] <= 399
                    and not self._safe_redirect_location(item[0], item[2])
                )
                for item in actual
            )
            or len({item[0] for item in actual if isinstance(item, list) and item})
            != len(checks)
        ):
            raise DeploymentError(
                f"Internal HTTP routes returned unexpected statuses in {container}"
            )

    def _container_names_for_service(self, service: str) -> tuple[str, ...]:
        names: list[str] = []
        for container in self._running_service_containers(
            service, context=f"Container name discovery for {service}"
        ):
            name = self._run(
                ["docker", "inspect", "--format", "{{.Name}}", container],
                context=f"Container name verification for {service}",
            ).strip().lstrip("/")
            names.append(self._safe_container_name(name))
        return tuple(names)

    def _remove_web_slots(self, names: Sequence[str]) -> None:
        for name in tuple(dict.fromkeys(names)):
            try:
                exists = self._run(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        f"name=^/{name}$",
                    ],
                    context=f"Web slot cleanup discovery for {name}",
                ).strip()
                if exists:
                    self._run(
                        ["docker", "rm", "--force", "--volumes", name],
                        context=f"Web slot cleanup for {name}",
                    )
            except DeploymentError:
                raise
        removed = set(names)
        self.candidate_web_slots = tuple(
            name for name in self.candidate_web_slots if name not in removed
        )
        self.previous_web_slots = tuple(
            name for name in self.previous_web_slots if name not in removed
        )

    def _retag_image(
        self,
        source: str,
        reference: str,
        expected_image: str,
        *,
        context: str,
    ) -> None:
        self._run(["docker", "image", "tag", source, reference], context=context)
        resolved = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            context=f"{context} verification",
        ).strip()
        if resolved != expected_image:
            raise DeploymentError(f"{context} resolved to the wrong image")

    def _candidate_runtime_checks(self, bundle: ValidatedBundle) -> None:
        if not self.candidate_web_slots:
            raise DeploymentError("Candidate web slots were not started")
        for container in self.candidate_web_slots:
            self._container_version_probe(container, bundle)
            self._internal_http_checks(container)
        primary = self.candidate_web_slots[0]
        self._manage_container(
            primary, "check", "--no-color", context="Candidate live Django checks"
        )
        migrations = self._manage_container(
            primary,
            "showmigrations",
            "--plan",
            "--no-color",
            context="Candidate live migration state",
        )
        if re.search(r"(?m)^\s*\[ \]", migrations):
            raise DeploymentError("Candidate web slot has pending migrations")
        database = self._manage_container(
            primary,
            "shell",
            "-c",
            "from django.db import connection;"
            "connection.ensure_connection();print(connection.vendor)",
            context="Candidate database connectivity",
        )
        if "mysql" not in database.lower():
            raise DeploymentError("Candidate did not verify the MariaDB connection")
        self._verify_static_asset(primary)
        for command in self.config.application_health_commands:
            self._manage_container(
                primary,
                command,
                "--no-color",
                context=f"Read-only application health command {command}",
            )

    def candidate_health(self, bundle: ValidatedBundle) -> None:
        expected = self.candidate_image_ids.get(self.config.gunicorn_service)
        if expected is None:
            raise DeploymentError("Candidate Gunicorn image was not captured")
        self._start_web_slots(
            bundle, role="candidate", expected_image=expected
        )
        retained = {
            **self.auth_replica_counts,
            self.config.database_service: 1,
            self.config.redis_service: 1,
            self.config.proxy_service: 1,
        }
        if not self._containers_healthy(retained, zero_restart_services=set()):
            raise DeploymentError(
                "A retained production container changed or restarted during preparation"
            )
        self._verify_previous_static_fallback()
        self._candidate_runtime_checks(bundle)
        self._redis_health()
        self._celery_health()
        self._scan_new_logs(
            (
                *self.config.auth_services,
                *self.candidate_web_slots,
                *self.previous_web_slots,
            ),
            owner_transition_phase=(
                "candidate-health" if self.recovery_baseline_verified else None
            ),
        )

    def switch_traffic(self, bundle: ValidatedBundle) -> None:
        del bundle
        if not self.candidate_web_slots:
            raise DeploymentError("Candidate web slots are unavailable")
        if not self.previous_web_slots:
            raise DeploymentError("Previous static-safe web slots are unavailable")
        # Mark before writing or signaling Nginx: an uncertain reload is treated
        # as a traffic side effect and rollback routes users first.
        self.traffic_switch_started = True
        self._update_recovery_plan("candidate-traffic-switch-started")
        self._activate_upstream(
            self.candidate_web_slots,
            backup_targets=(
                *self.previous_web_slots,
                self.config.gunicorn_service,
            ),
            context="Candidate traffic switch",
        )
        self._public_smoke_checks()

    def _scale_arguments_for(self, services: Sequence[str]) -> tuple[str, ...]:
        arguments: list[str] = []
        for service in services:
            count = self.auth_replica_counts.get(service)
            if count is None or not 1 <= count <= MAX_SERVICE_REPLICAS:
                raise DeploymentError(
                    f"Live service {service} replica count was not captured"
                )
            arguments.extend(("--scale", f"{service}={count}"))
        return tuple(arguments)

    def _wait_for_compose_services(
        self,
        expected: Mapping[str, int],
        *,
        zero_restarts: set[str],
        context: str,
    ) -> None:
        for _ in range(self.config.health_attempts):
            if self._containers_healthy(
                expected, zero_restart_services=zero_restarts
            ):
                return
            time.sleep(self.config.health_interval_seconds)
        raise DeploymentError(f"{context} did not become stable")

    def _containers_healthy(
        self,
        expected_replicas: Mapping[str, int],
        *,
        zero_restart_services: set[str],
    ) -> bool:
        try:
            for service, expected_count in expected_replicas.items():
                if not 1 <= expected_count <= MAX_SERVICE_REPLICAS:
                    return False
                ids = self._running_service_containers(
                    service, context=f"Container discovery for {service}"
                )
                if len(ids) != expected_count:
                    return False
                for container in ids:
                    state = self._run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            "{{.State.Status}}|{{.RestartCount}}|"
                            "{{if .State.Health}}{{.State.Health.Status}}"
                            "{{else}}none{{end}}",
                            container,
                        ],
                        context=f"Container health for {service}",
                    ).strip()
                    parts = state.split("|")
                    restart_count = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None
                    if (
                        len(parts) != 3
                        or parts[0] != "running"
                        or restart_count is None
                        or parts[2] not in {"none", "healthy"}
                        or (
                            service in zero_restart_services
                            and restart_count != 0
                        )
                        or (
                            service not in zero_restart_services
                            and self.restart_baselines.get(container)
                            != restart_count
                        )
                    ):
                        return False
            return True
        except DeploymentError:
            return False

    def replace_workers(self, bundle: ValidatedBundle) -> None:
        beat = self.config.beat_service
        if self.auth_replica_counts.get(beat) != 1:
            raise DeploymentError("Exactly one live Celery beat replica is required")
        workers = tuple(
            service
            for service in self.config.auth_services
            if service not in {self.config.gunicorn_service, beat}
        )
        if self.recovery_baseline_verified:
            cutoff = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._scan_new_logs(
                (*self.config.auth_services, *self.candidate_web_slots),
                owner_transition_phase="worker-cutover",
            )
            self.log_since = cutoff
        self.workers_replacement_started = True
        self._update_recovery_plan("worker-replacement-started")
        if workers:
            self._compose(
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                *self._scale_arguments_for(workers),
                *workers,
                context="Celery worker replacement",
            )
        # A deliberate stop-before-start creates a short scheduling gap but
        # proves there can never be two beat schedulers publishing duplicate work.
        self._compose("stop", beat, context="Celery beat singleton stop")
        self._compose(
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--force-recreate",
            "--scale",
            f"{beat}=1",
            beat,
            context="Celery beat singleton replacement",
        )
        replaced = (*workers, beat)
        self._wait_for_compose_services(
            {service: self.auth_replica_counts[service] for service in replaced},
            zero_restarts=set(replaced),
            context="Replacement Celery services",
        )
        self._require_live_images(
            {service: self.candidate_image_ids[service] for service in replaced}
        )
        for service in replaced:
            self._version_probe(service, bundle, live=True)
        self._celery_health()
        self._scan_new_logs((*replaced, *self.candidate_web_slots))

    def _redis_health(self) -> None:
        output = self._compose(
            "exec",
            "-T",
            self.config.redis_service,
            "redis-cli",
            "ping",
            context="Redis health check",
        )
        if output.strip().upper() != "PONG":
            raise DeploymentError("Redis did not return PONG")

    def _expected_celery_nodes(self) -> frozenset[str]:
        """Derive the exact Celery nodename owned by every running worker."""

        worker_services = tuple(
            service
            for service in self.config.auth_services
            if service not in {
                self.config.gunicorn_service,
                self.config.beat_service,
            }
        )
        nodes: set[str] = set()
        for service in worker_services:
            containers = self._running_service_containers(
                service, context=f"Celery identity discovery for {service}"
            )
            if len(containers) != self.auth_replica_counts.get(service):
                raise DeploymentError(
                    f"Celery service {service} changed replica count"
                )
            for container in containers:
                output = self._run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Config.Hostname}}\n{{json .Config.Entrypoint}}\n"
                        "{{json .Config.Cmd}}",
                        container,
                    ],
                    context=f"Celery node identity for {service}",
                )
                lines = output.splitlines()
                if len(lines) != 3:
                    raise DeploymentError(
                        f"Celery service {service} returned malformed identity metadata"
                    )
                hostname = lines[0]
                if not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", hostname
                ):
                    raise DeploymentError(
                        f"Celery service {service} returned an unsafe hostname"
                    )
                try:
                    entrypoint = json.loads(lines[1])
                    command = json.loads(lines[2])
                except json.JSONDecodeError as exc:
                    raise DeploymentError(
                        f"Celery service {service} returned malformed process metadata"
                    ) from exc
                if entrypoint is None:
                    entrypoint = []
                if command is None:
                    command = []
                if (
                    not isinstance(entrypoint, list)
                    or not isinstance(command, list)
                    or any(not isinstance(item, str) for item in (*entrypoint, *command))
                ):
                    raise DeploymentError(
                        f"Celery service {service} returned unsafe process metadata"
                    )
                arguments = [*entrypoint, *command]
                patterns: list[str] = []
                for index, argument in enumerate(arguments):
                    if argument in {"-n", "--hostname"}:
                        if index + 1 >= len(arguments):
                            raise DeploymentError(
                                f"Celery service {service} has no nodename value"
                            )
                        patterns.append(arguments[index + 1])
                    elif argument.startswith("--hostname="):
                        patterns.append(argument.partition("=")[2])
                    elif argument.startswith("-n") and len(argument) > 2:
                        patterns.append(argument[2:].lstrip("="))
                if len(patterns) != 1 or not patterns[0]:
                    raise DeploymentError(
                        f"Celery service {service} must declare one explicit nodename"
                    )
                short_hostname, separator, domain = hostname.partition(".")
                node = patterns[0]
                placeholder = "\x00"
                node = node.replace("%%", placeholder)
                node = node.replace("%h", hostname)
                node = node.replace("%n", short_hostname)
                node = node.replace("%d", domain if separator else "")
                node = node.replace(placeholder, "%")
                if "%" in node or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}", node
                ):
                    raise DeploymentError(
                        f"Celery service {service} has an unsafe resolved nodename"
                    )
                if node in nodes:
                    raise DeploymentError("Celery worker nodenames are not unique")
                nodes.add(node)
        expected_count = sum(
            self.auth_replica_counts.get(service, 0) for service in worker_services
        )
        if not nodes or len(nodes) != expected_count:
            raise DeploymentError("Celery worker identity set is incomplete")
        return frozenset(nodes)

    def _celery_inspect(
        self, command: str, *, expected_nodes: frozenset[str]
    ) -> dict[str, Any]:
        output = self._compose(
            "exec",
            "-T",
            self.config.worker_service,
            "celery",
            "--app=myauth",
            "inspect",
            command,
            "--timeout=5",
            "--json",
            context=f"Celery {command} inspection",
        )
        start = output.find("{")
        if start < 0:
            raise DeploymentError(f"Celery {command} did not return JSON")
        try:
            result = json.loads(output[start:])
        except json.JSONDecodeError as exc:
            raise DeploymentError(f"Celery {command} returned malformed JSON") from exc
        if (
            not isinstance(result, dict)
            or not 1 <= len(result) <= MAX_SERVICE_REPLICAS
            or set(result) != expected_nodes
            or any(
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,199}", name)
                for name in result
            )
        ):
            raise DeploymentError(
                f"Celery {command} did not return every expected worker"
            )
        return result

    def _celery_health(self) -> None:
        expected_nodes = self._expected_celery_nodes()
        ping = self._celery_inspect("ping", expected_nodes=expected_nodes)
        if any(
            not isinstance(value, dict) or value.get("ok") != "pong"
            for value in ping.values()
        ):
            raise DeploymentError("Celery workers did not return a heartbeat")

        queue_report = self._celery_inspect(
            "active_queues", expected_nodes=expected_nodes
        )
        queues: set[str] = set()
        for value in queue_report.values():
            if not isinstance(value, list):
                raise DeploymentError("Celery active queue evidence is malformed")
            for queue in value:
                name = queue.get("name") if isinstance(queue, dict) else None
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", name)
                ):
                    raise DeploymentError("Celery active queue evidence is malformed")
                queues.add(name)
        missing_queues = [
            queue for queue in self.config.required_celery_queues if queue not in queues
        ]
        if missing_queues:
            raise DeploymentError(
                f"Celery is missing expected queues: {missing_queues}"
            )

        task_report = self._celery_inspect(
            "registered", expected_nodes=expected_nodes
        )
        tasks: set[str] = set()
        for value in task_report.values():
            if (
                not isinstance(value, list)
                or any(
                    not isinstance(task, str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", task
                    )
                    for task in value
                )
            ):
                raise DeploymentError("Celery registered task evidence is malformed")
            tasks.update(value)
        missing_tasks = [
            task for task in self.config.required_celery_tasks if task not in tasks
        ]
        if missing_tasks:
            raise DeploymentError(
                f"Celery is missing expected tasks: {missing_tasks}"
            )

    @staticmethod
    def _safe_redirect_location(request_path: str, location: Any) -> bool:
        if (
            not isinstance(location, str)
            or not 1 <= len(location) <= 500
            or any(ord(character) < 32 or ord(character) == 127 for character in location)
            or "\\" in location
        ):
            return False
        target = urlsplit(urljoin("https://auth.b-uh.com" + request_path, location))
        if (
            target.scheme != "https"
            or target.netloc != "auth.b-uh.com"
            or target.username is not None
            or target.password is not None
            or bool(target.fragment)
            or not target.path.startswith("/")
            or "//" in target.path
            or "%" in target.path
            or any(part in {".", ".."} for part in target.path.split("/"))
            or target.path == request_path
        ):
            return False
        protected = {
            "/",
            "/moon-tax/",
            "/structure-operations/",
            "/mining-analytics/",
        }
        if request_path in protected and target.path != "/account/login/":
            return False
        return True

    def _public_smoke_checks(self) -> None:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        for check in self.config.smoke_checks:
            request = urllib.request.Request(
                check.url,
                headers={"User-Agent": "B-UH-Platform-v2-health/1"},
            )
            location: str | None = None
            try:
                with opener.open(request, timeout=15) as response:
                    status = response.status
                    location = response.headers.get("Location")
            except urllib.error.HTTPError as exc:
                status = exc.code
                location = (
                    exc.headers.get("Location") if exc.headers is not None else None
                )
            except (OSError, urllib.error.URLError) as exc:
                raise DeploymentError("An HTTPS smoke route was unreachable") from exc
            if status not in check.statuses:
                raise DeploymentError(
                    f"An HTTPS smoke route returned unexpected status {status}"
                )
            if 300 <= status <= 399 and not self._safe_redirect_location(
                urlsplit(check.url).path, location
            ):
                raise DeploymentError(
                    "An HTTPS smoke route returned an unsafe redirect Location"
                )

    def _scan_new_logs(
        self,
        services_and_slots: Sequence[str],
        *,
        owner_transition_phase: str | None = None,
    ) -> tuple[str, ...]:
        infrastructure = (
            self.config.proxy_service,
            self.config.database_service,
            self.config.redis_service,
        )
        compose_services = (*self.config.auth_services, *infrastructure)
        requested = set(services_and_slots)
        service_set = tuple(
            service
            for service in compose_services
            if service in requested or service in infrastructure
        )
        logs_by_source: list[tuple[str, str]] = []
        for service in service_set:
            logs_by_source.append(
                (
                    service,
                    self._compose(
                        "logs",
                        f"--since={self.log_since}",
                        "--no-color",
                        "--no-log-prefix",
                        service,
                        bounded_output=True,
                        context=f"New deployment log scan for {service}",
                    ),
                )
            )
        for slot in services_and_slots:
            if slot in compose_services:
                continue
            logs_by_source.append(
                (
                    slot,
                    self._run(
                        [
                            "docker",
                            "logs",
                            f"--since={self.log_since}",
                            slot,
                        ],
                        bounded_output=True,
                        context=f"New web-slot log scan for {slot}",
                    ),
                )
            )
        allowed: list[str] = []
        rejected: list[str] = []
        allowlist: list[re.Pattern[str]] = []
        for signature in self.config.fatal_log_allowlist:
            match = FATAL_LOG_ALLOWLIST_RE.fullmatch(signature)
            if match is None:  # Defensive: ReceiverConfig already rejects this.
                raise DeploymentError("Fatal log allowlist signature is invalid")
            component, exception, message = match.groups()
            token_boundary = r"[A-Za-z0-9_.-]"
            allowlist.append(
                re.compile(
                    rf"{ALLOWLISTED_LOG_LEVEL_RE}"
                    rf"{re.escape(component)}(?!{token_boundary})"
                    rf"(?:[ \t]+|[|:=-][ \t]*)"
                    rf"{re.escape(exception)}(?!{token_boundary}):?[ \t]+"
                    rf"{re.escape(message)}[.!]?",
                    re.IGNORECASE,
                )
            )
        owner_service_findings: dict[str, set[int]] = {}
        if owner_transition_phase is not None:
            if (
                owner_transition_phase
                not in {"candidate-health", "worker-cutover", "rollback"}
                or not self.recovery_baseline_verified
            ):
                raise DeploymentError("Discord owner transition phase is invalid")
            owner = recovery_policy.load_policy()["host_baseline"]["discord_owner"]
            retry = re.compile(
                rf".*update_nickname failed for user {re.escape(owner['auth_username'])}, "
                r"retrying in 60 secs[.!]?$",
                re.IGNORECASE,
            )
            denied = re.compile(
                r".*(?:403 Forbidden \(error code: 50013\): Missing Permissions|"
                r"Discord HTTP 403, code 50013: Missing Permissions)[.!]?$",
                re.IGNORECASE,
            )
            worker_services = {
                service
                for service in self.config.auth_services
                if service not in {
                    self.config.gunicorn_service,
                    self.config.beat_service,
                }
            }
            for source, text in logs_by_source:
                if source not in worker_services:
                    continue
                lines = text.splitlines()
                indexes: set[int] = set()
                for index, line in enumerate(lines):
                    if denied.fullmatch(line) is None:
                        continue
                    start = max(0, index - 6)
                    matches = [
                        candidate
                        for candidate in range(start, index)
                        if retry.fullmatch(lines[candidate]) is not None
                    ]
                    if len(matches) == 1:
                        indexes.update((matches[0], index))
                if indexes:
                    owner_service_findings[source] = indexes
        for source, text in logs_by_source:
            owner_indexes = owner_service_findings.get(source, set())
            for index, raw_line in enumerate(text.splitlines()):
                if index in owner_indexes:
                    safe = (
                        f"expected Discord guild-owner nickname 50013 during "
                        f"{owner_transition_phase} on retained {source}"
                    )
                    if safe not in allowed and len(allowed) < MAX_RETAINED_LOG_FINDINGS:
                        allowed.append(safe)
                    continue
                if not FATAL_LOG_RE.search(raw_line):
                    continue
                safe = _safe_report_text(raw_line, MAX_RETAINED_LOG_FINDING_CHARS)
                if any(pattern.fullmatch(raw_line) for pattern in allowlist):
                    if safe not in allowed and len(allowed) < MAX_RETAINED_LOG_FINDINGS:
                        allowed.append(safe)
                elif len(rejected) < MAX_RETAINED_LOG_FINDINGS:
                    rejected.append(safe)
        if rejected:
            raise DeploymentError(
                "New fatal AllianceAuth log pattern was detected: "
                + rejected[0]
            )
        return tuple(allowed)

    def _write_health_report(
        self,
        bundle: ValidatedBundle,
        *,
        result: str,
        iterations: int,
        allowed_findings: Sequence[str],
        failure: str | None = None,
    ) -> Mapping[str, Any]:
        if self.backup_path is None:
            raise DeploymentError("Deployment evidence directory is unavailable")
        safe_findings = [
            _safe_report_text(item, MAX_RETAINED_LOG_FINDING_CHARS)
            for item in list(allowed_findings)[:MAX_RETAINED_LOG_FINDINGS]
        ]
        safe_findings = [item for item in safe_findings if item]
        safe_failure = _safe_report_text(failure or "", 500) or None
        if result == "failed" and safe_failure is None:
            safe_failure = "Stabilization health check failed"
        report = {
            "schema_version": 1,
            "attempt_id": bundle.request.attempt_id,
            "release_commit": bundle.request.release_commit,
            "manifest_sha256": bundle.request.manifest_sha256,
            "result": result,
            "stabilization_seconds": self.config.stabilization_seconds,
            "iterations": iterations,
            "checks": [
                "application_commands",
                "candidate_and_active_http",
                "celery_queues_and_tasks",
                "container_images_and_restarts",
                "database_and_migrations",
                "django",
                "new_logs",
                "public_routes",
                "redis",
                "static_assets",
            ],
            "allowed_log_findings": safe_findings,
            "failure": safe_failure,
        }
        path = self.backup_path / "HEALTH.json"
        _atomic_text(
            path, canonical_json_bytes(report).decode("ascii"), 0o600
        )
        evidence = {
            "filename": path.name,
            "sha256": sha256_file(path),
            "result": result,
            "iterations": iterations,
            "allowed_log_findings": len(report["allowed_log_findings"]),
            "warnings": list(report["allowed_log_findings"]),
            "failure": report["failure"],
        }
        self._last_stabilization_evidence = evidence
        return evidence

    def stabilization_evidence(self) -> Mapping[str, Any] | None:
        if self._last_stabilization_evidence is None:
            return None
        evidence = dict(self._last_stabilization_evidence)
        evidence["warnings"] = list(evidence["warnings"])
        return evidence

    def _functional_health(
        self, bundle: ValidatedBundle, *, promoted: bool
    ) -> tuple[str, ...]:
        gunicorn = self.config.gunicorn_service
        expected_images = dict(self.candidate_image_ids)
        zero_restarts = set(self.config.auth_services)
        if not promoted:
            expected_images[gunicorn] = self.previous_images[gunicorn][0]
            zero_restarts.remove(gunicorn)
        expected_replicas = {
            **self.auth_replica_counts,
            self.config.database_service: 1,
            self.config.redis_service: 1,
            self.config.proxy_service: 1,
        }
        if not self._containers_healthy(
            expected_replicas, zero_restart_services=zero_restarts
        ):
            raise DeploymentError("Required containers are not stable")
        self._require_live_images(expected_images)
        for service in self.config.auth_services:
            if service != gunicorn or promoted:
                self._version_probe(service, bundle, live=True)
        self._wait_for_slots(
            self.candidate_web_slots,
            self.candidate_image_ids[gunicorn],
            context="Candidate web slots",
        )
        self._candidate_runtime_checks(bundle)
        for container in self._container_names_for_service(gunicorn):
            self._internal_http_checks(container)
        if self.previous_web_slots:
            self._wait_for_slots(
                self.previous_web_slots,
                self.previous_images[gunicorn][0],
                context="Previous web rollback slots",
            )
            for container in self.previous_web_slots:
                self._verify_previous_static_manifest(container)
                self._internal_http_checks(container)
        self._redis_health()
        self._celery_health()
        self._public_smoke_checks()
        return self._scan_new_logs(
            (
                *self.config.auth_services,
                *self.candidate_web_slots,
                *self.previous_web_slots,
            )
        )

    def stabilize(self, bundle: ValidatedBundle) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.config.stabilization_seconds
        iterations = 0
        allowed: list[str] = []
        try:
            while True:
                findings = self._functional_health(bundle, promoted=False)
                for finding in findings:
                    if (
                        finding not in allowed
                        and len(allowed) < MAX_RETAINED_LOG_FINDINGS
                    ):
                        allowed.append(finding)
                iterations += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(
                    min(self.config.stabilization_interval_seconds, remaining)
                )
        except BaseException as error:
            self._write_health_report(
                bundle,
                result="failed",
                iterations=iterations,
                allowed_findings=allowed,
                failure=str(error),
            )
            raise
        return self._write_health_report(
            bundle,
            result="success",
            iterations=iterations,
            allowed_findings=allowed,
        )

    def _start_previous_web_slots(self, bundle: ValidatedBundle) -> None:
        gunicorn = self.config.gunicorn_service
        previous_image = self.previous_images[gunicorn][0]
        if not self.previous_web_slots:
            self._start_web_slots(
                bundle, role="previous", expected_image=previous_image
            )
        self._verify_previous_static_fallback()

    def promote_web(self, bundle: ValidatedBundle) -> None:
        gunicorn = self.config.gunicorn_service
        self._start_previous_web_slots(bundle)
        # Keep exact previous-image slots as Nginx backup servers before the
        # Compose Gunicorn service is replaced. Candidate traffic stays primary,
        # but simultaneous candidate failure cannot expose a raw 502.
        self._activate_upstream(
            self.candidate_web_slots,
            backup_targets=self.previous_web_slots,
            context="Candidate rollback fallback switch",
        )
        self._public_smoke_checks()
        self.gunicorn_replacement_started = True
        self.live_replacement_started = True
        self._update_recovery_plan("gunicorn-replacement-started")
        self._compose(
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--force-recreate",
            *self._scale_arguments_for((gunicorn,)),
            gunicorn,
            context="Active Gunicorn replacement behind candidate traffic",
        )
        self._wait_for_compose_services(
            {gunicorn: self.auth_replica_counts[gunicorn]},
            zero_restarts={gunicorn},
            context="Replacement active Gunicorn",
        )
        self._require_live_images({gunicorn: self.candidate_image_ids[gunicorn]})
        self._version_probe(gunicorn, bundle, live=True)
        for container in self._container_names_for_service(gunicorn):
            self._internal_http_checks(container)
        self._activate_upstream(
            (gunicorn,),
            backup_targets=self.candidate_web_slots,
            context="Promoted Gunicorn traffic switch",
        )
        self._public_smoke_checks()

    def finalize(self, bundle: ValidatedBundle) -> None:
        if self.staged_release is None:
            raise DeploymentError("Release was not staged")
        # Recheck the fully promoted stack before deleting either safety slot.
        self._functional_health(bundle, promoted=True)
        self._activate_upstream(
            (self.config.gunicorn_service,),
            context="Final active Gunicorn traffic route",
        )
        self._public_smoke_checks()
        marker = {
            "schema_version": 1,
            "platform_version": bundle.request.platform_version,
            "release_commit": bundle.request.release_commit,
            "manifest_sha256": bundle.request.manifest_sha256,
        }
        path = self._platform_current_path()
        self.platform_current_write_started = True
        self._update_recovery_plan("current-marker-write-started")
        _atomic_text(path, canonical_json_bytes(marker).decode("ascii"), 0o644)

    def cleanup_success(self, bundle: ValidatedBundle) -> str:
        del bundle
        self._remove_web_slots(
            (*self.candidate_web_slots, *self.previous_web_slots)
        )
        self._discard_previous_image_pins()
        return "Post-success candidate and previous web slots were removed."

    def _restore_service_set(self, services: Sequence[str], *, context: str) -> None:
        if not services:
            return
        beat = self.config.beat_service
        ordinary = tuple(service for service in services if service != beat)
        if ordinary:
            self._compose(
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                *self._scale_arguments_for(ordinary),
                *ordinary,
                context=context,
            )
        if beat in services:
            self._compose("stop", beat, context="Rollback Celery beat singleton stop")
            self._compose(
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                "--scale",
                f"{beat}=1",
                beat,
                context="Rollback Celery beat singleton restoration",
            )

    def _verify_restored(self, replaced_services: set[str]) -> None:
        expected = {
            **self.auth_replica_counts,
            self.config.database_service: 1,
            self.config.redis_service: 1,
            self.config.proxy_service: 1,
        }
        self._wait_for_compose_services(
            expected,
            zero_restarts=replaced_services,
            context="Restored production services",
        )
        self._require_live_images(
            {
                service: image
                for service, (image, _reference) in self.previous_images.items()
            }
        )
        self._manage_live(
            "check", "--no-color", context="Restored Django checks"
        )
        migrations = self._manage_live(
            "showmigrations",
            "--plan",
            "--no-color",
            context="Restored migration state",
        )
        if re.search(r"(?m)^\s*\[ \]", migrations):
            raise DeploymentError("Restored deployment has pending migrations")
        self._redis_health()
        self._celery_health()
        for container in self._container_names_for_service(
            self.config.gunicorn_service
        ):
            self._internal_http_checks(container)
        self._verify_static_asset(None)
        for command in self.config.application_health_commands:
            self._manage_live(
                command,
                "--no-color",
                context=f"Restored read-only application health command {command}",
            )
        self._public_smoke_checks()
        self._scan_new_logs(
            self.config.auth_services,
            owner_transition_phase=(
                "rollback" if self.recovery_baseline_verified else None
            ),
        )

    def rollback(self, bundle: ValidatedBundle, last_state: str | None) -> str:
        del last_state
        prepared = self.original_dockerfile is not None
        if not prepared:
            return "Candidate preparation never changed production."

        gunicorn = self.config.gunicorn_service
        errors: list[str] = []

        def attempt(label: str, action: Any) -> bool:
            try:
                result = action()
                if result is False:
                    raise DeploymentError(f"{label} reported an incomplete restoration")
                return True
            except Exception as exc:
                detail = redact_sensitive_text(str(exc)).replace("\r", " ").replace(
                    "\n", " "
                )
                errors.append(f"{label}: {detail[:500]}")
                return False

        # Route users to a known previous-image slot before retagging or
        # replacing any live application container.
        rollback_targets: tuple[str, ...] = ()
        if self.traffic_switch_started:
            targets = self.previous_web_slots
            if not targets and not self.gunicorn_replacement_started:
                targets = self._container_names_for_service(gunicorn)
            if not targets:
                raise DeploymentError(
                    "Traffic-first rollback has no previous Gunicorn slot"
                )
            self._activate_upstream(
                targets,
                backup_targets=(gunicorn,),
                context="Traffic-first rollback switch",
            )
            rollback_targets = tuple(targets)
            attempt("traffic-first rollback smoke", self._public_smoke_checks)

        configuration_restored = attempt(
            "candidate configuration restoration",
            self._restore_candidate_configuration,
        )

        workers_restored = True
        if self.workers_replacement_started:
            worker_services = tuple(
                service
                for service in self.config.auth_services
                if service != gunicorn
            )
            workers_restored = attempt(
                "Celery worker restoration",
                lambda: self._restore_service_set(
                    worker_services, context="Rollback Celery worker restoration"
                ),
            )
        gunicorn_restored = True
        if self.gunicorn_replacement_started:
            gunicorn_restored = attempt(
                "Gunicorn restoration",
                lambda: self._restore_service_set(
                    (gunicorn,), context="Rollback Gunicorn restoration"
                ),
            )

        active_route_restored = not self.traffic_switch_started
        if self.traffic_switch_started and configuration_restored and gunicorn_restored:
            active_route_restored = attempt(
                "restored Gunicorn traffic switch",
                lambda: self._activate_upstream(
                    (gunicorn,),
                    backup_targets=self.previous_web_slots,
                    context="Restored Gunicorn traffic switch",
                ),
            )

        replaced_services: set[str] = set()
        if self.workers_replacement_started:
            replaced_services.update(
                service
                for service in self.config.auth_services
                if service != gunicorn
            )
        if self.gunicorn_replacement_started:
            replaced_services.add(gunicorn)
        requires_verification = bool(
            self.migration_started or replaced_services or self.traffic_switch_started
        )
        if (
            requires_verification
            and configuration_restored
            and workers_restored
            and gunicorn_restored
            and active_route_restored
        ):
            attempt(
                "restored production verification",
                lambda: self._verify_restored(replaced_services),
            )

        if errors:
            raise DeploymentError(
                "Rollback retained previous safety slots after failures: "
                + "; ".join(errors)
            )

        if requires_verification:
            if self.traffic_switch_started:
                try:
                    self._activate_upstream(
                        (gunicorn,), context="Final restored Gunicorn traffic route"
                    )
                    self._public_smoke_checks()
                except Exception as exc:
                    detail = redact_sensitive_text(str(exc)).replace("\r", " ").replace(
                        "\n", " "
                    )
                    final_errors = [
                        f"final restored Gunicorn route verification: {detail[:500]}"
                    ]
                    if rollback_targets:
                        try:
                            self._activate_upstream(
                                rollback_targets,
                                backup_targets=(gunicorn,),
                                context="Failed final rollback route fail-safe",
                            )
                        except Exception as route_exc:
                            route_detail = redact_sensitive_text(str(route_exc)).replace(
                                "\r", " "
                            ).replace("\n", " ")
                            final_errors.append(
                                "previous-slot fail-safe route: "
                                + route_detail[:500]
                            )
                    raise DeploymentError(
                        "Rollback retained previous safety slots after failures: "
                        + "; ".join(final_errors)
                    ) from exc
            self._remove_web_slots(
                (*self.candidate_web_slots, *self.previous_web_slots)
            )
            self.complete_recovery_plan()
            traffic = (
                "Traffic was switched back first"
                if self.traffic_switch_started
                else "Live traffic remained on the previous web slot"
            )
            return (
                f"{traffic} and exact previous application "
                "images and replica topology were restored and verified. Database "
                "migrations were not reversed; the verified backup was retained."
            )
        self._remove_web_slots(
            (*self.candidate_web_slots, *self.previous_web_slots)
        )
        self.complete_recovery_plan()
        return (
            "Candidate slots and configuration were removed without replacing live "
            "containers. Database migrations were not run."
        )
