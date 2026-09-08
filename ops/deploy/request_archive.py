#!/usr/bin/env python3
"""Build the deterministic, bounded archive consumed by the trusted receiver."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:  # Support the reviewed command-line entry point.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.release import buh_release, recovery_policy


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_RELEASE_ENTRIES = 128
MAX_ENTRY_BYTES = 64 * 1024 * 1024
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class RequestArchiveError(RuntimeError):
    """A request archive input did not match reviewed immutable state."""


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise RequestArchiveError("Git identity verification could not run") from exc
    if result.returncode != 0:
        raise RequestArchiveError(
            f"Git identity verification failed with exit code {result.returncode}"
        )
    return result.stdout.strip()


def _target_identity(
    manifest: Mapping[str, Any], release_commit: str
) -> dict[str, str]:
    version = manifest["platform_version"]
    return {
        "manifest_sha256": "",  # filled after the exact release path is known
        "platform_version": version,
        "release_commit": release_commit,
        "release_ref": f"release/platform-v{version}",
        "source_commit": manifest["source_commit"],
    }


def _verify_release_commit(
    root: Path,
    release_dir: Path,
    release_commit: str,
    manifest: Mapping[str, Any],
) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", release_commit) is None:
        raise RequestArchiveError("Release commit is not a full GitHub object ID")
    resolved = _git(root, "rev-parse", "--verify", f"{release_commit}^{{commit}}")
    if resolved != release_commit:
        raise RequestArchiveError("Release commit did not resolve exactly")
    parents = _git(root, "rev-list", "--parents", "-n", "1", release_commit).split()
    if len(parents) != 2 or parents[0] != release_commit:
        raise RequestArchiveError("Release commit does not have one exact source parent")
    if parents[1] != manifest.get("source_commit"):
        raise RequestArchiveError("Release commit parent differs from the manifest source")
    relative = release_dir.resolve().relative_to(root.resolve()).as_posix()
    tracked = _git(root, "ls-tree", "-r", "--name-only", release_commit, "--", relative)
    expected = sorted(f"{relative}/{path.name}" for path in release_dir.iterdir())
    if tracked.splitlines() != expected:
        raise RequestArchiveError("Release directory differs from its exact commit tree")
    for path in release_dir.iterdir():
        committed = subprocess.run(
            ["git", "show", f"{release_commit}:{relative}/{path.name}"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if committed.returncode != 0 or committed.stdout != path.read_bytes():
            raise RequestArchiveError("Release bytes differ from their exact commit tree")


def _regular_release_entries(release_dir: Path) -> list[Path]:
    entries = sorted(release_dir.iterdir(), key=lambda item: item.name)
    if not entries or len(entries) > MAX_RELEASE_ENTRIES:
        raise RequestArchiveError("Release bundle has an unsafe entry count")
    for path in entries:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or path.is_symlink()
            or details.st_size <= 0
            or details.st_size > MAX_ENTRY_BYTES
        ):
            raise RequestArchiveError(f"Release entry is unsafe: {path.name}")
    return entries


def _recovery_transition(
    *,
    manifest: Mapping[str, Any],
    target: Mapping[str, str],
    bootstrap_recovery: bool,
) -> dict[str, Any] | None:
    policy = recovery_policy.load_policy()
    fixed = policy["published_releases"]
    expected_manifest = recovery_policy.manifest_recovery(policy)
    declared = manifest.get("deployment_recovery")
    if bootstrap_recovery:
        if declared is not None or dict(target) != fixed[-1]:
            raise RequestArchiveError(
                "Receiver-upgrade bootstrap must target exact immutable v0.6.1"
            )
        purpose = "receiver-upgrade-preflight"
        releases = fixed
    elif declared is not None:
        if declared != expected_manifest:
            raise RequestArchiveError("Release recovery attestation changed")
        purpose = "production-recovery"
        releases = [*fixed, dict(target)]
    else:
        return None
    return {
        "policy_id": policy["policy_id"],
        "policy_sha256": recovery_policy.policy_sha256(),
        "purpose": purpose,
        "releases": releases,
    }


def build_archive(
    *,
    root: Path,
    release_dir: Path,
    repository: str,
    release_commit: str,
    mode: str,
    workflow_run_id: str,
    workflow_run_attempt: int,
    output: Path,
    bootstrap_recovery: bool = False,
    metadata_output: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    release_dir = release_dir.resolve()
    if not release_dir.is_relative_to(root) or output.exists() or output.is_symlink():
        raise RequestArchiveError("Request archive paths are unsafe")
    if mode not in {"preflight", "deploy"}:
        raise RequestArchiveError("Request archive mode is invalid")
    if RUN_ID_RE.fullmatch(workflow_run_id) is None:
        raise RequestArchiveError("Request archive run ID is invalid")
    if type(workflow_run_attempt) is not int or not 1 <= workflow_run_attempt <= 1000:
        raise RequestArchiveError("Request archive run attempt is invalid")
    try:
        manifest = buh_release.verify_release_dir(release_dir)
    except (OSError, buh_release.ReleaseError) as exc:
        raise RequestArchiveError("Target release verification failed") from exc
    _verify_release_commit(root, release_dir, release_commit, manifest)
    manifest_digest = buh_release.sha256_file(release_dir / "RELEASE.json")
    target = _target_identity(manifest, release_commit)
    target["manifest_sha256"] = manifest_digest
    transition = _recovery_transition(
        manifest=manifest,
        target=target,
        bootstrap_recovery=bootstrap_recovery,
    )
    if bootstrap_recovery and mode != "preflight":
        raise RequestArchiveError("Receiver-upgrade bootstrap can only be a preflight")
    request = {
        "manifest_sha256": manifest_digest,
        "mode": mode,
        "platform_version": manifest["platform_version"],
        "release_commit": release_commit,
        "release_ref": f"release/platform-v{manifest['platform_version']}",
        "repository": repository,
        "schema_version": 2 if transition is not None else 1,
        "workflow_run_attempt": workflow_run_attempt,
        "workflow_run_id": workflow_run_id,
        **({"recovery_transition": transition} if transition is not None else {}),
    }
    request_bytes = recovery_policy.canonical_json_bytes(request)
    if len(request_bytes) > 16 * 1024:
        raise RequestArchiveError("Deployment request is unexpectedly large")
    entries = _regular_release_entries(release_dir)

    lineage: list[tuple[str, bytes]] = []
    if transition is not None:
        for identity in transition["releases"][:-1]:
            version = identity["platform_version"]
            lineage_dir = root / "releases" / "platform" / f"v{version}"
            try:
                lineage_manifest = buh_release.verify_release_dir(lineage_dir)
            except (OSError, buh_release.ReleaseError) as exc:
                raise RequestArchiveError(
                    f"Recovery lineage v{version} failed verification"
                ) from exc
            _verify_release_commit(
                root,
                lineage_dir,
                identity["release_commit"],
                lineage_manifest,
            )
            source = lineage_dir / "RELEASE.json"
            if (
                lineage_manifest["source_commit"] != identity["source_commit"]
                or buh_release.sha256_file(source) != identity["manifest_sha256"]
            ):
                raise RequestArchiveError(f"Recovery lineage v{version} changed")
            lineage.append((f"v{version}.RELEASE.json", source.read_bytes()))

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
                ) as archive:
                    def add_bytes(name: str, data: bytes, mode_value: int = 0o644) -> None:
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        info.mode = mode_value
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(data))

                    add_bytes("REQUEST.json", request_bytes, 0o600)
                    release_info = tarfile.TarInfo("release")
                    release_info.type = tarfile.DIRTYPE
                    release_info.mode = 0o755
                    release_info.uid = release_info.gid = 0
                    release_info.uname = release_info.gname = ""
                    release_info.mtime = 0
                    archive.addfile(release_info)
                    for path in entries:
                        add_bytes(f"release/{path.name}", path.read_bytes())
                    if lineage:
                        lineage_info = tarfile.TarInfo("lineage")
                        lineage_info.type = tarfile.DIRTYPE
                        lineage_info.mode = 0o755
                        lineage_info.uid = lineage_info.gid = 0
                        lineage_info.uname = lineage_info.gname = ""
                        lineage_info.mtime = 0
                        archive.addfile(lineage_info)
                        for name, data in lineage:
                            add_bytes(f"lineage/{name}", data)
        if output.stat().st_size > MAX_ARCHIVE_BYTES:
            raise RequestArchiveError("Deployment archive exceeds the receiver maximum")
        metadata = {
            "manifest_sha256": manifest_digest,
            "platform_version": manifest["platform_version"],
            "recovery_transition_sha256": (
                recovery_policy.recovery_digest(transition)
                if transition is not None
                else None
            ),
            "release_refs": (
                transition["releases"] if transition is not None else [target]
            ),
            "schema_version": 1,
        }
        if metadata_output is not None:
            if metadata_output.exists() or metadata_output.is_symlink():
                raise RequestArchiveError("Request metadata output already exists")
            metadata_output.parent.mkdir(parents=True, exist_ok=True)
            metadata_output.write_bytes(recovery_policy.canonical_json_bytes(metadata))
        return metadata
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-commit", required=True)
    parser.add_argument("--mode", choices=("preflight", "deploy"), required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--bootstrap-recovery", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        metadata = build_archive(
            root=arguments.root,
            release_dir=arguments.release_dir,
            repository=arguments.repository,
            release_commit=arguments.release_commit,
            mode=arguments.mode,
            workflow_run_id=arguments.workflow_run_id,
            workflow_run_attempt=arguments.workflow_run_attempt,
            output=arguments.output,
            bootstrap_recovery=arguments.bootstrap_recovery,
            metadata_output=arguments.metadata_output,
        )
    except (RequestArchiveError, recovery_policy.RecoveryPolicyError) as exc:
        print(f"request archive error: {str(exc)[:300]}", file=os.sys.stderr)
        return 2
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
