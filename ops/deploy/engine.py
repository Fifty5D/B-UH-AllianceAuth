"""Serializable deployment state machine with explicit recovery semantics."""

from __future__ import annotations

import os
import re
import signal
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .contracts import (
    DeploymentError,
    ValidatedBundle,
    canonical_json_bytes,
    redact_sensitive_text,
)


DEPLOYMENT_STATES = (
    "validated",
    "prepared",
    "backed_up",
    "migrated",
    "candidate_healthy",
    "traffic_switched",
    "workers_replaced",
    "stabilized",
    "web_promoted",
    "verified",
)
MAX_FAILURE_DETAIL_BYTES = 32 * 1024
MAX_VERIFICATION_WARNINGS = 32
MAX_VERIFICATION_WARNING_CHARS = 200


class _TerminationSignalGuard:
    """Turn catchable process termination into a rollback-triggering exception.

    Once recovery begins, later termination signals are deliberately deferred so
    they cannot interrupt traffic restoration. SIGKILL remains uncatchable and
    therefore requires the journal-driven manual recovery path.
    """

    def __init__(self) -> None:
        self._previous: dict[signal.Signals, Any] = {}
        self._armed = True

    @staticmethod
    def _signals() -> tuple[signal.Signals, ...]:
        values: list[signal.Signals] = []
        for name in ("SIGHUP", "SIGINT", "SIGTERM"):
            value = getattr(signal, name, None)
            if value is not None and value not in values:
                values.append(value)
        return tuple(values)

    def __enter__(self) -> _TerminationSignalGuard:
        try:
            for value in self._signals():
                self._previous[value] = signal.getsignal(value)
                signal.signal(value, self._handle)
        except (OSError, RuntimeError, ValueError) as error:
            for value, previous in self._previous.items():
                signal.signal(value, previous)
            raise DeploymentError(
                "Could not install the deployment termination-signal guard"
            ) from error
        return self

    def _handle(self, number: int, _frame: Any) -> None:
        if not self._armed:
            return
        self._armed = False
        try:
            name = signal.Signals(number).name
        except ValueError:  # pragma: no cover - handlers are installed by value
            name = str(number)
        raise DeploymentError(
            f"Deployment interrupted by {name}; automatic rollback is required"
        )

    def protect_recovery(self) -> None:
        self._armed = False

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        for value, previous in self._previous.items():
            signal.signal(value, previous)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:  # pragma: no cover - Windows-only local contract tests
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _failure_fields(error: BaseException) -> tuple[str, str]:
    raw = str(error).strip().replace("\r\n", "\n").replace("\r", "\n")
    raw = redact_sensitive_text(raw)
    # Keep the root-only journal itself below the observer's strict 64 KiB
    # limit even when hostile command output contains JSON-expensive Unicode.
    raw = raw.encode("ascii", errors="replace").decode("ascii")
    detail = "\n".join(line[:500] for line in raw.splitlines()[-80:])
    detail_bytes = (detail or error.__class__.__name__).encode("utf-8")
    detail = detail_bytes[-MAX_FAILURE_DETAIL_BYTES:].decode("utf-8", errors="replace")
    summary = (raw.splitlines()[0] if raw else error.__class__.__name__)[:500]
    return summary, detail


def _verification_record(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate bounded, already-redacted stabilization evidence."""

    expected = {
        "filename",
        "sha256",
        "result",
        "iterations",
        "allowed_log_findings",
        "warnings",
        "failure",
    }
    result = dict(value)
    warnings = result.get("warnings")
    failure = result.get("failure")
    if (
        set(result) != expected
        or result.get("filename") != "HEALTH.json"
        or not isinstance(result.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", result["sha256"])
        or result.get("result") not in {"success", "failed"}
        or type(result.get("iterations")) is not int
        or result["iterations"] < 0
        or (result["result"] == "success" and result["iterations"] < 1)
        or type(result.get("allowed_log_findings")) is not int
        or not isinstance(warnings, list)
        or len(warnings) > MAX_VERIFICATION_WARNINGS
        or result["allowed_log_findings"] != len(warnings)
        or any(
            not isinstance(item, str)
            or re.fullmatch(
                rf"[\x20-\x7e]{{1,{MAX_VERIFICATION_WARNING_CHARS}}}", item
            )
            is None
            for item in warnings
        )
        or (
            failure is not None
            and (
                not isinstance(failure, str)
                or re.fullmatch(r"[\x20-\x7e]{1,500}", failure) is None
            )
        )
        or (result["result"] == "success" and failure is not None)
        or (result["result"] == "failed" and failure is None)
    ):
        raise DeploymentError("Deployment verification evidence is invalid")
    result["warnings"] = [redact_sensitive_text(item) for item in warnings]
    if isinstance(failure, str):
        result["failure"] = redact_sensitive_text(failure)
    return result


def _rollback_record(recovery: str, passed: bool) -> dict[str, str]:
    _summary, detail = _failure_fields(DeploymentError(recovery))
    return {
        "result": "passed" if passed else "failed",
        "detail": detail.replace("\n", " ")[:500],
    }


def _base_record(bundle: ValidatedBundle, operation: str) -> dict[str, Any]:
    now = _timestamp()
    return {
        "schema_version": 1,
        "operation": operation,
        "attempt_id": bundle.request.attempt_id,
        "repository": bundle.request.repository,
        "release_commit": bundle.request.release_commit,
        "source_commit": bundle.manifest["source_commit"],
        "platform_version": bundle.request.platform_version,
        "manifest_sha256": bundle.request.manifest_sha256,
        "started_at": now,
        "updated_at": now,
        "state": None,
        "history": [],
        "backup": None,
        "verification": None,
        "result": "running",
        "failure": None,
        "failure_detail": None,
        "recovery": None,
        "rollback": None,
        "cleanup": None,
    }


def _attempt_path(state_dir: Path, bundle: ValidatedBundle) -> Path:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt_dir = state_dir / "attempts"
    attempt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = attempt_dir / f"{bundle.request.attempt_id}.json"
    if path.exists():
        raise DeploymentError(
            "This exact GitHub workflow attempt was already received; rerun it "
            "to obtain a new attempt identity"
        )
    return path


class DeploymentBackend(Protocol):
    def validate(self, bundle: ValidatedBundle) -> None: ...

    def prepare_candidate(self, bundle: ValidatedBundle) -> None: ...

    def backup(self, bundle: ValidatedBundle) -> Mapping[str, Any]: ...

    def migrate(self, bundle: ValidatedBundle) -> None: ...

    def candidate_health(self, bundle: ValidatedBundle) -> None: ...

    def switch_traffic(self, bundle: ValidatedBundle) -> None: ...

    def replace_workers(self, bundle: ValidatedBundle) -> None: ...

    def stabilize(self, bundle: ValidatedBundle) -> Mapping[str, Any]: ...

    def stabilization_evidence(self) -> Mapping[str, Any] | None: ...

    def promote_web(self, bundle: ValidatedBundle) -> None: ...

    def finalize(self, bundle: ValidatedBundle) -> None: ...

    def cleanup_success(self, bundle: ValidatedBundle) -> str: ...

    def rollback(self, bundle: ValidatedBundle, last_state: str | None) -> str: ...


@dataclass
class DeploymentJournal:
    state_dir: Path
    bundle: ValidatedBundle

    def __post_init__(self) -> None:
        self.path = _attempt_path(self.state_dir, self.bundle)
        self.record = _base_record(self.bundle, "deploy")
        self._save()

    @property
    def state(self) -> str | None:
        value = self.record["state"]
        return value if isinstance(value, str) else None

    def _save(self) -> None:
        _atomic_write(self.path, canonical_json_bytes(self.record))

    def advance(
        self,
        state: str,
        *,
        backup: Mapping[str, Any] | None = None,
        verification: Mapping[str, Any] | None = None,
    ) -> None:
        if state not in DEPLOYMENT_STATES:
            raise DeploymentError(f"Unknown deployment state: {state}")
        if self.state == DEPLOYMENT_STATES[-1]:
            raise DeploymentError("A verified deployment has no further state")
        expected_index = 0 if self.state is None else DEPLOYMENT_STATES.index(self.state) + 1
        if DEPLOYMENT_STATES[expected_index] != state:
            raise DeploymentError(
                f"Invalid deployment transition from {self.state!r} to {state!r}"
            )
        verification_value: dict[str, Any] | None = None
        if verification is not None:
            if state != "stabilized":
                raise DeploymentError("Deployment verification evidence is invalid")
            verification_value = _verification_record(verification)
            if verification_value["result"] != "success":
                raise DeploymentError("Deployment verification evidence is invalid")
        elif state == "stabilized":
            raise DeploymentError(
                "A stabilized deployment requires bounded verification evidence"
            )
        now = _timestamp()
        self.record["state"] = state
        self.record["updated_at"] = now
        self.record["history"].append({"state": state, "at": now})
        if backup is not None:
            self.record["backup"] = dict(backup)
        if verification_value is not None:
            self.record["verification"] = verification_value
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

    def record_cleanup(self, detail: str, *, passed: bool) -> None:
        if self.record["result"] != "success" or self.state != "verified":
            raise DeploymentError("Post-success cleanup requires a verified deployment")
        self.record["cleanup"] = _rollback_record(detail, passed)
        self.record["updated_at"] = _timestamp()
        self._save()

    def fail(
        self,
        error: BaseException,
        recovery: str,
        *,
        rollback_passed: bool,
        verification: Mapping[str, Any] | None = None,
    ) -> None:
        summary, detail = _failure_fields(error)
        self.record["result"] = "failed"
        self.record["failure"] = summary
        self.record["failure_detail"] = detail
        rollback = _rollback_record(recovery, rollback_passed)
        self.record["recovery"] = rollback["detail"]
        self.record["rollback"] = rollback
        if verification is not None:
            self.record["verification"] = _verification_record(verification)
        self.record["updated_at"] = _timestamp()
        self._save()


@dataclass
class PreflightJournal:
    state_dir: Path
    bundle: ValidatedBundle

    def __post_init__(self) -> None:
        self.path = _attempt_path(self.state_dir, self.bundle)
        self.record = _base_record(self.bundle, "preflight")
        self._save()

    def _save(self) -> None:
        _atomic_write(self.path, canonical_json_bytes(self.record))

    def succeed(self, recovery: str) -> None:
        now = _timestamp()
        self.record["state"] = "candidate_validated"
        self.record["history"].append({"state": "candidate_validated", "at": now})
        self.record["result"] = "success"
        rollback = _rollback_record(recovery, True)
        self.record["recovery"] = rollback["detail"]
        self.record["rollback"] = rollback
        self.record["updated_at"] = now
        self._save()

    def fail(
        self, error: BaseException, recovery: str, *, rollback_passed: bool
    ) -> None:
        summary, detail = _failure_fields(error)
        self.record["result"] = "failed"
        self.record["failure"] = summary
        self.record["failure_detail"] = detail
        rollback = _rollback_record(recovery, rollback_passed)
        self.record["recovery"] = rollback["detail"]
        self.record["rollback"] = rollback
        self.record["updated_at"] = _timestamp()
        self._save()


class PreflightEngine:
    """Build and inspect a candidate, then restore production without restarting it."""

    def __init__(self, backend: DeploymentBackend, journal: PreflightJournal):
        self.backend = backend
        self.journal = journal

    def run(self, bundle: ValidatedBundle) -> None:
        with _TerminationSignalGuard() as signal_guard:
            try:
                self.backend.validate(bundle)
                self.backend.prepare_candidate(bundle)
            except BaseException as error:
                signal_guard.protect_recovery()
                try:
                    recovery = self.backend.rollback(bundle, "validated")
                    rollback_passed = True
                except BaseException as rollback_error:
                    rollback_summary = _failure_fields(rollback_error)[0]
                    recovery = (
                        "Candidate preflight cleanup also failed; production containers "
                        "were not intentionally restarted "
                        f"({rollback_error.__class__.__name__}: {rollback_summary})."
                    )
                    rollback_passed = False
                self.journal.fail(
                    error, recovery, rollback_passed=rollback_passed
                )
                if isinstance(error, DeploymentError):
                    raise
                raise DeploymentError(f"Candidate preflight failed: {error}") from error

            signal_guard.protect_recovery()
            try:
                recovery = self.backend.rollback(bundle, "validated")
            except BaseException as error:
                message = (
                    "Candidate checks passed but restoring the previous build target "
                    "failed; production containers were not intentionally restarted."
                )
                self.journal.fail(error, message, rollback_passed=False)
                if isinstance(error, DeploymentError):
                    raise
                raise DeploymentError(
                    f"Candidate preflight cleanup failed: {error}"
                ) from error
            self.journal.succeed(recovery)


class DeploymentEngine:
    """Run exactly one deployment while leaving recovery decisions explicit."""

    def __init__(self, backend: DeploymentBackend, journal: DeploymentJournal):
        self.backend = backend
        self.journal = journal

    def run(self, bundle: ValidatedBundle) -> None:
        with _TerminationSignalGuard() as signal_guard:
            try:
                self.backend.validate(bundle)
                self.journal.advance("validated")

                self.backend.prepare_candidate(bundle)
                self.journal.advance("prepared")
                backup = self.backend.backup(bundle)
                self.journal.advance("backed_up", backup=backup)

                self.backend.migrate(bundle)
                self.journal.advance("migrated")

                self.backend.candidate_health(bundle)
                self.journal.advance("candidate_healthy")

                self.backend.switch_traffic(bundle)
                self.journal.advance("traffic_switched")

                self.backend.replace_workers(bundle)
                self.journal.advance("workers_replaced")

                verification = self.backend.stabilize(bundle)
                self.journal.advance("stabilized", verification=verification)

                self.backend.promote_web(bundle)
                self.journal.advance("web_promoted")

                self.backend.finalize(bundle)
                self.journal.advance("verified")
                # Publishing the verified attempt and current pointer is one
                # uninterruptible commit boundary. A catchable signal received
                # here is deferred until those two fsynced writes agree.
                signal_guard.protect_recovery()
                self.journal.succeed()
                complete_plan = getattr(
                    self.backend, "complete_recovery_plan", None
                )
                if callable(complete_plan):
                    try:
                        complete_plan()
                    except BaseException:
                        # The success journal is the authoritative commit point.
                        # Startup consumes a leftover plan and recognizes this
                        # verified attempt instead of reversing it.
                        pass
            except BaseException as error:
                signal_guard.protect_recovery()
                verification = self.backend.stabilization_evidence()
                try:
                    recovery = self.backend.rollback(bundle, self.journal.state)
                    rollback_passed = True
                except BaseException as rollback_error:
                    rollback_summary = _failure_fields(rollback_error)[0]
                    recovery = (
                        "Automatic code rollback also failed; keep the database backup "
                        "and inspect the host locally "
                        f"({rollback_error.__class__.__name__}: {rollback_summary})."
                    )
                    rollback_passed = False
                self.journal.fail(
                    error,
                    recovery,
                    rollback_passed=rollback_passed,
                    verification=verification,
                )
                if isinstance(error, DeploymentError):
                    raise
                raise DeploymentError(f"Deployment failed: {error}") from error

            # The deployment is already durably verified. Slot cleanup must not
            # turn a housekeeping failure into a rollback or destroy the final
            # remaining old slot before success is published.
            signal_guard.protect_recovery()
            try:
                cleanup = self.backend.cleanup_success(bundle)
                self.journal.record_cleanup(cleanup, passed=True)
            except BaseException as cleanup_error:
                cleanup_summary = _failure_fields(cleanup_error)[0]
                try:
                    self.journal.record_cleanup(
                        "Post-success web-slot cleanup failed; retained rollback "
                        f"images and any remaining slots ({cleanup_summary}).",
                        passed=False,
                    )
                except BaseException:
                    # The success/current records were already fsynced. Never
                    # initiate a production rollback for diagnostic housekeeping.
                    pass
