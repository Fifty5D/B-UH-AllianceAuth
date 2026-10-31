"""Portable host-wrapper safety tests without Docker, Django, or production data."""

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "ops" / "recovery"
SOURCE = ROOT / "apps" / "memberaudit-autoreg" / "src" / "buh_memberaudit_autoreg"


class HostWrapperTests(unittest.TestCase):
    def setUp(self):
        fake_fcntl = ModuleType("fcntl")
        fake_fcntl.LOCK_EX = 1
        fake_fcntl.LOCK_NB = 2
        fake_fcntl.flock = MagicMock()
        budget_spec = importlib.util.spec_from_file_location("retry_budget", TOOLS / "retry_budget.py")
        budget = importlib.util.module_from_spec(budget_spec)
        budget_spec.loader.exec_module(budget)
        self.modules = patch.dict(sys.modules, {"fcntl": fake_fcntl, "retry_budget": budget})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("host_wrapper_under_test", TOOLS / "host_recover_memberaudit.py")
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = Path(self.temp.name)
        self.nofollow = patch.object(self.host.os, "O_NOFOLLOW", 0, create=True)
        self.nofollow.start()
        self.addCleanup(self.nofollow.stop)
        self.plan = {
            "character_pk": 7, "sections": ["skills"],
            "authorization_ids": [13], "status": "planned",
        }
        self.before = {
            "preview": {
                "results": [self.plan],
                "summary": {"actionable_characters": 1, "actionable_sections": 1},
            },
            "snapshot": {"characters": [{"character_pk": 7, "sections": ["skills"]}]},
            "results": [],
        }

    def run_main(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            result = self.host.main(["--snapshot-dir", str(self.evidence), *args])
        return result, output.getvalue()

    def test_snapshot_and_reserved_state_are_written_before_apply(self):
        events = []

        def container_run(*args, **kwargs):
            if kwargs.get("apply"):
                events.append(("apply", kwargs["selection"]))
                return {"results": [{"character_pk": 7, "status": "queued"}]}
            events.append(("preview", None))
            return self.before

        def private_json(path, data, **kwargs):
            events.append((Path(path).name, data))

        with patch.object(self.host, "container_run", side_effect=container_run), \
             patch.object(self.host, "private_json", side_effect=private_json), \
             patch.object(self.host.time, "time", return_value=10000):
            result, _ = self.run_main("--apply", "--periodic")
        self.assertEqual(result, 0)
        self.assertEqual(events[0][0], "preview")
        self.assertTrue(events[1][0].endswith(".before.json"))
        self.assertEqual(events[1][1], self.before["snapshot"])
        self.assertEqual(events[2][0], "retry-state.json")
        self.assertEqual(events[2][1]["7:skills"]["attempts"], 1)
        self.assertEqual(events[2][1]["7:skills"]["last_attempt"], 10000)
        self.assertEqual(events[3], ("apply", [self.plan]))
        self.assertTrue(events[4][0].endswith(".result.json"))

    def test_preview_never_applies_validates_or_reserves_budget(self):
        with patch.object(self.host, "container_run", return_value=self.before) as run, \
             patch.object(self.host, "private_json") as write, \
             patch.object(self.host, "choose_retries") as choose:
            result, output = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), self.before["preview"])
        run.assert_called_once()
        self.assertNotIn("apply", run.call_args.kwargs)
        write.assert_not_called()
        choose.assert_not_called()

    def test_expected_count_mismatch_blocks_apply(self):
        with patch.object(self.host, "container_run", return_value=self.before) as run, \
             patch.object(self.host, "private_json") as write:
            with self.assertRaisesRegex(RuntimeError, "actionable_sections changed"):
                self.run_main("--apply", "--expect-sections", "21")
        run.assert_called_once()
        write.assert_not_called()

    def test_exhausted_periodic_budget_never_applies(self):
        prior = {"7:skills": {"attempts": 3, "last_attempt": 1, "authorization_ids": [13]}}
        (self.evidence / "retry-state.json").write_text(json.dumps(prior), encoding="utf-8")
        with patch.object(self.host, "container_run", return_value=self.before) as run, \
             patch.object(self.host, "private_json") as write, \
             patch.object(self.host.time, "time", return_value=10000):
            result, output = self.run_main("--apply", "--periodic")
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output)["reason"], "no_budgeted_retries")
        run.assert_called_once()
        write.assert_called_once_with(self.evidence / "retry-state.json", prior, replace=True)

    def test_apply_failure_leaves_attempt_reserved_before_exception(self):
        writes = []

        def container_run(*args, **kwargs):
            if kwargs.get("apply"):
                self.assertEqual(len(writes), 2)
                self.assertEqual(writes[1][1]["7:skills"]["attempts"], 1)
                raise RuntimeError("Synthetic Auth process failed")
            return self.before

        with patch.object(self.host, "container_run", side_effect=container_run), \
             patch.object(self.host, "private_json", side_effect=lambda p, d, **kw: writes.append((p, d))):
            with self.assertRaisesRegex(RuntimeError, "Synthetic Auth process failed"):
                self.run_main("--apply", "--periodic")
        self.assertEqual(len(writes), 2)

    def test_nonzero_container_exit_never_returns_raw_output(self):
        complete = SimpleNamespace(
            returncode=1,
            stdout="synthetic-private-stdout",
            stderr="synthetic-private-stderr",
        )
        with patch.object(self.host.subprocess, "run", return_value=complete):
            with self.assertRaises(RuntimeError) as raised:
                self.host.container_run("synthetic-auth", SOURCE)
        self.assertEqual(str(raised.exception), "Auth recovery process failed (exit 1)")

    def test_invalid_container_output_is_rejected_without_raw_text(self):
        complete = SimpleNamespace(returncode=0, stdout="synthetic-private-invalid-json", stderr="")
        with patch.object(self.host.subprocess, "run", return_value=complete):
            with self.assertRaises(RuntimeError) as raised:
                self.host.container_run("synthetic-auth", SOURCE)
        self.assertEqual(str(raised.exception), "Auth recovery process returned invalid safe metadata")

    def test_container_payload_uses_stdin_and_safe_metadata_only(self):
        complete = SimpleNamespace(returncode=0, stdout='{"results": []}', stderr="")
        with patch.object(self.host.subprocess, "run", return_value=complete) as run:
            result = self.host.container_run(
                "synthetic-auth", SOURCE,
                apply=True, selection=[self.plan],
            )
        self.assertEqual(result, {"results": []})
        self.assertEqual(run.call_args.args[0], ["docker", "exec", "-i", "synthetic-auth", "python"])
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(run.call_args.kwargs["timeout"], 300)
        first_line = run.call_args.kwargs["input"].splitlines()[0]
        import ast
        payload = json.loads(ast.literal_eval(first_line.removeprefix("PAYLOAD = ")))
        self.assertEqual(set(payload), {"sources", "apply", "selection"})
        self.assertEqual(payload["selection"], [self.plan])
        self.assertEqual(set(payload["sources"]), {"recovery.py", "management/commands/buh_memberaudit_recover.py"})

    def test_bootstrap_requests_complete_candidate_inventory(self):
        import ast
        tree = ast.parse(self.host.CONTAINER_BOOTSTRAP)
        previews = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "handle"
            and any(keyword.arg == "apply" and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False for keyword in node.keywords)
        ]
        self.assertEqual(len(previews), 1)
        limits = [keyword.value for keyword in previews[0].keywords if keyword.arg == "limit"]
        self.assertEqual(len(limits), 1)
        self.assertIsInstance(limits[0], ast.Constant)
        self.assertEqual(limits[0].value, 0)


if __name__ == "__main__":
    unittest.main()
