"""Forced-command entry point for guarded Platform v2 deployments."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from .contracts import (
    DeploymentError,
    ReceiverConfig,
    extract_archive,
    load_validated_bundle,
    read_bounded_stream,
)
from .docker_host import DockerHost
from .engine import (
    DeploymentEngine,
    DeploymentJournal,
    PreflightEngine,
    PreflightJournal,
)


COMMANDS = {
    "preflight platform-v2": "preflight",
    "deploy platform-v2": "deploy",
}


class LockBusy(DeploymentError):
    """Raised when another deployment owns the host lock."""


def _verify_root_owned_ancestors(path: Path) -> None:
    """Reject lock paths reachable through a replaceable directory ancestor."""

    if not path.is_absolute() or path == Path("/"):
        raise DeploymentError("Deployment path is not a safe absolute path")
    current = Path("/")
    for part in path.parent.parts[1:]:
        current /= part
        try:
            details = current.lstat()
        except OSError as exc:
            raise DeploymentError("Deployment path ancestor is unavailable") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            raise DeploymentError("Deployment path ancestor is unsafe")


def _arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Receive a checksum-verified B-UH Platform v2 release"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/buh-platform-v2/receiver.json"),
    )
    parser.add_argument(
        "--inherited-lock-fd",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "forced_mode",
        nargs="?",
        choices=("preflight", "deploy"),
        help="Trusted mode supplied by the installed forced-command dispatcher",
    )
    return parser.parse_args(argv)


def _forced_mode(explicit_mode: str | None) -> str:
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    original_mode = COMMANDS.get(original)
    if explicit_mode is not None:
        if original and original_mode != explicit_mode:
            raise DeploymentError(
                "The forced mode does not match the original SSH command"
            )
        return explicit_mode
    mode = original_mode
    if mode is None:
        raise DeploymentError("The forced command rejected this SSH request")
    return mode


def _open_lock(config: ReceiverConfig, inherited_descriptor: int | None = None) -> int:
    if os.geteuid() != 0:
        raise DeploymentError("Platform v2 receiver must run as root")
    _verify_root_owned_ancestors(config.state_dir)
    _verify_root_owned_ancestors(config.backup_dir)
    try:
        details = config.state_dir.lstat()
        backup_details = config.backup_dir.lstat()
    except OSError as exc:
        raise DeploymentError("Deployment state or backup directory is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or config.state_dir.is_symlink()
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise DeploymentError("Deployment state directory is not private and root-owned")
    if (
        not stat.S_ISDIR(backup_details.st_mode)
        or config.backup_dir.is_symlink()
        or backup_details.st_uid != 0
        or stat.S_IMODE(backup_details.st_mode) != 0o700
    ):
        raise DeploymentError("Deployment backup directory is not private and root-owned")
    lock_path = config.state_dir / "deploy.lock"
    if inherited_descriptor is not None:
        if type(inherited_descriptor) is not int or inherited_descriptor < 3:
            raise DeploymentError("Inherited deployment lock descriptor is invalid")
        try:
            path_details = lock_path.stat(follow_symlinks=False)
            lock_details = os.fstat(inherited_descriptor)
        except OSError as exc:
            raise DeploymentError("Inherited deployment lock is unavailable") from exc
        if (
            not stat.S_ISREG(path_details.st_mode)
            or path_details.st_uid != 0
            or stat.S_IMODE(path_details.st_mode) != 0o600
            or not stat.S_ISREG(lock_details.st_mode)
            or lock_details.st_uid != 0
            or stat.S_IMODE(lock_details.st_mode) != 0o600
            or (path_details.st_dev, path_details.st_ino)
            != (lock_details.st_dev, lock_details.st_ino)
        ):
            raise DeploymentError("Inherited deployment lock identity is invalid")
        descriptor = os.dup(inherited_descriptor)
        try:
            os.set_inheritable(descriptor, False)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise LockBusy("Another Platform v2 operation is already running") from exc
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        lock_details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_details.st_mode)
            or lock_details.st_uid != 0
            or stat.S_IMODE(lock_details.st_mode) != 0o600
        ):
            raise DeploymentError("Deployment lock is not a private root-owned file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusy("Another Platform v2 operation is already running") from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def receive(
    config_path: Path,
    stream,
    explicit_mode: str | None = None,
    inherited_lock_descriptor: int | None = None,
) -> dict[str, object]:
    """Process exactly one archive from a forced SSH command."""

    forced_mode = _forced_mode(explicit_mode)
    config = ReceiverConfig.load(config_path)
    lock = _open_lock(config, inherited_lock_descriptor)
    try:
        recovery = DockerHost.recover_incomplete_plan(config)
        if recovery is not None:
            raise DeploymentError(
                recovery
                + " The new archive was not read; submit a fresh workflow attempt."
            )
        archive = read_bounded_stream(stream, config.max_archive_bytes)
        with tempfile.TemporaryDirectory(
            prefix="incoming-", dir=config.state_dir
        ) as temporary:
            root = Path(temporary) / "payload"
            extract_archive(archive, root)
            bundle = load_validated_bundle(root, config)
            if bundle.request.mode != forced_mode:
                raise DeploymentError(
                    "The deployment request mode does not match the forced command"
                )
            host = DockerHost(config)
            if forced_mode == "preflight":
                journal = PreflightJournal(config.state_dir, bundle)
                PreflightEngine(host, journal).run(bundle)
                return {
                    "schema_version": 1,
                    "result": "preflight-passed",
                    "platform_version": bundle.request.platform_version,
                    "manifest_sha256": bundle.request.manifest_sha256,
                }
            journal = DeploymentJournal(config.state_dir, bundle)
            DeploymentEngine(host, journal).run(bundle)
            return {
                "schema_version": 1,
                "result": "verified",
                "platform_version": bundle.request.platform_version,
                "manifest_sha256": bundle.request.manifest_sha256,
            }
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(sys.argv[1:] if argv is None else argv)
    try:
        result = receive(
            arguments.config,
            sys.stdin.buffer,
            arguments.forced_mode,
            arguments.inherited_lock_fd,
        )
    except LockBusy as exc:
        print(str(exc), file=sys.stderr)
        return 73
    except DeploymentError as exc:
        # Detailed command output is already bounded and redacted by the backend.
        # Only the first line crosses the SSH boundary; the journal and observer
        # diagnostics retain the host-side detail.
        message = str(exc).splitlines()[0][:500]
        print(f"Platform v2 operation failed: {message}", file=sys.stderr)
        return 65
    except BaseException as exc:  # pragma: no cover - last-resort host boundary
        print(
            f"Platform v2 operation failed internally: {exc.__class__.__name__}",
            file=sys.stderr,
        )
        return 70
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
