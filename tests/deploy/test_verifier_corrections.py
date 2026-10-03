
"""Actual loader and strict log regressions for legacy receiver recovery."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import DockerHost, LogScanError, _prove_discord_nickname_429
from tests.deploy.test_docker_host import (
    make_bundle, make_config, prepare_upstream_backup, simulated_root_owned_lstat,
    write_host_files,
)
from tests.deploy.verifier_fixtures import nickname_case


def retained_host(root):
    config = make_config(root)
    write_host_files(config)
    host = DockerHost(config)
    attempt = make_bundle(root).request.attempt_id
    host.backup_path = config.backup_dir / attempt
    host.backup_path.mkdir(parents=True)
    host.original_dockerfile = host.backup_path / "custom.dockerfile"
    host.original_dockerfile.write_bytes((config.app_dir / config.custom_dockerfile).read_bytes())
    host.original_local_settings = host.backup_path / "local.py"
    settings = config.app_dir / config.local_settings
    host.original_local_settings.write_bytes(settings.read_bytes())
    info = settings.stat()
    host.original_local_settings_metadata = (info.st_uid, info.st_gid, info.st_mode & 0o777)
    prepare_upstream_backup(host, config)
    host.platform_current_existed = host.deployment_current_existed = False
    host.static_root_path = "/var/www/static"
    host.static_manifest_path = "/var/www/static/staticfiles.json"
    host.static_manifest_backup = host.backup_path / "staticfiles.previous.json"
    host.static_manifest_backup.write_text('{"paths":{"x":"x"},"version":"1.1","hash":"abc"}')
    host.static_manifest_sha256 = hashlib.sha256(host.static_manifest_backup.read_bytes()).hexdigest()
    host.static_manifest_entries, host.static_manifest_metadata = 1, (1001, 1002, 0o640)
    host.static_assets_backup = host.backup_path / "static-assets.previous.tar"
    host.static_assets_backup.write_bytes(b"asset archive")
    host.static_assets_backup_sha256 = hashlib.sha256(b"asset archive").hexdigest()
    host.static_assets_sha256, host.static_assets_count, host.static_assets_bytes = "d" * 64, 1, 12
    host.previous_images = {
        role: ("sha256:" + f"{number+1:x}" * 64, f"example.invalid/{role}:old")
        for number, role in enumerate(config.auth_services)
    }
    host.previous_image_pins = {
        role: f"buh-platform-v2-rollback:{attempt}-{number}"
        for number, role in enumerate(config.auth_services)
    }
    host.auth_replica_counts = {role: 1 for role in config.auth_services}
    roles = [*config.auth_services, config.database_service, config.redis_service, config.proxy_service]
    host.restart_baselines = {f"{number+1:064x}": number for number in range(len(roles))}
    host.restart_baseline_services = {identity: role for identity, role in zip(host.restart_baselines, roles)}
    host.migration_started = True
    host._save_recovery_plan(attempt, "candidate-slot-start-1")
    return host, attempt


class RestartRehydrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original, self.attempt = retained_host(Path(self.temp.name))
        self.path = self.original.config.state_dir / "active-recovery.json"
        self.enterContext(simulated_root_owned_lstat(self.path))

    def health(self, host, *, restart_delta=0, identity_change=False, role_change=False, image_change=False):
        def discover(role, **kwargs):
            ids = [identity for identity, service in self.original.restart_baseline_services.items()
                   if service == role]
            if role_change and role == self.original.config.gunicorn_service:
                return [next(identity for identity, service in self.original.restart_baseline_services.items()
                             if service == self.original.config.beat_service)]
            return ["f" * 64] if identity_change else ids

        def command(args, **kwargs):
            if args[3] == "{{.Image}}":
                return "sha256:" + "f" * 64 if image_change else host.retained_restart_images[args[-1]]
            return f"running|{self.original.restart_baselines.get(args[-1], 0)+restart_delta}|healthy"

        required = {**host.auth_replica_counts, host.config.database_service: 1,
                    host.config.redis_service: 1, host.config.proxy_service: 1}
        with mock.patch.object(host, "_running_service_containers", side_effect=discover), mock.patch.object(
                host, "_run", side_effect=command):
            return host._containers_healthy(required, zero_restart_services=set())

    def legacy(self):
        plan = json.loads(self.path.read_text())
        plan.pop("restart_baselines")
        plan.pop("restart_baseline_services")
        self.path.write_text(json.dumps(plan))
        self.path.chmod(0o600)
        return plan

    def review(self, plan):
        rows = [{"container_id": identity, "service": role, "restart_count": self.original.restart_baselines[identity],
                 "image_id": self.original.previous_images.get(role, ("sha256:" + "c"*64, ""))[0]}
                for identity, role in self.original.restart_baseline_services.items()]
        return {"schema_version": 1, "attempt_id": self.attempt,
                "plan_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                "host_evidence_sha256": "a" * 64, "host_observed_at": "2026-10-03T19:05:00+00:00",
                "verification_since": "2026-10-03T18:56:59+00:00", "containers": rows, "discord_429": None}

    def save_review(self, review):
        path = self.original.backup_path / "VERIFIER-REVIEW.json"
        path.write_text(json.dumps(review))
        path.chmod(0o600)
        return path

    def test_legacy_empty_baseline_reproduces_the_false_failure(self):
        self.legacy()
        loaded, _ = DockerHost.load_incomplete_plan(self.original.config)
        self.assertEqual(loaded.restart_baselines, {})
        self.assertFalse(self.health(loaded))

    def test_new_plan_roundtrip_restores_counts_and_exact_service_identities(self):
        loaded, _ = DockerHost.load_incomplete_plan(self.original.config)
        self.assertEqual(loaded.restart_baselines, self.original.restart_baselines)
        self.assertEqual(loaded.restart_baseline_services, self.original.restart_baseline_services)
        self.assertTrue(self.health(loaded))
        self.assertFalse(self.health(loaded, restart_delta=1))
        self.assertFalse(self.health(loaded, identity_change=True))
        self.assertFalse(self.health(loaded, role_change=True))

    def test_legacy_review_reconstructs_baseline_and_rejects_restart_identity_image_changes(self):
        plan = self.legacy()
        review = self.save_review(self.review(plan))
        with simulated_root_owned_lstat(review):
            loaded, _ = DockerHost.load_incomplete_plan(self.original.config)
        self.assertTrue(self.health(loaded))
        for kw in ({"restart_delta": 1}, {"identity_change": True}, {"role_change": True}, {"image_change": True}):
            with self.subTest(kw=kw):
                self.assertFalse(self.health(loaded, **kw))
        self.assertEqual(self.path.read_text(), json.dumps(plan))

    def test_review_cannot_rebind_attempt_or_plan_or_topology_or_old_image(self):
        plan = self.legacy()
        original = self.review(plan)
        for field, value in (("attempt_id", "gh-999-1"), ("plan_sha256", "b"*64),
                             ("containers", original["containers"][:-1])):
            with self.subTest(field=field):
                altered = copy.deepcopy(original)
                altered[field] = value
                path = self.save_review(altered)
                with simulated_root_owned_lstat(path), self.assertRaises(DeploymentError):
                    DockerHost.load_incomplete_plan(self.original.config)
        altered = copy.deepcopy(original)
        altered["containers"][0]["image_id"] = "sha256:" + "f"*64
        path = self.save_review(altered)
        with simulated_root_owned_lstat(path), self.assertRaises(DeploymentError):
            DockerHost.load_incomplete_plan(self.original.config)

    @unittest.skipIf(os.name == "nt", "POSIX private review permissions")
    def test_world_readable_review_is_rejected(self):
        path = self.save_review(self.review(self.legacy()))
        path.chmod(0o644)
        with simulated_root_owned_lstat(path), self.assertRaises(DeploymentError):
            DockerHost.load_incomplete_plan(self.original.config)

    def test_wrong_service_count_bool_or_partial_saved_table_is_rejected(self):
        value = json.loads(self.path.read_text())
        for changes in ({"restart_baselines": {}}, {"restart_baseline_services": {}},
                        {"restart_baselines": {identity: True for identity in self.original.restart_baselines}}):
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps({**value, **changes}))
                self.path.chmod(0o600)
                with self.assertRaises(DeploymentError):
                    DockerHost.load_incomplete_plan(self.original.config)


class RecoveredDiscordTests(unittest.TestCase):
    def test_exact_correlated_short_retry_keeps_original_warning_and_hashes(self):
        lines, proof = nickname_case()
        hashes, warning = _prove_discord_nickname_429(lines, proof)
        self.assertEqual(hashes, set(proof["error_line_sha256"]))
        self.assertIn("recovered warning", warning)
        self.assertIn("429 -> HTTP 204", warning)

    def test_unmatched_repeated_permission_wrong_request_operation_and_delayed_cases_block(self):
        lines, proof = nickname_case()
        mutations = [
            lines[:-2], lines + [lines[3]],
            lines + ['[03/Oct/2026 19:00:47] ERROR [allianceauth.services.modules.discord.discord_client.client:679] '
                     '[Discord Service] permanent: Discord API returned error code 403: Missing Permissions code 50013'],
            [line.replace("sending PATCH", "sending PUT") for line in lines],
            [line.replace(proof["retry_request_id"], "c"*32) for line in lines],
            [line.replace("/members/222222222222222222", "/members/333333333333333333")
             if proof["retry_request_id"] in line else line for line in lines],
            [line.replace("19:00:46", "19:00:55") for line in lines],
            [line.replace("updated", "failed") for line in lines],
            [line.replace("update_nickname", "update_roles") for line in lines],
            lines + ['[03/Oct/2026 19:00:47] ERROR [synthetic.module:1] unknown provider failure'],
        ]
        for index, altered in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(DeploymentError):
                _prove_discord_nickname_429(altered, proof)

    def test_other_source_or_unreviewed_logs_are_never_allowed(self):
        lines, proof = nickname_case()
        with tempfile.TemporaryDirectory() as tmp:
            host = DockerHost(make_config(Path(tmp)))
            fatal = "\n".join(lines[3:5])
            with self.assertRaises(LogScanError):
                host._classify_log_batch([("allianceauth_worker_services", fatal)], set(), None)
            host.retained_verifier_review = {"discord_429": proof}
            host.recovered_discord_429_hashes, _ = _prove_discord_nickname_429(lines, proof)
            with self.assertRaises(LogScanError):
                host._classify_log_batch([("allianceauth_worker", fatal)], set(), None)
            self.assertEqual(host._classify_log_batch(
                [("allianceauth_worker_services", fatal)], set(), None), ())
            with self.assertRaises(LogScanError):
                host._classify_log_batch([("allianceauth_worker_services", fatal)], set(), None)

    def test_full_stream_read_error_and_changed_runtime_cannot_use_old_proof(self):
        lines, proof = nickname_case()
        with tempfile.TemporaryDirectory() as tmp:
            host = DockerHost(make_config(Path(tmp)))
            host.retained_verifier_review = {"discord_429": proof}
            host.restart_baseline_services = {proof["container_id"]: proof["service"]}
            with mock.patch.object(host, "_containers_healthy", return_value=False), self.assertRaises(DeploymentError):
                host._verified_discord_429()
            def broken():
                yield lines[0]
                raise DeploymentError("synthetic unreadable log")
            with mock.patch.object(host, "_containers_healthy", return_value=True), mock.patch.object(
                    host, "_stream_log_lines", return_value=broken()), self.assertRaises(DeploymentError):
                host._verified_discord_429()

    def test_saved_review_does_not_change_ordinary_deployment_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = DockerHost(make_config(Path(tmp)))
            self.assertEqual(host._verified_discord_429(), ())
            self.assertFalse(host.recovered_discord_429_hashes)
