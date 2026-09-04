#!/usr/bin/env python3
"""Open the exact, mandatory platform release-ledger synchronization PR.

This helper is intentionally standard-library only.  Its sole remote mutation is
creating one pull request; it never updates refs, merges a pull request, or
modifies either ``main`` or an immutable release branch.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Protocol, Sequence, TextIO


SCHEMA_VERSION = 1
GET_ATTEMPTS = 3
HTTP_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPOSITORY_NAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9._-])?$"
)
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SAFE_OUTPUT_RE = re.compile(r"^[\x20-\x7e]+$")


class SyncPrError(ValueError):
    """A fail-closed error safe to show without exposing response bodies."""


class AmbiguousCreateError(SyncPrError):
    """The POST may have succeeded even though its response was not usable."""


@dataclasses.dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: int,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        request: urllib.request.Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        return None


def _bounded_read(stream: BinaryIO) -> bytes:
    body = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise OSError("GitHub response exceeded the fixed size limit")
    return body


class UrllibTransport:
    """urllib transport that refuses redirects so credentials cannot move hosts."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: int,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method=method
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, _bounded_read(response))
        except urllib.error.HTTPError as exc:
            try:
                response_body = _bounded_read(exc)
            finally:
                exc.close()
            return HttpResponse(exc.code, response_body)


def _validate_url(name: str, value: str, *, allow_path: bool) -> str:
    if (
        not value
        or len(value) > 2048
        or not value.isascii()
        or any(character.isspace() or ord(character) < 32 for character in value)
        or "\\" in value
        or "%" in value
    ):
        raise SyncPrError(f"Unsafe {name}")
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SyncPrError(f"Unsafe {name}") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
        or value.endswith("/")
        or not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname)
    ):
        raise SyncPrError(f"Unsafe {name}")
    if not allow_path and parsed.path:
        raise SyncPrError(f"Unsafe {name}")
    if parsed.path:
        if not parsed.path.startswith("/") or "//" in parsed.path:
            raise SyncPrError(f"Unsafe {name}")
        if any(part in {".", ".."} for part in parsed.path.split("/")):
            raise SyncPrError(f"Unsafe {name}")
        if not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@/-]+", parsed.path):
            raise SyncPrError(f"Unsafe {name}")
    return value


@dataclasses.dataclass(frozen=True)
class SyncConfig:
    owner: str
    repository: str
    version: str
    release_branch: str
    sync_branch: str
    source_sha: str
    release_commit: str
    api_url: str
    server_url: str
    output: Path

    @classmethod
    def validated(
        cls,
        *,
        owner: str,
        repository: str,
        version: str,
        release_branch: str,
        sync_branch: str,
        source_sha: str,
        release_commit: str,
        api_url: str,
        server_url: str,
        output: Path,
    ) -> "SyncConfig":
        if OWNER_RE.fullmatch(owner) is None:
            raise SyncPrError("Unsafe repository owner")
        parts = repository.split("/")
        if (
            len(parts) != 2
            or parts[0] != owner
            or REPOSITORY_NAME_RE.fullmatch(parts[1]) is None
            or parts[1] in {".", ".."}
        ):
            raise SyncPrError("Unsafe or inconsistent repository name")
        if VERSION_RE.fullmatch(version) is None:
            raise SyncPrError("Expected a strict platform semantic version")
        expected_release_branch = f"release/platform-v{version}"
        if release_branch != expected_release_branch:
            raise SyncPrError("Release branch does not match the platform version")
        expected_sync_branch = f"sync/platform-v{version}"
        if sync_branch != expected_sync_branch:
            raise SyncPrError("Sync branch does not match the platform version")
        if COMMIT_RE.fullmatch(source_sha) is None:
            raise SyncPrError("Source SHA must be a full lowercase Git object id")
        if COMMIT_RE.fullmatch(release_commit) is None:
            raise SyncPrError("Release commit must be a full lowercase Git object id")
        if len(source_sha) != len(release_commit):
            raise SyncPrError("Source and release object ids use different formats")
        if not isinstance(output, Path) or not output.name:
            raise SyncPrError("A report output path is required")
        return cls(
            owner=owner,
            repository=repository,
            version=version,
            release_branch=release_branch,
            sync_branch=sync_branch,
            source_sha=source_sha,
            release_commit=release_commit,
            api_url=_validate_url("GitHub API URL", api_url, allow_path=True),
            server_url=_validate_url(
                "GitHub server URL", server_url, allow_path=False
            ),
            output=output,
        )

    @property
    def manual_url(self) -> str:
        return (
            f"{self.server_url}/{self.repository}/compare/"
            f"main...{self.sync_branch}?expand=1"
        )


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _decode_json(response: HttpResponse, context: str) -> Any:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SyncPrError(f"GitHub returned invalid JSON for {context}") from exc


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        transport: Transport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not token
            or len(token) > 4096
            or "\n" in token
            or "\r" in token
            or any(ord(character) < 32 for character in token)
        ):
            raise SyncPrError("GITHUB_TOKEN must be a non-empty single-line secret")
        self.api_url = api_url
        self.token = token
        self.transport = transport or UrllibTransport()
        self.sleeper = sleeper

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "buh-platform-release-sync/1",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def get(self, path: str, query: Mapping[str, str] | None = None) -> Any:
        url = self._url(path, query)
        for attempt in range(GET_ATTEMPTS):
            try:
                response = self.transport.request(
                    "GET",
                    url,
                    self.headers,
                    None,
                    HTTP_TIMEOUT_SECONDS,
                )
            except (OSError, TimeoutError, urllib.error.URLError):
                if attempt + 1 == GET_ATTEMPTS:
                    raise SyncPrError(
                        "GitHub GET failed after bounded retries"
                    ) from None
            else:
                if response.status == 200:
                    return _decode_json(response, "GET")
                retryable = response.status in {408, 425, 429} or (
                    500 <= response.status <= 599
                )
                if not retryable or attempt + 1 == GET_ATTEMPTS:
                    raise SyncPrError(
                        f"GitHub GET failed with HTTP {response.status}"
                    )
            self.sleeper(0.25 * (2**attempt))
        raise SyncPrError("GitHub GET exhausted its fixed attempt limit")

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        body = _canonical_json_bytes(payload)
        try:
            response = self.transport.request(
                "POST",
                self._url(path),
                {**self.headers, "Content-Type": "application/json"},
                body,
                HTTP_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError, urllib.error.URLError):
            raise AmbiguousCreateError(
                "GitHub did not return a conclusive pull-request creation response"
            ) from None
        if response.status == 201:
            try:
                return _decode_json(response, "pull-request creation")
            except SyncPrError as exc:
                raise AmbiguousCreateError(str(exc)) from exc
        if response.status in {408, 422, 425, 429} or 500 <= response.status <= 599:
            raise AmbiguousCreateError(
                f"GitHub returned ambiguous HTTP {response.status} after creation"
            )
        raise SyncPrError(
            f"GitHub denied pull-request creation with HTTP {response.status}"
        )


def _api_path(config: SyncConfig, suffix: str) -> str:
    return f"/repos/{config.repository}/{suffix}"


def _read_ref(
    client: GitHubClient,
    config: SyncConfig,
    ref: str,
) -> str:
    encoded_ref = urllib.parse.quote(ref.removeprefix("refs/"), safe="/")
    response = client.get(_api_path(config, f"git/ref/{encoded_ref}"))
    if not isinstance(response, dict):
        raise SyncPrError("GitHub returned a malformed Git reference")
    obj = response.get("object")
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if (
        response.get("ref") != ref
        or not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or not isinstance(sha, str)
        or COMMIT_RE.fullmatch(sha) is None
        or len(sha) != len(config.source_sha)
    ):
        raise SyncPrError(f"GitHub returned a malformed remote {ref}")
    return sha


def _verify_exact_ref(
    client: GitHubClient,
    config: SyncConfig,
    ref: str,
    expected_sha: str,
) -> None:
    if _read_ref(client, config, ref) != expected_sha:
        raise SyncPrError(f"Remote {ref} does not match the expected exact commit")


def _verify_release_parent(client: GitHubClient, config: SyncConfig) -> None:
    response = client.get(
        _api_path(config, f"git/commits/{config.release_commit}")
    )
    parents = response.get("parents") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("sha") != config.release_commit
        or not isinstance(parents, list)
        or len(parents) != 1
        or not isinstance(parents[0], dict)
        or parents[0].get("sha") != config.source_sha
    ):
        raise SyncPrError(
            "Release commit is not the exact single-parent child of the source"
        )


def _verify_main_descends_from_source(
    client: GitHubClient, config: SyncConfig, main_sha: str
) -> None:
    if main_sha == config.source_sha:
        return
    comparison = client.get(
        _api_path(config, f"compare/{config.source_sha}...{main_sha}")
    )
    base = comparison.get("base_commit") if isinstance(comparison, dict) else None
    merge_base = (
        comparison.get("merge_base_commit")
        if isinstance(comparison, dict)
        else None
    )
    ahead_by = comparison.get("ahead_by") if isinstance(comparison, dict) else None
    behind_by = comparison.get("behind_by") if isinstance(comparison, dict) else None
    if (
        not isinstance(comparison, dict)
        or comparison.get("status") != "ahead"
        or type(ahead_by) is not int
        or ahead_by < 1
        or behind_by != 0
        or not isinstance(base, dict)
        or base.get("sha") != config.source_sha
        or not isinstance(merge_base, dict)
        or merge_base.get("sha") != config.source_sha
    ):
        raise SyncPrError(
            "Current main does not descend from the exact release source commit"
        )


def _verify_remote_state(client: GitHubClient, config: SyncConfig) -> str:
    main_sha = _read_ref(client, config, "refs/heads/main")
    _verify_exact_ref(
        client,
        config,
        f"refs/heads/{config.release_branch}",
        config.release_commit,
    )
    _verify_exact_ref(
        client,
        config,
        f"refs/heads/{config.sync_branch}",
        config.release_commit,
    )
    _verify_release_parent(client, config)
    _verify_main_descends_from_source(client, config, main_sha)
    return main_sha


def _pulls_path(config: SyncConfig) -> str:
    return _api_path(config, "pulls")


def _validate_pr(pr: Any, config: SyncConfig) -> dict[str, Any]:
    if not isinstance(pr, dict):
        raise SyncPrError("GitHub returned a malformed synchronization PR")
    number = pr.get("number")
    expected_url = (
        f"{config.server_url}/{config.repository}/pull/{number}"
        if type(number) is int and number >= 1
        else ""
    )
    head = pr.get("head")
    base = pr.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if (
        type(number) is not int
        or number < 1
        or pr.get("state") != "open"
        or pr.get("draft") is not False
        or pr.get("auto_merge") is not None
        or pr.get("maintainer_can_modify") is not False
        or not isinstance(head, dict)
        or head.get("ref") != config.sync_branch
        or head.get("sha") != config.release_commit
        or not isinstance(head_repo, dict)
        or head_repo.get("full_name") != config.repository
        or not isinstance(base, dict)
        or base.get("ref") != "main"
        or not isinstance(base_repo, dict)
        or base_repo.get("full_name") != config.repository
        or pr.get("html_url") != expected_url
    ):
        raise SyncPrError(
            "GitHub did not return the exact open, non-draft same-repository PR"
        )
    return pr


def _listed_pr_number(pr: Any, config: SyncConfig) -> int:
    """Validate fields guaranteed by the list representation and return its id."""
    if not isinstance(pr, dict):
        raise SyncPrError("GitHub returned a malformed synchronization PR list item")
    number = pr.get("number")
    head = pr.get("head")
    base = pr.get("base")
    head_repo = head.get("repo") if isinstance(head, dict) else None
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if (
        type(number) is not int
        or number < 1
        or pr.get("state") != "open"
        or not isinstance(head, dict)
        or head.get("ref") != config.sync_branch
        or head.get("sha") != config.release_commit
        or not isinstance(head_repo, dict)
        or head_repo.get("full_name") != config.repository
        or not isinstance(base, dict)
        or base.get("ref") != "main"
        or not isinstance(base_repo, dict)
        or base_repo.get("full_name") != config.repository
    ):
        raise SyncPrError(
            "GitHub did not list the exact same-repository synchronization PR"
        )
    return number


def _list_exact_pr(client: GitHubClient, config: SyncConfig) -> dict[str, Any] | None:
    response = client.get(
        _pulls_path(config),
        {
            "state": "open",
            "base": "main",
            "head": f"{config.owner}:{config.sync_branch}",
            "per_page": "100",
        },
    )
    if not isinstance(response, list):
        raise SyncPrError("GitHub returned a malformed pull-request list")
    if len(response) > 1:
        raise SyncPrError("GitHub returned more than one synchronization PR")
    if not response:
        return None
    # GitHub's list representation does not include every field needed for the
    # policy check (notably maintainer_can_modify).  Resolve the candidate to
    # the full pull-request representation before accepting or reusing it.
    number = _listed_pr_number(response[0], config)
    return _validate_pr(
        client.get(_api_path(config, f"pulls/{number}")),
        config,
    )


def _create_payload(config: SyncConfig) -> dict[str, Any]:
    body = (
        "This pull request synchronizes the immutable platform release ledger.\n\n"
        "Approve the workflow run GitHub queues for this automated PR, then "
        "merge it with a merge commit after every required Source CI check "
        "passes; do not squash or rebase it.\n\n"
        f"Source commit: {config.source_sha}\n"
        f"Release commit: {config.release_commit}\n\n"
        "Production has not been deployed by this release workflow.\n"
    )
    return {
        "base": "main",
        "body": body,
        "draft": False,
        "head": config.sync_branch,
        "maintainer_can_modify": False,
        "title": f"Sync platform release v{config.version}",
    }


def open_sync_pr(
    config: SyncConfig,
    token: str,
    *,
    transport: Transport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    client = GitHubClient(
        config.api_url, token, transport=transport, sleeper=sleeper
    )
    _verify_remote_state(client, config)
    pr = _list_exact_pr(client, config)
    if pr is not None:
        action = "reused"
    else:
        try:
            pr = _validate_pr(
                client.post(_pulls_path(config), _create_payload(config)), config
            )
            action = "created"
        except AmbiguousCreateError:
            pr = _list_exact_pr(client, config)
            if pr is None:
                raise SyncPrError(
                    "Ambiguous creation could not be recovered as one exact PR"
                ) from None
            action = "recovered"
    # Close the race where main or either just-published ref moves while the API
    # operation is in flight, then read the PR itself again.  Never report a PR
    # from the earlier list/POST response after it has been closed, drafted,
    # retargeted, or moved to a different head.
    main_sha = _verify_remote_state(client, config)
    pr = _validate_pr(
        client.get(_api_path(config, f"pulls/{pr['number']}")),
        config,
    )
    return {
        "action": action,
        "manual_compare_url": config.manual_url,
        "platform_version": config.version,
        "pull_request": {
            "number": pr["number"],
            "url": pr["html_url"],
        },
        "main_commit": main_sha,
        "release_branch": config.release_branch,
        "release_commit": config.release_commit,
        "repository": config.repository,
        "schema_version": SCHEMA_VERSION,
        "source_commit": config.source_sha,
        "sync_branch": config.sync_branch,
    }


def _write_report(path: Path, encoded: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise SyncPrError("Could not write the canonical PR report") from exc


def _append_output(path: str | None, report: Mapping[str, Any]) -> None:
    if not path:
        return
    values = {
        "action": report["action"],
        "number": str(report["pull_request"]["number"]),
        "url": report["pull_request"]["url"],
    }
    if any(
        not isinstance(value, str) or SAFE_OUTPUT_RE.fullmatch(value) is None
        for value in values.values()
    ):
        raise SyncPrError("Refusing to write an unsafe GitHub output value")
    try:
        with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    except OSError as exc:
        raise SyncPrError("Could not append GitHub workflow outputs") from exc


def _append_summary(path: str | None, text: str) -> None:
    if not path:
        return
    try:
        with Path(path).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except OSError as exc:
        raise SyncPrError("Could not append the GitHub step summary") from exc


def _success_summary(config: SyncConfig, report: Mapping[str, Any]) -> str:
    return (
        "### Mandatory release synchronization PR ready\n\n"
        f"- Pull request: {report['pull_request']['url']}\n"
        f"- Exact head: {config.release_commit}\n"
        f"- Result: {report['action']}\n\n"
        "A maintainer must approve the queued workflows.\n"
        "Merge only after Source CI passes. Production was not deployed.\n"
    )


def _failure_summary(config: SyncConfig | None, reason: str) -> str:
    manual_url = config.manual_url if config is not None else "unavailable"
    release_commit = config.release_commit if config is not None else "unavailable"
    return (
        "### Release published; synchronization PR still required\n\n"
        "The helper did not create or confirm the exact synchronization PR.\n\n"
        f"- Manual PR: {manual_url}\n"
        f"- Release commit: {release_commit}\n"
        f"- Failure: {reason}\n\n"
        "Later releases fail closed until this release is merged into main "
        "with a merge commit.\n"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open the exact platform release synchronization PR"
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-branch", required=True)
    parser.add_argument("--sync-branch", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    config: SyncConfig | None = None
    try:
        args = _parser().parse_args(argv)
        config = SyncConfig.validated(
            owner=args.owner,
            repository=args.repository,
            version=args.version,
            release_branch=args.release_branch,
            sync_branch=args.sync_branch,
            source_sha=args.source_sha,
            release_commit=args.release_commit,
            api_url=args.api_url,
            server_url=args.server_url,
            output=args.output,
        )
        token = environment.get("GITHUB_TOKEN", "")
        report = open_sync_pr(
            config, token, transport=transport, sleeper=sleeper
        )
        encoded = _canonical_json_bytes(report)
        _write_report(config.output, encoded)
        _append_output(environment.get("GITHUB_OUTPUT"), report)
        _append_summary(
            environment.get("GITHUB_STEP_SUMMARY"),
            _success_summary(config, report),
        )
        stdout.write(encoded.decode("utf-8"))
        return 0
    except SyncPrError as exc:
        reason = str(exc)
    except Exception:
        # Never render arbitrary exception text: transports can include request
        # headers (and therefore the token) in exception representations.
        reason = "Unexpected synchronization helper failure"
    try:
        _append_summary(
            environment.get("GITHUB_STEP_SUMMARY"),
            _failure_summary(config, reason),
        )
    except SyncPrError:
        pass
    stderr.write(f"sync PR error: {reason}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
