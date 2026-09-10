"""Execution rehearsal for the bounded immutable-v0.6.2 validation recovery."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import yaml

from ops.deploy import contracts, receiver_upgrade, request_archive
from ops.deploy.docker_host import DockerHost
from ops.deploy.engine import (
    DeploymentEngine,
    DeploymentJournal,
    PreflightEngine,
    PreflightJournal,
)
from ops.release import buh_release, recovery_policy
from tests.deploy.test_docker_host import (
    RETAINED_DISCORD_OWNER_LOG,
    container_id,
    discord_nickname_record,
    make_config,
    make_recovery_bundle,
    owner_log_environment,
    write_host_files,
)
ROOT = Path(__file__).resolve().parents[2]
TEST_SUPPORT_ROOT = ROOT / ".buh-recovery-test-support"


def load_approval_fixture():
    """Load activation-side approval support without replacing release code."""

    fixture_path = TEST_SUPPORT_ROOT / "tests/release/test_platform_approval.py"
    if not fixture_path.is_file():
        from tests.release import test_platform_approval

        return test_platform_approval

    support_modules = (
        "buh_release",
        "ledger",
        "open_sync_pr",
        "platform_approval",
        "recovery_policy",
        "validation_recovery",
    )
    saved = {name: sys.modules.pop(name, None) for name in support_modules}
    support_release = str(TEST_SUPPORT_ROOT / "ops/release")
    sys.path.insert(0, support_release)
    try:
        spec = importlib.util.spec_from_file_location(
            "_buh_recovery_approval_fixture", fixture_path
        )
        if spec is None or spec.loader is None:
            raise AssertionError("recovery approval fixture loader is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(support_release)
        for name in support_modules:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


approval_fixture = load_approval_fixture()


V061_RELEASE = "43234a8c0b6371fdfc62b59fa75a7980924ac3a5"
SYNTHETIC_RECOVERY_FRAGMENT = """\
schema_version = 1
app = "platform"
kind = "fix"
summary = "Exercise the bounded coordinated-recovery release rehearsal."
deployment_predecessor = "0.5.6"
"""


def run(*arguments: object, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def synthetic_recovery_plan(
    checkout: Path,
    fixture_root: Path,
    source_commit: str,
) -> dict[str, Any]:
    """Plan the bounded recovery from fixture-owned intent."""

    synthetic_changes = fixture_root / "synthetic-release-intent"
    synthetic_changes.mkdir(parents=True)
    (synthetic_changes / "coordinated-recovery-rehearsal.toml").write_text(
        SYNTHETIC_RECOVERY_FRAGMENT,
        encoding="utf-8",
    )
    return buh_release.create_plan(
        repo_root=checkout,
        registry_path=checkout / "ops/release/apps.toml",
        compatibility_path=checkout / "platform/compatibility.toml",
        changes_dir=synthetic_changes,
        previous_manifest_path=(
            checkout / "releases/platform/v0.6.1/RELEASE.json"
        ),
        source_commit=source_commit,
        test_run="synthetic-coordinated-rehearsal",
    )


def release_fingerprint(release_dir: Path) -> tuple[tuple[str, str], ...]:
    return tuple(
        (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(release_dir.iterdir(), key=lambda item: item.name)
    )


def populate_receiver(root: Path, marker: str) -> None:
    for index, logical in enumerate(receiver_upgrade.TARGETS):
        path = root / logical.removeprefix("/")
        if logical in {"/usr/local/lib/buh-platform-v2", "/etc/buh-platform-v2"}:
            path.mkdir(parents=True)
            (path / "state.txt").write_text(
                f"{marker}:{index}\n", encoding="utf-8"
            )
            path.chmod(0o750)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{marker}:{index}\n", encoding="utf-8")
            path.chmod(0o755)


def populate_reviewed_receiver(
    root: Path, repo: Path, config: Path, commit: str
) -> None:
    """Materialize the exact output contract of the reviewed shell installer."""

    directory_modes = {
        "/usr/local/lib/buh-platform-v2": 0o755,
        "/usr/local/lib/buh-platform-v2/ops": 0o755,
        "/usr/local/lib/buh-platform-v2/ops/deploy": 0o755,
        "/usr/local/lib/buh-platform-v2/ops/release": 0o755,
        "/etc/buh-platform-v2": 0o700,
    }
    for logical, mode in directory_modes.items():
        path = root / logical.removeprefix("/")
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
    provenance = {
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "files": {
            relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
            for relative in receiver_upgrade.INSTALL_SOURCES
        },
        "schema_version": 1,
        "source_commit": commit,
    }
    files: dict[str, tuple[bytes, int]] = {
        "/etc/buh-platform-v2/receiver.json": (config.read_bytes(), 0o600),
        "/etc/buh-platform-v2/INSTALL.json": (
            contracts.canonical_json_bytes(provenance),
            0o600,
        ),
        "/usr/local/sbin/buh-platform-v2-receiver": (
            (repo / "ops/deploy/buh-platform-v2-receiver").read_bytes(),
            0o755,
        ),
        "/usr/local/sbin/buh-deploy-dispatch": (
            (repo / "ops/deploy/buh-deploy-dispatch").read_bytes(),
            0o755,
        ),
        "/usr/local/bin/buh-github-observe-entry": (
            (repo / "ops/buh-github-observe-entry").read_bytes(),
            0o755,
        ),
        "/usr/local/sbin/buh-github-observe-root": (
            (repo / "ops/buh-github-observe-root").read_bytes(),
            0o755,
        ),
        "/usr/local/libexec/buh-redact-diagnostics": (
            (repo / "ops/buh-redact-diagnostics.py").read_bytes(),
            0o755,
        ),
        "/etc/sudoers.d/buh-platform-v2": (
            (
                "buh-deployer ALL=(root) NOPASSWD: "
                f"{receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH}\n"
                "buh-deployer ALL=(root) NOPASSWD: /usr/local/sbin/"
                "buh-platform-v2-receiver preflight\n"
                "buh-deployer ALL=(root) NOPASSWD: /usr/local/sbin/"
                "buh-platform-v2-receiver deploy\n"
            ).encode("utf-8"),
            0o440,
        ),
    }
    library = {
        "ops/__init__.py": "ops/__init__.py",
        "ops/deploy/__init__.py": "ops/deploy/__init__.py",
        "ops/deploy/contracts.py": "ops/deploy/contracts.py",
        "ops/deploy/coordinated-recovery.json": "ops/deploy/coordinated-recovery.json",
        "ops/deploy/docker_host.py": "ops/deploy/docker_host.py",
        "ops/deploy/engine.py": "ops/deploy/engine.py",
        "ops/deploy/receiver.py": "ops/deploy/receiver.py",
        "ops/release/__init__.py": "ops/release/__init__.py",
        "ops/release/buh_release.py": "ops/release/buh_release.py",
        "ops/release/recovery_policy.py": "ops/release/recovery_policy.py",
    }
    for installed, relative in library.items():
        files[f"/usr/local/lib/buh-platform-v2/{installed}"] = (
            (repo / relative).read_bytes(),
            0o644,
        )
    for logical, (data, mode) in files.items():
        path = root / logical.removeprefix("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)


def receiver_fingerprint(root: Path) -> dict[str, tuple[tuple[str, bytes], ...]]:
    result: dict[str, tuple[tuple[str, bytes], ...]] = {}
    for logical in receiver_upgrade.TARGETS:
        path = root / logical.removeprefix("/")
        files = [path] if path.is_file() else sorted(
            (item for item in path.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix(),
        )
        result[logical] = tuple(
            (item.relative_to(path).as_posix() if item != path else ".", item.read_bytes())
            for item in files
        )
    return result


class SyntheticFleet:
    """Stateful external boundary used by the real deployment engines."""

    def __init__(self, bundle: contracts.ValidatedBundle, *, fail_at: str | None = None):
        policy = recovery_policy.load_policy()
        self.bundle = bundle
        self.fail_at = fail_at
        self.calls: list[str] = []
        self.original_images = {
            service: identity["image_id"]
            for service, identity in policy["host_baseline"]["auth_services"].items()
        }
        self.original_replicas = {
            service: identity["replicas"]
            for service, identity in policy["host_baseline"]["auth_services"].items()
        }
        self.images = dict(self.original_images)
        self.replicas = dict(self.original_replicas)
        self.candidate_images = {
            service: f"sha256:{index + 1:064x}"
            for index, service in enumerate(self.images)
        }
        self.marker = dict(policy["baseline"])
        self.static_mapping = "v0.5.6"
        self.traffic = "v0.5.6"
        self.beat_replicas = 1
        self.backup_retained = False
        self.backup_restore_verified = False
        self.migrations_applied = False
        self.rollback_order: list[str] = []
        self.verification: dict[str, Any] | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name:
            if name == "stabilize":
                self.verification = {
                    "filename": "HEALTH.json",
                    "sha256": "6" * 64,
                    "result": "failed",
                    "iterations": 2,
                    "allowed_log_findings": 1,
                    "warnings": [
                        "expected Discord guild-owner nickname 50013 during worker-cutover"
                    ],
                    "failure": "synthetic stabilization failure",
                }
            raise contracts.DeploymentError(f"synthetic {name} failure")

    def validate(self, bundle: contracts.ValidatedBundle) -> None:
        self._call("validate")
        transition = bundle.request.recovery_transition
        assert transition is not None
        assert transition["purpose"] == "production-recovery"
        assert transition["releases"][0] == recovery_policy.load_policy()["baseline"]

    def prepare_candidate(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("prepare_candidate")

    def backup(self, _bundle: contracts.ValidatedBundle) -> dict[str, str]:
        self._call("backup")
        self.backup_retained = True
        self.backup_restore_verified = True
        return {"filename": "database.sql.gz", "sha256": "4" * 64}

    def migrate(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("migrate")
        self.migrations_applied = True

    def candidate_health(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("candidate_health")

    def switch_traffic(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("switch_traffic")
        self.traffic = "candidate"
        self.static_mapping = "candidate-versioned"

    def replace_workers(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("replace_workers")
        self.images = dict(self.candidate_images)
        self.beat_replicas = 1

    def stabilize(self, _bundle: contracts.ValidatedBundle) -> dict[str, Any]:
        self._call("stabilize")
        self.verification = {
            "filename": "HEALTH.json",
            "sha256": "5" * 64,
            "result": "success",
            "iterations": 21,
            "allowed_log_findings": 0,
            "warnings": [],
            "failure": None,
        }
        return self.verification

    def stabilization_evidence(self) -> dict[str, Any] | None:
        return self.verification

    def promote_web(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("promote_web")
        self.traffic = self.bundle.request.platform_version

    def finalize(self, _bundle: contracts.ValidatedBundle) -> None:
        self._call("finalize")
        if not self.backup_restore_verified or self.beat_replicas != 1:
            raise contracts.DeploymentError("synthetic final invariant failed")
        self.marker = {
            "manifest_sha256": self.bundle.request.manifest_sha256,
            "platform_version": self.bundle.request.platform_version,
            "release_commit": self.bundle.request.release_commit,
            "source_commit": self.bundle.manifest["source_commit"],
        }

    def cleanup_success(self, _bundle: contracts.ValidatedBundle) -> str:
        self._call("cleanup_success")
        return "synthetic cleanup passed"

    def rollback(self, _bundle: contracts.ValidatedBundle, last_state: str | None) -> str:
        self.calls.append(f"rollback:{last_state}")
        self.rollback_order.append("traffic")
        self.traffic = "v0.5.6"
        self.rollback_order.append("images-and-replicas")
        self.images = dict(self.original_images)
        self.replicas = dict(self.original_replicas)
        self.beat_replicas = 1
        self.rollback_order.append("static")
        self.static_mapping = "v0.5.6"
        self.rollback_order.append("marker")
        self.marker = dict(recovery_policy.load_policy()["baseline"])
        return "synthetic exact topology and static mapping restored"


class SyntheticDockerBoundary:
    """Stateful Docker/Compose transport for a real ``DockerHost`` instance."""

    def __init__(
        self,
        host: DockerHost,
        bundle: contracts.ValidatedBundle,
        *,
        fail_during_stabilization: bool,
    ) -> None:
        policy = recovery_policy.load_policy()
        self.host = host
        self.bundle = bundle
        self.fail_during_stabilization = fail_during_stabilization
        self.original_images = {
            service: identity["image_id"]
            for service, identity in policy["host_baseline"]["auth_services"].items()
        }
        self.counts = {
            service: identity["replicas"]
            for service, identity in policy["host_baseline"]["auth_services"].items()
        }
        self.references = {
            service: f"fixture/{service}:live" for service in host.config.auth_services
        }
        self.candidate_images = {
            service: f"sha256:{index + 11:064x}"
            for index, service in enumerate(host.config.auth_services)
        }
        self.tags = {
            self.references[service]: image
            for service, image in self.original_images.items()
        }
        self.containers: dict[str, dict[str, Any]] = {}
        self.services: dict[str, list[str]] = {}
        self.next_container = 100
        for service in host.config.auth_services:
            self._replace_service(service, self.original_images[service])
        for service, image in (
            (host.config.database_service, "sha256:" + "d" * 64),
            (host.config.redis_service, "sha256:" + "e" * 64),
            (
                host.config.proxy_service,
                policy["host_baseline"]["nginx"]["image_id"],
            ),
        ):
            self.counts[service] = 1
            self.references[service] = f"fixture/{service}:live"
            self._replace_service(service, image)
        front_proxy = policy["host_baseline"]["front_proxy"]
        front_proxy_service = front_proxy["service"]
        self.counts[front_proxy_service] = 1
        self.references[front_proxy_service] = (
            f"fixture/{front_proxy_service}:live"
        )
        self._replace_service(front_proxy_service, front_proxy["image_id"])
        nginx_container = self.services[host.config.proxy_service][0]
        self.containers[nginx_container]["name"] = policy["host_baseline"][
            "nginx"
        ]["container"]
        proxy_container = self.services[front_proxy_service][0]
        self.containers[proxy_container]["name"] = front_proxy["container"]
        self.replaced_workers = False
        self.post_replace_log_round = 0
        self.static_mapping = "v0.5.6"
        self.reload_count = 0
        self.beat_stop_count = 0
        self.max_beat_replicas = 1

    def _new_container(self, service: str, image: str, index: int) -> str:
        identity = f"{self.next_container:064x}"
        self.next_container += 1
        name = f"fixture-{service}-{index + 1}"
        self.containers[identity] = {
            "image": image,
            "name": name,
            "reference": self.references[service],
            "restart": 0,
            "service": service,
        }
        return identity

    def _replace_service(self, service: str, image: str) -> None:
        for old in self.services.get(service, []):
            self.containers.pop(old, None)
        self.services[service] = [
            self._new_container(service, image, index)
            for index in range(self.counts[service])
        ]

    def compose(self, *arguments: str, context: str, **_kwargs) -> str:
        del context
        if arguments[:2] == ("ps", "-q"):
            return "\n".join(self.services[arguments[-1]]) + "\n"
        if arguments[:2] == ("config", "--services"):
            return "\n".join(self.services) + "\n"
        if arguments[:2] == ("config", "--no-interpolate"):
            return json.dumps(
                {
                    "services": {
                        service: {
                            "build": {
                                "context": str(self.host.config.app_dir),
                                "dockerfile": str(self.host.config.custom_dockerfile),
                            }
                        }
                        for service in self.host.config.auth_services
                    }
                }
            )
        if arguments[:2] == ("config", "--quiet"):
            return ""
        if arguments and arguments[0] == "build":
            for service in self.host.config.auth_services:
                self.tags[self.references[service]] = self.candidate_images[service]
            return ""
        if arguments[:2] == ("run", "--detach"):
            name = arguments[arguments.index("--name") + 1]
            service = arguments[-1]
            image = self.tags[self.references[service]]
            self.containers[name] = {
                "image": image,
                "name": name,
                "reference": self.references[service],
                "restart": 0,
                "service": service,
            }
            return name + "\n"
        if arguments and arguments[0] == "up":
            selected = [
                service for service in self.host.config.auth_services if service in arguments
            ]
            for service in selected:
                self._replace_service(service, self.tags[self.references[service]])
                if service == self.host.config.beat_service:
                    self.max_beat_replicas = max(
                        self.max_beat_replicas,
                        len(self.services[service]),
                    )
            if any(
                service != self.host.config.gunicorn_service for service in selected
            ):
                self.replaced_workers = True
            return ""
        if arguments and arguments[0] == "stop":
            service = arguments[-1]
            if service == self.host.config.beat_service:
                self.beat_stop_count += 1
                for container in self.services.get(service, []):
                    self.containers.pop(container, None)
                self.services[service] = []
            return ""
        if arguments and arguments[0] == "logs":
            service = arguments[-1]
            if self.replaced_workers and service == self.host.config.gunicorn_service:
                self.post_replace_log_round += 1
            if (
                self.fail_during_stabilization
                and self.replaced_workers
                and self.post_replace_log_round >= 2
                and service == self.host.config.worker_service
                and self.containers[self.services[service][0]]["image"]
                != self.original_images[service]
            ):
                return "ERROR synthetic stabilization boundary failure\n"
            return ""
        if arguments[:2] == ("exec", "-T"):
            return ""
        raise AssertionError(f"unexpected Compose boundary call: {arguments!r}")

    def run(self, arguments: list[str], *, context: str, **_kwargs) -> str:
        if arguments[:3] == ["docker", "image", "tag"]:
            source, target = arguments[-2:]
            self.tags[target] = self.tags.get(source, source)
            return ""
        if arguments[:3] == ["docker", "image", "rm"]:
            self.tags.pop(arguments[-1], None)
            return ""
        if arguments[:3] == ["docker", "image", "inspect"]:
            reference = arguments[-1]
            image = self.tags[reference]
            if any("{{json .Config.Labels}}" in item for item in arguments):
                labels = self.host._candidate_provenance_labels(self.bundle)
                return f"{image}|{json.dumps(labels)}\n"
            return image + "\n"
        if arguments[:2] == ["docker", "inspect"]:
            container = arguments[-1]
            state = self.containers[container]
            format_value = arguments[arguments.index("--format") + 1]
            if format_value == "{{.State.Status}}|{{.Image}}|{{.Config.Image}}|{{.RestartCount}}":
                return (
                    f"running|{state['image']}|{state['reference']}|"
                    f"{state['restart']}\n"
                )
            if format_value == "{{json .Config.Labels}}":
                baseline = recovery_policy.load_policy()["baseline"]
                return json.dumps(
                    {
                        "com.b-uh.platform.source": baseline["source_commit"],
                        "com.b-uh.platform.version": baseline["platform_version"],
                    }
                )
            if format_value == "{{.Name}}|{{.Image}}|{{.State.Status}}|{{.RestartCount}}":
                return f"/{state['name']}|{state['image']}|running|{state['restart']}\n"
            if format_value == "{{.State.Status}}|{{.RestartCount}}":
                return f"running|{state['restart']}\n"
            if "{{if .State.Health}}" in format_value:
                return f"running|{state['restart']}|healthy\n"
            if format_value == "{{.State.Status}}|{{.RestartCount}}|{{.Image}}":
                return f"running|{state['restart']}|{state['image']}\n"
            if format_value == "{{.State.Status}}|{{.Image}}|{{.RestartCount}}":
                return f"running|{state['image']}|{state['restart']}\n"
            if format_value == "{{.Image}}":
                return state["image"] + "\n"
            if format_value == "{{.Name}}":
                return "/" + state["name"] + "\n"
            raise AssertionError(f"unexpected inspect format: {format_value}")
        if arguments[:2] == ["docker", "logs"]:
            container = arguments[-1]
            state = self.containers[container]
            service = state["service"]
            if (
                service == self.host.config.worker_service
                and state["image"] == self.original_images[service]
                and container == self.services[service][0]
            ):
                return RETAINED_DISCORD_OWNER_LOG.read_text(encoding="utf-8")
            if (
                self.fail_during_stabilization
                and self.replaced_workers
                and self.post_replace_log_round >= 2
                and service == self.host.config.worker_service
                and state["image"] != self.original_images[service]
            ):
                return (
                    "[2026-09-07 22:20:00,000: ERROR/ForkPoolWorker-2] "
                    "synthetic stabilization boundary failure\n"
                )
            return ""
        if arguments[:3] == ["docker", "run", "--detach"]:
            return "synthetic-restore\n"
        if arguments[:3] == ["docker", "rm", "--force"]:
            self.containers.pop(arguments[-1], None)
            return ""
        if arguments[:2] == ["docker", "cp"]:
            return ""
        if arguments[:3] == ["docker", "ps", "-aq"]:
            name = arguments[-1].removeprefix("name=^/").removesuffix("$")
            return name + "\n" if name in self.containers else ""
        if arguments[:2] == ["docker", "exec"]:
            if context == "Previous static manifest atomic restoration":
                self.static_mapping = "v0.5.6"
            return ""
        raise AssertionError(f"unexpected Docker boundary call: {arguments!r}")

    def proxy(self, *arguments: str, context: str, **_kwargs) -> str:
        del context
        if arguments[0] == "sha256sum":
            upstream = self.host._upstream_path().read_text(encoding="utf-8")
            return f"{hashlib.sha256(upstream.encode()).hexdigest()}  mounted\n"
        if arguments == ("nginx", "-T"):
            return """
            events {}
            http {
                upstream buh_platform_v2_active { server allianceauth_gunicorn:8000; }
                server {
                    listen 443 ssl;
                    server_name auth.b-uh.com;
                    location / { proxy_pass http://buh_platform_v2_active; }
                }
            }
            """
        if arguments == ("nginx", "-s", "reload"):
            self.reload_count += 1
        return ""

    def capture_static(self) -> None:
        host = self.host
        assert host.backup_path is not None
        manifest = host.backup_path / "staticfiles.previous.json"
        manifest.write_bytes(
            contracts.canonical_json_bytes(
                {
                    "hash": "a" * 12,
                    "paths": {
                        "admin/css/base.css": "admin/css/base.synthetic.css"
                    },
                    "version": "1.1",
                }
            )
        )
        manifest.chmod(0o444)
        assets = host.backup_path / "static-assets.previous.tar"
        assets.write_bytes(b"bounded synthetic static archive")
        assets.chmod(0o400)
        host.static_root_path = "/var/www/myauth/static"
        host.static_manifest_path = "/var/www/myauth/static/staticfiles.json"
        host.static_manifest_backup = manifest
        host.static_manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
        host.static_manifest_entries = 1
        host.static_manifest_metadata = (1000, 1000, 0o644)
        host.static_assets_backup = assets
        host.static_assets_backup_sha256 = hashlib.sha256(assets.read_bytes()).hexdigest()
        host.static_assets_count = 1
        host.static_assets_bytes = 32
        host.static_assets_sha256 = "f" * 64

    @contextmanager
    def database_lock(self, _container: str):
        yield

    def dump_to_file(self, _arguments, path: Path, **_kwargs) -> None:
        path.write_bytes(b"-- synthetic MariaDB dump --\n" + b"x" * 256)

    def restore_static(self) -> None:
        self.static_mapping = "v0.5.6"

    def manage_image(self, *arguments: str, context: str) -> str:
        if context == "Recovery Discord owner association verification":
            owner = recovery_policy.load_policy()["host_baseline"]["discord_owner"]
            return json.dumps(
                {
                    "configured": owner["discord_user_id"],
                    "guild": owner["guild_id"],
                    "uid": owner["discord_user_id"],
                    "username": owner["auth_username"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        if arguments and arguments[0] == "collectstatic":
            self.static_mapping = self.bundle.request.platform_version
        return ""

    @staticmethod
    def manage_runtime(*arguments: str, **_kwargs) -> str:
        if arguments and arguments[0] == "showmigrations":
            return "[X] synthetic migration\n"
        if arguments and arguments[0] == "shell":
            return "mysql\n"
        return ""


def prepare_real_recovery_host(
    root: Path,
    bundle: contracts.ValidatedBundle,
    *,
    fail_during_stabilization: bool,
) -> tuple[DockerHost, SyntheticDockerBoundary]:
    """Create the confirmed v0.5.6 filesystem/topology at a synthetic boundary."""

    policy = recovery_policy.load_policy()
    root.mkdir(parents=True)
    config = dataclasses.replace(
        make_config(root),
        health_attempts=2,
        health_interval_seconds=0,
        stabilization_seconds=1,
        stabilization_interval_seconds=1,
    )
    write_host_files(config)
    compose_files = policy["host_baseline"]["compose_files_after_activation"]
    for relative in compose_files[1:]:
        (config.app_dir / relative).write_text("services: {}\n", encoding="utf-8")
    runtime_image = bundle.manifest["compatibility"]["values"][
        "production_runtime"
    ]["base_image"]
    (config.app_dir / config.env_file).write_text(
        f"AA_DOCKER_TAG={runtime_image}\n"
        f"COMPOSE_FILE={':'.join(compose_files)}\n",
        encoding="utf-8",
    )
    settings = config.app_dir / config.local_settings
    settings.write_text(
        "INSTALLED_APPS += "
        + json.dumps(bundle.install_plan["django_apps"], sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    settings.chmod(0o640)
    config.state_dir.mkdir(mode=0o700, parents=True)
    config.backup_dir.mkdir(mode=0o700, parents=True)
    baseline = policy["baseline"]
    current = {
        "schema_version": 1,
        "platform_version": baseline["platform_version"],
        "release_commit": baseline["release_commit"],
        "source_commit": baseline["source_commit"],
        "manifest_sha256": baseline["manifest_sha256"],
        "verified_at": "2026-09-07T22:17:17+00:00",
    }
    (config.state_dir / "current.json").write_bytes(
        contracts.canonical_json_bytes(current)
    )
    platform_current = config.app_dir / "conf/buh-platform-v2/CURRENT.json"
    platform_current.parent.mkdir(parents=True, exist_ok=True)
    platform_current.write_bytes(
        contracts.canonical_json_bytes(
            {
                "schema_version": 1,
                "platform_version": baseline["platform_version"],
                "release_commit": baseline["release_commit"],
                "manifest_sha256": baseline["manifest_sha256"],
            }
        )
    )
    host = DockerHost(config)
    return host, SyntheticDockerBoundary(
        host,
        bundle,
        fail_during_stabilization=fail_during_stabilization,
    )


@contextmanager
def exercised_docker_boundary(
    host: DockerHost, boundary: SyntheticDockerBoundary
):
    """Patch only host/OS transport while keeping DockerHost logic exercised."""

    local_settings = host.config.app_dir / host.config.local_settings
    real_stat = Path.stat
    monotonic_values = iter((0.0, 0.0, 1.0))

    def synthetic_stat(path: Path, *args, **kwargs):
        details = real_stat(path, *args, **kwargs)
        if path != local_settings:
            return details
        return SimpleNamespace(
            st_mode=(details.st_mode & ~0o777) | 0o640,
            st_uid=0,
            st_gid=61000,
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_size=details.st_size,
            st_atime=details.st_atime,
            st_mtime=details.st_mtime,
            st_ctime=details.st_ctime,
        )

    def monotonic() -> float:
        return next(monotonic_values, 1.0)

    def manage_container(_container: str, *arguments: str, **kwargs) -> str:
        return boundary.manage_runtime(*arguments, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", synthetic_stat))
        stack.enter_context(
            mock.patch(
                "ops.deploy.docker_host.os.geteuid", return_value=0, create=True
            )
        )
        stack.enter_context(
            mock.patch(
                "ops.deploy.docker_host.shutil.disk_usage",
                return_value=SimpleNamespace(free=2 * 1024 * 1024 * 1024),
            )
        )
        stack.enter_context(
            mock.patch("ops.deploy.docker_host.time.monotonic", side_effect=monotonic)
        )
        stack.enter_context(mock.patch("ops.deploy.docker_host.time.sleep"))
        stack.enter_context(
            mock.patch.object(host, "_secure_private_directory", return_value=None)
        )
        stack.enter_context(mock.patch.object(host, "_compose", side_effect=boundary.compose))
        stack.enter_context(mock.patch.object(host, "_run", side_effect=boundary.run))
        stack.enter_context(
            mock.patch.object(host, "_proxy_exec", side_effect=boundary.proxy)
        )
        stack.enter_context(
            mock.patch.object(
                host, "_capture_static_manifest", side_effect=boundary.capture_static
            )
        )
        stack.enter_context(
            mock.patch.object(
                host, "_verify_previous_static_manifest", return_value=None
            )
        )
        stack.enter_context(
            mock.patch.object(
                host,
                "_container_environment",
                return_value={"MARIADB_DATABASE": "allianceauth"},
            )
        )
        stack.enter_context(
            mock.patch.object(
                host, "_application_database", return_value="allianceauth"
            )
        )
        stack.enter_context(
            mock.patch.object(
                host, "_database_read_lock", side_effect=boundary.database_lock
            )
        )
        stack.enter_context(
            mock.patch.object(host, "_run_to_file", side_effect=boundary.dump_to_file)
        )
        stack.enter_context(mock.patch.object(host, "_run_from_file", return_value=None))
        stack.enter_context(
            mock.patch.object(
                host,
                "_evidence_counts",
                return_value={"django_migrations": 10, "buh_moon_tax_event": 3},
            )
        )
        stack.enter_context(
            mock.patch.object(host, "_manage_image", side_effect=boundary.manage_image)
        )
        stack.enter_context(
            mock.patch.object(host, "_manage_live", side_effect=boundary.manage_runtime)
        )
        stack.enter_context(
            mock.patch.object(host, "_manage_container", side_effect=manage_container)
        )
        for method in (
            "_version_probe",
            "_container_version_probe",
            "_verify_static_asset",
            "_internal_http_checks",
            "_redis_health",
            "_celery_health",
            "_public_smoke_checks",
        ):
            stack.enter_context(mock.patch.object(host, method, return_value=None))
        yield


class CoordinatedRecoveryRehearsal(unittest.TestCase):
    maxDiff = 2000

    def test_schema_v2_nginx_boundary_validates_switches_and_restores(self) -> None:
        """Exercise the real managed-route parser and atomic file switch."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            nginx_dump = """
            events {}
            http {
                upstream buh_platform_v2_active {
                    server allianceauth_gunicorn:8000;
                }
                server {
                    listen 443 ssl;
                    server_name auth.b-uh.com;
                    location / { proxy_pass http://buh_platform_v2_active; }
                }
            }
            """
            calls: list[str] = []

            def proxy(*arguments, context, **_kwargs):
                calls.append(context)
                if arguments[0] == "sha256sum":
                    upstream = config.app_dir / config.nginx_upstream_file
                    visible = upstream.read_text(encoding="utf-8").encode("utf-8")
                    return f"{hashlib.sha256(visible).hexdigest()}  mounted\n"
                if arguments == ("nginx", "-T"):
                    return nginx_dump
                return ""

            with mock.patch.object(host, "_proxy_exec", side_effect=proxy):
                host._verify_proxy_contract()
                host._activate_upstream(
                    ("buh-web-candidate-gh-6000001-1-1",),
                    backup_targets=(config.gunicorn_service,),
                    context="Synthetic candidate switch",
                )
            switched = (
                config.app_dir / config.nginx_upstream_file
            ).read_text(encoding="utf-8")
            self.assertIn("buh-web-candidate-gh-6000001-1-1:8000", switched)
            self.assertIn("allianceauth_gunicorn:8000", switched)
            self.assertIn("Synthetic candidate switch atomic reload", calls)

            reloads = 0

            def failing_proxy(*arguments, context, **_kwargs):
                nonlocal reloads
                if arguments[0] == "sha256sum":
                    upstream = config.app_dir / config.nginx_upstream_file
                    visible = upstream.read_text(encoding="utf-8").encode("utf-8")
                    return f"{hashlib.sha256(visible).hexdigest()}  mounted\n"
                if arguments == ("nginx", "-s", "reload"):
                    reloads += 1
                    if reloads == 1:
                        raise contracts.DeploymentError("synthetic reload rejection")
                return ""

            with mock.patch.object(
                host, "_proxy_exec", side_effect=failing_proxy
            ), self.assertRaisesRegex(
                contracts.DeploymentError, "synthetic reload rejection"
            ):
                host._activate_upstream(
                    ("buh-web-candidate-gh-6000001-1-2",),
                    context="Injected candidate switch",
                )
            self.assertEqual(
                (config.app_dir / config.nginx_upstream_file).read_text(
                    encoding="utf-8"
                ),
                switched,
            )
            self.assertEqual(reloads, 2)

            schema_v1 = dataclasses.replace(config, schema_version=1)
            schema_v1_host = DockerHost(schema_v1)
            with mock.patch(
                "ops.deploy.docker_host.os.geteuid", return_value=0, create=True
            ), mock.patch.object(schema_v1_host, "_compose") as compose, self.assertRaisesRegex(
                contracts.DeploymentError,
                "requires receiver configuration schema v2",
            ):
                schema_v1_host.validate(make_recovery_bundle(root))
            compose.assert_not_called()

    def test_release_fixture_accepts_source_and_published_shapes(self) -> None:
        """Run the planner/assembler with both caller release states."""

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            checkout = base / "caller-snapshot"
            cloned = run(
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                ROOT,
                checkout,
                cwd=ROOT,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr[-1000:])
            head = run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip()
            self.assertEqual(
                run(
                    "git", "config", "core.autocrlf", "false", cwd=checkout
                ).returncode,
                0,
            )
            checked_out = run(
                "git", "checkout", "--quiet", "--detach", head, cwd=checkout
            )
            self.assertEqual(checked_out.returncode, 0, checked_out.stderr[-1000:])

            source_plan = synthetic_recovery_plan(
                checkout,
                base / "source-shape",
                head,
            )
            source_output = checkout / ".recovery-source-shape/platform/v0.6.2"
            self.assertFalse(source_output.exists())
            buh_release.assemble_release(
                plan=source_plan,
                wheel_dir=base / "unused-source-wheels",
                previous_release_dir=checkout / "releases/platform/v0.6.1",
                output_dir=source_output,
                repo_root=checkout,
            )
            self.assertEqual(
                buh_release.verify_release_dir(source_output)["platform_version"],
                "0.6.2",
            )

            # Reproduce the release snapshot's two material properties inside
            # this disposable checkout: source fragments are consumed and the
            # immutable target directory already exists. Use the exact payload
            # just assembled rather than a marker or mocked directory.
            published_release = checkout / "releases/platform/v0.6.2"
            if not published_release.exists():
                buh_release.assemble_release(
                    plan=source_plan,
                    wheel_dir=base / "unused-published-wheels",
                    previous_release_dir=checkout / "releases/platform/v0.6.1",
                    output_dir=published_release,
                    repo_root=checkout,
                )
            self.assertEqual(
                buh_release.verify_release_dir(published_release)[
                    "platform_version"
                ],
                "0.6.2",
            )
            for fragment in (checkout / "changes").glob("*.toml"):
                fragment.unlink()
            self.assertEqual(list((checkout / "changes").glob("*.toml")), [])
            published_fingerprint = release_fingerprint(published_release)

            published_plan = synthetic_recovery_plan(
                checkout,
                base / "published-shape",
                head,
            )
            published_output = (
                checkout / ".recovery-published-shape/platform/v0.6.2"
            )
            buh_release.assemble_release(
                plan=published_plan,
                wheel_dir=base / "unused-post-published-wheels",
                previous_release_dir=checkout / "releases/platform/v0.6.1",
                output_dir=published_output,
                repo_root=checkout,
            )
            self.assertEqual(
                buh_release.verify_release_dir(published_output)[
                    "platform_version"
                ],
                "0.6.2",
            )
            self.assertEqual(
                release_fingerprint(published_release),
                published_fingerprint,
            )

    def test_complete_recovery_and_next_release_sequence(self) -> None:
        policy = recovery_policy.load_policy()
        self.assertEqual(
            policy["server_evidence"]["sha256"],
            "df9865e121e068cc65c3e6b05cb287f1b87f05772e54ecd4c7d07fa61137fc38",
        )
        self.assertEqual(
            [item["platform_version"] for item in policy["published_releases"]],
            ["0.5.6", "0.6.0", "0.6.1"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            checkout = base / "exact-checkout"
            cloned = run(
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                ROOT,
                checkout,
                cwd=ROOT,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr[-1000:])
            head = run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip()
            self.assertEqual(
                run("git", "config", "core.autocrlf", "false", cwd=checkout).returncode,
                0,
            )
            self.assertEqual(
                run("git", "config", "user.name", "Recovery Rehearsal", cwd=checkout).returncode,
                0,
            )
            self.assertEqual(
                run(
                    "git",
                    "config",
                    "user.email",
                    "recovery-rehearsal@example.invalid",
                    cwd=checkout,
                ).returncode,
                0,
            )
            checked_out = run(
                "git", "checkout", "--quiet", "--detach", head, cwd=checkout
            )
            self.assertEqual(checked_out.returncode, 0, checked_out.stderr[-1000:])

            manifest_061 = buh_release.verify_release_dir(
                checkout / "releases/platform/v0.6.1"
            )
            history_request = contracts.DeploymentRequest(
                mode="deploy",
                repository=policy["repository"],
                release_commit=V061_RELEASE,
                release_ref="release/platform-v0.6.1",
                platform_version="0.6.1",
                manifest_sha256=policy["published_releases"][-1]["manifest_sha256"],
                workflow_run_id="5000001",
                workflow_run_attempt=1,
            )
            history_bundle = contracts.ValidatedBundle(
                root=checkout,
                release_dir=checkout / "releases/platform/v0.6.1",
                request=history_request,
                manifest=manifest_061,
                install_plan={},
            )
            history_host = DockerHost(
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                )
            )
            live = {
                key: policy["baseline"][key]
                for key in (
                    "manifest_sha256",
                    "platform_version",
                    "release_commit",
                    "source_commit",
                )
            }
            with mock.patch.object(history_host, "_load_current", return_value=live), self.assertRaisesRegex(
                contracts.DeploymentError, "predecessor does not match"
            ):
                history_host._validate_release_transition(history_bundle)
            self.assertEqual(live["platform_version"], "0.5.6")

            # Keep the synthetic release intent independent of whether the
            # caller is the feature source (fragment present, v0.6.2 absent) or
            # the already-published release snapshot (fragment consumed,
            # immutable v0.6.2 present). The real planner still validates every
            # source input and the reviewed v0.6.1 recovery predecessor.
            plan = synthetic_recovery_plan(
                checkout,
                base / "complete-sequence",
                head,
            )
            self.assertEqual(plan["platform_version"], "0.6.2")
            self.assertEqual(plan["previous_platform_version"], "0.6.1")
            self.assertEqual(
                plan["deployment_predecessor"]["baseline"], policy["baseline"]
            )
            self.assertEqual(
                plan["deployment_predecessor"]["intervening_releases"],
                policy["published_releases"][1:],
            )

            # Assemble the same release payload under a fixture-only path. The
            # commit still has the reviewed source as its sole parent and
            # reuses the exact prior wheels, while never colliding with or
            # editing a caller's already-published v0.6.2 directory.
            next_release = (
                checkout / ".recovery-rehearsal-output/platform/v0.6.2"
            )
            self.assertNotEqual(
                next_release,
                checkout / "releases/platform/v0.6.2",
            )
            buh_release.assemble_release(
                plan=plan,
                wheel_dir=base / "unused-wheels",
                previous_release_dir=checkout / "releases/platform/v0.6.1",
                output_dir=next_release,
                repo_root=checkout,
            )
            self.assertEqual(
                run(
                    "git",
                    "add",
                    "--force",
                    "--",
                    ".recovery-rehearsal-output/platform/v0.6.2",
                    cwd=checkout,
                ).returncode,
                0,
            )
            committed = run(
                "git",
                "commit",
                "--quiet",
                "-m",
                "synthetic recovery release",
                cwd=checkout,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr[-1000:])
            release_commit = run("git", "rev-parse", "HEAD", cwd=checkout).stdout.strip()
            self.assertEqual(
                run("git", "show", "-s", "--format=%P", release_commit, cwd=checkout).stdout.strip(),
                head,
            )
            preserved_caller_state = run(
                "git",
                "diff",
                "--exit-code",
                head,
                release_commit,
                "--",
                "changes",
                "releases/platform/v0.6.2",
                cwd=checkout,
            )
            self.assertEqual(
                preserved_caller_state.returncode,
                0,
                preserved_caller_state.stderr[-1000:],
            )
            production_archive = base / "production-recovery.tar.gz"
            production_metadata = request_archive.build_archive(
                root=checkout,
                release_dir=next_release,
                repository=policy["repository"],
                release_commit=release_commit,
                mode="deploy",
                workflow_run_id="6000001",
                workflow_run_attempt=1,
                output=production_archive,
            )
            self.assertEqual(
                [item["platform_version"] for item in production_metadata["release_refs"]],
                ["0.5.6", "0.6.0", "0.6.1", "0.6.2"],
            )
            production_root = base / "production-extracted"
            contracts.extract_archive(production_archive.read_bytes(), production_root)
            deployment_bundle = contracts.load_validated_bundle(
                production_root,
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                ),
            )
            self.assertEqual(
                deployment_bundle.request.recovery_transition["purpose"],
                "production-recovery",
            )

            archives = [base / "bootstrap-1.tar.gz", base / "bootstrap-2.tar.gz"]
            archive_metadata = []
            for output in archives:
                archive_metadata.append(
                    request_archive.build_archive(
                        root=checkout,
                        release_dir=checkout / "releases/platform/v0.6.1",
                        repository=policy["repository"],
                        release_commit=V061_RELEASE,
                        mode="preflight",
                        workflow_run_id="4323400000000000000",
                        workflow_run_attempt=1,
                        output=output,
                        bootstrap_recovery=True,
                    )
                )
            self.assertEqual(archives[0].read_bytes(), archives[1].read_bytes())
            self.assertEqual(archive_metadata[0], archive_metadata[1])
            extracted = base / "bootstrap-extracted"
            contracts.extract_archive(archives[0].read_bytes(), extracted)
            bootstrap_bundle = contracts.load_validated_bundle(
                extracted,
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                ),
            )
            self.assertEqual(
                bootstrap_bundle.request.recovery_transition["purpose"],
                "receiver-upgrade-preflight",
            )
            self.assertEqual(bootstrap_bundle.request.mode, "preflight")

            system_root = base / "synthetic-host"
            populate_receiver(system_root, "old-receiver")
            original_receiver = receiver_fingerprint(system_root)
            config_input = base / "receiver-v1.json"
            legacy_input = base / "legacy-receiver"
            request_input = base / "request.tar.gz"
            config_data = json.loads(
                (checkout / "ops/deploy/receiver-config.example.json").read_text(
                    encoding="utf-8"
                )
            )
            v1_schema = json.loads(
                (checkout / "ops/deploy/receiver-config-v1.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            config_data = {
                key: value
                for key, value in config_data.items()
                if key in v1_schema["properties"]
            }
            config_data["schema_version"] = 1
            config_input.write_bytes(contracts.canonical_json_bytes(config_data))
            legacy_input.write_text("legacy\n", encoding="ascii")
            request_input.write_bytes(archives[0].read_bytes())
            backup_root = base / "receiver-backups"

            def reviewed_install(staging: Path) -> None:
                if os.name != "posix":
                    # Windows cannot unlink a chmod-0440 rollback fixture.  The
                    # Linux hosted lane exercises the exact installer modes.
                    populate_receiver(staging, "windows-contract-fallback")
                    return
                populate_reviewed_receiver(staging, checkout, config_input, head)
                receiver_upgrade._verify_real_staged_contract(
                    staging,
                    checkout,
                    config_input,
                    head,
                    receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH,
                    require_root_owner=False,
                )

            def verified_bootstrap_preflight() -> None:
                with tempfile.TemporaryDirectory() as extracted_temporary:
                    extracted_root = Path(extracted_temporary) / "payload"
                    contracts.extract_archive(
                        request_input.read_bytes(), extracted_root
                    )
                    checked = contracts.load_validated_bundle(
                        extracted_root,
                        contracts.ReceiverConfig.load(config_input),
                    )
                    self.assertEqual(checked.request.mode, "preflight")
                    self.assertEqual(
                        checked.request.recovery_transition["purpose"],
                        "receiver-upgrade-preflight",
                    )
                    transition_host = DockerHost(
                        contracts.ReceiverConfig.load(config_input)
                    )
                    with mock.patch.object(
                        transition_host,
                        "_load_current",
                        return_value={
                            key: policy["baseline"][key]
                            for key in (
                                "manifest_sha256",
                                "platform_version",
                                "release_commit",
                                "source_commit",
                            )
                        },
                    ):
                        transition_host._validate_release_transition(checked)

            def failed_preflight() -> None:
                raise receiver_upgrade.UpgradeError("synthetic bootstrap rejection")

            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "Verified backup restored"
            ):
                receiver_upgrade.execute_upgrade(
                    commit=head,
                    repo_root=checkout,
                    config=config_input,
                    legacy_receiver=legacy_input,
                    request=request_input,
                    system_root=system_root,
                    backup_root=backup_root,
                    install_callback=reviewed_install,
                    preflight_callback=failed_preflight,
                )
            self.assertEqual(receiver_fingerprint(system_root), original_receiver)

            receiver_upgrade.execute_upgrade(
                commit=head,
                repo_root=checkout,
                config=config_input,
                legacy_receiver=legacy_input,
                request=request_input,
                system_root=system_root,
                backup_root=backup_root,
                install_callback=reviewed_install,
                preflight_callback=verified_bootstrap_preflight,
            )
            installed_receiver = (
                system_root / "usr/local/sbin/buh-platform-v2-receiver"
            ).read_bytes()
            if os.name == "posix":
                self.assertEqual(
                    installed_receiver,
                    (checkout / "ops/deploy/buh-platform-v2-receiver").read_bytes(),
                )
            else:
                self.assertIn(b"windows-contract-fallback", installed_receiver)

            compose_overlay = yaml.safe_load(
                (checkout / "ops/deploy/bootstrap/docker-compose.buh-platform-v2.yml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                compose_overlay["services"]["nginx"]["image"],
                policy["host_baseline"]["nginx"]["image_id"],
            )
            self.assertIn(
                "./conf/buh-platform-v2/nginx:/etc/nginx/buh-platform-v2:ro",
                compose_overlay["services"]["nginx"]["volumes"],
            )
            self.assertEqual(
                policy["host_baseline"]["compose_files_before_activation"],
                ["docker-compose.yml", "docker-compose.buh-vps-health.yml"],
            )
            self.assertEqual(
                policy["host_baseline"]["compose_files_after_activation"][-1],
                "docker-compose.buh-platform-v2.yml",
            )

            health_host = DockerHost(
                contracts.ReceiverConfig.load(
                    checkout / "ops/deploy/receiver-config.example.json"
                )
            )
            health_host.log_since = "2026-09-07T22:17:17+00:00"
            owner_container = container_id(20)
            with owner_log_environment(
                health_host,
                {
                    owner_container: RETAINED_DISCORD_OWNER_LOG.read_text(
                        encoding="utf-8"
                    )
                },
            ):
                visible = health_host._scan_new_logs(
                    health_host.config.auth_services,
                    owner_transition_phase="candidate-health",
                )
                self.assertEqual(len(visible), 1)
                self.assertIn("guild-owner nickname 50013", visible[0])
                with self.assertRaisesRegex(
                    contracts.DeploymentError, "fatal AllianceAuth log"
                ):
                    with mock.patch.object(
                        health_host,
                        "_compose",
                        return_value=discord_nickname_record(frames=39),
                    ):
                        health_host._scan_new_logs(health_host.config.auth_services)

            target = deployment_bundle.request.recovery_transition["releases"][-1]
            self.assertEqual(target["release_commit"], release_commit)

            preflight_bundle = dataclasses.replace(
                deployment_bundle,
                request=dataclasses.replace(
                    deployment_bundle.request,
                    # The no-change preflight is the same verified release and
                    # recovery evidence, differing only in operation identity.
                    mode="preflight",
                    workflow_run_id="6000002",
                ),
            )
            preflight_host, preflight_boundary = prepare_real_recovery_host(
                base / "preflight-host",
                preflight_bundle,
                fail_during_stabilization=False,
            )
            preflight_current = (
                preflight_host.config.state_dir / "current.json"
            ).read_bytes()
            preflight_marker = preflight_host._platform_current_path().read_bytes()
            preflight_journal = PreflightJournal(
                preflight_host.config.state_dir, preflight_bundle
            )
            with exercised_docker_boundary(preflight_host, preflight_boundary):
                PreflightEngine(preflight_host, preflight_journal).run(
                    preflight_bundle
                )
            self.assertEqual(
                (preflight_host.config.state_dir / "current.json").read_bytes(),
                preflight_current,
            )
            self.assertEqual(
                preflight_host._platform_current_path().read_bytes(),
                preflight_marker,
            )
            self.assertEqual(preflight_boundary.static_mapping, "v0.5.6")
            self.assertEqual(preflight_boundary.reload_count, 0)
            for service, expected in preflight_boundary.original_images.items():
                self.assertEqual(
                    {
                        preflight_boundary.containers[container]["image"]
                        for container in preflight_boundary.services[service]
                    },
                    {expected},
                )
            preflight_digest = "sha256:" + hashlib.sha256(
                preflight_journal.path.read_bytes()
            ).hexdigest()
            preflight_artifact = "platform-v2-preflight-6000002-1"

            # Rehearse the one published-release recovery protocol and carry
            # its authorization output into the existing deployment handoff.
            # The immutable release and source are this test's real assembled
            # objects; activation/workflow identities remain synthetic remote
            # evidence and are kept explicitly separate.
            activation = "a" * 40
            activation_head = "b" * 40
            activation_tree = "c" * 40
            activation_run = 6000003
            recovery_run = 6000004
            continuation = "d" * 40
            continuation_head = "e" * 40
            continuation_tree = "f" * 40
            continuation_run = 6000005
            recovery_artifact_digest = "sha256:" + hashlib.sha256(
                b"synthetic exact-release validation artifact"
            ).hexdigest()
            recovery_artifact = (
                f"platform-validation-recovery-v{target['platform_version']}-"
                f"{release_commit[:12]}-{recovery_run}-1"
            )
            with mock.patch.multiple(
                approval_fixture,
                ACTIVATION=activation,
                ACTIVATION_HEAD=activation_head,
                ACTIVATION_RUN=activation_run,
                ACTIVATION_TREE=activation_tree,
                ARTIFACT=preflight_artifact,
                ARTIFACT_DIGEST=preflight_digest,
                FIRST_PARENT=head,
                CONTINUATION=continuation,
                CONTINUATION_HEAD=continuation_head,
                CONTINUATION_RUN=continuation_run,
                CONTINUATION_TREE=continuation_tree,
                MANIFEST=target["manifest_sha256"],
                PREFLIGHT_RUN=6000002,
                RECOVERY_ARTIFACT=recovery_artifact,
                RECOVERY_ARTIFACT_DIGEST=recovery_artifact_digest,
                RECOVERY_RUN=recovery_run,
                RELEASE=release_commit,
                SOURCE=head,
                VERSION=target["platform_version"],
            ):
                contract = approval_fixture._recovery_contract()
                attestation = approval_fixture._recovery_attestation()
                ready_report = approval_fixture.approval.ready_recovery(
                    contract,
                    approval_fixture.TOKEN,
                    owner=approval_fixture.OWNER,
                    repository=approval_fixture.REPOSITORY,
                    api_url="https://api.github.com",
                    server_url="https://github.com",
                    output=base / "recovery-ready.json",
                    feature_readiness=approval_fixture._published_readiness(
                        merge_source_commit=head
                    ),
                    activation_readiness=approval_fixture._published_readiness(
                        pull_request=53,
                        feature_head=activation_head,
                        merge_source_commit=activation,
                    ),
                    continuation_readiness=approval_fixture._published_readiness(
                        pull_request=54,
                        feature_head=continuation_head,
                        merge_source_commit=continuation,
                    ),
                    validation_attestation=attestation,
                    validation_artifact_id=(
                        approval_fixture.RECOVERY_ARTIFACT_ID
                    ),
                    validation_artifact_name=recovery_artifact,
                    validation_artifact_digest=recovery_artifact_digest,
                    transport=approval_fixture.RecoveryTransport(ready=True),
                    sleeper=lambda _: None,
                )
                self.assertTrue(
                    ready_report["approval_marker"].startswith(
                        approval_fixture.approval.RECOVERY_APPROVAL_PREFIX
                    )
                )
                self.assertEqual(
                    ready_report["ready"]["recovery_validation"]["attestation"][
                        "release"
                    ]["commit"],
                    release_commit,
                )
                self.assertEqual(
                    ready_report["ready"]["recovery_validation"]["attestation"][
                        "harness"
                    ]["commit"],
                    continuation,
                )

                authorization_index = 0

                def authorize(transport):
                    nonlocal authorization_index
                    authorization_index += 1
                    with mock.patch.object(
                        approval_fixture.approval.validation_recovery,
                        "load_contract",
                        return_value=contract,
                    ):
                        return approval_fixture.approval.authorize(
                            approval_fixture._recovery_event(),
                            owner=approval_fixture.OWNER,
                            repository=approval_fixture.REPOSITORY,
                            actor=approval_fixture.OWNER,
                            triggering_actor=approval_fixture.OWNER,
                            run_attempt=1,
                            api_url="https://api.github.com",
                            server_url="https://github.com",
                            output=base / f"approval-{authorization_index}.json",
                            token=approval_fixture.TOKEN,
                            transport=transport,
                            sleeper=lambda _: None,
                        )

                with self.assertRaisesRegex(
                    approval_fixture.approval.ApprovalError,
                    "lacks the ChatGPT approval marker",
                ):
                    authorize(
                        approval_fixture.RecoveryTransport(
                            ready=False,
                            payload=ready_report["ready"],
                            include_merge_marker=False
                        )
                    )
                authorized = authorize(
                    approval_fixture.RecoveryTransport(
                        ready=False, payload=ready_report["ready"]
                    )
                )
                self.assertEqual(
                    authorized["validation_mode"], "published-release-recovery"
                )
                self.assertEqual(authorized["release_commit"], release_commit)
                self.assertEqual(authorized["source_commit"], head)
                self.assertEqual(
                    authorized["manifest_sha256"],
                    deployment_bundle.request.manifest_sha256,
                )
                changed_evidence = approval_fixture.RecoveryTransport(
                    ready=False, payload=ready_report["ready"]
                )
                changed_evidence.recovery_run_conclusion = "failure"
                with self.assertRaisesRegex(
                    approval_fixture.approval.ApprovalError,
                    "did not complete successfully",
                ):
                    authorize(changed_evidence)

            failed_host, failed_boundary = prepare_real_recovery_host(
                base / "failed-host",
                deployment_bundle,
                fail_during_stabilization=True,
            )
            failed_current = (
                failed_host.config.state_dir / "current.json"
            ).read_bytes()
            failed_marker = failed_host._platform_current_path().read_bytes()
            failed_journal = DeploymentJournal(
                failed_host.config.state_dir, deployment_bundle
            )
            with self.assertRaisesRegex(
                contracts.DeploymentError,
                "synthetic stabilization boundary failure",
            ):
                with exercised_docker_boundary(failed_host, failed_boundary):
                    DeploymentEngine(failed_host, failed_journal).run(
                        deployment_bundle
                    )
            self.assertEqual(failed_boundary.static_mapping, "v0.5.6")
            self.assertGreaterEqual(failed_boundary.reload_count, 1)
            self.assertEqual(failed_boundary.max_beat_replicas, 1)
            self.assertGreaterEqual(failed_boundary.beat_stop_count, 2)
            for service, expected_count in (
                policy["host_baseline"]["auth_services"].items()
            ):
                containers = failed_boundary.services[service]
                self.assertEqual(len(containers), expected_count["replicas"])
                self.assertEqual(
                    {
                        failed_boundary.containers[container]["image"]
                        for container in containers
                    },
                    {expected_count["image_id"]},
                )
            self.assertEqual(
                (failed_host.config.state_dir / "current.json").read_bytes(),
                failed_current,
            )
            self.assertEqual(
                failed_host._platform_current_path().read_bytes(), failed_marker
            )
            self.assertTrue(
                (failed_host.backup_path / "database.sql.gz").is_file()
            )
            failed_record = json.loads(
                failed_journal.path.read_text(encoding="ascii")
            )
            self.assertEqual(failed_record["result"], "failed")
            self.assertEqual(failed_record["rollback"]["result"], "passed")
            self.assertEqual(failed_record["verification"]["result"], "failed")
            self.assertEqual(
                failed_record["verification"]["allowed_log_findings"], 3
            )

            successful_host, successful_boundary = prepare_real_recovery_host(
                base / "successful-host",
                deployment_bundle,
                fail_during_stabilization=False,
            )
            successful_journal = DeploymentJournal(
                successful_host.config.state_dir, deployment_bundle
            )
            with exercised_docker_boundary(successful_host, successful_boundary):
                DeploymentEngine(successful_host, successful_journal).run(
                    deployment_bundle
                )
            self.assertEqual(successful_boundary.static_mapping, "0.6.2")
            self.assertEqual(successful_boundary.max_beat_replicas, 1)
            self.assertEqual(successful_boundary.beat_stop_count, 1)
            for service, expected_count in (
                policy["host_baseline"]["auth_services"].items()
            ):
                containers = successful_boundary.services[service]
                self.assertEqual(len(containers), expected_count["replicas"])
                self.assertEqual(
                    {
                        successful_boundary.containers[container]["image"]
                        for container in containers
                    },
                    {successful_boundary.candidate_images[service]},
                )
            self.assertEqual(
                json.loads(
                    successful_host._platform_current_path().read_text(
                        encoding="ascii"
                    )
                )["release_commit"],
                target["release_commit"],
            )
            self.assertEqual(
                json.loads(
                    (successful_host.config.state_dir / "current.json").read_text(
                        encoding="ascii"
                    )
                )["manifest_sha256"],
                target["manifest_sha256"],
            )
            successful_record = json.loads(
                successful_journal.path.read_text(encoding="ascii")
            )
            self.assertEqual(successful_record["result"], "success")
            self.assertEqual(successful_record["verification"]["result"], "success")
            self.assertEqual(
                successful_record["verification"]["allowed_log_findings"], 2
            )
            self.assertIn(
                "server allianceauth_gunicorn:8000",
                (
                    successful_host.config.app_dir
                    / successful_host.config.nginx_upstream_file
                ).read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
