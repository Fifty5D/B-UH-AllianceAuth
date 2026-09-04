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
            self.assertEqual(block.count("sha256sum --check --strict"), 3)
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

    def test_only_recreated_auth_services_require_zero_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            host = DockerHost(config)

            def compose(*arguments, **_kwargs):
                return f"id-{arguments[2]}"

            def inspect(arguments, **_kwargs):
                container = arguments[-1]
                if container.endswith(config.database_service):
                    return "running|7\n"
                return "running|0\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=inspect
            ):
                self.assertTrue(
                    host._containers_healthy(
                        [config.gunicorn_service, config.database_service],
                        zero_restart_services=set(config.auth_services),
                    )
                )

            def restarted_auth(arguments, **_kwargs):
                container = arguments[-1]
                return "running|1\n" if container.endswith(config.gunicorn_service) else "running|0\n"

            with mock.patch.object(host, "_compose", side_effect=compose), mock.patch.object(
                host, "_run", side_effect=restarted_auth
            ):
                self.assertFalse(
                    host._containers_healthy(
                        [config.gunicorn_service],
                        zero_restart_services=set(config.auth_services),
                    )
                )


if __name__ == "__main__":
    unittest.main()
