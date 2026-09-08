import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ops.deploy import receiver_upgrade
from ops.release import recovery_policy


ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
if BASH is None and os.name == "nt":
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if git_bash.is_file():
        BASH = str(git_bash)
POWERSHELL = shutil.which("pwsh")
PYTHON3 = shutil.which("python3")


def actual(root: Path, logical: str) -> Path:
    return root / logical.removeprefix("/")


def powershell_required_sources() -> tuple[str, ...]:
    helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text(
        encoding="utf-8"
    )
    block = helper.split("$RequiredSources = @(\n", 1)[1].split("\n)", 1)[0]
    sources = tuple(
        line.strip().removeprefix('"').removesuffix('",').removesuffix('"')
        for line in block.splitlines()
        if line.strip().startswith('"')
    )
    if not sources or len(sources) != len(set(sources)):
        raise AssertionError("PowerShell receiver source allowlist is malformed")
    return sources


@contextmanager
def simulated_receiver_upgrade_root_metadata():
    """Expose runner-owned lock fixtures as root-owned without changing bytes."""

    real_lstat = Path.lstat
    real_fstat = os.fstat

    def root_details(details):
        return SimpleNamespace(
            st_mode=details.st_mode,
            st_uid=0,
            st_gid=0,
            st_dev=details.st_dev,
            st_ino=details.st_ino,
            st_size=details.st_size,
        )

    def lstat_as_root(path):
        return root_details(real_lstat(path))

    def fstat_as_root(descriptor):
        return root_details(real_fstat(descriptor))

    with mock.patch(
        "ops.deploy.receiver_upgrade.os.geteuid", return_value=0
    ), mock.patch.object(Path, "lstat", lstat_as_root), mock.patch(
        "ops.deploy.receiver_upgrade.os.fstat", side_effect=fstat_as_root
    ), mock.patch(
        "ops.deploy.receiver_upgrade._verify_root_owned_ancestor_chain"
    ):
        yield


def populate(root: Path, marker: str, *, all_targets: bool) -> None:
    for index, logical in enumerate(receiver_upgrade.TARGETS):
        if not all_targets and index % 2:
            continue
        path = actual(root, logical)
        if logical in {"/usr/local/lib/buh-platform-v2", "/etc/buh-platform-v2"}:
            path.mkdir(parents=True)
            child = path / "state.txt"
            child.write_text(f"{marker}:{index}\n", encoding="utf-8")
            child.chmod(0o640)
            path.chmod(0o750)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{marker}:{index}\n", encoding="utf-8")
            path.chmod(0o750 if "sbin" in logical or "bin" in logical else 0o640)


def independent_fingerprint(root: Path):
    result = {}
    for logical in receiver_upgrade.TARGETS:
        path = actual(root, logical)
        if not path.exists() and not path.is_symlink():
            result[logical] = None
            continue
        entries = []
        paths = [path]
        if path.is_dir():
            paths.extend(sorted(path.rglob("*")))
        for item in paths:
            details = item.lstat()
            relative = "." if item == path else item.relative_to(path).as_posix()
            content = item.read_bytes() if stat.S_ISREG(details.st_mode) else None
            entries.append(
                (
                    relative,
                    stat.S_IFMT(details.st_mode),
                    stat.S_IMODE(details.st_mode),
                    details.st_uid,
                    details.st_gid,
                    content,
                )
            )
        result[logical] = entries
    return result


class ConfirmedReceiverBaselineTests(unittest.TestCase):
    def _exercise(self, *, changed_path: str | None = None):
        policy = recovery_policy.load_policy()
        expected = policy["host_baseline"]["installed_receiver"]
        identities = {
            expected["config"]["path"]: expected["config"],
            expected["install_record"]["path"]: expected["install_record"],
            **expected["files"],
        }
        record_files = {
            "ops/deploy/contracts.py": expected["files"][
                "/usr/local/lib/buh-platform-v2/ops/deploy/contracts.py"
            ]["sha256"],
            "ops/deploy/docker_host.py": expected["files"][
                "/usr/local/lib/buh-platform-v2/ops/deploy/docker_host.py"
            ]["sha256"],
            "ops/deploy/buh-deploy-dispatch": expected["files"][
                "/usr/local/sbin/buh-deploy-dispatch"
            ]["sha256"],
            "ops/deploy/buh-platform-v2-receiver": expected["files"][
                "/usr/local/sbin/buh-platform-v2-receiver"
            ]["sha256"],
        }

        def details_for(path):
            logical = "/" + Path(path).as_posix().lstrip("/")
            identity = identities[logical]
            return SimpleNamespace(
                st_mode=stat.S_IFREG | int(identity["mode"], 8),
                st_uid=identity["uid"],
                st_gid=identity["gid"],
            )

        def digest_for(path, _context):
            logical = "/" + Path(path).as_posix().lstrip("/")
            if logical == changed_path:
                return "0" * 64
            return identities[logical]["sha256"]

        record = {
            "config_sha256": expected["config"]["sha256"],
            "files": record_files,
            "schema_version": 1,
            "source_commit": expected["install_record"]["source_commit"],
        }
        with mock.patch.object(Path, "lstat", details_for), mock.patch.object(
            receiver_upgrade, "_checked_sha256", side_effect=digest_for
        ), mock.patch.object(
            receiver_upgrade.ReceiverConfig,
            "load",
            return_value=SimpleNamespace(schema_version=1),
        ), mock.patch.object(
            receiver_upgrade, "_load_json_object", return_value=record
        ):
            receiver_upgrade._verify_confirmed_installed_receiver()

    def test_accepts_the_exact_confirmed_installed_receiver(self):
        self._exercise()

    def test_rejects_any_changed_confirmed_receiver_file(self):
        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError,
            "Confirmed installed receiver baseline changed",
        ):
            self._exercise(changed_path="/usr/local/sbin/buh-platform-v2-receiver")


class ReceiverUpgradeBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.system = self.base / "system"
        self.system.mkdir()
        self.backups = self.base / "backups"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        for relative in receiver_upgrade.RECOVERY_SOURCES:
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        self.config = self.base / "receiver.json"
        self.legacy = self.base / "legacy"
        self.request = self.base / "request.json"
        self.config.write_text("{}\n", encoding="utf-8")
        self.legacy.write_text("#!/bin/sh\n", encoding="utf-8")
        self.request.write_text("{}\n", encoding="utf-8")
        populate(self.system, "old", all_targets=False)
        self.sentinel_paths = (
            actual(self.system, "/root/.ssh/authorized_keys"),
            actual(
                self.system,
                "/usr/local/sbin/buh-moon-tax-platform-remote",
            ),
            actual(self.system, "/opt/allianceauth/.env"),
        )
        for index, path in enumerate(self.sentinel_paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"sentinel-{index}\n".encode("ascii"))
            path.chmod(0o600 if index != 1 else 0o755)
        self.sentinel_state = {
            path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in self.sentinel_paths
        }
        self.independent_before = independent_fingerprint(self.system)

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, install, preflight, **kwargs):
        return receiver_upgrade.execute_upgrade(
            commit="a" * 40,
            repo_root=self.repo,
            config=self.config,
            legacy_receiver=self.legacy,
            request=self.request,
            system_root=self.system,
            backup_root=self.backups,
            install_callback=install,
            preflight_callback=preflight,
            **kwargs,
        )

    def install_new(self, staging: Path):
        populate(staging, "new", all_targets=True)

    def assert_exact_old_state(self):
        self.assertEqual(independent_fingerprint(self.system), self.independent_before)
        self.assert_sentinels_unchanged()

    def assert_sentinels_unchanged(self):
        self.assertEqual(
            {
                path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in self.sentinel_paths
            },
            self.sentinel_state,
        )

    def only_backup(self) -> Path:
        backups = list(self.backups.iterdir())
        self.assertEqual(len(backups), 1)
        return backups[0]

    def transaction(self) -> dict[str, object]:
        return json.loads(
            (self.only_backup() / "transaction.json").read_text(encoding="utf-8")
        )

    def test_backup_failure_prevents_installation(self):
        installed = False

        def install(_staging):
            nonlocal installed
            installed = True

        with mock.patch(
            "ops.deploy.receiver_upgrade._copy_tree",
            side_effect=OSError("injected backup write failure"),
        ):
            with self.assertRaisesRegex(receiver_upgrade.UpgradeError, "could not be created"):
                self.execute(install, lambda: None)
        self.assertFalse(installed)
        self.assert_exact_old_state()

    def test_non_directory_backup_root_is_rejected_before_installation(self):
        self.backups.write_text("not a private directory\n", encoding="utf-8")
        installed = False

        def install(_staging):
            nonlocal installed
            installed = True

        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "backup could not be created"
        ):
            self.execute(install, lambda: None)
        self.assertFalse(installed)
        self.assert_exact_old_state()

    def test_wrong_owner_backup_root_is_rejected_before_installation(self):
        self.backups.mkdir(mode=0o700)
        impossible_owner = self.backups.stat().st_uid + 1
        with mock.patch(
            "ops.deploy.receiver_upgrade._private_directory_owner",
            return_value=impossible_owner,
        ):
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "backup could not be created"
            ):
                self.execute(self.install_new, lambda: None)
        self.assert_exact_old_state()

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits required")
    def test_shared_backup_root_is_rejected_before_installation(self):
        self.backups.mkdir(mode=0o755)
        self.backups.chmod(0o755)
        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "backup could not be created"
        ):
            self.execute(self.install_new, lambda: None)
        self.assert_exact_old_state()

    def test_non_directory_staging_root_is_rejected_before_installation(self):
        unsafe_staging = self.base / "unsafe-staging"
        unsafe_staging.write_text("not a directory\n", encoding="utf-8")
        installed = False

        def install(_staging):
            nonlocal installed
            installed = True

        with mock.patch(
            "ops.deploy.receiver_upgrade.tempfile.mkdtemp",
            return_value=str(unsafe_staging),
        ):
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "staging could not be created"
            ):
                self.execute(install, lambda: None)
        self.assertFalse(installed)
        self.assertEqual(self.transaction()["state"], "backup-verified")
        self.assert_exact_old_state()

    def test_partial_staged_installation_restores_exact_state(self):
        def fail_install(staging):
            first = actual(staging, receiver_upgrade.TARGETS[0])
            first.mkdir(parents=True)
            (first / "partial").write_text("partial", encoding="utf-8")
            raise receiver_upgrade.UpgradeError("staged install failed")

        with self.assertRaisesRegex(receiver_upgrade.UpgradeError, "No installed path changed"):
            self.execute(fail_install, lambda: None)
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "not-activated")

    def test_partial_activation_restores_present_and_absent_paths(self):
        def fail_after_two(index, _logical):
            if index == 1:
                raise OSError("injected activation failure")

        with self.assertRaisesRegex(receiver_upgrade.UpgradeError, "verified backup restored"):
            self.execute(self.install_new, lambda: None, after_promote=fail_after_two)
        self.assert_exact_old_state()

    def test_preflight_failure_restores_exact_state(self):
        def fail_preflight():
            raise receiver_upgrade.UpgradeError("preflight failed")

        with self.assertRaisesRegex(receiver_upgrade.UpgradeError, "Verified backup restored"):
            self.execute(self.install_new, fail_preflight)
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rolled-back")

    def test_interrupt_during_activation_restores_exact_state(self):
        def interrupt(index, _logical):
            if index == 1:
                raise KeyboardInterrupt()

        with self.assertRaisesRegex(receiver_upgrade.UpgradeError, "verified backup restored"):
            self.execute(self.install_new, lambda: None, after_promote=interrupt)
        self.assert_exact_old_state()

    def assert_termination_signal_rolls_back(self, signum: int) -> None:
        previous = signal.getsignal(signum)
        state_during_activation = None

        def interrupt(index, _logical):
            nonlocal state_during_activation
            if index == 1:
                state_during_activation = self.transaction()["state"]
                handler = signal.getsignal(signum)
                self.assertTrue(callable(handler))
                if os.name == "posix":
                    os.kill(os.getpid(), signum)
                else:
                    handler(signum, None)

        name = signal.Signals(signum).name
        with self.assertRaisesRegex(receiver_upgrade.UpgradeError, name):
            self.execute(self.install_new, lambda: None, after_promote=interrupt)
        self.assertEqual(signal.getsignal(signum), previous)
        self.assertEqual(state_during_activation, "activation-in-progress")
        self.assertEqual(self.transaction()["state"], "rolled-back")
        self.assertFalse(self.transaction()["recovery_required"])
        self.assert_exact_old_state()

    def test_sigterm_during_activation_restores_exact_state(self):
        self.assert_termination_signal_rolls_back(signal.SIGTERM)

    def test_sigint_and_repeat_during_rollback_restore_exact_state(self):
        previous = signal.getsignal(signal.SIGINT)
        original_restore = receiver_upgrade.ReceiverSnapshot.restore
        repeated_signal_was_deferred = False

        def interrupt(index, _logical):
            if index == 1:
                handler = signal.getsignal(signal.SIGINT)
                self.assertTrue(callable(handler))
                handler(signal.SIGINT, None)

        def restore_with_repeat(snapshot):
            nonlocal repeated_signal_was_deferred
            handler = signal.getsignal(signal.SIGINT)
            self.assertTrue(callable(handler))
            handler(signal.SIGINT, None)
            repeated_signal_was_deferred = True
            return original_restore(snapshot)

        with mock.patch.object(
            receiver_upgrade.ReceiverSnapshot,
            "restore",
            autospec=True,
            side_effect=restore_with_repeat,
        ):
            with self.assertRaisesRegex(receiver_upgrade.UpgradeError, "SIGINT"):
                self.execute(self.install_new, lambda: None, after_promote=interrupt)

        self.assertTrue(repeated_signal_was_deferred)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assertEqual(self.transaction()["state"], "rolled-back")
        self.assertFalse(self.transaction()["recovery_required"])
        self.assert_exact_old_state()

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP is unavailable")
    def test_sighup_during_activation_restores_exact_state(self):
        self.assert_termination_signal_rolls_back(signal.SIGHUP)

    def test_success_keeps_verified_backup_and_new_receiver(self):
        backup, restored = self.execute(self.install_new, lambda: None)
        self.assertFalse(restored)
        self.assert_sentinels_unchanged()
        self.assertTrue((backup / "VERIFIED.sha256").is_file())
        receiver_upgrade.ReceiverSnapshot(backup, self.system).verify()
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertTrue(manifest["targets"][0]["present"])
        self.assertFalse(manifest["targets"][1]["present"])
        file_entry = next(
            entry
            for entry in manifest["targets"][0]["entries"]
            if entry["kind"] == "file"
        )
        self.assertRegex(file_entry["sha256"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(file_entry["uid"], int)
        self.assertIsInstance(file_entry["gid"], int)
        self.assertIsInstance(file_entry["mode"], int)
        transaction = json.loads(
            (backup / "transaction.json").read_text(encoding="utf-8")
        )
        self.assertEqual(transaction["schema_version"], 1)
        self.assertEqual(transaction["source_commit"], "a" * 40)
        self.assertEqual(transaction["state"], "completed")
        self.assertFalse(transaction["recovery_required"])
        self.assertEqual(
            transaction["backup_manifest_sha256"],
            receiver_upgrade._sha256(backup / "manifest.json"),
        )
        self.assertEqual(
            transaction["recovery_manifest_sha256"],
            receiver_upgrade._sha256(backup / "recovery-manifest.json"),
        )
        recovery_manifest = json.loads(
            (backup / "recovery-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recovery_manifest["source_commit"], "a" * 40)
        self.assertEqual(
            set(recovery_manifest["files"]), set(receiver_upgrade.RECOVERY_SOURCES)
        )
        self.assertEqual(
            (backup / "recovery-config.json").read_bytes(), self.config.read_bytes()
        )
        for index, logical in enumerate(receiver_upgrade.TARGETS):
            path = actual(self.system, logical)
            self.assertTrue(path.exists(), logical)
            if path.is_dir():
                value = (path / "state.txt").read_text(encoding="utf-8")
            else:
                value = path.read_text(encoding="utf-8")
            self.assertEqual(value, f"new:{index}\n")

    def mark_recovery_required(self, backup: Path) -> None:
        receiver_upgrade.ReceiverTransaction(
            backup,
            "a" * 40,
            backup / "manifest.json",
            backup / "recovery-manifest.json",
        ).record("activation-in-progress")

    def test_manual_recovery_restores_the_verified_prior_state(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        self.mark_recovery_required(backup)
        receiver_upgrade.recover_backup(
            backup=backup,
            commit="a" * 40,
            system_root=self.system,
        )
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rolled-back")
        self.assertFalse(self.transaction()["recovery_required"])

    def test_manual_recovery_resolves_application_before_terminal_marker(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        self.mark_recovery_required(backup)
        with mock.patch(
            "ops.deploy.receiver_upgrade._recover_application_before_receiver_restore",
            side_effect=receiver_upgrade.UpgradeError(
                "injected unresolved application rollback"
            ),
        ) as application_recovery, self.assertRaisesRegex(
            receiver_upgrade.UpgradeError,
            "Manual receiver recovery failed",
        ):
            receiver_upgrade.recover_backup(
                backup=backup,
                commit="a" * 40,
                system_root=self.system,
            )
        application_recovery.assert_called_once_with(
            self.system, backup / "recovery-config.json"
        )
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rollback-failed")
        self.assertTrue(self.transaction()["recovery_required"])

    def test_retained_recovery_source_runs_without_live_receiver_modules(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        self.mark_recovery_required(backup)
        retained_source = backup / "recovery-source"
        self.assertFalse((retained_source / "ops/deploy/receiver.py").exists())
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(retained_source)
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from ops.deploy.receiver_upgrade import recover_backup; "
                    "recover_backup(backup=Path(sys.argv[1]), commit=sys.argv[2], "
                    "system_root=Path(sys.argv[3]))"
                ),
                str(backup),
                "a" * 40,
                str(self.system),
            ],
            cwd=self.base,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rolled-back")
        self.assertFalse(self.transaction()["recovery_required"])

    def test_manual_recovery_rejects_a_completed_transaction(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "does not require recovery"
        ):
            receiver_upgrade.recover_backup(
                backup=backup,
                commit="a" * 40,
                system_root=self.system,
            )

    def test_manual_recovery_rejects_tampered_recovery_source(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        self.mark_recovery_required(backup)
        installed_state = independent_fingerprint(self.system)
        source = backup / "recovery-source/ops/deploy/receiver_upgrade.py"
        source.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "recovery source identity"
        ):
            receiver_upgrade.recover_backup(
                backup=backup,
                commit="a" * 40,
                system_root=self.system,
            )
        self.assertEqual(independent_fingerprint(self.system), installed_state)
        self.assert_sentinels_unchanged()
        self.assertTrue(self.transaction()["recovery_required"])

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and modes required")
    def test_manual_recovery_rejects_non_private_recovery_source_mode(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        self.mark_recovery_required(backup)
        installed_state = independent_fingerprint(self.system)
        source = backup / "recovery-source/ops/deploy/receiver_upgrade.py"
        source.chmod(0o644)
        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "ownership or mode is unsafe"
        ):
            receiver_upgrade.recover_backup(
                backup=backup,
                commit="a" * 40,
                system_root=self.system,
            )
        self.assertEqual(independent_fingerprint(self.system), installed_state)
        self.assert_sentinels_unchanged()
        self.assertTrue(self.transaction()["recovery_required"])

    def test_recovery_main_rejects_backup_before_loading_its_configuration(self):
        with mock.patch(
            "ops.deploy.receiver_upgrade._verify_private_directory",
            side_effect=receiver_upgrade.UpgradeError("unsafe backup"),
        ), mock.patch.object(
            receiver_upgrade.ReceiverConfig, "load"
        ) as load, mock.patch("builtins.print"):
            result = receiver_upgrade.main(
                ["recover", "a" * 40, str(self.base / "unsafe-backup")]
            )
        self.assertEqual(result, 1)
        load.assert_not_called()

    def test_installer_and_preflight_output_are_suppressed(self):
        secret = "DO-NOT-PRINT-THIS-PREFLIGHT-SECRET"
        class FailedPreflight:
            pid = 12345

            def wait(self, timeout=None):
                del timeout
                return 1

        with mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.Popen",
            return_value=FailedPreflight(),
        ) as popen:
            with self.assertRaises(receiver_upgrade.UpgradeError) as raised:
                receiver_upgrade.execute_upgrade(
                    commit="b" * 40,
                    repo_root=self.repo,
                    config=self.config,
                    legacy_receiver=self.legacy,
                    request=self.request,
                    system_root=self.system,
                    backup_root=self.backups,
                    install_callback=self.install_new,
                    production_lock_fd=123,
                )
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("--inherited-lock-fd", popen.call_args.args[0])
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (123,))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(
            popen.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1"
        )
        self.assertIs(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assert_exact_old_state()

    def test_backup_fsync_failure_prevents_installer_and_live_mutation(self):
        installed = False

        def install(_staging):
            nonlocal installed
            installed = True

        with mock.patch(
            "ops.deploy.receiver_upgrade._fsync_tree",
            side_effect=OSError("injected durable flush failure"),
        ), self.assertRaisesRegex(receiver_upgrade.UpgradeError, "could not be created"):
            self.execute(install, lambda: None)
        self.assertFalse(installed)
        self.assert_exact_old_state()

    def test_backup_root_fsync_failure_prevents_installer_and_live_mutation(self):
        installed = False

        def install(_staging):
            nonlocal installed
            installed = True

        with mock.patch(
            "ops.deploy.receiver_upgrade._fsync_directory",
            side_effect=OSError("injected backup-root flush failure"),
        ), self.assertRaisesRegex(receiver_upgrade.UpgradeError, "could not be created"):
            self.execute(install, lambda: None)
        self.assertFalse(installed)
        self.assert_exact_old_state()

    def test_staging_corruption_after_install_is_rejected_before_activation(self):
        original_verify = receiver_upgrade.ReceiverSnapshot.verify_live
        staging_holder: dict[str, Path] = {}

        def install(staging):
            staging_holder["path"] = staging
            self.install_new(staging)

        def mutate_during_live_verify(snapshot):
            original_verify(snapshot)
            target = actual(
                staging_holder["path"], receiver_upgrade.TARGETS[0]
            ) / "state.txt"
            target.write_text("corrupted after install\n", encoding="utf-8")

        with mock.patch.object(
            receiver_upgrade.ReceiverSnapshot,
            "verify_live",
            autospec=True,
            side_effect=mutate_during_live_verify,
        ), self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "changed before activation"
        ):
            self.execute(install, lambda: None)
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "not-activated")

    def test_silent_activation_corruption_is_detected_and_exactly_rolled_back(self):
        def corrupt_after_last_promote(index, _logical):
            if index == len(receiver_upgrade.TARGETS) - 1:
                target = actual(
                    self.system, receiver_upgrade.TARGETS[0]
                ) / "state.txt"
                target.write_text("corrupted after promotion\n", encoding="utf-8")

        with self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "Verified backup restored"
        ):
            self.execute(
                self.install_new,
                lambda: None,
                after_promote=corrupt_after_last_promote,
            )
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rolled-back")

    def test_live_fsync_failure_blocks_completion_and_durably_rolls_back(self):
        durable = receiver_upgrade._fsync_live_targets
        calls = 0

        def fail_first(root):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected live fsync failure")
            durable(root)

        with mock.patch(
            "ops.deploy.receiver_upgrade._fsync_live_targets",
            side_effect=fail_first,
        ), self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "(?i)verified backup restored"
        ):
            self.execute(self.install_new, lambda: None)
        self.assertGreaterEqual(calls, 2)
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rolled-back")

    def test_restore_fsync_failure_retains_recovery_required_marker(self):
        with mock.patch(
            "ops.deploy.receiver_upgrade._fsync_live_targets",
            side_effect=[None, OSError("injected restore fsync failure")],
        ), self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "exact rollback both failed"
        ):
            self.execute(
                self.install_new,
                lambda: (_ for _ in ()).throw(
                    receiver_upgrade.UpgradeError("preflight failed")
                ),
            )
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "rollback-failed")
        self.assertTrue(self.transaction()["recovery_required"])

    def test_activation_rejects_a_missing_target_parent_without_creating_it(self):
        missing_parent = self.system / "usr/local/sbin"
        shutil.rmtree(missing_parent)
        before = independent_fingerprint(self.system)
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            populate(staging, "new", all_targets=True)
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "parent is unavailable"
            ):
                receiver_upgrade.activate_staged(staging, self.system)
        self.assertFalse(missing_parent.exists())
        self.assertEqual(independent_fingerprint(self.system), before)

    def test_installer_failure_output_is_suppressed(self):
        secret = "DO-NOT-PRINT-INSTALLER-CREDENTIAL"
        hashes = {
            "BUH_PINNED_CONFIG_SHA256": receiver_upgrade._sha256(self.config),
            "BUH_PINNED_LEGACY_SHA256": receiver_upgrade._sha256(self.legacy),
            "BUH_PINNED_REQUEST_SHA256": receiver_upgrade._sha256(self.request),
            "BUH_RECEIVER_CONFIG_PATH": receiver_upgrade.CANONICAL_CONFIG_PATH,
            "BUH_LEGACY_RECEIVER_PATH": (
                receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH
            ),
        }
        completed = subprocess.CompletedProcess([], 1, stdout=secret, stderr=secret)
        with mock.patch.dict(os.environ, hashes, clear=False), mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.run", return_value=completed
        ) as run, self.assertRaises(receiver_upgrade.UpgradeError) as raised:
            self.execute(install=None, preflight=lambda: None)
        self.assertNotIn(secret, str(raised.exception))
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(
            run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1"
        )
        self.assert_exact_old_state()

    def test_production_path_rejects_missing_pinned_input_identities(self):
        with mock.patch.dict(
            os.environ,
            {
                "BUH_RECEIVER_CONFIG_PATH": receiver_upgrade.CANONICAL_CONFIG_PATH,
                "BUH_LEGACY_RECEIVER_PATH": (
                    receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH
                ),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "Pinned receiver input identity"
            ):
                receiver_upgrade.execute_upgrade(
                    commit="b" * 40,
                    repo_root=self.repo,
                    config=self.config,
                    legacy_receiver=self.legacy,
                    request=self.request,
                    system_root=self.system,
                    backup_root=self.backups,
                )
        self.assertFalse(self.backups.exists())
        self.assert_exact_old_state()

    def test_production_path_rejects_missing_lock_before_backup(self):
        pinned = {
            "BUH_PINNED_CONFIG_SHA256": receiver_upgrade._sha256(self.config),
            "BUH_PINNED_LEGACY_SHA256": receiver_upgrade._sha256(self.legacy),
            "BUH_PINNED_REQUEST_SHA256": receiver_upgrade._sha256(self.request),
            "BUH_RECEIVER_CONFIG_PATH": receiver_upgrade.CANONICAL_CONFIG_PATH,
            "BUH_LEGACY_RECEIVER_PATH": (
                receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH
            ),
        }
        with mock.patch.dict(os.environ, pinned, clear=False):
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "production deployment lock"
            ):
                receiver_upgrade.execute_upgrade(
                    commit="b" * 40,
                    repo_root=self.repo,
                    config=self.config,
                    legacy_receiver=self.legacy,
                    request=self.request,
                    system_root=Path("/"),
                    backup_root=self.backups,
                )
        self.assertFalse(self.backups.exists())
        self.assert_exact_old_state()

    def test_pinned_input_mutation_fails_before_activation(self):
        hashes = {
            "BUH_PINNED_CONFIG_SHA256": receiver_upgrade._sha256(self.config),
            "BUH_PINNED_LEGACY_SHA256": receiver_upgrade._sha256(self.legacy),
            "BUH_PINNED_REQUEST_SHA256": receiver_upgrade._sha256(self.request),
            "BUH_RECEIVER_CONFIG_PATH": receiver_upgrade.CANONICAL_CONFIG_PATH,
            "BUH_LEGACY_RECEIVER_PATH": (
                receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH
            ),
        }

        def staged_install(_arguments, **kwargs):
            staging = Path(kwargs["env"]["BUH_RECEIVER_INSTALL_ROOT"])
            populate(staging, "new", all_targets=True)
            self.request.write_text("changed\n", encoding="utf-8")
            return subprocess.CompletedProcess([], 0)

        with mock.patch.dict(os.environ, hashes, clear=False):
            with mock.patch(
                "ops.deploy.receiver_upgrade.subprocess.run",
                side_effect=staged_install,
            ):
                with self.assertRaisesRegex(
                    receiver_upgrade.UpgradeError, "changed during staging"
                ):
                    receiver_upgrade.execute_upgrade(
                        commit="b" * 40,
                        repo_root=self.repo,
                        config=self.config,
                        legacy_receiver=self.legacy,
                        request=self.request,
                        system_root=self.system,
                        backup_root=self.backups,
                        preflight_callback=lambda: None,
                    )
        self.assert_exact_old_state()
        self.assertEqual(self.transaction()["state"], "not-activated")

    def test_noncanonical_privileged_paths_are_rejected_before_installation(self):
        hashes = {
            "BUH_PINNED_CONFIG_SHA256": receiver_upgrade._sha256(self.config),
            "BUH_PINNED_LEGACY_SHA256": receiver_upgrade._sha256(self.legacy),
            "BUH_PINNED_REQUEST_SHA256": receiver_upgrade._sha256(self.request),
            "BUH_RECEIVER_CONFIG_PATH": "/root/alternate-receiver.json",
            "BUH_LEGACY_RECEIVER_PATH": "/usr/bin/bash",
        }
        with mock.patch.dict(os.environ, hashes, clear=False), mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.run"
        ) as installer, self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "path binding"
        ):
            self.execute(install=None, preflight=lambda: None)
        installer.assert_not_called()
        self.assertFalse(self.backups.exists())
        self.assert_exact_old_state()

    def test_terminal_marker_write_falls_back_to_recovery_required(self):
        backup, _ = self.execute(self.install_new, lambda: None)
        transaction = receiver_upgrade.ReceiverTransaction(
            backup,
            "a" * 40,
            backup / "manifest.json",
            backup / "recovery-manifest.json",
        )
        values = []

        def capture(_path, value):
            values.append(value)
            if len(values) == 1:
                raise OSError("injected terminal marker fsync failure")

        with mock.patch(
            "ops.deploy.receiver_upgrade._atomic_json", side_effect=capture
        ), self.assertRaises(OSError):
            transaction.record("completed")
        self.assertEqual([value["state"] for value in values], [
            "completed",
            "rollback-failed",
        ])
        self.assertTrue(values[-1]["recovery_required"])

    def test_unexpected_cli_error_does_not_expose_exception_message(self):
        secret = "DO-NOT-PRINT-/root/private-input-secret"
        output = StringIO()
        with mock.patch(
            "ops.deploy.receiver_upgrade._verify_production_path_bindings",
            side_effect=OSError(secret),
        ), redirect_stderr(output):
            result = receiver_upgrade.main(
                [
                    "upgrade",
                    "a" * 40,
                    "/reviewed",
                    "/pinned/config",
                    "/pinned/legacy",
                    "/pinned/request",
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_upgrade_main_selects_lock_from_canonical_live_config(self):
        read_descriptor, write_descriptor = os.pipe()
        os.close(write_descriptor)
        canonical_config = mock.sentinel.canonical_config
        with mock.patch(
            "ops.deploy.receiver_upgrade._verify_production_path_bindings"
        ), mock.patch(
            "ops.deploy.receiver_upgrade._verify_live_production_inputs"
        ), mock.patch.object(
            receiver_upgrade.ReceiverConfig,
            "load",
            return_value=canonical_config,
        ) as load, mock.patch(
            "ops.deploy.receiver_upgrade._open_production_lock",
            return_value=read_descriptor,
        ) as open_lock, mock.patch(
            "ops.deploy.receiver_upgrade.execute_upgrade",
            return_value=(Path("/verified-backup"), False),
        ), mock.patch("builtins.print"):
            result = receiver_upgrade.main(
                [
                    "upgrade",
                    "a" * 40,
                    "/reviewed",
                    "/pinned/config",
                    "/pinned/legacy",
                    "/pinned/request",
                ]
            )
        self.assertEqual(result, 0)
        load.assert_called_once_with(Path(receiver_upgrade.CANONICAL_CONFIG_PATH))
        open_lock.assert_called_once_with(canonical_config)


class ReceiverNestedPreflightTests(unittest.TestCase):
    @staticmethod
    def invoke(process, termination):
        recovery_check = mock.Mock()
        success_check = mock.Mock()
        with mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.Popen", return_value=process
        ), mock.patch(
            "ops.deploy.receiver_upgrade._signal_nested_preflight"
        ) as signal_child, mock.patch(
            "ops.deploy.receiver_upgrade._kill_nested_preflight"
        ) as kill_child:
            with unittest.TestCase().assertRaises(receiver_upgrade.UpgradeError) as raised:
                receiver_upgrade._run_nested_preflight(
                    arguments=["receiver", "preflight"],
                    input_handle=None,
                    environment={},
                    production_lock_fd=123,
                    termination=termination,
                    recovery_check=recovery_check,
                    success_check=success_check,
                )
        return (
            raised.exception,
            signal_child,
            kill_child,
            recovery_check,
            success_check,
        )

    def test_timeout_forwards_term_and_allows_bounded_child_rollback(self):
        class Process:
            pid = 2468

            def __init__(self):
                self.calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("receiver", timeout)
                return 0

        process = Process()
        termination = receiver_upgrade._TerminationGuard()
        error, signal_child, kill_child, recovery_check, success_check = self.invoke(
            process, termination
        )
        self.assertIn("coordinated rollback", str(error))
        signal_child.assert_called_once_with(process, signal.SIGTERM)
        kill_child.assert_not_called()
        recovery_check.assert_called_once_with()
        success_check.assert_not_called()
        self.assertEqual(process.calls, 2)

    def test_child_that_cannot_rollback_is_killed_after_the_grace_period(self):
        class Process:
            pid = 2468

            def __init__(self):
                self.calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls <= 2:
                    raise subprocess.TimeoutExpired("receiver", timeout)
                return -9

        process = Process()
        termination = receiver_upgrade._TerminationGuard()
        error, signal_child, kill_child, recovery_check, success_check = self.invoke(
            process, termination
        )
        self.assertIn("did not complete rollback", str(error))
        signal_child.assert_called_once_with(process, signal.SIGTERM)
        kill_child.assert_called_once_with(process)
        recovery_check.assert_called_once_with()
        success_check.assert_not_called()
        self.assertEqual(process.calls, 3)

    def test_operator_signal_is_forwarded_to_the_child_process_group(self):
        interrupted = receiver_upgrade.UpgradeError("Receiver upgrade interrupted by SIGINT.")

        class Process:
            pid = 2468

            def __init__(self):
                self.calls = 0

            def wait(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise interrupted
                return 0

        process = Process()
        termination = receiver_upgrade._TerminationGuard()
        termination.signal_number = signal.SIGINT
        error, signal_child, kill_child, recovery_check, success_check = self.invoke(
            process, termination
        )
        self.assertIs(error, interrupted)
        signal_child.assert_called_once_with(process, signal.SIGINT)
        kill_child.assert_not_called()
        recovery_check.assert_called_once_with()
        success_check.assert_not_called()
        self.assertEqual(process.calls, 2)

    def test_unresolved_child_recovery_has_a_distinct_fail_closed_error(self):
        class Process:
            pid = 2468

            def wait(self, timeout=None):
                del timeout
                return 1

        process = Process()
        termination = receiver_upgrade._TerminationGuard()
        with mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.Popen", return_value=process
        ):
            with self.assertRaises(
                receiver_upgrade._NestedPreflightRecoveryError
            ) as raised:
                receiver_upgrade._run_nested_preflight(
                    arguments=["receiver", "preflight"],
                    input_handle=None,
                    environment={},
                    production_lock_fd=123,
                    termination=termination,
                    recovery_check=mock.Mock(
                        side_effect=receiver_upgrade.UpgradeError("unresolved")
                    ),
                    success_check=mock.Mock(),
                )
        self.assertEqual(
            str(raised.exception),
            "Application preflight rollback could not be verified.",
        )

    def test_success_with_active_recovery_state_is_recovered_but_rejected(self):
        class Process:
            pid = 2468

            def wait(self, timeout=None):
                del timeout
                return 0

        recovery_check = mock.Mock()
        success_check = mock.Mock(
            side_effect=receiver_upgrade.UpgradeError("active recovery marker")
        )
        with mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.Popen", return_value=Process()
        ), self.assertRaisesRegex(
            receiver_upgrade.UpgradeError, "reported success with unresolved"
        ):
            receiver_upgrade._run_nested_preflight(
                arguments=["receiver", "preflight"],
                input_handle=None,
                environment={},
                production_lock_fd=123,
                termination=receiver_upgrade._TerminationGuard(),
                recovery_check=recovery_check,
                success_check=success_check,
            )
        success_check.assert_called_once_with()
        recovery_check.assert_called_once_with()

    def test_clean_success_does_not_invoke_recovery(self):
        process = mock.Mock()
        process.wait.return_value = 0
        recovery_check = mock.Mock()
        success_check = mock.Mock()
        with mock.patch(
            "ops.deploy.receiver_upgrade.subprocess.Popen", return_value=process
        ):
            receiver_upgrade._run_nested_preflight(
                arguments=["receiver", "preflight"],
                input_handle=None,
                environment={},
                production_lock_fd=123,
                termination=receiver_upgrade._TerminationGuard(),
                recovery_check=recovery_check,
                success_check=success_check,
            )
        success_check.assert_called_once_with()
        recovery_check.assert_not_called()

    def test_production_manual_recovery_uses_reviewed_application_recovery(self):
        config = Path("/root/recovery-config.json")
        with mock.patch(
            "ops.deploy.receiver_upgrade._verify_nested_preflight_recovery"
        ) as recovery:
            receiver_upgrade._recover_application_before_receiver_restore(
                Path("/"), config
            )
        recovery.assert_called_once_with(config)

    def test_nested_failure_consumes_and_proves_the_child_recovery_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = receiver_upgrade.ReceiverConfig.load(
                ROOT / "ops/deploy/receiver-config.example.json"
            )
            state = root / "state"
            state.mkdir()
            config = dataclasses.replace(source, state_dir=state)
            marker = state / "active-recovery.json"
            marker.write_text("{}\n", encoding="ascii")

            def recover(_config):
                marker.unlink()
                return "recovered"

            with mock.patch.object(
                receiver_upgrade.ReceiverConfig, "load", return_value=config
            ), mock.patch(
                "ops.deploy.docker_host.DockerHost.recover_incomplete_plan",
                side_effect=recover,
            ) as recovery:
                receiver_upgrade._verify_nested_preflight_recovery(
                    root / "receiver.json"
                )
            recovery.assert_called_once_with(config)
            self.assertFalse(marker.exists())

    def test_nested_failure_rejects_an_unresolved_child_recovery_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = receiver_upgrade.ReceiverConfig.load(
                ROOT / "ops/deploy/receiver-config.example.json"
            )
            state = root / "state"
            state.mkdir()
            config = dataclasses.replace(source, state_dir=state)
            (state / "active-recovery.json").write_text("{}\n", encoding="ascii")
            with mock.patch.object(
                receiver_upgrade.ReceiverConfig, "load", return_value=config
            ), mock.patch(
                "ops.deploy.docker_host.DockerHost.recover_incomplete_plan",
                return_value=None,
            ), self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "marker remains active"
            ):
                receiver_upgrade._verify_nested_preflight_recovery(
                    root / "receiver.json"
                )


@unittest.skipUnless(os.name == "posix", "production lock uses POSIX flock")
class ReceiverUpgradeLockTests(unittest.TestCase):
    def config(self, root: Path):
        source = receiver_upgrade.ReceiverConfig.load(
            ROOT / "ops/deploy/receiver-config.example.json"
        )
        return dataclasses.replace(
            source,
            state_dir=root / "state",
            backup_dir=root / "backups",
        )

    def test_upgrade_requires_existing_private_directories_and_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            config.state_dir.mkdir(mode=0o700)
            config.backup_dir.mkdir(mode=0o700)
            before = {
                path: stat.S_IMODE(path.stat().st_mode)
                for path in (config.state_dir, config.backup_dir)
            }
            with simulated_receiver_upgrade_root_metadata(), self.assertRaisesRegex(
                receiver_upgrade.DeploymentError, "lock is unavailable"
            ):
                receiver_upgrade._open_production_lock(config)
            self.assertFalse((config.state_dir / "deploy.lock").exists())
            self.assertEqual(
                {
                    path: stat.S_IMODE(path.stat().st_mode)
                    for path in (config.state_dir, config.backup_dir)
                },
                before,
            )

    def test_upgrade_lock_open_preserves_exact_existing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            config.state_dir.mkdir(mode=0o700)
            config.backup_dir.mkdir(mode=0o700)
            lock_path = config.state_dir / "deploy.lock"
            lock_path.write_bytes(b"existing-lock-sentinel\n")
            lock_path.chmod(0o600)
            before = (
                lock_path.read_bytes(),
                stat.S_IMODE(lock_path.stat().st_mode),
                stat.S_IMODE(config.state_dir.stat().st_mode),
                stat.S_IMODE(config.backup_dir.stat().st_mode),
            )
            with simulated_receiver_upgrade_root_metadata():
                descriptor = receiver_upgrade._open_production_lock(config)
                os.close(descriptor)
            self.assertEqual(
                (
                    lock_path.read_bytes(),
                    stat.S_IMODE(lock_path.stat().st_mode),
                    stat.S_IMODE(config.state_dir.stat().st_mode),
                    stat.S_IMODE(config.backup_dir.stat().st_mode),
                ),
                before,
            )

    def test_upgrade_rejects_shared_backup_directory_without_chmod(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            config.state_dir.mkdir(mode=0o700)
            config.backup_dir.mkdir(mode=0o755)
            config.backup_dir.chmod(0o755)
            lock_path = config.state_dir / "deploy.lock"
            lock_path.touch(mode=0o600)
            with simulated_receiver_upgrade_root_metadata(), self.assertRaisesRegex(
                receiver_upgrade.DeploymentError, "backup directory is not private"
            ):
                receiver_upgrade._open_production_lock(config)
            self.assertEqual(stat.S_IMODE(config.backup_dir.stat().st_mode), 0o755)

    def test_world_writable_ancestor_is_rejected_before_lock_access(self):
        def details(path):
            mode = 0o40777 if path == Path("/unsafe") else 0o40755
            return SimpleNamespace(st_mode=mode, st_uid=0)

        with mock.patch.object(
            Path, "lstat", autospec=True, side_effect=details
        ), self.assertRaisesRegex(receiver_upgrade.UpgradeError, "ancestor is unsafe"):
            receiver_upgrade._verify_root_owned_ancestor_chain(
                Path("/unsafe/state/deploy.lock")
            )


@unittest.skipUnless(
    os.name == "posix"
    and BASH
    and PYTHON3
    and shutil.which("git")
    and shutil.which("install"),
    "requires POSIX bash, Python, Git, and install",
)
class ReceiverInstallerBehaviorTests(unittest.TestCase):
    def test_real_installer_populates_only_the_temporary_install_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            staging = base / "staging"
            repo.mkdir()
            staging.mkdir()
            install_sources = (
                "ops/__init__.py",
                "ops/buh-github-observe-entry",
                "ops/buh-github-observe-root",
                "ops/buh-redact-diagnostics.py",
                "ops/deploy/__init__.py",
                "ops/deploy/contracts.py",
                "ops/deploy/coordinated-recovery.json",
                "ops/deploy/docker_host.py",
                "ops/deploy/engine.py",
                "ops/deploy/receiver.py",
                "ops/deploy/buh-deploy-dispatch",
                "ops/deploy/buh-platform-v2-receiver",
                "ops/deploy/install-receiver.sh",
                "ops/release/__init__.py",
                "ops/release/buh_release.py",
                "ops/release/recovery_policy.py",
            )
            for relative in install_sources:
                destination = repo / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)

            subprocess.run(["git", "init", "--quiet", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "Receiver Test"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "config",
                    "user.email",
                    "receiver@example.invalid",
                ],
                check=True,
            )
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "--quiet", "-m", "fixture"],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            inventory = base / "TRACKED"
            inventory.write_text(
                subprocess.run(
                    ["git", "-C", repo, "ls-tree", "-r", "--full-tree", commit],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout,
                encoding="ascii",
            )
            inventory.chmod(0o600)

            live_state = base / "live-state-must-remain-absent"
            live_backups = base / "live-backups-must-remain-absent"
            config_value = json.loads(
                (ROOT / "ops/deploy/receiver-config.example.json").read_text(
                    encoding="utf-8"
                )
            )
            config_value["state_dir"] = live_state.as_posix()
            config_value["backup_dir"] = live_backups.as_posix()
            config = base / "receiver.json"
            config.write_text(
                json.dumps(
                    config_value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n",
                encoding="utf-8",
            )
            legacy = base / "legacy-receiver"
            legacy_bytes = b"#!/bin/sh\nexit 0\n"
            legacy.write_bytes(legacy_bytes)
            legacy.chmod(0o755)
            logical_legacy = base / "logical-legacy-receiver"
            # This decoy models caller-controlled legacy bytes. The installer
            # must consume only the pinned copy and bind sudoers to the one
            # canonical, separately verified forced-command path.
            logical_legacy_bytes = b"#!/bin/sh\nexit 19\n"
            logical_legacy.write_bytes(logical_legacy_bytes)
            logical_legacy.chmod(0o755)

            # Ownership-changing flags require root in production. The test keeps
            # the real `install` implementation and strips only -o/-g so an
            # unprivileged Linux CI runner can exercise every staging write.
            command = r'''
id() {
  if [[ "${1-}" == "-u" ]]; then printf '0\n'; else return 0; fi
}
stat() {
  if [[ "${*: -1}" == "$BUH_REVIEWED_INVENTORY" ]]; then
    printf '0:600\n'
  else
    command stat "$@"
  fi
}
install() {
  local filtered=()
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) filtered+=("$1"); shift ;;
    esac
  done
  if [[ " ${filtered[*]} " == *" -d "* ]]; then
    local consumes_next=0
    local value
    for value in "${filtered[@]}"; do
      if [[ "$consumes_next" -eq 1 ]]; then
        consumes_next=0
      elif [[ "$value" == "-m" ]]; then
        consumes_next=1
      elif [[ "$value" != -* ]]; then
        [[ "$value" == "$BUH_RECEIVER_INSTALL_ROOT"/* ]]
      fi
    done
  else
    local destination="${filtered[${#filtered[@]}-1]}"
    [[ "$destination" == "$BUH_RECEIVER_INSTALL_ROOT"/* ]]
  fi
  command install "${filtered[@]}"
}
visudo() { [[ "${1-}" == "-cf" && -f "${2-}" ]]; }
source "$1" "${@:2}"
'''
            environment = os.environ.copy()
            environment["BUH_RECEIVER_INSTALL_ROOT"] = str(staging)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["BUH_REVIEWED_COMMIT"] = commit
            environment["BUH_REVIEWED_INVENTORY"] = str(inventory)
            environment["BUH_RECEIVER_CONFIG_PATH"] = (
                receiver_upgrade.CANONICAL_CONFIG_PATH
            )
            environment["BUH_LEGACY_RECEIVER_PATH"] = (
                receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH
            )
            environment["BUH_PINNED_LEGACY_SHA256"] = hashlib.sha256(
                legacy_bytes
            ).hexdigest()
            result = subprocess.run(
                [
                    BASH,
                    "-c",
                    command,
                    "receiver-installer-test",
                    repo / "ops/deploy/install-receiver.sh",
                    config,
                    legacy,
                ],
                cwd=repo,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(live_state.exists())
            self.assertFalse(live_backups.exists())
            self.assertEqual(legacy.read_bytes(), legacy_bytes)
            self.assertEqual(logical_legacy.read_bytes(), logical_legacy_bytes)
            for logical in receiver_upgrade.TARGETS:
                self.assertTrue(actual(staging, logical).exists(), logical)
            self.assertTrue(actual(staging, live_state.as_posix()).is_dir())
            self.assertTrue(actual(staging, live_backups.as_posix()).is_dir())
            installed_config = actual(
                staging, "/etc/buh-platform-v2/receiver.json"
            )
            self.assertEqual(installed_config.read_bytes(), config.read_bytes())
            provenance = json.loads(
                actual(staging, "/etc/buh-platform-v2/INSTALL.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(provenance["source_commit"], commit)
            self.assertEqual(
                provenance["config_sha256"],
                hashlib.sha256(config.read_bytes()).hexdigest(),
            )
            expected_files = {
                relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
                for relative in install_sources
                if relative != "ops/deploy/install-receiver.sh"
            }
            self.assertEqual(provenance["files"], expected_files)
            sudoers = actual(
                staging, "/etc/sudoers.d/buh-platform-v2"
            ).read_text(encoding="utf-8")
            self.assertIn(receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH, sudoers)
            self.assertNotIn(str(legacy), sudoers)
            receiver_upgrade._verify_real_staged_contract(
                staging,
                repo,
                config,
                commit,
                receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH,
                require_root_owner=False,
            )
            if os.geteuid() != 0:
                with self.assertRaisesRegex(
                    receiver_upgrade.UpgradeError, "ownership is not root-owned"
                ):
                    receiver_upgrade._verify_real_staged_contract(
                        staging,
                        repo,
                        config,
                        commit,
                        receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH,
                        require_root_owner=True,
                    )
            unexpected = actual(
                staging, "/etc/buh-platform-v2/unexpected.conf"
            )
            unexpected.write_text("must fail closed\n", encoding="utf-8")
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "differs from the install contract"
            ):
                receiver_upgrade._verify_real_staged_contract(
                    staging,
                    repo,
                    config,
                    commit,
                    receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH,
                    require_root_owner=False,
                )
            unexpected.unlink()
            actual(staging, "/etc/buh-platform-v2").chmod(0o755)
            with self.assertRaisesRegex(
                receiver_upgrade.UpgradeError, "differs from the install contract"
            ):
                receiver_upgrade._verify_real_staged_contract(
                    staging,
                    repo,
                    config,
                    commit,
                    receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH,
                    require_root_owner=False,
                )


@unittest.skipUnless(
    POWERSHELL and shutil.which("git") and shutil.which("ssh") and shutil.which("scp"),
    "requires PowerShell, Git, SSH, and SCP",
)
class ReceiverPowerShellPreRootTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.preflight = Path(self.temporary.name) / "prepared-preflight.tar.gz"
        self.preflight.write_bytes(b"synthetic prepared preflight\n")
        for relative in powershell_required_sources():
            destination = self.repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        (self.repo / ".gitignore").write_text("ignored-secret\n", encoding="utf-8")
        subprocess.run(["git", "init", "--quiet", self.repo], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "config", "user.name", "Receiver Test"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                self.repo,
                "config",
                "user.email",
                "receiver@example.invalid",
            ],
            check=True,
        )
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)
        subprocess.run(
            ["git", "-C", self.repo, "commit", "--quiet", "-m", "fixture"],
            check=True,
        )
        self.commit = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tearDown(self):
        self.temporary.cleanup()

    def run_helper(self, *extra_arguments):
        return subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                self.repo / "setup/Upgrade-BUH-PlatformV2Receiver.ps1",
                "-ReviewedCommit",
                self.commit,
                "-PreflightRequest",
                self.preflight,
                *extra_arguments,
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def assert_rejected_before_remote_execution(self):
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "contains tracked, untracked, or ignored files",
            result.stdout + result.stderr,
        )
        return result

    def test_rejects_tracked_modification_before_remote_execution(self):
        with (self.repo / "ops/deploy/install-receiver.sh").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write("# modified\n")
        self.assert_rejected_before_remote_execution()

    def test_rejects_untracked_file_before_remote_execution(self):
        (self.repo / "untracked-secret").write_text("must not be read", encoding="utf-8")
        self.assert_rejected_before_remote_execution()

    def test_rejects_ignored_file_before_remote_execution(self):
        (self.repo / "ignored-secret").write_text("must not be imported", encoding="utf-8")
        ignored = subprocess.run(
            ["git", "-C", self.repo, "check-ignore", "ignored-secret"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)
        self.assert_rejected_before_remote_execution()

    def test_rejects_a_tracked_credential_path_before_remote_execution(self):
        credential = self.repo / ".env.production"
        credential.write_text("not-a-real-secret\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", self.repo, "add", "-f", ".env.production"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.repo, "commit", "--quiet", "-m", "unsafe fixture"],
            check=True,
        )
        self.commit = subprocess.run(
            ["git", "-C", self.repo, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "forbidden credential-like path", result.stdout + result.stderr
        )

    def test_rejects_noncanonical_config_and_legacy_paths_before_remote_execution(self):
        for arguments in (
            ("-ConfigPath", "/root/alternate-receiver.json"),
            ("-LegacyReceiver", "/usr/bin/bash"),
            (
                "-LegacyReceiver",
                "/usr/local/sbin/buh-platform-v2-receiver",
            ),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_helper(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "must match the canonical production configuration",
                    result.stdout + result.stderr,
                )


@unittest.skipUnless(BASH, "requires bash")
class ReceiverRootBootstrapContractTests(unittest.TestCase):
    def test_root_upgrade_rejects_a_normal_checkout(self):
        environment = os.environ.copy()
        for name in (
            "BUH_REVIEWED_INVENTORY",
            "BUH_REVIEWED_INVENTORY_SHA256",
            "BUH_REVIEWED_COMMIT",
            "BUH_PINNED_CONFIG_SHA256",
            "BUH_PINNED_LEGACY_SHA256",
            "BUH_PINNED_REQUEST_SHA256",
        ):
            environment.pop(name, None)
        result = subprocess.run(
            [
                BASH,
                "-c",
                "id() { printf '0\\n'; }; "
                "unset BUH_REVIEWED_INVENTORY BUH_REVIEWED_COMMIT; "
                'source "$1" "${@:2}"',
                "receiver-root-bootstrap-test",
                ROOT / "ops/deploy/upgrade-receiver.sh",
                "a" * 40,
                "/missing/config",
                "/missing/legacy",
                "/missing/request",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("root-private reviewed receiver bootstrap", result.stderr)


class ReceiverPowerShellPackageTests(unittest.TestCase):
    def test_helper_exports_only_the_explicit_commit_bound_allowlist(self):
        helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text()
        required = powershell_required_sources()
        self.assertIn("git -C $RepoRoot archive", helper)
        self.assertIn("$ReviewedCommit -- $RequiredSources", helper)
        self.assertIn("ls-tree -r --full-tree $ReviewedCommit -- $RequiredSources", helper)
        self.assertEqual(len(required), len(set(required)))
        self.assertIn("tests/deploy/test_upgrade_receiver.py", required)
        self.assertNotIn(".env", required)

    def test_manual_recovery_imports_only_independently_verified_reviewed_source(self):
        helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text()
        self.assertIn('ParameterSetName = "Recover"', helper)
        self.assertIn("-m ops.deploy.receiver_upgrade recover", helper)
        self.assertIn('PYTHONPATH="$root_stage/source"', helper)
        self.assertIn("Receiver tree inventory verification failed.", helper)
        self.assertNotIn('PYTHONPATH="${BACKUP}/recovery-source"', helper)
        inventory_gate = helper.index("if actual != expected:")
        recovery_import = helper.index("-m ops.deploy.receiver_upgrade recover")
        self.assertLess(inventory_gate, recovery_import)

    def test_privileged_bootstrap_python_cannot_import_from_the_ssh_cwd(self):
        helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text()
        bootstrap = helper.split("$RootBootstrap = @'\n", 1)[1].split("\n'@", 1)[0]
        first_python = bootstrap.index("/usr/bin/python3 -P -")
        self.assertLess(bootstrap.index('cd -- "$root_stage"'), first_python)
        python_lines = [
            line.strip()
            for line in bootstrap.splitlines()
            if "python3" in line and "-m ops.deploy.receiver_upgrade" not in line
        ]
        self.assertTrue(python_lines)
        self.assertTrue(
            all("/usr/bin/python3 -P -" in line for line in python_lines),
            python_lines,
        )
        installer = (ROOT / "ops/deploy/install-receiver.sh").read_text()
        inline_python = [
            line.strip()
            for line in installer.splitlines()
            if "python3" in line
        ]
        self.assertTrue(all("/usr/bin/python3 -P" in line for line in inline_python))

    def test_helper_and_installer_bind_canonical_privileged_paths(self):
        helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text()
        installer = (ROOT / "ops/deploy/install-receiver.sh").read_text()
        upgrade = (ROOT / "ops/deploy/upgrade-receiver.sh").read_text()
        dispatcher = (ROOT / "ops/deploy/buh-deploy-dispatch").read_text()
        for source in (helper, installer, upgrade):
            self.assertIn(receiver_upgrade.CANONICAL_CONFIG_PATH, source)
        for source in (helper, installer, upgrade, dispatcher):
            self.assertIn(receiver_upgrade.CANONICAL_LEGACY_RECEIVER_PATH, source)
        self.assertIn("must not overlap a managed receiver target", installer)
        self.assertIn('[ "$#" -ne 1 ]', dispatcher)

    def test_helper_uses_existing_alias_and_current_tree_archive(self):
        helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text()
        self.assertIn('$SshTarget = "b-uh"', helper)
        self.assertNotIn("buh-vps", helper)
        self.assertIn("git -C $RepoRoot archive", helper)
        self.assertIn("ls-tree -r --full-tree", helper)
        self.assertNotIn("git bundle", helper)
        self.assertNotIn("receiver.bundle", helper)
        self.assertIn("--untracked-files=all", helper)
        self.assertIn("sha256sum -c", helper)
        self.assertIn("sudo --", helper)
        self.assertIn("/usr/bin/env -i HOME=/root", helper)
        self.assertIn('flags |= getattr(os, "O_NOFOLLOW", 0)', helper)
        self.assertIn("details.st_uid != 0", helper)
        self.assertIn("mode != 0o600", helper)
        self.assertIn("validate_parents(path)", helper)
        self.assertIn("os.fsync(outgoing.fileno())", helper)
        self.assertIn('PSObject.Properties["ArgumentList"]', helper)
        self.assertIn("$RootProcessInfo.Arguments", helper)
        self.assertIn("$RootProcess.StandardInput.Write($LfBootstrap)", helper)
        upgrade = (ROOT / "ops/deploy/upgrade-receiver.sh").read_text()
        self.assertIn("Run only from the root-private reviewed receiver bootstrap", upgrade)
        self.assertNotIn("git_root=", upgrade)
        self.assertEqual(upgrade.count("\nverify_reviewed_tree\n"), 2)
        self.assertIn("BUH_REVIEWED_INVENTORY_SHA256", helper + upgrade)
        self.assertNotIn("/.ssh/", helper + upgrade)

    @unittest.skipUnless(BASH, "requires bash")
    def test_embedded_root_bootstrap_is_valid_bash(self):
        helper = (ROOT / "setup/Upgrade-BUH-PlatformV2Receiver.ps1").read_text()
        bootstrap = helper.split("$RootBootstrap = @'\n", 1)[1].split("\n'@", 1)[0]
        result = subprocess.run(
            [BASH, "-n"],
            input=bootstrap,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(
        os.name == "posix" and POWERSHELL and shutil.which("git"),
        "requires POSIX, PowerShell, and Git",
    )
    def test_helper_transfers_only_current_tree_and_lf_only_root_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            capture = base / "capture"
            fake_bin = base / "fake-bin"
            repo.mkdir()
            capture.mkdir()
            fake_bin.mkdir()
            required = powershell_required_sources()
            for relative in required:
                destination = repo / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)

            subprocess.run(["git", "init", "--quiet", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "Receiver Test"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repo,
                    "config",
                    "user.email",
                    "receiver@example.invalid",
                ],
                check=True,
            )
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            history_secret = f"history-{secrets.token_hex(24)}"
            historical = repo / "removed-history-secret.txt"
            historical.write_text(history_secret + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", repo, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "--quiet", "-m", "historical fixture"],
                check=True,
            )
            historical.unlink()
            subprocess.run(["git", "-C", repo, "add", "-u"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "--quiet", "-m", "reviewed fixture"],
                check=True,
            )
            commit = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            fake_native = """#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

capture = Path(os.environ["BUH_FAKE_CAPTURE"])
tool = Path(sys.argv[0]).name
with (capture / "calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": tool, "argv": sys.argv[1:]}) + "\\n")
if tool == "scp":
    values = [value for value in sys.argv[1:] if value != "--"]
    for source in values[:-1]:
        shutil.copy2(source, capture / Path(source).name)
elif any("/bin/bash -seu" in value for value in sys.argv[1:]):
    (capture / "bootstrap.bin").write_bytes(sys.stdin.buffer.read())
"""
            for name in ("ssh", "scp"):
                executable = fake_bin / name
                executable.write_text(fake_native, encoding="utf-8", newline="\n")
                executable.chmod(0o755)

            secret = f"local-{secrets.token_hex(24)}"
            preflight_secret = f"preflight-{secrets.token_hex(24)}"
            preflight = base / "prepared-preflight.tar.gz"
            preflight.write_text(preflight_secret + "\n", encoding="ascii")
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["BUH_FAKE_CAPTURE"] = str(capture)
            environment["BUH_LOCAL_SECRET"] = secret
            result = subprocess.run(
                [
                    POWERSHELL,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    repo / "setup/Upgrade-BUH-PlatformV2Receiver.ps1",
                    "-ReviewedCommit",
                    commit,
                    "-PreflightRequest",
                    preflight,
                ],
                cwd=repo,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-1000:])

            archive = capture / "receiver-tree.tar"
            inventory = capture / "TRACKED"
            checksums = capture / "SHA256SUMS"
            self.assertTrue(archive.is_file())
            self.assertTrue(inventory.is_file())
            self.assertTrue(checksums.is_file())
            checksum_parts = checksums.read_text(encoding="ascii").split()
            expected_hashes = dict(
                zip(checksum_parts[1::2], checksum_parts[0::2], strict=True)
            )
            self.assertEqual(
                set(expected_hashes),
                {"receiver-tree.tar", "TRACKED", "preflight-request.tar.gz"},
            )
            self.assertEqual(
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                expected_hashes["receiver-tree.tar"],
            )
            self.assertEqual(
                hashlib.sha256(inventory.read_bytes()).hexdigest(),
                expected_hashes["TRACKED"],
            )
            transferred_request = capture / "preflight-request.tar.gz"
            self.assertEqual(transferred_request.read_text(encoding="ascii"), preflight_secret + "\n")
            self.assertEqual(
                hashlib.sha256(transferred_request.read_bytes()).hexdigest(),
                expected_hashes["preflight-request.tar.gz"],
            )
            archived_commit = subprocess.run(
                ["git", "get-tar-commit-id"],
                check=True,
                input=archive.read_bytes(),
                capture_output=True,
            ).stdout.decode("ascii").strip()
            self.assertEqual(archived_commit, commit)
            with tarfile.open(archive, "r:") as reviewed_tar:
                archived_files = {
                    member.name for member in reviewed_tar.getmembers() if member.isfile()
                }
            self.assertEqual(archived_files, set(required))
            self.assertNotIn("removed-history-secret.txt", archived_files)
            archive_bytes = archive.read_bytes()
            inventory_text = inventory.read_text(encoding="ascii")
            self.assertFalse(
                history_secret.encode("ascii") in archive_bytes,
                "Historical sentinel leaked into the reviewed source archive",
            )
            self.assertFalse(
                history_secret in inventory_text,
                "Historical sentinel leaked into the reviewed tree inventory",
            )

            bootstrap_bytes = (capture / "bootstrap.bin").read_bytes()
            self.assertTrue(bootstrap_bytes.endswith(b"\n"))
            self.assertFalse(
                b"\r" in bootstrap_bytes,
                "Root bootstrap contains a carriage-return byte",
            )
            self.assertFalse(
                secret.encode("ascii") in bootstrap_bytes,
                "Caller environment sentinel leaked into the root bootstrap",
            )
            self.assertFalse(
                secret.encode("ascii") in archive_bytes,
                "Caller environment sentinel leaked into the reviewed source archive",
            )
            bootstrap = bootstrap_bytes.decode("utf-8")
            self.assertIn("BUH_REVIEWED_INVENTORY", bootstrap)
            self.assertIn("upgrade-receiver.sh", bootstrap)

            calls = [
                json.loads(line)
                for line in (capture / "calls.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            ssh_calls = [call for call in calls if call["tool"] == "ssh"]
            scp_calls = [call for call in calls if call["tool"] == "scp"]
            self.assertEqual(len(scp_calls), 1)
            self.assertTrue(scp_calls[0]["argv"][-1].startswith("b-uh:/tmp/"))
            transferred = {
                Path(value).name
                for value in scp_calls[0]["argv"]
                if value != "--" and not value.startswith("b-uh:")
            }
            self.assertEqual(
                transferred,
                {
                    "receiver-tree.tar",
                    "TRACKED",
                    "SHA256SUMS",
                    "preflight-request.tar.gz",
                },
            )
            self.assertFalse(any("bundle" in value for value in transferred))
            root_call = next(
                call
                for call in ssh_calls
                if any("/bin/bash -seu" in value for value in call["argv"])
            )
            self.assertEqual(root_call["argv"][:2], ["-T", "b-uh"])
            root_command = root_call["argv"][2]
            self.assertIn(commit, root_command)
            self.assertIn("/tmp/buh-receiver-upgrade-", root_command)
            self.assertIn("preflight-request.tar.gz", root_command)
            self.assertNotIn(secret, root_command)
            self.assertNotIn(preflight_secret, root_command)


class ReceiverExecutableContractTests(unittest.TestCase):
    def test_directly_executed_receiver_scripts_have_lf_shebangs(self):
        for relative in (
            "ops/deploy/install-receiver.sh",
            "ops/deploy/upgrade-receiver.sh",
        ):
            with self.subTest(path=relative):
                payload = (ROOT / relative).read_bytes()
                self.assertTrue(payload.startswith(b"#!/usr/bin/env bash\n"))
                self.assertNotIn(b"\r", payload)


if __name__ == "__main__":
    unittest.main()
