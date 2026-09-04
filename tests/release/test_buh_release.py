from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ops" / "release"))

import buh_release as release  # noqa: E402


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def make_wheel(
    directory: Path,
    distribution: str,
    version: str,
    *,
    unsafe_member: str | None = None,
    corrupt_record: bool = False,
    requires_dist: tuple[str, ...] = (),
) -> Path:
    normalized = release.normalize_distribution(distribution).replace("-", "_")
    filename = f"{normalized}-{version}-py3-none-any.whl"
    path = directory / filename
    dist_info = f"{normalized}-{version}.dist-info"
    module = normalized.replace("aa_", "").replace("-", "_") or "example"
    files = {
        f"{module}/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {distribution}\n"
            f"Version: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in requires_dist)
            + "\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ).encode(),
    }
    if unsafe_member:
        files[unsafe_member] = b"unsafe"
    record_name = f"{dist_info}/RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, content in sorted(files.items()):
        digest = "sha256=bad" if corrupt_record and name.endswith("/__init__.py") else _record_hash(content)
        writer.writerow([name, digest, str(len(content))])
    writer.writerow([record_name, "", ""])
    files[record_name] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return path


def rewrite_checksums(directory: Path) -> None:
    payloads = sorted(
        path for path in directory.iterdir() if path.name != "SHA256SUMS"
    )
    (directory / "SHA256SUMS").write_text(
        "".join(
            f"{release.sha256_file(path)}  {path.name}\n" for path in payloads
        ),
        encoding="ascii",
    )


class RepoFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry = root / "ops/release/apps.toml"
        self.compatibility = root / "platform/compatibility.toml"
        self.changes = root / "changes"
        self.registry.parent.mkdir(parents=True)
        self.compatibility.parent.mkdir(parents=True)
        self.changes.mkdir(parents=True)
        (self.registry.parent / "buh_release.py").write_text("TOOL = 1\n", encoding="utf-8")
        self.registry.write_text(
            """schema_version = 1

[platform]
id = "test-platform"
initial_version = "1.2.3"
release_root = "releases/platform"
compatibility_file = "platform/compatibility.toml"
shared_build_inputs = []
platform_build_inputs = [
  "platform/build/**/*",
  ".github/workflows/source-*",
]

[[apps]]
id = "alpha"
path = "apps/alpha"
distribution = "aa-alpha"
import_name = "alpha"
django_app = "alpha"
version_file = "src/alpha/__init__.py"
dependencies = []
setup_commands = ["alpha_setup"]

[[apps]]
id = "beta"
path = "apps/beta"
distribution = "aa-beta"
import_name = "beta"
django_app = "beta"
version_file = "src/beta/__init__.py"
dependencies = ["alpha"]
setup_commands = ["beta_setup"]
""",
            encoding="utf-8",
        )
        self.compatibility.write_text(
            """schema_version = 1

[runtime]
python = "3.12"
allianceauth = "5.2.0"
mariadb = "11.8"
redis = "8"
""",
            encoding="utf-8",
        )
        platform_build = self.root / "platform/build"
        platform_build.mkdir(parents=True)
        (platform_build / "Dockerfile").write_text(
            "FROM python:3.12\n", encoding="utf-8"
        )
        workflows = self.root / ".github/workflows"
        workflows.mkdir(parents=True)
        (workflows / "source-ci.yml").write_text(
            "name: source-ci\n", encoding="utf-8"
        )
        (workflows / "deploy-legacy-v1.yml").write_text(
            "name: legacy\n", encoding="utf-8"
        )
        self.make_app("alpha", "1.0.0")
        self.make_app("beta", "2.3.4")

    def make_app(self, app_id: str, version: str) -> None:
        app_root = self.root / f"apps/{app_id}"
        module = app_root / f"src/{app_id}"
        module.mkdir(parents=True)
        (module / "__init__.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
        (module / "models.py").write_text(f"VALUE = {version!r}\n", encoding="utf-8")
        dependencies = (
            'dependencies = ["aa-alpha>=1,<2"]\n' if app_id == "beta" else ""
        )
        (app_root / "pyproject.toml").write_text(
            f"[project]\nname = \"aa-{app_id}\"\n"
            f"dynamic = [\"version\"]\n{dependencies}",
            encoding="utf-8",
        )

    def fragment(self, name: str, app_id: str, kind: str, summary: str = "Changed") -> Path:
        path = self.changes / f"{name}.toml"
        path.write_text(
            "schema_version = 1\n"
            f"summary = {summary!r}\n\n"
            "[[changes]]\n"
            f"app = {app_id!r}\n"
            f"kind = {kind!r}\n",
            encoding="utf-8",
        )
        return path

    def add_dependency(
        self,
        *,
        component: str = "vendor-lib",
        distribution: str = "vendor-lib",
        version: str = "4.5.6",
        git_blob_sha: str | None = "auto",
    ) -> Path:
        vendor = self.root / "vendor"
        vendor.mkdir(exist_ok=True)
        wheel = make_wheel(vendor, distribution, version)
        if git_blob_sha == "auto":
            git_blob_sha = release.git_blob_object_id(wheel, hex_length=40)
        stanza = (
            "\n[[dependencies]]\n"
            f"id = {component!r}\n"
            f"distribution = {distribution!r}\n"
            f"version = {version!r}\n"
            f"filename = {wheel.name!r}\n"
            f"sha256 = {release.sha256_file(wheel)!r}\n"
            f"source = {wheel.relative_to(self.root).as_posix()!r}\n"
        )
        if git_blob_sha is not None:
            stanza += f"git_blob_sha = {git_blob_sha!r}\n"
        with self.registry.open("a", encoding="utf-8") as stream:
            stream.write(stanza)
        return wheel

    def plan(
        self,
        *,
        previous: Path | None = None,
        commit: str = "a" * 40,
    ) -> dict:
        return release.create_plan(
            repo_root=self.root,
            registry_path=self.registry,
            compatibility_path=self.compatibility,
            changes_dir=self.changes,
            previous_manifest_path=previous,
            source_commit=commit,
            test_run="unit-test",
        )


class SemVerTests(unittest.TestCase):
    def test_rollovers_are_numeric(self) -> None:
        self.assertEqual(str(release.SemVer.parse("0.9.9").bump("minor")), "0.10.0")
        self.assertEqual(str(release.SemVer.parse("9.9.9").bump("major")), "10.0.0")
        self.assertEqual(str(release.SemVer.parse("1.2.9").bump("patch")), "1.2.10")

    def test_rejects_non_release_versions(self) -> None:
        for value in ("v1.2.3", "01.2.3", "1.2", "1.2.3-rc1", "1.2.3+build"):
            with self.subTest(value=value), self.assertRaises(release.ReleaseError):
                release.SemVer.parse(value)


class RegistryAndChangeTests(unittest.TestCase):
    def test_version_only_change_does_not_change_input_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            registry = release.load_registry(fixture.registry)
            app = registry.by_id["alpha"]
            before = release.app_input_digest(fixture.root, app, ())
            (fixture.root / app.path / app.version_file).write_text(
                '__version__ = "8.9.10"\n', encoding="utf-8"
            )
            after = release.app_input_digest(fixture.root, app, ())
            self.assertEqual(before, after)

    def test_change_fragment_is_strict_and_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            fixture.fragment("feature", "alpha", "feature", "New dashboard")
            changes = release.load_changes(fixture.changes, {"alpha", "beta"})
            self.assertEqual(changes[0].bump, "minor")
            self.assertEqual(changes[0].summary, "New dashboard")
            fixture.fragment("invalid", "unknown", "fix")
            with self.assertRaisesRegex(release.ReleaseError, "Unknown app"):
                release.load_changes(fixture.changes, {"alpha", "beta"})

    def test_simple_fragment_and_readme_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            (fixture.changes / "README.md").write_text(
                "Human instructions are not release intent.\n", encoding="utf-8"
            )
            (fixture.changes / "alpha-fix.toml").write_text(
                'app = "alpha"\nkind = "fix"\nsummary = "Correct totals"\n',
                encoding="utf-8",
            )
            changes = release.load_changes(fixture.changes, {"alpha", "beta"})
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].as_dict(), {
                "fragment": "alpha-fix.toml",
                "app_id": "alpha",
                "kind": "fix",
                "bump": "patch",
                "summary": "Correct totals",
            })

    def test_registry_rejects_dependency_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            text = fixture.registry.read_text(encoding="utf-8")
            text = text.replace(
                'id = "alpha"\npath', 'id = "alpha"\npath'
            ).replace("dependencies = []", 'dependencies = ["beta"]', 1)
            fixture.registry.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(release.ReleaseError, "cycle"):
                release.load_registry(fixture.registry)

    def test_registry_rejects_distribution_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            fixture.add_dependency(distribution="aa-alpha")
            with self.assertRaisesRegex(release.ReleaseError, "Duplicate release distribution"):
                release.load_registry(fixture.registry)

    def test_registry_rejects_git_internal_paths_and_globs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            original = fixture.registry.read_text(encoding="utf-8")
            cases = {
                "shared": original.replace(
                    "shared_build_inputs = []",
                    'shared_build_inputs = [".git/HEAD"]',
                ),
                "platform": original.replace(
                    '  "platform/build/**/*",',
                    '  ".git/**",',
                ),
            }
            for name, contents in cases.items():
                with self.subTest(name=name):
                    fixture.registry.write_text(contents, encoding="utf-8")
                    with self.assertRaisesRegex(release.ReleaseError, "Unsafe"):
                        release.load_registry(fixture.registry)


class PlanningTests(unittest.TestCase):
    def _bootstrap_release(self, fixture: RepoFixture) -> Path:
        plan = fixture.plan()
        wheels = fixture.root / "built"
        wheels.mkdir()
        make_wheel(wheels, "aa-alpha", "1.0.0")
        make_wheel(
            wheels, "aa-beta", "2.3.4", requires_dist=("aa-alpha>=1,<2",)
        )
        output = fixture.root / "release-1"
        release.assemble_release(
            plan=plan,
            wheel_dir=wheels,
            previous_release_dir=None,
            output_dir=output,
            repo_root=fixture.root,
        )
        return output

    def test_current_schema_router_matches_explicit_v1_planner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            routed = fixture.plan()
            explicit = release.create_plan_v1(
                repo_root=fixture.root,
                registry_path=fixture.registry,
                compatibility_path=fixture.compatibility,
                changes_dir=fixture.changes,
                previous_manifest_path=None,
                source_commit="a" * 40,
                test_run="unit-test",
            )
            self.assertEqual(routed, explicit)
            self.assertEqual(explicit["schema_version"], release.PLAN_SCHEMA_V1)

    def test_v1_planner_fails_closed_after_unimplemented_schema_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            with mock.patch.object(release, "SCHEMA_VERSION", 2):
                with self.assertRaisesRegex(
                    release.ReleaseError,
                    "requires a preserved compatibility implementation",
                ):
                    release.create_plan_v1(
                        repo_root=fixture.root,
                        registry_path=fixture.registry,
                        compatibility_path=fixture.compatibility,
                        changes_dir=fixture.changes,
                        previous_manifest_path=None,
                        source_commit="a" * 40,
                        test_run="unit-test",
                    )

    def test_bootstrap_preserves_current_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            plan = fixture.plan()
            self.assertTrue(plan["bootstrap"])
            self.assertEqual(plan["platform_version"], "1.2.3")
            self.assertEqual(
                {item["id"]: item["version"] for item in plan["apps"]},
                {"alpha": "1.0.0", "beta": "2.3.4"},
            )
            self.assertEqual({item["id"] for item in plan["build_matrix"]}, {"alpha", "beta"})

    def test_bootstrap_applies_owned_app_change_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            fixture.fragment("alpha-source-first", "alpha", "internal")
            fixture.fragment("beta-source-first", "beta", "internal")
            plan = fixture.plan()
            self.assertEqual(
                {item["id"]: item["version"] for item in plan["build_matrix"]},
                {"alpha": "1.0.1", "beta": "2.3.5"},
            )
            self.assertEqual(
                {item["id"]: item["bump"] for item in plan["apps"]},
                {"alpha": "patch", "beta": "patch"},
            )

    def test_changed_app_requires_intent_and_only_it_builds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous_dir = self._bootstrap_release(fixture)
            previous = previous_dir / "RELEASE.json"
            (fixture.root / "apps/alpha/src/alpha/models.py").write_text(
                "VALUE = 'changed'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(release.ReleaseError, "requires a change fragment"):
                fixture.plan(previous=previous, commit="b" * 40)
            fixture.fragment("alpha-fix", "alpha", "fix", "Correct alpha")
            plan = fixture.plan(previous=previous, commit="b" * 40)
            self.assertEqual(plan["platform_version"], "1.2.4")
            self.assertEqual(plan["build_matrix"], [{"id": "alpha", "path": "apps/alpha", "version": "1.0.1"}])
            self.assertEqual(plan["affected_tests"], ["alpha", "beta"])
            beta = next(item for item in plan["apps"] if item["id"] == "beta")
            self.assertFalse(beta["build"])

    def test_stale_fragment_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous = self._bootstrap_release(fixture) / "RELEASE.json"
            fixture.fragment("stale", "alpha", "fix")
            with self.assertRaisesRegex(release.ReleaseError, "stale change fragment"):
                fixture.plan(previous=previous, commit="b" * 40)

    def test_no_change_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous = self._bootstrap_release(fixture) / "RELEASE.json"
            plan = fixture.plan(previous=previous, commit="b" * 40)
            self.assertFalse(plan["release_required"])
            self.assertEqual(plan["platform_version"], "1.2.3")
            self.assertEqual(plan["build_matrix"], [])

    def test_tool_change_requires_platform_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous = self._bootstrap_release(fixture) / "RELEASE.json"
            (fixture.registry.parent / "buh_release.py").write_text("TOOL = 2\n", encoding="utf-8")
            with self.assertRaisesRegex(release.ReleaseError, "platform fragment"):
                fixture.plan(previous=previous, commit="b" * 40)
            fixture.fragment("tooling", "platform", "internal", "Harden release tool")
            plan = fixture.plan(previous=previous, commit="b" * 40)
            self.assertTrue(plan["release_required"])
            self.assertEqual(plan["build_matrix"], [])
            self.assertEqual(plan["platform_version"], "1.2.4")

    def test_declared_platform_input_requires_fragment_but_legacy_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous_dir = self._bootstrap_release(fixture)
            previous = previous_dir / "RELEASE.json"
            previous_manifest = json.loads(previous.read_text(encoding="utf-8"))

            legacy = fixture.root / ".github/workflows/deploy-legacy-v1.yml"
            legacy.write_text("name: legacy changed\n", encoding="utf-8")
            self.assertFalse(
                fixture.plan(previous=previous, commit="b" * 40)["release_required"]
            )

            dockerfile = fixture.root / "platform/build/Dockerfile"
            dockerfile.write_text("FROM python:3.12.11\n", encoding="utf-8")
            with self.assertRaisesRegex(release.ReleaseError, "platform fragment"):
                fixture.plan(previous=previous, commit="b" * 40)
            fixture.fragment(
                "test-runtime", "platform", "internal", "Update test runtime image"
            )
            plan = fixture.plan(previous=previous, commit="b" * 40)
            self.assertTrue(plan["release_required"])
            self.assertEqual(plan["platform_version"], "1.2.4")
            self.assertEqual(plan["build_matrix"], [])
            self.assertNotEqual(
                plan["build"]["platform_sha256"],
                previous_manifest["build"]["platform_sha256"],
            )

    def test_incompatible_dependent_is_rejected_until_its_range_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous = self._bootstrap_release(fixture) / "RELEASE.json"
            (fixture.root / "apps/alpha/src/alpha/models.py").write_text(
                "VALUE = 'breaking'\n", encoding="utf-8"
            )
            fixture.fragment("alpha-breaking", "alpha", "breaking", "Alpha 2 API")
            with self.assertRaisesRegex(
                release.ReleaseError,
                r"beta requires aa-alpha>=1,<2, but planned alpha is 2\.0\.0",
            ):
                fixture.plan(previous=previous, commit="b" * 40)

            beta_project = fixture.root / "apps/beta/pyproject.toml"
            beta_project.write_text(
                beta_project.read_text(encoding="utf-8").replace(
                    "aa-alpha>=1,<2", "aa-alpha>=1,<3"
                ),
                encoding="utf-8",
            )
            fixture.fragment(
                "beta-compatibility",
                "beta",
                "fix",
                "Declare compatibility with Alpha 2",
            )
            plan = fixture.plan(previous=previous, commit="b" * 40)
            self.assertEqual(plan["platform_version"], "2.0.0")
            self.assertEqual(
                {item["id"]: item["version"] for item in plan["build_matrix"]},
                {"alpha": "2.0.0", "beta": "2.3.5"},
            )

    def test_pinned_postrelease_dependency_constraint_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            fixture.add_dependency(version="4.5.6.post1")
            alpha_project = fixture.root / "apps/alpha/pyproject.toml"
            alpha_project.write_text(
                alpha_project.read_text(encoding="utf-8")
                + 'dependencies = ["vendor-lib==4.5.6.post1"]\n',
                encoding="utf-8",
            )
            self.assertEqual(fixture.plan()["dependency_artifacts"][0]["action"], "provide")

            registry_text = fixture.registry.read_text(encoding="utf-8").replace(
                "version = '4.5.6.post1'", "version = '4.5.6.post2'"
            )
            fixture.registry.write_text(registry_text, encoding="utf-8")
            with self.assertRaisesRegex(
                release.ReleaseError,
                "alpha requires vendor-lib==4.5.6.post1, but planned vendor-lib "
                "is 4.5.6.post2",
            ):
                fixture.plan()


class WheelTests(unittest.TestCase):
    def test_valid_wheel_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = make_wheel(Path(temp), "aa-example", "1.2.3")
            info = release.inspect_wheel(
                wheel, expected_distribution="aa-example", expected_version="1.2.3"
            )
            self.assertEqual(info.sha256, release.sha256_file(wheel))

    def test_rejects_bad_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = make_wheel(
                Path(temp), "aa-example", "1.2.3", corrupt_record=True
            )
            with self.assertRaisesRegex(release.ReleaseError, "RECORD verification"):
                release.inspect_wheel(wheel)

    def test_rejects_zip_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = make_wheel(
                Path(temp), "aa-example", "1.2.3", unsafe_member="../escape.py"
            )
            with self.assertRaisesRegex(release.ReleaseError, "Unsafe wheel member"):
                release.inspect_wheel(wheel)

    def test_git_blob_ids_are_recomputed_from_wheel_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = make_wheel(Path(temp), "aa-example", "1.2.3")
            for length in (40, 64):
                expected = release.git_blob_object_id(wheel, hex_length=length)
                release._verify_git_blob_object_id(wheel, expected)
                wrong = ("0" if expected[0] != "0" else "1") + expected[1:]
                with self.assertRaisesRegex(
                    release.ReleaseError, "does not match bytes"
                ):
                    release._verify_git_blob_object_id(wheel, wrong)


class AssemblyTests(unittest.TestCase):
    def _bootstrap(self, fixture: RepoFixture) -> Path:
        wheels = fixture.root / "wheels-1"
        wheels.mkdir()
        make_wheel(wheels, "aa-alpha", "1.0.0")
        make_wheel(
            wheels, "aa-beta", "2.3.4", requires_dist=("aa-alpha>=1,<2",)
        )
        output = fixture.root / "release-1"
        release.assemble_release(
            plan=fixture.plan(),
            wheel_dir=wheels,
            previous_release_dir=None,
            output_dir=output,
            repo_root=fixture.root,
        )
        return output

    def test_changed_wheel_build_and_unchanged_wheel_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            previous_dir = self._bootstrap(fixture)
            previous_manifest = json.loads(
                (previous_dir / "RELEASE.json").read_text(encoding="utf-8")
            )
            beta_prior = next(
                item for item in previous_manifest["artifacts"] if item["component"] == "beta"
            )
            beta_blob = release.git_blob_object_id(
                previous_dir / beta_prior["filename"], hex_length=40
            )
            beta_prior["git_blob_sha"] = beta_blob
            (previous_dir / "RELEASE.json").write_bytes(
                release._canonical_json_bytes(previous_manifest)
            )
            sums = sorted(
                path for path in previous_dir.iterdir() if path.name != "SHA256SUMS"
            )
            (previous_dir / "SHA256SUMS").write_text(
                "".join(f"{release.sha256_file(path)}  {path.name}\n" for path in sums),
                encoding="ascii",
            )
            release.verify_release_dir(previous_dir)

            (fixture.root / "apps/alpha/src/alpha/models.py").write_text(
                "VALUE = 'changed'\n", encoding="utf-8"
            )
            fixture.fragment("alpha-feature", "alpha", "feature", "Improve alpha")
            plan = fixture.plan(
                previous=previous_dir / "RELEASE.json", commit="b" * 40
            )
            wheels = fixture.root / "wheels-2"
            wheels.mkdir()
            make_wheel(wheels, "aa-alpha", "1.1.0")
            output = fixture.root / "release-2"
            release.assemble_release(
                plan=plan,
                wheel_dir=wheels,
                previous_release_dir=previous_dir,
                output_dir=output,
                repo_root=fixture.root,
            )
            manifest = release.verify_release_dir(output)
            origins = {item["component"]: item["origin"] for item in manifest["artifacts"]}
            self.assertEqual(origins, {"alpha": "built", "beta": "reused"})
            beta = next(item for item in manifest["artifacts"] if item["component"] == "beta")
            self.assertEqual(beta["git_blob_sha"], beta_blob)
            self.assertEqual(beta["sha256"], beta_prior["sha256"])

    def test_assembly_rejects_wheel_metadata_incompatible_with_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            wheels = fixture.root / "incompatible-wheels"
            wheels.mkdir()
            make_wheel(wheels, "aa-alpha", "1.0.0")
            make_wheel(
                wheels,
                "aa-beta",
                "2.3.4",
                requires_dist=("aa-alpha>=2,<3",),
            )
            with self.assertRaisesRegex(
                release.ReleaseError,
                r"beta requires aa-alpha>=2,<3, but planned alpha is 1\.0\.0",
            ):
                release.assemble_release(
                    plan=fixture.plan(),
                    wheel_dir=wheels,
                    previous_release_dir=None,
                    output_dir=fixture.root / "incompatible-release",
                    repo_root=fixture.root,
                )

    def test_assembly_rejects_republished_identity_with_different_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            history = fixture.root / "releases/legacy/v1"
            history.mkdir(parents=True)
            make_wheel(history, "aa-alpha", "1.0.0")
            wheels = fixture.root / "identity-collision-wheels"
            wheels.mkdir()
            make_wheel(
                wheels,
                "aa-alpha",
                "1.0.0",
                requires_dist=("requests>=2",),
            )
            make_wheel(
                wheels,
                "aa-beta",
                "2.3.4",
                requires_dist=("aa-alpha>=1,<2",),
            )
            with self.assertRaisesRegex(
                release.ReleaseError,
                r"Wheel identity aa-alpha 1\.0\.0 already exists with different bytes",
            ):
                release.assemble_release(
                    plan=fixture.plan(),
                    wheel_dir=wheels,
                    previous_release_dir=None,
                    output_dir=fixture.root / "identity-collision-release",
                    repo_root=fixture.root,
                )

    def test_third_party_dependency_is_provided_then_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            dependency_wheel = fixture.add_dependency()
            dependency_blob = release.git_blob_object_id(
                dependency_wheel, hex_length=40
            )
            first_plan = fixture.plan()
            self.assertEqual(
                first_plan["dependency_artifacts"],
                [
                    {
                        "component": "vendor-lib",
                        "kind": "third-party",
                        "distribution": "vendor-lib",
                        "version": "4.5.6",
                        "filename": dependency_wheel.name,
                        "sha256": release.sha256_file(dependency_wheel),
                        "source": "vendor/vendor_lib-4.5.6-py3-none-any.whl",
                        "action": "provide",
                        "git_blob_sha": dependency_blob,
                    }
                ],
            )
            first_wheels = fixture.root / "wheels-dependency-1"
            first_wheels.mkdir()
            make_wheel(first_wheels, "aa-alpha", "1.0.0")
            make_wheel(
                first_wheels,
                "aa-beta",
                "2.3.4",
                requires_dist=("aa-alpha>=1,<2",),
            )
            first_output = fixture.root / "release-dependency-1"
            release.assemble_release(
                plan=first_plan,
                wheel_dir=first_wheels,
                previous_release_dir=None,
                output_dir=first_output,
                repo_root=fixture.root,
            )
            first_manifest = release.verify_release_dir(first_output)
            provided = next(
                item
                for item in first_manifest["artifacts"]
                if item["component"] == "vendor-lib"
            )
            self.assertEqual(provided["origin"], "provided")
            self.assertEqual(provided["git_blob_sha"], dependency_blob)
            install_plan = json.loads(
                (first_output / "INSTALL_PLAN.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [wheel["component"] for wheel in install_plan["wheels"]],
                ["vendor-lib", "alpha", "beta"],
            )

            (fixture.root / "apps/alpha/src/alpha/models.py").write_text(
                "VALUE = 'changed again'\n", encoding="utf-8"
            )
            fixture.fragment("alpha-fix", "alpha", "fix", "Correct alpha")
            second_plan = fixture.plan(
                previous=first_output / "RELEASE.json", commit="b" * 40
            )
            self.assertEqual(second_plan["dependency_artifacts"][0]["action"], "reuse")
            second_wheels = fixture.root / "wheels-dependency-2"
            second_wheels.mkdir()
            make_wheel(second_wheels, "aa-alpha", "1.0.1")
            second_output = fixture.root / "release-dependency-2"
            release.assemble_release(
                plan=second_plan,
                wheel_dir=second_wheels,
                previous_release_dir=first_output,
                output_dir=second_output,
                repo_root=fixture.root,
            )
            second_manifest = release.verify_release_dir(second_output)
            reused = next(
                item
                for item in second_manifest["artifacts"]
                if item["component"] == "vendor-lib"
            )
            self.assertEqual(reused["origin"], "reused")
            self.assertEqual(reused["sha256"], provided["sha256"])
            self.assertEqual(reused["git_blob_sha"], dependency_blob)

    def test_refuses_to_overwrite_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            output = fixture.root / "existing"
            output.mkdir()
            with self.assertRaisesRegex(release.ReleaseError, "Refusing to overwrite"):
                release.assemble_release(
                    plan=fixture.plan(),
                    wheel_dir=fixture.root,
                    previous_release_dir=None,
                    output_dir=output,
                    repo_root=fixture.root,
                )

    def test_verifier_rejects_extra_or_modified_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            output = self._bootstrap(fixture)
            (output / "extra.txt").write_text("not declared", encoding="utf-8")
            with self.assertRaises(release.ReleaseError):
                release.verify_release_dir(output)
            (output / "extra.txt").unlink()
            wheel = next(output.glob("*.whl"))
            wheel.write_bytes(wheel.read_bytes() + b"tamper")
            with self.assertRaisesRegex(release.ReleaseError, "SHA-256 mismatch"):
                release.verify_release_dir(output)

    def test_verifier_rejects_internally_incompatible_wheel_set(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            output = self._bootstrap(fixture)
            replacement_dir = fixture.root / "replacement"
            replacement_dir.mkdir()
            replacement = make_wheel(
                replacement_dir,
                "aa-beta",
                "2.3.4",
                requires_dist=("aa-alpha>=2,<3",),
            )
            beta_path = output / replacement.name
            beta_path.write_bytes(replacement.read_bytes())
            beta_sha = release.sha256_file(beta_path)

            install_plan = json.loads(
                (output / "INSTALL_PLAN.json").read_text(encoding="utf-8")
            )
            beta_install = next(
                item for item in install_plan["wheels"] if item["component"] == "beta"
            )
            beta_install["sha256"] = beta_sha
            (output / "INSTALL_PLAN.json").write_bytes(
                release._canonical_json_bytes(install_plan)
            )

            manifest = json.loads(
                (output / "RELEASE.json").read_text(encoding="utf-8")
            )
            beta_artifact = next(
                item for item in manifest["artifacts"] if item["component"] == "beta"
            )
            beta_artifact["sha256"] = beta_sha
            beta_artifact["size"] = beta_path.stat().st_size
            manifest["install_plan"]["sha256"] = release.sha256_file(
                output / "INSTALL_PLAN.json"
            )
            (output / "RELEASE.json").write_bytes(
                release._canonical_json_bytes(manifest)
            )
            rewrite_checksums(output)

            with self.assertRaisesRegex(
                release.ReleaseError,
                r"beta requires aa-alpha>=2,<3, but planned alpha is 1\.0\.0",
            ):
                release.verify_release_dir(output)

    def test_isolated_install_validates_exact_distributions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            output = self._bootstrap(fixture)
            report = release.validate_isolated_install(output)
            self.assertEqual(report["platform_version"], "1.2.3")
            self.assertEqual(report["artifact_count"], 2)
            self.assertEqual(
                report["distributions"], {"aa-alpha": "1.0.0", "aa-beta": "2.3.4"}
            )

    def test_schema_validation_fails_closed_on_wrong_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            output = self._bootstrap(fixture)
            manifest = json.loads(
                (output / "RELEASE.json").read_text(encoding="utf-8")
            )
            manifest["compatibility"]["sha256"] = 123
            with self.assertRaisesRegex(release.ReleaseError, "compatibility digest"):
                release.validate_manifest_data(manifest)
            install_plan = json.loads(
                (output / "INSTALL_PLAN.json").read_text(encoding="utf-8")
            )
            install_plan["wheels"][0]["sha256"] = 123
            with self.assertRaisesRegex(release.ReleaseError, "wheel hash"):
                release.validate_install_plan_data(install_plan)


class BuildTests(unittest.TestCase):
    def test_build_uses_temp_version_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = RepoFixture(Path(temp))
            registry = release.load_registry(fixture.registry)
            app = registry.by_id["alpha"]
            plan = fixture.plan()
            alpha_plan = next(item for item in plan["apps"] if item["id"] == "alpha")
            alpha_plan["version"] = "1.0.1"
            output = fixture.root / "built"

            def fake_run(command, *, cwd, env, check, text):
                self.assertTrue(check)
                self.assertTrue(text)
                self.assertEqual(env["PYTHONHASHSEED"], "0")
                version_text = (Path(cwd) / app.version_file).read_text(encoding="utf-8")
                self.assertIn('"1.0.1"', version_text)
                outdir = Path(command[command.index("--outdir") + 1])
                make_wheel(outdir, "aa-alpha", "1.0.1")
                return mock.Mock(returncode=0)

            with mock.patch.object(release.subprocess, "run", side_effect=fake_run):
                wheel = release.build_app(
                    repo_root=fixture.root,
                    registry_path=fixture.registry,
                    plan=plan,
                    app_id="alpha",
                    output_dir=output,
                )
            self.assertEqual(wheel.name, "aa_alpha-1.0.1-py3-none-any.whl")
            original = (fixture.root / app.path / app.version_file).read_text(encoding="utf-8")
            self.assertIn('"1.0.0"', original)


class SchemaParityTests(unittest.TestCase):
    def test_safety_critical_schema_limits_match_authoritative_verifier(self) -> None:
        manifest_schema = json.loads(
            (PROJECT_ROOT / "ops/release/manifest.schema.json").read_text(
                encoding="utf-8"
            )
        )
        artifact_schema = manifest_schema["$defs"]["artifact"]["properties"]
        self.assertEqual(
            artifact_schema["size"]["maximum"], release.MAX_FILE_BYTES
        )
        self.assertEqual(
            manifest_schema["$defs"]["gitObject"]["pattern"],
            release.GIT_OBJECT_RE.pattern,
        )
        self.assertEqual(
            manifest_schema["$defs"]["sha256"]["pattern"],
            release.SHA256_RE.pattern,
        )
        self.assertIn(
            "platform_sha256",
            manifest_schema["properties"]["build"]["required"],
        )

    def test_schema_boundary_adversaries_fail_runtime_validation(self) -> None:
        artifact = {
            "component": "alpha",
            "kind": "owned",
            "distribution": "aa-alpha",
            "version": "1.0.0",
            "filename": "aa_alpha-1.0.0-py3-none-any.whl",
            "size": release.MAX_FILE_BYTES + 1,
            "sha256": "a" * 64,
            "input_sha256": "b" * 64,
            "import_name": "alpha",
            "origin": "built",
        }
        with self.assertRaisesRegex(release.ReleaseError, "artifact size"):
            release._validate_artifact_data(artifact, "adversarial artifact")
        artifact["size"] = 1
        artifact["git_blob_sha"] = "c" * 41
        with self.assertRaisesRegex(release.ReleaseError, "Git object"):
            release._validate_artifact_data(artifact, "adversarial artifact")


class AppendOnlyTests(unittest.TestCase):
    def test_accepts_only_new_release_additions(self) -> None:
        release.validate_append_only_changes(
            [
                ("M", "apps/moon-tax/src/buh_moon_tax/views.py"),
                ("A", "releases/platform/v0.4.0/RELEASE.json"),
                ("A", "releases/platform/v0.4.0/SHA256SUMS"),
            ],
            new_release_path="releases/platform/v0.4.0",
        )

    def test_rejects_prior_release_mutation(self) -> None:
        for status, path in (
            ("M", "releases/platform/v0.3.3/README.md"),
            ("D", "releases/platform/v0.3.3/old.whl"),
            ("A", "releases/platform/v0.3.3/extra.txt"),
        ):
            with self.subTest(status=status, path=path), self.assertRaises(
                release.ReleaseError
            ):
                release.validate_append_only_changes(
                    [
                        (status, path),
                        ("A", "releases/platform/v0.4.0/RELEASE.json"),
                    ],
                    new_release_path="releases/platform/v0.4.0",
                )


if __name__ == "__main__":
    unittest.main()
