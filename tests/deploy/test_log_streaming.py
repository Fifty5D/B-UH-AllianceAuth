"""Full-interval pipe regressions; no Docker/log-parser reconstruction in mocks."""

import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ops.deploy import docker_host as runtime
from ops.deploy.contracts import DeploymentError
from tests.fake_esi.server import Handler
from tests.deploy.test_docker_host import (
    RETAINED_DISCORD_OWNER_LOG,
    container_id,
    make_config,
    owner_log_environment,
)

# Only the first production prefix was available; headers and the second format
# are synthetic, grounded in the normal headers emitted by tests/fake_esi.
ESI_SUCCESS_METADATA = Path(__file__).with_name("fixtures") / "esi-success-metadata.log"


class LogStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.host = runtime.DockerHost(make_config(Path(self.temporary)))
        self.retained = RETAINED_DISCORD_OWNER_LOG.read_text(encoding="utf-8")

    def scan_program(self, program, *, owner=True, seconds=30):
        deadline = time.monotonic() + seconds
        lines = self.host._stream_log_lines(
            [sys.executable, "-u", "-c", program],
            deadline=deadline,
            context="Synthetic logs",
        )
        batches = self.host._log_batches(lines, owner_scoped=owner, deadline=deadline)
        findings = []
        try:
            for batch in batches:
                findings.extend(
                    self.host._classify_log_batch(
                        [("worker/fixture", batch)],
                        {"worker"} if owner else set(),
                        "rollback" if owner else None,
                    )
                )
        finally:
            batches.close()
            lines.close()
        return findings

    @staticmethod
    def output(text):
        return f"sys.stdout.write({text!r}); sys.stdout.flush()\n"

    @staticmethod
    def noise():
        # More than the unchanged 4 MiB ordinary-command cap, generated in the
        # child so neither the test nor reader accumulates the interval.
        return (
            "for _ in range(6000):\n"
            " sys.stdout.write('[2026-09-12 19:10:00,001: INFO/MainProcess] ' + 'x'*800 + '\\n')\n"
        )

    def test_clean_interval_exceeds_four_mib_with_bounded_memory(self):
        metadata = ESI_SUCCESS_METADATA.read_text(encoding="utf-8")
        repeats = runtime.MAX_COMMAND_OUTPUT // len(metadata.encode()) + 100
        for program in (
            self.noise(),
            f"for _ in range({repeats}):\n sys.stdout.write({metadata!r})\n",
        ):
            tracemalloc.start()
            try:
                self.assertEqual(self.scan_program("import sys\n" + program), [])
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, runtime.MAX_COMMAND_OUTPUT)
        self.assertEqual(runtime.MAX_COMMAND_OUTPUT, 4 * 1024 * 1024)

    def test_successful_fake_esi_http_metadata_is_not_a_severity(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/latest/markets/10000002/orders/",
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Esi-Error-Limit-Remain"], "100")
                    record = (
                        "[12/Sep/2026 20:09:00] DEBUG [memberaudit.core.esi_status:181] "
                        f"esi status response: {response.status} headers: {dict(response.headers)!r}\n"
                    )
                self.assertEqual(
                    self.scan_program("import sys\n" + self.output(record)), []
                )
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_metadata_tokens_do_not_hide_real_severity_or_exception_evidence(self):
        metadata = ESI_SUCCESS_METADATA.read_text(encoding="utf-8")
        benign = (
            metadata + "[12/Sep/2026 20:09:01] DEBUG [example.error.client:12] healthy\n"
        )
        for owner in (False, True):
            with self.subTest(owner=owner):
                self.assertEqual(
                    self.scan_program("import sys\n" + self.output(benign), owner=owner), []
                )
        for fatal in (
            metadata.replace(
                "] DEBUG [memberaudit", "] ERROR [memberaudit"
            ),  # actual severity with an otherwise successful response
            metadata.replace("DEBUG/MainProcess", "CRITICAL/MainProcess"),
            "DEBUG X-Esi-Error-Limit-Remain: ERROR backend failed\n",
            "DEBUG X-Esi-Error-Limit-Remain: 100; PermissionError: denied\n",
            "DEBUG ModuleNotFoundError: unavailable\n",
            "DEBUG Traceback (most recent call last):\n",
            "DEBUG ImportError: unavailable\n",
            "DEBUG SyntaxError: invalid\n",
            "DEBUG Worker failed to boot\n",
            "DEBUG ImproperlyConfigured: database\n",
            "DEBUG django.db.migrations.exceptions.InconsistentMigrationHistory\n",
            "DEBUG permission denied\n",
            "DEBUG 403 Forbidden (error code: 50013): Missing Permissions\n",
            "DEBUG restarting repeatedly\n",
            'DEBUG {"error": "unexpected failure"}\n',
            "[error] failed\n",
            "ERROR: failed\n",
        ):
            with self.subTest(fatal=fatal[:70]), self.assertRaises(DeploymentError):
                self.scan_program("import sys\n" + self.output(fatal), owner=False)

    def test_fatal_at_start_boundary_and_end_is_never_lost(self):
        noise = self.noise()
        fatal = self.output("ERROR token=never-retain unexpected failure\n")
        for before, after in (("", noise), (noise, noise), (noise, "")):
            with self.subTest(before=bool(before), after=bool(after)):
                with self.assertRaisesRegex(
                    DeploymentError, "fatal AllianceAuth"
                ) as caught:
                    self.scan_program("import sys\n" + before + fatal + after)
                self.assertNotIn("never-retain", str(caught.exception))
                self.assertLess(len(str(caught.exception)), 500)

    def test_owner_tracebacks_survive_pipe_and_record_batch_boundaries(self):
        # Three-byte child writes deliberately split header fields, unicode,
        # denial IDs and traceback lines. A later same-second duplicate must be
        # classified with its original even after megabytes of harmless records.
        program = (
            "import sys, os\n"
            + self.noise()
            + f"record = {self.retained.encode()!r}\n"
            + "for i in range(0,len(record),3): os.write(1, record[i:i+3])\n"
            + self.noise()
        )
        findings = self.scan_program(program)
        self.assertTrue(findings)
        self.assertIn("formats allianceauth,celery", findings[0])
        with self.assertRaisesRegex(DeploymentError, "fatal AllianceAuth"):
            self.scan_program(program + self.output(self.retained))

    def test_invalid_owner_and_other_fatal_records_remain_fatal(self):
        for text in (
            self.retained.replace("Fifty5D", "OrdinaryMember"),
            self.retained.replace("update_nickname", "update_groups"),
            self.retained.replace("318985508913020930", "318985508913020931"),
            self.retained.replace("1521272563626672198", "1521272563626672199"),
            self.retained + "CRITICAL unrelated failure\n",
            self.retained[: self.retained.rfind("Traceback")],
        ):
            with self.subTest(length=len(text)), self.assertRaises(DeploymentError):
                self.scan_program("import sys\n" + self.output(text))

    def test_exact_pipe_cuts_inside_header_traceback_and_owner_id(self):
        record = self.retained[self.retained.index("[07/Sep/") :]
        for cut in (
            record.index("WARNING") + 3,
            record.index("Traceback") + 4,
            record.index("318985508913020930") + 8,
        ):
            with self.subTest(cut=cut):
                padding = runtime.LOG_READ_BYTES - cut - len("INFO \n")
                program = (
                    "import sys\n"
                    + f"sys.stdout.write('INFO ' + 'x'*{padding} + '\\n')\n"
                    + self.output(record)
                )
                self.assertTrue(self.scan_program(program))

    def test_nonmonotonic_owner_incidents_are_not_split_or_forgiven(self):
        later = self.retained.replace("04:50:00", "04:51:00")
        self.assertTrue(
            self.scan_program("import sys\n" + self.output(later + self.retained))
        )
        with self.assertRaises(DeploymentError):
            self.scan_program("import sys\n" + self.output(later + self.retained + later))

    def test_failed_incomplete_and_timed_out_reads_never_pass(self):
        programs = (
            "import sys\nsys.stdout.write('INFO partial')",
            "import sys\n" + self.output(self.retained) + "sys.exit(7)",
            "import sys\n" + self.output("INFO complete\n") + "sys.exit(9)",
            "import time\ntime.sleep(10)",
        )
        for program in programs:
            with self.subTest(program=program[-20:]), self.assertRaises(DeploymentError):
                self.scan_program(program, seconds=0.3)

        process = mock.Mock(stdout=mock.Mock(), returncode=0)
        process.stdout.read.side_effect = OSError("token=read-error-secret")
        process.poll.return_value = 0
        with mock.patch.object(runtime.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(DeploymentError, "failed while reading") as caught:
                self.scan_program("")
        self.assertNotIn("read-error-secret", str(caught.exception))

    def test_incomplete_oversized_records_and_spool_failure_fail_closed(self):
        for text in (
            "INFO " + "x" * (runtime.LOG_READ_BYTES + 1) + "\n",
            self.retained + "continuation\n" * 130,
        ):
            with self.subTest(size=len(text)), self.assertRaises(DeploymentError):
                # A tiny generated child command avoids Windows command-line limits.
                if text.startswith("INFO"):
                    program = f"import sys\nsys.stdout.write('INFO '+'x'*{len(text)}+'\\n')"
                else:
                    program = "import sys\n" + self.output(text)
                self.scan_program(program)
        with mock.patch.object(
            runtime.sqlite3, "connect", side_effect=runtime.sqlite3.OperationalError
        ):
            with self.assertRaises(DeploymentError):
                self.scan_program("import sys\n" + self.output(self.retained))

    def test_scoped_candidate_cutover_and_rollback_use_full_interval_reader(self):
        real_popen = subprocess.Popen
        container = container_id(25)  # the services worker from the live topology
        for phase in ("candidate-health", "worker-cutover", "rollback"):
            with self.subTest(phase=phase):
                calls = []

                def popen(args, **kwargs):
                    calls.append(args)
                    program = "import sys\n"
                    if args[-1] == container:
                        program += self.noise() + self.output(
                            ESI_SUCCESS_METADATA.read_text(encoding="utf-8") + self.retained
                        )
                    return real_popen([sys.executable, "-u", "-c", program], **kwargs)

                with (
                    owner_log_environment(
                        self.host, rollback=phase == "rollback", streaming=True
                    ),
                    mock.patch.object(
                        runtime.DockerHost,
                        "compose_prefix",
                        new_callable=mock.PropertyMock,
                        return_value=["docker", "compose"],
                    ),
                    mock.patch.object(runtime.subprocess, "Popen", side_effect=popen),
                ):
                    findings = self.host._scan_new_logs(
                        self.host.config.auth_services, owner_transition_phase=phase
                    )
                self.assertEqual(len(findings), 1)
                self.assertTrue(
                    all(f"--since={self.host.log_since}" in args for args in calls)
                )
                self.assertFalse(
                    any(
                        any("--tail" in a or "--until" in a for a in args) for args in calls
                    )
                )

    def test_aggregate_scan_reaches_late_errors_and_other_services_after_failure(self):
        real_popen = subprocess.Popen
        calls = []

        def popen(args, **kwargs):
            calls.append(args[-1])
            program = "import sys\n"
            if args[-1] == self.host.config.auth_services[0]:
                program += self.output("ERROR first-task-failed\n")
                program += self.noise()
                program += self.output("ERROR late-task-failed\n")
            if args[-1] == self.host.config.database_service:
                program += self.output("CRITICAL database-failed password=secret-value\n")
            return real_popen([sys.executable, "-u", "-c", program], **kwargs)

        with (
            mock.patch.object(runtime.DockerHost, "compose_prefix", new_callable=mock.PropertyMock,
                              return_value=["docker", "compose"]),
            mock.patch.object(runtime.subprocess, "Popen", side_effect=popen),
            self.assertRaises(runtime.LogScanError) as failure,
        ):
            self.host._scan_new_logs(self.host.config.auth_services)
        findings = "\n".join(failure.exception.findings)
        for phrase in ("first-task-failed", "late-task-failed", "database-failed"):
            self.assertIn(phrase, findings)
        self.assertNotIn("secret-value", findings)
        self.assertIn(self.host.config.proxy_service, calls)
        self.assertTrue(failure.exception.scan_complete)

    def test_failed_stream_does_not_prevent_other_sources_and_never_passes(self):
        real_popen = subprocess.Popen

        def popen(args, **kwargs):
            program = "import sys\n"
            if args[-1] == self.host.config.database_service:
                program += "sys.exit(7)\n"
            else:
                program += self.output("ERROR independent-failure\n")
            return real_popen([sys.executable, "-u", "-c", program], **kwargs)

        with (
            mock.patch.object(runtime.DockerHost, "compose_prefix", new_callable=mock.PropertyMock,
                              return_value=["docker", "compose"]),
            mock.patch.object(runtime.subprocess, "Popen", side_effect=popen),
            self.assertRaises(runtime.LogScanError) as failure,
        ):
            self.host._scan_new_logs(self.host.config.auth_services)
        self.assertFalse(failure.exception.scan_complete)
        self.assertIn("independent-failure", str(failure.exception))

    def test_findings_report_is_bounded_and_declares_overflow(self):
        with self.assertRaises(runtime.LogScanError) as failure:
            self.host._classify_log_batch(
                [("worker", "\n".join(f"ERROR task-{i}" for i in range(100)))], set(), None
            )
        self.assertEqual(len(failure.exception.findings), runtime.MAX_RETAINED_LOG_FINDINGS)
        self.assertTrue(failure.exception.findings_truncated)

    def test_repeated_tasks_are_grouped_and_http_status_is_retained_without_payload(self):
        records = []
        for i in range(50):
            records.append(
                f"[2026-09-13 00:08:{i:02d},259: ERROR/MainProcess] "
                f"Task structures.tasks.update[{i:08x}-c5c3-435b-9179-e914b1dcf21c] raised unexpected: HTTPError()\n"
                "requests.exceptions.HTTPError: 403 Client Error: Forbidden for url: "
                "https://example.invalid/private?token=must-never-appear\n"
            )
        records.append("CRITICAL independent-database-failure\n")
        with self.assertRaises(runtime.LogScanError) as failure:
            self.host._classify_log_batch([("worker", "".join(records))], set(), None)
        self.assertEqual(len(failure.exception.findings), 3)
        self.assertFalse(failure.exception.findings_truncated)
        self.assertIn("HTTP=403", str(failure.exception))
        self.assertNotIn("example.invalid", str(failure.exception))
        self.assertNotIn("must-never-appear", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
