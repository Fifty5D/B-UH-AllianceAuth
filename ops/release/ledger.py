#!/usr/bin/env python3
"""Verify the immutable platform-release ledger against an exact source commit.

The helper deliberately uses only the Python standard library.  Remote GitHub
authentication is passed to child Git processes through their environment, so
the token never appears in an argument, report, Git configuration file, or
diagnostic emitted by this module.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

try:  # Support both package imports and direct script execution.
    from .buh_release import (
        ReleaseError,
        SemVer,
        VERSION_ASSIGNMENT_RE,
        _source_requires_dist,
        _validate_internal_dependency_contracts,
        IGNORED_PARTS,
        PLATFORM_INPUT_IGNORED_PARTS,
        app_input_digest,
        create_plan_v1,
        deployment_payload_identity,
        git_blob_object_id,
        inspect_wheel,
        load_changes,
        load_compatibility,
        load_registry,
        normalize_distribution,
        platform_input_digest,
        read_app_version,
        render_release_readme_v1,
        sha256_file,
        tool_input_digest,
        verify_release_dir,
    )
except ImportError:  # pragma: no cover - exercised by the CLI integration test.
    from buh_release import (
        ReleaseError,
        SemVer,
        VERSION_ASSIGNMENT_RE,
        _source_requires_dist,
        _validate_internal_dependency_contracts,
        IGNORED_PARTS,
        PLATFORM_INPUT_IGNORED_PARTS,
        app_input_digest,
        create_plan_v1,
        deployment_payload_identity,
        git_blob_object_id,
        inspect_wheel,
        load_changes,
        load_compatibility,
        load_registry,
        normalize_distribution,
        platform_input_digest,
        read_app_version,
        render_release_readme_v1,
        sha256_file,
        tool_input_digest,
        verify_release_dir,
    )


LEDGER_SCHEMA_VERSION = 1
REMOTE_REF_PREFIX = "refs/heads/release/platform-v"
PRIVATE_REF_NAMESPACE = "refs/buh-release-ledger/releases/"
PRIVATE_REF_PREFIX = f"{PRIVATE_REF_NAMESPACE}v"
DEFAULT_RELEASE_ROOT = "releases/platform"
BUMP_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}
MAX_SNAPSHOT_FILES = 100_000
MAX_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_SNAPSHOT_FILE_BYTES = 256 * 1024 * 1024
CHECKOUT_TRANSFORM_ATTRIBUTES = (
    "text",
    "eol",
    "crlf",
    "working-tree-encoding",
    "filter",
    "ident",
)
AMBIGUOUS_ATTRIBUTE_ASSIGNMENT_RE = (
    r"(^|[[:space:]])"
    r"(text|eol|crlf|working-tree-encoding|filter|ident)="
    r"(unset|unspecified)($|[[:space:]])"
)

# Exact immutable releases created by the former deployment-equivalence bridge.
# They remain replayable for append-only ledger history, while every other
# non-immediate predecessor is rejected.
LEGACY_DEPLOYMENT_BRIDGES = {
    "0.5.2": {
        "manifest_sha256": (
            "76d8eb70c23f6145dba1264bc14f4719dee74dace3e66e11e4a00e7f256b981c"
        ),
        "release_commit": "d5e8db7e393f9596b98668ba9b50c8bb47bb3255",
        "source_commit": "c282fa9ece625778b9100037636814909073607b",
    },
    "0.5.3": {
        "manifest_sha256": (
            "05c166a49c2b32501955326a6c29a80ed574604cc603975849289d18a4437270"
        ),
        "release_commit": "8827ac8ed9166e1baf67898b90c0f6fedfad1a51",
        "source_commit": "f03a142004b31550b3b697a5b79ac5a592c1f975",
    },
}

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RELEASE_REF_RE = re.compile(
    r"^refs/heads/release/platform-v"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
RELEASE_DIR_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class LedgerError(ReleaseError):
    """Raised when the remote and source release ledgers do not agree."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _validate_release_root(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise LedgerError(f"Unsafe release root: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", "..", ".git"} for part in path.parts)
    ):
        raise LedgerError(f"Unsafe release root: {value!r}")
    return value


def _object_id_pattern(object_format: str) -> re.Pattern[str]:
    if object_format == "sha1":
        return re.compile(r"^[0-9a-f]{40}$")
    if object_format == "sha256":
        return re.compile(r"^[0-9a-f]{64}$")
    raise LedgerError(f"Unsupported Git object format: {object_format!r}")


def _validate_registry_git_blob_ids(
    registry: Any,
    object_id_re: re.Pattern[str],
    *,
    context: str,
) -> None:
    for dependency in registry.dependencies:
        git_blob_sha = dependency.git_blob_sha
        if git_blob_sha is not None and not object_id_re.fullmatch(git_blob_sha):
            raise LedgerError(
                f"{context} dependency Git blob identity does not match the "
                f"repository object format: {dependency.component}"
            )


def _validate_third_party_artifact_git_blob_ids(
    manifest: Mapping[str, Any],
    object_id_re: re.Pattern[str],
    *,
    context: str,
) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise LedgerError("Verified release manifest has invalid artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or artifact.get("kind") != "third-party":
            continue
        git_blob_sha = artifact.get("git_blob_sha")
        if git_blob_sha is not None and not object_id_re.fullmatch(git_blob_sha):
            raise LedgerError(
                f"{context} third-party artifact Git blob identity does not match "
                f"the repository object format: {artifact.get('component')}"
            )


def _git_environment(token: str) -> dict[str, str]:
    if not token or "\n" in token or "\r" in token:
        raise LedgerError("GITHUB_TOKEN must be a non-empty, single-line secret")
    credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode(
        "ascii"
    )
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credential}"
    return environment


def _run_git(
    root: Path,
    arguments: Sequence[str],
    *,
    operation: str,
    environment: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=dict(environment) if environment is not None else None,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise LedgerError(f"Git {operation} could not be completed") from exc
    if check and result.returncode != 0:
        # Do not echo the command or Git's remote diagnostics. Either can contain
        # credentials supplied by repository configuration outside this helper.
        raise LedgerError(
            f"Git {operation} failed with exit code {result.returncode}"
        )
    return result


def _run_git_bytes(
    root: Path,
    arguments: Sequence[str],
    *,
    operation: str,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise LedgerError(f"Git {operation} could not be completed") from exc
    if result.returncode != 0:
        raise LedgerError(
            f"Git {operation} failed with exit code {result.returncode}"
        )
    return result.stdout


def _resolve_commit(
    root: Path, revision: str, object_id_re: re.Pattern[str]
) -> str:
    result = _run_git(
        root,
        ["rev-parse", "--verify", f"{revision}^{{commit}}"],
        operation="commit resolution",
    )
    commit = result.stdout.strip()
    if not object_id_re.fullmatch(commit):
        raise LedgerError(f"Git returned an invalid commit object id for {revision!r}")
    return commit


def _tree_id(
    root: Path,
    commit: str,
    path: str,
    object_id_re: re.Pattern[str],
) -> str | None:
    result = _run_git(
        root,
        ["rev-parse", "--verify", f"{commit}:{path}"],
        operation="tree resolution",
        check=False,
    )
    if result.returncode != 0:
        return None
    object_id = result.stdout.strip()
    if not object_id_re.fullmatch(object_id):
        raise LedgerError(f"Git returned an invalid object id for {path}")
    kind = _run_git(
        root,
        ["cat-file", "-t", object_id],
        operation="tree type inspection",
    ).stdout.strip()
    if kind != "tree":
        raise LedgerError(f"Release path is not a Git tree: {path}")
    return object_id


def _release_versions_at_commit(
    root: Path,
    commit: str,
    release_root: str,
    object_id_re: re.Pattern[str],
) -> dict[SemVer, str]:
    root_tree = _tree_id(root, commit, release_root, object_id_re)
    if root_tree is None:
        return {}
    result = _run_git(
        root,
        ["ls-tree", "-z", root_tree],
        operation="source release-ledger inspection",
    )
    versions: dict[SemVer, str] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        try:
            metadata, name = record.split("\t", 1)
            mode, kind, object_id = metadata.split(" ", 2)
        except ValueError as exc:
            raise LedgerError("Git returned malformed release-ledger tree data") from exc
        match = RELEASE_DIR_RE.fullmatch(name)
        if match is None:
            raise LedgerError(
                f"Source release ledger contains a non-version entry: {name!r}"
            )
        if mode != "040000" or kind != "tree" or not object_id_re.fullmatch(object_id):
            raise LedgerError(f"Source release entry is not a regular tree: {name}")
        version = SemVer(*(int(part) for part in match.groups()))
        if version in versions:
            raise LedgerError(f"Source release ledger repeats version {version}")
        versions[version] = object_id
    return versions


def _list_remote_releases(
    root: Path,
    remote: str,
    environment: Mapping[str, str],
    object_id_re: re.Pattern[str],
) -> dict[SemVer, tuple[str, str]]:
    result = _run_git(
        root,
        [
            "ls-remote",
            "--heads",
            "--refs",
            remote,
            f"{REMOTE_REF_PREFIX}*",
        ],
        operation="remote release-ref discovery",
        environment=environment,
    )
    releases: dict[SemVer, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        try:
            object_id, ref = line.split("\t", 1)
        except ValueError as exc:
            raise LedgerError("Git returned malformed remote release-ref data") from exc
        match = RELEASE_REF_RE.fullmatch(ref)
        if match is None:
            raise LedgerError(f"Malformed platform release ref on remote: {ref!r}")
        if not object_id_re.fullmatch(object_id):
            raise LedgerError(f"Remote release ref has an invalid object id: {ref}")
        version = SemVer(*(int(part) for part in match.groups()))
        if version in releases:
            raise LedgerError(f"Remote repeats platform release version {version}")
        releases[version] = (ref, object_id)
    return releases


def _fetch_remote_releases(
    root: Path,
    remote: str,
    releases: Mapping[SemVer, tuple[str, str]],
    environment: Mapping[str, str],
    object_id_re: re.Pattern[str],
) -> dict[SemVer, str]:
    # One authenticated wildcard fetch keeps verification constant-round-trip as
    # the append-only ledger grows.  Both surrounding ls-remote snapshots are
    # strict, and the private namespace is independently enumerated below, so a
    # malformed or racing ref cannot be accepted through the wildcard.
    _run_git(
        root,
        [
            "fetch",
            "--force",
            "--prune",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            remote,
            f"+{REMOTE_REF_PREFIX}*:{PRIVATE_REF_PREFIX}*",
        ],
        operation="exact platform release-ref namespace fetch",
        environment=environment,
    )
    result = _run_git(
        root,
        [
            "for-each-ref",
            "--format=%(objectname)%09%(refname)",
            PRIVATE_REF_NAMESPACE,
        ],
        operation="private release-ref namespace inspection",
    )
    local_ref_re = re.compile(
        r"^refs/buh-release-ledger/releases/v"
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    local_refs: dict[SemVer, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        try:
            object_id, local_ref = line.split("\t", 1)
        except ValueError as exc:
            raise LedgerError("Git returned malformed private release-ref data") from exc
        match = local_ref_re.fullmatch(local_ref)
        if match is None or not object_id_re.fullmatch(object_id):
            raise LedgerError(
                "Private release-ref namespace contains malformed or stale state"
            )
        version = SemVer(*(int(part) for part in match.groups()))
        if version in local_refs:
            raise LedgerError(f"Private namespace repeats release version {version}")
        local_refs[version] = (local_ref, object_id)
    if set(local_refs) != set(releases):
        raise LedgerError("Fetched release-ref namespace does not match the remote")

    fetched: dict[SemVer, str] = {}
    for version in sorted(releases):
        remote_ref, expected = releases[version]
        local_ref, fetched_object = local_refs[version]
        commit = _resolve_commit(root, local_ref, object_id_re)
        if commit != expected or fetched_object != expected:
            raise LedgerError(f"Remote release ref changed while fetching: {remote_ref}")
        fetched[version] = commit
    return fetched


def _require_ancestor(root: Path, ancestor: str, descendant: str) -> None:
    result = _run_git(
        root,
        ["merge-base", "--is-ancestor", ancestor, descendant],
        operation="release ancestry inspection",
        check=False,
    )
    if result.returncode == 1:
        raise LedgerError(
            "Stale main release ledger: release commit "
            f"{ancestor} is not an ancestor of source {descendant}; merge the "
            "published release synchronization PR into main first"
        )
    if result.returncode != 0:
        raise LedgerError(
            f"Git release ancestry inspection failed with exit code {result.returncode}"
        )


def _commit_parent(root: Path, commit: str, object_id_re: re.Pattern[str]) -> str:
    fields = _run_git(
        root,
        ["rev-list", "--parents", "-n", "1", commit],
        operation="release parent inspection",
    ).stdout.strip().split()
    if len(fields) != 2 or fields[0] != commit:
        raise LedgerError(
            f"Release commit {commit} must have exactly one parent"
        )
    parent = fields[1]
    if not object_id_re.fullmatch(parent):
        raise LedgerError(f"Release commit {commit} has an invalid parent object id")
    return parent


def _safe_snapshot_path(value: str) -> PurePosixPath:
    if (
        not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise LedgerError("Historical source tree contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(
        part in {"", ".", "..", ".git"} for part in path.parts
    ):
        raise LedgerError("Historical source tree contains an unsafe path")
    return path


@contextlib.contextmanager
def _materialized_source_commit(
    root: Path,
    commit: str,
    release_root: str,
) -> Iterator[tuple[Path, frozenset[str], frozenset[str]]]:
    """Materialize immutable planner inputs without checking out the repository.

    The append-only release payload is deliberately excluded: it is verified by
    Git tree identity elsewhere and can grow to hundreds of megabytes.  Git
    symlinks are represented by inert marker files and reported separately so
    the exact planner input-selection rules can reject only selected symlinks.
    """

    object_id_re = _object_id_pattern("sha256" if len(commit) == 64 else "sha1")
    raw_tree = _run_git_bytes(
        root,
        ["ls-tree", "-r", "-z", "--full-tree", commit],
        operation="historical source tree inspection",
    )
    blob_paths: dict[str, list[tuple[str, str]]] = defaultdict(list)
    gitlinks: list[str] = []
    omitted_release_paths: list[tuple[str, str]] = []
    seen: set[str] = set()
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode_bytes, kind_bytes, object_bytes = metadata.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            kind = kind_bytes.decode("ascii")
            object_id = object_bytes.decode("ascii")
            name = encoded_path.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise LedgerError("Git returned malformed historical tree data") from exc
        relative = _safe_snapshot_path(name)
        name = relative.as_posix()
        if name in seen or not object_id_re.fullmatch(object_id):
            raise LedgerError("Git returned inconsistent historical tree data")
        seen.add(name)
        in_release_root = name == release_root or name.startswith(
            release_root.rstrip("/") + "/"
        )
        if mode in {"100644", "100755", "120000"} and kind == "blob":
            if in_release_root:
                omitted_release_paths.append((name, mode))
            else:
                blob_paths[object_id].append((name, mode))
        elif mode == "160000" and kind == "commit":
            if not in_release_root:
                gitlinks.append(name)
        else:
            raise LedgerError("Historical source tree contains an unsupported entry")

    # The append-only release payload is verified by Git tree identity and must
    # not consume the source-materialization budget as history grows forever.
    file_count = sum(len(paths) for paths in blob_paths.values())
    if file_count > MAX_SNAPSHOT_FILES:
        raise LedgerError("Historical source snapshot exceeds the file-count limit")

    with tempfile.TemporaryDirectory(prefix="buh-ledger-source-") as temp:
        snapshot = Path(temp) / "source"
        snapshot.mkdir()
        symlinks: set[str] = set()
        try:
            for name, _mode in omitted_release_paths:
                destination = snapshot.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch(exist_ok=False)
            for name in gitlinks:
                snapshot.joinpath(*PurePosixPath(name).parts).mkdir(
                    parents=True, exist_ok=False
                )
            process = subprocess.Popen(
                ["git", "cat-file", "--batch"],
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise LedgerError(
                "Historical source snapshot could not be materialized"
            ) from exc
        assert process.stdin is not None
        assert process.stdout is not None
        total_size = 0
        try:
            for expected_object in sorted(blob_paths):
                process.stdin.write(expected_object.encode("ascii") + b"\n")
                process.stdin.flush()
                header = process.stdout.readline()
                try:
                    actual_bytes, kind, size_bytes = header.rstrip(b"\n").split(b" ")
                    actual_object = actual_bytes.decode("ascii")
                    size = int(size_bytes)
                except (ValueError, UnicodeError) as exc:
                    raise LedgerError(
                        "Git returned malformed historical blob data"
                    ) from exc
                if (
                    actual_object != expected_object
                    or kind != b"blob"
                    or size < 0
                    or size > MAX_SNAPSHOT_FILE_BYTES
                ):
                    raise LedgerError("Git returned inconsistent historical blob data")
                logical_size = size * len(blob_paths[expected_object])
                total_size += logical_size
                if total_size > MAX_SNAPSHOT_BYTES:
                    raise LedgerError(
                        "Historical source snapshot exceeds the total-size limit"
                    )
                content = bytearray()
                remaining = size
                while remaining:
                    block = process.stdout.read(min(1024 * 1024, remaining))
                    if not block:
                        raise LedgerError("Git returned truncated historical blob data")
                    content.extend(block)
                    remaining -= len(block)
                if process.stdout.read(1) != b"\n":
                    raise LedgerError("Git returned malformed historical blob framing")
                for name, mode in blob_paths[expected_object]:
                    destination = snapshot.joinpath(*PurePosixPath(name).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if mode == "120000":
                        symlinks.add(name)
                        destination.touch(exist_ok=False)
                    else:
                        with destination.open("xb") as output:
                            output.write(content)
                        destination.chmod(0o755 if mode == "100755" else 0o644)
            process.stdin.close()
            if process.wait() != 0:
                raise LedgerError("Git historical blob extraction failed")
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()
            raise
        process.stdout.close()
        yield (
            snapshot,
            frozenset(symlinks),
            frozenset(name for name, _mode in omitted_release_paths),
        )


def _path_is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _literal_glob_prefix(pattern: str) -> str | None:
    """Return the path prefix before the first glob-bearing component."""

    parts: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if any(character in part for character in "*?["):
            break
        parts.append(part)
    if not parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _planner_path_anchors(registry: Any) -> tuple[str, ...]:
    """Return configured paths whose ancestors must be ordinary directories."""

    anchors = {
        "ops/release",
        "ops/release/apps.toml",
        "changes",
        registry.compatibility_file,
        *registry.shared_build_inputs,
    }
    for app in registry.apps:
        anchors.add(app.path)
        anchors.add(f"{app.path}/{app.version_file}")
    for pattern in registry.platform_build_inputs:
        prefix = _literal_glob_prefix(pattern)
        if prefix is not None:
            anchors.add(prefix)
    return tuple(sorted(anchors))


def _ignored_source_path(path: str, directory: str, ignored: set[str]) -> bool:
    relative = PurePosixPath(path).relative_to(PurePosixPath(directory))
    return any(
        part in ignored or part.endswith(".egg-info")
        for part in relative.parts
    )


def _selected_planner_paths(snapshot: Path, registry: Any) -> tuple[str, ...]:
    """Return every historical file whose worktree bytes can affect planning."""

    selected = {
        registry.compatibility_file,
        *registry.shared_build_inputs,
    }
    for app in registry.apps:
        app_root = snapshot / app.path
        if not app_root.is_dir():
            continue
        for path in app_root.rglob("*"):
            relative = path.relative_to(app_root).as_posix()
            if path.is_file() and not any(
                part in IGNORED_PARTS or part.endswith(".egg-info")
                for part in PurePosixPath(relative).parts
            ):
                selected.add(path.relative_to(snapshot).as_posix())

    tool_root = snapshot / "ops/release"
    if tool_root.is_dir():
        for path in tool_root.rglob("*"):
            relative = path.relative_to(tool_root).as_posix()
            if (
                path.is_file()
                and path.name != "README.md"
                and path.suffix in {".py", ".toml", ".json"}
                and not any(
                    part in IGNORED_PARTS or part.endswith(".egg-info")
                    for part in PurePosixPath(relative).parts
                )
            ):
                selected.add(path.relative_to(snapshot).as_posix())

    for pattern in registry.platform_build_inputs:
        for path in snapshot.glob(pattern):
            relative = path.relative_to(snapshot).as_posix()
            if path.is_file() and not any(
                part in PLATFORM_INPUT_IGNORED_PARTS
                or part.endswith(".egg-info")
                or part.endswith((".pyc", ".pyo"))
                for part in PurePosixPath(relative).parts
            ):
                selected.add(relative)

    changes = snapshot / "changes"
    if changes.is_dir():
        for path in changes.iterdir():
            if (
                path.is_file()
                and not path.name.startswith(".")
                and path.name != "README.md"
                and path.suffix == ".toml"
            ):
                selected.add(path.relative_to(snapshot).as_posix())
    return tuple(sorted(selected))


def _legacy_release_wheel_paths(
    snapshot: Path, release_root: str
) -> tuple[str, ...]:
    releases = snapshot / "releases"
    if not releases.is_dir():
        return ()
    return tuple(
        sorted(
            path.relative_to(snapshot).as_posix()
            for path in releases.rglob("*.whl")
            if path.is_file()
            and not _path_is_within(
                path.relative_to(snapshot).as_posix(), release_root
            )
        )
    )


def _reject_path_checkout_transformations(
    root: Path,
    commit: str,
    paths: Sequence[str],
    *,
    context: str,
) -> None:
    """Require selected paths to have blob-identical checkout bytes."""

    ambiguous = _run_git(
        root,
        [
            "grep",
            "--quiet",
            "-I",
            "-E",
            AMBIGUOUS_ATTRIBUTE_ASSIGNMENT_RE,
            commit,
            "--",
            ".gitattributes",
            "**/.gitattributes",
        ],
        operation="historical attribute sentinel inspection",
        check=False,
    )
    if ambiguous.returncode == 0:
        raise LedgerError(
            "Historical Git attributes use an ambiguous literal sentinel assignment"
        )
    if ambiguous.returncode != 1:
        raise LedgerError(
            "Historical Git attribute sentinel inspection could not be completed"
        )

    autocrlf = _run_git(
        root,
        ["config", "--get", "core.autocrlf"],
        operation="checkout byte-normalization configuration inspection",
        check=False,
    )
    if autocrlf.returncode not in {0, 1}:
        raise LedgerError("Git checkout byte-normalization configuration is invalid")
    if autocrlf.returncode == 0 and autocrlf.stdout.strip().lower() not in {
        "false",
        "no",
        "off",
        "0",
        "input",
    }:
        raise LedgerError(
            "Git core.autocrlf must not transform verified repository bytes"
        )

    encoded_paths = b"".join(path.encode("utf-8") + b"\0" for path in paths)
    try:
        result = subprocess.run(
            [
                "git",
                "check-attr",
                f"--source={commit}",
                "-z",
                "--stdin",
                *CHECKOUT_TRANSFORM_ATTRIBUTES,
            ],
            cwd=root,
            input=encoded_paths,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise LedgerError("Historical Git attributes could not be inspected") from exc
    if result.returncode != 0:
        raise LedgerError(
            f"Historical Git attribute inspection failed with exit code {result.returncode}"
        )
    fields = result.stdout.split(b"\0")
    if not fields or fields[-1] != b"":
        raise LedgerError("Git returned malformed historical attribute data")
    fields.pop()
    if len(fields) % 3:
        raise LedgerError("Git returned malformed historical attribute data")
    expected = {
        (path, attribute)
        for path in paths
        for attribute in CHECKOUT_TRANSFORM_ATTRIBUTES
    }
    seen: set[tuple[str, str]] = set()
    for offset in range(0, len(fields), 3):
        try:
            path = fields[offset].decode("utf-8")
            attribute = fields[offset + 1].decode("ascii")
        except UnicodeError as exc:
            raise LedgerError("Git returned malformed historical attribute data") from exc
        key = (path, attribute)
        if key not in expected or key in seen:
            raise LedgerError("Git returned inconsistent historical attribute data")
        seen.add(key)
        if fields[offset + 2] not in {b"unspecified", b"unset"}:
            raise LedgerError(
                f"{context} has a checkout-transforming Git attribute "
                f"{attribute}: {path}"
            )
    if seen != expected:
        raise LedgerError("Git omitted historical attribute data")


def _reject_checkout_transformations(
    root: Path,
    commit: str,
    snapshot: Path,
    registry: Any,
) -> None:
    _reject_path_checkout_transformations(
        root,
        commit,
        _selected_planner_paths(snapshot, registry),
        context="Historical planner input",
    )


def _inspect_dependency_source(
    snapshot: Path,
    dependency: Any,
    symlinks: frozenset[str],
    omitted_release_paths: frozenset[str],
) -> Any:
    if any(
        _path_is_within(dependency.source, blocked_path)
        for blocked_path in symlinks | omitted_release_paths
    ):
        raise LedgerError(
            "Provided dependency source is not an exact regular file: "
            f"{dependency.component}"
        )
    source = snapshot.joinpath(*PurePosixPath(dependency.source).parts)
    try:
        info = inspect_wheel(
            source,
            expected_distribution=dependency.distribution,
            expected_version=dependency.version,
        )
    except (OSError, ReleaseError) as exc:
        raise LedgerError(
            f"Provided dependency source cannot be reproduced: {dependency.component}"
        ) from exc
    if source.name != dependency.filename or info.sha256 != dependency.sha256:
        raise LedgerError(
            "Provided dependency source differs from its historical plan: "
            f"{dependency.component}"
        )
    if dependency.git_blob_sha and git_blob_object_id(
        source, hex_length=len(dependency.git_blob_sha)
    ) != dependency.git_blob_sha:
        raise LedgerError(
            "Provided dependency source has the wrong Git blob identity: "
            f"{dependency.component}"
        )
    return info


def _require_exact_100644_blob(
    root: Path,
    commit: str,
    path: str,
    *,
    context: str,
) -> None:
    object_id_re = _object_id_pattern("sha256" if len(commit) == 64 else "sha1")
    entry = _tree_entry(root, commit, path, object_id_re)
    if entry is None or entry[0] != "100644" or entry[1] != "blob":
        raise LedgerError(f"{context} is not an exact 100644 blob: {path}")


def _validate_source_reproducibility(
    root: Path,
    source_commit: str,
    release_root: str,
    expected_registry: Any,
    previous_manifest: Mapping[str, Any] | None,
) -> None:
    """Fail before publication if exact committed source cannot be replayed."""

    with _materialized_source_commit(
        root, source_commit, release_root
    ) as (snapshot, symlinks, omitted_release_paths):
        registry = _load_historical_registry(snapshot, source_commit)
        if (
            registry.sha256 != expected_registry.sha256
            or registry.platform_id != expected_registry.platform_id
            or registry.initial_version != expected_registry.initial_version
            or registry.release_root != expected_registry.release_root
        ):
            raise LedgerError(
                "Committed source registry differs from the verified working registry"
            )
        _reject_selected_snapshot_paths(
            snapshot, symlinks, registry, description="is a symlink"
        )
        _reject_selected_snapshot_paths(
            snapshot,
            omitted_release_paths,
            registry,
            description="depends on the immutable release output",
        )
        _reject_checkout_transformations(root, source_commit, snapshot, registry)
        _reject_path_checkout_transformations(
            root,
            source_commit,
            _legacy_release_wheel_paths(snapshot, registry.release_root),
            context="Immutable legacy wheel history",
        )
        for app in registry.apps:
            _require_exact_100644_blob(
                root,
                source_commit,
                f"{app.path}/{app.version_file}",
                context="Registered version file",
            )
        try:
            current_changes = load_changes(snapshot / "changes", set(registry.by_id))
        except (OSError, ReleaseError) as exc:
            raise LedgerError(
                "Committed change fragments cannot be reproduced"
            ) from exc
        for fragment in sorted({change.fragment for change in current_changes}):
            _require_exact_100644_blob(
                root,
                source_commit,
                f"changes/{fragment}",
                context="Prospective change fragment",
            )
        previous_artifacts = (
            _manifest_artifacts(previous_manifest)
            if previous_manifest is not None
            else {}
        )
        provided_dependencies = []
        for dependency in registry.dependencies:
            prior = previous_artifacts.get(dependency.component)
            reusable = bool(
                prior
                and prior.get("kind") == "third-party"
                and isinstance(prior.get("distribution"), str)
                and normalize_distribution(prior["distribution"])
                == normalize_distribution(dependency.distribution)
                and prior.get("version") == dependency.version
                and prior.get("filename") == dependency.filename
                and prior.get("sha256") == dependency.sha256
            )
            if not reusable:
                provided_dependencies.append(dependency)
        dependency_paths = tuple(
            sorted(dependency.source for dependency in provided_dependencies)
        )
        _reject_path_checkout_transformations(
            root,
            source_commit,
            dependency_paths,
            context="Committed dependency source",
        )
        for dependency in provided_dependencies:
            _require_exact_100644_blob(
                root,
                source_commit,
                dependency.source,
                context="Provided dependency source",
            )
            _inspect_dependency_source(
                snapshot,
                dependency,
                symlinks,
                omitted_release_paths,
            )


def _reject_selected_snapshot_paths(
    snapshot: Path,
    paths: frozenset[str],
    registry: Any,
    *,
    description: str,
) -> None:
    selected_paths = set(_selected_planner_paths(snapshot, registry))
    anchors = _planner_path_anchors(registry)
    for path in paths:
        if path in selected_paths or any(
            _path_is_within(anchor, path) for anchor in anchors
        ):
            raise LedgerError(f"Historical release input {description}: {path}")
        if path == registry.compatibility_file or path in registry.shared_build_inputs:
            raise LedgerError(f"Historical release input {description}: {path}")
        for app in registry.apps:
            if _path_is_within(path, app.path) and not _ignored_source_path(
                path, app.path, IGNORED_PARTS
            ):
                raise LedgerError(f"Historical release input {description}: {path}")
        if _path_is_within(path, "ops/release") and not _ignored_source_path(
            path, "ops/release", IGNORED_PARTS
        ):
            raise LedgerError(f"Historical release input {description}: {path}")

    for pattern in registry.platform_build_inputs:
        for candidate in snapshot.glob(pattern):
            relative = candidate.relative_to(snapshot).as_posix()
            if relative not in paths:
                continue
            parts = PurePosixPath(relative).parts
            if any(
                part in PLATFORM_INPUT_IGNORED_PARTS
                or part.endswith(".egg-info")
                or part.endswith((".pyc", ".pyo"))
                for part in parts
            ):
                continue
            raise LedgerError(
                f"Historical release input {description}: {relative}"
            )


def _validate_changed_path(value: str) -> str:
    if (
        not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise LedgerError(f"Release commit contains an unsafe path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(
        part in {"", ".", "..", ".git"} for part in path.parts
    ):
        raise LedgerError(f"Release commit contains an unsafe path: {value!r}")
    return value


def _commit_changes(root: Path, parent: str, commit: str) -> dict[str, str]:
    raw = _run_git_bytes(
        root,
        [
            "diff-tree",
            "-r",
            "-z",
            "--no-commit-id",
            "--name-status",
            "--no-renames",
            parent,
            commit,
            "--",
        ],
        operation="release commit diff inspection",
    )
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise LedgerError("Git returned malformed release commit diff data")
    changes: dict[str, str] = {}
    for index in range(0, len(fields), 2):
        try:
            status = fields[index].decode("ascii")
            path = fields[index + 1].decode("utf-8")
        except UnicodeError as exc:
            raise LedgerError("Release commit diff contains invalid text") from exc
        _validate_changed_path(path)
        if status not in {"A", "M", "D", "T"} or path in changes:
            raise LedgerError("Release commit diff contains an invalid or duplicate entry")
        changes[path] = status
    if not changes:
        raise LedgerError("Release commit contains no changes")
    return changes


def _tree_entry(
    root: Path,
    commit: str,
    path: str,
    object_id_re: re.Pattern[str],
) -> tuple[str, str, str] | None:
    raw = _run_git_bytes(
        root,
        ["ls-tree", "-z", commit, "--", path],
        operation="release commit path inspection",
    )
    if not raw:
        return None
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise LedgerError(f"Git returned ambiguous tree data for {path}")
    try:
        header, returned_path = records[0].split(b"\t", 1)
        mode_bytes, kind_bytes, object_bytes = header.split(b" ", 2)
        mode = mode_bytes.decode("ascii")
        kind = kind_bytes.decode("ascii")
        object_id = object_bytes.decode("ascii")
        decoded_path = returned_path.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise LedgerError(f"Git returned malformed tree data for {path}") from exc
    if decoded_path != path or not object_id_re.fullmatch(object_id):
        raise LedgerError(f"Git returned inconsistent tree data for {path}")
    return mode, kind, object_id


def _regular_blob_bytes(
    root: Path,
    commit: str,
    path: str,
    object_id_re: re.Pattern[str],
) -> bytes:
    entry = _tree_entry(root, commit, path, object_id_re)
    if entry is None or entry[0] != "100644" or entry[1] != "blob":
        raise LedgerError(f"Expected a regular tracked file at {commit}:{path}")
    return _run_git_bytes(
        root,
        ["cat-file", "blob", f"{commit}:{path}"],
        operation="release commit blob inspection",
    )


def _version_replacement(parent: bytes, target: str, path: str) -> bytes:
    matches = list(VERSION_ASSIGNMENT_RE.finditer(parent))
    if len(matches) != 1:
        raise LedgerError(f"Registered version file is not canonical: {path}")
    target_bytes = target.encode("ascii")
    match = matches[0]
    replacement = match.group("prefix") + target_bytes + match.group("suffix")
    return parent[: match.start()] + replacement + parent[match.end() :]


def _version_from_file(content: bytes, path: str) -> SemVer:
    matches = list(VERSION_ASSIGNMENT_RE.finditer(content))
    if len(matches) != 1:
        raise LedgerError(f"Registered version file is not canonical: {path}")
    match = matches[0]
    try:
        value = content[match.end("prefix") : match.start("suffix")].decode("ascii")
        return SemVer.parse(value)
    except (UnicodeError, ReleaseError) as exc:
        raise LedgerError(f"Registered version file has an invalid version: {path}") from exc


def _highest_manifest_bump(
    changes: Sequence[Mapping[str, Any]], app_id: str | None = None
) -> str:
    selected = [
        change.get("bump")
        for change in changes
        if app_id is None or change.get("app_id") == app_id
    ]
    if any(bump not in BUMP_RANK for bump in selected):
        raise LedgerError("Release manifest contains an invalid semantic-version bump")
    return max(selected, key=BUMP_RANK.__getitem__, default="none")


def _load_historical_registry(snapshot: Path, commit: str) -> Any:
    try:
        return load_registry(snapshot / "ops/release/apps.toml")
    except (OSError, ReleaseError) as exc:
        raise LedgerError(
            f"Historical release registry is malformed at source {commit}"
        ) from exc


def _ordered_registry_apps(registry: Any) -> list[Any]:
    by_id = registry.by_id
    incoming = {
        app.app_id: len(app.dependencies) for app in registry.apps
    }
    outgoing: dict[str, set[str]] = defaultdict(set)
    for app in registry.apps:
        for dependency in app.dependencies:
            outgoing[dependency].add(app.app_id)
    queue = deque(sorted(app_id for app_id, count in incoming.items() if count == 0))
    ordered: list[Any] = []
    while queue:
        app_id = queue.popleft()
        ordered.append(by_id[app_id])
        for dependent in sorted(outgoing[app_id]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                queue.append(dependent)
    if len(ordered) != len(registry.apps):
        raise LedgerError("Historical application dependency graph contains a cycle")
    return ordered


def _manifest_artifacts(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise LedgerError("Verified release manifest has invalid artifacts")
    result: dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise LedgerError("Verified release manifest has a malformed artifact")
        component = artifact.get("component")
        if not isinstance(component, str) or component in result:
            raise LedgerError("Verified release manifest repeats an artifact component")
        result[component] = artifact
    return result


def _load_verified_install_plan(
    release_dir: Path,
    manifest: Mapping[str, Any],
) -> Mapping[str, Any]:
    reference = manifest.get("install_plan")
    if (
        not isinstance(reference, Mapping)
        or reference.get("filename") != "INSTALL_PLAN.json"
        or not isinstance(reference.get("sha256"), str)
    ):
        raise LedgerError("Verified release manifest has an invalid install-plan reference")
    path = release_dir / "INSTALL_PLAN.json"
    try:
        encoded = path.read_bytes()
        plan = json.loads(encoded)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LedgerError("Verified release install plan could not be read") from exc
    if (
        not isinstance(plan, Mapping)
        or encoded != _canonical_json_bytes(plan)
        or sha256_file(path) != reference["sha256"]
    ):
        raise LedgerError("Verified release install plan is not canonical and bound")
    return plan


def _validate_manifest_changes(
    snapshot: Path,
    manifest: Mapping[str, Any],
    registry: Any,
) -> tuple[list[dict[str, str]], set[str]]:
    manifest_changes = manifest.get("changes")
    if not isinstance(manifest_changes, list) or not all(
        isinstance(change, Mapping) for change in manifest_changes
    ):
        raise LedgerError("Verified release manifest has malformed changes")
    try:
        parsed = [
            change.as_dict()
            for change in load_changes(snapshot / "changes", set(registry.by_id))
        ]
    except (OSError, ReleaseError) as exc:
        raise LedgerError("Historical change fragments are malformed") from exc
    def change_key(item: Mapping[str, Any]) -> tuple[Any, Any]:
        return item["fragment"], item["app_id"]

    if sorted(parsed, key=change_key) != sorted(
        (dict(change) for change in manifest_changes), key=change_key
    ):
        raise LedgerError(
            "Historical change fragments do not exactly match the verified release manifest"
        )
    return parsed, {f"changes/{change['fragment']}" for change in parsed}


def _validate_install_plan_identity(
    install_plan: Mapping[str, Any],
    manifest_artifacts: Mapping[str, Mapping[str, Any]],
    registry: Any,
    release_version: SemVer,
    compatibility_sha256: str,
) -> None:
    if (
        install_plan.get("platform_version") != str(release_version)
        or install_plan.get("compatibility_sha256") != compatibility_sha256
    ):
        raise LedgerError("Install plan is not bound to historical platform inputs")
    ordered_apps = _ordered_registry_apps(registry)
    expected_django_apps = [app.django_app for app in ordered_apps]
    expected_setup_commands = [
        command for app in ordered_apps for command in app.setup_commands
    ]
    if install_plan.get("django_apps") != expected_django_apps:
        raise LedgerError("Install-plan Django applications differ from the registry")
    if install_plan.get("setup_commands") != expected_setup_commands:
        raise LedgerError("Install-plan setup commands differ from the registry")

    wheels = install_plan.get("wheels")
    if not isinstance(wheels, list) or not all(
        isinstance(wheel, Mapping) for wheel in wheels
    ):
        raise LedgerError("Verified install plan has malformed wheels")
    expected_components = [
        dependency.component
        for dependency in sorted(
            registry.dependencies,
            key=lambda dependency: (
                normalize_distribution(dependency.distribution),
                dependency.version,
            ),
        )
    ] + [app.app_id for app in ordered_apps]
    if [wheel.get("component") for wheel in wheels] != expected_components:
        raise LedgerError("Install-plan wheel order differs from the historical plan")
    for wheel in wheels:
        artifact = manifest_artifacts.get(wheel.get("component"))
        if artifact is None or any(
            wheel.get(key) != artifact.get(key)
            for key in ("component", "distribution", "version", "filename", "sha256")
        ):
            raise LedgerError("Install-plan wheel identity differs from the manifest")


def _validate_authoritative_plan_replay(
    snapshot: Path,
    source_commit: str,
    release_root: str,
    release_dir: Path,
    manifest: Mapping[str, Any],
    previous_manifest: Mapping[str, Any] | None,
) -> None:
    """Run the schema-v1 compatibility planner against exact historical source."""

    manifest_build = manifest.get("build")
    if not isinstance(manifest_build, Mapping):
        raise LedgerError("Verified release manifest has invalid build metadata")
    previous_path = None
    if previous_manifest is not None:
        previous_version = previous_manifest.get("platform_version")
        try:
            previous_version = str(SemVer.parse(previous_version))
        except ReleaseError as exc:
            raise LedgerError("Previous verified release has an invalid version") from exc
        previous_path = release_dir.parent / f"v{previous_version}" / "RELEASE.json"
    try:
        if manifest.get("schema_version") != 1:
            raise LedgerError("No historical planner is registered for manifest schema")
        plan = create_plan_v1(
            repo_root=snapshot,
            registry_path=snapshot / "ops/release/apps.toml",
            compatibility_path=None,
            changes_dir=snapshot / "changes",
            previous_manifest_path=previous_path,
            source_commit=source_commit,
            test_run=manifest_build.get("test_run"),
        )
    except (OSError, ReleaseError) as exc:
        raise LedgerError(
            "Authoritative historical release plan cannot be reproduced"
        ) from exc

    expected_build = {**plan["build"], "test_run": plan.get("test_run")}
    expected_compatibility = {
        "sha256": plan["build"]["compatibility_sha256"],
        "values": plan["compatibility"],
    }
    previous_platform = (
        previous_manifest.get("platform_version")
        if previous_manifest is not None
        else None
    )
    planned_deployment_predecessor = plan.get("deployment_predecessor")
    if planned_deployment_predecessor is not None:
        if not isinstance(planned_deployment_predecessor, Mapping):
            raise LedgerError(
                "Authoritative historical deployment_predecessor is malformed"
            )
        legacy_fields = {
            "manifest_sha256",
            "path",
            "payload_sha256",
            "platform_version",
            "source_commit",
        }
        if set(planned_deployment_predecessor) == legacy_fields:
            expected_predecessor = {
                key: planned_deployment_predecessor[key]
                for key in (
                    "manifest_sha256",
                    "platform_version",
                    "source_commit",
                )
            }
            expected_recovery = None
        else:
            expected_predecessor = None
            expected_recovery = {
                key: value
                for key, value in planned_deployment_predecessor.items()
                if key != "path"
            }
    else:
        expected_predecessor = None
        expected_recovery = None
    if expected_predecessor is None and previous_manifest is not None:
        previous_path = release_dir.parent / (
            f"v{previous_manifest['platform_version']}/RELEASE.json"
        )
        expected_predecessor = {
            "platform_version": previous_manifest["platform_version"],
            "source_commit": previous_manifest["source_commit"],
            "manifest_sha256": sha256_file(previous_path),
        }
    if manifest.get("previous_release") != expected_predecessor:
        raise LedgerError(
            "Release does not name the exact immediately preceding verified release"
        )
    if manifest.get("deployment_recovery") != expected_recovery:
        raise LedgerError(
            "Release deployment recovery differs from its authoritative historical plan"
        )
    if (
        not plan.get("release_required")
        or plan.get("platform_id") != manifest.get("platform_id")
        or plan.get("source_commit") != manifest.get("source_commit")
        or plan.get("release_root") != release_root
        or plan.get("previous_platform_version") != previous_platform
        or plan.get("platform_version") != manifest.get("platform_version")
        or expected_build != dict(manifest_build)
        or expected_compatibility != manifest.get("compatibility")
        or plan.get("changes") != manifest.get("changes")
    ):
        raise LedgerError(
            "Verified release manifest differs from its authoritative historical plan"
        )

    artifacts = _manifest_artifacts(manifest)
    planned_apps = plan.get("apps")
    planned_dependencies = plan.get("dependency_artifacts")
    if not isinstance(planned_apps, list) or not isinstance(planned_dependencies, list):
        raise LedgerError("Authoritative historical release plan is malformed")
    for app in planned_apps:
        artifact = artifacts.get(app.get("id"))
        if artifact is None or any(
            artifact.get(key) != app.get(key)
            for key in ("distribution", "version", "input_sha256", "import_name")
        ):
            raise LedgerError(
                "Owned artifact differs from the authoritative historical plan"
            )
        expected_origin = "built" if app.get("build") else "reused"
        if artifact.get("origin") != expected_origin:
            raise LedgerError(
                "Owned artifact action differs from the authoritative historical plan"
            )
    for dependency in planned_dependencies:
        artifact = artifacts.get(dependency.get("component"))
        dependency_action = dependency.get("action")
        expected_git_blob = dependency.get("git_blob_sha")
        if dependency_action == "reuse" and previous_manifest is not None:
            previous_artifact = _manifest_artifacts(previous_manifest).get(
                dependency.get("component")
            )
            expected_git_blob = (
                previous_artifact.get("git_blob_sha")
                if previous_artifact is not None
                else None
            )
        if artifact is None or any(
            artifact.get(key) != dependency.get(key)
            for key in ("distribution", "version", "filename", "sha256")
        ):
            raise LedgerError(
                "Third-party artifact differs from the authoritative historical plan"
            )
        if artifact.get("git_blob_sha") != expected_git_blob:
            raise LedgerError(
                "Third-party artifact differs from the authoritative historical plan"
            )
        expected_origin = (
            "reused" if dependency_action == "reuse" else "provided"
        )
        if artifact.get("origin") != expected_origin:
            raise LedgerError(
                "Third-party artifact action differs from the authoritative historical plan"
            )


def _validate_historical_plan_semantics(
    root: Path,
    source_commit: str,
    snapshot: Path,
    symlinks: frozenset[str],
    omitted_release_paths: frozenset[str],
    release_dir: Path,
    manifest: Mapping[str, Any],
    previous_manifest: Mapping[str, Any] | None,
    registry: Any,
    release_version: SemVer,
    wheel_history: dict[tuple[str, str], set[str]],
) -> set[str]:
    """Replay all source-derived schema-v1 planning decisions."""

    _reject_selected_snapshot_paths(
        snapshot, symlinks, registry, description="is a symlink"
    )
    _reject_selected_snapshot_paths(
        snapshot,
        omitted_release_paths,
        registry,
        description="depends on the immutable release output",
    )
    _reject_checkout_transformations(root, source_commit, snapshot, registry)
    parsed_changes, expected_fragments = _validate_manifest_changes(
        snapshot, manifest, registry
    )
    artifacts = _manifest_artifacts(manifest)
    manifest_artifact_list = manifest.get("artifacts")
    assert isinstance(manifest_artifact_list, list)
    if [artifact.get("component") for artifact in manifest_artifact_list] != sorted(
        artifacts
    ):
        raise LedgerError(
            "Verified release artifacts are not in canonical component order"
        )
    expected_components = set(registry.by_id) | {
        dependency.component for dependency in registry.dependencies
    }
    if set(artifacts) != expected_components:
        raise LedgerError(
            "Verified release artifacts do not exactly cover the historical registry"
        )
    previous_artifacts = (
        _manifest_artifacts(previous_manifest)
        if previous_manifest is not None
        else {}
    )

    try:
        _, compatibility_sha256 = load_compatibility(
            snapshot / registry.compatibility_file
        )
        build = {
            "registry_sha256": registry.sha256,
            "compatibility_sha256": compatibility_sha256,
            "tool_sha256": tool_input_digest(
                snapshot, snapshot / "ops/release/apps.toml"
            ),
            "platform_sha256": platform_input_digest(
                snapshot, registry.platform_build_inputs
            ),
        }
    except (OSError, ReleaseError) as exc:
        raise LedgerError("Historical platform inputs cannot be reproduced") from exc
    manifest_build = manifest.get("build")
    if not isinstance(manifest_build, Mapping) or any(
        manifest_build.get(key) != value for key, value in build.items()
    ):
        raise LedgerError(
            "Verified release build digests do not match its historical source"
        )

    app_changes: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for change in parsed_changes:
        app_changes[change["app_id"]].append(change)
    bootstrap = previous_manifest is None
    previous_platform_version = (
        None if previous_manifest is None else previous_manifest.get("platform_version")
    )
    app_plans: list[dict[str, Any]] = []
    owned_wheel_requirements: dict[str, tuple[str, ...]] = {}
    release_wheel_identities: list[tuple[tuple[str, str], str]] = []
    built_owned_wheels: list[Any] = []
    for app_id, app in registry.by_id.items():
        artifact = artifacts[app_id]
        if (
            artifact.get("kind") != "owned"
            or artifact.get("distribution") != app.distribution
            or artifact.get("import_name") != app.import_name
        ):
            raise LedgerError(
                f"Owned artifact identity differs from the historical registry: {app_id}"
            )
        try:
            current_version = SemVer.parse(read_app_version(snapshot, app))
            input_sha256 = app_input_digest(
                snapshot, app, registry.shared_build_inputs
            )
        except (OSError, ReleaseError) as exc:
            raise LedgerError(
                f"Historical application inputs cannot be reproduced: {app_id}"
            ) from exc
        if artifact.get("input_sha256") != input_sha256:
            raise LedgerError(
                f"Owned artifact input digest differs from historical source: {app_id}"
            )

        prior = previous_artifacts.get(app_id)
        if bootstrap:
            changed = True
        elif prior is None:
            changed = True
        else:
            if prior.get("kind") != "owned" or prior.get("version") != str(
                current_version
            ):
                raise LedgerError(
                    f"Historical source version does not match prior artifact: {app_id}"
                )
            changed = prior.get("input_sha256") != input_sha256
        bump = _highest_manifest_bump(app_changes[app_id], app_id)
        if not bootstrap and changed and bump == "none":
            raise LedgerError(
                f"Changed application is missing a change fragment: {app_id}"
            )
        if not bootstrap and not changed and bump != "none":
            raise LedgerError(
                f"Unchanged application has a stale change fragment: {app_id}"
            )
        target_version = current_version.bump(bump)
        if artifact.get("version") != str(target_version):
            raise LedgerError(
                f"Owned-app version transition does not match consumed fragments: {app_id}"
            )
        app_plans.append(
            {
                "id": app_id,
                "distribution": app.distribution,
                "version": str(target_version),
                "dependencies": list(app.dependencies),
            }
        )
        expected_origin = "built" if changed else "reused"
        if artifact.get("origin") != expected_origin:
            raise LedgerError(
                f"Owned artifact action differs from the historical plan: {app_id}"
            )
        if expected_origin == "reused":
            expected_reused = dict(prior)
            expected_reused["origin"] = "reused"
            expected_reused["reused_from"] = previous_platform_version
            if dict(artifact) != expected_reused:
                raise LedgerError(
                    f"Reused owned artifact differs from its immutable predecessor: {app_id}"
                )
        elif "reused_from" in artifact:
            raise LedgerError(
                f"Built owned artifact incorrectly names a prior release: {app_id}"
            )
        if expected_origin == "built" and "git_blob_sha" in artifact:
            raise LedgerError(
                f"Built owned artifact contains non-assembler Git metadata: {app_id}"
            )
        filename = artifact.get("filename")
        if (
            not isinstance(filename, str)
            or PurePosixPath(filename).name != filename
            or "\\" in filename
        ):
            raise LedgerError(f"Owned artifact has an unsafe filename: {app_id}")
        try:
            wheel_info = inspect_wheel(
                release_dir / filename,
                expected_distribution=app.distribution,
                expected_version=artifact.get("version"),
            )
        except (OSError, ReleaseError) as exc:
            raise LedgerError(
                f"Historical owned wheel cannot be reproduced: {app_id}"
            ) from exc
        if (
            artifact.get("size") != wheel_info.size
            or artifact.get("sha256") != wheel_info.sha256
        ):
            raise LedgerError(
                f"Historical owned wheel differs from its manifest: {app_id}"
            )
        owned_wheel_requirements[app_id] = wheel_info.requires_dist
        wheel_identity = (
            normalize_distribution(wheel_info.distribution),
            wheel_info.version,
        )
        if expected_origin == "built":
            built_owned_wheels.append(wheel_info)
        release_wheel_identities.append((wheel_identity, wheel_info.sha256))

    try:
        _validate_internal_dependency_contracts(
            app_plans,
            {
                app.app_id: _source_requires_dist(snapshot, app)
                for app in registry.apps
            },
            require_registry_match=True,
            bundled_dependencies=[
                {
                    "component": dependency.component,
                    "distribution": dependency.distribution,
                    "version": dependency.version,
                }
                for dependency in registry.dependencies
            ],
        )
    except (OSError, ReleaseError) as exc:
        raise LedgerError(
            "Historical application dependency contracts cannot be reproduced"
        ) from exc

    provided_sources = []
    for dependency in registry.dependencies:
        prior = previous_artifacts.get(dependency.component)
        reusable = bool(
            prior
            and prior.get("kind") == "third-party"
            and isinstance(prior.get("distribution"), str)
            and normalize_distribution(prior["distribution"])
            == normalize_distribution(dependency.distribution)
            and prior.get("version") == dependency.version
            and prior.get("filename") == dependency.filename
            and prior.get("sha256") == dependency.sha256
        )
        if not reusable:
            provided_sources.append(dependency.source)
    _reject_path_checkout_transformations(
        root,
        source_commit,
        tuple(sorted(provided_sources)),
        context="Historical provided dependency source",
    )

    for dependency in registry.dependencies:
        artifact = artifacts[dependency.component]
        prior = previous_artifacts.get(dependency.component)
        reusable = bool(
            prior
            and prior.get("kind") == "third-party"
            and isinstance(prior.get("distribution"), str)
            and normalize_distribution(prior["distribution"])
            == normalize_distribution(dependency.distribution)
            and prior.get("version") == dependency.version
            and prior.get("filename") == dependency.filename
            and prior.get("sha256") == dependency.sha256
        )
        expected_distribution = (
            prior.get("distribution") if reusable else dependency.distribution
        )
        if (
            artifact.get("kind") != "third-party"
            or artifact.get("distribution") != expected_distribution
            or artifact.get("version") != dependency.version
            or artifact.get("filename") != dependency.filename
            or artifact.get("sha256") != dependency.sha256
        ):
            raise LedgerError(
                "Third-party artifact identity differs from the historical registry: "
                f"{dependency.component}"
            )
        expected_origin = "reused" if reusable else "provided"
        if artifact.get("origin") != expected_origin:
            raise LedgerError(
                "Third-party artifact action differs from the historical plan: "
                f"{dependency.component}"
            )
        if reusable:
            expected_reused = dict(prior)
            expected_reused["origin"] = "reused"
            expected_reused["reused_from"] = previous_platform_version
            if dict(artifact) != expected_reused:
                raise LedgerError(
                    "Reused dependency differs from its immutable predecessor: "
                    f"{dependency.component}"
                )
        elif "reused_from" in artifact:
            raise LedgerError(
                "Provided dependency incorrectly names a prior release: "
                f"{dependency.component}"
            )
        expected_git_blob = (
            prior.get("git_blob_sha") if reusable else dependency.git_blob_sha
        )
        if artifact.get("git_blob_sha") != expected_git_blob:
            raise LedgerError(
                "Third-party artifact Git identity differs from the registry: "
                f"{dependency.component}"
            )
        filename = artifact.get("filename")
        if (
            not isinstance(filename, str)
            or PurePosixPath(filename).name != filename
            or "\\" in filename
        ):
            raise LedgerError(
                f"Third-party artifact has an unsafe filename: {dependency.component}"
            )
        try:
            release_dependency_info = inspect_wheel(
                release_dir / filename,
                expected_distribution=artifact.get("distribution"),
                expected_version=artifact.get("version"),
            )
        except (OSError, ReleaseError) as exc:
            raise LedgerError(
                "Historical third-party wheel cannot be reproduced: "
                f"{dependency.component}"
            ) from exc
        if (
            artifact.get("size") != release_dependency_info.size
            or artifact.get("sha256") != release_dependency_info.sha256
        ):
            raise LedgerError(
                "Historical third-party wheel differs from its manifest: "
                f"{dependency.component}"
            )
        release_wheel_identities.append(
            (
                (
                    normalize_distribution(release_dependency_info.distribution),
                    release_dependency_info.version,
                ),
                release_dependency_info.sha256,
            )
        )
        if not reusable:
            source_info = _inspect_dependency_source(
                snapshot,
                dependency,
                symlinks,
                omitted_release_paths,
            )
            if (
                artifact.get("size") != source_info.size
                or artifact.get("sha256") != source_info.sha256
            ):
                raise LedgerError(
                    "Provided dependency source differs from its historical plan: "
                    f"{dependency.component}"
                )

    try:
        _validate_internal_dependency_contracts(
            app_plans,
            owned_wheel_requirements,
            require_registry_match=True,
            bundled_dependencies=[
                artifact
                for artifact in artifacts.values()
                if artifact.get("kind") == "third-party"
            ],
        )
    except (OSError, ReleaseError) as exc:
        raise LedgerError(
            "Historical owned-wheel dependency contracts cannot be reproduced"
        ) from exc

    platform_entries = app_changes["platform"]
    if not bootstrap:
        previous_build = previous_manifest.get("build")
        if not isinstance(previous_build, Mapping):
            raise LedgerError("Previous verified release has invalid build metadata")
        platform_inputs_changed = any(
            previous_build.get(key) != value for key, value in build.items()
        )
        if platform_inputs_changed and not platform_entries:
            raise LedgerError("Changed platform inputs are missing a platform fragment")
        if not platform_inputs_changed and platform_entries:
            raise LedgerError("Unchanged platform inputs have a stale platform fragment")

    expected_platform_version = (
        SemVer.parse(registry.initial_version)
        if bootstrap
        else SemVer.parse(str(previous_platform_version)).bump(
            _highest_manifest_bump(parsed_changes)
        )
    )
    if release_version != expected_platform_version:
        raise LedgerError(
            "Platform version transition does not match the consumed change fragments"
        )

    _validate_authoritative_plan_replay(
        snapshot,
        source_commit,
        registry.release_root,
        release_dir,
        manifest,
        previous_manifest,
    )

    install_plan = _load_verified_install_plan(release_dir, manifest)
    _validate_install_plan_identity(
        install_plan,
        artifacts,
        registry,
        release_version,
        compatibility_sha256,
    )
    if built_owned_wheels:
        releases = snapshot / "releases"
        if releases.exists() and (not releases.is_dir() or releases.is_symlink()):
            raise LedgerError("Historical releases path is not a regular directory")
        if releases.is_dir():
            legacy_wheels = sorted(
                releases.rglob("*.whl"), key=lambda path: path.as_posix()
            )
            if len(legacy_wheels) > 10_000:
                raise LedgerError("Immutable wheel history exceeds the safe scan limit")
            legacy_wheel_paths = tuple(
                path.relative_to(snapshot).as_posix()
                for path in legacy_wheels
                if not _path_is_within(
                    path.relative_to(snapshot).as_posix(), registry.release_root
                )
            )
            _reject_path_checkout_transformations(
                root,
                source_commit,
                legacy_wheel_paths,
                context="Immutable legacy wheel history",
            )
            for path in legacy_wheels:
                relative = path.relative_to(snapshot).as_posix()
                if _path_is_within(relative, registry.release_root):
                    continue
                if relative in symlinks or not path.is_file():
                    raise LedgerError(
                        f"Immutable release history contains an unsafe wheel: {relative}"
                    )
                try:
                    info = inspect_wheel(path)
                except (OSError, ReleaseError) as exc:
                    raise LedgerError(
                        f"Immutable release history contains an invalid wheel: {relative}"
                    ) from exc
                wheel_history.setdefault(
                    (normalize_distribution(info.distribution), info.version), set()
                ).add(info.sha256)
        for info in built_owned_wheels:
            identity = (normalize_distribution(info.distribution), info.version)
            if any(sha256 != info.sha256 for sha256 in wheel_history.get(identity, set())):
                raise LedgerError(
                    "Immutable wheel identity was previously released with different "
                    f"bytes: {info.distribution} {info.version}"
                )
    if _path_is_within(registry.release_root, "releases"):
        for identity, sha256 in release_wheel_identities:
            wheel_history.setdefault(identity, set()).add(sha256)
    return expected_fragments


def _validate_release_commit_diff(
    root: Path,
    parent: str,
    release_commit: str,
    release_path: str,
    manifest: Mapping[str, Any],
    registry: Any,
    object_id_re: re.Pattern[str],
    release_version: SemVer,
    previous_platform_version: str | None,
    expected_fragments: set[str],
) -> None:
    changes = _commit_changes(root, parent, release_commit)
    release_prefix = release_path + "/"
    release_additions = {
        path
        for path, status in changes.items()
        if status == "A" and path.startswith(release_prefix)
    }
    if not release_additions:
        raise LedgerError("Release commit does not add its immutable release payload")

    manifest_changes = manifest["changes"]
    if previous_platform_version is None:
        expected_platform_version = SemVer.parse(registry.initial_version)
    else:
        expected_platform_version = SemVer.parse(previous_platform_version).bump(
            _highest_manifest_bump(manifest_changes)
        )
    if release_version != expected_platform_version:
        raise LedgerError(
            "Platform version transition does not match the consumed change fragments"
        )

    owned_artifacts = {
        artifact["component"]: artifact
        for artifact in manifest["artifacts"]
        if artifact.get("kind") == "owned"
    }
    if set(owned_artifacts) != set(registry.by_id):
        raise LedgerError(
            "Verified release manifest does not cover exactly the registered applications"
        )
    version_paths: dict[str, str] = {}
    expected_version_changes: set[str] = set()
    for app_id, app in registry.by_id.items():
        path = f"{app.path}/{app.version_file}"
        _validate_changed_path(path)
        if path in version_paths:
            raise LedgerError("Application registry repeats a version-file path")
        version_paths[path] = app_id
        target = owned_artifacts[app_id].get("version")
        try:
            target_version = str(SemVer.parse(target))
        except ReleaseError as exc:
            raise LedgerError(
                f"Manifest has an invalid owned-app version for {app_id}"
            ) from exc
        parent_bytes = _regular_blob_bytes(
            root, parent, path, object_id_re
        )
        release_bytes = _regular_blob_bytes(
            root, release_commit, path, object_id_re
        )
        expected_bytes = _version_replacement(parent_bytes, target_version, path)
        current_version = _version_from_file(parent_bytes, path)
        expected_target = current_version.bump(
            _highest_manifest_bump(manifest_changes, app_id)
        )
        if SemVer.parse(target_version) != expected_target:
            raise LedgerError(
                f"Owned-app version transition does not match consumed fragments: {app_id}"
            )
        if release_bytes != expected_bytes:
            raise LedgerError(
                f"Release commit version file is not the exact planned update: {path}"
            )
        if release_bytes != parent_bytes:
            expected_version_changes.add(path)

    actual_versions: set[str] = set()
    actual_fragments: set[str] = set()
    for path, status in changes.items():
        if status == "A" and path.startswith(release_prefix):
            entry = _tree_entry(root, release_commit, path, object_id_re)
            if entry is None or entry[0] != "100644" or entry[1] != "blob":
                raise LedgerError(
                    f"Release payload entry is not an exact 100644 blob: {path}"
                )
            continue
        if status == "M" and path in version_paths:
            actual_versions.add(path)
            continue
        if status == "D" and path in expected_fragments:
            actual_fragments.add(path)
            continue
        raise LedgerError(f"Forbidden release commit change: {status} {path}")
    if actual_versions != expected_version_changes:
        raise LedgerError(
            "Release commit does not contain exactly the planned app-version updates"
        )
    if actual_fragments != expected_fragments:
        raise LedgerError(
            "Release commit does not delete exactly the manifest-listed change fragments"
        )
    for path in expected_fragments:
        _regular_blob_bytes(root, parent, path, object_id_re)


def _working_release_root(
    root: Path, release_root: str, source_versions: Mapping[SemVer, str]
) -> Path:
    root = root.resolve()
    path = root.joinpath(*PurePosixPath(release_root).parts)
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise LedgerError("Release root resolves outside the repository") from exc
    dirty = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", release_root],
        operation="release-ledger worktree inspection",
    ).stdout
    if dirty:
        raise LedgerError("Working release ledger has uncommitted changes")
    if not source_versions:
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise LedgerError("Working release root is not a regular directory")
        if path.exists() and any(path.iterdir()):
            raise LedgerError("Working release root is not empty in bootstrap state")
        return path
    if not path.is_dir() or path.is_symlink():
        raise LedgerError("Working release root does not match the source commit")
    expected_names = {f"v{version}" for version in source_versions}
    actual_names = {entry.name for entry in path.iterdir()}
    if actual_names != expected_names:
        raise LedgerError("Working release root does not match the source release ledger")
    return path


def verify_ledger(
    root: Path,
    source_commit: str,
    *,
    remote: str = "origin",
    release_root: str = DEFAULT_RELEASE_ROOT,
    target_version: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a canonical-data report after fail-closed ledger verification."""

    root = root.resolve()
    if not root.is_dir():
        raise LedgerError(f"Repository root does not exist: {root}")
    if not REMOTE_NAME_RE.fullmatch(remote):
        raise LedgerError(f"Unsafe Git remote name: {remote!r}")
    release_root = _validate_release_root(release_root)

    object_format = _run_git(
        root,
        ["rev-parse", "--show-object-format"],
        operation="object-format inspection",
    ).stdout.strip()
    object_id_re = _object_id_pattern(object_format)
    if not object_id_re.fullmatch(source_commit):
        raise LedgerError(
            f"Source commit must be a full {object_format} object id"
        )
    resolved_source = _resolve_commit(root, source_commit, object_id_re)
    if resolved_source != source_commit:
        raise LedgerError("Source commit did not resolve to the exact requested commit")
    head = _resolve_commit(root, "HEAD", object_id_re)
    if head != source_commit:
        raise LedgerError(
            "Working tree HEAD does not match the exact source commit being verified"
        )
    registry_relative = "ops/release/apps.toml"
    registry_dirty = _run_git(
        root,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            registry_relative,
        ],
        operation="release registry worktree inspection",
    ).stdout
    if registry_dirty:
        raise LedgerError("Release registry has uncommitted changes")
    current_registry = load_registry(root / registry_relative)
    _validate_registry_git_blob_ids(
        current_registry,
        object_id_re,
        context="Current registry",
    )
    if current_registry.release_root != release_root:
        raise LedgerError(
            "Configured release root does not match the authoritative application registry"
        )
    initial_version = SemVer.parse(current_registry.initial_version)
    shallow = _run_git(
        root,
        ["rev-parse", "--is-shallow-repository"],
        operation="repository-depth inspection",
    ).stdout.strip()
    if shallow != "false":
        raise LedgerError(
            "Release ledger ancestry verification requires complete Git history; "
            "checkout with fetch-depth: 0"
        )
    environment_values = os.environ if environ is None else environ
    token = environment_values.get("GITHUB_TOKEN", "")
    git_environment = _git_environment(token)
    # Preserve explicit test/caller environment while keeping token injection private.
    for key, value in environment_values.items():
        if key not in {
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_TERMINAL_PROMPT",
        }:
            git_environment[key] = value

    first_snapshot = _list_remote_releases(
        root, remote, git_environment, object_id_re
    )
    fetched = _fetch_remote_releases(
        root, remote, first_snapshot, git_environment, object_id_re
    )
    second_snapshot = _list_remote_releases(
        root, remote, git_environment, object_id_re
    )
    if first_snapshot != second_snapshot:
        raise LedgerError("Remote platform release refs changed during verification")

    source_preflight_complete = False
    if not first_snapshot:
        # Bootstrap source has no predecessor whose manifest can decide dependency
        # reuse. Validate before interpreting release_root so dependency source
        # bytes cannot hide inside the directory reserved for immutable output.
        _validate_source_reproducibility(
            root,
            source_commit,
            release_root,
            current_registry,
            None,
        )
        source_preflight_complete = True

    source_versions = _release_versions_at_commit(
        root, source_commit, release_root, object_id_re
    )
    remote_versions = set(first_snapshot)
    local_versions = set(source_versions)
    if remote_versions and min(remote_versions) != initial_version:
        raise LedgerError(
            "The first immutable release ref does not match the registry initial_version"
        )
    missing_from_source = sorted(remote_versions - local_versions)
    if missing_from_source:
        missing = ", ".join(
            f"release/platform-v{version}" for version in missing_from_source
        )
        raise LedgerError(
            f"Stale main release ledger: {missing} is published remotely but absent "
            "from the source commit; merge the published release synchronization PR "
            "into main first"
        )
    unpublished = sorted(local_versions - remote_versions)
    if unpublished:
        values = ", ".join(f"v{version}" for version in unpublished)
        raise LedgerError(
            f"Source release ledger contains versions without immutable release refs: {values}"
        )

    working_root = _working_release_root(root, release_root, source_versions)
    records: list[dict[str, Any]] = []
    previous_manifest: Mapping[str, Any] | None = None
    previous_record: Mapping[str, Any] | None = None
    wheel_history: dict[tuple[str, str], set[str]] = {}
    for version in sorted(remote_versions):
        branch_ref, advertised_commit = first_snapshot[version]
        release_commit = fetched[version]
        if release_commit != advertised_commit:
            raise LedgerError(f"Fetched release identity changed for {branch_ref}")
        _require_ancestor(root, release_commit, source_commit)
        parent = _commit_parent(root, release_commit, object_id_re)
        release_path = f"{release_root}/v{version}"
        release_dir = working_root / f"v{version}"
        manifest = verify_release_dir(release_dir)
        try:
            release_payload_paths = tuple(
                sorted(
                    path.relative_to(root).as_posix()
                    for path in release_dir.rglob("*")
                    if path.is_file()
                )
            )
        except (OSError, ValueError) as exc:
            raise LedgerError(
                f"Verified release payload paths cannot be inspected for {branch_ref}"
            ) from exc
        _reject_path_checkout_transformations(
            root,
            release_commit,
            release_payload_paths,
            context="Historical release payload",
        )
        if source_commit != release_commit:
            _reject_path_checkout_transformations(
                root,
                source_commit,
                release_payload_paths,
                context="Current release payload",
            )
        if manifest.get("platform_version") != str(version):
            raise LedgerError(
                f"Branch, directory, and verified manifest versions disagree for {branch_ref}"
            )
        manifest_source = manifest.get("source_commit")
        if not isinstance(manifest_source, str) or not object_id_re.fullmatch(
            manifest_source
        ):
            raise LedgerError(
                f"Manifest source commit does not match repository object format for {branch_ref}"
            )
        if parent != manifest_source:
            raise LedgerError(
                f"Release commit parent does not match manifest source_commit for {branch_ref}"
            )
        with _materialized_source_commit(
            root, parent, release_root
        ) as (source_snapshot, source_symlinks, omitted_release_paths):
            historical_registry = _load_historical_registry(
                source_snapshot, parent
            )
            _validate_registry_git_blob_ids(
                historical_registry,
                object_id_re,
                context=f"Historical registry for {branch_ref}",
            )
            if (
                historical_registry.platform_id != current_registry.platform_id
                or historical_registry.release_root != release_root
                or historical_registry.initial_version != current_registry.initial_version
            ):
                raise LedgerError(
                    f"Historical registry changes immutable platform identity for {branch_ref}"
                )
            if manifest.get("platform_id") != historical_registry.platform_id:
                raise LedgerError(
                    f"Verified manifest platform_id does not match the registry for {branch_ref}"
                )
            _validate_third_party_artifact_git_blob_ids(
                manifest,
                object_id_re,
                context=f"Immutable {branch_ref}",
            )
            expected_fragments = _validate_historical_plan_semantics(
                root,
                parent,
                source_snapshot,
                source_symlinks,
                omitted_release_paths,
                release_dir,
                manifest,
                previous_manifest,
                historical_registry,
                version,
                wheel_history,
            )
            _validate_release_commit_diff(
                root,
                parent,
                release_commit,
                release_path,
                manifest,
                historical_registry,
                object_id_re,
                version,
                (
                    previous_record["platform_version"]
                    if previous_record is not None
                    else None
                ),
                expected_fragments,
            )

        try:
            expected_readme = render_release_readme_v1(
                platform_version=manifest["platform_version"],
                source_commit=manifest["source_commit"],
                changes=manifest["changes"],
                artifacts=manifest["artifacts"],
            )
            actual_readme = (release_dir / "README.md").read_bytes()
        except (KeyError, OSError, ReleaseError) as exc:
            raise LedgerError(
                f"Verified release README cannot be reproduced for {branch_ref}"
            ) from exc
        if actual_readme != expected_readme:
            raise LedgerError(
                f"Verified release README is not canonical for {branch_ref}"
            )

        release_trees = _release_versions_at_commit(
            root, release_commit, release_root, object_id_re
        )
        release_tree = release_trees.get(version)
        source_tree = source_versions[version]
        if release_tree is None or release_tree != source_tree:
            raise LedgerError(
                f"Stale main release ledger: {release_path} is not byte-identical "
                f"between {branch_ref} and the source commit"
            )
        prior_trees = {
            SemVer.parse(item["platform_version"]): item["release_tree"]
            for item in records
        }
        parent_trees = _release_versions_at_commit(
            root, parent, release_root, object_id_re
        )
        if parent_trees != prior_trees:
            raise LedgerError(
                f"Release commit parent does not contain the exact prior immutable "
                f"release ledger for {branch_ref}"
            )
        expected_release_trees = dict(prior_trees)
        expected_release_trees[version] = release_tree
        if release_trees != expected_release_trees:
            raise LedgerError(
                f"Release commit must add only its new immutable release directory "
                f"within {release_root}: {branch_ref}"
            )
        manifest_digest = sha256_file(release_dir / "RELEASE.json")
        predecessor = manifest.get("previous_release")
        if previous_manifest is None:
            if predecessor is not None:
                raise LedgerError(
                    f"First platform release v{version} must have no predecessor"
                )
        else:
            assert previous_record is not None
            expected_predecessor = {
                "platform_version": previous_manifest["platform_version"],
                "source_commit": previous_manifest["source_commit"],
                "manifest_sha256": previous_record["manifest_sha256"],
            }
            _require_ancestor(root, previous_record["release_commit"], parent)
            if predecessor != expected_predecessor:
                legacy = LEGACY_DEPLOYMENT_BRIDGES.get(str(version))
                if legacy != {
                    "manifest_sha256": manifest_digest,
                    "release_commit": release_commit,
                    "source_commit": manifest_source,
                }:
                    raise LedgerError(
                        f"Release v{version} does not name its exact immediate predecessor"
                    )
                matches = [
                    (index, record)
                    for index, record in enumerate(records)
                    if predecessor
                    == {
                        "platform_version": record["platform_version"],
                        "source_commit": record["source_commit"],
                        "manifest_sha256": record["manifest_sha256"],
                    }
                ]
                if len(matches) != 1:
                    raise LedgerError(
                        f"Historical release v{version} predecessor is not an exact "
                        "verified ledger release"
                    )
                predecessor_index, predecessor_record = matches[0]
                try:
                    payload = deployment_payload_identity(
                        working_root
                        / f"v{predecessor_record['platform_version']}"
                    )
                    for skipped_record in records[predecessor_index + 1 :]:
                        if deployment_payload_identity(
                            working_root
                            / f"v{skipped_record['platform_version']}"
                        ) != payload:
                            raise LedgerError(
                                f"Historical release v{version} crosses changed "
                                f"payload v{skipped_record['platform_version']}"
                            )
                    if deployment_payload_identity(release_dir) != payload:
                        raise LedgerError(
                            f"Historical release v{version} reconciliation payload changed"
                        )
                except (OSError, ReleaseError) as exc:
                    raise LedgerError(
                        f"Historical release v{version} reconciliation cannot be verified"
                    ) from exc

        record = {
            "platform_version": str(version),
            "ref": branch_ref,
            "release_commit": release_commit,
            "release_path": release_path,
            "release_tree": release_tree,
            "source_commit": manifest_source,
            "manifest_sha256": manifest_digest,
        }
        records.append(record)
        previous_manifest = manifest
        previous_record = record

    if not source_preflight_complete:
        _validate_source_reproducibility(
            root,
            source_commit,
            release_root,
            current_registry,
            previous_manifest,
        )

    target: dict[str, Any] | None = None
    if target_version is not None:
        parsed_target = SemVer.parse(target_version)
        if not records and parsed_target != initial_version:
            raise LedgerError(
                "Bootstrap target version must equal the registry initial_version"
            )
        if records and parsed_target <= SemVer.parse(records[-1]["platform_version"]):
            raise LedgerError(
                "Target platform version must be newer than the verified release ledger"
            )
        if parsed_target in remote_versions or parsed_target in local_versions:
            raise LedgerError(f"Target platform version already exists: {parsed_target}")
        target = {
            "platform_version": str(parsed_target),
            "ref": f"{REMOTE_REF_PREFIX}{parsed_target}",
            "release_path": f"{release_root}/v{parsed_target}",
            "absent": True,
        }

    closing_snapshot = _list_remote_releases(
        root, remote, git_environment, object_id_re
    )
    if closing_snapshot != first_snapshot:
        raise LedgerError(
            "Remote platform release refs changed during ledger verification"
        )

    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "object_format": object_format,
        "platform_id": current_registry.platform_id,
        "initial_version": str(initial_version),
        "source_commit": source_commit,
        "release_root": release_root,
        "bootstrap": not records,
        "releases": records,
        "latest": records[-1] if records else None,
        "target": target,
    }


def _write_report(path: Path, report_bytes: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise LedgerError(f"Refusing to overwrite ledger report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(report_bytes)
    except OSError as exc:
        raise LedgerError(f"Could not write ledger report: {path}") from exc


def _github_output_lines(report: Mapping[str, Any], report_path: Path) -> list[str]:
    latest = report["latest"] or {}
    target = report["target"] or {}
    return [
        f"ledger_report={report_path}",
        f"platform_id={report['platform_id']}",
        f"initial_version={report['initial_version']}",
        f"bootstrap={'true' if report['bootstrap'] else 'false'}",
        f"release_count={len(report['releases'])}",
        f"latest_version={latest.get('platform_version', '')}",
        f"latest_ref={latest.get('ref', '')}",
        f"latest_release_commit={latest.get('release_commit', '')}",
        f"latest_source_commit={latest.get('source_commit', '')}",
        f"latest_manifest_sha256={latest.get('manifest_sha256', '')}",
        f"latest_release_path={latest.get('release_path', '')}",
        f"target_version={target.get('platform_version', '')}",
        f"target_ref={target.get('ref', '')}",
        f"target_release_path={target.get('release_path', '')}",
        f"target_version_absent={'true' if target.get('absent') else ''}",
    ]


def _append_github_output(
    path_value: str | None, report: Mapping[str, Any], report_path: Path
) -> None:
    if not path_value:
        return
    path = Path(path_value)
    lines = _github_output_lines(report, report_path)
    if any("\n" in line or "\r" in line for line in lines):
        raise LedgerError("Refusing unsafe multiline GitHub output")
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise LedgerError("Could not append GitHub workflow outputs") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the B-UH immutable platform-release ledger"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="verify remote/source ledger parity")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--source-commit", required=True)
    verify.add_argument("--remote", default="origin")
    verify.add_argument("--release-root", default=DEFAULT_RELEASE_ROOT)
    verify.add_argument("--target-version")
    verify.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command != "verify":  # Defensive if another subcommand is added.
            raise LedgerError(f"Unsupported ledger command: {args.command}")
        report = verify_ledger(
            args.root,
            args.source_commit,
            remote=args.remote,
            release_root=args.release_root,
            target_version=args.target_version,
        )
        encoded = _canonical_json_bytes(report)
        _write_report(args.output, encoded)
        _append_github_output(os.environ.get("GITHUB_OUTPUT"), report, args.output)
        sys.stdout.buffer.write(encoded)
        return 0
    except (LedgerError, ReleaseError) as exc:
        parser.exit(2, f"ledger error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
