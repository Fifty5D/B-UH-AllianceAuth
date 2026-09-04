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
                    f"{service_references[service]}\n"
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
                    f"running|sha256:{digest * 64}|aa-docker-allianceauth:latest\n"
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
                    "aa-docker-allianceauth:latest\n"
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
            config = make_config(Path(temporary))
            host = DockerHost(config)
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

            def inspect(arguments, **_kwargs):
                return candidates[references[arguments[-1]]] + "\n"

            with mock.patch.object(host, "_run", side_effect=inspect) as run:
                host._capture_candidate_images()

            self.assertEqual(host.candidate_image_ids, candidates)
            self.assertEqual(run.call_count, len(config.auth_services))

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

            self.assertIn("never replaced", recovery)
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
                                previous_image_pins[service],
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
            self.assertEqual(restored_local_details.st_mode & 0o777, 0o640)
            self.assertEqual(
                (restored_local_details.st_uid, restored_local_details.st_gid),
                original_local_owner,
            )

            local_settings.chmod(0o600)
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
            host.previous_images = {
                service: (
                    "sha256:" + str(index) * 64,
                    f"aa-docker-{service}:latest",
                )
                for index, service in enumerate(config.auth_services, start=1)
            }
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

            reference_images = {
                reference: image for image, reference in host.previous_images.values()
            }

            def restore_image(arguments, **_kwargs):
                if arguments[:4] == ["docker", "image", "inspect", "--format"]:
                    return reference_images[arguments[-1]] + "\n"
                return ""

            with mock.patch.object(host, "_run", side_effect=restore_image), mock.patch.object(
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

    def test_full_health_gate_includes_proxy_and_captured_topology(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dataclasses.replace(make_config(root), health_attempts=1)
            host = DockerHost(config)
            host.auth_replica_counts = {
                service: (5 if service == config.worker_service else 1)
                for service in config.auth_services
            }
            host.candidate_image_ids = {
                service: "sha256:" + str(index) * 64
                for index, service in enumerate(config.auth_services, start=1)
            }
            response = mock.MagicMock()
            response.__enter__.return_value.status = 200

            def manage(*arguments, **_kwargs):
                return "[X] applied\n" if arguments[0] == "showmigrations" else ""

            def compose(*arguments, **_kwargs):
                return "pong\n" if "celery" in arguments else ""

            with mock.patch.object(
                host, "_containers_healthy", return_value=True
            ) as healthy, mock.patch.object(
                host, "_require_live_images"
            ) as images, mock.patch.object(
                host, "_version_probe"
            ) as versions, mock.patch.object(
                host, "_manage_live", side_effect=manage
            ), mock.patch.object(
                host, "_compose", side_effect=compose
            ), mock.patch(
                "ops.deploy.docker_host.urllib.request.urlopen", return_value=response
            ):
                host.health(make_bundle(root))

            expected = {
                **host.auth_replica_counts,
                config.database_service: 1,
                config.redis_service: 1,
                config.proxy_service: 1,
            }
            self.assertEqual(healthy.call_args.args[0], expected)
            self.assertEqual(
                healthy.call_args.kwargs["zero_restart_services"],
                set(config.auth_services),
            )
            images.assert_called_once_with(host.candidate_image_ids)
            self.assertEqual(
                versions.call_args_list,
                [
                    mock.call(service, mock.ANY, live=True)
                    for service in config.auth_services
                ],
            )


if __name__ == "__main__":
    unittest.main()
