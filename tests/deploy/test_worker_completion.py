"""Cold recovery with an exact immutable v0.6.2 archive and real plan parsing."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ops.deploy import worker_recovery as recovery
from ops.deploy.contracts import DeploymentError, extract_archive, load_validated_bundle
from ops.deploy.docker_host import DockerHost
from ops.deploy.request_archive import build_archive
from tests.deploy.test_celery_identity import workers
from tests.deploy.test_docker_host import (
    make_config,
    prepare_upstream_backup,
    simulated_root_owned_lstat,
    write_host_files,
)

ROOT = Path(__file__).resolve().parents[2]


def plan(root):
    config = make_config(root)
    write_host_files(config)
    host = DockerHost(config)
    host.backup_path = config.backup_dir / recovery.ATTEMPT
    host.backup_path.mkdir(parents=True)
    prepare_upstream_backup(host, config)
    for attr, live, filename in (
        (
            "original_dockerfile",
            config.app_dir / config.custom_dockerfile,
            "custom.dockerfile",
        ),
        ("original_local_settings", config.app_dir / config.local_settings, "local.py"),
        (
            "original_platform_current",
            host._platform_current_path(),
            "platform-CURRENT.json",
        ),
        (
            "original_deployment_current",
            config.state_dir / "current.json",
            "deployment-current.json",
        ),
    ):
        live.parent.mkdir(parents=True, exist_ok=True)
        if not live.exists():
            live.write_text("{}\n")
        saved = host.backup_path / filename
        saved.write_bytes(live.read_bytes())
        setattr(host, attr, saved)
        info = live.stat()
        if attr != "original_dockerfile":
            setattr(
                host, attr + "_metadata", (info.st_uid, info.st_gid, info.st_mode & 0o777)
            )
    host.platform_current_existed = host.deployment_current_existed = True
    host.static_root_path = "/var/www/static"
    host.static_manifest_path = "/var/www/static/staticfiles.json"
    host.static_manifest_backup = host.backup_path / "staticfiles.previous.json"
    host.static_manifest_backup.write_text('{"synthetic":"static"}')
    host.static_assets_backup = host.backup_path / "static-assets.previous.tar"
    host.static_assets_backup.write_bytes(b"synthetic archive")
    host.static_manifest_sha256 = hashlib.sha256(
        host.static_manifest_backup.read_bytes()
    ).hexdigest()
    host.static_assets_backup_sha256 = hashlib.sha256(
        host.static_assets_backup.read_bytes()
    ).hexdigest()
    host.static_manifest_entries = host.static_assets_count = 1
    host.static_manifest_metadata = (0, 0, 0o444)
    host.static_assets_sha256 = "d" * 64
    host.static_assets_bytes = 1
    host.previous_images = {
        s: ("sha256:" + "a" * 64, f"fixture/{s}:old") for s in config.auth_services
    }
    host.previous_image_pins = {s: f"fixture/{s}:retained" for s in config.auth_services}
    host.auth_replica_counts = {s: 1 for s in config.auth_services}
    host.auth_replica_counts[config.worker_service] = 5
    host.previous_web_slots = (f"buh-web-previous-{recovery.ATTEMPT}-1",)
    host.candidate_web_slots = (f"buh-web-candidate-{recovery.ATTEMPT}-1",)
    host.static_collection_started = host.traffic_switch_started = (
        host.migration_started
    ) = True
    host._save_recovery_plan(recovery.ATTEMPT, "candidate-slot-start-1")
    return config


class WorkerCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.checkout = Path(cls.tmp.name) / "immutable"
        for args in (
            ["clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(cls.checkout)],
            ["-C", str(cls.checkout), "config", "core.autocrlf", "false"],
            ["-C", str(cls.checkout), "checkout", "--quiet", recovery.RELEASE],
        ):
            subprocess.run(["git", *args], check=True, capture_output=True)
        output = Path(cls.tmp.name) / "unchanged-v062.tar.gz"
        cls.metadata = build_archive(
            root=cls.checkout,
            release_dir=cls.checkout / "releases/platform/v0.6.2",
            repository="Fifty5D/B-UH-AllianceAuth",
            release_commit=recovery.RELEASE,
            mode="deploy",
            workflow_run_id="34713182347",
            workflow_run_attempt=1,
            output=output,
        )
        cls.payload = Path(cls.tmp.name) / "payload"
        extract_archive(output.read_bytes(), cls.payload)

    def test_exact_immutable_cold_plan_verify_complete_and_failures(self):
        for failure in (None, "missing", "restart", "wrong-release", "missing-approval"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                config = plan(Path(tmp))
                active = config.state_dir / "active-recovery.json"
                saved_plan = active.read_bytes()
                with simulated_root_owned_lstat(active):
                    host, value = DockerHost.load_incomplete_plan(config)
                self.assertEqual(host.restart_baselines, {})  # actual cold-start defect
                bundle = load_validated_bundle(self.payload, config)  # never mocked
                self.assertEqual(bundle.request.release_commit, recovery.RELEASE)
                self.assertEqual(len(bundle.lineage_manifests), 3)
                if failure == "wrong-release":
                    from dataclasses import replace

                    bundle = replace(
                        bundle, request=replace(bundle.request, release_commit="a" * 40)
                    )

                def capture():
                    host.restart_baselines = {
                        "synthetic-retained-id": 1 if failure == "restart" else 0
                    }

                events = []
                with ExitStack() as stack:
                    stack.enter_context(
                        workers(
                            host, override={"ping": {}} if failure == "missing" else None
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(host, "_capture_live_images", side_effect=capture)
                    )
                    for method in (
                        "_capture_infrastructure_restart_baselines",
                        "_validate_recovery_host_baseline",
                        "_verify_previous_static_fallback",
                        "_verify_previous_static_manifest",
                        "_wait_for_slots",
                        "_verify_proxy_upstream_bytes",
                        "_proxy_exec",
                        "_wait_for_compose_services",
                        "_require_live_images",
                        "_redis_health",
                        "_internal_http_checks",
                        "_verify_static_asset",
                        "_public_smoke_checks",
                        "_activate_upstream",
                        "_restore_platform_current",
                        "_restore_deployment_current",
                        "_restore_static_manifest",
                        "_discard_previous_image_pins",
                    ):
                        stack.enter_context(mock.patch.object(host, method))
                    stack.enter_context(
                        mock.patch.object(
                            host, "_container_names_for_service", return_value=("old-web",)
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(
                            host, "_manage_live", return_value="[X] compatible"
                        )
                    )
                    stack.enter_context(
                        mock.patch.object(host, "_scan_new_logs", return_value=())
                    )
                    stack.enter_context(
                        mock.patch.object(
                            host, "_restore_candidate_configuration", return_value=True
                        )
                    )
                    replacements = stack.enter_context(
                        mock.patch.object(host, "_restore_service_set")
                    )
                    cleanup = stack.enter_context(
                        mock.patch.object(
                            host,
                            "_remove_web_slots",
                            side_effect=lambda *_: events.append("cleanup"),
                        )
                    )
                    if failure is None:
                        result = recovery.verify_restored(host, value, bundle)
                        self.assertEqual(result["cleanup"], "not-run")
                        self.assertEqual(len(result["workers"]), 6)
                        self.assertEqual(active.read_bytes(), saved_plan)
                        cleanup.assert_not_called()
                        result = recovery.complete(
                            host,
                            value,
                            bundle,
                            confirmation=f"COMPLETE ROLLBACK {recovery.ATTEMPT}",
                        )
                        self.assertEqual(result["cleanup"], "passed")
                        self.assertFalse(active.exists())
                        self.assertEqual(events, ["cleanup"])
                    else:
                        with self.assertRaises(DeploymentError):
                            recovery.complete(
                                host,
                                value,
                                bundle,
                                confirmation=""
                                if failure == "missing-approval"
                                else f"COMPLETE ROLLBACK {recovery.ATTEMPT}",
                            )
                        cleanup.assert_not_called()
                        self.assertEqual(active.read_bytes(), saved_plan)
                    replacements.assert_not_called()
                    self.assertEqual(
                        (host.backup_path / "RECOVERY.json").read_bytes(), saved_plan
                    )

    def test_ordinary_preflight_cannot_implicitly_resume_consumed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = plan(Path(tmp))
            with (
                simulated_root_owned_lstat(config.state_dir / "active-recovery.json"),
                mock.patch.object(DockerHost, "rollback") as rollback,
            ):
                with self.assertRaisesRegex(DeploymentError, "explicit worker-recovery"):
                    DockerHost.recover_incomplete_plan(config)
            rollback.assert_not_called()

    def test_pinned_evidence_change_or_install_approval_cannot_write(self):
        with (
            mock.patch.object(
                recovery, "read_file", return_value=({"sha256": "f" * 64}, b"")
            ),
            mock.patch.object(recovery, "_atomic_bytes") as write,
        ):
            with self.assertRaises(DeploymentError):
                recovery.verify_pins("a" * 64)
            with self.assertRaisesRegex(DeploymentError, "approval"):
                recovery.install("a" * 64, confirmation="")
        write.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux atomic receiver file boundary")
    def test_receiver_install_executes_atomic_file_change_and_failure_restore(self):
        # Use real file reads and atomic replacements, with only root/host identity
        # discovery redirected to this disposable fixture (never service operations).
        from ops.deploy import receiver

        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                library = root / "lib"
                target = library / "ops/deploy/docker_host.py"
                target.parent.mkdir(parents=True)
                old = b"# previous receiver\n"
                new = (ROOT / "ops/deploy/docker_host.py").read_bytes()
                target.write_bytes(old)
                original = hashlib.sha256(old).hexdigest()
                digest = hashlib.sha256(new).hexdigest()
                receipt = root / "WORKER-REPAIR.json"
                real_read = recovery.read_file

                def read(path, **kwargs):
                    if path == target:
                        raw = path.read_bytes()
                    elif path.name == "docker_host.py":
                        raw = new
                    elif path.name == "INSTALL.json":
                        raw = b'{"base":"unchanged"}'
                    else:
                        return real_read(path, **kwargs)
                    return {"sha256": hashlib.sha256(raw).hexdigest(), "mode": "0o644"}, raw

                backup = root / "backup"
                with (
                    mock.patch.object(recovery, "LIBRARY", library),
                    mock.patch.object(recovery, "ORIGINAL_RUNTIME", original),
                    mock.patch.object(recovery, "REPAIR_RECEIPT", receipt),
                    mock.patch.object(recovery, "read_file", side_effect=read),
                    mock.patch.object(
                        recovery,
                        "verify_pins",
                        side_effect=[
                            None,
                            DeploymentError("injected post-activation failure"),
                        ]
                        if fail
                        else [None, None],
                    ),
                    mock.patch.object(receiver, "_verify_root_owned_ancestors"),
                    mock.patch.object(recovery, "_repair_backup", return_value=backup),
                    mock.patch("os.fchown"),
                ):
                    if fail:
                        with self.assertRaises(DeploymentError):
                            recovery.install(
                                digest, confirmation=f"INSTALL WORKER CHECK {digest}"
                            )
                        self.assertEqual(target.read_bytes(), old)
                    else:
                        result = recovery.install(
                            digest, confirmation=f"INSTALL WORKER CHECK {digest}"
                        )
                        self.assertEqual(target.read_bytes(), new)
                        self.assertFalse(result["deployment_performed"])
                        self.assertTrue(receipt.is_file())
                    self.assertEqual((backup / "docker_host.py").read_bytes(), old)
