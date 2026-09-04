from __future__ import annotations

import gzip
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.deploy import contracts


ROOT = Path(__file__).resolve().parents[2]


def write_canonical(path: Path, value) -> None:
    path.write_bytes(contracts.canonical_json_bytes(value))


def request_data(**overrides):
    value = {
        "schema_version": 1,
        "mode": "preflight",
        "repository": "Fifty5D/B-UH-AllianceAuth",
        "release_commit": "a" * 40,
        "release_ref": "release/platform-v0.4.0",
        "platform_version": "0.4.0",
        "manifest_sha256": "b" * 64,
        "workflow_run_id": "123456789",
        "workflow_run_attempt": 1,
    }
    value.update(overrides)
    return value


def tar_bytes(entries, *, pax=False) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
        with tarfile.open(
            fileobj=compressed,
            mode="w",
            format=tarfile.PAX_FORMAT if pax else tarfile.USTAR_FORMAT,
        ) as archive:
            for name, payload, kind in entries:
                info = tarfile.TarInfo(name)
                info.mtime = 0
                info.mode = 0o644
                if kind == "directory":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = str(payload)
                    archive.addfile(info)
                else:
                    data = bytes(payload)
                    info.size = len(data)
                    if pax:
                        info.pax_headers = {"comment": "forbidden"}
                    archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


class ReceiverConfigTests(unittest.TestCase):
    def test_canonical_example_loads_with_expected_production_boundary(self):
        path = ROOT / "ops/deploy/receiver-config.example.json"
        config = contracts.ReceiverConfig.load(path)
        self.assertEqual(config.repository, "Fifty5D/B-UH-AllianceAuth")
        self.assertEqual(config.app_dir, Path("/opt/aa-docker"))
        self.assertEqual(config.legacy_platform_version, "0.3.3")
        self.assertEqual(config.restore_tmpfs_mb, 4096)
        self.assertEqual(config.database_service, "auth_mysql")
        self.assertEqual(config.redis_service, "redis")
        self.assertEqual(config.proxy_service, "nginx")
        self.assertEqual(
            config.auth_services,
            (
                "allianceauth_gunicorn",
                "allianceauth_worker",
                "allianceauth_worker_services",
                "allianceauth_beat",
            ),
        )
        self.assertEqual(path.read_bytes(), contracts.canonical_json_bytes(json.loads(path.read_bytes())))

    def test_unknown_field_and_noncanonical_json_fail_closed(self):
        source = json.loads(
            (ROOT / "ops/deploy/receiver-config.example.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receiver.json"
            source["surprise"] = True
            write_canonical(path, source)
            with self.assertRaisesRegex(contracts.DeploymentError, "unknown"):
                contracts.ReceiverConfig.load(path)
            source.pop("surprise")
            path.write_text(json.dumps(source, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(contracts.DeploymentError, "canonical"):
                contracts.ReceiverConfig.load(path)

    def test_service_and_https_guards_are_strict(self):
        source = json.loads(
            (ROOT / "ops/deploy/receiver-config.example.json").read_text()
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receiver.json"
            source["worker_service"] = "bad service"
            write_canonical(path, source)
            with self.assertRaisesRegex(contracts.DeploymentError, "safe service"):
                contracts.ReceiverConfig.load(path)
            source["worker_service"] = "allianceauth_worker"
            source["smoke_checks"][0]["url"] = "http://auth.b-uh.com/"
            write_canonical(path, source)
            with self.assertRaisesRegex(contracts.DeploymentError, "HTTPS"):
                contracts.ReceiverConfig.load(path)


class RequestTests(unittest.TestCase):
    def test_request_identity_and_attempt_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "REQUEST.json"
            write_canonical(path, request_data())
            request = contracts.DeploymentRequest.load(path)
            self.assertEqual(request.attempt_id, "gh-123456789-1")

    def test_ref_version_and_unknown_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "REQUEST.json"
            write_canonical(path, request_data(release_ref="release/platform-v9.9.9"))
            with self.assertRaisesRegex(contracts.DeploymentError, "disagree"):
                contracts.DeploymentRequest.load(path)
            write_canonical(path, request_data(extra="no"))
            with self.assertRaisesRegex(contracts.DeploymentError, "unknown"):
                contracts.DeploymentRequest.load(path)


class ArchiveTests(unittest.TestCase):
    def valid_entries(self):
        return [
            ("REQUEST.json", b"{}\n", "file"),
            ("release", b"", "directory"),
            ("release/RELEASE.json", b"{}\n", "file"),
        ]

    def test_minimal_archive_extracts_without_tarfile_extract(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "payload"
            contracts.extract_archive(tar_bytes(self.valid_entries()), destination)
            self.assertEqual((destination / "REQUEST.json").read_bytes(), b"{}\n")
            self.assertEqual((destination / "release/RELEASE.json").read_bytes(), b"{}\n")

    def test_traversal_links_nested_paths_and_case_collisions_are_rejected(self):
        variants = {
            "traversal": [("../REQUEST.json", b"{}", "file")],
            "symlink": [("REQUEST.json", "target", "symlink")],
            "nested": [
                ("REQUEST.json", b"{}", "file"),
                ("release", b"", "directory"),
                ("release/nested/file", b"x", "file"),
            ],
            "collision": [
                ("REQUEST.json", b"{}", "file"),
                ("request.JSON", b"{}", "file"),
            ],
        }
        for name, entries in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(contracts.DeploymentError):
                    contracts.extract_archive(
                        tar_bytes(entries), Path(temporary) / "payload"
                    )

    def test_pax_headers_and_oversized_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(contracts.DeploymentError, "headers"):
                contracts.extract_archive(
                    tar_bytes(self.valid_entries(), pax=True),
                    Path(temporary) / "pax",
                )
            with mock.patch.object(contracts, "MAX_FILE_BYTES", 4):
                with self.assertRaisesRegex(contracts.DeploymentError, "unsafe size"):
                    contracts.extract_archive(
                        tar_bytes(
                            [
                                ("REQUEST.json", b"12345", "file"),
                                ("release", b"", "directory"),
                            ]
                        ),
                        Path(temporary) / "large",
                    )


class BundleTests(unittest.TestCase):
    def test_verified_bundle_binds_request_manifest_and_install_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release"
            release.mkdir()
            manifest = {
                "platform_id": "buh-allianceauth",
                "platform_version": "0.4.0",
            }
            manifest_bytes = contracts.canonical_json_bytes(manifest)
            (release / "RELEASE.json").write_bytes(manifest_bytes)
            write_canonical(release / "INSTALL_PLAN.json", {"wheels": []})
            request = request_data(
                manifest_sha256=contracts.sha256_file(release / "RELEASE.json")
            )
            write_canonical(root / "REQUEST.json", request)
            config = contracts.ReceiverConfig.load(
                ROOT / "ops/deploy/receiver-config.example.json"
            )
            with mock.patch(
                "ops.release.buh_release.verify_release_dir",
                return_value=manifest,
            ):
                bundle = contracts.load_validated_bundle(root, config)
            self.assertEqual(bundle.manifest, manifest)
            self.assertEqual(bundle.request.platform_version, "0.4.0")

            request["repository"] = "SomeoneElse/WrongRepo"
            write_canonical(root / "REQUEST.json", request)
            with self.assertRaisesRegex(contracts.DeploymentError, "wrong repository"):
                contracts.load_validated_bundle(root, config)


if __name__ == "__main__":
    unittest.main()
