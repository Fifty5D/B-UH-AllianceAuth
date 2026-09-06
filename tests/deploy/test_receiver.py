from __future__ import annotations

import dataclasses
import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ops.deploy.contracts import DeploymentError, ReceiverConfig
from ops.deploy.receiver import LockBusy, _forced_mode, _open_lock, receive


ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def simulated_root_metadata():
    """Present temporary runner-owned files as root-owned to the root-only unit."""

    real_lstat = Path.lstat
    real_fstat = os.fstat

    def lstat_as_root(path):
        details = real_lstat(path)
        return SimpleNamespace(
            st_mode=details.st_mode,
            st_uid=0,
            st_dev=details.st_dev,
            st_ino=details.st_ino,
        )

    def fstat_as_root(descriptor):
        details = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=details.st_mode,
            st_uid=0,
            st_dev=details.st_dev,
            st_ino=details.st_ino,
        )

    with mock.patch(
        "ops.deploy.receiver.os.geteuid", return_value=0
    ), mock.patch.object(Path, "lstat", lstat_as_root), mock.patch(
        "ops.deploy.receiver.os.fstat", side_effect=fstat_as_root
    ), mock.patch(
        "ops.deploy.receiver._verify_root_owned_ancestors"
    ):
        yield


class ReceiverBoundaryTests(unittest.TestCase):
    def test_incomplete_recovery_is_consumed_before_new_archive_bytes(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        stream = io.BytesIO(b"must-not-be-read")
        read_descriptor, write_descriptor = os.pipe()
        os.close(write_descriptor)
        with mock.patch(
            "ops.deploy.receiver.ReceiverConfig.load", return_value=source
        ), mock.patch(
            "ops.deploy.receiver._open_lock", return_value=read_descriptor
        ), mock.patch(
            "ops.deploy.receiver.DockerHost.recover_incomplete_plan",
            return_value="Recovered an incomplete prior deployment.",
        ) as recover, self.assertRaisesRegex(
            DeploymentError, "new archive was not read"
        ):
            receive(Path("receiver.json"), stream, "deploy")
        recover.assert_called_once_with(source)
        self.assertEqual(stream.tell(), 0)

    def test_original_command_accepts_only_two_exact_operations(self):
        for command, expected in (
            ("preflight platform-v2", "preflight"),
            ("deploy platform-v2", "deploy"),
        ):
            with self.subTest(command=command), mock.patch.dict(
                os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True
            ):
                self.assertEqual(_forced_mode(None), expected)
        for command in ("", "deploy platform-v2 ", "deploy moon-tax", "sh"):
            with self.subTest(command=command), mock.patch.dict(
                os.environ, {"SSH_ORIGINAL_COMMAND": command}, clear=True
            ):
                with self.assertRaisesRegex(DeploymentError, "rejected"):
                    _forced_mode(None)

    def test_dispatcher_supplied_mode_must_match_original_when_preserved(self):
        with mock.patch.dict(
            os.environ, {"SSH_ORIGINAL_COMMAND": "preflight platform-v2"}, clear=True
        ):
            self.assertEqual(_forced_mode("preflight"), "preflight")
            with self.assertRaisesRegex(DeploymentError, "does not match"):
                _forced_mode("deploy")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_forced_mode("deploy"), "deploy")

    def test_host_lock_is_nonblocking_and_private(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            backup = Path(temporary) / "backup"
            state.mkdir(mode=0o700)
            backup.mkdir(mode=0o700)
            config = dataclasses.replace(source, state_dir=state, backup_dir=backup)
            with simulated_root_metadata():
                first = _open_lock(config)
                try:
                    self.assertEqual(
                        (state / "deploy.lock").stat().st_mode & 0o777, 0o600
                    )
                    with self.assertRaises(LockBusy):
                        _open_lock(config)
                finally:
                    os.close(first)

    def test_world_readable_state_directory_is_rejected(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o755)
            state.chmod(0o755)
            backup = Path(temporary) / "backup"
            backup.mkdir(mode=0o700)
            config = dataclasses.replace(source, state_dir=state, backup_dir=backup)
            with simulated_root_metadata(), self.assertRaisesRegex(
                DeploymentError, "private"
            ):
                _open_lock(config)

    def test_exact_inherited_lock_descriptor_reuses_production_lock(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            backup = Path(temporary) / "backup"
            state.mkdir(mode=0o700)
            backup.mkdir(mode=0o700)
            config = dataclasses.replace(source, state_dir=state, backup_dir=backup)
            with simulated_root_metadata():
                parent = _open_lock(config)
                try:
                    inherited = _open_lock(config, parent)
                    os.close(inherited)
                    unrelated_path = Path(temporary) / "unrelated.lock"
                    unrelated_path.touch(mode=0o600)
                    unrelated = os.open(unrelated_path, os.O_RDWR)
                    try:
                        with self.assertRaisesRegex(DeploymentError, "identity"):
                            _open_lock(config, unrelated)
                    finally:
                        os.close(unrelated)
                finally:
                    os.close(parent)

    def test_missing_state_directory_is_rejected_without_creation(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "missing-state"
            backup = Path(temporary) / "backup"
            backup.mkdir(mode=0o700)
            config = dataclasses.replace(source, state_dir=state, backup_dir=backup)
            with simulated_root_metadata(), self.assertRaisesRegex(
                DeploymentError, "unavailable"
            ):
                _open_lock(config)
            self.assertFalse(state.exists())

    def test_writable_lock_ancestor_is_rejected_before_lock_creation(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        config = dataclasses.replace(
            source,
            state_dir=Path("/unsafe-parent/state"),
            backup_dir=Path("/safe-parent/backup"),
        )

        def details(path):
            mode = 0o40777 if path == Path("/unsafe-parent") else 0o40755
            return SimpleNamespace(st_mode=mode, st_uid=0)

        with mock.patch.object(Path, "lstat", side_effect=details), mock.patch(
            "ops.deploy.receiver.os.geteuid", return_value=0
        ), self.assertRaisesRegex(DeploymentError, "ancestor is unsafe"):
            _open_lock(config)


if __name__ == "__main__":
    unittest.main()
