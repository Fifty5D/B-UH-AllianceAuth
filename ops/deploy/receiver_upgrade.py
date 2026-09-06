"""Transactional installer for the reviewed Platform v2 receiver upgrade.

This module is invoked only by ``upgrade-receiver.sh`` after that script has
verified the exact reviewed source export and run the deployment test suite.  It
keeps a verified, root-only snapshot of every receiver path and restores the exact
previous present/absent state after any activation or preflight failure.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import DeploymentError, ReceiverConfig


SCHEMA_VERSION = 1
TARGETS = (
    "/usr/local/lib/buh-platform-v2",
    "/etc/buh-platform-v2",
    "/usr/local/bin/buh-github-observe-entry",
    "/usr/local/sbin/buh-github-observe-root",
    "/usr/local/libexec/buh-redact-diagnostics",
    "/usr/local/sbin/buh-platform-v2-receiver",
    "/etc/sudoers.d/buh-platform-v2",
    # The live forced command reaches this path. Replace it last.
    "/usr/local/sbin/buh-deploy-dispatch",
)
RECOVERY_SOURCES = (
    "ops/__init__.py",
    "ops/deploy/__init__.py",
    "ops/deploy/contracts.py",
    "ops/deploy/receiver_upgrade.py",
)
INSTALL_SOURCES = (
    "ops/__init__.py",
    "ops/buh-github-observe-entry",
    "ops/buh-github-observe-root",
    "ops/buh-redact-diagnostics.py",
    "ops/deploy/__init__.py",
    "ops/deploy/contracts.py",
    "ops/deploy/docker_host.py",
    "ops/deploy/engine.py",
    "ops/deploy/receiver.py",
    "ops/deploy/buh-deploy-dispatch",
    "ops/deploy/buh-platform-v2-receiver",
    "ops/release/__init__.py",
    "ops/release/buh_release.py",
)
NESTED_PREFLIGHT_TIMEOUT_SECONDS = 3600
NESTED_ROLLBACK_GRACE_SECONDS = 300
NESTED_KILL_REAP_SECONDS = 30
CANONICAL_CONFIG_PATH = "/etc/buh-platform-v2/receiver.json"
CANONICAL_LEGACY_RECEIVER_PATH = (
    "/usr/local/sbin/buh-moon-tax-platform-remote"
)


class UpgradeError(RuntimeError):
    """A fail-closed receiver upgrade error safe to show to the operator."""


class _NestedPreflightRecoveryError(UpgradeError):
    """The child preflight stopped, but its application rollback is unresolved."""


class _TerminationGuard:
    """Turn catchable termination into rollback and defer repeats during restore."""

    def __init__(self) -> None:
        self.rollback_started = False
        self.interrupted = False
        self.signal_number: int | None = None

    def begin_rollback(self) -> None:
        self.rollback_started = True

    def handle(self, signum: int, _frame: Any) -> None:
        if self.rollback_started or self.interrupted:
            return
        self.interrupted = True
        self.signal_number = signum
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover - Python supplied the signal number
            name = str(signum)
        raise UpgradeError(f"Receiver upgrade interrupted by {name}.")


@contextmanager
def _guard_termination_signals():
    """Install transaction-scoped HUP/INT/TERM handlers on the main thread."""

    guard = _TerminationGuard()
    previous: dict[int, Any] = {}
    watched = tuple(
        value
        for value in (
            getattr(signal, "SIGHUP", None),
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
        )
        if value is not None
    )
    try:
        for value in watched:
            previous[value] = signal.getsignal(value)
            signal.signal(value, guard.handle)
    except (OSError, ValueError) as exc:
        for value, handler in previous.items():
            signal.signal(value, handler)
        raise UpgradeError("Receiver termination guards could not be installed.") from exc
    try:
        yield guard
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _actual(system_root: Path, logical: str) -> Path:
    if not logical.startswith("/") or ".." in Path(logical).parts:
        raise UpgradeError("Receiver target is not a safe absolute path.")
    return system_root / logical.removeprefix("/")


def _private_directory_owner(system_root: Path) -> int | None:
    """Return the required private-directory owner (root in production)."""

    if os.name != "posix":
        return None
    if system_root == Path("/"):
        return 0
    # Alternate system roots are test fixtures and must still be owned by the
    # account executing the test; production never takes this branch.
    return os.geteuid()


def _verify_private_directory(path: Path, *, owner_uid: int | None) -> None:
    """Reject redirected, shared, or incorrectly owned transaction storage."""

    if not path.is_absolute() or path == Path("/"):
        raise UpgradeError("Receiver private directory path is unsafe.")
    try:
        details = path.lstat()
    except OSError as exc:
        raise UpgradeError("Receiver private directory is unavailable.") from exc
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise UpgradeError("Receiver private directory is not a real directory.")
    if owner_uid is not None and (
        details.st_uid != owner_uid or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise UpgradeError("Receiver private directory ownership or mode is unsafe.")


def _verify_private_file(
    path: Path,
    *,
    owner_uid: int | None,
    maximum_bytes: int = 1024 * 1024,
) -> None:
    """Reject redirected or non-private receiver transaction metadata."""

    try:
        details = path.lstat()
    except OSError as exc:
        raise UpgradeError("Receiver private file is unavailable.") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or not 1 <= details.st_size <= maximum_bytes
        or (
            owner_uid is not None
            and (
                details.st_uid != owner_uid
                or stat.S_IMODE(details.st_mode) != 0o600
            )
        )
    ):
        raise UpgradeError("Receiver private file ownership or mode is unsafe.")


def _verify_root_owned_ancestor_chain(path: Path, *, include_path: bool = False) -> None:
    """Reject symlinked or unprivileged-writable production path ancestors."""

    if os.name != "posix":
        return
    if not path.is_absolute() or path == Path("/"):
        raise UpgradeError("Receiver production path is unsafe.")
    limit = path if include_path else path.parent
    current = Path("/")
    for part in limit.parts[1:]:
        current /= part
        try:
            details = current.lstat()
        except OSError as exc:
            raise UpgradeError("Receiver production path ancestor is unavailable.") from exc
        if (
            not stat.S_ISDIR(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != 0
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            raise UpgradeError("Receiver production path ancestor is unsafe.")


def _verify_production_path_bindings() -> None:
    """Bind privileged installation and locking to the reviewed host paths."""

    if (
        os.environ.get("BUH_RECEIVER_CONFIG_PATH") != CANONICAL_CONFIG_PATH
        or os.environ.get("BUH_LEGACY_RECEIVER_PATH")
        != CANONICAL_LEGACY_RECEIVER_PATH
        or CANONICAL_LEGACY_RECEIVER_PATH in TARGETS
    ):
        raise UpgradeError("Canonical receiver path binding is invalid.")


def _verify_live_production_inputs(config: Path, legacy_receiver: Path) -> None:
    """Recheck the root-owned live inputs that privileged policy is bound to."""

    expected = (
        (
            Path(CANONICAL_CONFIG_PATH),
            os.environ.get("BUH_PINNED_CONFIG_SHA256", ""),
            True,
            False,
        ),
        (
            Path(CANONICAL_LEGACY_RECEIVER_PATH),
            os.environ.get("BUH_PINNED_LEGACY_SHA256", ""),
            False,
            True,
        ),
    )
    for path, digest, private, executable in expected:
        _verify_root_owned_ancestor_chain(path)
        try:
            details = path.lstat()
        except OSError as exc:
            raise UpgradeError("Canonical receiver input is unavailable.") from exc
        mode = stat.S_IMODE(details.st_mode)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_uid != 0
            or details.st_size <= 0
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or (private and mode != 0o600)
            or (not private and mode & 0o022)
            or (executable and not mode & 0o111)
            or _sha256(path) != digest
        ):
            raise UpgradeError("Canonical receiver input identity is invalid.")
    if (
        _sha256(config) != expected[0][1]
        or _sha256(legacy_receiver) != expected[1][1]
    ):
        raise UpgradeError("Pinned receiver input differs from its canonical path.")


def _open_production_lock(config: ReceiverConfig) -> int:
    """Acquire the receiver's production lock without importing mutable live code."""

    if os.name != "posix" or os.geteuid() != 0:
        raise DeploymentError("Platform v2 receiver upgrade must run as root")
    import fcntl

    _verify_root_owned_ancestor_chain(config.state_dir)
    _verify_root_owned_ancestor_chain(config.backup_dir)
    try:
        details = config.state_dir.lstat()
    except OSError as exc:
        raise DeploymentError("Deployment state directory is unavailable") from exc
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_uid != 0
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        raise DeploymentError("Deployment state directory is not private and root-owned")

    try:
        backup_details = config.backup_dir.lstat()
    except OSError as exc:
        raise DeploymentError("Deployment backup directory is unavailable") from exc
    if (
        not stat.S_ISDIR(backup_details.st_mode)
        or stat.S_ISLNK(backup_details.st_mode)
        or backup_details.st_uid != 0
        or stat.S_IMODE(backup_details.st_mode) != 0o700
    ):
        raise DeploymentError("Deployment backup directory is not private and root-owned")

    lock_path = config.state_dir / "deploy.lock"
    flags = os.O_RDWR | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise DeploymentError("Deployment lock is unavailable") from exc
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
            raise DeploymentError(
                "Another Platform v2 operation is already running"
            ) from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _entry(path: Path, relative: str) -> dict[str, Any]:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode):
        raise UpgradeError("Receiver backup refuses symbolic links.")
    if stat.S_ISREG(value.st_mode):
        kind = "file"
    elif stat.S_ISDIR(value.st_mode):
        kind = "directory"
    else:
        raise UpgradeError("Receiver backup accepts only regular files and directories.")
    result: dict[str, Any] = {
        "gid": value.st_gid,
        "kind": kind,
        "mode": stat.S_IMODE(value.st_mode),
        "path": relative,
        "uid": value.st_uid,
    }
    if kind == "file":
        result["sha256"] = _sha256(path)
    return result


def _scan(path: Path) -> list[dict[str, Any]]:
    if not path.exists() and not path.is_symlink():
        return []
    entries = [_entry(path, ".")]
    if not path.is_dir():
        return entries
    for directory, names, files in os.walk(path, followlinks=False):
        base = Path(directory)
        names.sort()
        files.sort()
        for name in names + files:
            child = base / name
            relative = child.relative_to(path).as_posix()
            entries.append(_entry(child, relative))
    return entries


def _staged_manifest(staging_root: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    target_roots = tuple(_actual(staging_root, logical) for logical in TARGETS)
    for logical, path in zip(TARGETS, target_roots, strict=True):
        if not path.exists() or path.is_symlink():
            raise UpgradeError("Receiver staging output is incomplete or unsafe.")
        result[logical] = _scan(path)
    for directory, names, files in os.walk(staging_root, followlinks=False):
        base = Path(directory)
        for name in names:
            child = base / name
            if child.is_symlink():
                raise UpgradeError("Receiver staging output contains a symbolic link.")
        for name in files:
            child = base / name
            if child.is_symlink() or not any(
                child == root or root in child.parents for root in target_roots
            ):
                raise UpgradeError("Receiver staging output contains an unexpected file.")
    return result


def _verify_real_staged_contract(
    staging_root: Path,
    repo_root: Path,
    config: Path,
    commit: str,
    legacy_receiver_path: str,
    *,
    require_root_owner: bool,
) -> None:
    """Compare installer output to the exact reviewed install contract."""

    if legacy_receiver_path != CANONICAL_LEGACY_RECEIVER_PATH:
        raise UpgradeError("Receiver staging legacy path is not canonical.")
    provenance = {
        "config_sha256": _sha256(config),
        "files": {
            relative: _sha256(repo_root / relative) for relative in INSTALL_SOURCES
        },
        "schema_version": 1,
        "source_commit": commit,
    }
    generated = (
        json.dumps(provenance, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")
    expected_files: dict[str, tuple[bytes, int]] = {
        "/etc/buh-platform-v2/receiver.json": (config.read_bytes(), 0o600),
        "/etc/buh-platform-v2/INSTALL.json": (generated, 0o600),
        "/usr/local/sbin/buh-platform-v2-receiver": (
            (repo_root / "ops/deploy/buh-platform-v2-receiver").read_bytes(),
            0o755,
        ),
        "/usr/local/sbin/buh-deploy-dispatch": (
            (repo_root / "ops/deploy/buh-deploy-dispatch").read_bytes(),
            0o755,
        ),
        "/usr/local/bin/buh-github-observe-entry": (
            (repo_root / "ops/buh-github-observe-entry").read_bytes(),
            0o755,
        ),
        "/usr/local/sbin/buh-github-observe-root": (
            (repo_root / "ops/buh-github-observe-root").read_bytes(),
            0o755,
        ),
        "/usr/local/libexec/buh-redact-diagnostics": (
            (repo_root / "ops/buh-redact-diagnostics.py").read_bytes(),
            0o755,
        ),
        "/etc/sudoers.d/buh-platform-v2": (
            (
                f"buh-deployer ALL=(root) NOPASSWD: {legacy_receiver_path}\n"
                "buh-deployer ALL=(root) NOPASSWD: /usr/local/sbin/"
                "buh-platform-v2-receiver preflight\n"
                "buh-deployer ALL=(root) NOPASSWD: /usr/local/sbin/"
                "buh-platform-v2-receiver deploy\n"
            ).encode("utf-8"),
            0o440,
        ),
    }
    library_mapping = {
        "ops/__init__.py": "ops/__init__.py",
        "ops/deploy/__init__.py": "ops/deploy/__init__.py",
        "ops/deploy/contracts.py": "ops/deploy/contracts.py",
        "ops/deploy/docker_host.py": "ops/deploy/docker_host.py",
        "ops/deploy/engine.py": "ops/deploy/engine.py",
        "ops/deploy/receiver.py": "ops/deploy/receiver.py",
        "ops/release/__init__.py": "ops/release/__init__.py",
        "ops/release/buh_release.py": "ops/release/buh_release.py",
    }
    for installed, relative in library_mapping.items():
        expected_files[f"/usr/local/lib/buh-platform-v2/{installed}"] = (
            (repo_root / relative).read_bytes(),
            0o644,
        )
    expected_directories = {
        "/usr/local/lib/buh-platform-v2": 0o755,
        "/usr/local/lib/buh-platform-v2/ops": 0o755,
        "/usr/local/lib/buh-platform-v2/ops/deploy": 0o755,
        "/usr/local/lib/buh-platform-v2/ops/release": 0o755,
        "/etc/buh-platform-v2": 0o700,
    }
    actual_files: dict[str, tuple[bytes, int]] = {}
    actual_directories: dict[str, int] = {}
    for logical in TARGETS:
        root = _actual(staging_root, logical)
        paths = [root] if root.is_file() else [root, *sorted(root.rglob("*"))]
        for path in paths:
            details = path.lstat()
            installed = "/" + path.relative_to(staging_root).as_posix()
            if stat.S_ISREG(details.st_mode):
                actual_files[installed] = (
                    path.read_bytes(),
                    stat.S_IMODE(details.st_mode),
                )
            elif stat.S_ISDIR(details.st_mode):
                actual_directories[installed] = stat.S_IMODE(details.st_mode)
            else:  # _staged_manifest also rejects this; keep this check standalone.
                raise UpgradeError("Receiver staging output contains an unsafe path.")
            if require_root_owner and (details.st_uid != 0 or details.st_gid != 0):
                raise UpgradeError("Receiver staging ownership is not root-owned.")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise UpgradeError("Receiver staging output differs from the install contract.")


def _verify_live_matches_staged(
    system_root: Path,
    staged_manifest: dict[str, list[dict[str, Any]]],
) -> None:
    """Prove every promoted target still exactly matches its staged manifest."""

    if set(staged_manifest) != set(TARGETS):
        raise UpgradeError("Receiver staging manifest has an invalid target set.")
    for logical in TARGETS:
        live = _actual(system_root, logical)
        if not live.exists() or live.is_symlink():
            raise UpgradeError("Activated receiver output is incomplete or unsafe.")
        if _scan(live) != staged_manifest[logical]:
            raise UpgradeError("Activated receiver output differs from its verified staging.")


def _verify_reviewed_source(repo_root: Path, commit: str) -> None:
    """Recheck the exact reviewed allowlist export without importing from it."""

    inventory = Path(os.environ.get("BUH_REVIEWED_INVENTORY", ""))
    inventory_sha256 = os.environ.get("BUH_REVIEWED_INVENTORY_SHA256", "")
    if (
        not inventory.is_absolute()
        or not inventory.is_file()
        or inventory.is_symlink()
        or len(inventory_sha256) != 64
        or _sha256(inventory) != inventory_sha256
    ):
        raise UpgradeError("Reviewed receiver inventory identity is invalid.")
    expected: dict[str, tuple[int, str]] = {}
    for line in inventory.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(
            r"(100644|100755) blob ([0-9a-f]{40})\t([A-Za-z0-9._/-]+)",
            line,
        )
        if match is None or match.group(3) in expected:
            raise UpgradeError("Reviewed receiver inventory is malformed.")
        expected[match.group(3)] = (int(match.group(1)[-3:], 8), match.group(2))
    actual = {
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual != set(expected):
        raise UpgradeError("Reviewed receiver source contains missing or extra files.")
    for relative, (mode, object_id) in expected.items():
        path = repo_root / relative
        details = path.lstat()
        data = path.read_bytes()
        digest = hashlib.sha1(
            f"blob {len(data)}\0".encode("ascii") + data
        ).hexdigest()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or stat.S_IMODE(details.st_mode) != mode
            or digest != object_id
        ):
            raise UpgradeError("Reviewed receiver source differs from its inventory.")
    if os.environ.get("BUH_REVIEWED_COMMIT") != commit:
        raise UpgradeError("Reviewed receiver source commit identity changed.")


def _copy_tree(source: Path, destination: Path, entries: Sequence[dict[str, Any]]) -> None:
    if destination.exists() or destination.is_symlink():
        raise UpgradeError("Receiver staging destination already exists.")
    if source.is_dir():
        shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    # chown can clear special permission bits, so restore ownership first and mode second.
    for entry in sorted(entries, key=lambda item: item["path"].count("/"), reverse=True):
        target = destination if entry["path"] == "." else destination / entry["path"]
        if hasattr(os, "chown"):
            os.chown(target, entry["uid"], entry["gid"], follow_symlinks=False)
        try:
            os.chmod(target, entry["mode"], follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.chmod(target, entry["mode"])


def _remove(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _rename_exchange(first: Path, second: Path) -> bool:
    """Atomically exchange two paths on the Linux production host."""

    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:  # pragma: no cover - unsupported Linux libc
        raise UpgradeError("Atomic receiver path exchange is unavailable.")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_exchange = 2
    if (
        renameat2(
            at_fdcwd,
            os.fsencode(first),
            at_fdcwd,
            os.fsencode(second),
            rename_exchange,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(first), str(second))
    return True


def _promote_candidate(candidate: Path, destination: Path) -> Path | None:
    """Put candidate at destination and return the still-retained previous path."""

    if not destination.exists() and not destination.is_symlink():
        os.replace(candidate, destination)
        return None
    if _rename_exchange(candidate, destination):
        # renameat2(RENAME_EXCHANGE) leaves the previous destination at candidate.
        return candidate

    # Windows-only local tests use a two-rename fallback. Production Linux fails
    # closed above if atomic exchange is unavailable.
    previous = destination.with_name(
        f".{destination.name}.buh-previous-{secrets.token_hex(8)}"
    )
    os.replace(destination, previous)
    try:
        os.replace(candidate, destination)
    except BaseException:
        os.replace(previous, destination)
        raise
    return previous


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if os.name == "posix":
        descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(path: Path) -> None:
    """Durably flush every retained recovery byte and directory bottom-up."""

    if os.name != "posix":
        return
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode):
        raise UpgradeError("Receiver recovery material contains a symbolic link.")
    if stat.S_ISREG(details.st_mode):
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    if not stat.S_ISDIR(details.st_mode):
        raise UpgradeError("Receiver recovery material contains an unsafe path.")
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        _fsync_tree(child)
    _fsync_directory(path)


def _target_parent_directories(system_root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            {_actual(system_root, logical).parent for logical in TARGETS},
            key=lambda path: (len(path.parts), str(path)),
        )
    )


def _verify_target_parents(system_root: Path) -> None:
    """Require every live rename parent and ancestor to pre-exist safely."""

    if system_root == Path("/"):
        for parent in _target_parent_directories(system_root):
            _verify_root_owned_ancestor_chain(parent, include_path=True)
        return
    checked: set[Path] = set()
    for parent in _target_parent_directories(system_root):
        relative = parent.relative_to(system_root)
        current = system_root
        for part in relative.parts:
            current /= part
            if current in checked:
                continue
            checked.add(current)
            try:
                details = current.lstat()
            except OSError as exc:
                raise UpgradeError(
                    "Receiver target parent is unavailable; no activation is safe."
                ) from exc
            if (
                not stat.S_ISDIR(details.st_mode)
                or stat.S_ISLNK(details.st_mode)
            ):
                raise UpgradeError("Receiver target parent is unsafe.")


def _fsync_live_targets(system_root: Path) -> None:
    """Durably flush target bytes and every parent containing a live rename."""

    _verify_target_parents(system_root)
    for logical in TARGETS:
        target = _actual(system_root, logical)
        if target.exists() or target.is_symlink():
            _fsync_tree(target)
    for parent in reversed(_target_parent_directories(system_root)):
        _fsync_directory(parent)


def _checked_sha256(path: Path, context: str) -> str:
    try:
        return _sha256(path)
    except OSError as exc:
        raise UpgradeError(f"{context} is unreadable.") from exc


def _retain_recovery_material(
    backup: Path, repo_root: Path, config: Path, commit: str
) -> Path:
    """Retain the exact, root-only code and config needed for manual recovery."""

    source_root = backup / "recovery-source"
    source_root.mkdir(mode=0o700)
    files: dict[str, str] = {}
    for relative in RECOVERY_SOURCES:
        source = repo_root / relative
        if not source.is_file() or source.is_symlink():
            raise UpgradeError("Receiver recovery source is missing or unsafe.")
        destination = source_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, destination, follow_symlinks=False)
        os.chmod(destination, 0o600)
        files[relative] = _sha256(destination)

    recovery_config = backup / "recovery-config.json"
    shutil.copyfile(config, recovery_config, follow_symlinks=False)
    os.chmod(recovery_config, 0o600)
    manifest_path = backup / "recovery-manifest.json"
    _atomic_json(
        manifest_path,
        {
            "config_sha256": _sha256(recovery_config),
            "files": files,
            "schema_version": SCHEMA_VERSION,
            "source_commit": commit,
        },
    )
    return manifest_path


def _load_json_object(path: Path, context: str) -> dict[str, Any]:
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"{context} is unreadable.") from exc
    if not data or len(data) > 1024 * 1024 or not isinstance(value, dict):
        raise UpgradeError(f"{context} is malformed.")
    return value


class ReceiverTransaction:
    """Retained, secret-free recovery state for an upgrade attempt."""

    def __init__(
        self,
        backup: Path,
        commit: str,
        manifest_path: Path,
        recovery_manifest_path: Path,
    ) -> None:
        self.path = backup / "transaction.json"
        self.commit = commit
        self.manifest_path = manifest_path
        self.recovery_manifest_path = recovery_manifest_path

    def record(self, state: str) -> None:
        if state not in {
            "backup-verified",
            "activation-in-progress",
            "completed",
            "not-activated",
            "rolled-back",
            "rollback-failed",
        }:
            raise UpgradeError("Receiver transaction state is invalid.")
        def value(recorded_state: str) -> dict[str, Any]:
            return {
                "backup_manifest_sha256": _sha256(self.manifest_path),
                "recovery_manifest_sha256": _sha256(
                    self.recovery_manifest_path
                ),
                "recovery_required": recorded_state
                in {"activation-in-progress", "rollback-failed"},
                "schema_version": SCHEMA_VERSION,
                "source_commit": self.commit,
                "state": recorded_state,
            }

        try:
            _atomic_json(self.path, value(state))
        except BaseException:
            if state in {"completed", "rolled-back"}:
                try:
                    _atomic_json(self.path, value("rollback-failed"))
                except BaseException:
                    # The preceding durable in-progress/failure marker remains
                    # the authoritative recovery requirement when this fails.
                    pass
            raise


class ReceiverSnapshot:
    """A verified filesystem snapshot of the fixed receiver target set."""

    def __init__(self, backup: Path, system_root: Path = Path("/")) -> None:
        self.backup = backup
        self.system_root = system_root
        self.payload = backup / "payload"
        self.manifest_path = backup / "manifest.json"
        self.marker_path = backup / "VERIFIED.sha256"

    def create(self) -> None:
        self.backup.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.payload.mkdir(mode=0o700)
        targets: list[dict[str, Any]] = []
        for logical in TARGETS:
            source = _actual(self.system_root, logical)
            present = source.exists() or source.is_symlink()
            record: dict[str, Any] = {"path": logical, "present": present}
            if present:
                entries = _scan(source)
                destination = _actual(self.payload, logical)
                _copy_tree(source, destination, entries)
                if _scan(destination) != entries:
                    raise UpgradeError("Receiver backup verification failed.")
                record["entries"] = entries
            targets.append(record)
        _atomic_json(
            self.manifest_path,
            {"schema_version": SCHEMA_VERSION, "targets": targets},
        )
        marker = f"{_sha256(self.manifest_path)}  manifest.json\n"
        with self.marker_path.open("x", encoding="ascii", newline="\n") as handle:
            handle.write(marker)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.marker_path, 0o600)
        if os.name == "posix":
            descriptor = os.open(
                self.backup,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self.verify()

    def _manifest(self) -> dict[str, Any]:
        try:
            marker = self.marker_path.read_text(encoding="ascii").split()
            if marker != [_sha256(self.manifest_path), "manifest.json"]:
                raise UpgradeError("Receiver backup verification marker is invalid.")
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UpgradeError("Receiver backup metadata is unreadable.") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "targets"}:
            raise UpgradeError("Receiver backup metadata is malformed.")
        if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["targets"], list):
            raise UpgradeError("Receiver backup schema is unsupported.")
        if [item.get("path") for item in value["targets"] if isinstance(item, dict)] != list(
            TARGETS
        ):
            raise UpgradeError("Receiver backup target list is invalid.")
        return value

    def verify(self) -> None:
        value = self._manifest()
        for record in value["targets"]:
            if set(record) not in ({"path", "present"}, {"entries", "path", "present"}):
                raise UpgradeError("Receiver backup target metadata is malformed.")
            if not isinstance(record["present"], bool):
                raise UpgradeError("Receiver backup presence metadata is malformed.")
            backup_path = _actual(self.payload, record["path"])
            if record["present"]:
                if not isinstance(record.get("entries"), list):
                    raise UpgradeError("Receiver backup entries are missing.")
                if _scan(backup_path) != record["entries"]:
                    raise UpgradeError("Receiver backup payload does not match its manifest.")
            elif backup_path.exists() or backup_path.is_symlink() or "entries" in record:
                raise UpgradeError("Absent receiver path unexpectedly has backup content.")

    def restore(self) -> None:
        value = self._manifest()
        self.verify()
        _verify_target_parents(self.system_root)
        for record in value["targets"]:
            destination = _actual(self.system_root, record["path"])
            if not record["present"]:
                if destination.exists() or destination.is_symlink():
                    retired = destination.with_name(
                        f".{destination.name}.buh-failed-{secrets.token_hex(8)}"
                    )
                    os.replace(destination, retired)
                    _remove(retired)
                continue
            source = _actual(self.payload, record["path"])
            candidate = destination.with_name(
                f".{destination.name}.buh-restore-{secrets.token_hex(8)}"
            )
            _copy_tree(source, candidate, record["entries"])
            if _scan(candidate) != record["entries"]:
                raise UpgradeError("Receiver restore candidate failed verification.")
            retired = _promote_candidate(candidate, destination)
            if _scan(destination) != record["entries"]:
                raise UpgradeError("Receiver restore activation failed verification.")
            if retired is not None:
                _remove(retired)
        self.verify_live()
        _fsync_live_targets(self.system_root)
        self.verify_live()

    def verify_live(self) -> None:
        value = self._manifest()
        for record in value["targets"]:
            live = _actual(self.system_root, record["path"])
            present = live.exists() or live.is_symlink()
            if present != record["present"]:
                raise UpgradeError("Restored receiver presence does not match the backup.")
            if present and _scan(live) != record["entries"]:
                raise UpgradeError("Restored receiver content does not match the backup.")


def activate_staged(
    staging_root: Path,
    system_root: Path = Path("/"),
    before_promote: Callable[[int, str], None] | None = None,
    after_promote: Callable[[int, str], None] | None = None,
) -> None:
    """Promote staged receiver paths, atomically per target, dispatcher last."""

    retired: list[Path] = []
    candidates: list[Path] = []
    _verify_target_parents(system_root)
    try:
        for index, logical in enumerate(TARGETS):
            source = _actual(staging_root, logical)
            if not source.exists() or source.is_symlink():
                raise UpgradeError("Receiver staging output is incomplete or unsafe.")
            entries = _scan(source)
            destination = _actual(system_root, logical)
            candidate = destination.with_name(
                f".{destination.name}.buh-candidate-{secrets.token_hex(8)}"
            )
            candidates.append(candidate)
            _copy_tree(source, candidate, entries)
            if _scan(candidate) != entries:
                raise UpgradeError("Receiver staging copy failed verification.")

            if before_promote is not None:
                before_promote(index, logical)
            old = _promote_candidate(candidate, destination)
            if old is not None:
                retired.append(old)
            if after_promote is not None:
                after_promote(index, logical)
    finally:
        for path in candidates + retired:
            _remove(path)


InstallCallback = Callable[[Path], None]
PreflightCallback = Callable[[], None]


def _signal_nested_preflight(process: subprocess.Popen[Any], signum: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        else:  # pragma: no cover - production is Linux
            process.send_signal(signum)
    except ProcessLookupError:
        return


def _kill_nested_preflight(process: subprocess.Popen[Any]) -> None:
    """Unconditionally stop the nested process group after rollback grace."""

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - production is Linux
            process.kill()
    except ProcessLookupError:
        return


def _check_nested_preflight_recovery(recovery_check: Callable[[], None]) -> None:
    """Convert every child recovery failure into a state the outer rollback tracks."""

    try:
        recovery_check()
    except BaseException as exc:
        raise _NestedPreflightRecoveryError(
            "Application preflight rollback could not be verified."
        ) from exc


def _run_nested_preflight(
    *,
    arguments: Sequence[str],
    input_handle: Any,
    environment: dict[str, str],
    production_lock_fd: int,
    termination: _TerminationGuard,
    recovery_check: Callable[[], None],
    success_check: Callable[[], None],
) -> None:
    """Let the nested engine finish its traffic-first rollback on TERM/timeout."""

    try:
        process = subprocess.Popen(
            list(arguments),
            stdin=input_handle,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(production_lock_fd,),
            start_new_session=True,
        )
    except OSError as exc:
        raise UpgradeError("No-change receiver preflight could not start.") from exc
    try:
        returncode = process.wait(timeout=NESTED_PREFLIGHT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _signal_nested_preflight(process, signal.SIGTERM)
        try:
            process.wait(timeout=NESTED_ROLLBACK_GRACE_SECONDS)
        except subprocess.TimeoutExpired as rollback_timeout:
            _kill_nested_preflight(process)
            try:
                process.wait(timeout=NESTED_KILL_REAP_SECONDS)
            except subprocess.TimeoutExpired as reap_timeout:
                _check_nested_preflight_recovery(recovery_check)
                raise UpgradeError(
                    "No-change receiver preflight could not be reaped after termination."
                ) from reap_timeout
            _check_nested_preflight_recovery(recovery_check)
            raise UpgradeError(
                "No-change receiver preflight timed out and did not complete rollback."
            ) from rollback_timeout
        _check_nested_preflight_recovery(recovery_check)
        raise UpgradeError(
            "No-change receiver preflight timed out after coordinated rollback."
        ) from exc
    except BaseException:
        signum = termination.signal_number or signal.SIGTERM
        _signal_nested_preflight(process, signum)
        try:
            process.wait(timeout=NESTED_ROLLBACK_GRACE_SECONDS)
        except subprocess.TimeoutExpired as rollback_timeout:
            _kill_nested_preflight(process)
            try:
                process.wait(timeout=NESTED_KILL_REAP_SECONDS)
            except subprocess.TimeoutExpired as reap_timeout:
                _check_nested_preflight_recovery(recovery_check)
                raise UpgradeError(
                    "Interrupted receiver preflight could not be reaped."
                ) from reap_timeout
            _check_nested_preflight_recovery(recovery_check)
            raise UpgradeError(
                "Interrupted receiver preflight did not complete rollback."
            ) from rollback_timeout
        _check_nested_preflight_recovery(recovery_check)
        raise
    if returncode != 0:
        _check_nested_preflight_recovery(recovery_check)
        raise UpgradeError("No-change receiver preflight failed.")
    try:
        success_check()
    except BaseException as exc:
        _check_nested_preflight_recovery(recovery_check)
        raise UpgradeError(
            "No-change receiver preflight reported success with unresolved recovery state."
        ) from exc


def _verify_nested_preflight_recovery(config_path: Path) -> None:
    """Recover and then prove no child deployment recovery marker remains."""

    config = ReceiverConfig.load(config_path)
    # Import from the exact reviewed source used to start this upgrade process,
    # not from the newly activated or previous installed receiver tree.
    from .docker_host import DockerHost

    DockerHost.recover_incomplete_plan(config)
    marker = config.state_dir / "active-recovery.json"
    try:
        details = marker.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise UpgradeError(
            "Nested receiver preflight recovery marker remains active."
        )
    raise UpgradeError("Nested receiver preflight recovery marker is unsafe.")


def _verify_nested_preflight_success(config_path: Path) -> None:
    """A successful child must leave no application recovery plan behind."""

    config = ReceiverConfig.load(config_path)
    marker = config.state_dir / "active-recovery.json"
    try:
        marker.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UpgradeError(
            "Nested receiver preflight recovery state is unreadable."
        ) from exc
    raise UpgradeError("Nested receiver preflight left active recovery state.")


def _recover_application_before_receiver_restore(
    system_root: Path, recovery_config: Path
) -> None:
    """Resolve an interrupted child deployment using clean reviewed code first."""

    if system_root == Path("/"):
        _verify_nested_preflight_recovery(recovery_config)


def execute_upgrade(
    *,
    commit: str,
    repo_root: Path,
    config: Path,
    legacy_receiver: Path,
    request: Path,
    system_root: Path = Path("/"),
    backup_root: Path = Path("/var/backups/buh-receiver-upgrade"),
    install_callback: InstallCallback | None = None,
    preflight_callback: PreflightCallback | None = None,
    after_promote: Callable[[int, str], None] | None = None,
    production_lock_fd: int | None = None,
) -> tuple[Path, bool]:
    """Install and preflight one receiver upgrade, restoring on every failure."""

    pinned_inputs: dict[Path, str] = {}
    if install_callback is None:
        _verify_production_path_bindings()
        try:
            for variable, path in (
                ("BUH_PINNED_CONFIG_SHA256", config),
                ("BUH_PINNED_LEGACY_SHA256", legacy_receiver),
                ("BUH_PINNED_REQUEST_SHA256", request),
            ):
                expected = os.environ.get(variable, "")
                if (
                    len(expected) != 64
                    or any(
                        character not in "0123456789abcdef"
                        for character in expected
                    )
                    or _sha256(path) != expected
                ):
                    raise UpgradeError("Pinned receiver input identity is invalid.")
                pinned_inputs[path] = expected
        except OSError as exc:
            raise UpgradeError("Pinned receiver input identity is invalid.") from exc
    if system_root == Path("/") and production_lock_fd is None:
        raise UpgradeError("The production deployment lock is not held.")
    if install_callback is None and system_root == Path("/"):
        _verify_live_production_inputs(config, legacy_receiver)

    private_owner = _private_directory_owner(system_root)
    try:
        _verify_target_parents(system_root)
        if system_root == Path("/"):
            _verify_root_owned_ancestor_chain(backup_root)
        backup_root.mkdir(mode=0o700, parents=False, exist_ok=True)
        _verify_private_directory(backup_root, owner_uid=private_owner)
        backup = backup_root / (
            f"buh-receiver-{commit[:12]}-{secrets.token_hex(8)}"
        )
        snapshot = ReceiverSnapshot(backup, system_root)
        snapshot.create()
        _verify_private_directory(backup, owner_uid=private_owner)
        recovery_manifest = _retain_recovery_material(
            backup, repo_root, config, commit
        )
        transaction = ReceiverTransaction(
            backup,
            commit,
            snapshot.manifest_path,
            recovery_manifest,
        )
        transaction.record("backup-verified")
        _fsync_tree(backup)
        _fsync_directory(backup_root)
        # Emit the exact recovery handle before any live path can change.  This
        # contains no request/configuration data and remains useful if SIGKILL or
        # host loss prevents the normal final status message.
        if system_root == Path("/"):
            print(f"Receiver verified backup created: {backup}", flush=True)
    except Exception as exc:
        raise UpgradeError(
            "Verified receiver backup could not be created; no installed path changed."
        ) from exc

    staging_root: Path | None = None
    try:
        staging_parent = Path("/var/tmp") if os.name == "posix" else None
        staging_root = Path(
            tempfile.mkdtemp(prefix="buh-receiver-stage-", dir=staging_parent)
        )
        _verify_private_directory(staging_root, owner_uid=private_owner)
    except (OSError, UpgradeError) as exc:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)
        raise UpgradeError(
            f"Receiver staging could not be created; verified backup retained: {backup}"
        ) from exc
    assert staging_root is not None
    restored = False
    live_mutation_started = False

    def mark_live_mutation(_index: int, _logical: str) -> None:
        nonlocal live_mutation_started
        live_mutation_started = True

    try:
        with _guard_termination_signals() as termination:
            try:
                real_installer = install_callback is None
                verify_real_install = real_installer and (
                    system_root == Path("/")
                    or bool(os.environ.get("BUH_REVIEWED_INVENTORY"))
                )
                if real_installer:
                    environment = {
                        "HOME": "/root",
                        "LANG": "C.UTF-8",
                        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "PYTHONPATH": str(repo_root),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                    for name in (
                        "BUH_REVIEWED_COMMIT",
                        "BUH_REVIEWED_INVENTORY",
                        "BUH_RECEIVER_CONFIG_PATH",
                        "BUH_PINNED_CONFIG_SHA256",
                        "BUH_PINNED_LEGACY_SHA256",
                        "BUH_PINNED_REQUEST_SHA256",
                        "BUH_LEGACY_RECEIVER_PATH",
                        "GIT_CONFIG_GLOBAL",
                        "GIT_CONFIG_NOSYSTEM",
                    ):
                        if name in os.environ:
                            environment[name] = os.environ[name]
                    environment["BUH_RECEIVER_INSTALL_ROOT"] = str(staging_root)
                    result = subprocess.run(
                        [
                            str(repo_root / "ops/deploy/install-receiver.sh"),
                            str(config),
                            str(legacy_receiver),
                        ],
                        cwd=repo_root,
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=1800,
                    )
                    if result.returncode != 0:
                        raise UpgradeError("Staged receiver installation failed.")
                else:
                    assert install_callback is not None
                    install_callback(staging_root)

                for path, expected in pinned_inputs.items():
                    if _sha256(path) != expected:
                        raise UpgradeError("Pinned receiver input changed during staging.")

                staged_manifest = _staged_manifest(staging_root)
                if verify_real_install:
                    _verify_reviewed_source(repo_root, commit)
                    _verify_real_staged_contract(
                        staging_root,
                        repo_root,
                        config,
                        commit,
                        os.environ["BUH_LEGACY_RECEIVER_PATH"],
                        require_root_owner=system_root == Path("/"),
                    )
                snapshot.verify_live()
                if _staged_manifest(staging_root) != staged_manifest:
                    raise UpgradeError("Receiver staging output changed before activation.")
                if verify_real_install:
                    _verify_reviewed_source(repo_root, commit)
                    _verify_real_staged_contract(
                        staging_root,
                        repo_root,
                        config,
                        commit,
                        os.environ["BUH_LEGACY_RECEIVER_PATH"],
                        require_root_owner=system_root == Path("/"),
                    )
                if real_installer and system_root == Path("/"):
                    _verify_live_production_inputs(config, legacy_receiver)
                # This conservative marker is retained if SIGKILL, host loss, or a
                # power failure prevents either completion or automatic rollback.
                transaction.record("activation-in-progress")
                activate_staged(
                    staging_root,
                    system_root,
                    before_promote=mark_live_mutation,
                    after_promote=after_promote,
                )
                _verify_live_matches_staged(system_root, staged_manifest)
                _fsync_live_targets(system_root)
                _verify_live_matches_staged(system_root, staged_manifest)
                if real_installer and system_root == Path("/"):
                    _verify_live_production_inputs(config, legacy_receiver)
                if preflight_callback is None:
                    if production_lock_fd is None:
                        raise UpgradeError("The production deployment lock is not held.")
                    library = _actual(system_root, "/usr/local/lib/buh-platform-v2")
                    if request in pinned_inputs and _sha256(request) != pinned_inputs[request]:
                        raise UpgradeError("Pinned preflight input changed before use.")
                    environment = {
                        "HOME": "/root",
                        "LANG": "C.UTF-8",
                        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "PYTHONPATH": str(library),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                    with request.open("rb") as input_handle:
                        _run_nested_preflight(
                            arguments=[
                                "/usr/bin/python3",
                                "-P",
                                "-m",
                                "ops.deploy.receiver",
                                "--config",
                                str(config),
                                "--inherited-lock-fd",
                                str(production_lock_fd),
                                "preflight",
                            ],
                            input_handle=input_handle,
                            environment=environment,
                            production_lock_fd=production_lock_fd,
                            termination=termination,
                            recovery_check=lambda: _verify_nested_preflight_recovery(
                                config
                            ),
                            success_check=lambda: _verify_nested_preflight_success(
                                config
                            ),
                        )
                else:
                    preflight_callback()
                _fsync_live_targets(system_root)
                _verify_live_matches_staged(system_root, staged_manifest)
                if real_installer and system_root == Path("/"):
                    _verify_live_production_inputs(config, legacy_receiver)
                transaction.record("completed")
            except BaseException as exc:
                if live_mutation_started:
                    termination.begin_rollback()
                    try:
                        snapshot.restore()
                        restored = True
                    except BaseException as restore_exc:
                        try:
                            transaction.record("rollback-failed")
                        except BaseException:
                            # The in-progress marker remains conservative evidence.
                            pass
                        raise UpgradeError(
                            "Receiver upgrade and exact rollback both failed."
                        ) from restore_exc
                    if isinstance(exc, _NestedPreflightRecoveryError):
                        transaction.record("rollback-failed")
                        raise UpgradeError(
                            "Receiver paths were restored, but the application "
                            "preflight rollback remains unresolved; manual recovery "
                            f"is required from verified backup: {backup}"
                        ) from exc
                    transaction.record("rolled-back")
                    if isinstance(exc, UpgradeError):
                        raise UpgradeError(f"{exc} Verified backup restored: {backup}") from exc
                    raise UpgradeError(
                        f"Receiver upgrade failed; verified backup restored: {backup}"
                    ) from exc
                transaction.record("not-activated")
                if isinstance(exc, UpgradeError):
                    raise UpgradeError(
                        f"{exc} No installed path changed; backup: {backup}"
                    ) from exc
                raise UpgradeError(
                    "Receiver upgrade failed before activation; no installed path changed; "
                    f"backup: {backup}"
                ) from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return backup, restored


def recover_backup(
    *,
    backup: Path,
    commit: str,
    system_root: Path = Path("/"),
    production_lock_fd: int | None = None,
) -> None:
    """Restore one retained in-progress receiver backup under the caller's lock."""

    if (
        len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
        or (system_root == Path("/") and production_lock_fd is None)
    ):
        raise UpgradeError("Manual receiver recovery authorization is invalid.")
    private_owner = _private_directory_owner(system_root)
    if system_root == Path("/"):
        _verify_root_owned_ancestor_chain(backup, include_path=True)
    _verify_private_directory(backup, owner_uid=private_owner)
    for directory in (
        backup / "payload",
        backup / "recovery-source",
        backup / "recovery-source/ops",
        backup / "recovery-source/ops/deploy",
    ):
        _verify_private_directory(directory, owner_uid=private_owner)
    for metadata in (
        backup / "transaction.json",
        backup / "manifest.json",
        backup / "VERIFIED.sha256",
        backup / "recovery-manifest.json",
        backup / "recovery-config.json",
    ):
        _verify_private_file(metadata, owner_uid=private_owner)
    transaction_value = _load_json_object(
        backup / "transaction.json", "Receiver transaction"
    )
    if set(transaction_value) != {
        "backup_manifest_sha256",
        "recovery_manifest_sha256",
        "recovery_required",
        "schema_version",
        "source_commit",
        "state",
    }:
        raise UpgradeError("Receiver transaction is malformed.")
    if (
        transaction_value["schema_version"] != SCHEMA_VERSION
        or transaction_value["source_commit"] != commit
        or transaction_value["recovery_required"] is not True
        or transaction_value["state"]
        not in {"activation-in-progress", "rollback-failed"}
    ):
        raise UpgradeError("Receiver transaction does not require recovery.")

    snapshot = ReceiverSnapshot(backup, system_root)
    if transaction_value["backup_manifest_sha256"] != _checked_sha256(
        snapshot.manifest_path, "Receiver backup manifest"
    ):
        raise UpgradeError("Receiver transaction backup identity is invalid.")
    snapshot.verify()

    recovery_manifest_path = backup / "recovery-manifest.json"
    if transaction_value["recovery_manifest_sha256"] != _checked_sha256(
        recovery_manifest_path, "Receiver recovery manifest"
    ):
        raise UpgradeError("Receiver recovery manifest identity is invalid.")
    recovery_value = _load_json_object(
        recovery_manifest_path, "Receiver recovery manifest"
    )
    if set(recovery_value) != {
        "config_sha256",
        "files",
        "schema_version",
        "source_commit",
    } or not isinstance(recovery_value["files"], dict):
        raise UpgradeError("Receiver recovery manifest is malformed.")
    if (
        recovery_value["schema_version"] != SCHEMA_VERSION
        or recovery_value["source_commit"] != commit
        or set(recovery_value["files"]) != set(RECOVERY_SOURCES)
    ):
        raise UpgradeError("Receiver recovery manifest identity is invalid.")
    for relative in RECOVERY_SOURCES:
        source = backup / "recovery-source" / relative
        _verify_private_file(source, owner_uid=private_owner)
        expected = recovery_value["files"].get(relative)
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or not source.is_file()
            or source.is_symlink()
            or _checked_sha256(source, "Receiver recovery source") != expected
        ):
            raise UpgradeError("Receiver recovery source identity is invalid.")
    recovery_config = backup / "recovery-config.json"
    if (
        not recovery_config.is_file()
        or recovery_config.is_symlink()
        or _checked_sha256(
            recovery_config, "Receiver recovery configuration"
        )
        != recovery_value["config_sha256"]
    ):
        raise UpgradeError("Receiver recovery configuration identity is invalid.")

    transaction = ReceiverTransaction(
        backup,
        commit,
        snapshot.manifest_path,
        recovery_manifest_path,
    )
    with _guard_termination_signals() as termination:
        termination.begin_rollback()
        try:
            snapshot.restore()
            _recover_application_before_receiver_restore(
                system_root, recovery_config
            )
            transaction.record("rolled-back")
        except BaseException as exc:
            try:
                transaction.record("rollback-failed")
            except BaseException:
                pass
            raise UpgradeError(
                "Manual receiver recovery failed; backup retained for investigation."
            ) from exc


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="transactional Platform v2 receiver upgrade"
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    upgrade = commands.add_parser("upgrade")
    upgrade.add_argument("commit")
    upgrade.add_argument("repo_root", type=Path)
    upgrade.add_argument("config", type=Path)
    upgrade.add_argument("legacy_receiver", type=Path)
    upgrade.add_argument("request", type=Path)
    recover = commands.add_parser("recover")
    recover.add_argument("commit")
    recover.add_argument("backup", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    lock: int | None = None
    try:
        if args.operation == "recover":
            owner = _private_directory_owner(Path("/"))
            _verify_root_owned_ancestor_chain(args.backup, include_path=True)
            _verify_private_directory(args.backup, owner_uid=owner)
            _verify_private_file(
                args.backup / "recovery-config.json", owner_uid=owner
            )
            config_path = args.backup / "recovery-config.json"
        else:
            _verify_production_path_bindings()
            _verify_live_production_inputs(args.config, args.legacy_receiver)
            # Lock from the canonical live receiver configuration, whose exact
            # bytes were just matched to the descriptor-pinned upgrade input.
            config_path = Path(CANONICAL_CONFIG_PATH)
        config = ReceiverConfig.load(config_path)
        if args.operation == "upgrade":
            _verify_live_production_inputs(args.config, args.legacy_receiver)
        lock = _open_production_lock(config)
        if args.operation == "recover":
            recover_backup(
                backup=args.backup,
                commit=args.commit,
                production_lock_fd=lock,
            )
            backup = args.backup
        else:
            backup, _ = execute_upgrade(
                commit=args.commit,
                repo_root=args.repo_root,
                config=args.config,
                legacy_receiver=args.legacy_receiver,
                request=args.request,
                production_lock_fd=lock,
            )
    except (UpgradeError, DeploymentError) as exc:
        print(str(exc), file=os.sys.stderr)
        return 1
    except BaseException as exc:
        # Never let paths, config values, subprocess output, or secrets from an
        # unexpected low-level failure cross the privileged SSH boundary.
        print(
            "Receiver upgrade failed internally: " + exc.__class__.__name__,
            file=os.sys.stderr,
        )
        return 1
    finally:
        if lock is not None:
            os.close(lock)
    if args.operation == "recover":
        print(f"Receiver recovery passed. Verified backup retained: {backup}")
    else:
        print(
            "Receiver upgrade and no-change preflight passed. "
            f"Verified backup: {backup}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
