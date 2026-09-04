from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ops" / "release"))

import open_sync_pr as sync  # noqa: E402


SOURCE = "1" * 40
RELEASE = "2" * 40
ADVANCED = "3" * 40
TOKEN = "secret-token-that-must-never-appear"


def _json_response(status: int, value: Any) -> sync.HttpResponse:
    return sync.HttpResponse(
        status,
        (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"),
    )


def _ref(name: str, sha: str) -> dict[str, Any]:
    return {"ref": name, "object": {"sha": sha, "type": "commit"}}


def _pr(number: int = 17, *, head_sha: str = RELEASE) -> dict[str, Any]:
    return {
        "auto_merge": None,
        "base": {"ref": "main", "repo": {"full_name": "Fifty5D/B-UH-AllianceAuth"}},
        "draft": False,
        "head": {
            "ref": "sync/platform-v0.4.4",
            "repo": {"full_name": "Fifty5D/B-UH-AllianceAuth"},
            "sha": head_sha,
        },
        "html_url": f"https://github.com/Fifty5D/B-UH-AllianceAuth/pull/{number}",
        "maintainer_can_modify": False,
        "number": number,
        "state": "open",
    }


def _listed_pr(number: int = 17, *, head_sha: str = RELEASE) -> dict[str, Any]:
    """Return the fields provided by GitHub's pull-request list shape."""
    return {
        key: value
        for key, value in _pr(number, head_sha=head_sha).items()
        if key not in {"auto_merge", "maintainer_can_modify"}
    }


class FakeTransport:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, Mapping[str, str], bytes | None, int]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: int,
    ) -> sync.HttpResponse:
        self.calls.append((method, url, headers, body, timeout))
        if not self.script:
            raise AssertionError("unexpected HTTP request")
        response = self.script.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class SyncPrTests(unittest.TestCase):
    def config(self, output: Path) -> sync.SyncConfig:
        return sync.SyncConfig.validated(
            owner="Fifty5D",
            repository="Fifty5D/B-UH-AllianceAuth",
            version="0.4.4",
            release_branch="release/platform-v0.4.4",
            sync_branch="sync/platform-v0.4.4",
            source_sha=SOURCE,
            release_commit=RELEASE,
            api_url="https://api.github.com",
            server_url="https://github.com",
            output=output,
        )

    @staticmethod
    def state(main: str = SOURCE) -> list[sync.HttpResponse]:
        responses = [
            _json_response(200, _ref("refs/heads/main", main)),
            _json_response(
                200, _ref("refs/heads/release/platform-v0.4.4", RELEASE)
            ),
            _json_response(
                200, _ref("refs/heads/sync/platform-v0.4.4", RELEASE)
            ),
            _json_response(
                200, {"sha": RELEASE, "parents": [{"sha": SOURCE}]}
            ),
        ]
        if main != SOURCE:
            responses.append(
                _json_response(
                    200,
                    {
                        "ahead_by": 1,
                        "base_commit": {"sha": SOURCE},
                        "behind_by": 0,
                        "merge_base_commit": {"sha": SOURCE},
                        "status": "ahead",
                    },
                )
            )
        return responses

    def test_create_success_posts_once_with_locked_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            script = self.state() + [
                _json_response(200, []),
                _json_response(201, _pr()),
            ] + self.state() + [_json_response(200, _pr())]
            transport = FakeTransport(script)
            report = sync.open_sync_pr(
                config, TOKEN, transport=transport, sleeper=lambda _: None
            )
            self.assertEqual(report["action"], "created")
            post = [call for call in transport.calls if call[0] == "POST"]
            self.assertEqual(len(post), 1)
            payload = json.loads(post[0][3] or b"null")
            self.assertEqual(payload["base"], "main")
            self.assertEqual(payload["head"], config.sync_branch)
            self.assertIs(payload["draft"], False)
            self.assertIs(payload["maintainer_can_modify"], False)
            self.assertEqual(post[0][4], sync.HTTP_TIMEOUT_SECONDS)

    def test_reuses_one_exact_open_pr_without_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            script = (
                self.state()
                + [
                    _json_response(200, [_listed_pr(19)]),
                    _json_response(200, _pr(19)),
                ]
                + self.state()
                + [_json_response(200, _pr(19))]
            )
            transport = FakeTransport(script)
            report = sync.open_sync_pr(
                config, TOKEN, transport=transport, sleeper=lambda _: None
            )
            self.assertEqual(report["action"], "reused")
            self.assertEqual(report["pull_request"]["number"], 19)
            self.assertFalse(any(call[0] == "POST" for call in transport.calls))

    def test_allows_main_to_advance_when_source_remains_its_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            script = self.state(ADVANCED) + [
                _json_response(200, []),
                _json_response(201, _pr()),
            ] + self.state(ADVANCED) + [_json_response(200, _pr())]
            report = sync.open_sync_pr(
                config,
                TOKEN,
                transport=FakeTransport(script),
                sleeper=lambda _: None,
            )
            self.assertEqual(report["main_commit"], ADVANCED)

    def test_rejects_main_that_diverged_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            divergent = self.state(ADVANCED)
            divergent[-1] = _json_response(
                200,
                {
                    "ahead_by": 1,
                    "base_commit": {"sha": SOURCE},
                    "behind_by": 1,
                    "merge_base_commit": {"sha": "4" * 40},
                    "status": "diverged",
                },
            )
            with self.assertRaisesRegex(sync.SyncPrError, "does not descend"):
                sync.open_sync_pr(
                    config,
                    TOKEN,
                    transport=FakeTransport(divergent),
                    sleeper=lambda _: None,
                )

    def test_final_pr_read_rejects_state_changed_after_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            closed = {**_pr(), "state": "closed"}
            script = self.state() + [
                _json_response(200, []),
                _json_response(201, _pr()),
            ] + self.state() + [_json_response(200, closed)]
            with self.assertRaisesRegex(sync.SyncPrError, "exact open"):
                sync.open_sync_pr(
                    config,
                    TOKEN,
                    transport=FakeTransport(script),
                    sleeper=lambda _: None,
                )

    def test_rejects_auto_merge_on_initial_or_final_pr_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            auto_merge = {
                **_pr(),
                "auto_merge": {"merge_method": "merge"},
            }
            scenarios = {
                "initial": self.state()
                + [
                    _json_response(200, [_listed_pr()]),
                    _json_response(200, auto_merge),
                ],
                "final": self.state()
                + [_json_response(200, []), _json_response(201, _pr())]
                + self.state()
                + [_json_response(200, auto_merge)],
            }
            for name, script in scenarios.items():
                with self.subTest(name=name), self.assertRaisesRegex(
                    sync.SyncPrError, "exact open"
                ):
                    sync.open_sync_pr(
                        config,
                        TOKEN,
                        transport=FakeTransport(script),
                        sleeper=lambda _: None,
                    )

    def test_rejects_existing_pr_with_wrong_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            transport = FakeTransport(
                self.state()
                + [
                    _json_response(200, [_listed_pr()]),
                    _json_response(200, _pr(head_sha="4" * 40)),
                ]
            )
            with self.assertRaisesRegex(sync.SyncPrError, "exact open"):
                sync.open_sync_pr(
                    config, TOKEN, transport=transport, sleeper=lambda _: None
                )
            self.assertFalse(any(call[0] == "POST" for call in transport.calls))

    def test_create_denial_fails_without_retry_or_recovery_post(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            transport = FakeTransport(
                self.state()
                + [_json_response(200, []), _json_response(403, {"message": TOKEN})]
            )
            with self.assertRaisesRegex(sync.SyncPrError, "HTTP 403"):
                sync.open_sync_pr(
                    config, TOKEN, transport=transport, sleeper=lambda _: None
                )
            self.assertEqual(
                len([call for call in transport.calls if call[0] == "POST"]), 1
            )

    def test_ambiguous_post_recovers_only_one_exact_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            script = self.state() + [
                _json_response(200, []),
                OSError("response lost"),
                _json_response(200, [_listed_pr(23)]),
                _json_response(200, _pr(23)),
            ] + self.state() + [_json_response(200, _pr(23))]
            transport = FakeTransport(script)
            report = sync.open_sync_pr(
                config, TOKEN, transport=transport, sleeper=lambda _: None
            )
            self.assertEqual(report["action"], "recovered")
            self.assertEqual(report["pull_request"]["number"], 23)
            self.assertEqual(
                len([call for call in transport.calls if call[0] == "POST"]), 1
            )

    def test_422_race_recovers_only_by_relisting_one_exact_pr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            script = self.state() + [
                _json_response(200, []),
                _json_response(422, {"message": "already exists"}),
                _json_response(200, [_listed_pr(29)]),
                _json_response(200, _pr(29)),
            ] + self.state() + [_json_response(200, _pr(29))]
            transport = FakeTransport(script)
            report = sync.open_sync_pr(
                config, TOKEN, transport=transport, sleeper=lambda _: None
            )
            self.assertEqual(report["action"], "recovered")
            self.assertEqual(report["pull_request"]["number"], 29)
            self.assertEqual(
                len([call for call in transport.calls if call[0] == "POST"]), 1
            )

    def test_cli_output_is_canonical_and_failure_redacts_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "sync.json"
            github_output = root / "github-output.txt"
            summary = root / "summary.md"
            environment = {
                **os.environ,
                "GITHUB_TOKEN": TOKEN,
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            argv = [
                "--owner",
                "Fifty5D",
                "--repository",
                "Fifty5D/B-UH-AllianceAuth",
                "--version",
                "0.4.4",
                "--release-branch",
                "release/platform-v0.4.4",
                "--sync-branch",
                "sync/platform-v0.4.4",
                "--source-sha",
                SOURCE,
                "--release-commit",
                RELEASE,
                "--api-url",
                "https://api.github.com",
                "--server-url",
                "https://github.com",
                "--output",
                str(output),
            ]
            script = self.state() + [
                _json_response(200, []),
                _json_response(201, _pr()),
            ] + self.state() + [_json_response(200, _pr())]
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = sync.main(
                argv,
                environ=environment,
                transport=FakeTransport(script),
                sleeper=lambda _: None,
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(code, 0, stderr.getvalue())
            report = json.loads(stdout.getvalue())
            expected = sync._canonical_json_bytes(report)
            self.assertEqual(stdout.getvalue().encode("utf-8"), expected)
            self.assertEqual(output.read_bytes(), expected)
            outputs = github_output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(outputs[0], "action=created")
            self.assertEqual(outputs[1], "number=17")
            self.assertEqual(outputs[2], f"url={_pr()['html_url']}")
            self.assertIn("must approve", summary.read_text(encoding="utf-8"))
            combined = stdout.getvalue() + stderr.getvalue() + summary.read_text()
            self.assertNotIn(TOKEN, combined)

            failure_summary = root / "failure-summary.md"
            failure_stderr = io.StringIO()
            hostile = FakeTransport([RuntimeError(f"transport leaked {TOKEN}")])
            failed = sync.main(
                argv,
                environ={
                    **environment,
                    "GITHUB_STEP_SUMMARY": str(failure_summary),
                    "GITHUB_OUTPUT": str(root / "unused-output.txt"),
                },
                transport=hostile,
                sleeper=lambda _: None,
                stdout=io.StringIO(),
                stderr=failure_stderr,
            )
            self.assertEqual(failed, 2)
            failure_text = failure_summary.read_text(encoding="utf-8")
            self.assertEqual(
                failure_text.count(
                    "### Release published; synchronization PR still required"
                ),
                1,
            )
            self.assertIn(self.config(output).manual_url, failure_text)
            self.assertNotIn(TOKEN, failure_text + failure_stderr.getvalue())

    def test_query_is_strictly_scoped_to_owner_branch_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = self.config(Path(temp) / "report.json")
            transport = FakeTransport(
                self.state() + [_json_response(200, [_pr(head_sha="4" * 40)])]
            )
            with self.assertRaises(sync.SyncPrError):
                sync.open_sync_pr(
                    config, TOKEN, transport=transport, sleeper=lambda _: None
                )
            list_url = transport.calls[4][1]
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(list_url).query)
            self.assertEqual(query["state"], ["open"])
            self.assertEqual(query["base"], ["main"])
            self.assertEqual(
                query["head"], ["Fifty5D:sync/platform-v0.4.4"]
            )


if __name__ == "__main__":
    unittest.main()
