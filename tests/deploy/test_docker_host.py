from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from ops.deploy.contracts import (
    DeploymentError,
    DeploymentRequest,
    ReceiverConfig,
    ValidatedBundle,
)
from ops.deploy.docker_host import (
    BEGIN_LEGACY,
    BEGIN_V2,
    END_LEGACY,
    END_V2,
    MAX_COMMAND_OUTPUT,
    DockerHost,
)
from ops.release import recovery_policy


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_IMAGE = (
    "ghcr.io/allianceauth/allianceauth:v5.2.0@sha256:" + "1" * 64
)
RETAINED_DISCORD_OWNER_LOG = (
    ROOT / "tests/deploy/fixtures/discord-owner-50013-mainprocess.log"
)


@contextmanager
def simulated_root_owned_lstat(path: Path):
    """Expose one runner-owned safety fixture as root-owned on POSIX CI."""

    real_lstat = Path.lstat

    def lstat_as_root(candidate):
        details = real_lstat(candidate)
        if candidate != path:
            return details
        return SimpleNamespace(
            st_mode=details.st_mode,
            st_uid=0,
            st_gid=0,
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_size=details.st_size,
        )

    with mock.patch.object(Path, "lstat", lstat_as_root):
        yield


def container_id(number: int) -> str:
    return f"{number:064x}"


def discord_nickname_record(
    *,
    user: str = "Fifty5D",
    process: str = "ForkPoolWorker-1",
    operation: str = "update_nickname",
    denied: str = "Discord HTTP 403, code 50013: Missing Permissions",
    frames: int = 12,
) -> str:
    """Synthetic Alliance Auth 5.2.0 multi-line Celery logging record."""

    lines = [
        (
            f"[2026-09-07 22:18:00,000: WARNING/{process}] [Discord Service] "
            f"{operation} failed for user {user}, retrying in 60 secs"
        ),
        "Traceback (most recent call last):",
        *[
            f'  File "/usr/local/lib/python3.11/site-packages/fixture.py", line {index}, in call'
            for index in range(frames)
        ],
        denied,
    ]
    return "\n".join(lines) + "\n"


@contextmanager
def owner_log_environment(
    host: DockerHost,
    logs: dict[str, str] | None = None,
    *,
    changed_container: bool = False,
    changed_image: bool = False,
    rollback: bool = False,
):
    """Provide exact retained-container and per-container log boundaries."""

    policy = recovery_policy.load_policy()
    owner = policy["host_baseline"]["discord_owner"]
    host.recovery_baseline_verified = True
    host.recovery_discord_owner = (
        owner["guild_id"],
        owner["discord_user_id"],
        owner["auth_username"],
    )
    worker_services = tuple(
        service
        for service in host.config.auth_services
        if service not in {
            host.config.gunicorn_service,
            host.config.beat_service,
        }
    )
    service_containers: dict[str, tuple[str, ...]] = {}
    next_id = 20
    for service in worker_services:
        count = policy["host_baseline"]["auth_services"][service]["replicas"]
        service_containers[service] = tuple(
            container_id(index) for index in range(next_id, next_id + count)
        )
        next_id += count
        host.auth_replica_counts[service] = count
        host.previous_images[service] = (
            policy["host_baseline"]["auth_services"][service]["image_id"],
            f"fixture/{service}:old",
        )
    if changed_container:
        service = worker_services[0]
        values = list(service_containers[service])
        values[0] = container_id(99)
        service_containers[service] = tuple(values)
    host.restart_baselines.update(
        {
            container: 0
            for containers in service_containers.values()
            for container in containers
            if not changed_container or container != container_id(99)
        }
    )
    supplied_logs = logs or {}

    def running(service: str, **_kwargs):
        return service_containers[service]

    def command(arguments, **_kwargs):
        container = arguments[-1]
        if arguments[1] == "inspect":
            service = next(
                name
                for name, containers in service_containers.items()
                if container in containers
            )
            image = host.previous_images[service][0]
            if changed_image and container == service_containers[worker_services[0]][0]:
                image = "sha256:" + "f" * 64
            restart = 0
            return f"running|{image}|{restart}\n"
        if arguments[1] == "logs":
            return supplied_logs.get(container, "")
        raise AssertionError(f"unexpected synthetic Docker command: {arguments!r}")

    if rollback:
        host.restart_baselines.clear()
    with mock.patch.object(
        host, "_running_service_containers", side_effect=running
    ), mock.patch.object(host, "_run", side_effect=command), mock.patch.object(
        host, "_compose", return_value=""
    ):
        yield service_containers


def make_config(root: Path) -> ReceiverConfig:
    source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
    app = root / "app"
    app.mkdir()
    return dataclasses.replace(
        source,
        app_dir=app,
        state_dir=root / "state",
        backup_dir=root / "backups",
    )


def make_bundle(
    root: Path, *, image: str = RUNTIME_IMAGE, mode: str = "preflight"
) -> ValidatedBundle:
    request = DeploymentRequest(
        mode=mode,
        repository="Fifty5D/B-UH-AllianceAuth",
        release_commit="a" * 40,
        release_ref="release/platform-v0.4.0",
        platform_version="0.4.0",
        manifest_sha256="b" * 64,
        workflow_run_id="1234",
        workflow_run_attempt=1,
    )
    artifacts = [
        {
            "component": "moon-tax",
            "kind": "owned",
            "origin": "built",
            "distribution": "aa-buh-moon-tax",
            "version": "0.3.3",
            "filename": "moon.whl",
            "sha256": "2" * 64,
        },
        {
            "component": "structure-ops",
            "kind": "owned",
            "origin": "reused",
            "distribution": "aa-buh-structure-ops",
            "version": "0.3.0",
            "filename": "structure.whl",
            "sha256": "3" * 64,
        },
        {
            "component": "aa-structures",
            "kind": "third-party",
            "origin": "provided",
            "distribution": "aa-structures",
            "version": "4.0.3",
            "filename": "vendor.whl",
            "sha256": "4" * 64,
        },
    ]
    manifest = {
        "source_commit": "c" * 40,
        "previous_release": None,
        "compatibility": {
            "values": {
                "production_baseline": {
                    "platform_version": "0.3.3",
                    "release_commit": "e70a8ee713d358e2f0e3c1dc5b23305c863aa628",
                    "deployment_generation": "legacy-v1",
                },
                "production_runtime": {"base_image": image},
                "policy": {
                    "database_migrations_must_be_rollback_compatible": True,
                    "legacy_bootstrap_may_skip_uninstalled_v2_releases": True,
                },
            }
        },
        "artifacts": artifacts,
    }
    plan = {
        "wheels": [
            {"distribution": item["distribution"], "version": item["version"]}
            for item in artifacts
        ],
        "setup_commands": ["buh_moon_tax_setup"],
        "django_apps": ["buh_moon_tax"],
    }
    return ValidatedBundle(root, root / "release", request, manifest, plan)


def make_recovery_bundle(root: Path, *, bootstrap: bool = False) -> ValidatedBundle:
    policy = recovery_policy.load_policy()
    fixed = policy["published_releases"]
    if bootstrap:
        target = fixed[-1]
        releases = fixed
        purpose = "receiver-upgrade-preflight"
        mode = "preflight"
    else:
        target = {
            "manifest_sha256": "9" * 64,
            "platform_version": "0.6.2",
            "release_commit": "8" * 40,
            "release_ref": "release/platform-v0.6.2",
            "source_commit": "7" * 40,
        }
        releases = [*fixed, target]
        purpose = "production-recovery"
        mode = "deploy"
    request = DeploymentRequest(
        mode=mode,
        repository=policy["repository"],
        release_commit=target["release_commit"],
        release_ref=target["release_ref"],
        platform_version=target["platform_version"],
        manifest_sha256=target["manifest_sha256"],
        workflow_run_id="1234",
        workflow_run_attempt=1,
        recovery_transition={
            "policy_id": policy["policy_id"],
            "policy_sha256": recovery_policy.policy_sha256(),
            "purpose": purpose,
            "releases": releases,
        },
    )
    manifest = dict(make_bundle(root).manifest)
    previous = fixed[-2] if bootstrap else fixed[-1]
    manifest.update(
        {
            "platform_version": target["platform_version"],
            "source_commit": target["source_commit"],
            "previous_release": {
                "manifest_sha256": previous["manifest_sha256"],
                "platform_version": previous["platform_version"],
                "source_commit": previous["source_commit"],
            },
        }
    )
    if not bootstrap:
        manifest["deployment_recovery"] = recovery_policy.manifest_recovery(policy)
    return ValidatedBundle(root, root / "release", request, manifest, {})


def write_host_files(config: ReceiverConfig) -> None:
    app = config.app_dir
    (app / config.compose_file).write_text("services: {}\n", encoding="utf-8")
    (app / config.env_file).write_text(
        f"AA_DOCKER_TAG={RUNTIME_IMAGE}\n", encoding="utf-8"
    )
    (app / config.custom_dockerfile).write_text(
        "FROM example.invalid/base\n"
        f"{BEGIN_LEGACY}\nRUN echo legacy\n{END_LEGACY}\n",
        encoding="utf-8",
    )
    settings = app / config.local_settings
    settings.parent.mkdir(parents=True)
    settings.write_text('INSTALLED_APPS += ["buh_moon_tax"]\n', encoding="utf-8")
    upstream = app / config.nginx_upstream_file
    upstream.parent.mkdir(parents=True)
    upstream.write_text(
        "# Managed by the B-UH Platform v2 receiver.\n"
        "upstream buh_platform_v2_active {\n"
        "    least_conn;\n"
        f"    server {config.gunicorn_service}:{config.gunicorn_port} "
        "max_fails=1 fail_timeout=5s;\n"
        "}\n",
        encoding="utf-8",
    )


def prepare_upstream_backup(host: DockerHost, config: ReceiverConfig) -> None:
    assert host.backup_path is not None
    upstream = config.app_dir / config.nginx_upstream_file
    details = upstream.stat()
    host.original_upstream = host.backup_path / "nginx-upstream.conf"
    host.original_upstream.write_bytes(upstream.read_bytes())
    host.original_upstream_metadata = (
        details.st_uid,
        details.st_gid,
        details.st_mode & 0o777,
    )


class DockerHostContracts(unittest.TestCase):
    def test_database_restore_clone_cleanup_failure_blocks_backup_without_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            raw_path = host.backup_path / "database.sql"

            def dump(_arguments, destination, **_kwargs):
                destination.write_bytes(b"-" * 256)

            def run(arguments, **kwargs):
                context = kwargs["context"]
                if context == "Database image discovery":
                    return "sha256:" + "a" * 64
                if context == "Ephemeral restore cleanup":
                    raise DeploymentError(
                        "cleanup failed access_token=must-never-be-retained"
                    )
                return ""

            with mock.patch.object(
                host, "_database_container", return_value="database-container"
            ), mock.patch.object(
                host, "_container_environment", return_value={}
            ), mock.patch.object(
                host, "_application_database", return_value="allianceauth"
            ), mock.patch.object(
                host, "_database_read_lock", return_value=nullcontext()
            ), mock.patch.object(
                host, "_run_to_file", side_effect=dump
            ), mock.patch.object(
                host, "_evidence_counts", return_value={"django_migrations": 4}
            ), mock.patch.object(
                host, "_run_from_file"
            ), mock.patch.object(
                host, "_run", side_effect=run
            ), self.assertRaisesRegex(
                DeploymentError, "container could not be removed"
            ) as caught:
                host.backup(make_bundle(root))
            self.assertNotIn("must-never-be-retained", str(caught.exception))
            self.assertTrue(raw_path.exists())
            self.assertFalse((host.backup_path / "BACKUP.json").exists())

    def test_crash_restart_consumes_durable_recovery_plan_across_mutation_states(self):
        for flag in (
            "migration_started",
            "traffic_switch_started",
            "workers_replacement_started",
            "gunicorn_replacement_started",
        ):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = make_config(root)
                write_host_files(config)
                host = DockerHost(config)
                attempt_id = make_bundle(root).request.attempt_id
                host.backup_path = config.backup_dir / attempt_id
                host.backup_path.mkdir(parents=True)
                host.original_dockerfile = host.backup_path / "custom.dockerfile"
                host.original_dockerfile.write_bytes(
                    (config.app_dir / config.custom_dockerfile).read_bytes()
                )
                host.original_local_settings = host.backup_path / "local.py"
                live_settings = config.app_dir / config.local_settings
                host.original_local_settings.write_bytes(live_settings.read_bytes())
                settings_details = live_settings.stat()
                host.original_local_settings_metadata = (
                    settings_details.st_uid,
                    settings_details.st_gid,
                    settings_details.st_mode & 0o777,
                )
                prepare_upstream_backup(host, config)
                host.platform_current_existed = False
                host.deployment_current_existed = False
                host.static_root_path = "/var/www/static"
                host.static_manifest_path = "/var/www/static/staticfiles.json"
                host.static_manifest_backup = host.backup_path / "staticfiles.previous.json"
                host.static_manifest_backup.write_text(
                    json.dumps(
                        {
                            "paths": {"x.js": "x.abcdef012345.js"},
                            "version": "1.1",
                            "hash": "abcdef012345",
                        }
                    ),
                    encoding="ascii",
                )
                host.static_manifest_sha256 = hashlib.sha256(
                    host.static_manifest_backup.read_bytes()
                ).hexdigest()
                host.static_manifest_entries = 1
                host.static_manifest_metadata = (1001, 1002, 0o640)
                host.static_assets_backup = host.backup_path / "static-assets.previous.tar"
                host.static_assets_backup.write_bytes(b"asset archive")
                host.static_assets_backup_sha256 = hashlib.sha256(
                    b"asset archive"
                ).hexdigest()
                host.static_assets_sha256 = "d" * 64
                host.static_assets_count = 1
                host.static_assets_bytes = 12
                host.previous_images = {
                    service: (
                        "sha256:" + format(index + 1, "x") * 64,
                        f"example.invalid/{service}:old",
                    )
                    for index, service in enumerate(config.auth_services)
                }
                host.previous_image_pins = {
                    service: f"buh-platform-v2-rollback:{attempt_id}-{index}"
                    for index, service in enumerate(config.auth_services)
                }
                host.auth_replica_counts = {
                    service: 1 for service in config.auth_services
                }
                setattr(host, flag, True)
                host._save_recovery_plan(attempt_id, f"crash-{flag}")
                captured: list[tuple[str, bool]] = []

                def recover(recovered_host, _bundle, phase):
                    captured.append((phase, getattr(recovered_host, flag)))
                    recovered_host.complete_recovery_plan()
                    return "exact prior topology restored"

                recovery_plan = config.state_dir / "active-recovery.json"
                with simulated_root_owned_lstat(recovery_plan), mock.patch.object(
                    DockerHost, "rollback", autospec=True, side_effect=recover
                ):
                    result = DockerHost.recover_incomplete_plan(config)
                self.assertIn("Recovered an incomplete prior deployment", result)
                self.assertEqual(captured, [(f"crash-{flag}", True)])
                self.assertFalse((config.state_dir / "active-recovery.json").exists())

    def test_bounded_command_output_fails_closed_without_dropping_early_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))

            class FakeProcess:
                def __init__(self):
                    self.stdout = io.BytesIO(b"x" * (MAX_COMMAND_OUTPUT + 1))
                    self.killed = False

                def wait(self, timeout=None):
                    del timeout
                    return 0

                def kill(self):
                    self.killed = True

            process = FakeProcess()
            with mock.patch(
                "ops.deploy.docker_host.subprocess.Popen", return_value=process
            ):
                with self.assertRaisesRegex(DeploymentError, "bounded output limit"):
                    host._run_bounded_output(
                        ["synthetic-logs"], timeout=5, context="Synthetic log scan"
                    )
            self.assertTrue(process.killed)

    def test_compose_prefix_preserves_every_host_overlay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            overlay = Path("docker-compose.buh-vps-health.yml")
            (config.app_dir / overlay).write_text("services: {}\n", encoding="utf-8")
            (config.app_dir / config.env_file).write_text(
                f"AA_DOCKER_TAG={RUNTIME_IMAGE}\n"
                f"COMPOSE_FILE={config.compose_file.as_posix()}:{overlay.as_posix()}\n",
                encoding="utf-8",
            )

            self.assertEqual(
                DockerHost(config).compose_prefix,
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(config.env_file),
                    "-f",
                    str(config.compose_file),
                    "-f",
                    str(overlay),
                ],
            )

    def test_compose_prefix_uses_configured_base_without_compose_file_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)

            self.assertEqual(
                DockerHost(config).compose_prefix[-2:],
                ["-f", str(config.compose_file)],
            )

    def test_compose_stack_rejects_missing_unsafe_or_incomplete_overlays(self):
        cases = (
            (
                "docker-compose.yml:missing.yml",
                "missing.yml is not a regular in-tree file",
            ),
            ("docker-compose.yml:../escape.yml", "unsafe path"),
            ("docker-compose.yml:docker-compose.yml", "duplicate files"),
            ("docker-compose.buh-vps-health.yml", "configured base file"),
            ("docker-compose.yml:", "invalid number of files"),
        )
        for compose_files, message in cases:
            with self.subTest(compose_files=compose_files), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = make_config(root)
                write_host_files(config)
                (config.app_dir / "docker-compose.buh-vps-health.yml").write_text(
                    "services: {}\n", encoding="utf-8"
                )
                (config.app_dir / config.env_file).write_text(
                    f"AA_DOCKER_TAG={RUNTIME_IMAGE}\nCOMPOSE_FILE={compose_files}\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(DeploymentError, message):
                    DockerHost(config).compose_prefix

    def test_compose_stack_rejects_nonstandard_path_separator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            (config.app_dir / config.env_file).write_text(
                f"AA_DOCKER_TAG={RUNTIME_IMAGE}\n"
                "COMPOSE_FILE=docker-compose.yml;docker-compose.override.yml\n"
                "COMPOSE_PATH_SEPARATOR=;\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DeploymentError, "must be ':'"):
                DockerHost(config).compose_prefix

    def test_database_shell_exports_the_discovered_password(self):
        shell = DockerHost._database_shell()
        self.assertIn('export MYSQL_PWD="$password"', shell)
        self.assertNotIn('MYSQL_PWD="$password" exec', shell)

    def test_database_dump_uses_the_explicitly_discovered_database(self):
        shell = DockerHost._database_dump_shell()
        self.assertIn('database="$1"', shell)
        self.assertNotIn("MARIADB_DATABASE:-${MYSQL_DATABASE", shell)

    def test_database_discovery_accepts_a_verified_declared_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            with mock.patch.object(
                host, "_database_query", return_value="allianceauth\n"
            ):
                self.assertEqual(
                    host._application_database(
                        container_id(1), {"MARIADB_DATABASE": "allianceauth"}
                    ),
                    "allianceauth",
                )

    def test_database_discovery_supports_legacy_container_without_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            with mock.patch.object(
                host, "_database_query", return_value="allianceauth\n"
            ):
                self.assertEqual(
                    host._application_database(container_id(1), {}),
                    "allianceauth",
                )

    def test_database_discovery_fails_closed_for_ambiguous_django_schemas(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            with mock.patch.object(
                host,
                "_database_query",
                return_value="allianceauth\nother_django\n",
            ), self.assertRaisesRegex(DeploymentError, "exactly one Django database"):
                host._application_database(container_id(1), {})

    def test_database_discovery_rejects_unverified_declared_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            with mock.patch.object(
                host, "_database_query", return_value="allianceauth\n"
            ), self.assertRaisesRegex(DeploymentError, "no Django migration history"):
                host._application_database(
                    container_id(1), {"MYSQL_DATABASE": "wrong_database"}
                )

    def test_preflight_validates_legacy_transition_runtime_and_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            services = [
                *config.auth_services,
                config.database_service,
                config.redis_service,
                config.proxy_service,
            ]
            config.state_dir.mkdir(mode=0o700)
            config.backup_dir.mkdir(mode=0o700)
            with mock.patch(
                "ops.deploy.docker_host.os.geteuid", return_value=0, create=True
            ), mock.patch.object(
                host, "_secure_private_directory"
            ), mock.patch.object(
                host, "_compose", return_value="\n".join(services)
            ), mock.patch.object(
                host, "_verify_compose_build_contract"
            ), mock.patch.object(host, "_verify_proxy_contract"):
                host.validate(make_bundle(root))
            if os.name != "nt":
                self.assertEqual(config.state_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(config.backup_dir.stat().st_mode & 0o777, 0o700)

    def test_schema_v1_allows_upgrade_preflight_without_managed_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(make_config(root), schema_version=1)
            write_host_files(config)
            (config.app_dir / config.nginx_upstream_file).unlink()
            host = DockerHost(config)
            services = [
                *config.auth_services,
                config.database_service,
                config.redis_service,
                config.proxy_service,
            ]
            with mock.patch(
                "ops.deploy.docker_host.os.geteuid", return_value=0, create=True
            ), mock.patch.object(
                host, "_secure_private_directory"
            ), mock.patch.object(
                host, "_compose", return_value="\n".join(services)
            ), mock.patch.object(
                host, "_verify_compose_build_contract"
            ), mock.patch.object(host, "_verify_proxy_contract") as proxy_contract:
                host.validate(make_bundle(root, mode="preflight"))
            proxy_contract.assert_not_called()

    def test_schema_v1_rejects_production_deploy_before_host_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(make_config(root), schema_version=1)
            host = DockerHost(config)
            with mock.patch(
                "ops.deploy.docker_host.os.geteuid", return_value=0, create=True
            ), mock.patch.object(host, "_compose") as compose, self.assertRaisesRegex(
                DeploymentError, "requires receiver configuration schema v2"
            ):
                host.validate(make_bundle(root, mode="deploy"))
            compose.assert_not_called()

    def test_compose_build_contract_resolves_every_auth_service_to_reviewed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            services = {
                service: {
                    "build": {
                        "context": str(config.app_dir),
                        "dockerfile": str(config.custom_dockerfile),
                    }
                }
                for service in config.auth_services
            }
            rendered = "warning before JSON\n" + json.dumps({"services": services})
            with mock.patch.object(host, "_compose", return_value=rendered) as compose:
                host._verify_compose_build_contract()
            compose.assert_called_once_with(
                "config",
                "--no-interpolate",
                "--no-env-resolution",
                "--format",
                "json",
                bounded_output=True,
                context="Docker Compose build contract discovery",
            )

    def test_compose_build_contract_rejects_noop_or_misdirected_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            services = {
                service: {
                    "build": {
                        "context": str(config.app_dir),
                        "dockerfile": str(config.custom_dockerfile),
                    }
                }
                for service in config.auth_services
            }
            services[config.beat_service] = {}
            with mock.patch.object(
                host, "_compose", return_value=json.dumps({"services": services})
            ), self.assertRaisesRegex(DeploymentError, "file-backed build"):
                host._verify_compose_build_contract()

            services[config.beat_service] = {
                "build": {
                    "context": str(config.app_dir),
                    "dockerfile": "unreviewed.dockerfile",
                }
            }
            with mock.patch.object(
                host, "_compose", return_value=json.dumps({"services": services})
            ), self.assertRaisesRegex(DeploymentError, "reviewed custom Dockerfile"):
                host._verify_compose_build_contract()

    def test_proxy_contract_binds_managed_upstream_to_production_auth_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            configuration = """
            events {}
            http {
                upstream buh_platform_v2_active {
                    server allianceauth_gunicorn:8000;
                }
                server {
                    listen 80;
                    server_name auth.b-uh.com;
                    return 301 https://auth.b-uh.com$request_uri;
                }
                server {
                    listen 443 ssl;
                    server_name auth.b-uh.com;
                    location / {
                        proxy_pass http://buh_platform_v2_active;
                    }
                }
            }
            """
            with mock.patch.object(
                host, "_verify_proxy_upstream_bytes"
            ), mock.patch.object(host, "_proxy_exec", return_value=configuration) as proxy:
                host._verify_proxy_contract()
            proxy.assert_called_once_with(
                "nginx",
                "-T",
                bounded_output=True,
                context="Nginx production route discovery",
            )

    def test_proxy_contract_rejects_decoy_managed_route_and_live_direct_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            configuration = """
            http {
                upstream buh_platform_v2_active {
                    server allianceauth_gunicorn:8000;
                }
                server {
                    server_name decoy.invalid;
                    location / {
                        proxy_pass http://buh_platform_v2_active;
                    }
                }
                server {
                    server_name auth.b-uh.com;
                    location / {
                        proxy_pass http://allianceauth_gunicorn:8000;
                    }
                }
            }
            """
            with mock.patch.object(
                host, "_verify_proxy_upstream_bytes"
            ), mock.patch.object(
                host, "_proxy_exec", return_value=configuration
            ), self.assertRaisesRegex(DeploymentError, "production Auth route"):
                host._verify_proxy_contract()

    def test_legacy_bootstrap_requires_explicit_policy_to_skip_uninstalled_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            bundle = make_bundle(root)
            manifest = dict(bundle.manifest)
            manifest["previous_release"] = {
                "platform_version": "0.4.0",
                "source_commit": "d" * 40,
                "manifest_sha256": "e" * 64,
            }
            successor = dataclasses.replace(bundle, manifest=manifest)

            host._validate_release_transition(successor)

            compatibility = dict(manifest["compatibility"])
            values = dict(compatibility["values"])
            policy = dict(values["policy"])
            policy["legacy_bootstrap_may_skip_uninstalled_v2_releases"] = False
            values["policy"] = policy
            compatibility["values"] = values
            blocked_manifest = dict(manifest)
            blocked_manifest["compatibility"] = compatibility
            blocked = dataclasses.replace(bundle, manifest=blocked_manifest)
            with self.assertRaisesRegex(DeploymentError, "not authorized"):
                host._validate_release_transition(blocked)

    def test_established_host_recovery_requires_the_exact_live_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            bundle = make_recovery_bundle(root)
            baseline = recovery_policy.load_policy()["baseline"]
            current = {
                "manifest_sha256": baseline["manifest_sha256"],
                "platform_version": baseline["platform_version"],
                "release_commit": baseline["release_commit"],
                "source_commit": baseline["source_commit"],
            }
            with mock.patch.object(host, "_load_current", return_value=current):
                host._validate_release_transition(bundle)

            changed = {**current, "manifest_sha256": "0" * 64}
            with mock.patch.object(
                host, "_load_current", return_value=changed
            ), self.assertRaisesRegex(DeploymentError, "baseline.*live state"):
                host._validate_release_transition(bundle)

            direct = make_bundle(root)
            direct_manifest = dict(direct.manifest)
            direct_manifest["previous_release"] = {
                key: current[key]
                for key in ("manifest_sha256", "platform_version", "source_commit")
            }
            stale = dataclasses.replace(
                bundle,
                manifest=direct_manifest,
                request=dataclasses.replace(
                    bundle.request,
                    platform_version="0.5.7",
                    release_ref="release/platform-v0.5.7",
                ),
            )
            with mock.patch.object(
                host, "_load_current", return_value=current
            ), self.assertRaisesRegex(DeploymentError, "stale recovery"):
                host._validate_release_transition(stale)

    def test_recovery_host_baseline_binds_confirmed_topology_and_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            bundle = make_recovery_bundle(root)
            policy = recovery_policy.load_policy()
            expected = policy["host_baseline"]
            host.previous_images = {
                service: (identity["image_id"], f"aa-docker-{service}:latest")
                for service, identity in expected["auth_services"].items()
            }
            host.auth_replica_counts = {
                service: identity["replicas"]
                for service, identity in expected["auth_services"].items()
            }
            marker = config.app_dir / "conf/buh-platform-v2/CURRENT.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "manifest_sha256": policy["baseline"]["manifest_sha256"],
                        "platform_version": policy["baseline"]["platform_version"],
                        "release_commit": policy["baseline"]["release_commit"],
                        "schema_version": 1,
                    }
                ),
                encoding="ascii",
            )

            def containers(service, **_kwargs):
                if service in expected["auth_services"]:
                    count = expected["auth_services"][service]["replicas"]
                    return tuple(f"{service}-{index}" for index in range(count))
                if service == expected["nginx"]["service"]:
                    return (expected["nginx"]["container"],)
                if service == expected["front_proxy"]["service"]:
                    return (expected["front_proxy"]["container"],)
                return ()

            def command(_arguments, *, context, **_kwargs):
                if "provenance verification" in context:
                    return json.dumps(
                        {
                            "com.b-uh.platform.source": policy["baseline"][
                                "source_commit"
                            ],
                            "com.b-uh.platform.version": policy["baseline"][
                                "platform_version"
                            ],
                        }
                    )
                key = "nginx" if "nginx" in context else "front_proxy"
                identity = expected[key]
                return (
                    f"/{identity['container']}|{identity['image_id']}|running|0\n"
                )

            local = config.app_dir / config.local_settings
            real_stat = Path.stat

            def stat_with_confirmed_owner(path, *args, **kwargs):
                details = real_stat(path, *args, **kwargs)
                if path != local:
                    return details
                return SimpleNamespace(
                    st_mode=(details.st_mode & ~0o777) | 0o640,
                    st_uid=0,
                    st_gid=61000,
                )

            owner = expected["discord_owner"]
            owner_result = json.dumps(
                {
                    "configured": owner["discord_user_id"],
                    "guild": owner["guild_id"],
                    "uid": owner["discord_user_id"],
                    "username": owner["auth_username"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            with mock.patch.object(
                host,
                "_compose_files",
                return_value=tuple(
                    Path(item)
                    for item in expected["compose_files_after_activation"]
                ),
            ), mock.patch.object(
                host, "_running_service_containers", side_effect=containers
            ), mock.patch.object(
                host, "_run", side_effect=command
            ), mock.patch.object(
                host, "_manage_image", return_value=owner_result
            ), mock.patch.object(Path, "stat", stat_with_confirmed_owner):
                host._validate_recovery_host_baseline(bundle)
            self.assertTrue(host.recovery_baseline_verified)
            self.assertEqual(
                host.recovery_discord_owner,
                (
                    owner["guild_id"],
                    owner["discord_user_id"],
                    owner["auth_username"],
                ),
            )

            host.recovery_baseline_verified = False
            host.recovery_discord_owner = None
            changed_owner_result = json.dumps(
                {**json.loads(owner_result), "guild": "changed-guild"},
                sort_keys=True,
                separators=(",", ":"),
            )
            with mock.patch.object(
                host,
                "_compose_files",
                return_value=tuple(
                    Path(item)
                    for item in expected["compose_files_after_activation"]
                ),
            ), mock.patch.object(
                host, "_running_service_containers", side_effect=containers
            ), mock.patch.object(
                host, "_run", side_effect=command
            ), mock.patch.object(
                host, "_manage_image", return_value=changed_owner_result
            ), mock.patch.object(
                Path, "stat", stat_with_confirmed_owner
            ), self.assertRaisesRegex(DeploymentError, "owner association changed"):
                host._validate_recovery_host_baseline(bundle)

            host.recovery_baseline_verified = False
            bootstrap_bundle = make_recovery_bundle(root, bootstrap=True)
            with mock.patch.object(
                host,
                "_compose_files",
                return_value=tuple(
                    Path(item)
                    for item in expected["compose_files_before_activation"]
                ),
            ), mock.patch.object(
                host, "_running_service_containers", side_effect=containers
            ), mock.patch.object(
                host, "_run", side_effect=command
            ), mock.patch.object(
                host, "_manage_image", return_value=owner_result
            ), mock.patch.object(Path, "stat", stat_with_confirmed_owner):
                host._validate_recovery_host_baseline(bootstrap_bundle)
            self.assertTrue(host.recovery_baseline_verified)

            host.recovery_baseline_verified = False
            host.previous_images[config.beat_service] = (
                "sha256:" + "0" * 64,
                "changed",
            )
            with mock.patch.object(
                host,
                "_compose_files",
                return_value=tuple(
                    Path(item)
                    for item in expected["compose_files_after_activation"]
                ),
            ), self.assertRaisesRegex(DeploymentError, "image or replica baseline"):
                host._validate_recovery_host_baseline(bundle)

    def test_runtime_digest_or_host_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            with self.assertRaisesRegex(DeploymentError, "digest pinned"):
                host._validate_runtime_image(make_bundle(root, image="tag-only"))
            (config.app_dir / config.env_file).write_text(
                "AA_DOCKER_TAG=ghcr.io/example/wrong@sha256:" + "5" * 64 + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(DeploymentError, "does not match"):
                host._validate_runtime_image(make_bundle(root))

    def test_dockerfile_uses_stable_per_wheel_layers_and_replaces_legacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            bundle = make_bundle(root)
            dockerfile = config.app_dir / config.custom_dockerfile
            dockerfile.chmod(0o640)
            original_owner = (dockerfile.stat().st_uid, dockerfile.stat().st_gid)
            block = host._dockerfile_block(bundle)
            self.assertLess(block.index("vendor.whl"), block.index("structure.whl"))
            self.assertLess(block.index("structure.whl"), block.index("moon.whl"))
            self.assertEqual(block.count("python3 -m pip install"), 3)
            self.assertEqual(block.count("--force-reinstall"), 3)
            self.assertEqual(block.count("sha256sum --check --strict"), 3)
            self.assertNotIn("rm -f /tmp/buh-platform-v2", block)
            for label, value in host._candidate_provenance_labels(bundle).items():
                self.assertIn(f"{label}={json.dumps(value)}", block)
            host._write_candidate_dockerfile(bundle)
            written = (config.app_dir / config.custom_dockerfile).read_text()
            self.assertIn(BEGIN_V2, written)
            self.assertIn(END_V2, written)
            self.assertNotIn(BEGIN_LEGACY, written)
            self.assertNotIn(END_LEGACY, written)
            expected_printf = "RUN printf '%s  %s\\n' \\"
            self.assertEqual(
                [line for line in written.splitlines() if line.startswith("RUN printf")],
                [expected_printf] * 3,
            )

            # Replacing an existing Platform v2 block must preserve the same
            # literal shell escapes as the legacy-to-v2 migration path.
            host._write_candidate_dockerfile(bundle)
            rewritten = (config.app_dir / config.custom_dockerfile).read_text()
            self.assertEqual(
                [line for line in rewritten.splitlines() if line.startswith("RUN printf")],
                [expected_printf] * 3,
            )
            rewritten_details = dockerfile.stat()
            if os.name != "nt":
                self.assertEqual(rewritten_details.st_mode & 0o777, 0o640)
            self.assertEqual(
                (rewritten_details.st_uid, rewritten_details.st_gid), original_owner
            )

    def test_live_image_capture_accepts_scaled_services_and_records_topology(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            service_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            service_ids: dict[str, list[str]] = {}
            next_id = 1
            for service, count in service_counts.items():
                service_ids[service] = [
                    container_id(number) for number in range(next_id, next_id + count)
                ]
                next_id += count
            container_services = {
                container: service
                for service, containers in service_ids.items()
                for container in containers
            }
            service_images = {
                service: "sha256:" + str(index) * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            service_references = {
                service: f"aa-docker-{service}:latest"
                for service in config.auth_services
            }

            def compose(*arguments, **_kwargs):
                self.assertEqual(arguments[:2], ("ps", "-q"))
                return "\n".join(service_ids[arguments[-1]])

            def inspect(arguments, **_kwargs):
                service = container_services[arguments[-1]]
                return (
                    f"running|{service_images[service]}|"
                    f"{service_references[service]}|0\n"
                )

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ) as run:
                host._capture_live_images()

            self.assertEqual(host.auth_replica_counts, service_counts)
            self.assertEqual(
                host.previous_images,
                {
                    service: (service_images[service], service_references[service])
                    for service in config.auth_services
                },
            )
            self.assertEqual(
                host.restart_baselines,
                {
                    container: 0
                    for containers in service_ids.values()
                    for container in containers
                },
            )
            self.assertEqual(run.call_count, sum(service_counts.values()))

    def test_live_image_capture_rejects_mixed_images_within_one_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            service_ids = {
                service: [container_id(index)]
                for index, service in enumerate(config.auth_services, start=1)
            }
            service_ids[config.worker_service].append(container_id(99))

            def compose(*arguments, **_kwargs):
                return "\n".join(service_ids[arguments[-1]])

            def inspect(arguments, **_kwargs):
                digest = "7" if arguments[-1] == container_id(99) else "6"
                return (
                    f"running|sha256:{digest * 64}|"
                    "aa-docker-allianceauth:latest|0\n"
                )

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ), self.assertRaisesRegex(
                DeploymentError, "replicas for service allianceauth_worker"
            ):
                host._capture_live_images()

    def test_live_image_capture_rejects_one_reference_for_different_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            service_ids = {
                service: [container_id(index)]
                for index, service in enumerate(config.auth_services, start=1)
            }
            container_services = {
                container: service
                for service, containers in service_ids.items()
                for container in containers
            }

            def compose(*arguments, **_kwargs):
                return "\n".join(service_ids[arguments[-1]])

            def inspect(arguments, **_kwargs):
                service = container_services[arguments[-1]]
                digest = str(config.auth_services.index(service) + 1)
                return (
                    f"running|sha256:{digest * 64}|"
                    "aa-docker-allianceauth:latest|0\n"
                )

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ), self.assertRaisesRegex(DeploymentError, "one image reference"):
                host._capture_live_images()

    def test_live_image_capture_rejects_a_missing_or_duplicate_replica(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            with mock.patch.object(host, "_compose", return_value=""), self.assertRaisesRegex(
                DeploymentError, "found no running containers"
            ):
                host._capture_live_images()

            duplicate = container_id(1)
            with mock.patch.object(
                host, "_compose", return_value=f"{duplicate}\n{duplicate}\n"
            ), self.assertRaisesRegex(DeploymentError, "invalid container identities"):
                host._capture_live_images()

    def test_infrastructure_restart_baselines_are_captured_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            host.restart_baselines = {container_id(1): 2}
            services = (
                config.database_service,
                config.redis_service,
                config.proxy_service,
            )
            identities = {
                service: container_id(index)
                for index, service in enumerate(services, start=20)
            }

            def compose(*arguments, **_kwargs):
                return identities[arguments[-1]]

            # The Docker inspect call receives the container identity, not the
            # Compose service; use its stable position for a non-zero baseline.
            def inspect_container(arguments, **_kwargs):
                container = arguments[-1]
                index = tuple(identities.values()).index(container)
                return f"running|{index + 4}\n"

            with mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch.object(host, "_run", side_effect=inspect_container):
                host._capture_infrastructure_restart_baselines()

            self.assertEqual(host.restart_baselines[container_id(1)], 2)
            self.assertEqual(
                {
                    container: host.restart_baselines[container]
                    for container in identities.values()
                },
                {
                    identities[config.database_service]: 4,
                    identities[config.redis_service]: 5,
                    identities[config.proxy_service]: 6,
                },
            )

    def test_live_image_check_covers_every_scaled_replica_per_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            service_ids = {
                service: [container_id(index)]
                for index, service in enumerate(config.auth_services, start=1)
            }
            service_ids[config.worker_service].extend(
                container_id(index) for index in range(10, 14)
            )
            host.auth_replica_counts = {
                service: len(ids) for service, ids in service_ids.items()
            }
            container_services = {
                container: service
                for service, containers in service_ids.items()
                for container in containers
            }
            expected_images = {
                service: "sha256:" + str(index) * 64
                for index, service in enumerate(config.auth_services, start=1)
            }

            def compose(*arguments, **_kwargs):
                return "\n".join(service_ids[arguments[-1]])

            def inspect(arguments, **_kwargs):
                service = container_services[arguments[-1]]
                return expected_images[service] + "\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ) as run:
                host._require_live_images(expected_images)
            self.assertEqual(run.call_count, sum(host.auth_replica_counts.values()))

            def mixed_images(arguments, **_kwargs):
                if arguments[-1] == container_id(13):
                    return "sha256:" + "f" * 64 + "\n"
                service = container_services[arguments[-1]]
                return expected_images[service] + "\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=mixed_images
            ), self.assertRaisesRegex(DeploymentError, "does not use its expected image"):
                host._require_live_images(expected_images)

    def test_candidate_image_capture_records_each_service_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            bundle = make_bundle(root)
            host.previous_images = {
                service: (
                    "sha256:" + str(index) * 64,
                    f"aa-docker-{service}:latest",
                )
                for index, service in enumerate(config.auth_services, start=1)
            }
            candidates = {
                service: "sha256:" + format(index + 8, "x") * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            references = {
                reference: service
                for service, (_image, reference) in host.previous_images.items()
            }
            labels = host._candidate_provenance_labels(bundle)

            def inspect(arguments, **_kwargs):
                return (
                    candidates[references[arguments[-1]]]
                    + "|"
                    + json.dumps(labels)
                    + "\n"
                )

            with mock.patch.object(host, "_run", side_effect=inspect) as run:
                host._capture_candidate_images(bundle)

            self.assertEqual(host.candidate_image_ids, candidates)
            self.assertEqual(run.call_count, len(config.auth_services))

    def test_candidate_image_capture_rejects_noop_and_wrong_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            bundle = make_bundle(root)
            host.previous_images = {
                service: (
                    "sha256:" + str(index) * 64,
                    f"aa-docker-{service}:latest",
                )
                for index, service in enumerate(config.auth_services, start=1)
            }
            labels = dict(host._candidate_provenance_labels(bundle))
            first_previous = host.previous_images[config.auth_services[0]][0]
            with mock.patch.object(
                host,
                "_run",
                return_value=first_previous + "|" + json.dumps(labels),
            ), self.assertRaisesRegex(DeploymentError, "previous image unchanged"):
                host._capture_candidate_images(bundle)

            labels["com.b-uh.platform.release"] = "f" * 40
            with mock.patch.object(
                host,
                "_run",
                return_value="sha256:" + "f" * 64 + "|" + json.dumps(labels),
            ), self.assertRaisesRegex(DeploymentError, "provenance labels"):
                host._capture_candidate_images(bundle)

    def test_previous_images_are_pinned_before_candidate_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            bundle = make_bundle(root)
            host.previous_images = {
                service: (
                    "sha256:" + str(index) * 64,
                    f"aa-docker-{service}:latest",
                )
                for index, service in enumerate(config.auth_services, start=1)
            }
            pins = {
                service: (
                    f"buh-platform-v2-rollback:{bundle.request.attempt_id}-{index}"
                )
                for index, service in enumerate(config.auth_services)
            }
            pin_images = {
                pins[service]: image
                for service, (image, _reference) in host.previous_images.items()
            }

            def pin_image(arguments, **_kwargs):
                if arguments[:4] == ["docker", "image", "inspect", "--format"]:
                    return pin_images[arguments[-1]] + "\n"
                return ""

            with mock.patch.object(host, "_run", side_effect=pin_image) as run:
                host._pin_previous_images(bundle)

            self.assertEqual(host.previous_image_pins, pins)
            self.assertEqual(
                run.call_args_list,
                [
                    call
                    for service in config.auth_services
                    for call in (
                        mock.call(
                            [
                                "docker",
                                "image",
                                "tag",
                                host.previous_images[service][0],
                                pins[service],
                            ],
                            context=f"Previous image retention for {service}",
                        ),
                        mock.call(
                            [
                                "docker",
                                "image",
                                "inspect",
                                "--format",
                                "{{.Id}}",
                                pins[service],
                            ],
                            context=(
                                "Previous image retention verification for "
                                f"{service}"
                            ),
                        ),
                    )
                ],
            )

    def test_rollback_retags_exact_previous_images_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            prepare_upstream_backup(host, config)
            host.original_dockerfile = host.backup_path / "custom.dockerfile"
            host.original_local_settings = host.backup_path / "local.py"
            host.original_dockerfile.write_text("FROM restored\n", encoding="utf-8")
            host.original_local_settings.write_text(
                (config.app_dir / config.local_settings).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            host.original_local_settings.chmod(0o600)
            local_settings = config.app_dir / config.local_settings
            local_settings.chmod(0o640)
            original_local_owner = (
                local_settings.stat().st_uid,
                local_settings.stat().st_gid,
            )
            host.original_local_settings_metadata = (
                *original_local_owner,
                local_settings.stat().st_mode & 0o777,
            )
            host.previous_images = {
                service: (
                    "sha256:" + str(index) * 64,
                    f"aa-docker-{service}:latest",
                )
                for index, service in enumerate(config.auth_services, start=1)
            }
            previous_image_pins = {
                service: f"buh-platform-v2-rollback:gh-test-{index}"
                for index, service in enumerate(config.auth_services, start=1)
            }
            host.previous_image_pins = previous_image_pins.copy()

            def restore_image(arguments, **_kwargs):
                if arguments[:4] == ["docker", "image", "inspect", "--format"]:
                    service = arguments[-1].removeprefix("aa-docker-").removesuffix(
                        ":latest"
                    )
                    index = config.auth_services.index(service) + 1
                    return "sha256:" + str(index) * 64 + "\n"
                return ""

            with mock.patch.object(host, "_run", side_effect=restore_image) as run, mock.patch.object(
                host, "_compose", return_value=""
            ) as compose:
                recovery = host.rollback(make_bundle(root), "validated")

            self.assertIn("without replacing live containers", recovery)
            self.assertEqual(
                run.call_args_list,
                [
                    call
                    for index, service in enumerate(config.auth_services, start=1)
                    for call in (
                        mock.call(
                            [
                                "docker",
                                "image",
                                "tag",
                                host.previous_images[service][0],
                                f"aa-docker-{service}:latest",
                            ],
                            context=f"Previous image reference restoration for {service}",
                        ),
                        mock.call(
                            [
                                "docker",
                                "image",
                                "inspect",
                                "--format",
                                "{{.Id}}",
                                f"aa-docker-{service}:latest",
                            ],
                            context=f"Previous image reference verification for {service}",
                        ),
                    )
                ]
                + [
                    mock.call(
                        ["docker", "image", "rm", previous_image_pins[service]],
                        context="Temporary rollback image tag cleanup",
                    )
                    for service in config.auth_services
                ],
            )
            self.assertEqual(host.previous_image_pins, {})
            compose.assert_not_called()
            self.assertEqual(
                (config.app_dir / config.custom_dockerfile).read_text(), "FROM restored\n"
            )
            restored_local_details = local_settings.stat()
            if os.name != "nt":
                self.assertEqual(restored_local_details.st_mode & 0o777, 0o640)
            self.assertEqual(
                (restored_local_details.st_uid, restored_local_details.st_gid),
                original_local_owner,
            )

    def test_schema_v1_preflight_cleanup_does_not_require_managed_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(make_config(root), schema_version=1)
            write_host_files(config)
            (config.app_dir / config.nginx_upstream_file).unlink()
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            host.original_dockerfile = host.backup_path / "custom.dockerfile"
            host.original_local_settings = host.backup_path / "local.py"
            host.original_dockerfile.write_text("FROM restored\n", encoding="utf-8")
            live_settings = config.app_dir / config.local_settings
            host.original_local_settings.write_bytes(live_settings.read_bytes())
            if os.name != "nt":
                live_settings.chmod(0o640)
            details = live_settings.stat()
            host.original_local_settings_metadata = (
                details.st_uid,
                details.st_gid,
                details.st_mode & 0o777,
            )

            self.assertTrue(host._restore_candidate_configuration())
            self.assertIsNone(host.original_upstream)

            if os.name != "nt":
                live_settings.chmod(0o600)
                with self.assertRaisesRegex(
                    DeploymentError, "ownership or permissions changed"
                ):
                    host._restore_candidate_configuration()

    def test_candidate_restore_retags_a_shared_reference_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            prepare_upstream_backup(host, config)
            host.original_dockerfile = host.backup_path / "custom.dockerfile"
            host.original_local_settings = host.backup_path / "local.py"
            host.original_dockerfile.write_text("FROM restored\n", encoding="utf-8")
            host.original_local_settings.write_text(
                (config.app_dir / config.local_settings).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            local_details = (config.app_dir / config.local_settings).stat()
            host.original_local_settings_metadata = (
                local_details.st_uid,
                local_details.st_gid,
                local_details.st_mode & 0o777,
            )
            shared = ("sha256:" + "a" * 64, "aa-docker-allianceauth:latest")
            host.previous_images = {
                service: (
                    shared
                    if index <= 2
                    else (
                        "sha256:" + str(index) * 64,
                        f"aa-docker-{service}:latest",
                    )
                )
                for index, service in enumerate(config.auth_services, start=1)
            }

            reference_images = {
                reference: image for image, reference in host.previous_images.values()
            }

            def restore_image(arguments, **_kwargs):
                if arguments[:4] == ["docker", "image", "inspect", "--format"]:
                    return reference_images[arguments[-1]] + "\n"
                return ""

            with mock.patch.object(host, "_run", side_effect=restore_image) as run:
                self.assertTrue(host._restore_candidate_configuration())

            self.assertEqual(
                run.call_args_list,
                [
                    mock.call(
                        ["docker", "image", "tag", shared[0], shared[1]],
                        context=(
                            "Previous image reference restoration for "
                            f"{config.auth_services[0]}"
                        ),
                    ),
                    mock.call(
                        [
                            "docker",
                            "image",
                            "inspect",
                            "--format",
                            "{{.Id}}",
                            shared[1],
                        ],
                        context=(
                            "Previous image reference verification for "
                            f"{config.auth_services[0]}"
                        ),
                    ),
                    *[
                        call
                        for index, service in enumerate(
                            config.auth_services[2:], start=3
                        )
                        for call in (
                            mock.call(
                                [
                                    "docker",
                                    "image",
                                    "tag",
                                    "sha256:" + str(index) * 64,
                                    f"aa-docker-{service}:latest",
                                ],
                                context=(
                                    "Previous image reference restoration for "
                                    f"{service}"
                                ),
                            ),
                            mock.call(
                                [
                                    "docker",
                                    "image",
                                    "inspect",
                                    "--format",
                                    "{{.Id}}",
                                    f"aa-docker-{service}:latest",
                                ],
                                context=(
                                    "Previous image reference verification for "
                                    f"{service}"
                                ),
                            ),
                        )
                    ],
                ],
            )

    def test_worker_replacement_preserves_scale_and_never_duplicates_beat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            host.candidate_image_ids = {
                service: "sha256:" + str(index) * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            workers = tuple(
                service
                for service in config.auth_services
                if service not in {config.gunicorn_service, config.beat_service}
            )
            with mock.patch.object(host, "_compose", return_value="") as compose, mock.patch.object(
                host, "_wait_for_compose_services"
            ), mock.patch.object(host, "_require_live_images"), mock.patch.object(
                host, "_version_probe"
            ), mock.patch.object(host, "_celery_health"):
                host.replace_workers(make_bundle(root))

            self.assertTrue(host.workers_replacement_started)
            self.assertEqual(
                compose.call_args_list[0],
                mock.call(
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--force-recreate",
                    *host._scale_arguments_for(workers),
                    *workers,
                    context="Celery worker replacement",
                ),
            )
            self.assertEqual(
                compose.call_args_list[1],
                mock.call(
                    "stop",
                    config.beat_service,
                    context="Celery beat singleton stop",
                ),
            )
            self.assertEqual(
                compose.call_args_list[2],
                mock.call(
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--force-recreate",
                    "--scale",
                    f"{config.beat_service}=1",
                    config.beat_service,
                    context="Celery beat singleton replacement",
                ),
            )
            self.assertNotIn(config.gunicorn_service, compose.call_args_list[0].args)

    def test_recovery_worker_cutover_scans_owner_before_and_strictly_after_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.recovery_baseline_verified = True
            host.auth_replica_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            host.candidate_image_ids = {
                service: "sha256:" + str(index) * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            host.candidate_web_slots = ("candidate-slot",)
            replaced = tuple(
                service
                for service in config.auth_services
                if service != config.gunicorn_service
            )
            with mock.patch.object(host, "_compose", return_value=""), mock.patch.object(
                host, "_wait_for_compose_services"
            ), mock.patch.object(host, "_require_live_images"), mock.patch.object(
                host, "_version_probe"
            ), mock.patch.object(host, "_celery_health"), mock.patch.object(
                host, "_scan_new_logs", return_value=()
            ) as scan:
                host.replace_workers(make_recovery_bundle(root))

            self.assertEqual(
                scan.call_args_list,
                [
                    mock.call(
                        (*config.auth_services, *host.candidate_web_slots),
                        owner_transition_phase="worker-cutover",
                    ),
                    mock.call((*replaced, *host.candidate_web_slots)),
                ],
            )

    def test_recovery_candidate_health_uses_only_the_old_worker_owner_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.recovery_baseline_verified = True
            host.candidate_image_ids = {config.gunicorn_service: "sha256:" + "1" * 64}
            host.auth_replica_counts = {service: 1 for service in config.auth_services}
            host.candidate_web_slots = ("candidate-slot",)
            host.previous_web_slots = ("previous-slot",)
            with mock.patch.object(host, "_start_web_slots"), mock.patch.object(
                host, "_containers_healthy", return_value=True
            ), mock.patch.object(host, "_verify_previous_static_fallback"), mock.patch.object(
                host, "_candidate_runtime_checks"
            ), mock.patch.object(host, "_redis_health"), mock.patch.object(
                host, "_celery_health"
            ), mock.patch.object(host, "_scan_new_logs", return_value=()) as scan:
                host.candidate_health(make_recovery_bundle(root))
            scan.assert_called_once_with(
                (*config.auth_services, "candidate-slot", "previous-slot"),
                owner_transition_phase="candidate-health",
            )

    def test_worker_replacement_fails_before_mutation_when_beat_is_scaled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: (2 if service == config.beat_service else 1)
                for service in config.auth_services
            }
            with mock.patch.object(host, "_compose") as compose, self.assertRaisesRegex(
                DeploymentError, "Exactly one"
            ):
                host.replace_workers(make_bundle(root))
            compose.assert_not_called()
            self.assertFalse(host.workers_replacement_started)

    def test_celery_health_requires_exact_workers_queues_and_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: 1 for service in config.auth_services
            }
            workers = ("worker-a@host", "worker-b@host")
            reports = {
                "ping": {
                    worker: {"ok": "pong"} for worker in workers
                },
                "active_queues": {
                    worker: [
                        {"name": queue}
                        for queue in config.required_celery_queues
                    ]
                    for worker in workers
                },
                "registered": {
                    worker: list(config.required_celery_tasks)
                    for worker in workers
                },
            }

            def compose(*arguments, **_kwargs):
                command = next(
                    name
                    for name in ("ping", "active_queues", "registered")
                    if name in arguments
                )
                return "warning before structured output\n" + json.dumps(
                    reports[command]
                )

            with mock.patch.object(
                host, "_expected_celery_nodes", return_value=frozenset(workers)
            ), mock.patch.object(host, "_compose", side_effect=compose) as call:
                host._celery_health()
            self.assertEqual(call.call_count, 3)
            self.assertTrue(all("--json" in item.args for item in call.call_args_list))

            reports["active_queues"] = {
                worker: [{"name": "mycelery"}, {"name": "myservices"}]
                for worker in workers
            }
            with mock.patch.object(
                host, "_expected_celery_nodes", return_value=frozenset(workers)
            ), mock.patch.object(host, "_compose", side_effect=compose), self.assertRaisesRegex(
                DeploymentError, "missing expected queues"
            ):
                host._celery_health()

            reports["active_queues"] = {
                worker: [
                    {"name": queue} for queue in config.required_celery_queues
                ]
                for worker in workers[:1]
            }
            with mock.patch.object(
                host, "_expected_celery_nodes", return_value=frozenset(workers)
            ), mock.patch.object(host, "_compose", side_effect=compose), self.assertRaisesRegex(
                DeploymentError, "every expected worker"
            ):
                host._celery_health()

            stale = "worker-stale@host"
            reports["ping"] = {
                workers[0]: {"ok": "pong"},
                stale: {"ok": "pong"},
            }
            with mock.patch.object(
                host, "_expected_celery_nodes", return_value=frozenset(workers)
            ), mock.patch.object(host, "_compose", side_effect=compose), self.assertRaisesRegex(
                DeploymentError, "every expected worker"
            ):
                host._celery_health()

    def test_expected_celery_nodes_are_derived_from_running_containers(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            worker_services = tuple(
                service
                for service in config.auth_services
                if service not in {config.gunicorn_service, config.beat_service}
            )
            host.auth_replica_counts = {
                service: 1 for service in config.auth_services
            }
            containers = {
                service: container_id(index)
                for index, service in enumerate(worker_services, start=1)
            }

            def running(service, **_kwargs):
                return (containers[service],)

            def inspect(arguments, **_kwargs):
                service = next(
                    name for name, container in containers.items()
                    if container == arguments[-1]
                )
                hostname = arguments[-1][:12]
                pattern = (
                    "worker_services_%n"
                    if service.endswith("services")
                    else "worker_%n"
                )
                return (
                    hostname
                    + "\n"
                    + json.dumps(["celery", "-A", "myauth", "worker", "-n", pattern])
                    + "\nnull\n"
                )

            with mock.patch.object(
                host, "_running_service_containers", side_effect=running
            ), mock.patch.object(host, "_run", side_effect=inspect):
                nodes = host._expected_celery_nodes()
            self.assertEqual(
                nodes,
                frozenset(
                    (
                        "celery@worker_" + containers[worker_services[0]][:12],
                        "celery@worker_services_" + containers[worker_services[1]][:12],
                    )
                ),
            )

    def test_candidate_switch_uses_validated_nginx_reload_not_proxy_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            with mock.patch.object(host, "_activate_upstream") as activate, mock.patch.object(
                host, "_public_smoke_checks"
            ):
                host.switch_traffic(make_bundle(root))
            activate.assert_called_once_with(
                host.candidate_web_slots,
                backup_targets=(*host.previous_web_slots, config.gunicorn_service),
                context="Candidate traffic switch",
            )
            self.assertTrue(host.traffic_switch_started)

    def test_static_health_uses_the_collected_content_hashed_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            with mock.patch.object(host, "_manage_container") as candidate:
                host._verify_static_asset("candidate-slot")
            candidate.assert_called_once()
            candidate_call = candidate.call_args
            self.assertEqual(candidate_call.args[:3], ("candidate-slot", "shell", "-c"))
            self.assertIn("stored_name", candidate_call.args[3])
            self.assertIn("stored != source", candidate_call.args[3])
            self.assertIn("s.exists(stored)", candidate_call.args[3])

            with mock.patch.object(host, "_manage_live") as active:
                host._verify_static_asset(None)
            self.assertEqual(active.call_args.args[:2], ("shell", "-c"))
            self.assertEqual(active.call_args.args[2], candidate_call.args[3])

    def test_public_smoke_never_follows_external_or_looping_redirects(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))

            class RedirectingOpener:
                def __init__(self, location):
                    self.location = location

                def open(self, request, timeout):
                    del timeout
                    raise urllib.error.HTTPError(
                        request.full_url,
                        302,
                        "Found",
                        {"Location": self.location},
                        None,
                    )

            for location in ("https://attacker.invalid/healthy", "/"):
                with self.subTest(location=location), mock.patch(
                    "ops.deploy.docker_host.urllib.request.build_opener",
                    return_value=RedirectingOpener(location),
                ), self.assertRaisesRegex(DeploymentError, "unsafe redirect"):
                    host._public_smoke_checks()

    def test_internal_smoke_rejects_off_origin_redirect_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            actual = [
                [
                    urlsplit(check.url).path,
                    302,
                    (
                        "https://attacker.invalid/healthy"
                        if index == 0
                        else "/account/login/"
                    ),
                ]
                for index, check in enumerate(host.config.smoke_checks)
            ]
            with mock.patch.object(
                host, "_run", return_value=json.dumps(actual)
            ) as run, self.assertRaisesRegex(DeploymentError, "unexpected statuses"):
                host._internal_http_checks("candidate-slot")
            self.assertIn("getheader('Location')", run.call_args.args[0][5])

    def test_previous_static_manifest_fingerprint_and_every_hash_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            manifest = {
                "paths": {
                    "admin/css/base.css": "admin/css/base.0123456789ab.css",
                    "admin/js/core.js": "admin/js/core.abcdef012345.js",
                },
                "version": "1.1",
                "hash": "abcdef012345",
            }

            def static_command(arguments, **_kwargs):
                if arguments[1] == "exec":
                    return "1001:1002:640\n"
                self.assertEqual(arguments[1], "cp")
                (host.backup_path / "staticfiles.previous.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                return ""

            def asset_fingerprint(*_args, **_kwargs):
                return json.dumps(
                    {
                        "manifest_sha256": host.static_manifest_sha256,
                        "entries": 2,
                        "assets": 2,
                        "asset_bytes": 246,
                        "assets_sha256": "d" * 64,
                        "missing": 0,
                    }
                )

            with mock.patch.object(
                host,
                "_manage_live",
                return_value=json.dumps(
                    {"location": "/var/www/static", "manifest_name": "staticfiles.json"}
                ),
            ), mock.patch.object(
                host, "_running_service_containers", return_value=(container_id(1),)
            ), mock.patch.object(
                host, "_run", side_effect=static_command
            ), mock.patch.object(
                host, "_manage_container", side_effect=asset_fingerprint
            ) as fingerprint, mock.patch.object(
                host, "_capture_static_assets"
            ) as capture_assets:
                host._capture_static_manifest()

            self.assertEqual(
                host.static_manifest_path, "/var/www/static/staticfiles.json"
            )
            self.assertEqual(host.static_manifest_entries, 2)
            self.assertEqual(host.static_manifest_metadata, (1001, 1002, 0o640))
            self.assertEqual(host.static_assets_count, 2)
            self.assertEqual(host.static_assets_bytes, 246)
            self.assertEqual(host.static_assets_sha256, "d" * 64)
            capture_assets.assert_called_once_with(
                container_id(1),
                {
                    "admin/css/base.0123456789ab.css",
                    "admin/js/core.abcdef012345.js",
                },
            )
            fingerprint_program = fingerprint.call_args.args[3]
            self.assertIn("digest.update(chunk)", fingerprint_program)
            self.assertIn("max_total=536870912", fingerprint_program)
            if os.name != "nt":
                self.assertEqual(
                    host.static_manifest_backup.stat().st_mode & 0o777, 0o444
                )
            evidence = json.dumps(
                {
                    "manifest_sha256": host.static_manifest_sha256,
                    "entries": 2,
                    "assets": 2,
                    "asset_bytes": 246,
                    "assets_sha256": "d" * 64,
                    "missing": 0,
                }
            )
            with mock.patch.object(host, "_manage_container", return_value=evidence):
                host._verify_previous_static_manifest("previous-slot")
            missing = json.dumps(
                {
                    "manifest_sha256": host.static_manifest_sha256,
                    "entries": 2,
                    "assets": 2,
                    "asset_bytes": 246,
                    "assets_sha256": "d" * 64,
                    "missing": 1,
                }
            )
            with mock.patch.object(
                host, "_manage_container", return_value=missing
            ), self.assertRaisesRegex(DeploymentError, "referenced hashed asset"):
                host._verify_previous_static_manifest("previous-slot")

            overwritten = json.dumps(
                {
                    "manifest_sha256": host.static_manifest_sha256,
                    "entries": 2,
                    "assets": 2,
                    "asset_bytes": 246,
                    "assets_sha256": "e" * 64,
                    "missing": 0,
                }
            )
            with mock.patch.object(
                host, "_manage_container", return_value=overwritten
            ), self.assertRaisesRegex(
                DeploymentError, "hashed asset bytes changed"
            ):
                host._verify_previous_static_manifest("previous-slot")

    def test_public_static_manifest_copy_rejects_unexpected_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            manifest = {
                "paths": {
                    "admin/css/base.css": "admin/css/base.0123456789ab.css"
                },
                "version": "1.1",
                "hash": "abcdef012345",
                "access_token": "must-never-become-container-readable",
            }

            def static_command(arguments, **_kwargs):
                if arguments[1] == "exec":
                    return "1001:1002:640\n"
                (host.backup_path / "staticfiles.previous.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                return ""

            with mock.patch.object(
                host,
                "_manage_live",
                return_value=json.dumps(
                    {"location": "/var/www/static", "manifest_name": "staticfiles.json"}
                ),
            ), mock.patch.object(
                host, "_running_service_containers", return_value=(container_id(1),)
            ), mock.patch.object(
                host, "_run", side_effect=static_command
            ), self.assertRaisesRegex(
                DeploymentError, "mapping is unsafe"
            ):
                host._capture_static_manifest()

    def test_previous_slot_read_only_binds_the_pre_collection_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.auth_replica_counts = {
                service: 1 for service in host.config.auth_services
            }
            host.static_manifest_backup = root / "staticfiles.previous.json"
            host.static_manifest_path = "/var/www/static/staticfiles.json"
            with mock.patch.object(host, "_compose", return_value="") as compose, mock.patch.object(
                host, "_wait_for_slots"
            ):
                host._start_web_slots(
                    make_bundle(root),
                    role="previous",
                    expected_image="sha256:" + "a" * 64,
                )
            arguments = compose.call_args.args
            self.assertIn("--volume", arguments)
            self.assertIn(
                f"{host.static_manifest_backup}:{host.static_manifest_path}:ro",
                arguments,
            )
            self.assertLess(
                arguments.index("--volume"),
                arguments.index(host.config.gunicorn_service),
            )

    def test_previous_slots_start_before_candidate_build_or_collectstatic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            events = []

            def capture_live():
                host.previous_images = {
                    service: ("sha256:" + "a" * 64, f"image-{service}:old")
                    for service in config.auth_services
                }
                host.auth_replica_counts = {
                    service: 1 for service in config.auth_services
                }

            def compose(*arguments, **_kwargs):
                if arguments and arguments[0] == "build":
                    events.append("build")
                return ""

            with mock.patch.object(
                host, "_capture_live_images", side_effect=capture_live
            ), mock.patch.object(
                host, "_celery_health", side_effect=lambda: events.append("baseline-workers")
            ), mock.patch.object(
                host, "_capture_infrastructure_restart_baselines"
            ), mock.patch.object(
                host, "_pin_previous_images"
            ), mock.patch.object(
                host,
                "_capture_static_manifest",
                side_effect=lambda: events.append("manifest"),
            ), mock.patch.object(
                host,
                "_start_previous_web_slots",
                side_effect=lambda _bundle: events.append("previous"),
            ), mock.patch.object(
                host, "_stage_release", return_value=root / "staged"
            ), mock.patch.object(
                host, "_write_candidate_dockerfile"
            ), mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch.object(
                host, "_capture_candidate_images"
            ), mock.patch.object(
                host, "_version_probe"
            ), mock.patch.object(host, "_manage_image"):
                host.prepare_candidate(make_bundle(root))

            self.assertLess(events.index("manifest"), events.index("previous"))
            self.assertLess(events.index("baseline-workers"), events.index("manifest"))
            self.assertLess(events.index("previous"), events.index("build"))

    def test_collectstatic_routes_to_isolated_old_slots_and_rechecks_before_setup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            events: list[str] = []

            def manage(command, *_args, **_kwargs):
                events.append(command)
                if command == "collectstatic":
                    raise DeploymentError("synthetic collection failure")
                return ""

            with mock.patch.object(
                host, "_manage_image", side_effect=manage
            ), mock.patch.object(
                host,
                "_verify_previous_static_fallback",
                side_effect=lambda: events.append("verify-old-static"),
            ), mock.patch.object(
                host,
                "_activate_upstream",
                side_effect=lambda targets, **kwargs: events.append(
                    f"route:{targets[0]}:{kwargs['context']}"
                ),
            ), mock.patch.object(
                host, "_public_smoke_checks", side_effect=lambda: events.append("public")
            ):
                with self.assertRaisesRegex(DeploymentError, "collection failure"):
                    host.migrate(make_bundle(root))

            protection = next(
                index
                for index, event in enumerate(events)
                if event.endswith("Pre-collection static-safe traffic switch")
            )
            self.assertLess(events.index("verify-old-static"), protection)
            self.assertLess(protection, events.index("collectstatic"))
            self.assertTrue(host.traffic_switch_started)

            host.original_dockerfile = root / "original"
            with mock.patch.object(
                host,
                "_activate_upstream",
                side_effect=lambda targets, **kwargs: events.append(
                    f"rollback-route:{targets[0]}:{kwargs['context']}"
                ),
            ), mock.patch.object(
                host, "_public_smoke_checks", side_effect=lambda: events.append("public")
            ), mock.patch.object(
                host,
                "_restore_candidate_configuration",
                side_effect=lambda: events.append("restore-old-manifest") or True,
            ), mock.patch.object(
                host,
                "_verify_restored",
                side_effect=lambda _bundle, _services: events.append("verify-restored"),
            ), mock.patch.object(
                host,
                "_remove_web_slots",
                side_effect=lambda _slots: events.append("remove-slots"),
            ):
                host.rollback(make_bundle(root), "backed_up")

            rollback_previous = next(
                index
                for index, event in enumerate(events)
                if event.startswith("rollback-route:buh-web-previous")
            )
            restore = events.index("restore-old-manifest")
            rollback_active = next(
                index
                for index, event in enumerate(events)
                if event.startswith(
                    f"rollback-route:{host.config.gunicorn_service}"
                )
            )
            self.assertLess(rollback_previous, restore)
            self.assertLess(restore, rollback_active)
            self.assertLess(events.index("verify-restored"), events.index("remove-slots"))

    def test_collectstatic_rechecks_old_manifest_before_setup_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            events: list[str] = []

            def manage(command, *_args, **_kwargs):
                events.append(command)
                return ""

            with mock.patch.object(
                host, "_manage_image", side_effect=manage
            ), mock.patch.object(
                host,
                "_protect_static_collection_traffic",
                side_effect=lambda: events.append("protect-static-traffic"),
            ), mock.patch.object(
                host,
                "_verify_previous_static_fallback",
                side_effect=lambda: events.append("verify-old-static"),
            ):
                host.migrate(make_bundle(root))
            self.assertLess(
                events.index("protect-static-traffic"), events.index("collectstatic")
            )
            self.assertLess(
                events.index("collectstatic"), events.index("verify-old-static")
            )
            self.assertLess(
                events.index("verify-old-static"), events.index("buh_moon_tax_setup")
            )

    def test_static_manifest_rollback_is_staged_and_atomically_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.static_manifest_backup = root / "staticfiles.previous.json"
            host.static_manifest_backup.write_text("{}", encoding="ascii")
            host.static_manifest_path = "/var/www/static/staticfiles.json"
            host.static_manifest_sha256 = "a" * 64
            host.static_manifest_metadata = (1001, 1002, 0o640)
            host.static_assets_backup = root / "static-assets.previous.tar"
            host.static_assets_backup.write_bytes(b"archive")
            host.static_assets_backup_sha256 = hashlib.sha256(b"archive").hexdigest()
            host.static_root_path = "/var/www/static"
            host.static_assets_count = 1
            host.static_assets_bytes = 7
            host.static_assets_sha256 = "b" * 64
            host.static_collection_started = True
            host.candidate_web_slots = ("candidate-slot",)
            with mock.patch.object(host, "_run", return_value="") as run, mock.patch.object(
                host, "_container_names_for_service", return_value=()
            ), mock.patch.object(
                host, "_restore_static_assets", return_value="candidate-slot"
            ) as restore_assets, mock.patch.object(
                host, "_verify_previous_static_manifest"
            ) as verify:
                host._restore_static_manifest()
            restore_assets.assert_called_once_with(["candidate-slot"])
            verify.assert_called_once_with("candidate-slot")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][0:2], ["docker", "cp"])
            atomic_arguments = run.call_args_list[1].args[0]
            self.assertEqual(
                atomic_arguments[0:5],
                ["docker", "exec", "--user", "0", "candidate-slot"],
            )
            self.assertTrue(
                any('stat -c "%d"' in argument for argument in atomic_arguments)
            )
            self.assertIn(host.static_manifest_sha256, atomic_arguments)
            self.assertEqual(atomic_arguments[-3:], ["1001", "1002", "640"])

    def test_static_asset_archive_evidence_covers_every_old_hashed_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            archive_path = root / "static-assets.tar"
            assets = {
                "admin/css/base.0123456789ab.css": b"old-css",
                "vendor/module.abcdef012345.js": b"old-vendor-javascript",
            }
            with tarfile.open(archive_path, "w") as archive:
                for name, payload in assets.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mode = 0o640
                    info.uid = 1001
                    info.gid = 1002
                    archive.addfile(info, io.BytesIO(payload))
            aggregate = hashlib.sha256()
            for name in sorted(assets):
                aggregate.update(
                    (
                        json.dumps(
                            {
                                "path": name,
                                "sha256": hashlib.sha256(assets[name]).hexdigest(),
                                "size": len(assets[name]),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("ascii")
                )
            self.assertEqual(
                host._static_asset_archive_evidence(archive_path, set(assets)),
                {
                    "assets": 2,
                    "asset_bytes": sum(map(len, assets.values())),
                    "assets_sha256": aggregate.hexdigest(),
                },
            )

    def test_static_asset_restore_is_per_file_atomic_and_fsyncs_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.static_assets_backup = root / "static-assets.previous.tar"
            host.static_assets_backup.write_bytes(b"archive")
            host.static_assets_backup_sha256 = hashlib.sha256(b"archive").hexdigest()
            host.static_root_path = "/var/www/static"
            with mock.patch.object(host, "_run", return_value="") as run:
                restored = host._restore_static_assets(("candidate-slot",))
            self.assertEqual(restored, "candidate-slot")
            self.assertEqual(run.call_count, 2)
            program = run.call_args_list[1].args[0][7]
            self.assertIn("os.replace(temporary,target)", program)
            self.assertIn("os.fsync(directory)", program)
            self.assertIn("os.fchown(output.fileno(),member.uid,member.gid)", program)

    def test_no_change_preflight_does_not_rewrite_untouched_static_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.static_manifest_backup = root / "staticfiles.previous.json"
            host.static_manifest_path = "/var/www/static/staticfiles.json"
            host.static_manifest_sha256 = "a" * 64
            host.static_manifest_metadata = (1001, 1002, 0o640)
            with mock.patch.object(host, "_run") as run:
                host._restore_static_manifest()
            run.assert_not_called()

    def test_docker_command_failure_redacts_every_credential_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            secrets = (
                b'{"access_token":"json-secret"} '
                b'https://example.invalid/?token=query-secret '
                b'Authorization: Bearer bearer-secret '
                b'Cookie: sessionid=cookie-secret; '
                b'Set-Cookie: csrftoken=csrf-secret; '
                b'Bot abcdefghijklmnop '
                b'https://discord.com/api/webhooks/123/webhook-secret '
                b'eyJabcdefghijk.abcdefghijk.abcdefghijk'
            )
            completed = subprocess.CompletedProcess(
                args=["docker", "inspect"], returncode=1, stdout=secrets
            )
            with mock.patch(
                "ops.deploy.docker_host.subprocess.run", return_value=completed
            ), self.assertRaises(DeploymentError) as caught:
                host._run(["docker", "inspect"], context="Synthetic Docker command")
            retained = str(caught.exception)
            for secret in (
                "json-secret",
                "query-secret",
                "bearer-secret",
                "cookie-secret",
                "csrf-secret",
                "abcdefghijklmnop",
                "webhook-secret",
                "eyJabcdefghijk",
            ):
                self.assertNotIn(secret, retained)
            self.assertIn("<redacted>", retained)

    def test_candidate_upstream_has_verified_old_service_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            rendered = host._render_upstream(
                ("buh-web-candidate-gh-1234-1-1",),
                backup_targets=(host.config.gunicorn_service,),
            )
            self.assertIn(
                "server buh-web-candidate-gh-1234-1-1:8000 "
                "max_fails=1 fail_timeout=5s;",
                rendered,
            )
            self.assertIn(
                f"server {host.config.gunicorn_service}:8000 "
                "max_fails=1 fail_timeout=5s backup;",
                rendered,
            )
            with self.assertRaisesRegex(DeploymentError, "duplicate targets"):
                host._render_upstream(
                    (host.config.gunicorn_service,),
                    backup_targets=(host.config.gunicorn_service,),
                )

    def test_failed_nginx_reload_restores_previous_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            original = (config.app_dir / config.nginx_upstream_file).read_text()
            with mock.patch.object(
                host, "_verify_proxy_upstream_bytes"
            ) as visible, mock.patch.object(
                host,
                "_nginx_test_and_reload",
                side_effect=[
                    DeploymentError("synthetic reload failure"),
                    None,
                ],
            ) as reload:
                with self.assertRaisesRegex(DeploymentError, "synthetic reload"):
                    host._activate_upstream(
                        ("buh-web-candidate-gh-1234-1-1",),
                        context="Candidate traffic switch",
                    )
            self.assertEqual(
                (config.app_dir / config.nginx_upstream_file).read_text(),
                original,
            )
            self.assertEqual(reload.call_count, 2)
            self.assertEqual(visible.call_count, 2)
            self.assertIn("restoration", reload.call_args_list[-1].args[0])

    def test_stale_single_file_bind_fails_before_reload_and_restores_host_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            upstream = config.app_dir / config.nginx_upstream_file
            original = upstream.read_text(encoding="utf-8")
            stale_digest = hashlib.sha256(original.encode("utf-8")).hexdigest()

            with mock.patch.object(
                host,
                "_proxy_exec",
                return_value=(
                    f"{stale_digest}  {config.nginx_upstream_container_file}\n"
                ),
            ), mock.patch.object(
                host, "_nginx_test_and_reload"
            ) as reload, self.assertRaisesRegex(
                DeploymentError, "not visible as exact bytes"
            ):
                host._activate_upstream(
                    ("buh-web-candidate-gh-1234-1-1",),
                    context="Candidate traffic switch",
                )

            self.assertEqual(upstream.read_text(encoding="utf-8"), original)
            reload.assert_called_once_with("Candidate traffic switch restoration")

    def test_exact_container_visible_digest_precedes_each_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            candidate = "buh-web-candidate-gh-1234-1-1"
            rendered = host._render_upstream((candidate,))
            digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            events: list[str] = []

            def visible(*_args, **_kwargs):
                events.append("container-visible-digest")
                return f"{digest}  {config.nginx_upstream_container_file}\n"

            with mock.patch.object(
                host, "_proxy_exec", side_effect=visible
            ), mock.patch.object(
                host,
                "_nginx_test_and_reload",
                side_effect=lambda _context: events.append("reload"),
            ):
                host._activate_upstream(
                    (candidate,), context="Candidate traffic switch"
                )

            self.assertEqual(events, ["container-visible-digest", "reload"])

    def test_equal_host_bytes_still_require_exact_container_visibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            original = (config.app_dir / config.nginx_upstream_file).read_text(
                encoding="utf-8"
            )
            with mock.patch.object(
                host,
                "_proxy_exec",
                return_value=(
                    f"{'0' * 64}  {config.nginx_upstream_container_file}\n"
                ),
            ), mock.patch.object(
                host, "_nginx_test_and_reload"
            ) as reload, self.assertRaisesRegex(
                DeploymentError, "not visible as exact bytes"
            ):
                host._activate_upstream(
                    (config.gunicorn_service,),
                    context="Final active Gunicorn traffic route",
                )

            self.assertEqual(
                (config.app_dir / config.nginx_upstream_file).read_text(
                    encoding="utf-8"
                ),
                original,
            )
            reload.assert_not_called()

    def test_rollback_reloads_restored_upstream_before_removing_web_slots(self):
        """The host file can match while Nginx still serves the rollback slot."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            host.original_dockerfile = root / "original"
            host.traffic_switch_started = True
            host.workers_replacement_started = True
            host.gunicorn_replacement_started = True
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            events: list[str] = []
            upstream = config.app_dir / config.nginx_upstream_file

            def restore_configuration():
                # Reproduce the production rollback sequence: restoring the
                # backed-up file makes its bytes equal the final active target,
                # but the Nginx master still has the previous-slot route loaded.
                upstream.write_text(
                    host._render_upstream((config.gunicorn_service,)),
                    encoding="utf-8",
                )
                events.append("restore-config")
                return True

            with mock.patch.object(
                host,
                "_nginx_test_and_reload",
                side_effect=lambda _context: events.append("reload"),
            ), mock.patch.object(
                host, "_verify_proxy_upstream_bytes"
            ), mock.patch.object(
                host, "_public_smoke_checks"
            ), mock.patch.object(
                host,
                "_restore_candidate_configuration",
                side_effect=restore_configuration,
            ), mock.patch.object(
                host, "_restore_service_set"
            ), mock.patch.object(
                host,
                "_remove_web_slots",
                side_effect=lambda _names: events.append("remove-slots"),
            ), mock.patch.object(host, "_verify_restored"):
                host.rollback(make_bundle(root), "stabilized")

            self.assertEqual(events.count("reload"), 3)
            self.assertLess(
                max(index for index, value in enumerate(events) if value == "reload"),
                events.index("remove-slots"),
            )

    def test_rollback_routes_previous_slot_before_restoring_containers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            host.original_dockerfile = root / "original"
            host.traffic_switch_started = True
            host.workers_replacement_started = True
            host.gunicorn_replacement_started = True
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            calls: list[str] = []

            def activate(targets, *, context, backup_targets=()):
                backup = ",".join(backup_targets)
                calls.append(f"route:{targets[0]}:{backup}:{context}")

            with mock.patch.object(
                host, "_activate_upstream", side_effect=activate
            ), mock.patch.object(
                host, "_public_smoke_checks", side_effect=lambda: calls.append("public")
            ), mock.patch.object(
                host,
                "_restore_candidate_configuration",
                side_effect=lambda: calls.append("restore-config") or True,
            ), mock.patch.object(
                host,
                "_restore_service_set",
                side_effect=lambda services, **_kwargs: calls.append(
                    "restore:" + ",".join(services)
                ),
            ), mock.patch.object(
                host,
                "_remove_web_slots",
                side_effect=lambda _names: calls.append("remove-slots"),
            ), mock.patch.object(
                host,
                "_verify_restored",
                side_effect=lambda _bundle, _services: calls.append("verify"),
            ):
                recovery = host.rollback(make_bundle(root), "stabilized")

            self.assertTrue(calls[0].startswith("route:buh-web-previous"))
            self.assertIn(config.gunicorn_service, calls[0])
            self.assertLess(calls.index("restore-config"), calls.index("verify"))
            self.assertLess(calls.index("verify"), calls.index("remove-slots"))
            self.assertTrue(
                any(item.startswith(f"route:{config.gunicorn_service}") for item in calls)
            )
            self.assertTrue(
                any(
                    item.startswith(f"route:{config.gunicorn_service}")
                    and host.previous_web_slots[0] in item
                    for item in calls
                )
            )
            self.assertIn("Traffic was switched back first", recovery)
            self.assertIn("Database migrations were not reversed", recovery)

    def test_failed_rollback_health_retains_web_slots_for_diagnosis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.original_dockerfile = root / "original"
            host.traffic_switch_started = True
            host.gunicorn_replacement_started = True
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            with mock.patch.object(
                host, "_activate_upstream"
            ), mock.patch.object(
                host, "_public_smoke_checks"
            ), mock.patch.object(
                host, "_restore_candidate_configuration", return_value=True
            ), mock.patch.object(
                host, "_restore_service_set"
            ), mock.patch.object(
                host,
                "_verify_restored",
                side_effect=DeploymentError("restored health failed"),
            ), mock.patch.object(host, "_remove_web_slots") as remove, self.assertRaisesRegex(
                DeploymentError, "restored health failed"
            ):
                host.rollback(make_bundle(root), "web_promoted")
            remove.assert_not_called()

    def test_failed_traffic_first_smoke_still_restores_all_independent_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.original_dockerfile = root / "original"
            host.traffic_switch_started = True
            host.workers_replacement_started = True
            host.gunicorn_replacement_started = True
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            events: list[str] = []
            with mock.patch.object(
                host,
                "_activate_upstream",
                side_effect=lambda *_args, **kwargs: events.append(kwargs["context"]),
            ), mock.patch.object(
                host,
                "_public_smoke_checks",
                side_effect=DeploymentError("synthetic rollback smoke failure"),
            ), mock.patch.object(
                host,
                "_restore_candidate_configuration",
                side_effect=lambda: events.append("configuration") or True,
            ), mock.patch.object(
                host,
                "_restore_service_set",
                side_effect=lambda services, **_kwargs: events.append(
                    "services:" + ",".join(services)
                ),
            ), mock.patch.object(
                host,
                "_verify_restored",
                side_effect=lambda _bundle, _services: events.append("verify"),
            ), mock.patch.object(host, "_remove_web_slots") as remove, self.assertRaisesRegex(
                DeploymentError, "traffic-first rollback smoke"
            ):
                host.rollback(make_bundle(root), "web_promoted")

            self.assertEqual(events[0], "Traffic-first rollback switch")
            self.assertIn("configuration", events)
            self.assertTrue(
                any(event.startswith("services:") for event in events), events
            )
            self.assertIn("verify", events)
            remove.assert_not_called()

    def test_stabilization_repeats_full_health_and_writes_bounded_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(
                make_config(root),
                stabilization_seconds=30,
                stabilization_interval_seconds=10,
            )
            host = DockerHost(config)
            host.backup_path = root / "evidence"
            host.backup_path.mkdir()
            bundle = make_bundle(root)
            with mock.patch.object(
                host,
                "_functional_health",
                return_value=("ERROR known and explicitly allowed",),
            ) as health, mock.patch(
                "ops.deploy.docker_host.time.monotonic",
                side_effect=[0, 10, 20, 30],
            ), mock.patch("ops.deploy.docker_host.time.sleep"):
                evidence = host.stabilize(bundle)

            self.assertEqual(health.call_count, 3)
            report = json.loads((host.backup_path / "HEALTH.json").read_text())
            self.assertEqual(report["stabilization_seconds"], 30)
            self.assertEqual(report["iterations"], 3)
            self.assertEqual(len(report["allowed_log_findings"]), 1)
            self.assertEqual(evidence["filename"], "HEALTH.json")
            self.assertEqual(evidence["result"], "success")
            self.assertEqual(
                evidence["warnings"], ["ERROR known and explicitly allowed"]
            )
            self.assertIsNone(evidence["failure"])
            self.assertEqual(host.stabilization_evidence(), evidence)

    def test_failed_stabilization_retains_redacted_bounded_health_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(
                make_config(root),
                stabilization_seconds=30,
                stabilization_interval_seconds=10,
            )
            host = DockerHost(config)
            host.backup_path = root / "evidence"
            host.backup_path.mkdir()
            with mock.patch.object(
                host,
                "_functional_health",
                side_effect=DeploymentError(
                    'CRITICAL {"access_token":"json-health-secret"} '
                    "?token=query-health-secret Authorization: Bearer bearer-health-secret "
                    "Cookie: sessionid=cookie-health-secret; "
                    "https://discord.com/api/webhooks/123/health-webhook-secret "
                    + "x" * 1000
                ),
            ), mock.patch("ops.deploy.docker_host.time.monotonic", return_value=0):
                with self.assertRaisesRegex(DeploymentError, "CRITICAL"):
                    host.stabilize(make_bundle(root))

            evidence = host.stabilization_evidence()
            self.assertIsNotNone(evidence)
            assert evidence is not None
            self.assertEqual(evidence["result"], "failed")
            for secret in (
                "json-health-secret",
                "query-health-secret",
                "bearer-health-secret",
                "cookie-health-secret",
                "health-webhook-secret",
            ):
                self.assertNotIn(secret, evidence["failure"])
            self.assertLessEqual(len(evidence["failure"]), 500)
            report = json.loads((host.backup_path / "HEALTH.json").read_text())
            self.assertEqual(report["failure"], evidence["failure"])

    def test_log_scan_uses_empty_by_default_narrow_literal_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            with mock.patch.object(
                host,
                "_compose",
                return_value="CRITICAL token=do-not-retain\n",
            ):
                with self.assertRaisesRegex(DeploymentError, "CRITICAL") as caught:
                    host._scan_new_logs(config.auth_services)
            self.assertNotIn("do-not-retain", str(caught.exception))

            allowed_host = DockerHost(
                dataclasses.replace(
                    config,
                    fatal_log_allowlist=(
                        "component=moon_tax|exception=ExpectedProbeError|"
                        "message=expected during synthetic readiness probe",
                    ),
                )
            )
            allowed_line = (
                "ERROR moon_tax ExpectedProbeError "
                "expected during synthetic readiness probe"
            )
            with mock.patch.object(
                allowed_host,
                "_compose",
                return_value=allowed_line + "\n",
            ):
                self.assertEqual(
                    allowed_host._scan_new_logs(config.auth_services),
                    (allowed_line,),
                )
            with mock.patch.object(
                allowed_host,
                "_compose",
                return_value=(
                    "ERROR moon_tax ExpectedProbeError "
                    "unexpected production failure variant\n"
                ),
            ), self.assertRaisesRegex(DeploymentError, "fatal AllianceAuth log"):
                allowed_host._scan_new_logs(config.auth_services)

            for near_match in (
                "ERROR ExpectedProbeError moon_tax expected during synthetic readiness probe",
                "ERROR moon_tax ExpectedProbeError expected during synthetic readiness probe extra",
                "CRITICAL database corruption detected; moon_tax "
                "ExpectedProbeError expected during synthetic readiness probe",
            ):
                with self.subTest(near_match=near_match), mock.patch.object(
                    allowed_host, "_compose", return_value=near_match + "\n"
                ), self.assertRaisesRegex(DeploymentError, "fatal AllianceAuth log"):
                    allowed_host._scan_new_logs(config.auth_services)

    def test_allowlisted_health_finding_rejects_unstructured_secret_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = dataclasses.replace(
                make_config(Path(temporary)),
                fatal_log_allowlist=(
                    "component=moon_tax|exception=ExpectedProbeError|"
                    "message=expected during synthetic readiness probe",
                ),
            )
            host = DockerHost(config)
            raw = (
                '{"access_token":"finding-secret"} ERROR moon_tax '
                "ExpectedProbeError expected during synthetic readiness probe"
            )
            with mock.patch.object(
                host, "_compose", return_value=raw + "\n"
            ), self.assertRaisesRegex(DeploymentError, "fatal AllianceAuth log") as caught:
                host._scan_new_logs(config.auth_services)
            self.assertNotIn("finding-secret", str(caught.exception))
            self.assertIn("<redacted>", str(caught.exception))

    def test_log_scan_cannot_drop_an_early_fatal_line_after_one_thousand_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            logs = "ERROR early deployment failure\n" + "ordinary line\n" * 1500
            with mock.patch.object(host, "_compose", return_value=logs) as compose:
                with self.assertRaisesRegex(DeploymentError, "early deployment failure"):
                    host._scan_new_logs(config.auth_services)
            self.assertNotIn("--tail=1000", compose.call_args.args)
            self.assertTrue(compose.call_args.kwargs["bounded_output"])

    def test_log_gate_always_includes_proxy_database_and_redis(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            with mock.patch.object(
                host, "_compose", return_value="CRITICAL nginx configuration failed\n"
            ) as compose, self.assertRaisesRegex(
                DeploymentError, "nginx configuration failed"
            ):
                host._scan_new_logs(config.auth_services)
            scanned = {call.args[-1] for call in compose.call_args_list}
            self.assertTrue(
                {
                    config.proxy_service,
                    config.database_service,
                    config.redis_service,
                }.issubset(scanned)
            )

    def test_owner_nickname_records_are_bound_to_old_container_process_and_phase(self):
        policy = recovery_policy.load_policy()
        guild = policy["host_baseline"]["discord_owner"]["guild_id"]
        retained = RETAINED_DISCORD_OWNER_LOG.read_text(encoding="utf-8")
        retained_retries = "".join(
            retained.replace("04:50:00", timestamp)
            for timestamp in ("04:50:00", "04:51:00", "04:52:00")
        )
        cases = (
            ("fork-pool", discord_nickname_record(frames=40), "ForkPoolWorker-1", "celery"),
            (
                "main-process",
                discord_nickname_record(process="MainProcess", frames=40),
                "MainProcess",
                "celery",
            ),
            ("retained-production", retained, "MainProcess", "allianceauth,celery"),
            (
                "retained-production-retries",
                retained_retries,
                "MainProcess",
                "allianceauth,celery",
            ),
        )
        for phase in ("candidate-health", "worker-cutover", "rollback"):
            for name, record, process, formats in cases:
                with (
                    self.subTest(phase=phase, record=name),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    config = make_config(Path(temporary))
                    host = DockerHost(config)
                    container = container_id(20)
                    with owner_log_environment(
                        host,
                        {container: record},
                        rollback=phase == "rollback",
                    ):
                        findings = host._scan_new_logs(
                            config.auth_services, owner_transition_phase=phase
                        )
                    self.assertEqual(len(findings), 1)
                    self.assertIn(
                        f"retained {config.worker_service}/{container[:12]}",
                        findings[0],
                    )
                    self.assertIn(f"process {process}", findings[0])
                    self.assertIn(f"formats {formats}", findings[0])
                    self.assertIn(f"guild {guild}", findings[0])

    def test_retained_owner_record_near_matches_remain_fatal(self):
        retained = RETAINED_DISCORD_OWNER_LOG.read_text(encoding="utf-8")
        cases = {
            "another-member": retained.replace("user Fifty5D", "user OrdinaryMember"),
            "role-operation": retained.replace(
                "update_nickname failed", "update_groups failed"
            ),
            "unrelated-error": retained
            + "[2026-09-07 04:50:00,226: ERROR/MainProcess] unrelated failure\n",
            "wrong-member-id": retained.replace(
                "318985508913020930", "318985508913020931"
            ),
            "wrong-guild-id": retained.replace(
                "1521272563626672198", "1521272563626672199"
            ),
        }
        for name, record in cases.items():
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                config = make_config(Path(temporary))
                host = DockerHost(config)
                container = container_id(20)
                with owner_log_environment(
                    host,
                    {container: record},
                ), self.assertRaisesRegex(
                    DeploymentError, "fatal AllianceAuth log"
                ):
                    host._scan_new_logs(
                        config.auth_services,
                        owner_transition_phase="candidate-health",
                    )

    def test_owner_transition_rejects_ambiguous_member_role_repeated_and_transient_records(self):
        owner = discord_nickname_record(frames=4)
        owner_prefix = "\n".join(owner.splitlines()[:-1]) + "\n"
        member = discord_nickname_record(user="OrdinaryMember", frames=2)
        role = discord_nickname_record(operation="update_groups", frames=2)
        transient = discord_nickname_record(
            denied="Discord HTTP 429: transient rate limit", frames=2
        )
        unpaired = owner_prefix
        repeated_unpaired = owner + unpaired
        duplicate_denied = owner + "Discord HTTP 403, code 50013: Missing Permissions\n"
        interleaved = owner_prefix + member
        cases = {
            "another-member": member,
            "role-operation": role,
            "transient": transient,
            "unpaired": unpaired,
            "repeated-unpaired": repeated_unpaired,
            "duplicate-denial": duplicate_denied,
            "interleaved": interleaved,
        }
        for name, text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                config = make_config(Path(temporary))
                host = DockerHost(config)
                container = container_id(20)
                with owner_log_environment(host, {container: text}), self.assertRaisesRegex(
                    DeploymentError, "fatal AllianceAuth log"
                ):
                    host._scan_new_logs(
                        config.auth_services, owner_transition_phase="candidate-health"
                    )

    def test_owner_transition_rejects_changed_association_container_image_and_candidate(self):
        cases = ("association", "container", "image", "candidate")
        for name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                config = make_config(Path(temporary))
                host = DockerHost(config)
                candidate_logs = (
                    {"candidate-slot": discord_nickname_record()}
                    if name == "candidate"
                    else None
                )
                with owner_log_environment(
                    host,
                    candidate_logs,
                    changed_container=name == "container",
                    changed_image=name == "image",
                ):
                    if name == "association":
                        host.recovery_discord_owner = (
                            "changed-guild",
                            "318985508913020930",
                            "Fifty5D",
                        )
                    requested = (
                        (*config.auth_services, "candidate-slot")
                        if name == "candidate"
                        else config.auth_services
                    )
                    with self.assertRaises(DeploymentError):
                        host._scan_new_logs(
                            requested, owner_transition_phase="candidate-health"
                        )

    def test_owner_transition_ends_after_worker_replacement_and_is_retained_in_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            container = container_id(20)
            record = discord_nickname_record(frames=8)
            host.backup_path = root / "evidence"
            host.backup_path.mkdir()
            bundle = make_recovery_bundle(root)
            with owner_log_environment(host, {container: record}):
                findings = host._scan_new_logs(
                    config.auth_services, owner_transition_phase="worker-cutover"
                )
            host._retain_transition_log_findings(bundle, findings)
            with mock.patch.object(
                host, "_functional_health", return_value=()
            ), mock.patch(
                "ops.deploy.docker_host.time.monotonic", side_effect=(0, 300)
            ):
                evidence = host.stabilize(bundle)
            report = json.loads((host.backup_path / "HEALTH.json").read_text())
            self.assertEqual(evidence["allowed_log_findings"], 1)
            self.assertEqual(report["allowed_log_findings"], list(findings))

            def compose(*arguments, **_kwargs):
                return record if arguments[-1] == config.worker_service else ""

            with mock.patch.object(
                host, "_compose", side_effect=compose
            ), self.assertRaisesRegex(DeploymentError, "fatal AllianceAuth log"):
                host._scan_new_logs(config.auth_services)

    def test_exact_discord_permission_warning_is_visible_but_near_matches_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            default_config = make_config(Path(temporary))
            plain_warning = (
                "WARNING structure_ops Forbidden: 403 Forbidden "
                "(error code: 50013): Missing Permissions"
            )
            default_host = DockerHost(default_config)
            for unallowed in (
                "403 Forbidden (error code: 50013): Missing Permissions",
                plain_warning,
            ):
                with self.subTest(unallowed=unallowed), mock.patch.object(
                    default_host, "_compose", return_value=unallowed + "\n"
                ), self.assertRaisesRegex(
                    DeploymentError, "fatal AllianceAuth log"
                ):
                    default_host._scan_new_logs(default_config.auth_services)

            config = dataclasses.replace(
                default_config,
                fatal_log_allowlist=(
                    "component=structure_ops|exception=Forbidden|"
                    "message=403 Forbidden (error code: 50013): Missing Permissions",
                ),
            )
            host = DockerHost(config)
            exact = plain_warning
            with mock.patch.object(host, "_compose", return_value=exact + "\n"):
                self.assertEqual(
                    host._scan_new_logs(config.auth_services), (exact,)
                )
            for near_match in (
                exact.replace("50013", "50014"),
                exact.replace("structure_ops", "moon_tax"),
                exact + " for another resource",
            ):
                with self.subTest(near_match=near_match), mock.patch.object(
                    host, "_compose", return_value=near_match + "\n"
                ), self.assertRaisesRegex(DeploymentError, "fatal AllianceAuth log"):
                    host._scan_new_logs(config.auth_services)

    def test_scaled_auth_health_requires_every_expected_replica(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            service_ids = {
                config.gunicorn_service: [container_id(1)],
                config.worker_service: [container_id(index) for index in range(2, 7)],
                config.database_service: [container_id(7)],
            }
            host.restart_baselines = {container_id(7): 7}

            def compose(*arguments, **_kwargs):
                return "\n".join(service_ids[arguments[-1]])

            def inspect(arguments, **_kwargs):
                container = arguments[-1]
                if container == container_id(7):
                    return "running|7|healthy\n"
                return "running|0|healthy\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ):
                self.assertTrue(
                    host._containers_healthy(
                        {
                            config.gunicorn_service: 1,
                            config.worker_service: 5,
                            config.database_service: 1,
                        },
                        zero_restart_services=set(config.auth_services),
                    )
                )

            def restarted_auth(arguments, **_kwargs):
                container = arguments[-1]
                if container == container_id(1):
                    return "running|1|healthy\n"
                return "running|0|healthy\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=restarted_auth
            ):
                self.assertFalse(
                    host._containers_healthy(
                        {config.gunicorn_service: 1},
                        zero_restart_services=set(config.auth_services),
                    )
                )

            service_ids[config.worker_service].pop()
            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ):
                self.assertFalse(
                    host._containers_healthy(
                        {config.worker_service: 5},
                        zero_restart_services=set(config.auth_services),
                    )
                )

            service_ids[config.worker_service].append(container_id(6))
            with mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch.object(
                host, "_run", return_value="running|0|unhealthy\n"
            ):
                self.assertFalse(
                    host._containers_healthy(
                        {config.worker_service: 5},
                        zero_restart_services=set(config.auth_services),
                    )
                )

    def test_retained_container_restart_counts_cannot_change_during_stabilization(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            retained = {
                config.gunicorn_service: container_id(1),
                config.database_service: container_id(2),
                config.redis_service: container_id(3),
                config.proxy_service: container_id(4),
            }
            host.restart_baselines = {
                container: index
                for index, container in enumerate(retained.values(), start=3)
            }

            def compose(*arguments, **_kwargs):
                return retained[arguments[-1]]

            def inspect(arguments, **_kwargs):
                count = host.restart_baselines[arguments[-1]]
                return f"running|{count}|healthy\n"

            expected = {service: 1 for service in retained}
            with mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch.object(host, "_run", side_effect=inspect):
                self.assertTrue(
                    host._containers_healthy(
                        expected, zero_restart_services=set()
                    )
                )

            def incremented(arguments, **_kwargs):
                count = host.restart_baselines[arguments[-1]]
                if arguments[-1] == retained[config.proxy_service]:
                    count += 1
                return f"running|{count}|healthy\n"

            with mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch.object(host, "_run", side_effect=incremented):
                self.assertFalse(
                    host._containers_healthy(
                        expected, zero_restart_services=set()
                    )
                )

            retained[config.gunicorn_service] = container_id(99)
            with mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch.object(
                host, "_run", return_value="running|0|healthy\n"
            ):
                self.assertFalse(
                    host._containers_healthy(
                        expected, zero_restart_services=set()
                    )
                )

    def test_candidate_web_slots_match_live_gunicorn_replica_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(
                make_config(root), health_attempts=1
            )
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: (3 if service == config.gunicorn_service else 1)
                for service in config.auth_services
            }
            expected_image = "sha256:" + "a" * 64
            with mock.patch.object(host, "_compose", return_value="") as compose, mock.patch.object(
                host, "_wait_for_slots"
            ):
                names = host._start_web_slots(
                    make_bundle(root),
                    role="candidate",
                    expected_image=expected_image,
                )
            self.assertEqual(len(names), 3)
            self.assertEqual(host.candidate_web_slots, names)
            self.assertEqual(compose.call_count, 3)
            self.assertTrue(
                all(
                    call.args[:4] == ("run", "--detach", "--no-deps", "--name")
                    for call in compose.call_args_list
                )
            )

    def test_web_promotion_keeps_candidate_live_until_atomic_active_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            host.candidate_image_ids = {
                service: "sha256:" + str(index) * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            events: list[str] = []
            def start_previous(_bundle):
                host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
                events.append("previous-slot")

            def activate(*_args, context, **_kwargs):
                events.append(context)

            with mock.patch.object(
                host,
                "_start_previous_web_slots",
                side_effect=start_previous,
            ), mock.patch.object(
                host, "_compose", side_effect=lambda *_args, **_kwargs: events.append("replace") or ""
            ), mock.patch.object(
                host, "_wait_for_compose_services"
            ), mock.patch.object(
                host, "_require_live_images"
            ), mock.patch.object(
                host, "_version_probe"
            ), mock.patch.object(
                host,
                "_container_names_for_service",
                return_value=("allianceauth_gunicorn_1",),
            ), mock.patch.object(
                host, "_internal_http_checks"
            ), mock.patch.object(
                host,
                "_activate_upstream",
                side_effect=activate,
            ), mock.patch.object(host, "_public_smoke_checks"):
                host.promote_web(make_bundle(root))

            self.assertEqual(
                events,
                [
                    "previous-slot",
                    "Candidate rollback fallback switch",
                    "replace",
                    "Promoted Gunicorn traffic switch",
                ],
            )
            self.assertTrue(host.gunicorn_replacement_started)
            self.assertTrue(host.live_replacement_started)

    def test_final_marker_failure_retains_every_web_safety_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.staged_release = root / "releases" / "v0.4.0"
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
            with mock.patch.object(
                host, "_functional_health"
            ), mock.patch.object(
                host, "_activate_upstream"
            ), mock.patch.object(
                host, "_public_smoke_checks"
            ), mock.patch(
                "ops.deploy.docker_host._atomic_text",
                side_effect=DeploymentError("synthetic marker failure"),
            ), mock.patch.object(host, "_remove_web_slots") as remove:
                with self.assertRaisesRegex(DeploymentError, "marker failure"):
                    host.finalize(make_bundle(root))
            remove.assert_not_called()
            self.assertTrue(host.candidate_web_slots)
            self.assertTrue(host.previous_web_slots)

    def test_rollback_after_finalize_restores_exact_previous_current_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            marker = config.app_dir / "conf" / "buh-platform-v2" / "CURRENT.json"
            original = b'{"release":"previous-exact-bytes"}\n'
            marker.write_bytes(original)
            marker.chmod(0o640)
            original_mode = marker.stat().st_mode & 0o777
            deployment_marker = config.state_dir / "current.json"
            deployment_marker.parent.mkdir(parents=True)
            original_deployment = b'{"release":"previous-lineage"}\n'
            deployment_marker.write_bytes(original_deployment)
            deployment_marker.chmod(0o600)
            original_deployment_mode = deployment_marker.stat().st_mode & 0o777

            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            host.original_dockerfile = host.backup_path / "custom.dockerfile"
            host.original_dockerfile.write_bytes(
                (config.app_dir / config.custom_dockerfile).read_bytes()
            )
            host.original_local_settings = host.backup_path / "local.py"
            live_settings = config.app_dir / config.local_settings
            host.original_local_settings.write_bytes(live_settings.read_bytes())
            details = live_settings.stat()
            host.original_local_settings_metadata = (
                details.st_uid,
                details.st_gid,
                details.st_mode & 0o777,
            )
            prepare_upstream_backup(host, config)
            host._capture_platform_current()
            host._capture_deployment_current()
            host.staged_release = marker.parent / "releases" / "v0.4.0"

            with mock.patch.object(
                host, "_functional_health"
            ), mock.patch.object(
                host, "_activate_upstream"
            ), mock.patch.object(host, "_public_smoke_checks"):
                host.finalize(make_bundle(root))
            self.assertNotEqual(marker.read_bytes(), original)
            deployment_marker.write_bytes(b'{"release":"false-new-lineage"}\n')

            # This models a journal.advance/journal.succeed failure immediately
            # after DockerHost.finalize returned successfully.
            host.rollback(make_bundle(root), "web_promoted")
            self.assertEqual(marker.read_bytes(), original)
            self.assertEqual(deployment_marker.read_bytes(), original_deployment)
            if os.name != "nt":
                self.assertEqual(marker.stat().st_mode & 0o777, original_mode)
                self.assertEqual(
                    deployment_marker.stat().st_mode & 0o777,
                    original_deployment_mode,
                )

    def test_rollback_removes_current_marker_when_none_existed_before_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            host._capture_platform_current()
            host._capture_deployment_current()
            marker = host._platform_current_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text('{"release":"new"}\n', encoding="ascii")
            deployment_marker = config.state_dir / "current.json"
            deployment_marker.parent.mkdir(parents=True, exist_ok=True)
            deployment_marker.write_text('{"release":"new"}\n', encoding="ascii")
            host.platform_current_write_started = True

            host._restore_platform_current()
            host._restore_deployment_current()
            self.assertFalse(marker.exists())
            self.assertFalse(deployment_marker.exists())

    def test_post_success_cleanup_retains_image_pins_when_slot_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = DockerHost(make_config(root))
            host.previous_image_pins = {
                host.config.gunicorn_service: "buh-platform-v2-rollback:test"
            }
            with mock.patch.object(
                host,
                "_remove_web_slots",
                side_effect=DeploymentError("synthetic cleanup failure"),
            ), mock.patch.object(host, "_discard_previous_image_pins") as discard:
                with self.assertRaisesRegex(DeploymentError, "cleanup failure"):
                    host.cleanup_success(make_bundle(root))
            discard.assert_not_called()
            self.assertTrue(host.previous_image_pins)

    def test_functional_health_covers_proxy_topology_and_both_web_slots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(make_config(root), health_attempts=1)
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            host.previous_images = {
                service: (
                    "sha256:" + str(index) * 64,
                    f"aa-docker-{service}:latest",
                )
                for index, service in enumerate(config.auth_services, start=1)
            }
            host.candidate_image_ids = {
                service: "sha256:" + format(index + 8, "x") * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
            with mock.patch.object(
                host, "_containers_healthy", return_value=True
            ) as healthy, mock.patch.object(
                host, "_require_live_images"
            ) as images, mock.patch.object(
                host, "_version_probe"
            ) as versions, mock.patch.object(
                host, "_wait_for_slots"
            ), mock.patch.object(
                host, "_candidate_runtime_checks"
            ), mock.patch.object(
                host,
                "_container_names_for_service",
                return_value=("allianceauth_gunicorn",),
            ), mock.patch.object(
                host, "_internal_http_checks"
            ) as http, mock.patch.object(
                host, "_redis_health"
            ), mock.patch.object(
                host, "_celery_health"
            ), mock.patch.object(
                host, "_public_smoke_checks"
            ), mock.patch.object(
                host, "_scan_new_logs", return_value=()
            ):
                host._functional_health(make_bundle(root), promoted=False)

            expected_replicas = {
                **host.auth_replica_counts,
                config.database_service: 1,
                config.redis_service: 1,
                config.proxy_service: 1,
            }
            healthy.assert_called_once_with(
                expected_replicas,
                zero_restart_services=set(config.auth_services)
                - {config.gunicorn_service},
            )
            expected_images = dict(host.candidate_image_ids)
            expected_images[config.gunicorn_service] = host.previous_images[
                config.gunicorn_service
            ][0]
            images.assert_called_once_with(expected_images)
            self.assertEqual(
                {call.args[0] for call in versions.call_args_list},
                set(config.auth_services) - {config.gunicorn_service},
            )
            http.assert_called_once_with("allianceauth_gunicorn")



if __name__ == "__main__":
    unittest.main()
