"""Optional Docker health metadata: synthetic parsing and real Docker templates."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from ops.deploy.collect_worker_restart_evidence import INSPECT_FORMAT, decode_inspection
from ops.deploy.docker_host import DockerHost
from ops.deploy.verifier_repair import verify_retained_container
from tests.deploy.test_docker_host import make_config


class OptionalHealthParserTests(unittest.TestCase):
    def setUp(self):
        self.values = ["a" * 64, "/synthetic-worker", "sha256:" + "b" * 64, 8,
                       {"Status": "running", "OOMKilled": False, "ExitCode": 0,
                        "StartedAt": "2026-10-03T00:00:00.123456789Z",
                        "FinishedAt": "2026-10-02T23:59:59.000000000Z"},
                       {"com.docker.compose.service": "allianceauth_worker"}, None,
                       {"Name": "always", "MaximumRetryCount": 0}, []]

    def read(self, values=None):
        return decode_inspection(json.dumps(self.values if values is None else values))

    def test_absent_health_is_explicitly_absent_and_keeps_identity_restart_and_oom_evidence(self):
        result = self.read()
        self.assertIsNone(result["health"])
        self.assertFalse(result["health_metadata_present"])
        self.assertEqual(result["container_id"], self.values[0])
        self.assertEqual(result["image_id"], self.values[2])
        self.assertEqual(result["restart_count"], 8)
        self.assertFalse(result["oom_killed"])
        self.assertEqual(result["started_at"], self.values[4]["StartedAt"])
        self.assertEqual(result["exit_code"], 0)

    def test_present_healthy_unhealthy_and_starting_remain_distinct(self):
        for health in ("healthy", "unhealthy", "starting"):
            with self.subTest(health=health):
                self.values[4]["Health"] = {"Status": health}
                result = self.read()
                self.assertTrue(result["health_metadata_present"])
                self.assertEqual(result["health"], health)

    def test_malformed_present_health_and_bad_required_state_fail_closed(self):
        for value in (None, False, [], {}, {"Status": "unknown"}, {"Status": None}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate = copy.deepcopy(self.values)
                candidate[4]["Health"] = value
                self.read(candidate)
        for index, key, value in ((3, None, True), (4, "OOMKilled", "false"), (4, "ExitCode", "0")):
            candidate = copy.deepcopy(self.values)
            if key is None:
                candidate[index] = value
            else:
                candidate[index][key] = value
            with self.subTest(index=index, key=key), self.assertRaises(ValueError):
                self.read(candidate)

    def test_health_output_and_labels_are_not_copied_into_report(self):
        self.values[4]["Health"] = {"Status": "healthy", "Log": [
            {"Start": "synthetic", "End": "synthetic", "ExitCode": 0,
             "Output": "Authorization: synthetic-secret\nTesting Mem: 42 / 500000000\nAll Ok\n"}]}
        self.values[5]["unexpected_private_label"] = "synthetic-private-value"
        rendered = json.dumps(self.read())
        self.assertNotIn("synthetic-secret", rendered)
        self.assertNotIn("synthetic-private-value", rendered)
        self.assertIn("Testing Mem: 42 / 500000000", rendered)


def live_templates():
    """Capture the actual templates used by both verification paths."""
    with tempfile.TemporaryDirectory() as temp:
        host = DockerHost(make_config(Path(temp)))
        identity, image = "a" * 64, "sha256:" + "b" * 64
        host.restart_baselines[identity] = 0
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            return "running|0|none"
        with mock.patch.object(host, "_running_service_containers", return_value=(identity,)), mock.patch.object(
                host, "_run", side_effect=run):
            assert host._containers_healthy({host.config.gunicorn_service: 1}, zero_restart_services=set())
        health = commands[-1][3]
        row = {"container_id": identity, "image_id": image, "restart_count": 0,
               "started_at": "2026-10-01T00:00:00.000000000Z"}
        response = f"{identity}|/synthetic-web|{image}|running|false|0|{row['started_at']}|none"
        recorder = mock.Mock(return_value=response)
        verify_retained_container(SimpleNamespace(_run=recorder), identity, row, name="synthetic-web")
        previous = recorder.call_args.args[0][3]
    return health, previous


class ActualDockerHealthTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = os.environ.get("BUH_REQUIRE_DOCKER_HEALTH_REGRESSION") == "1"
        if shutil.which("docker") is None:
            if required:
                raise AssertionError("Hosted Docker health regression requires Docker")
            raise unittest.SkipTest("Docker is unavailable on this local executor")
        version = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                 capture_output=True, timeout=15)
        if version.returncode:
            if required:
                raise AssertionError("Hosted Docker daemon is unavailable")
            raise unittest.SkipTest("Local Docker daemon is unavailable")
        env = Path("platform/testenv/env.example").read_text()
        cls.image = next(line.split("=", 1)[1] for line in env.splitlines()
                         if line.startswith("BUH_REDIS_IMAGE="))
        subprocess.run(["docker", "pull", cls.image], capture_output=True, check=True, timeout=90)
        cls.templates = live_templates()

    def create(self, health=None):
        name = "buh-health-parser-" + uuid.uuid4().hex
        args = ["docker", "create", "--network", "none", "--name", name]
        if health is None:
            args.append("--no-healthcheck")
        else:
            args.extend(["--health-cmd", health, "--health-interval", "1s", "--health-timeout", "1s",
                         "--health-retries", "1", "--health-start-period", "0s"])
        args.extend(["--entrypoint", "sh", self.image, "-c", "while :; do sleep 60; done"])
        subprocess.run(args, capture_output=True, check=True, timeout=20)
        self.addCleanup(lambda: subprocess.run(["docker", "rm", "--force", name],
                                              capture_output=True, timeout=20))
        return name

    def inspect(self, name, template):
        return subprocess.run(["docker", "inspect", "--format", template, name],
                              capture_output=True, text=True, timeout=15)

    def test_original_formatter_fails_on_real_absent_health_and_all_corrected_templates_pass(self):
        name = self.create()
        original = self.inspect(name, "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}")
        self.assertNotEqual(original.returncode, 0)
        self.assertIn("Health", original.stderr)
        for template in self.templates:
            with self.subTest(template=template):
                result = self.inspect(name, template)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip().split("|")[-1], "none")
        result = self.inspect(name, INSPECT_FORMAT)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = decode_inspection(result.stdout)
        self.assertIsNone(parsed["health"])
        self.assertFalse(parsed["health_metadata_present"])
        self.assertEqual(parsed["status"], "created")

    def test_real_healthy_and_unhealthy_objects_preserve_status(self):
        for command, expected in (("true", "healthy"), ("false", "unhealthy")):
            with self.subTest(expected=expected):
                name = self.create(command)
                subprocess.run(["docker", "start", name], capture_output=True, check=True, timeout=20)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    result = self.inspect(name, INSPECT_FORMAT)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    parsed = decode_inspection(result.stdout)
                    if parsed["health"] == expected:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("Synthetic Docker healthcheck did not reach its expected state")
                self.assertTrue(parsed["health_metadata_present"])
                for template in self.templates:
                    result = self.inspect(name, template)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip().split("|")[-1], expected)

