"""Refuse unattributed output and unsupported standalone preview execution."""

import json
import unittest
import urllib.error
from unittest import mock
from copy import deepcopy

from ops import readiness
from tests.platform import test_readiness as fixture
from tests.platform.test_source_qualification import GitGraph, GraphAPI


class StepAPI(fixture.FakeGitHub):
    def __init__(self):
        super().__init__()
        self.downloads = []
        self.step_log = fixture.checkout_log()
        self.whole_log = fixture.checkout_log()
        self.step_unavailable = False
        self.job_change = None

    def download(self, url, maximum):
        self.downloads.append(url)
        if "/steps/" in url:
            if self.step_unavailable:
                raise readiness.ReadinessError("provider step API unavailable")
            return self.step_log
        if "/jobs/" in url and url.endswith("/logs"):
            return self.whole_log
        return super().download(url, maximum)

    def get_json(self, path, query=None):
        value = super().get_json(path, query)
        if "/actions/jobs/" in path and self.job_change:
            self.job_change(value)
        return value


class CheckoutProvenanceTests(unittest.TestCase):
    def test_step_api_version_does_not_change_metadata_contract_or_forward_token(self):
        client = readiness.GitHubClient("https://api.github.com", "synthetic-token")
        self.assertEqual(client.headers["X-GitHub-Api-Version"], "2022-11-28")
        for path, version in (
            ("/repos/owner/repo/actions/jobs/123/steps/1/logs", "2026-03-10"),
            ("/repos/owner/repo/actions/artifacts/123/zip", "2022-11-28"),
        ):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://api.github.com" + path,
                302,
                "redirect",
                {"Location": "https://logs.example.invalid/signed-log"},
                None,
            )
            with (
                mock.patch.object(
                    readiness.urllib.request, "build_opener", return_value=opener
                ),
                mock.patch.object(client, "_read", return_value=b"step output") as read,
            ):
                self.assertEqual(
                    client.download("https://api.github.com" + path, 100), b"step output"
                )
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header("X-github-api-version"), version)
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-token")
            redirected = read.call_args.args[0]
            self.assertIsNone(redirected.get_header("Authorization"))
            self.assertEqual(
                redirected.get_header("Accept"),
                "text/plain" if "/steps/" in path else "application/zip",
            )
        self.assertEqual(client.headers["X-GitHub-Api-Version"], "2022-11-28")

    def collect(self, client):
        return readiness.collect(client, fixture.REPOSITORY, fixture.PR, fixture.SHA)

    def published(self):
        client = fixture.PublishedGitHub()
        client.changed_files = ["apps/example/models.py"]
        client.readiness_record = readiness.collect(
            client, fixture.REPOSITORY, fixture.PR, fixture.SHA, merged=True
        )
        client.rebuild_readiness_artifact()
        return client

    def verify(self, client):
        return readiness.verify_published(
            client,
            fixture.REPOSITORY,
            fixture.PR,
            fixture.SHA,
            fixture.MERGE_SHA,
            allowed_mergers=["Anthony"],
        )

    def test_attributed_no_ui_collection_serialization_and_historical_verification(self):
        client = StepAPI()
        record = self.collect(client)
        self.assertEqual(record, self.collect(client))
        self.assertEqual(readiness.validate(json.loads(json.dumps(record))), record)
        self.assertFalse(record["preview_required"])
        for job in record["source_qualification"]["source_jobs"]:
            if job["name"] != readiness.REQUIRED_CHECK:
                self.assertEqual(job["checkout_step_number"], 2)
                self.assertTrue(job["log_sha256"].startswith("sha256:"))
        self.assertTrue(client.downloads)
        self.assertTrue(all("/steps/1/logs" in url for url in client.downloads))
        self.assertEqual(
            self.verify(self.published())["attestation_mode"], "premerge-published"
        )

    def test_saved_r2_later_or_earlier_output_cannot_replace_missing_step(self):
        for position in ("before", "after", "both"):
            client = StepAPI()
            client.step_log = b""
            printed = (
                b"2026-10-16T01:10:00Z ##[group]Run actions/checkout@11d5960a326750d5838078e36cf38b85af677262\n"
                + fixture.checkout_log()
                + b"2026-10-16T01:10:10Z ##[endgroup]\n"
            )
            client.whole_log = (
                (printed if position in {"before", "both"} else b"")
                + b"unrelated test output\n"
                + (printed if position in {"after", "both"} else b"")
            )
            for _ in range(3):
                with (
                    self.subTest(position=position),
                    self.assertRaisesRegex(
                        readiness.ReadinessError, "attributed checkout log is unavailable"
                    ),
                ):
                    self.collect(client)
            self.assertTrue(all("/steps/" in u for u in client.downloads))

    def test_whole_log_is_irrelevant_when_genuine_attributed_step_exists(self):
        client = StepAPI()
        client.whole_log = fixture.checkout_log(fixture.OTHER_SHA) * 2
        self.assertTrue(self.collect(client)["ready"])

    def test_missing_step_api_blocks_even_when_whole_log_looks_complete(self):
        client = StepAPI()
        client.step_unavailable = True
        with self.assertRaisesRegex(
            readiness.ReadinessError, "whole-job logs.*cannot substitute"
        ):
            self.collect(client)

    def test_printed_group_and_literal_commands_inside_other_output_are_not_proof(self):
        client = StepAPI()
        client.step_log = fixture.checkout_log().replace(
            b" [command]", b" literal fixture diagnostic: [command]"
        )
        with self.assertRaisesRegex(readiness.ReadinessError, "did not establish"):
            self.collect(client)

    def test_conflicting_wrong_missing_or_reordered_source_identity_blocks(self):
        good = fixture.checkout_log()
        cases = (
            fixture.checkout_log(fixture.OTHER_SHA),
            good + good,
            good.replace(fixture.REPOSITORY.encode(), b"other/repository"),
            good.replace(b"git log -1 --format=%H", b"git rev-parse HEAD"),
            b"\n".join(reversed(good.splitlines())),
            good + b"unframed arbitrary output\n",
        )
        for log in cases:
            client = StepAPI()
            client.step_log = log
            with self.subTest(log=log), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_step_name_number_success_and_uniqueness_are_bound(self):
        for kind in ("name", "number", "failure", "duplicate", "missing", "order"):
            client = StepAPI()
            original = client.get_json

            def get(path, query=None, kind=kind, original=original):
                value = original(path, query)
                if "/attempts/" in path and path.endswith("/jobs"):
                    steps = value["jobs"][0]["steps"]
                    if kind == "name":
                        steps[1]["name"] = "Check out arbitrary fixture"
                    elif kind == "number":
                        steps[1]["number"] = 9
                    elif kind == "failure":
                        steps[1]["conclusion"] = "failure"
                    elif kind == "duplicate":
                        steps.append(deepcopy(steps[1]))
                    elif kind == "missing":
                        steps.clear()
                    else:
                        steps.reverse()
                return value

            client.get_json = get
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_direct_job_recheck_refuses_another_run_attempt_source_or_step(self):
        for field, value in (
            ("run_id", 100),
            ("run_attempt", 2),
            ("head_sha", fixture.OTHER_SHA),
            ("id", 999),
        ):
            client = StepAPI()
            client.job_change = lambda job, f=field, v=value: job.update({f: v})
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    readiness.ReadinessError, "execution identity changed"
                ),
            ):
                self.collect(client)

    def test_published_revalidation_downloads_attributed_output_again(self):
        client = self.published()
        original = client.download
        client.download = lambda url, bound: (
            fixture.checkout_log(fixture.OTHER_SHA)
            if "/steps/" in url
            else original(url, bound)
        )
        with self.assertRaisesRegex(readiness.ReadinessError, "did not establish"):
            self.verify(client)

    def test_old_whole_log_qualification_and_missing_step_fields_cannot_publish(self):
        record = self.collect(StepAPI())
        for kind in ("old-version", "missing-number", "wrong-number", "missing-name"):
            changed = deepcopy(record)
            proof = changed["source_qualification"]
            if kind == "old-version":
                proof["schema_version"] = 1
            elif kind == "missing-number":
                proof["source_jobs"][0].pop("checkout_step_number")
            elif kind == "wrong-number":
                proof["source_jobs"][0]["checkout_step_number"] = 7
            else:
                proof["source_jobs"][0].pop("checkout_step_name")
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                readiness.validate(changed)

    def test_preview_checkout_attribution_can_be_verified_without_claiming_workflow(self):
        client = StepAPI()
        jobs = readiness._tested_jobs(
            client,
            fixture.REPOSITORY,
            fixture.SHA,
            fixture.RUN_ID,
            fixture.RUN_ATTEMPT,
            preview=True,
        )
        self.assertEqual(jobs[0]["checkout_step_number"], 3)
        self.assertEqual(
            client.downloads,
            [
                f"https://api.github.com/repos/{fixture.REPOSITORY}/actions/jobs/2001/steps/2/logs"
            ],
        )

    def test_applicable_preview_without_immutable_execution_identity_blocks(self):
        client = StepAPI()
        client.changed_files = ["templates/index.html"]
        for _ in range(3):
            with self.assertRaisesRegex(
                readiness.ReadinessError, "immutable executing workflow"
            ):
                self.collect(client)

    def test_ui_scope_cannot_be_removed_to_hide_unsupported_preview(self):
        for paths, labels in (
            (["apps/x/static/x.css"], []),
            (["feature.py"], ["ui-preview"]),
        ):
            client = StepAPI()
            client.changed_files = paths
            client.labels.extend(labels)
            with (
                self.subTest(paths=paths),
                self.assertRaisesRegex(
                    readiness.ReadinessError, "immutable executing workflow"
                ),
            ):
                self.collect(client)

    def test_preview_only_v3_record_is_refused_in_validation_and_published_consumption(
        self,
    ):
        current = self.collect(StepAPI())
        client = fixture.PublishedGitHub()
        record = deepcopy(client.readiness_record)
        record["schema_version"] = 3
        record["source_qualification"] = current["source_qualification"]
        client.readiness_record = record
        client.rebuild_readiness_artifact()
        with self.assertRaisesRegex(
            readiness.ReadinessError, "immutable executing workflow"
        ):
            readiness.validate(record)
        with self.assertRaisesRegex(
            readiness.ReadinessError, "immutable executing workflow"
        ):
            self.verify(client)

    def test_historical_v2_preview_remains_readable_without_disposition_route(self):
        self.assertTrue(self.verify(fixture.PublishedGitHub())["preview_required"])


class RestoredPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = GitGraph()

    def test_changed_then_restored_preview_contract_cannot_inherit_current_yaml(self):
        graph = self.graph
        original = (graph.path / readiness.PREVIEW_WORKFLOW).read_bytes()
        graph.git("checkout", "--detach", graph.p)
        graph.write(readiness.PREVIEW_WORKFLOW, "name: Unsupported older preview\n")
        graph.git("add", "--all")
        graph.git("commit", "-m", "Synthetic unsupported preview contract")
        prior = graph.git("rev-parse", "HEAD")
        graph.git("checkout", "--detach", prior)
        (graph.path / readiness.PREVIEW_WORKFLOW).write_bytes(original)
        graph.git("add", "--all")
        graph.git("commit", "-m", "Synthetic restored preview contract")
        restored = graph.git("rev-parse", "HEAD")
        client = GraphAPI(graph)
        client.base_sha, client.combined = restored, graph.merge(restored, graph.h)
        client.changed_files = ["templates/index.html"]
        self.assertEqual(
            graph.git("rev-parse", client.combined + "^{tree}"),
            graph.git("rev-parse", graph.h + "^{tree}"),
        )
        original_get = client.get_json

        def get(path, query=None):
            value = original_get(path, query)
            if path.endswith(f"/actions/runs/{fixture.RUN_ID}"):
                value["pull_requests"][0]["base"] = {"sha": prior}
            return value

        client.get_json = get
        for _ in range(3):
            with self.assertRaisesRegex(
                readiness.ReadinessError, "immutable executing workflow"
            ):
                readiness.collect(client, fixture.REPOSITORY, fixture.PR, graph.h)


if __name__ == "__main__":
    unittest.main()
