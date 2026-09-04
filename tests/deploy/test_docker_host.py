from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    DockerHost,
)


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_IMAGE = (
    "ghcr.io/allianceauth/allianceauth:v5.2.0@sha256:" + "1" * 64
)


def container_id(number: int) -> str:
    return f"{number:064x}"


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


def make_bundle(root: Path, *, image: str = RUNTIME_IMAGE) -> ValidatedBundle:
    request = DeploymentRequest(
        mode="preflight",
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


class DockerHostContracts(unittest.TestCase):
    def test_database_shell_exports_the_discovered_password(self):
        shell = DockerHost._database_shell()
        self.assertIn('export MYSQL_PWD="$password"', shell)
        self.assertNotIn('MYSQL_PWD="$password" exec', shell)

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
                "ops.deploy.docker_host.os.geteuid", return_value=0
            ), mock.patch.object(
                host, "_secure_private_directory"
            ), mock.patch.object(host, "_compose", return_value="\n".join(services)):
                host.validate(make_bundle(root))
            self.assertEqual(config.state_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(config.backup_dir.stat().st_mode & 0o777, 0o700)

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
            block = host._dockerfile_block(bundle)
            self.assertLess(block.index("vendor.whl"), block.index("structure.whl"))
            self.assertLess(block.index("structure.whl"), block.index("moon.whl"))
            self.assertEqual(block.count("python3 -m pip install"), 3)
            self.assertEqual(block.count("--force-reinstall"), 3)
            self.assertEqual(block.count("sha256sum --check --strict"), 3)
            self.assertNotIn("rm -f /tmp/buh-platform-v2", block)
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

            def compose(*arguments, **_kwargs):
                self.assertEqual(arguments[:2], ("ps", "-q"))
                return "\n".join(service_ids[arguments[-1]])

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host,
                "_run",
                return_value=(
                    "running|sha256:" + "6" * 64 + "|aa-docker-allianceauth:latest\n"
                ),
            ) as run:
                host._capture_live_image()

            self.assertEqual(host.auth_replica_counts, service_counts)
            self.assertEqual(host.previous_image_id, "sha256:" + "6" * 64)
            self.assertEqual(
                host.previous_image_references, ("aa-docker-allianceauth:latest",)
            )
            self.assertEqual(run.call_count, sum(service_counts.values()))

    def test_live_image_capture_rejects_mixed_replica_images(self):
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
                    f"running|sha256:{digest * 64}|aa-docker-allianceauth:latest\n"
                )

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ), self.assertRaisesRegex(DeploymentError, "do not share one image"):
                host._capture_live_image()

    def test_live_image_capture_rejects_a_missing_or_duplicate_replica(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            with mock.patch.object(host, "_compose", return_value=""), self.assertRaisesRegex(
                DeploymentError, "found no running containers"
            ):
                host._capture_live_image()

            duplicate = container_id(1)
            with mock.patch.object(
                host, "_compose", return_value=f"{duplicate}\n{duplicate}\n"
            ), self.assertRaisesRegex(DeploymentError, "invalid container identities"):
                host._capture_live_image()

    def test_shared_image_check_covers_every_scaled_replica(self):
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

            def compose(*arguments, **_kwargs):
                return "\n".join(service_ids[arguments[-1]])

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", return_value="sha256:" + "6" * 64 + "\n"
            ) as run:
                host._require_shared_image()
            self.assertEqual(run.call_count, sum(host.auth_replica_counts.values()))

            def mixed_images(arguments, **_kwargs):
                digest = "7" if arguments[-1] == container_id(13) else "6"
                return "sha256:" + digest * 64 + "\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=mixed_images
            ), self.assertRaisesRegex(DeploymentError, "do not share one image"):
                host._require_shared_image()

    def test_rollback_retags_exact_previous_image_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            host.original_dockerfile = host.backup_path / "custom.dockerfile"
            host.original_local_settings = host.backup_path / "local.py"
            host.original_dockerfile.write_text("FROM restored\n", encoding="utf-8")
            host.original_local_settings.write_text("RESTORED = True\n", encoding="utf-8")
            host.previous_image_id = "sha256:" + "6" * 64
            host.previous_image_references = ("aa-docker-auth:latest",)

            with mock.patch.object(host, "_run", return_value="") as run, mock.patch.object(
                host, "_compose", return_value=""
            ) as compose:
                recovery = host.rollback(make_bundle(root), "validated")

            self.assertIn("never replaced", recovery)
            run.assert_called_once_with(
                [
                    "docker",
                    "image",
                    "tag",
                    "sha256:" + "6" * 64,
                    "aa-docker-auth:latest",
                ],
                context="Previous AllianceAuth image reference restoration",
            )
            compose.assert_not_called()
            self.assertEqual(
                (config.app_dir / config.custom_dockerfile).read_text(), "FROM restored\n"
            )

    def test_swap_and_rollback_preserve_every_auth_replica(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            write_host_files(config)
            host = DockerHost(config)
            host.backup_path = root / "backup"
            host.backup_path.mkdir()
            host.original_dockerfile = host.backup_path / "custom.dockerfile"
            host.original_local_settings = host.backup_path / "local.py"
            host.original_dockerfile.write_text("FROM restored\n", encoding="utf-8")
            host.original_local_settings.write_text("RESTORED = True\n", encoding="utf-8")
            host.previous_image_id = "sha256:" + "6" * 64
            host.previous_image_references = ("aa-docker-auth:latest",)
            host.auth_replica_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            expected_scales = tuple(
                item
                for service in config.auth_services
                for item in (
                    "--scale",
                    f"{service}={host.auth_replica_counts[service]}",
                )
            )

            with mock.patch.object(host, "_compose", return_value="") as compose:
                host.swap(make_bundle(root))

            self.assertTrue(host.live_replacement_started)
            self.assertEqual(
                compose.call_args_list[0],
                mock.call(
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--force-recreate",
                    *expected_scales,
                    *config.auth_services,
                    context="AllianceAuth container replacement",
                ),
            )

            with mock.patch.object(host, "_run", return_value=""), mock.patch.object(
                host, "_compose", return_value=""
            ) as compose:
                recovery = host.rollback(make_bundle(root), "migrated")

            self.assertIn("containers were restored", recovery)
            compose.assert_any_call(
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                *expected_scales,
                *config.auth_services,
                context="Rollback container replacement",
            )

    def test_swap_fails_closed_without_captured_replica_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = DockerHost(make_config(Path(temporary)))
            with self.assertRaisesRegex(DeploymentError, "replica counts were not captured"):
                host.swap(make_bundle(Path(temporary)))
            self.assertFalse(host.live_replacement_started)

    def test_scaled_auth_health_requires_every_expected_replica(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)
            service_ids = {
                config.gunicorn_service: [container_id(1)],
                config.worker_service: [container_id(index) for index in range(2, 7)],
                config.database_service: [container_id(7)],
            }

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
            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", return_value="running|0|unhealthy\n"
            ):
                self.assertFalse(
                    host._containers_healthy(
                        {config.worker_service: 5},
                        zero_restart_services=set(config.auth_services),
                    )
                )


if __name__ == "__main__":
    unittest.main()
