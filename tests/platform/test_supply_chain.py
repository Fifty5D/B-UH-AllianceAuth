"""Focused regression tests for the source-test supply-chain verifier."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packaging.requirements import Requirement
from packaging.version import Version


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ops"))

import supply_chain as supply  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64


def _lock_entry(name: str, version: str, *hashes: str) -> str:
    lines = [f"{name}=={version} \\"]
    for index, digest in enumerate(hashes):
        continuation = " \\" if index < len(hashes) - 1 else ""
        lines.append(f"    --hash=sha256:{digest}{continuation}")
    return "\n".join(lines) + "\n"


def _locked(version: str) -> tuple[str, tuple[str, ...]]:
    return version, (SHA_A,)


class LockParsingTests(unittest.TestCase):
    def test_lock_entries_accept_canonical_hashed_pins_and_normalize_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requirements.lock"
            path.write_text(
                "# generated lock\n"
                + _lock_entry("Foo_Bar", "1.2.3", SHA_A, SHA_B)
                + _lock_entry("other.package", "4.5.6", SHA_B),
                encoding="utf-8",
            )

            self.assertEqual(
                supply._lock_entries(path),
                {
                    "foo-bar": ("1.2.3", (SHA_A, SHA_B)),
                    "other-package": ("4.5.6", (SHA_B,)),
                },
            )

    def test_lock_entries_reject_missing_hashes_and_normalized_duplicates(self):
        invalid_locks = {
            "missing hash": "demo==1.0\n",
            "short hash": _lock_entry("demo", "1.0", "a" * 63),
            "uppercase hash": _lock_entry("demo", "1.0", "A" * 64),
            "normalized duplicate": (
                _lock_entry("Foo_Bar", "1.0", SHA_A)
                + _lock_entry("foo-bar", "1.0", SHA_B)
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requirements.lock"
            for label, content in invalid_locks.items():
                with self.subTest(label=label):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(supply.SupplyChainError):
                        supply._lock_entries(path)

    def test_lock_entries_reject_ignored_options_and_adversarial_hash_lines(self):
        valid = _lock_entry("demo", "1.0", SHA_A)
        invalid_locks = {
            "leading indented option": (
                "    --index-url https://packages.invalid/simple\n" + valid
            ),
            "entry option": (
                valid.rstrip("\n")
                + " \\\n    --trusted-host packages.invalid\n"
            ),
            "text prefixed hash": (
                "demo==1.0 \\\n"
                f"    --trusted-host packages.invalid --hash=sha256:{SHA_A}\n"
            ),
            "included requirements file": (
                valid.rstrip("\n") + " \\\n    -r another.lock\n"
            ),
            "duplicate hash": _lock_entry("demo", "1.0", SHA_A, SHA_A),
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "requirements.lock"
            for label, content in invalid_locks.items():
                with self.subTest(label=label):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(supply.SupplyChainError):
                        supply._lock_entries(path)


class ImageReferenceTests(unittest.TestCase):
    def test_split_image_accepts_a_tag_and_lowercase_sha256_digest(self):
        references = {
            f"python:3.12.13-slim@sha256:{SHA_A}": (
                "python",
                "3.12.13-slim",
                f"sha256:{SHA_A}",
            ),
            f"mcr.microsoft.com/playwright:v1.55.0-noble@sha256:{SHA_B}": (
                "mcr.microsoft.com/playwright",
                "v1.55.0-noble",
                f"sha256:{SHA_B}",
            ),
            f"example/team-image:release_1@sha256:{SHA_A}": (
                "example/team-image",
                "release_1",
                f"sha256:{SHA_A}",
            ),
        }
        for reference, expected in references.items():
            with self.subTest(reference=reference):
                self.assertEqual(supply._split_image(reference), expected)

    def test_split_image_rejects_mutable_or_malformed_references(self):
        invalid = {
            "tag only": "python:3.12.13",
            "digest only": f"python@sha256:{SHA_A}",
            "latest": f"python:latest@sha256:{SHA_A}",
            "case variant latest": f"python:LATEST@sha256:{SHA_A}",
            "short digest": f"python:3.12@sha256:{'a' * 63}",
            "uppercase digest": f"python:3.12@sha256:{'A' * 64}",
            "non-hex digest": f"python:3.12@sha256:{'g' * 64}",
            "whitespace in tag": f"python:bad tag@sha256:{SHA_A}",
            "URL scheme": f"https://registry.example/image:v1@sha256:{SHA_A}",
            "unapproved registry": (
                f"registry.example:5000/team/image:v1@sha256:{SHA_A}"
            ),
            "empty path component": f"registry.example/team//image:v1@sha256:{SHA_A}",
            "slash in tag": f"python:bad/tag@sha256:{SHA_A}",
            "extra at sign": f"python:v1@@sha256:{SHA_A}",
            "overlong tag": f"python:{'a' * 129}@sha256:{SHA_A}",
        }
        for label, reference in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(supply.SupplyChainError):
                    supply._split_image(reference)


class RegistryRequestTests(unittest.TestCase):
    def test_request_rejects_non_https_or_unapproved_host_before_opening(self):
        invalid_urls = (
            "http://auth.docker.io/token",
            "https://registry.example.invalid/v2/image/manifests/tag",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                request = supply.urllib.request.Request(url)
                with mock.patch.object(
                    supply.urllib.request, "build_opener"
                ) as build_opener:
                    with self.assertRaises(supply.SupplyChainError):
                        supply._request(request)
                    build_opener.assert_not_called()

    def test_request_rejects_final_response_url_outside_registry_allowlist(self):
        escaped_urls = (
            "https://registry.example.invalid/v2/image/manifests/tag",
            "http://auth.docker.io/token",
        )
        for final_url in escaped_urls:
            with self.subTest(final_url=final_url):
                request = supply.urllib.request.Request(
                    "https://auth.docker.io/token"
                )
                response = mock.Mock()
                response.geturl.return_value = final_url
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.object(
                    supply.urllib.request,
                    "build_opener",
                    return_value=opener,
                ):
                    with self.assertRaises(supply.SupplyChainError):
                        supply._request(request)
                opener.open.assert_called_once_with(request, timeout=30)
                response.close.assert_called_once_with()

    def test_request_accepts_https_final_url_on_registry_allowlist(self):
        for host in sorted(supply.REGISTRY_HOSTS):
            with self.subTest(host=host):
                request = supply.urllib.request.Request(f"https://{host}/resource")
                response = mock.Mock()
                response.geturl.return_value = f"https://{host}/resource"
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.object(
                    supply.urllib.request,
                    "build_opener",
                    return_value=opener,
                ):
                    self.assertIs(supply._request(request), response)
                response.close.assert_not_called()


class DependencyContractTests(unittest.TestCase):
    contract = {"supply_chain": {"lock_python": "3.12.13"}}

    def _verify(
        self,
        *,
        app_requirements: list[Requirement],
        bundled_requirements: list[Requirement],
        lock: dict[str, tuple[str, tuple[str, ...]]],
        owned: dict[str, Version] | None = None,
        bundled: dict[str, Version] | None = None,
    ) -> None:
        with (
            mock.patch.object(
                supply,
                "_owned_requirements",
                return_value=(owned or {}, app_requirements),
            ),
            mock.patch.object(
                supply,
                "_bundled_contract",
                return_value=(bundled or {}, bundled_requirements),
            ),
        ):
            supply._verify_dependency_contract(self.contract, lock)

    def test_dependency_contract_covers_app_and_bundled_wheel_requirements(self):
        self._verify(
            owned={"aa-owned": Version("1.2.0")},
            bundled={"vendor-wheel": Version("4.0")},
            app_requirements=[
                Requirement("aa-owned>=1,<2"),
                Requirement("external_app>=2,<3"),
                Requirement("vendor-wheel==4.0"),
            ],
            bundled_requirements=[
                Requirement("wheel-runtime~=5.1"),
                Requirement('test-only>=1; extra == "test"'),
            ],
            lock={
                "external-app": _locked("2.5.0"),
                "wheel-runtime": _locked("5.1.4"),
            },
        )

    def test_dependency_contract_rejects_missing_or_wrong_app_dependency(self):
        app_requirements = [Requirement("external-app>=2,<3")]
        invalid_locks = {
            "missing": {},
            "wrong version": {"external-app": _locked("3.0.0")},
        }
        for label, lock in invalid_locks.items():
            with self.subTest(label=label):
                with self.assertRaises(supply.SupplyChainError):
                    self._verify(
                        app_requirements=app_requirements,
                        bundled_requirements=[],
                        lock=lock,
                    )

    def test_dependency_contract_rejects_missing_or_wrong_wheel_dependency(self):
        bundled_requirements = [Requirement("wheel-runtime>=5,<6")]
        invalid_locks = {
            "missing": {},
            "wrong version": {"wheel-runtime": _locked("6.0.0")},
        }
        for label, lock in invalid_locks.items():
            with self.subTest(label=label):
                with self.assertRaises(supply.SupplyChainError):
                    self._verify(
                        app_requirements=[],
                        bundled_requirements=bundled_requirements,
                        lock=lock,
                    )

    def test_dependency_contract_evaluates_markers_for_the_lock_target(self):
        app_requirements = [
            Requirement('linux-runtime>=1; sys_platform == "linux"'),
            Requirement('windows-runtime>=1; sys_platform == "win32"'),
            Requirement('future-runtime>=1; python_version >= "3.13"'),
        ]
        bundled_requirements = [
            Requirement('wheel-test-helper>=1; extra == "test"')
        ]

        self._verify(
            app_requirements=app_requirements,
            bundled_requirements=bundled_requirements,
            lock={"linux-runtime": _locked("1.0")},
        )
        with self.assertRaises(supply.SupplyChainError):
            self._verify(
                app_requirements=app_requirements,
                bundled_requirements=bundled_requirements,
                lock={},
            )


class BuildSystemTests(unittest.TestCase):
    build_input = "pip==26.2.1\nsetuptools==84.0.0\nwheel==0.48.0\n"
    build_lock = {
        "packaging": _locked("26.3"),
        "pip": _locked("26.2.1"),
        "setuptools": _locked("84.0.0"),
        "wheel": _locked("0.48.0"),
    }
    contract = {"applications": {"example": {"path": "apps/example"}}}

    def _write_fixture(self, root: Path, requires: list[str]) -> Path:
        input_path = root / "platform" / "requirements" / "build.txt"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(self.build_input, encoding="utf-8")
        pyproject = root / "apps" / "example" / "pyproject.toml"
        pyproject.parent.mkdir(parents=True, exist_ok=True)
        encoded = ", ".join(json.dumps(item) for item in requires)
        pyproject.write_text(
            "[build-system]\n"
            f"requires = [{encoded}]\n"
            'build-backend = "setuptools.build_meta"\n',
            encoding="utf-8",
        )
        return input_path

    def _verify(self, root: Path, input_path: Path, lock=None) -> None:
        inputs = {**supply.INPUTS, "build": input_path}
        with (
            mock.patch.object(supply, "ROOT", root),
            mock.patch.object(supply, "INPUTS", inputs),
        ):
            supply._verify_build_systems(
                self.contract, self.build_lock if lock is None else lock
            )

    def test_build_systems_accept_exact_pins_matching_the_build_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = self._write_fixture(
                root, ["setuptools==84.0.0", "wheel==0.48.0"]
            )
            self._verify(root, input_path)

    def test_build_systems_reject_missing_or_changed_build_lock_pins(self):
        invalid_locks = {
            "missing": {
                name: value for name, value in self.build_lock.items() if name != "wheel"
            },
            "changed": {**self.build_lock, "setuptools": _locked("83.0.0")},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = self._write_fixture(
                root, ["setuptools==84.0.0", "wheel==0.48.0"]
            )
            for label, lock in invalid_locks.items():
                with self.subTest(label=label):
                    with self.assertRaises(supply.SupplyChainError):
                        self._verify(root, input_path, lock)

    def test_build_systems_reject_non_exact_or_ambiguous_requirements(self):
        invalid_requirements = {
            "range": ["setuptools>=84.0.0", "wheel==0.48.0"],
            "wrong pin": ["setuptools==83.0.0", "wheel==0.48.0"],
            "missing": ["setuptools==84.0.0"],
            "extra": [
                "setuptools==84.0.0",
                "wheel==0.48.0",
                "build==1.3.0",
            ],
            "marker": [
                'setuptools==84.0.0; python_version >= "3.12"',
                "wheel==0.48.0",
            ],
            "extras": ["setuptools[core]==84.0.0", "wheel==0.48.0"],
            "normalized duplicate": [
                "setuptools==84.0.0",
                "Setuptools==84.0.0",
                "wheel==0.48.0",
            ],
            "direct reference": [
                "setuptools @ https://packages.invalid/setuptools.whl",
                "wheel==0.48.0",
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, requires in invalid_requirements.items():
                with self.subTest(label=label):
                    input_path = self._write_fixture(root, requires)
                    with self.assertRaises(supply.SupplyChainError):
                        self._verify(root, input_path)


class LockCompilationTests(unittest.TestCase):
    def test_compile_uses_hashes_binary_only_and_named_sdist_exceptions(self):
        contract = {
            "supply_chain": {
                "lock_python": "3.12.13",
                "lock_platform": "x86_64-manylinux_2_36",
                "allowed_source_distributions": [
                    "celery-once",
                    "django-bootstrap-form",
                    "mysqlclient",
                ],
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            source = temp / "production.txt"
            constraints = temp / "owned-and-bundled.txt"
            output = temp / "production.lock"
            observed: dict[str, object] = {}

            def fake_run(command, **kwargs):
                observed["command"] = command
                observed["kwargs"] = kwargs
                output.write_bytes(b"generated lock\n")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(supply.subprocess, "run", side_effect=fake_run):
                result = supply._compile_lock(
                    contract=contract,
                    sources=(source,),
                    constraints=constraints,
                    output=output,
                )

            self.assertEqual(result, b"generated lock\n")
            command = observed["command"]
            self.assertEqual(command[:4], [sys.executable, "-m", "uv", "pip"])
            self.assertEqual(command[4], "compile")
            self.assertIn(str(source), command)
            self.assertIn(str(constraints), command)
            self.assertIn("--generate-hashes", command)
            self.assertEqual(command[command.index("--only-binary") + 1], ":all:")
            self.assertEqual(
                [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "--no-binary"
                ],
                contract["supply_chain"]["allowed_source_distributions"],
            )
            self.assertEqual(
                command[command.index("--python-version") + 1], "3.12.13"
            )
            self.assertEqual(
                command[command.index("--python-platform") + 1],
                "x86_64-manylinux_2_36",
            )
            self.assertEqual(
                observed["kwargs"],
                {"cwd": supply.ROOT, "check": True, "text": True},
            )

    def test_compile_rejects_wildcard_or_malformed_sdist_exceptions(self):
        invalid = (":all:", "*", "one,two", "--no-index")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "lock.txt"

            def fake_run(command, **kwargs):
                output.write_bytes(b"generated lock\n")
                return subprocess.CompletedProcess(command, 0)

            for distribution in invalid:
                with self.subTest(distribution=distribution):
                    contract = {
                        "supply_chain": {
                            "lock_python": "3.12.13",
                            "lock_platform": "x86_64-manylinux_2_36",
                            "allowed_source_distributions": [distribution],
                        }
                    }
                    with mock.patch.object(
                        supply.subprocess, "run", side_effect=fake_run
                    ) as run:
                        with self.assertRaises(supply.SupplyChainError):
                            supply._compile_lock(
                                contract=contract,
                                sources=(Path("requirements.txt"),),
                                output=output,
                            )
                        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
