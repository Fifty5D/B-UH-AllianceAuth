"""Serializable deployment state machine with explicit recovery semantics."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .contracts import DeploymentError, ValidatedBundle, canonical_json_bytes


DEPLOYMENT_STATES = (
    "validated",
    "backed_up",
    "migrated",
    "swapped",
    "healthy",
    "verified",
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class DeploymentBackend(Protocol):
    def validate(self, bundle: ValidatedBundle) -> None: ...

    def prepare_candidate(self, bundle: ValidatedBundle) -> None: ...

    def backup(self, bundle: ValidatedBundle) -> Mapping[str, Any]: ...

    def migrate(self, bundle: ValidatedBundle) -> None: ...

    def swap(self, bundle: ValidatedBundle) -> None: ...

    def health(self, bundle: ValidatedBundle) -> None: ...

    def finalize(self, bundle: ValidatedBundle) -> None: ...

    def rollback(self, bundle: ValidatedBundle, last_state: str | None) -> str: ...


@dataclass
class DeploymentJournal:
    state_dir: Path
    bundle: ValidatedBundle

    def __post_init__(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.attempt_dir = self.state_dir / "attempts"
        self.attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.attempt_dir / f"{self.bundle.request.attempt_id}.json"
        if self.path.exists():
            raise DeploymentError(
                "This exact GitHub workflow attempt was already received; rerun it "
                "to obtain a new attempt identity"
            )
        now = _timestamp()
        self.record: dict[str, Any] = {
            "schema_version": 1,
            "attempt_id": self.bundle.request.attempt_id,
            "repository": self.bundle.request.repository,
            "release_commit": self.bundle.request.release_commit,
            "source_commit": self.bundle.manifest["source_commit"],
            "platform_version": self.bundle.request.platform_version,
            "manifest_sha256": self.bundle.request.manifest_sha256,
            "started_at": now,
            "updated_at": now,
            "state": None,
            "history": [],
            "backup": None,
            "result": "running",
            "failure": None,
            "recovery": None,
        }
        self._save()

    @property
    def state(self) -> str | None:
        value = self.record["state"]
        return value if isinstance(value, str) else None

    def _save(self) -> None:
        _atomic_write(self.path, canonical_json_bytes(self.record))

    def advance(self, state: str, *, backup: Mapping[str, Any] | None = None) -> None:
        if state not in DEPLOYMENT_STATES:
            raise DeploymentError(f"Unknown deployment state: {state}")
        if self.state == DEPLOYMENT_STATES[-1]:
            raise DeploymentError("A verified deployment has no further state")
        expected_index = 0 if self.state is None else DEPLOYMENT_STATES.index(self.state) + 1
        if DEPLOYMENT_STATES[expected_index] != state:
            raise DeploymentError(
                f"Invalid deployment transition from {self.state!r} to {state!r}"
            )
        now = _timestamp()
        self.record["state"] = state
        self.record["updated_at"] = now
        self.record["history"].append({"state": state, "at": now})
        if backup is not None:
            self.record["backup"] = dict(backup)
        self._save()

    def succeed(self) -> None:
        if self.state != "verified":
            raise DeploymentError("A deployment cannot succeed before verification")
        self.record["result"] = "success"
        self.record["updated_at"] = _timestamp()
        self._save()
        current = {
            "schema_version": 1,
            "platform_version": self.bundle.request.platform_version,
            "release_commit": self.bundle.request.release_commit,
            "source_commit": self.bundle.manifest["source_commit"],
            "manifest_sha256": self.bundle.request.manifest_sha256,
            "verified_at": self.record["updated_at"],
        }
        _atomic_write(self.state_dir / "current.json", canonical_json_bytes(current))

    def fail(self, error: BaseException, recovery: str) -> None:
        message = str(error).strip().replace("\r", " ").replace("\n", " ")
        self.record["result"] = "failed"
        self.record["failure"] = (message or error.__class__.__name__)[:500]
        self.record["recovery"] = recovery[:500]
        self.record["updated_at"] = _timestamp()
        self._save()


class DeploymentEngine:
    """Run exactly one deployment while leaving recovery decisions explicit."""

    def __init__(self, backend: DeploymentBackend, journal: DeploymentJournal):
        self.backend = backend
        self.journal = journal

    def run(self, bundle: ValidatedBundle) -> None:
        try:
            self.backend.validate(bundle)
            self.journal.advance("validated")

            self.backend.prepare_candidate(bundle)
            backup = self.backend.backup(bundle)
            self.journal.advance("backed_up", backup=backup)

            self.backend.migrate(bundle)
            self.journal.advance("migrated")

            self.backend.swap(bundle)
            self.journal.advance("swapped")

            self.backend.health(bundle)
            self.journal.advance("healthy")

            self.backend.finalize(bundle)
            self.journal.advance("verified")
            self.journal.succeed()
        except BaseException as error:
            try:
                recovery = self.backend.rollback(bundle, self.journal.state)
            except BaseException as rollback_error:
                recovery = (
                    "Automatic code rollback also failed; keep the database backup "
                    f"and inspect the host locally ({rollback_error.__class__.__name__})."
                )
            self.journal.fail(error, recovery)
            if isinstance(error, DeploymentError):
                raise
            raise DeploymentError(f"Deployment failed: {error}") from error
