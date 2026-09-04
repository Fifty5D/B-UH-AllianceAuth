from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.deploy.contracts import DeploymentError, ReceiverConfig
from ops.deploy.receiver import LockBusy, _forced_mode, _open_lock


ROOT = Path(__file__).resolve().parents[2]


class ReceiverBoundaryTests(unittest.TestCase):
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
            config = dataclasses.replace(source, state_dir=state)
            with mock.patch("ops.deploy.receiver.os.geteuid", return_value=0):
                first = _open_lock(config)
            try:
                self.assertEqual((state / "deploy.lock").stat().st_mode & 0o777, 0o600)
                with mock.patch(
                    "ops.deploy.receiver.os.geteuid", return_value=0
                ), self.assertRaises(LockBusy):
                    _open_lock(config)
            finally:
                os.close(first)

    def test_world_readable_state_directory_is_rejected(self):
        source = ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json")
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir(mode=0o755)
            config = dataclasses.replace(source, state_dir=state)
            with mock.patch(
                "ops.deploy.receiver.os.geteuid", return_value=0
            ), self.assertRaisesRegex(DeploymentError, "private"):
                _open_lock(config)


if __name__ == "__main__":
    unittest.main()
