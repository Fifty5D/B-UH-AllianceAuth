"""Captured September 12 GitHub responses plus a declared future repair merge.

No production workflow is dispatched. Only the future non-production PR/run and
its synthetic UI artifacts are simulated; PR52, approval, expired PR51 evidence,
failed/skipped deployment, and retained recovery/preflight data are real fixtures.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from unittest import mock

from tests.platform import test_readiness as preview_fixture
from tests.platform.test_release_workflow_execution import _bash_path, _execute_step, _git, _step_script

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops/release"))
import platform_approval as approval  # noqa: E402
import approved_deployment_continuation as continuation  # noqa: E402
import deployment_evidence  # noqa: E402
import open_sync_pr  # noqa: E402

PREFIX = "/repos/Fifty5D/B-UH-AllianceAuth/"
FIXTURE = Path(__file__).with_name("fixtures") / "v062-approved-deployment.json"
REPAIR_HEAD = preview_fixture.SHA
REPAIR_MERGE = preview_fixture.MERGE_SHA
REPAIR_TREE = "1" * 40
RUN_ID = 90001
TOKEN = "synthetic-regression-token"


class Clock(datetime.datetime):
    instant = datetime.datetime(2026, 9, 12, 12, tzinfo=datetime.timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.instant if tz else cls.instant.replace(tzinfo=None)


def _key(method, url, body=None):
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(sorted(urllib.parse.parse_qsl(parsed.query)))
    result = method + " " + parsed.path + ("?" + query if query else "")
    if body:
        result += " " + json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    return result


def _fixture_key(key):
    method, url, *body = key.split(" ", 2)
    return _key(method, "https://api.github.com" + url, body[0] if body else None)


def stable_zip(payload):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(output, "w") as destination:
        for name in source.namelist():
            destination.writestr(zipfile.ZipInfo(name, (2026, 9, 12, 0, 0, 0)), source.read(name))
    return output.getvalue()


class Replay:
    def __init__(self, *, future=False):
        fixture = json.loads(FIXTURE.read_text(encoding="ascii"))
        self.files = fixture.pop("__preflight_files__")
        self.records = {_fixture_key(key): value for key, value in fixture.items()}
        self.calls = []
        self.future = future
        self.repair = preview_fixture.PublishedGitHub()
        self.repair.merger = "Fifty5D"
        self.repair.labels.append("ui-preview")
        self.repair.merge_parents = [continuation.SYNC_MERGE, REPAIR_HEAD]
        self.repair.changed_files = ["ops/release/platform_approval.py"]
        # Retained artifacts do not change when a later job downloads them.
        # Fix ZIP timestamps so separate CLI processes use identical test bytes.
        for kind in ("manifest", "evidence"):
            payload = stable_zip(getattr(self.repair, kind + "_zip"))
            digest = hashlib.sha256(payload).hexdigest()
            setattr(self.repair, kind + "_zip", payload)
            setattr(self.repair, kind + "_digest", digest)
            self.repair.readiness_record["preview"][kind + "_artifact_digest"] = "sha256:" + digest
        self.repair.rebuild_readiness_artifact()
        self.repair.readiness_zip = stable_zip(self.repair.readiness_zip)
        self.repair.readiness_digest = hashlib.sha256(self.repair.readiness_zip).hexdigest()
        self.repair.pointer["artifact_digest"] = "sha256:" + self.repair.readiness_digest
        self.extra_runs = []
        self.future_main = REPAIR_MERGE
        self.future_status = "success"

    def value(self, suffix, query=""):
        return self.records[_key("GET", "https://api.github.com" + PREFIX + suffix + query)]["value"]

    def request(self, method, url, headers, body, timeout):
        self.calls.append(_key(method, url, body))
        assert method == "GET" or (method == "POST" and url.endswith("/graphql") and b"mutation" not in body)
        parsed = urllib.parse.urlsplit(url)
        suffix = parsed.path.removeprefix(PREFIX)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        result = None
        if self.future:
            result = self._future(method, parsed.path, suffix, query, body)
        if result is None:
            key = _key(method, url, body)
            if key not in self.records:
                raise AssertionError(f"Unrecorded read: {key[:350]}")
            item = self.records[key]
            return open_sync_pr.HttpResponse(item["status"], json.dumps(item["value"]).encode())
        return open_sync_pr.HttpResponse(200, json.dumps(result).encode())

    def _future(self, method, path, suffix, query, body):
        if suffix == "git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.future_main}}
        if suffix == f"commits/{REPAIR_MERGE}":
            return {"sha": REPAIR_MERGE, "parents": [{"sha": continuation.SYNC_MERGE}, {"sha": REPAIR_HEAD}], "commit": {"tree": {"sha": REPAIR_TREE}}}
        if suffix == f"commits/{REPAIR_HEAD}":
            return {"sha": REPAIR_HEAD, "commit": {"tree": {"sha": REPAIR_TREE}}}
        if suffix == f"commits/{REPAIR_MERGE}/pulls":
            return [{"number": preview_fixture.PR}]
        if suffix == f"pulls/{preview_fixture.PR}":
            pull = self.repair._pr_payload()
            pull.update({"draft": False, "merged_at": "2026-09-12T11:00:00Z", "user": {"login": "Fifty5D"}})
            pull["head"]["ref"] = continuation.BRANCH
            return pull
        if suffix == "actions/workflows/deploy-approved-platform-release.yml/runs":
            run = {
                "id": RUN_ID, "run_attempt": 1, "head_sha": REPAIR_MERGE, "head_branch": "main",
                "path": continuation.WORKFLOW, "event": "workflow_dispatch", "status": "in_progress", "conclusion": None,
                "actor": {"login": "Fifty5D"}, "triggering_actor": {"login": "Fifty5D"},
                "repository": {"full_name": continuation.REPOSITORY}, "head_repository": {"full_name": continuation.REPOSITORY},
            }
            return {"total_count": 1 + len(self.extra_runs), "workflow_runs": [run, *self.extra_runs]}
        if suffix == "actions/workflows/source-ci.yml/runs" and query.get("head_sha") == REPAIR_MERGE:
            run = {
                "id": 90002, "run_attempt": 1, "head_sha": REPAIR_MERGE, "head_branch": "main",
                "path": ".github/workflows/source-ci.yml", "event": "push", "status": "completed", "conclusion": self.future_status,
                "repository": {"full_name": continuation.REPOSITORY}, "head_repository": {"full_name": continuation.REPOSITORY},
            }
            return {"total_count": 1, "workflow_runs": [run]}
        if method == "POST":
            value = json.loads(body)
            if value["variables"].get("number") == preview_fixture.PR:
                return {"data": self.repair.graphql(value["query"], value["variables"])}
        fake_pages = {
            f"commits/{REPAIR_HEAD}/pulls", f"issues/{preview_fixture.PR}/comments",
            f"pulls/{preview_fixture.PR}/reviews", f"pulls/{preview_fixture.PR}/files",
        }
        if suffix in fake_pages:
            items = self.repair.pages(path)
            if suffix.endswith("/files"):
                return [{**item, "status": "modified"} for item in items]
            return items
        if (
            suffix in {f"actions/runs/{i}" for i in (99, 1234, 5678)}
            or suffix in {f"actions/artifacts/{i}" for i in (71, 72, 801)}
            or suffix == "check-runs/55"
            or (suffix in {"actions/workflows/source-ci.yml/runs", "actions/workflows/ui-preview.yml/runs"} and query.get("head_sha") == REPAIR_HEAD)
        ):
            return self.repair.get_json(path, query)
        return None


def environment():
    return {
        "GITHUB_TOKEN": TOKEN, "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": REPAIR_MERGE,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ID": str(RUN_ID), "GITHUB_WORKFLOW_REF": f"{continuation.REPOSITORY}/{continuation.WORKFLOW}@refs/heads/main",
    }


def event():
    return {"ref": "refs/heads/main", "inputs": {"confirmation": continuation.CONFIRMATION}, "sender": {"login": "Fifty5D"}}


def install_replay(replay):
    original = open_sync_pr.GitHubClient.__init__

    def init(client, api_url, token, **kwargs):
        original(client, api_url, token, transport=replay, sleeper=lambda _: None)

    patches = [
        mock.patch.object(open_sync_pr.GitHubClient, "__init__", init),
        mock.patch.object(deployment_evidence.ReadClient, "download", lambda client, url, maximum: replay.repair.download(url, maximum)),
        mock.patch.object(approval.dt, "datetime", Clock),
    ]
    for patch in patches:
        patch.start()
    return patches


class ApprovedDeploymentContinuationTests(unittest.TestCase):
    def setUp(self):
        Clock.instant = datetime.datetime(2026, 9, 12, 12, tzinfo=datetime.timezone.utc)
        self.replay = Replay(future=True)
        for patch in install_replay(self.replay):
            self.addCleanup(patch.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def boundary(self, *, env=None, request=None):
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(request or event()), encoding="ascii")
        output = self.root / "approval.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        status = approval.main([
            "boundary", "--owner", "Fifty5D", "--repository", continuation.REPOSITORY,
            "--actor", "Fifty5D", "--triggering-actor", "Fifty5D", "--run-attempt", "1",
            "--api-url", "https://api.github.com", "--server-url", "https://github.com",
            "--event", str(event_path), "--output", str(output),
        ], environment=env or environment(), stdout=stdout, stderr=stderr)
        if status:
            raise AssertionError(stderr.getvalue())
        return json.loads(output.read_text(encoding="ascii"))

    def test_actual_merged_approval_expired_previews_and_retained_preflight(self):
        for identity in (10083576809, 10083531661, 10083531309):
            self.assertIs(self.replay.value(f"actions/artifacts/{identity}")["expired"], True)
        report = self.boundary()
        self.assertEqual(report["merge_commit"], continuation.SYNC_MERGE)
        self.assertEqual(report["release_commit"], "6074b965cbd2e6ab2630cd539ee455b8d419aef6")
        self.assertNotEqual(report["release_commit"], report["continuation"]["workflow_commit"])
        preflight = self.root / "preflight"
        preflight.mkdir()
        for name, content in self.replay.files.items():
            (preflight / name).write_text(content, encoding="utf-8")
        approval.verify_artifact(report, preflight)
        # Caller initial/final and reusable queued/final all execute the same CLI.
        for _ in range(3):
            self.assertEqual(self.boundary(), report)
        self.assertFalse(any(f"actions/artifacts/{identity}" in key for identity in (10083576809, 10083531661, 10083531309) for key in self.replay.calls))

    def test_reject_deleted_changed_and_near_expiry_required_evidence(self):
        for run in (34297562022, 34638993007):
            key = next(key for key in self.replay.records if f"actions/runs/{run}/artifacts?" in key)
            original = copy.deepcopy(self.replay.records[key])
            for mutation in ("deleted", "digest", "expired", "queue"):
                with self.subTest(run=run, mutation=mutation):
                    self.replay.records[key] = copy.deepcopy(original)
                    value = self.replay.records[key]["value"]
                    if mutation == "deleted":
                        value.update({"artifacts": [], "total_count": 0})
                    elif mutation == "digest":
                        value["artifacts"][0]["digest"] = "sha256:" + "0" * 64
                    elif mutation == "expired":
                        value["artifacts"][0]["expired"] = True
                    else:
                        value["artifacts"][0]["expires_at"] = "2026-09-12T13:00:00Z"
                    with self.assertRaisesRegex(AssertionError, "artifact|evidence"):
                        self.boundary()
            self.replay.records[key] = original

    def test_queue_delay_fails_with_unchanged_metadata(self):
        self.boundary()
        Clock.instant = datetime.datetime(2026, 9, 16, 0, tzinfo=datetime.timezone.utc)
        with self.assertRaisesRegex(AssertionError, "90-minute"):
            self.boundary()

    def test_changed_release_and_revoked_approval_fail(self):
        for suffix, field, value in (
            ("git/ref/heads/release/platform-v0.6.2", "object", {"type": "commit", "sha": "f" * 40}),
            (f"commits/{continuation.SYNC_MERGE}", "commit", {"message": "approval removed", "tree": {"sha": continuation.SYNC_TREE}}),
        ):
            with self.subTest(suffix=suffix):
                record = self.replay.value(suffix)
                old = copy.deepcopy(record[field])
                record[field] = value
                with self.assertRaises(AssertionError):
                    self.boundary()
                record[field] = old
        report = self.boundary()
        report["manifest_sha256"] = "0" * 64
        preflight = self.root / "bad-preflight"
        preflight.mkdir()
        for name, content in self.replay.files.items():
            (preflight / name).write_text(content, encoding="utf-8")
        with self.assertRaisesRegex(approval.ApprovalError, "exact preflight"):
            approval.verify_artifact(report, preflight)

    def test_blocking_reviews_after_merge_still_fail(self):
        for number in (51, 52):
            key = next(key for key in self.replay.records if f"pulls/{number}/reviews?" in key)
            old = copy.deepcopy(self.replay.records[key]["value"])
            self.replay.records[key]["value"].append({
                "id": 999999999999, "user": {"login": "Reviewer"}, "state": "CHANGES_REQUESTED",
                "body": "Blocking review", "submitted_at": "2026-09-12T11:00:00Z", "commit_id": continuation.SYNC_HEAD,
            })
            with self.subTest(pr=number), self.assertRaisesRegex(AssertionError, "changes-requested"):
                self.boundary()
            self.replay.records[key]["value"] = old

    def test_no_repeat_dispatch_and_no_started_original_deployment(self):
        self.replay.extra_runs = [{"id": 90000, "conclusion": "failure"}]
        with self.assertRaisesRegex(AssertionError, "already been attempted"):
            self.boundary()
        self.replay.extra_runs = []
        jobs = self.replay.value(f"actions/runs/{continuation.FAILED_RUN}/jobs", "?per_page=100")["jobs"]
        next(job for job in jobs if job["id"] == continuation.SKIPPED_JOB)["conclusion"] = "failure"
        with self.assertRaisesRegex(AssertionError, "before server access"):
            self.boundary()

    def test_repair_readiness_preview_and_current_main_are_not_bypassed(self):
        self.replay.repair.preview_artifact_expired = True
        with self.assertRaisesRegex(AssertionError, "retained preview"):
            self.boundary()
        self.replay.repair.preview_artifact_expired = False
        self.replay.future_main = "f" * 40
        with self.assertRaisesRegex(AssertionError, "current main"):
            self.boundary()
        self.replay.future_main = REPAIR_MERGE
        self.replay.future_status = "failure"
        with self.assertRaisesRegex(AssertionError, "did not pass"):
            self.boundary()

    def test_actual_workflow_sequence_and_archive_after_immutable_checkout(self):
        """Execute the real Bash steps and CLIs; stub only GitHub/installation.

        The archive builder and all packaged receiver/runtime files execute from
        the actual immutable release. Only the reviewed approval tool is staged.
        """
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            base = Path(temporary)
            checkout = base / "checkout"
            _git(base, "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(checkout))
            _git(checkout, "config", "core.autocrlf", "false")
            _git(checkout, "config", "user.name", "Deployment Rehearsal")
            _git(checkout, "config", "user.email", "rehearsal@example.invalid")
            _git(checkout, "checkout", "--quiet", "-b", "rehearsal-repair", continuation.SYNC_MERGE)
            for name in sorted(continuation.ALLOWED_PATHS):
                source = ROOT / name
                if source.is_file():
                    target = checkout / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            _git(checkout, "add", "--all")
            _git(checkout, "commit", "--quiet", "-m", "test-only reviewed deployment repair")
            head = _git(checkout, "rev-parse", "HEAD")
            _git(checkout, "checkout", "--quiet", "-b", "rehearsal-main", continuation.SYNC_MERGE)
            _git(checkout, "merge", "--quiet", "--no-ff", head, "-m", "test-only repair merge")
            merge = _git(checkout, "rev-parse", "HEAD")
            tree = _git(checkout, "rev-parse", "HEAD^{tree}")
            held = approval.validation_recovery.validate_hold(checkout, merge, contract=approval.validation_recovery.load_contract())
            self.assertEqual(held["state"], "approved-deployment-continuation-held")
            self.assertEqual(_git(checkout, "diff", "--name-only", continuation.SYNC_MERGE, merge, "--", "releases"), "")
            # Reject even a single out-of-scope runtime edit in the hold.
            with mock.patch.object(approval.validation_recovery, "_changed_paths", return_value=("ops/deploy/docker_host.py",)):
                with self.assertRaisesRegex(approval.validation_recovery.ValidationRecoveryError, "unreviewed paths"):
                    continuation.validate_repair(checkout, merge)
            _git(checkout, "remote", "set-url", "origin", str(checkout))
            runner = base / "runner"
            runner.mkdir()
            bootstrap = base / "bootstrap"
            bootstrap.mkdir()
            stage = runner / "buh-platform-approval-source"
            # A transport-only subprocess bootstrap. The executed CLI and all
            # validators are loaded from the actual checkout/staged verifier.
            (bootstrap / "sitecustomize.py").write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(stage / 'ops/release')!r} if __import__('pathlib').Path({str(stage)!r}).exists() else {str(checkout / 'ops/release')!r})\n"
                "import platform_approval\n"
                "from tests.release import test_deployment_continuation as f\n"
                f"f.REPAIR_HEAD=f.preview_fixture.SHA={head!r}\n"
                f"f.REPAIR_MERGE=f.preview_fixture.MERGE_SHA={merge!r}\n"
                f"f.REPAIR_TREE={tree!r}\n"
                "f.install_replay(f.Replay(future=True))\n",
                encoding="utf-8",
            )
            event_path = runner / "event.json"
            event_path.write_text(json.dumps(event()), encoding="ascii")
            env = {
                **environment(), "GITHUB_SHA": merge, "GITHUB_EVENT_PATH": _bash_path(event_path),
                "GITHUB_OUTPUT": _bash_path(runner / "output.txt"), "RUNNER_TEMP": _bash_path(runner),
                "PYTHONPATH": os.pathsep.join([str(bootstrap), str(ROOT), os.environ.get("PYTHONPATH", "")]),
                "WORK_ACTOR": "", "GITHUB_API_URL": "https://api.github.com",
                "REPOSITORY": continuation.REPOSITORY, "REPOSITORY_OWNER": "Fifty5D",
                "APPROVED_CONTINUATION": "true", "APPROVAL_SOURCE_COMMIT": merge,
            }

            def run(workflow, job, name):
                script = _step_script(workflow, job, name)
                contexts = {
                    "github.repository_owner": "Fifty5D", "github.repository": continuation.REPOSITORY,
                    "github.actor": "Fifty5D", "github.triggering_actor": "Fifty5D", "github.run_attempt": "1",
                    "github.api_url": "https://api.github.com", "github.server_url": "https://github.com", "github.sha": merge,
                }
                for key, value in contexts.items():
                    script = script.replace("${{ " + key + " }}", value)
                if os.environ.get("BUH_TEST_TOOLS"):
                    tools = _bash_path(Path(os.environ["BUH_TEST_TOOLS"]))
                    script = f'export PATH="{tools}:$PATH"\n' + script
                completed = _execute_step(script, checkout, env)
                self.assertEqual(completed.returncode, 0, msg=name + "\n" + (completed.stdout + completed.stderr)[-2400:])

            caller = "deploy-approved-platform-release.yml"
            run(caller, "authorize", "Reverify the merge, evidence, and one ChatGPT approval")
            report = json.loads((checkout / "build/platform-deploy-approval.json").read_text())
            for key, value in report.items():
                if isinstance(value, (str, int)):
                    env[key.upper()] = str(value)
            for folder in ("preflight-evidence",):
                destination = checkout / "build" / folder
                destination.mkdir(parents=True)
                for name, content in self.replay.files.items():
                    (destination / name).write_text(content, encoding="utf-8")
            run(caller, "authorize", "Verify receiver and observer preflight evidence")
            run(caller, "authorize", "Close authorization races before deploy")
            # Fresh immutable checkout: deliberately remove only this rehearsal's
            # generated build directory, reproducing checkout cleanup.
            build = (checkout / "build").resolve()
            self.assertTrue(build.is_relative_to(base.resolve()))
            shutil.rmtree(build)
            _git(checkout, "checkout", "--quiet", "--detach", report["release_commit"])
            self.assertFalse((checkout / "build").exists())
            workflow = "deploy-platform-v2.yml"
            run(workflow, "deploy", "Stage the merge-owned approval verifier")
            env["APPROVAL_TOOL"] = _bash_path(stage / "ops/release/platform_approval.py")
            run(workflow, "deploy", "Verify release lineage, bundle, and production compatibility")
            env.update({"MODE": "deploy", "RELEASE_DIR": "releases/platform/v0.6.2", "RUN_ID": str(RUN_ID), "RUN_ATTEMPT": "1"})
            run(workflow, "deploy", "Build a deterministic bounded deployment archive")
            metadata = json.loads((checkout / "artifacts/deployment-request.json").read_text())
            self.assertIn(report["release_commit"], [item["release_commit"] for item in metadata["release_refs"]])
            self.assertEqual(metadata["manifest_sha256"], report["manifest_sha256"])
            self.assertEqual(_git(checkout, "status", "--short", "--untracked-files=no"), "")
            run(workflow, "deploy", "Reauthorize the queued production operation")
            destination = checkout / "build/production-boundary-preflight"
            destination.mkdir(parents=True)
            for name, content in self.replay.files.items():
                (destination / name).write_text(content, encoding="utf-8")
            env["EXPECTED_FEATURE_READINESS"] = json.dumps(report["feature_readiness"], sort_keys=True, separators=(",", ":"))
            env["RELEASE_MANIFEST_SHA256"] = report["manifest_sha256"]
            run(workflow, "deploy", "Reverify live feature readiness at the production boundary")
            # The next real step would materialize protected SSH identities.
            # Neither SSH nor receiver installation is executed by this test.
            self.assertEqual(json.loads((checkout / "build/platform-deploy-boundary-final.json").read_text()), report)


if __name__ == "__main__":
    unittest.main()
