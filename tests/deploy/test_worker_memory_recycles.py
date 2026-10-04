"""Every post-baseline recycle needs independent, complete causal evidence."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import zlib

from ops.deploy import docker_host as adapter
from ops.deploy import verifier_repair as repair
from ops.deploy.contracts import DeploymentError
from tests.deploy.test_docker_host import simulated_root_owned_lstat
from tests.deploy import test_verifier_corrections as rehydration
from tests.deploy.test_verifier_corrections import retained_host


IDENTITY = "a" * 64
NAME = "synthetic-worker-4"
BOOT = "b" * 32
OBSERVED = "2026-10-03T21:45:00+00:00"


def stamp(point):
    return point.isoformat(timespec="microseconds").replace("+00:00", "Z")


def causal_case(number=9, hour=23):
    start = datetime(2026, 10, 3, tzinfo=timezone.utc) + timedelta(hours=hour, minutes=12, seconds=46)
    rows = []
    for offset in (-180, -120, -60, 0):
        point = start + timedelta(seconds=offset)
        for command, fraction in (("exec_create: /memory_check.sh 500000000", 0),
                                  ("exec_start: /memory_check.sh 500000000", 0.001),
                                  ("exec_die", 0.04)):
            rows.append({"at": stamp(point + timedelta(seconds=fraction)), "service": NAME, "message": command})
    rows.extend({"at": stamp(start + timedelta(seconds=seconds)), "service": NAME, "message": message}
                for seconds, message in ((-59.95, "health_status: unhealthy"), (2.05, "die"),
                                         (2.20, "start"), (7.20, "health_status: healthy")))
    rows.sort(key=lambda row: row["at"])
    event = {"container_id": IDENTITY, "exit_code": 0, "exited_at": stamp(start + timedelta(seconds=2)),
             "manual_restart": False, "restart_count": number, "restart_policy": "always 0",
             "at": stamp(start + timedelta(seconds=2.03)), "message_sha256": "c" * 64}
    app = [{"at": stamp(start + timedelta(seconds=0.032)), "kind": "warm", "sha256": "d" * 64}]
    return event, rows, app, []


class CausalProofTests(unittest.TestCase):
    def prove(self, case):
        return adapter._prove_worker_recycle(*case, container=IDENTITY, name=NAME, observed=OBSERVED)

    def test_configured_threshold_graceful_exit_and_automatic_healthy_restart_are_retained(self):
        proof = self.prove(causal_case())
        self.assertEqual(proof["restart_count"], 9)
        self.assertEqual(proof["restart_record_sha256"], "c" * 64)
        self.assertEqual(proof["exit_code"], 0)
        self.assertFalse(proof["manual_restart"])
        self.assertIn("third-failure", proof["threshold_evidence"])
        self.assertEqual(len(proof["evidence_sha256"]), 64)

    def test_warm_exit_alone_missing_threshold_manual_crash_and_near_misses_block(self):
        mutations = []
        for key, value in (("container_id", "f" * 64), ("exit_code", 1), ("exit_code", True),
                           ("manual_restart", True), ("restart_policy", "on-failure 0")):
            case = copy.deepcopy(causal_case())
            case[0][key] = value
            mutations.append(case)
        for message in ("health_status: unhealthy", "die", "start", "health_status: healthy"):
            case = copy.deepcopy(causal_case())
            case[1][:] = [row for row in case[1] if row["message"] != message]
            mutations.append(case)
        for replacement in ("restart", "kill", "oom", "exec_start: /bin/sh -c kill -TERM 1"):
            case = copy.deepcopy(causal_case())
            case[1].append({"at": case[0]["at"], "service": NAME, "message": replacement})
            mutations.append(case)
        case = copy.deepcopy(causal_case())
        case[1][:] = [row for row in case[1] if row["message"] != "exec_start: /memory_check.sh 500000000"]
        mutations.append(case)
        case = copy.deepcopy(causal_case())
        case[2][0]["at"] = "2026-10-03T23:12:47Z"
        mutations.append(case)
        for text in ("Starting Docker", "sudo docker compose restart worker", "kill -TERM 1",
                     "Out of memory: Killed process", "daemon shutdown"):
            case = copy.deepcopy(causal_case())
            case[3].append({"at": case[0]["at"], "message": text})
            mutations.append(case)
        case = copy.deepcopy(causal_case())
        case[2].append({"at": case[0]["at"], "kind": "conflicting", "sha256": "e" * 64})
        mutations.append(case)
        for index, case in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(DeploymentError):
                self.prove(case)


class RuntimeRecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host, self.attempt = retained_host(Path(self.temp.name))
        self.container = next(identity for identity, service in self.host.restart_baseline_services.items()
                              if service == self.host.config.worker_service)
        self.original_counts = dict(self.host.restart_baselines)
        self.baseline_count = self.original_counts[self.container]
        self.host.retained_restart_images = {self.container: self.host.previous_images[self.host.config.worker_service][0]}
        self.host.retained_restart_started_at = {self.container: "2026-10-03T20:00:00.123456789Z"}
        self.host.retained_verifier_review = {"host_observed_at": OBSERVED}
        self.policy = {"schema_version": 1, "reviewed_evidence_sha256": "a" * 64,
                       "script_sha256": adapter.WORKER_MEMORY_SCRIPT_SHA256, "host_boot_id": BOOT,
                       "diagnostics_store_id": "e" * 32, "workers": {self.container: NAME},
                       "daemons": {unit: {"MainPID": "915", "ExecMainStartTimestampMonotonic": "6116073"}
                                   for unit in ("docker.service", "containerd.service")}}
        self.host.worker_recycle_policy = self.policy
        self.cases = []
        self.runtime = {"container_id": self.container, "image_id": self.host.retained_restart_images[self.container],
                        "restart_count": self.baseline_count,
                        "started_at": self.host.retained_restart_started_at[self.container],
                        "finished_at": "2026-10-03T19:59:58Z"}
        script = (Path(__file__).parent / "fixtures/worker-memory-check-installed.sh").read_bytes()
        self.assertEqual(hashlib.sha256(script).hexdigest(), adapter.WORKER_MEMORY_SCRIPT_SHA256)
        self.enterContext(mock.patch.object(self.host, "_worker_recycle_private_bytes", return_value=script))
        self.enterContext(mock.patch.object(Path, "read_text", return_value=BOOT))
        self.enterContext(mock.patch.object(self.host, "_worker_recycle_actions"))
        self.enterContext(mock.patch.object(self.host, "_worker_recycle_store", side_effect=self.store))
        self.enterContext(mock.patch.object(self.host, "_worker_recycle_application", side_effect=self.application))
        self.enterContext(mock.patch.object(self.host, "_worker_recycle_journal", side_effect=self.journal))
        self.enterContext(mock.patch.object(self.host, "_run", side_effect=self.command))
        self.enterContext(mock.patch.object(self.host, "_running_service_containers", return_value=(self.container,)))

    def advance(self, *, proven=True):
        number = self.baseline_count + len(self.cases) + 1
        # Each later recycle is independently proved, at least an hour apart.
        case = causal_case(number, 21 + len(self.cases) + 1)
        case[0]["container_id"] = self.container
        if not proven:
            case[2].clear()
        self.cases.append(case)
        started = stamp(adapter._recycle_time(case[0]["exited_at"]) + timedelta(seconds=0.1))
        self.runtime = {"container_id": self.container, "image_id": self.host.retained_restart_images[self.container],
                        "restart_count": number, "started_at": started, "finished_at": case[0]["exited_at"]}

    def case(self, start):
        return next(case for case in self.cases
                    if adapter._recycle_time(start) < adapter._recycle_time(case[0]["at"])
                    < adapter._recycle_time(start) + timedelta(minutes=6))

    def store(self, name, start, end):
        return self.case(start)[1]

    def application(self, container, start, end):
        return self.case(start)[2]

    def journal(self, since, until, *, daemons_only=False):
        if not daemons_only:
            return self.case(since)[3]
        return [{"at": case[0]["at"], "message": (
            'msg="restarting container" container=' + self.container + ' exitCode=0 exitedAt="'
            + case[0]["exited_at"] + '" manualRestart=false restartCount=' + str(case[0]["restart_count"])
            + ' restartPolicy="{always 0}"')} for case in self.cases]

    def command(self, args, **kwargs):
        if args[0] == "systemctl":
            return "MainPID=915\nExecMainStartTimestampMonotonic=6116073\nActiveState=active\nSubState=running\n"
        if args[3] == adapter.WORKER_RECYCLE_INSPECT:
            value = [self.container, "/" + NAME, self.runtime["image_id"], self.runtime["restart_count"],
                     {"Status": "running", "OOMKilled": False, "ExitCode": 0, "Error": "",
                      "StartedAt": self.runtime["started_at"], "FinishedAt": self.runtime["finished_at"],
                      "Health": {"Status": "healthy"}},
                     {"com.docker.compose.service": self.host.config.worker_service}, adapter.WORKER_MEMORY_CHECK,
                     {"Name": "always", "MaximumRetryCount": 0},
                     [{"Source": str(adapter.WORKER_MEMORY_SCRIPT), "Destination": "/memory_check.sh", "RW": True}]]
            return json.dumps(value)
        if args[3].startswith("{{.Image}}|"):
            return self.runtime["image_id"] + "|" + self.runtime["started_at"] + "|false"
        return "running|" + str(self.runtime["restart_count"]) + "|healthy"

    def healthy(self):
        return self.host._containers_healthy({self.host.config.worker_service: 1}, zero_restart_services=set())

    def test_baseline_then_two_independently_proved_recycles_then_unexplained_restart(self):
        self.assertTrue(self.healthy())
        self.advance()
        self.assertTrue(self.healthy())
        self.assertEqual(len(self.host.accepted_worker_recycles[self.container]), 1)
        self.advance()
        self.assertTrue(self.healthy())
        self.assertEqual(len(self.host.accepted_worker_recycles[self.container]), 2)
        self.advance(proven=False)
        self.assertFalse(self.healthy())
        self.assertEqual(len(self.host.accepted_worker_recycles[self.container]), 2)
        self.assertEqual(self.host.restart_baselines, self.original_counts)

    def test_a_missing_intermediate_restart_is_not_replaced_by_a_new_baseline(self):
        self.advance()
        self.advance()
        with mock.patch.object(self.host, "_worker_recycle_journal", return_value=self.journal(OBSERVED, "", daemons_only=True)[1:]):
            self.assertFalse(self.healthy())
        self.assertEqual(self.host.restart_baselines, self.original_counts)

    def test_identity_image_oom_missing_health_manual_operation_and_config_change_still_block(self):
        self.advance()
        real = self.command
        for index, mutation in (
                (0, "f" * 64), (2, "sha256:" + "f" * 64), (5, {"com.docker.compose.service": "other"}),
                (6, {**adapter.WORKER_MEMORY_CHECK, "Test": ["CMD", "/memory_check.sh", "900000000"]}),
                (7, {"Name": "no", "MaximumRetryCount": 0}), (8, [])):
            def changed(args, **kwargs):
                raw = real(args, **kwargs)
                if args[0] == "docker" and args[3] == adapter.WORKER_RECYCLE_INSPECT:
                    value = json.loads(raw)
                    value[index] = mutation
                    return json.dumps(value)
                return raw
            with self.subTest(index=index), mock.patch.object(self.host, "_run", side_effect=changed):
                self.assertFalse(self.healthy())
        for field, value in (("OOMKilled", True), ("ExitCode", 137), ("Error", "bad"), ("Health", None)):
            def changed(args, **kwargs):
                raw = real(args, **kwargs)
                if args[0] == "docker" and args[3] == adapter.WORKER_RECYCLE_INSPECT:
                    result = json.loads(raw)
                    result[4][field] = value
                    return json.dumps(result)
                return raw
            with self.subTest(field=field), mock.patch.object(self.host, "_run", side_effect=changed):
                self.assertFalse(self.healthy())

    def test_nonworker_restart_and_new_deployment_zero_restart_rules_remain_strict(self):
        self.advance()
        self.host.restart_baseline_services[self.container] = self.host.config.beat_service
        self.assertFalse(self.host._containers_healthy({self.host.config.beat_service: 1}, zero_restart_services=set()))
        self.host.restart_baseline_services[self.container] = self.host.config.worker_service
        self.assertFalse(self.host._containers_healthy({self.host.config.worker_service: 1},
                                                      zero_restart_services={self.host.config.worker_service}))

    def test_staged_previous_routing_probe_requires_the_same_proof_not_a_current_count_rebase(self):
        self.advance()
        row = {"container_id": self.container, "image_id": self.runtime["image_id"],
               "restart_count": self.baseline_count, "started_at": self.host.retained_restart_started_at[self.container]}
        output = "|".join([self.container, "/" + NAME, self.runtime["image_id"], "running", "false",
                           str(self.runtime["restart_count"]), self.runtime["started_at"], "healthy"])
        original = self.command
        def transport(args, **kwargs):
            if args[0] == "docker" and args[3].startswith("{{.Id}}|"):
                return output
            return original(args, **kwargs)
        with mock.patch.object(self.host, "_run", side_effect=transport):
            repair.verify_retained_container(self.host, self.container, row)
        self.assertEqual(self.host.restart_baselines, self.original_counts)

    def test_changed_boot_or_daemon_and_unreadable_evidence_do_not_pass(self):
        self.advance()
        with mock.patch.object(Path, "read_text", return_value="f" * 32):
            self.assertFalse(self.healthy())
        with mock.patch.object(self.host, "_worker_recycle_store", side_effect=DeploymentError("missing evidence")):
            self.assertFalse(self.healthy())
        with mock.patch.object(self.host, "_worker_recycle_actions", side_effect=DeploymentError("manual host action")):
            self.assertFalse(self.healthy())
        command = self.command
        def changed_daemon(args, **kwargs):
            if args[0] == "systemctl":
                return "MainPID=916\nExecMainStartTimestampMonotonic=6116073\nActiveState=active\nSubState=running\n"
            return command(args, **kwargs)
        with mock.patch.object(self.host, "_run", side_effect=changed_daemon):
            self.assertFalse(self.healthy())


class StoreReadOnlyTests(unittest.TestCase):
    def test_capture_lag_gaps_missing_store_and_missing_rows_are_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host, _ = retained_host(root)
            path = root / "history.sqlite3"
            with sqlite3.connect(path) as db:
                db.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);"
                                 "CREATE TABLE logs(id INTEGER PRIMARY KEY,at TEXT,service TEXT,source TEXT,message BLOB);"
                                 "CREATE TABLE gaps(at TEXT,source TEXT,reason TEXT);")
                db.executemany("INSERT INTO metadata VALUES(?,?)", [("store_id", "e" * 32),
                               ("source:docker-event:last_success", "2026-10-04T06:00:00Z")])
                db.execute("INSERT INTO logs VALUES(1,?,?,?,?)", ("2026-10-03T23:12:46Z", NAME,
                           "docker-event", zlib.compress(b"exec_start: /memory_check.sh 500000000")))
            path.chmod(0o600)
            host.worker_recycle_policy = {"diagnostics_store_id": "e" * 32}
            before = path.read_bytes()
            with mock.patch.object(adapter, "WORKER_RESTART_STORE", path), simulated_root_owned_lstat(path):
                records = host._worker_recycle_store(NAME, "2026-10-03T23:10:00Z", "2026-10-03T23:14:00Z")
                self.assertEqual(len(records), 1)
                with self.assertRaises(DeploymentError):
                    host._worker_recycle_store(NAME, "2026-10-04T05:59:00Z", "2026-10-04T06:01:00Z")
                host.worker_recycle_policy["diagnostics_store_id"] = "f" * 32
                with self.assertRaises(DeploymentError):
                    host._worker_recycle_store(NAME, "2026-10-03T23:10:00Z", "2026-10-03T23:14:00Z")
            self.assertEqual(path.read_bytes(), before)


class ReviewPolicyTests(unittest.TestCase):
    setUp = rehydration.RestartRehydrationTests.setUp
    legacy = rehydration.RestartRehydrationTests.legacy
    review = rehydration.RestartRehydrationTests.review
    save_review = rehydration.RestartRehydrationTests.save_review
    def test_schema_two_reloads_reviewed_original_baseline_without_learning_live_counts(self):
        plan = self.legacy()
        review = self.review(plan)
        policy = {"schema_version": 1, "reviewed_evidence_sha256": "a" * 64,
                  "script_sha256": adapter.WORKER_MEMORY_SCRIPT_SHA256, "host_boot_id": BOOT,
                  "diagnostics_store_id": "e" * 32,
                  "workers": {row["container_id"]: NAME for row in review["containers"]
                              if row["service"] == self.original.config.worker_service},
                  "daemons": {unit: {"MainPID": "915", "ExecMainStartTimestampMonotonic": "6116073"}
                              for unit in ("docker.service", "containerd.service")}}
        review.update(schema_version=2, worker_memory_recycles=policy)
        path = self.save_review(review)
        with simulated_root_owned_lstat(path):
            loaded, _ = adapter.DockerHost.load_incomplete_plan(self.original.config)
        self.assertEqual(loaded.restart_baselines, self.original.restart_baselines)
        self.assertEqual(loaded.worker_recycle_policy, policy)
        policy["workers"][review["containers"][0]["container_id"]] = "web"
        path = self.save_review(review)
        with simulated_root_owned_lstat(path), self.assertRaises(DeploymentError):
            adapter.DockerHost.load_incomplete_plan(self.original.config)
