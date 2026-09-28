"""Approval binding and interrupted diagnostics activation contracts."""

import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.diagnostics import buh_diagnostics_install as installer


class DiagnosticsInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.archive = self.root / "source.tar"
        self.key = self.root / "deploy-key"
        self.hosts = self.root / "known-hosts"
        self.manifest = self.root / "manifest.json"
        self.key.write_text("synthetic private key, never a real credential", encoding="utf-8")
        self.hosts.write_text("synthetic pinned host key", encoding="utf-8")
        self.commit = "a" * 40
        self.services = {"schema_version": 1, "compose_project": "aa-docker",
                         "services": {"allianceauth_gunicorn": 1,
                                      "allianceauth_worker": 5,
                                      "allianceauth_worker_services": 1,
                                      "allianceauth_beat": 1,
                                      "auth_mysql": 1, "redis": 1, "nginx": 1}}
        source_root = Path(__file__).resolve().parents[2]
        with tarfile.open(self.archive, "w", format=tarfile.PAX_FORMAT,
                          pax_headers={"comment": self.commit}) as archive:
            for relative in installer.SOURCE_FILES.values():
                source = source_root / relative
                target = self.bundle / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                archive.add(source, arcname=relative)
        fingerprint = patch.object(installer, "key_fingerprint",
                                   return_value="SHA256:synthetic-public-key-fingerprint")
        fingerprint.start()
        self.addCleanup(fingerprint.stop)
        value = installer.manifest_for(self.commit, self.archive, self.bundle,
                                       self.key, self.hosts, self.services)
        self.manifest.write_bytes(installer.canonical(value))
        self.approval_hash = installer.digest(self.manifest.read_bytes())
        self.paths = installer.Paths(self.root / "host")
        self.paths.units.mkdir(parents=True)

    def test_manifest_binds_archive_source_key_and_host_pin(self):
        value, observed_hash = installer.verify_manifest(
            self.manifest, self.archive, self.bundle, self.key, self.hosts
        )
        self.assertEqual(observed_hash, self.approval_hash)
        self.assertEqual(value["source_commit"], self.commit)
        target = self.bundle / installer.SOURCE_FILES["collector"]
        target.write_bytes(target.read_bytes() + b"\n# tampered\n")
        with self.assertRaisesRegex(ValueError, "differs"):
            installer.verify_manifest(self.manifest, self.archive, self.bundle,
                                      self.key, self.hosts)

    @unittest.skipIf(os.name == "nt", "systemd symlink transaction is Linux-only")
    def test_success_records_approved_identity_and_starts_only_after_activation(self):
        calls = []

        def systemctl(*args, check=True):
            calls.append(args)
            return args[0] != "is-enabled"

        with patch.object(installer, "systemctl", side_effect=systemctl):
            receipt = installer.install(
                self.paths, self.manifest, self.archive, self.bundle, self.key,
                self.hosts, "INSTALL BUH DIAGNOSTICS " + self.approval_hash,
            )
        self.assertEqual(receipt["manifest_sha256"], self.approval_hash)
        self.assertEqual(receipt["deploy_key_fingerprint"],
                         "SHA256:synthetic-public-key-fingerprint")
        self.assertEqual(receipt["source_archive_sha256"],
                         installer.file_digest(self.archive))
        self.assertTrue(self.paths.current.is_symlink())
        self.assertFalse(self.paths.marker.exists())
        self.assertEqual([args for args in calls if args[0] == "start"],
                         [("start", name) for name in installer.SERVICES])
        self.assertLess(calls.index(("start", installer.SERVICES[1])),
                        calls.index(("enable", "--now", installer.TIMERS[0])))

    @unittest.skipIf(os.name == "nt", "systemd symlink transaction is Linux-only")
    def test_service_failure_rolls_back_without_enabling_timers(self):
        calls = []

        def systemctl(*args, check=True):
            calls.append(args)
            if args == ("start", installer.SERVICES[1]):
                raise RuntimeError("synthetic publication failure")
            return args[0] != "is-enabled"

        with patch.object(installer, "systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(RuntimeError, "publication failure"):
                installer.install(
                    self.paths, self.manifest, self.archive, self.bundle, self.key,
                    self.hosts, "INSTALL BUH DIAGNOSTICS " + self.approval_hash,
                )
        self.assertFalse(self.paths.current.exists())
        self.assertFalse(self.paths.marker.exists())
        self.assertFalse(self.paths.receipt.exists())
        self.assertFalse(any(self.paths.units.iterdir()))
        self.assertNotIn(("enable", "--now", installer.TIMERS[0]), calls)

    @unittest.skipIf(os.name == "nt", "systemd symlink transaction is Linux-only")
    def test_process_termination_after_switch_recovers_previous_install(self):
        previous_hash = "b" * 64
        old = self.paths.base / "releases" / previous_hash
        old.mkdir(parents=True)
        os.symlink("releases/" + previous_hash, self.paths.current)
        self.paths.state.mkdir(parents=True)
        old_receipt = {"manifest_sha256": previous_hash}
        installer.atomic_file(self.paths.receipt, installer.canonical(old_receipt))
        original_switch = installer.switch_current

        def terminated_switch(paths, target):
            original_switch(paths, target)
            raise SystemExit("synthetic host loss after atomic activation")

        def systemctl(*args, check=True):
            return args == ("is-enabled", installer.TIMERS[0])

        with (patch.object(installer, "systemctl", side_effect=systemctl),
              patch.object(installer, "switch_current", side_effect=terminated_switch)):
            with self.assertRaises(SystemExit):
                installer.install(
                    self.paths, self.manifest, self.archive, self.bundle, self.key,
                    self.hosts, "INSTALL BUH DIAGNOSTICS " + self.approval_hash,
                )
        self.assertTrue(self.paths.marker.exists())
        with patch.object(installer, "systemctl", side_effect=systemctl):
            self.assertTrue(installer.recover(self.paths))
        self.assertEqual(os.readlink(self.paths.current), "releases/" + previous_hash)
        self.assertEqual(json.loads(self.paths.receipt.read_text()), old_receipt)
        self.assertFalse(self.paths.marker.exists())

    @unittest.skipIf(os.name == "nt", "systemd symlink transaction is Linux-only")
    def test_termination_at_each_activation_boundary_is_recoverable(self):
        for boundary in ("daemon-reload", "start-collector", "start-publisher",
                         "enable-collector", "enable-publisher", "receipt"):
            with self.subTest(boundary=boundary):
                paths = installer.Paths(self.root / ("host-" + boundary))
                paths.units.mkdir(parents=True)
                previous_hash = "b" * 64
                (paths.base / "releases" / previous_hash).mkdir(parents=True)
                os.symlink("releases/" + previous_hash, paths.current)
                paths.state.mkdir(parents=True)
                old_receipt = {"manifest_sha256": previous_hash}
                installer.atomic_file(paths.receipt, installer.canonical(old_receipt))
                original_atomic = installer.atomic_file

                def interrupted_atomic(path, data, mode=0o600):
                    original_atomic(path, data, mode)
                    if boundary == "receipt" and path == paths.receipt:
                        raise SystemExit("synthetic termination after receipt write")

                boundary_names = {
                    "start-collector": "start-buh-diagnostics",
                    "start-publisher": "start-buh-diagnostics-publish",
                    "enable-collector": "enable-buh-diagnostics",
                    "enable-publisher": "enable-buh-diagnostics-publish",
                }
                actual_boundary = boundary_names.get(boundary, boundary)

                def run(*args, check=True):
                    key = ("start-" + args[1].removesuffix(".service")
                           if args[0] == "start" else
                           "enable-" + args[2].removesuffix(".timer")
                           if args[0] == "enable" else args[0])
                    if key == actual_boundary:
                        raise SystemExit("synthetic termination at " + boundary)
                    return args[0] != "is-enabled"

                with (patch.object(installer, "systemctl", side_effect=run),
                      patch.object(installer, "atomic_file",
                                   side_effect=interrupted_atomic)):
                    with self.assertRaises(SystemExit):
                        installer.install(
                            paths, self.manifest, self.archive, self.bundle,
                            self.key, self.hosts,
                            "INSTALL BUH DIAGNOSTICS " + self.approval_hash,
                        )
                self.assertTrue(paths.marker.exists())
                with patch.object(installer, "systemctl", return_value=True):
                    self.assertTrue(installer.recover(paths))
                self.assertEqual(os.readlink(paths.current), "releases/" + previous_hash)
                self.assertEqual(json.loads(paths.receipt.read_text()), old_receipt)
                self.assertFalse(paths.marker.exists())

    def test_wrong_confirmation_never_changes_host(self):
        with self.assertRaisesRegex(ValueError, "confirmation"):
            installer.install(self.paths, self.manifest, self.archive, self.bundle,
                              self.key, self.hosts, "INSTALL BUH DIAGNOSTICS wrong")
        self.assertFalse(self.paths.base.exists())


if __name__ == "__main__":
    unittest.main()
