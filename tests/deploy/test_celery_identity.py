"""Worker identity failures reproduced at the actual shared health boundaries."""

from __future__ import annotations

import copy
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import DockerHost, celery_nodename
from ops.deploy.engine import DeploymentEngine, DeploymentJournal
from tests.deploy.test_docker_host import make_bundle, make_config

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/celery-v062-workers.json").read_text()
)


@contextmanager
def workers(host, *, rows=None, override=None):
    rows = copy.deepcopy(FIXTURE["workers"] if rows is None else rows)
    containers = {f"{index:064x}": row for index, row in enumerate(rows, 1)}
    host.auth_replica_counts = {service: 1 for service in host.config.auth_services}
    for service in {row["service"] for row in rows}:
        host.auth_replica_counts[service] = sum(row["service"] == service for row in rows)
    reports = {
        "ping": {row["node"]: {"ok": "pong"} for row in rows},
        "active_queues": {
            row["node"]: [{"name": queue} for queue in host.config.required_celery_queues]
            for row in rows
        },
        "registered": {
            row["node"]: list(host.config.required_celery_tasks) for row in rows
        },
    }
    if override:
        reports.update(override)

    def running(service, **_kwargs):
        return tuple(key for key, row in containers.items() if row["service"] == service)

    def inspect(arguments, **_kwargs):
        row = containers[arguments[-1]]
        return "\n".join(
            (
                row["hostname"],
                json.dumps(["celery", "-A", "myauth", "worker", "-n", row["pattern"]]),
                "null",
            )
        )

    def compose(*arguments, **_kwargs):
        if "inspect" not in arguments:
            return ""
        value = reports[arguments[arguments.index("inspect") + 1]]
        return value if isinstance(value, str) else json.dumps(value)

    with (
        mock.patch.object(host, "_running_service_containers", side_effect=running),
        mock.patch.object(host, "_run", side_effect=inspect),
        mock.patch.object(host, "_compose", side_effect=compose),
    ):
        yield reports


class CeleryIdentityTests(unittest.TestCase):
    def test_normalization_and_supported_placeholders(self):
        cases = {
            "worker_%n": "celery@worker_host",
            "worker_services_%n": "celery@worker_services_host",
            "named@%h": "named@host.example.invalid",
            "%n@%d": "host@example.invalid",
            "named@": "named@host.example.invalid",
            "@%h": "celery@host.example.invalid",
            "pool_%i%I@%n": "pool_0@host",
            "fixed@elsewhere.invalid": "fixed@elsewhere.invalid",
        }
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(celery_nodename(pattern, "host.example.invalid"), expected)
        for pattern in (
            "%p",
            "bad@host@other",
            "bad%q",
            "literal%%",
            "secret value",
            "@",
            "",
        ):
            # '@' itself is Celery's default and supported below.
            if pattern == "@":
                self.assertEqual(celery_nodename(pattern, "host"), "celery@host")
            else:
                with self.subTest(pattern=pattern), self.assertRaises(DeploymentError):
                    celery_nodename(pattern, "host")

    def test_six_retained_worker_names_are_not_bare_pattern_expansions(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = DockerHost(make_config(Path(tmp)))
            with workers(host):
                self.assertEqual(
                    host._expected_celery_nodes(),
                    frozenset(row["node"] for row in FIXTURE["workers"]),
                )
                host._celery_health()
                self.assertEqual(host.auth_replica_counts[host.config.worker_service], 5)

    def test_duplicate_normalized_identity_and_replica_drift_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = DockerHost(make_config(Path(tmp)))
            rows = copy.deepcopy(FIXTURE["workers"][:2])
            rows[1].update(hostname=rows[0]["hostname"], pattern="celery@worker_%n")
            with (
                workers(host, rows=rows),
                self.assertRaisesRegex(DeploymentError, "not unique"),
            ):
                host._expected_celery_nodes()
            with workers(host), self.assertRaisesRegex(DeploymentError, "replica count"):
                host.auth_replica_counts[host.config.worker_service] = 4
                host._expected_celery_nodes()

    def test_missing_unexpected_and_malformed_replies_fail_without_payloads(self):
        nodes = [row["node"] for row in FIXTURE["workers"]]
        secret = "never-publish-task-payload"
        cases = [
            {node: {"ok": "pong"} for node in nodes[:-1]},
            {
                **{node: {"ok": "pong"} for node in nodes},
                "unexpected@host": {"secret": secret},
            },
            {node: {"ok": "pong", "task": secret} for node in nodes},
            {node: {"ok": "not-pong"} for node in nodes},
            {node: [secret] for node in nodes},
            "not JSON " + secret,
            "{broken " + secret,
            '{"celery@one":{"ok":"pong"},"celery@one":{"ok":"pong"}}',
            '{"celery@one":{"ok":"pong","ok":"pong"}}',
            {secret + " / token=secret": {"task": secret}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            host = DockerHost(make_config(Path(tmp)))
            for value in cases:
                with (
                    self.subTest(value_type=type(value).__name__),
                    workers(host, override={"ping": value}),
                    self.assertRaises(DeploymentError) as caught,
                ):
                    host._celery_health()
                self.assertNotIn(secret, str(caught.exception))
                self.assertLessEqual(len(str(caught.exception)), 500)
            with (
                workers(host, override={"ping": cases[0]}),
                self.assertRaises(DeploymentError) as caught,
            ):
                host._celery_health()
            for field in ("expected=", "observed=", "missing=", "unexpected="):
                self.assertIn(field, str(caught.exception))
            self.assertIn(nodes[-1], str(caught.exception))

    def test_baseline_failure_precedes_preparation_backup_and_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = DockerHost(make_config(root))
            bundle = make_bundle(root)
            journal = DeploymentJournal(host.config.state_dir, bundle)
            with (
                workers(host, override={"ping": {}}),
                mock.patch.object(host, "validate"),
                mock.patch.object(host, "_capture_live_images"),
                mock.patch.object(host, "backup") as backup,
                mock.patch.object(host, "migrate") as migrate,
            ):
                with self.assertRaises(DeploymentError):
                    DeploymentEngine(host, journal).run(bundle)
            backup.assert_not_called()
            migrate.assert_not_called()
            self.assertIsNone(host.original_dockerfile)
            self.assertIsNone(host.backup_path)
            self.assertFalse(host.migration_started)
            self.assertEqual(journal.record["state"], "validated")

    def test_candidate_cutover_and_actual_rollback_share_corrected_health(self):
        for phase in ("candidate", "cutover", "rollback"):
            for healthy in (True, False):
                with (
                    self.subTest(phase=phase, healthy=healthy),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    host = DockerHost(make_config(root))
                    bundle = make_bundle(root)
                    host.candidate_web_slots = ("buh-web-candidate-gh-1234-1-1",)
                    host.previous_web_slots = ("buh-web-previous-gh-1234-1-1",)
                    host.candidate_image_ids = {
                        s: "sha256:" + "b" * 64 for s in host.config.auth_services
                    }
                    host.previous_images = {
                        s: ("sha256:" + "a" * 64, f"image-{s}:old")
                        for s in host.config.auth_services
                    }
                    with ExitStack() as stack:
                        stack.enter_context(
                            workers(host, override=None if healthy else {"ping": {}})
                        )
                        for method in (
                            "_start_web_slots",
                            "_verify_previous_static_fallback",
                            "_candidate_runtime_checks",
                            "_redis_health",
                            "_retain_transition_log_findings",
                            "_wait_for_compose_services",
                            "_require_live_images",
                            "_version_probe",
                            "_internal_http_checks",
                            "_verify_static_asset",
                            "_public_smoke_checks",
                            "_activate_upstream",
                            "_restore_service_set",
                            "complete_recovery_plan",
                        ):
                            stack.enter_context(mock.patch.object(host, method))
                        stack.enter_context(
                            mock.patch.object(
                                host, "_containers_healthy", return_value=True
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(host, "_scan_new_logs", return_value=())
                        )
                        stack.enter_context(
                            mock.patch.object(
                                host, "_manage_live", return_value="[X] checked"
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                host,
                                "_container_names_for_service",
                                return_value=("original-web",),
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                host, "_restore_candidate_configuration", return_value=True
                            )
                        )
                        cleanup = stack.enter_context(
                            mock.patch.object(host, "_remove_web_slots")
                        )
                        if phase == "candidate":

                            def action():
                                host.candidate_health(bundle)
                        elif phase == "cutover":

                            def action():
                                host.replace_workers(bundle)
                        else:
                            host.original_dockerfile = root / "retained-dockerfile"
                            host.migration_started = host.traffic_switch_started = True

                            def action():
                                host.rollback(bundle, "migrated")

                        if healthy:
                            action()
                        else:
                            with self.assertRaisesRegex(
                                DeploymentError, "every expected worker"
                            ):
                                action()
                            cleanup.assert_not_called()
                        if phase == "rollback" and healthy:
                            cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
