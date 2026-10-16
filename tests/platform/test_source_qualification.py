"""Bound current readiness to real combined trees and observed tested checkouts."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock
from copy import deepcopy
from pathlib import Path

from ops import readiness
from tests.platform import test_readiness as common
from tests.platform.test_commented_dispositions import DispositionAPI, reference


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY, PR = common.REPOSITORY, common.PR


class GitGraph:
    def __init__(self):
        # Retain the isolated graph for inspection; never clean a user's checkout.
        self.path = Path(tempfile.mkdtemp(prefix="readiness-combination-"))
        self.environment = dict(
            os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"
        )
        self.git("init", "--initial-branch=main")
        for name in readiness.QUALIFICATION_WORKFLOWS:
            self.write(name, (ROOT / name).read_text(encoding="utf-8"))
        self.write("config.py", "RATE = 1\n")
        self.write("base_test.py", "from config import RATE\nassert RATE > 0\n")
        self.a = self.commit("original reviewed base")
        self.write(
            "feature.py", "from config import RATE\ndef amount():\n    return RATE + 1\n"
        )
        self.write("feature_test.py", "from feature import amount\nassert amount() == 2\n")
        self.write(
            "ops/readiness.py", (ROOT / "ops/readiness.py").read_text(encoding="utf-8")
        )
        self.write(
            "policy_test.py",
            "from pathlib import Path\ncompile(Path('ops/readiness.py').read_text(), 'policy', 'exec')\n",
        )
        self.h = self.commit("reviewed head including policy contents")
        self.git("checkout", "--detach", self.a)
        self.write(
            "ops/readiness.py", (ROOT / "ops/readiness.py").read_text(encoding="utf-8")
        )
        self.write(
            "policy_test.py",
            "from pathlib import Path\ncompile(Path('ops/readiness.py').read_text(), 'policy', 'exec')\n",
        )
        self.p = self.commit("activate identical policy contents")
        self.git("checkout", "--detach", self.a)
        self.write(
            "policy-only-name.txt", "Unqualified contents absent from the tested source.\n"
        )
        self.u = self.commit("uncertain policy-only advance")
        self.git("checkout", "--detach", self.p)
        self.write("config.py", "RATE = 2\n")
        self.b = self.commit("material base change")
        self.old_ci = self.merge(self.a, self.h)
        self.safe = self.merge(self.p, self.h)
        self.uncertain = self.merge(self.u, self.h)
        self.broken = self.merge(self.b, self.h)
        self.git("checkout", "--detach", self.broken)
        self.write("feature_test.py", "from feature import amount\nassert amount() == 3\n")
        self.c = self.commit("qualify the updated combined source")
        self.corrected = self.merge(self.b, self.c)
        self.results, self.logs = {}, {}
        for identity in (
            self.a,
            self.h,
            self.p,
            self.b,
            self.safe,
            self.uncertain,
            self.broken,
            self.c,
        ):
            self.git("checkout", "--progress", "--force", identity)
            observed = self.git("log", "-1", "--format=%H")
            self.logs[identity] = common.checkout_log(observed)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import runpy; from pathlib import Path; "
                    "[runpy.run_path(str(p)) for p in sorted(Path('.').glob('*_test.py'))]",
                ],
                cwd=self.path,
                env=self.environment,
                capture_output=True,
            )
            self.results[identity] = result.returncode
        assert self.results[self.h] == self.results[self.b] == self.results[self.safe] == 0
        assert self.results[self.broken] != 0 and self.results[self.c] == 0

    def git(self, *arguments):
        return (
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Synthetic Review",
                    "-c",
                    "user.email=review@example.invalid",
                    *arguments,
                ],
                cwd=self.path,
                env=self.environment,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )

    def write(self, path, content):
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, message):
        dates = {
            "original reviewed base": "2026-10-12T00:00:00Z",
            "reviewed head including policy contents": "2026-10-13T00:00:00Z",
            "activate identical policy contents": "2026-10-15T00:00:00Z",
            "uncertain policy-only advance": "2026-10-15T01:00:00Z",
            "material base change": "2026-10-16T00:00:00Z",
            "qualify the updated combined source": "2026-10-16T01:00:00Z",
        }
        self.environment.update(
            GIT_AUTHOR_DATE=dates[message], GIT_COMMITTER_DATE=dates[message]
        )
        self.git("add", "--all")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge(self, base, head):
        date = "2026-10-16T04:00:00Z"
        if base == self.a:
            date = "2026-10-14T00:00:00Z"
        elif head == self.h and base != self.p:
            date = "2026-10-16T00:15:00Z"
        self.environment.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        tree = self.git("merge-tree", "--write-tree", base, head)
        return self.git(
            "commit-tree", tree, "-p", base, "-p", head, "-m", "Synthetic merge"
        )

    def commit_metadata(self, identity):
        return {
            "sha": identity,
            "parents": [
                {"sha": p} for p in self.git("show", "-s", "--format=%P", identity).split()
            ],
            "commit": {"tree": {"sha": self.git("rev-parse", identity + "^{tree}")}},
        }

    def tree_metadata(self, identity):
        entries = []
        for line in self.git("ls-tree", "-r", identity).splitlines():
            metadata, path = line.split("\t", 1)
            mode, kind, blob = metadata.split()
            entries.append({"path": path, "mode": mode, "type": kind, "sha": blob})
        return {"sha": identity, "truncated": False, "tree": entries}


class GraphAPI(DispositionAPI, common.PublishedGitHub):
    def __init__(self, graph, *, corrected=False):
        self.graph = graph
        self.head = graph.c if corrected else graph.h
        self.combined = graph.corrected if corrected else graph.safe
        self.workflow_commit = graph.corrected if corrected else graph.old_ci
        super().__init__()
        self.final_sha = self.head
        self.base_sha = graph.b if corrected else graph.p
        self.merged, self.pointer_count = False, 0
        self.labels = ["codex", "needs-codex"]
        self.ci_head = self.preview_head = self.head
        self.permission = "write"
        self.permission_user = {"login": "reviewer", "id": 101, "type": "User"}
        self.last_edited_at, self.provenance_mutation = None, None
        self.mutation, self.unavailable = None, None
        self.reviews = [
            {
                "id": 11,
                "user": deepcopy(self.permission_user),
                "state": "COMMENTED",
                "commit_id": graph.a,
                "submitted_at": "2026-10-13T01:00:00Z",
                "body": "[P1] First corrected finding.\n[P1] Second corrected finding.",
            },
            {
                "id": 21,
                "user": deepcopy(self.permission_user),
                "state": "COMMENTED",
                "commit_id": graph.h,
                "submitted_at": "2026-10-14T01:00:00Z",
                "body": f"source review: **PASS** at `{graph.h}`, base `{graph.a}`.",
            },
        ]
        if corrected:
            self.reviews.append(
                {
                    "id": 31,
                    "user": deepcopy(self.permission_user),
                    "state": "COMMENTED",
                    "commit_id": graph.c,
                    "submitted_at": "2026-10-16T02:00:00Z",
                    "body": f"source review: **PASS** at `{graph.c}`, base `{graph.b}`.",
                }
            )
        self.issue_comments = []
        self.disposition()
        manifest = self.convert(common.manifest())
        self.manifest_zip, self.evidence_zip = (
            common.manifest_zip(manifest),
            common.evidence_zip(manifest),
        )
        self.manifest_digest = hashlib.sha256(self.manifest_zip).hexdigest()
        self.evidence_digest = hashlib.sha256(self.evidence_zip).hexdigest()

    def convert(self, value):
        if isinstance(value, dict):
            return {key: self.convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.convert(item) for item in value]
        if isinstance(value, str):
            return (
                value.replace(common.SHA, self.head)
                .replace(common.BASE_SHA, self.graph.a)
                .replace(common.MERGE_SHA, self.combined)
                .replace(common.WORKFLOW_SHA, self.workflow_commit)
            )
        return value

    def disposition(self):
        passed = self.reviews[-1]
        base = self.graph.b if passed["id"] == 31 else self.graph.a
        payload = {
            "schema_version": 1,
            "repository": REPOSITORY,
            "pr": PR,
            "head_sha": passed["commit_id"],
            "base_sha": base,
            "pass_review": reference(passed),
            "resolved_reviews": [reference(self.reviews[0])],
        }
        self.issue_comments.append(
            {
                "id": 501,
                "node_id": "IC_combination",
                "user": deepcopy(self.permission_user),
                "body": readiness.COMMENTED_DISPOSITION_MARKER + "\n" + json.dumps(payload),
                "created_at": "2026-10-16T03:00:00Z",
                "updated_at": "2026-10-16T03:00:00Z",
            }
        )

    def get_json(self, path, query=None):
        if self.unavailable and self.unavailable in path:
            raise readiness.ReadinessError("synthetic qualification API unavailable")
        if "/commits/" in path and not path.endswith("/check-runs"):
            result = self.graph.commit_metadata(path.rsplit("/", 1)[1])
        elif "/git/trees/" in path:
            result = self.graph.tree_metadata(path.rsplit("/", 1)[1])
        elif "/compare/" in path:
            before, after = path.split("/compare/")[1].split("...")
            merge_base = self.graph.git("merge-base", before, after)
            result = {
                "status": "identical"
                if before == after
                else "ahead"
                if merge_base == before
                else "diverged",
                "merge_base_commit": {"sha": merge_base},
            }
        elif "/attempts/" in path and path.endswith("/jobs"):
            run_id, attempt = int(path.split("/")[-4]), int(path.split("/")[-2])
            preview = run_id == common.RUN_ID
            tested = self.preview_head if preview else self.ci_head
            result = common.qualified_jobs(run_id, attempt, tested, preview)
            if self.graph.results[tested]:
                for job in result["jobs"]:
                    job["conclusion"] = "failure"
        else:
            normalized_path = path.replace(self.head, common.SHA)
            result = self.convert(super().get_json(normalized_path, query))
        if self.mutation:
            self.mutation(path, result)
        return result

    def pages(self, path, key=None):
        return self.convert(super().pages(path.replace(self.head, common.SHA), key))

    def download(self, url, maximum):
        if url.endswith("/logs"):
            if self.unavailable == "logs":
                raise readiness.ReadinessError("synthetic checkout logs unavailable")
            identity = int(url.split("/")[-2])
            return self.graph.logs[self.preview_head if identity >= 2000 else self.ci_head]
        return super().download(url, maximum)

    def rebuild_readiness_artifact(self):
        self.readiness_zip = common.readiness_zip(self.readiness_record)
        self.readiness_digest = hashlib.sha256(self.readiness_zip).hexdigest()
        digest = self.readiness_record["review_evidence"]["digest"]
        self.pointer = {
            "schema_version": 2,
            "state": "qualified",
            "repository": REPOSITORY,
            "pr": PR,
            "head_sha": self.head,
            "review_digest": digest,
            "artifact_name": f"pr-readiness-{PR}-{self.head}-{digest.removeprefix('sha256:')}",
            "artifact_id": 801,
            "artifact_digest": "sha256:" + self.readiness_digest,
            "workflow": readiness.READINESS_WORKFLOW,
            "workflow_run_id": common.READINESS_RUN_ID,
            "workflow_run_attempt": common.READINESS_RUN_ATTEMPT,
        }

    def publish(self, record):
        self.readiness_record = record
        self.rebuild_readiness_artifact()
        self.merged, self.pointer_count = True, 1
        self.labels = ["codex", "ready-for-work"]


class SourceQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = GitGraph()

    def client(self, **kwargs):
        return GraphAPI(self.graph, **kwargs)

    def collect(self, client):
        return readiness.collect(client, REPOSITORY, PR, client.head)

    def verify(self, client):
        return readiness.verify_published(
            client,
            REPOSITORY,
            PR,
            client.head,
            client.combined,
            allowed_mergers=["Anthony"],
        )

    def test_real_conflict_free_broken_combination_cannot_requalify_old_evidence(self):
        client = self.client()
        client.base_sha, client.combined = self.graph.b, self.graph.broken
        self.assertEqual(self.graph.results[client.head], 0)
        self.assertEqual(self.graph.results[self.graph.b], 0)
        self.assertNotEqual(self.graph.results[client.combined], 0)
        for _ in range(3):
            with self.assertRaisesRegex(
                readiness.ReadinessError, "combined source is not the tested head tree"
            ):
                self.collect(client)

    def test_activation_reuses_only_exact_applicable_tree_and_preserves_review(self):
        client = self.client()
        originals = deepcopy(client.reviews)
        record = self.collect(client)
        self.assertEqual(record["schema_version"], 3)
        proof = record["source_qualification"]
        self.assertEqual(proof["base_sha"], self.graph.p)
        self.assertEqual(proof["head_sha"], self.graph.h)
        self.assertEqual(proof["merge_tree_sha"], proof["tested_tree_sha"])
        self.assertEqual(client.reviews, originals)
        self.assertEqual(self.collect(client), record)
        client.publish(record)
        self.assertEqual(self.verify(client)["attestation_mode"], "premerge-published")

    def test_unchanged_base_recollection_and_missing_or_wrong_combined_source(self):
        client = self.client()
        client.base_sha, client.combined = self.graph.a, self.graph.old_ci
        record = self.collect(client)
        self.assertEqual(self.collect(client), record)
        for kind in ("missing", "wrong-parents", "missing-tree"):
            client = self.client()

            def mutate(path, value, kind=kind, c=client):
                if kind == "missing" and path.endswith(f"/pulls/{PR}"):
                    value["merge_commit_sha"] = None
                elif path.endswith("/commits/" + c.combined):
                    if kind == "wrong-parents":
                        value["parents"][0]["sha"] = self.graph.a
                    elif kind == "missing-tree":
                        value["commit"].pop("tree")

            client.mutation = mutate
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_policy_only_filename_is_not_an_equivalence_exception(self):
        client = self.client()
        client.base_sha, client.combined = self.graph.u, self.graph.uncertain
        self.assertEqual(self.graph.results[client.combined], 0)
        with self.assertRaisesRegex(
            readiness.ReadinessError, "combined source is not the tested head tree"
        ):
            self.collect(client)

    def test_material_advance_after_current_source_tests_review_and_preview(self):
        client = self.client(corrected=True)
        original_pass = deepcopy(client.reviews[1])
        record = self.collect(client)
        self.assertEqual(record["source_qualification"]["tested_source_sha"], self.graph.c)
        self.assertEqual(record["source_qualification"]["base_sha"], self.graph.b)
        self.assertEqual(client.reviews[1], original_pass)
        client.publish(record)
        self.assertEqual(self.verify(client)["feature_pr"]["head_sha"], self.graph.c)

    def test_new_material_source_cannot_relabel_old_pass_or_preview_or_tests(self):
        for kind in ("review", "preview", "source"):
            client = self.client(corrected=True)
            if kind == "review":
                client.reviews.pop()
            elif kind == "preview":
                client.preview_head = self.graph.h
            else:
                client.ci_head = self.graph.h
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_missing_pending_failed_or_wrong_source_job_evidence(self):
        mutations = (
            lambda p: p.update(jobs=[], total_count=0),
            lambda p: p.update(total_count=20),
            lambda p: p["jobs"][0].update(status="in_progress"),
            lambda p: p["jobs"][0].update(conclusion="failure"),
            lambda p: p["jobs"][0].update(head_sha=self.graph.a),
            lambda p: p["jobs"][0].update(run_attempt=20),
            lambda p: p["jobs"][0].update(steps=[]),
            lambda p: p["jobs"][0].update(conclusion="skipped"),
        )
        for mutation in mutations:
            client = self.client()
            client.mutation = lambda path, p, m=mutation: (
                m(p) if "/runs/99/attempts/" in path else None
            )
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(readiness.ReadinessError),
            ):
                self.collect(client)

    def test_unavailable_or_wrong_actual_checkout_logs_fail_closed(self):
        for evidence in ("logs", "wrong-checkout", "missing-observed-head"):
            client = self.client()
            if evidence == "logs":
                client.unavailable = "logs"
            else:
                original = client.download
                client.download = lambda url, limit, k=evidence: (
                    (
                        common.checkout_log(self.graph.a)
                        if k == "wrong-checkout"
                        else b"displayed head only\n"
                    )
                    if url.endswith("/logs")
                    else original(url, limit)
                )
            with (
                self.subTest(evidence=evidence),
                self.assertRaises(readiness.ReadinessError),
            ):
                self.collect(client)

    def test_workflow_execution_identity_and_contract_are_required(self):
        for kind in (
            "missing-reference",
            "wrong-reference",
            "different-contract",
            "incomplete-tree",
        ):
            client = self.client()

            def mutate(path, value, kind=kind):
                if path.endswith("/actions/runs/99"):
                    if kind == "missing-reference":
                        value.pop("referenced_workflows")
                    elif kind == "wrong-reference":
                        value["referenced_workflows"][0]["path"] = "other/workflow"
                elif "/git/trees/" in path:
                    if kind == "different-contract":
                        value["tree"][0]["sha"] = self.graph.a
                    elif kind == "incomplete-tree":
                        value["truncated"] = True

            client.mutation = mutate
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_head_base_or_merge_movement_during_collection(self):
        for kind in ("head", "base", "merge"):
            client = self.client()

            def mutate(path, value, c=client, kind=kind):
                if path.endswith(f"/pulls/{PR}") and c.pr_calls == 2:
                    if kind == "head":
                        value["head"]["sha"] = self.graph.c
                    elif kind == "base":
                        value["base"]["sha"] = self.graph.b
                    else:
                        value["merge_commit_sha"] = self.graph.broken

            client.mutation = mutate
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.collect(client)

    def test_record_requires_bound_qualification_and_cannot_downgrade_dispositions(self):
        record = self.collect(self.client())
        for kind in (
            "missing",
            "legacy",
            "wrong-base",
            "wrong-head",
            "wrong-tree",
            "wrong-run",
            "wrong-review",
            "wrong-pr",
        ):
            changed = deepcopy(record)
            proof = changed["source_qualification"]
            if kind == "missing":
                changed.pop("source_qualification")
            elif kind == "legacy":
                changed.pop("source_qualification")
                changed["schema_version"] = 2
            elif kind == "wrong-base":
                proof["base_sha"] = self.graph.b
            elif kind == "wrong-head":
                proof["tested_source_sha"] = self.graph.a
            elif kind == "wrong-tree":
                proof["merge_tree_sha"] = self.graph.a
            elif kind == "wrong-run":
                proof["source_jobs"][0]["run_id"] = 100
            elif kind == "wrong-review":
                proof["review_digest"] = "sha256:" + "0" * 64
            else:
                changed["pr"] = PR + 1
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                readiness.validate(changed)

    def test_changed_merge_base_after_collection_and_head_race_in_published_verification(
        self,
    ):
        client = self.client()
        client.publish(self.collect(client))
        client.base_sha, client.combined = self.graph.b, self.graph.broken
        with self.assertRaises(readiness.ReadinessError):
            self.verify(client)
        client = self.client()
        client.publish(self.collect(client))
        client.pr_calls = 0

        def mutate(path, value):
            if path.endswith(f"/pulls/{PR}") and client.pr_calls == 2:
                value["head"]["sha"] = self.graph.c

        client.mutation = mutate
        with self.assertRaises(readiness.ReadinessError):
            self.verify(client)

    def test_published_qualification_rechecks_logs_and_current_preview_jobs(self):
        for kind in ("logs", "source", "preview"):
            client = self.client()
            client.publish(self.collect(client))
            if kind == "logs":
                client.unavailable = "logs"
            else:
                run_id = 99 if kind == "source" else common.RUN_ID
                client.mutation = lambda path, value, r=run_id: (
                    value["jobs"][0].update(conclusion="failure")
                    if f"/runs/{r}/attempts/" in path
                    else None
                )
            with self.subTest(kind=kind), self.assertRaises(readiness.ReadinessError):
                self.verify(client)

    def test_published_historical_merge_does_not_rebind_to_later_main(self):
        client = self.client()
        client.publish(self.collect(client))
        client.base_sha = self.graph.b
        self.assertEqual(self.verify(client)["attestation_mode"], "premerge-published")

    def test_v3_requires_qualification_without_dispositions_and_legacy_is_read_only(self):
        client = self.client()
        client.reviews.pop(0)
        client.issue_comments.clear()
        record = self.collect(client)
        self.assertEqual(record["schema_version"], 3)
        del record["source_qualification"]
        with self.assertRaises(readiness.ReadinessError):
            readiness.validate(record)
        legacy = readiness.validate(common.evidence())
        self.assertEqual(legacy["schema_version"], 2)
        legacy["source_qualification"] = None
        with self.assertRaises(readiness.ReadinessError):
            readiness.validate(legacy)

    def test_release_read_adapter_verifies_v3_without_weakening_contextless_callers(self):
        from tests.release import test_deployment_continuation as legacy

        client = self.client()
        client.publish(self.collect(client))

        class Transport:
            def request(self, method, url, headers, body, timeout):
                parsed = urllib.parse.urlparse(url)
                if method == "POST" and parsed.path == "/graphql":
                    request = json.loads(body)
                    payload = {
                        "data": client.graphql(request["query"], request["variables"])
                    }
                elif method == "GET":
                    query = {
                        key: values[0]
                        for key, values in urllib.parse.parse_qs(parsed.query).items()
                    }
                    if parsed.path.endswith(("/reviews", "/comments", "/files", "/pulls")):
                        payload = (
                            client.pages(parsed.path)
                            if query.get("page", "1") == "1"
                            else []
                        )
                    else:
                        payload = client.get_json(parsed.path, query)
                else:
                    raise AssertionError(
                        "Only synthetic read-only metadata calls are allowed"
                    )
                return legacy.open_sync_pr.HttpResponse(
                    200, json.dumps(payload).encode("utf-8")
                )

        adapter = legacy.deployment_evidence.ReadClient(
            legacy.approval.GitHubClient(
                "https://api.github.com", "synthetic-token", transport=Transport()
            )
        )
        module = legacy.deployment_evidence.readiness
        with mock.patch.object(adapter, "download", side_effect=client.download):
            result = module.verify_published(
                adapter,
                REPOSITORY,
                PR,
                client.head,
                client.combined,
                allowed_mergers=["Anthony"],
            )
        self.assertEqual(result["attestation_mode"], "premerge-published")
        with self.assertRaisesRegex(module.ReadinessError, "unresolved serious"):
            module._validate_review_evidence(
                module._review_evidence(adapter, REPOSITORY, PR)
            )

    def test_supported_docs_profile_and_partial_runtime_skip(self):
        client = common.FakeGitHub()
        client.changed_files = ["docs/status.md"]
        original = client.get_json
        runtime = {
            f"Source test suite / {name}" for name in readiness.SOURCE_JOB_NAMES[3:-1]
        }

        def get_json(path, query=None):
            value = original(path, query)
            if "/attempts/" in path and path.endswith("/jobs"):
                for job in value["jobs"]:
                    if job["name"] in runtime:
                        job["conclusion"] = "skipped"
            return value

        client.get_json = get_json
        record = readiness.collect(client, REPOSITORY, PR, common.SHA)
        self.assertFalse(record["preview_required"])
        self.assertEqual(
            sum(
                job["conclusion"] == "skipped"
                for job in record["source_qualification"]["source_jobs"]
            ),
            5,
        )
        record["source_qualification"]["source_jobs"][0]["conclusion"] = "failure"
        with self.assertRaises(readiness.ReadinessError):
            readiness.validate(record)


if __name__ == "__main__":
    unittest.main()
