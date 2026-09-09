from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "ops" / "release"))

import ledger  # noqa: E402
import buh_release  # noqa: E402


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _make_wheel(
    directory: Path,
    distribution: str,
    version: str,
    *,
    requires_dist: tuple[str, ...] = (),
) -> Path:
    normalized = buh_release.normalize_distribution(distribution).replace("-", "_")
    filename = f"{normalized}-{version}-py3-none-any.whl"
    path = directory / filename
    dist_info = f"{normalized}-{version}.dist-info"
    module = normalized.replace("test_", "") or "example"
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
            "Generator: ledger-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ).encode(),
    }
    record_name = f"{dist_info}/RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, content in sorted(files.items()):
        writer.writerow([name, _record_hash(content), str(len(content))])
    writer.writerow([record_name, "", ""])
    files[record_name] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return path


def _read_test_manifest(path: Path) -> dict:
    return json.loads((path / "RELEASE.json").read_text(encoding="utf-8"))


def _dummy_change(fragment: str, summary: str = "Correct dummy behavior") -> dict:
    return {
        "fragment": fragment,
        "app_id": "dummy",
        "kind": "fix",
        "bump": "patch",
        "summary": summary,
    }


def _platform_feature(fragment: str = "platform-feature.toml") -> dict:
    return {
        "fragment": fragment,
        "app_id": "platform",
        "kind": "feature",
        "bump": "minor",
        "summary": "Update release platform",
    }


def _app_change(
    app_id: str,
    fragment: str,
    *,
    kind: str = "feature",
    bump: str = "minor",
    summary: str = "Update application",
) -> dict:
    return {
        "fragment": fragment,
        "app_id": app_id,
        "kind": kind,
        "bump": bump,
        "summary": summary,
    }


class GitLedgerFixture:
    def __init__(
        self,
        root: Path,
        *,
        initial_version: str = "1.0.0",
        platform_id: str = "test-platform",
    ) -> None:
        self.root = root
        self.repo = root / "repo"
        self.remote = root / "remote.git"
        self.platform_id = platform_id
        self.manifests: dict[str, dict] = {}
        self.git("init", "--bare", str(self.remote), cwd=root)
        self.git("init", "-b", "main", str(self.repo), cwd=root)
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release@example.invalid")
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "README.md").write_text("source\n", encoding="utf-8")
        (self.repo / "platform.txt").write_text("platform source\n", encoding="utf-8")
        registry = self.repo / "ops/release/apps.toml"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            "schema_version = 1\n\n"
            "[platform]\n"
            f"id = {platform_id!r}\n"
            f"initial_version = {initial_version!r}\n"
            'release_root = "releases/platform"\n'
            'compatibility_file = "platform/compatibility.toml"\n'
            'platform_build_inputs = ["*.txt"]\n\n'
            "[[apps]]\n"
            'id = "dummy"\n'
            'path = "apps/dummy"\n'
            'distribution = "test-dummy"\n'
            'import_name = "dummy"\n'
            'django_app = "dummy"\n'
            'version_file = "src/dummy/__init__.py"\n',
            encoding="utf-8",
        )
        version_file = self.repo / "apps/dummy/src/dummy/__init__.py"
        version_file.parent.mkdir(parents=True)
        version_file.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        pyproject = self.repo / "apps/dummy/pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test-dummy"\nversion = "0.1.0"\n'
            "dependencies = []\n",
            encoding="utf-8",
        )
        compatibility = self.repo / "platform/compatibility.toml"
        compatibility.parent.mkdir(parents=True)
        compatibility.write_text(
            "schema_version = 1\n\n"
            "[runtime]\n"
            'python = ">=3.11"\n'
            'allianceauth = ">=4"\n'
            'mariadb = ">=10"\n'
            'redis = ">=7"\n',
            encoding="utf-8",
        )
        self.git(
            "add",
            "README.md",
            "platform.txt",
            "ops/release/apps.toml",
            "apps/dummy/pyproject.toml",
            "apps/dummy/src/dummy/__init__.py",
            "platform/compatibility.toml",
        )
        self.git("commit", "-m", "source baseline")
        self.git("push", "-u", "origin", "main")

    def git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.repo,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        if check and result.returncode != 0:
            raise AssertionError(
                f"git {' '.join(arguments)} failed ({result.returncode}): "
                f"{result.stderr}"
            )
        return result.stdout.strip()

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    @property
    def environment(self) -> dict[str, str]:
        return {**os.environ, "GITHUB_TOKEN": "unit-test-token-never-print"}

    def source_change(self, name: str) -> str:
        (self.repo / name).write_text(f"{name}\n", encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", f"source {name}")
        return self.head

    def register_second_app(
        self,
        *,
        distribution: str = "test-second",
        version: str = "0.5.0",
        registry_dependencies: tuple[str, ...] = (),
        project_dependencies: tuple[str, ...] = (),
    ) -> None:
        registry = self.repo / "ops/release/apps.toml"
        dependencies = ", ".join(
            json.dumps(dependency) for dependency in registry_dependencies
        )
        registry.write_text(
            registry.read_text(encoding="utf-8")
            + "\n[[apps]]\n"
            + 'id = "second"\n'
            + 'path = "apps/second"\n'
            + f"distribution = {json.dumps(distribution)}\n"
            + 'import_name = "second"\n'
            + 'django_app = "second"\n'
            + 'version_file = "src/second/__init__.py"\n'
            + f"dependencies = [{dependencies}]\n",
            encoding="utf-8",
        )
        version_file = self.repo / "apps/second/src/second/__init__.py"
        version_file.parent.mkdir(parents=True)
        version_file.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        pyproject = self.repo / "apps/second/pyproject.toml"
        requirements = ", ".join(
            json.dumps(requirement) for requirement in project_dependencies
        )
        pyproject.write_text(
            "[project]\n"
            f"name = {json.dumps(distribution)}\n"
            f"version = {json.dumps(version)}\n"
            f"dependencies = [{requirements}]\n",
            encoding="utf-8",
        )
        self.git(
            "add",
            "ops/release/apps.toml",
            "apps/second/pyproject.toml",
            "apps/second/src/second/__init__.py",
        )
        self.git("commit", "-m", "register second application")

    def register_dependency(
        self,
        *,
        component: str,
        distribution: str,
        version: str,
        source: str,
        filename: str,
        sha256: str,
        git_blob_sha: str | None = None,
    ) -> None:
        registry = self.repo / "ops/release/apps.toml"
        registry.write_text(
            registry.read_text(encoding="utf-8")
            + "\n[[dependencies]]\n"
            + f"id = {json.dumps(component)}\n"
            + f"distribution = {json.dumps(distribution)}\n"
            + f"version = {json.dumps(version)}\n"
            + f"filename = {json.dumps(filename)}\n"
            + f"sha256 = {json.dumps(sha256)}\n"
            + f"source = {json.dumps(source)}\n"
            + (
                f"git_blob_sha = {json.dumps(git_blob_sha)}\n"
                if git_blob_sha is not None
                else ""
            ),
            encoding="utf-8",
        )
        self.git("add", "ops/release/apps.toml", source)
        self.git("commit", "-m", f"register dependency {component}")

    def source_plan_inputs(
        self, source: str
    ) -> tuple[object, dict, dict[str, str], dict]:
        with ledger._materialized_source_commit(
            self.repo, source, "releases/platform"
        ) as (snapshot, _, _):
            registry = buh_release.load_registry(snapshot / "ops/release/apps.toml")
            compatibility, compatibility_sha256 = buh_release.load_compatibility(
                snapshot / registry.compatibility_file
            )
            build = {
                "registry_sha256": registry.sha256,
                "compatibility_sha256": compatibility_sha256,
                "tool_sha256": buh_release.tool_input_digest(
                    snapshot, snapshot / "ops/release/apps.toml"
                ),
                "platform_sha256": buh_release.platform_input_digest(
                    snapshot, registry.platform_build_inputs
                ),
            }
            app_inputs = {
                app.app_id: buh_release.app_input_digest(
                    snapshot, app, registry.shared_build_inputs
                )
                for app in registry.apps
            }
        return registry, build, app_inputs, compatibility

    def add_release(
        self,
        version: str,
        *,
        previous: dict | None,
        artifact_previous: dict | None = None,
        manifest_source: str | None = None,
        platform_id: str | None = None,
        app_version: str = "0.1.0",
        app_versions: dict[str, str] | None = None,
        wheel_requirements: dict[str, tuple[str, ...]] | None = None,
        dependency_release_sources: dict[str, Path] | None = None,
        changes: list[dict] | None = None,
        sync_main: bool = True,
    ) -> tuple[str, dict]:
        source = self.head
        release_dir = self.repo / "releases" / "platform" / f"v{version}"
        release_dir.mkdir(parents=True)
        release_changes = sorted(
            (dict(change) for change in (changes or [])),
            key=lambda change: (change["fragment"], change["app_id"]),
        )
        registry, build, app_inputs, compatibility = self.source_plan_inputs(source)
        compatibility_sha256 = build["compatibility_sha256"]
        versions = app_versions or {"dummy": app_version}
        reuse_predecessor = (
            artifact_previous if artifact_previous is not None else previous
        )
        previous_manifest = (
            self.manifests.get(reuse_predecessor["platform_version"])
            if reuse_predecessor is not None
            else None
        )
        previous_artifacts = {
            artifact["component"]: artifact
            for artifact in (previous_manifest or {}).get("artifacts", [])
        }
        artifacts = []
        for component, owned_version in sorted(versions.items()):
            app = registry.by_id[component]
            input_sha256 = app_inputs[component]
            prior = previous_artifacts.get(component)
            changed = prior is None or prior.get("input_sha256") != input_sha256
            if changed:
                requirements = (
                    wheel_requirements[component]
                    if wheel_requirements is not None
                    and component in wheel_requirements
                    else tuple(
                        f"{registry.by_id[dependency].distribution}=="
                        f"{versions[dependency]}"
                        for dependency in app.dependencies
                    )
                )
                wheel = _make_wheel(
                    release_dir,
                    app.distribution,
                    owned_version,
                    requires_dist=requirements,
                )
                artifact = {
                    "component": component,
                    "kind": "owned",
                    "distribution": app.distribution,
                    "version": owned_version,
                    "filename": wheel.name,
                    "size": wheel.stat().st_size,
                    "sha256": buh_release.sha256_file(wheel),
                    "input_sha256": input_sha256,
                    "import_name": app.import_name,
                    "origin": "built",
                }
            else:
                artifact = dict(prior)
                source_wheel = (
                    release_dir.parent
                    / f'v{reuse_predecessor["platform_version"]}'
                    / artifact["filename"]
                )
                shutil.copyfile(source_wheel, release_dir / artifact["filename"])
                artifact["origin"] = "reused"
                artifact["reused_from"] = reuse_predecessor["platform_version"]
            artifacts.append(artifact)

        for dependency in registry.dependencies:
            prior = previous_artifacts.get(dependency.component)
            reusable = bool(
                prior
                and prior.get("kind") == "third-party"
                and buh_release.normalize_distribution(prior.get("distribution", ""))
                == buh_release.normalize_distribution(dependency.distribution)
                and prior.get("version") == dependency.version
                and prior.get("filename") == dependency.filename
                and prior.get("sha256") == dependency.sha256
            )
            if reusable:
                artifact = dict(prior)
                source_wheel = (
                    release_dir.parent
                    / f'v{reuse_predecessor["platform_version"]}'
                    / artifact["filename"]
                )
                shutil.copyfile(source_wheel, release_dir / artifact["filename"])
                artifact["origin"] = "reused"
                artifact["reused_from"] = reuse_predecessor["platform_version"]
            else:
                source_wheel = (
                    dependency_release_sources[dependency.component]
                    if dependency_release_sources is not None
                    and dependency.component in dependency_release_sources
                    else self.repo / dependency.source
                )
                destination = release_dir / dependency.filename
                shutil.copyfile(source_wheel, destination)
                artifact = {
                    "component": dependency.component,
                    "kind": "third-party",
                    "distribution": dependency.distribution,
                    "version": dependency.version,
                    "filename": dependency.filename,
                    "size": destination.stat().st_size,
                    "sha256": buh_release.sha256_file(destination),
                    "origin": "provided",
                }
                if dependency.git_blob_sha:
                    artifact["git_blob_sha"] = dependency.git_blob_sha
            artifacts.append(artifact)
        ordered_apps = ledger._ordered_registry_apps(registry)
        by_component = {
            artifact["component"]: artifact for artifact in artifacts
        }
        install_artifacts = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact["kind"] == "third-party"
            ),
            key=lambda artifact: (
                buh_release.normalize_distribution(artifact["distribution"]),
                artifact["version"],
            ),
        ) + [by_component[app.app_id] for app in ordered_apps]
        install_plan = {
            "schema_version": 1,
            "platform_version": version,
            "compatibility_sha256": compatibility_sha256,
            "wheels": [
                {
                    key: artifact[key]
                    for key in (
                        "component",
                        "distribution",
                        "version",
                        "filename",
                        "sha256",
                    )
                }
                for artifact in install_artifacts
            ],
            "django_apps": [app.django_app for app in ordered_apps],
            "setup_commands": [
                command for app in ordered_apps for command in app.setup_commands
            ],
        }
        install_path = release_dir / "INSTALL_PLAN.json"
        install_path.write_bytes(_canonical(install_plan))
        (release_dir / "README.md").write_bytes(
            buh_release.render_release_readme_v1(
                platform_version=version,
                source_commit=manifest_source or source,
                changes=release_changes,
                artifacts=artifacts,
            )
        )
        manifest = {
            "schema_version": 1,
            "platform_id": platform_id or self.platform_id,
            "platform_version": version,
            "source_commit": manifest_source or source,
            "previous_release": previous,
            "artifacts": sorted(artifacts, key=lambda artifact: artifact["component"]),
            "changes": release_changes,
            "build": {**build, "test_run": None},
            "compatibility": {
                "sha256": compatibility_sha256,
                "values": compatibility,
            },
            "install_plan": {
                "filename": "INSTALL_PLAN.json",
                "sha256": hashlib.sha256(install_path.read_bytes()).hexdigest(),
            },
        }
        (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
        payloads = sorted(release_dir.iterdir(), key=lambda path: path.name)
        (release_dir / "SHA256SUMS").write_text(
            "".join(
                f"{buh_release.sha256_file(path)}  {path.name}\n"
                for path in payloads
            ),
            encoding="ascii",
        )
        self.git("add", release_dir.relative_to(self.repo).as_posix())
        self.git("commit", "-m", f"release platform v{version}")
        release_commit = self.head
        self.git(
            "push",
            "origin",
            f"HEAD:refs/heads/release/platform-v{version}",
        )
        if sync_main:
            self.git("push", "origin", "HEAD:main")
        self.manifests[version] = manifest
        return release_commit, manifest

    def stage_platform_feature(self) -> dict:
        change = _platform_feature()
        fragment = self.repo / "changes" / change["fragment"]
        fragment.parent.mkdir(exist_ok=True)
        fragment.write_text(
            'app = "platform"\nkind = "feature"\n'
            'summary = "Update release platform"\n',
            encoding="utf-8",
        )
        self.git("add", fragment.relative_to(self.repo).as_posix())
        self.git("commit", "-m", "declare platform feature")
        fragment.unlink()
        self.git("add", "-A", fragment.relative_to(self.repo).as_posix())
        return change

    def stage_change(
        self,
        change: dict,
        *,
        commit_message: str | None = None,
    ) -> None:
        self.stage_changes(
            [change],
            commit_message=commit_message
            or f'declare {change["app_id"]} {change["kind"]}',
        )

    def stage_changes(
        self,
        changes: list[dict],
        *,
        commit_message: str = "declare release changes",
    ) -> None:
        for change in changes:
            fragment = self.repo / "changes" / change["fragment"]
            fragment.parent.mkdir(exist_ok=True)
            fragment.write_text(
                f'app = "{change["app_id"]}"\n'
                f'kind = "{change["kind"]}"\n'
                f'summary = "{change["summary"]}"\n',
                encoding="utf-8",
            )
        self.git("add", "changes")
        self.git("commit", "-m", commit_message)
        for change in changes:
            (self.repo / "changes" / change["fragment"]).unlink()
        self.git("add", "-A", "changes")

    def republish_amended_release(self, version: str) -> None:
        self.git("add", f"releases/platform/v{version}")
        self.git("commit", "--amend", "--no-edit")
        self.git(
            "push",
            "--force",
            "origin",
            f"HEAD:refs/heads/release/platform-v{version}",
        )
        self.git("push", "--force", "origin", "HEAD:main")
        self.manifests[version] = _read_test_manifest(
            self.repo / "releases/platform" / f"v{version}"
        )

    def predecessor(self, version: str, manifest: dict) -> dict:
        release_dir = self.repo / "releases" / "platform" / f"v{version}"
        return {
            "platform_version": manifest["platform_version"],
            "source_commit": manifest["source_commit"],
            "manifest_sha256": ledger.sha256_file(release_dir / "RELEASE.json"),
        }

    def verify(self, *, target_version: str | None = None) -> dict:
        with mock.patch.object(
            ledger, "verify_release_dir", side_effect=_read_test_manifest
        ):
            return ledger.verify_ledger(
                self.repo,
                self.head,
                target_version=target_version,
                environ=self.environment,
            )


class ReleaseLedgerTests(unittest.TestCase):
    def test_planner_path_anchors_cover_configured_input_roots(self) -> None:
        registry = mock.Mock(
            compatibility_file="configuration/compatibility.toml",
            shared_build_inputs=("shared/build.json",),
            dependencies=(mock.Mock(source="vendor/package.whl"),),
            apps=(
                mock.Mock(
                    path="applications/dummy",
                    version_file="src/dummy/version.py",
                ),
            ),
            platform_build_inputs=(
                "platform/testenv/**/*.py",
                "root-file.txt",
                "*/wildcard-root.txt",
            ),
        )

        self.assertEqual(
            set(ledger._planner_path_anchors(registry)),
            {
                "applications/dummy",
                "applications/dummy/src/dummy/version.py",
                "changes",
                "configuration/compatibility.toml",
                "ops/release",
                "ops/release/apps.toml",
                "platform/testenv",
                "root-file.txt",
                "shared/build.json",
            },
        )

    def test_bootstrap_cli_writes_canonical_report_and_github_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp), initial_version="0.1.0")
            output = Path(temp) / "ledger.json"
            github_output = Path(temp) / "github-output.txt"
            github_output.touch()
            environment = {
                **fixture.environment,
                "GITHUB_OUTPUT": str(github_output),
            }
            result = subprocess.run(
                [
                    sys.executable,
                    str(PROJECT_ROOT / "ops" / "release" / "ledger.py"),
                    "verify",
                    "--root",
                    str(fixture.repo),
                    "--source-commit",
                    fixture.head,
                    "--target-version",
                    "0.1.0",
                    "--output",
                    str(output),
                ],
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, output.read_bytes())
            report = json.loads(result.stdout)
            self.assertEqual(result.stdout, _canonical(report))
            self.assertTrue(report["bootstrap"])
            self.assertEqual(report["platform_id"], "test-platform")
            self.assertEqual(report["initial_version"], "0.1.0")
            self.assertEqual(report["releases"], [])
            self.assertIsNone(report["latest"])
            self.assertEqual(
                report["target"],
                {
                    "absent": True,
                    "platform_version": "0.1.0",
                    "ref": "refs/heads/release/platform-v0.1.0",
                    "release_path": "releases/platform/v0.1.0",
                },
            )
            outputs = github_output.read_text(encoding="utf-8").splitlines()
            self.assertIn("platform_id=test-platform", outputs)
            self.assertIn("initial_version=0.1.0", outputs)
            self.assertIn("bootstrap=true", outputs)
            self.assertIn("release_count=0", outputs)
            self.assertIn("latest_version=", outputs)
            self.assertIn("target_version_absent=true", outputs)

    def test_verified_chain_is_numeric_and_fetches_private_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp), initial_version="0.9.9")
            first_commit, first_manifest = fixture.add_release(
                "0.9.9", previous=None
            )
            previous = fixture.predecessor("0.9.9", first_manifest)
            fixture.source_change("next.txt")
            platform_change = fixture.stage_platform_feature()
            second_commit, _ = fixture.add_release(
                "0.10.0", previous=previous, changes=[platform_change]
            )

            with mock.patch.object(
                ledger, "verify_release_dir", side_effect=_read_test_manifest
            ), mock.patch.object(
                ledger, "_run_git", wraps=ledger._run_git
            ) as run_git:
                report = ledger.verify_ledger(
                    fixture.repo,
                    fixture.head,
                    target_version="1.0.0",
                    environ=fixture.environment,
                )

            self.assertFalse(report["bootstrap"])
            self.assertEqual(
                [item["platform_version"] for item in report["releases"]],
                ["0.9.9", "0.10.0"],
            )
            self.assertEqual(report["latest"], report["releases"][-1])
            self.assertEqual(report["latest"]["release_commit"], second_commit)
            self.assertEqual(
                fixture.git(
                    "rev-parse", "refs/buh-release-ledger/releases/v0.9.9"
                ),
                first_commit,
            )
            fetch_arguments = [
                call.args[1]
                for call in run_git.call_args_list
                if call.args[1] and call.args[1][0] == "fetch"
            ]
            self.assertEqual(len(fetch_arguments), 1)
            self.assertIn(
                "+refs/heads/release/platform-v*:"
                "refs/buh-release-ledger/releases/v*",
                fetch_arguments[0],
            )

    def test_verified_chain_rejects_a_non_immediate_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            first = fixture.predecessor("1.0.0", first_manifest)

            fixture.source_change("skipped.txt")
            skipped_change = _platform_feature()
            fixture.stage_change(skipped_change)
            _, skipped_manifest = fixture.add_release(
                "1.1.0",
                previous=first,
                changes=[skipped_change],
            )
            skipped = fixture.predecessor("1.1.0", skipped_manifest)

            fixture.source_change("invalid-predecessor.txt")
            next_change = {
                "fragment": "next-release.toml",
                "app_id": "platform",
                "kind": "fix",
                "bump": "patch",
                "summary": "Continue the release chain",
            }
            fixture.stage_change(next_change)
            fixture.add_release(
                "1.1.1",
                previous=first,
                artifact_previous=skipped,
                changes=[next_change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "exact immediately preceding|immediate predecessor"
            ):
                fixture.verify()

    def test_verified_chain_continues_from_the_immediate_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            first = fixture.predecessor("1.0.0", first_manifest)

            fixture.source_change("intervening.txt")
            skipped_change = _platform_feature()
            fixture.stage_change(skipped_change)
            _, skipped_manifest = fixture.add_release(
                "1.1.0",
                previous=first,
                changes=[skipped_change],
            )
            skipped = fixture.predecessor("1.1.0", skipped_manifest)

            fixture.source_change("next.txt")
            next_change = {
                "fragment": "next.toml",
                "app_id": "platform",
                "kind": "fix",
                "bump": "patch",
                "summary": "Continue from the immediate predecessor",
            }
            fixture.stage_change(next_change)
            fixture.add_release(
                "1.1.1",
                previous=skipped,
                changes=[next_change],
            )

            self.assertEqual(fixture.verify()["latest"]["platform_version"], "1.1.1")

    def test_incident_regression_fails_with_clear_stale_main_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp), initial_version="0.4.2")
            _, first_manifest = fixture.add_release("0.4.2", previous=None)
            previous = fixture.predecessor("0.4.2", first_manifest)
            source_before_release = fixture.source_change("v043-source.txt")
            fixture.add_release(
                "0.4.3", previous=previous, sync_main=False
            )
            fixture.git("reset", "--hard", source_before_release)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                r"Stale main release ledger: .*release/platform-v0\.4\.3.*sync",
            ):
                fixture.verify()

    def test_release_tree_must_remain_byte_identical_in_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            (release_dir / "EXTRA.txt").write_text("mutation\n", encoding="utf-8")
            fixture.git("add", "releases/platform/v1.0.0/EXTRA.txt")
            fixture.git("commit", "-m", "mutate published release")

            with self.assertRaisesRegex(
                ledger.LedgerError, "not byte-identical"
            ):
                fixture.verify()

    def test_each_release_commit_must_preserve_all_older_release_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            first_path = fixture.repo / "releases/platform/v1.0.0/RELEASE.json"
            original_first = first_path.read_bytes()
            fixture.source_change("next.txt")
            platform_change = fixture.stage_platform_feature()

            first_path.write_bytes(original_first + b"\n")
            fixture.git("add", "releases/platform/v1.0.0/RELEASE.json")
            fixture.add_release(
                "1.1.0", previous=previous, changes=[platform_change]
            )

            first_path.write_bytes(original_first)
            fixture.git("add", "releases/platform/v1.0.0/RELEASE.json")
            fixture.git("commit", "-m", "restore prior immutable release bytes")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError, "Forbidden release commit change"
            ):
                fixture.verify()

    def test_release_parent_must_equal_verified_manifest_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release(
                "1.0.0", previous=None, manifest_source="f" * 40
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "parent does not match manifest source_commit"
            ):
                fixture.verify()

    def test_exact_version_update_and_consumed_fragment_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fragment = fixture.repo / "changes/dummy-fix.toml"
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\n'
                'summary = "Correct dummy behavior"\n',
                encoding="utf-8",
            )
            fixture.git("add", "changes/dummy-fix.toml")
            fixture.git("commit", "-m", "declare dummy fix")

            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            fragment.unlink()
            fixture.git(
                "add",
                "-A",
                "apps/dummy/src/dummy/__init__.py",
                "changes/dummy-fix.toml",
            )
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_version="0.1.1",
                changes=[_dummy_change("dummy-fix.toml")],
            )

            report = fixture.verify()
            self.assertEqual(report["latest"]["platform_version"], "1.0.0")

    def test_patch_fragment_cannot_authorize_arbitrary_app_version_jump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            change = _dummy_change("dummy-fix.toml")
            fixture.stage_change(change)
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.9.0"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_version="0.9.0",
                changes=[change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Owned-app version transition does not match consumed fragments",
            ):
                fixture.verify()

    def test_app_version_update_without_fragment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_version="0.1.1",
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Owned-app version transition does not match consumed fragments",
            ):
                fixture.verify()

    def test_platform_version_jump_must_match_highest_fragment_bump(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            fixture.source_change("platform-jump.txt")
            change = _platform_feature()
            fixture.stage_change(change)
            fixture.add_release("2.0.0", previous=previous, changes=[change])

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Platform version transition does not match the consumed change fragments",
            ):
                fixture.verify()

    def test_manifest_listed_fragment_must_be_deleted_by_release_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            change = _dummy_change("dummy-fix.toml")
            fragment = fixture.repo / "changes/dummy-fix.toml"
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\n'
                'summary = "Correct dummy behavior"\n',
                encoding="utf-8",
            )
            fixture.git("add", "changes/dummy-fix.toml")
            fixture.git("commit", "-m", "declare dummy fix")
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_version="0.1.1",
                changes=[change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "does not delete exactly the manifest-listed change fragments",
            ):
                fixture.verify()

    def test_release_commit_rejects_unrelated_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            (fixture.repo / "README.md").write_text(
                "source plus smuggled change\n", encoding="utf-8"
            )
            fixture.git("add", "README.md")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError, "Forbidden release commit change: M README.md"
            ):
                fixture.verify()

    def test_release_payload_entries_must_be_100644_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            readme = fixture.repo / "releases/platform/v1.0.0/README.md"
            readme.chmod(0o755)
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Release payload entry is not an exact 100644 blob",
            ):
                fixture.verify()

    def test_release_payload_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            readme = fixture.repo / "releases/platform/v1.0.0/README.md"
            readme.unlink()
            readme.symlink_to("RELEASE.json")
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Release payload entry is not an exact 100644 blob",
            ):
                fixture.verify()

    def test_release_readme_must_match_canonical_assembler_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            readme = fixture.repo / "releases/platform/v1.0.0/README.md"
            readme.write_text("handcrafted release notes\n", encoding="utf-8")
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "release README is not canonical",
            ):
                fixture.verify()

    def test_consumed_fragment_must_be_a_100644_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            change = _dummy_change("executable-fragment.toml")
            fragment = fixture.repo / "changes" / change["fragment"]
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\nsummary = "Correct dummy behavior"\n',
                encoding="utf-8",
            )
            fragment.chmod(0o755)
            fixture.git("add", fragment.relative_to(fixture.repo).as_posix())
            fixture.git("commit", "-m", "add executable change fragment")
            fragment.unlink()
            fixture.git("add", "-A", "changes")
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_version="0.1.1",
                changes=[change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Expected a regular tracked file",
            ):
                fixture.verify()

    def test_release_commit_rejects_extra_version_file_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            fragment = fixture.repo / "changes/dummy-fix.toml"
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\n'
                'summary = "Correct dummy behavior"\n',
                encoding="utf-8",
            )
            fixture.git("add", "changes/dummy-fix.toml")
            fixture.git("commit", "-m", "declare dummy version fix")
            version_file.write_text(
                '__version__ = "0.1.1"\nBACKDOOR = True\n', encoding="utf-8"
            )
            fragment.unlink()
            fixture.git(
                "add",
                "-A",
                "apps/dummy/src/dummy/__init__.py",
                "changes/dummy-fix.toml",
            )
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_version="0.1.1",
                changes=[_dummy_change("dummy-fix.toml")],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "input digest differs|not the exact planned update",
            ):
                fixture.verify()

    def test_release_commit_rejects_unlisted_fragment_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fragment = fixture.repo / "changes/unlisted.toml"
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\nsummary = "Listed nowhere"\n',
                encoding="utf-8",
            )
            fixture.git("add", "changes/unlisted.toml")
            fixture.git("commit", "-m", "add unlisted fragment")
            fragment.unlink()
            fixture.git("add", "-A", "changes/unlisted.toml")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "do not exactly match|Forbidden release commit change",
            ):
                fixture.verify()

    def test_release_commit_rejects_manifest_fragment_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fragment = fixture.repo / "changes/dummy-fix.toml"
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\nsummary = "Actual summary"\n',
                encoding="utf-8",
            )
            fixture.git("add", "changes/dummy-fix.toml")
            fixture.git("commit", "-m", "add real fragment")
            fragment.unlink()
            fixture.git("add", "-A", "changes/dummy-fix.toml")
            fixture.add_release(
                "1.0.0",
                previous=None,
                changes=[_dummy_change("dummy-fix.toml", "Forged summary")],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "do not exactly match|contents do not match"
            ):
                fixture.verify()

    def test_predecessor_must_match_immediately_prior_verified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            previous["manifest_sha256"] = "0" * 64
            fixture.source_change("next.txt")
            platform_change = fixture.stage_platform_feature()
            fixture.add_release(
                "1.1.0", previous=previous, changes=[platform_change]
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "exact immediately preceding"
            ):
                fixture.verify()

    def test_manifest_version_must_match_branch_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp), initial_version="1.2.3")
            fixture.add_release("1.2.3", previous=None)
            manifest_path = fixture.repo / "releases/platform/v1.2.3/RELEASE.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["platform_version"] = "1.2.4"
            manifest_path.write_bytes(_canonical(manifest))
            fixture.git("add", "releases/platform/v1.2.3/RELEASE.json")
            fixture.git("commit", "-m", "bad manifest version")

            with self.assertRaisesRegex(
                ledger.LedgerError, "versions disagree"
            ):
                fixture.verify()

    def test_manifest_platform_identity_must_match_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release(
                "1.0.0", previous=None, platform_id="another-platform"
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "platform_id does not match the registry"
            ):
                fixture.verify()

    def test_historical_release_survives_later_registered_app_addition(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)

            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8")
                + "\n[[apps]]\n"
                + 'id = "second"\n'
                + 'path = "apps/second"\n'
                + 'distribution = "test-second"\n'
                + 'import_name = "second"\n'
                + 'django_app = "second"\n'
                + 'version_file = "src/second/__init__.py"\n',
                encoding="utf-8",
            )
            second_version = fixture.repo / "apps/second/src/second/__init__.py"
            second_version.parent.mkdir(parents=True)
            second_version.write_text('__version__ = "0.5.0"\n', encoding="utf-8")
            second_pyproject = fixture.repo / "apps/second/pyproject.toml"
            second_pyproject.write_text(
                '[project]\nname = "test-second"\nversion = "0.5.0"\n'
                "dependencies = []\n",
                encoding="utf-8",
            )
            fixture.git(
                "add",
                "ops/release/apps.toml",
                "apps/second/pyproject.toml",
                "apps/second/src/second/__init__.py",
            )
            fixture.git("commit", "-m", "register second application")

            app_change = _app_change(
                "second",
                "second-feature.toml",
                summary="Add second application",
            )
            platform_change = _platform_feature("registry-feature.toml")
            for change in (app_change, platform_change):
                fragment = fixture.repo / "changes" / change["fragment"]
                fragment.parent.mkdir(exist_ok=True)
                fragment.write_text(
                    f'app = "{change["app_id"]}"\n'
                    f'kind = "{change["kind"]}"\n'
                    f'summary = "{change["summary"]}"\n',
                    encoding="utf-8",
                )
            fixture.git("add", "changes")
            fixture.git("commit", "-m", "declare second and registry features")
            for change in (app_change, platform_change):
                (fixture.repo / "changes" / change["fragment"]).unlink()
            fixture.git("add", "-A", "changes")
            second_version.write_text('__version__ = "0.6.0"\n', encoding="utf-8")
            fixture.git("add", "apps/second/src/second/__init__.py")
            fixture.add_release(
                "1.1.0",
                previous=previous,
                app_versions={"dummy": "0.1.0", "second": "0.6.0"},
                changes=[app_change, platform_change],
            )

            report = fixture.verify()
            self.assertEqual(
                [record["platform_version"] for record in report["releases"]],
                ["1.0.0", "1.1.0"],
            )

    def test_historical_registry_dependency_requires_matching_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8")
                + "\n[[apps]]\n"
                + 'id = "second"\n'
                + 'path = "apps/second"\n'
                + 'distribution = "test-second"\n'
                + 'import_name = "second"\n'
                + 'django_app = "second"\n'
                + 'version_file = "src/second/__init__.py"\n'
                + 'dependencies = ["dummy"]\n',
                encoding="utf-8",
            )
            second_version = fixture.repo / "apps/second/src/second/__init__.py"
            second_version.parent.mkdir(parents=True)
            second_version.write_text('__version__ = "0.5.0"\n', encoding="utf-8")
            second_pyproject = fixture.repo / "apps/second/pyproject.toml"
            second_pyproject.write_text(
                '[project]\nname = "test-second"\nversion = "0.5.0"\n'
                "dependencies = []\n",
                encoding="utf-8",
            )
            fixture.git(
                "add",
                "ops/release/apps.toml",
                "apps/second/pyproject.toml",
                "apps/second/src/second/__init__.py",
            )
            fixture.git("commit", "-m", "register mismatched second application")
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_versions={"dummy": "0.1.0", "second": "0.5.0"},
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "dependency contracts cannot be reproduced",
            ):
                fixture.verify()

    def test_owned_wheel_cannot_omit_registry_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.register_second_app(
                registry_dependencies=("dummy",),
                project_dependencies=("test-dummy>=0.1.0",),
            )
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_versions={"dummy": "0.1.0", "second": "0.5.0"},
                wheel_requirements={"second": ()},
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "owned-wheel dependency contracts cannot be reproduced",
            ):
                fixture.verify()

    def test_owned_wheel_cannot_add_unregistered_owned_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.register_second_app()
            fixture.add_release(
                "1.0.0",
                previous=None,
                app_versions={"dummy": "0.1.0", "second": "0.5.0"},
                wheel_requirements={"dummy": ("test-second==0.5.0",)},
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "owned-wheel dependency contracts cannot be reproduced",
            ):
                fixture.verify()

    def test_provided_dependency_source_bytes_are_historically_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            expected_dir = Path(temp) / "expected"
            expected_dir.mkdir()
            expected = _make_wheel(expected_dir, "vendor-lib", "2.0.0")
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(
                vendor_dir,
                "vendor-lib",
                "2.0.0",
                requires_dist=("external-package>=1",),
            )
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=expected.name,
                sha256=buh_release.sha256_file(expected),
                git_blob_sha=buh_release.git_blob_object_id(expected, hex_length=40),
            )
            fixture.add_release(
                "1.0.0",
                previous=None,
                dependency_release_sources={"vendor-lib": expected},
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Provided dependency source differs",
            ):
                fixture.verify()

    def test_provided_dependency_source_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            expected_dir = Path(temp) / "expected"
            expected_dir.mkdir()
            expected = _make_wheel(expected_dir, "vendor-lib", "2.0.0")
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = vendor_dir / expected.name
            source.symlink_to(expected)
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=expected.name,
                sha256=buh_release.sha256_file(expected),
            )
            fixture.add_release(
                "1.0.0",
                previous=None,
                dependency_release_sources={"vendor-lib": expected},
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "dependency source is not an exact regular file",
            ):
                fixture.verify()

    def test_provided_dependency_source_symlink_parent_is_rejected(self) -> None:
        dependency = mock.Mock(
            component="vendor-lib",
            source="vendor-alias/vendor-lib.whl",
        )

        with self.assertRaisesRegex(
            ledger.LedgerError,
            "dependency source is not an exact regular file",
        ):
            ledger._inspect_dependency_source(
                Path("unused-snapshot"),
                dependency,
                frozenset({"vendor-alias"}),
                frozenset(),
            )

    def test_provided_dependency_git_blob_identity_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
                git_blob_sha="0" * 40,
            )
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "wrong Git blob identity",
            ):
                fixture.verify()

    def test_current_registry_git_blob_must_match_repository_object_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
                git_blob_sha=buh_release.git_blob_object_id(source, hex_length=64),
            )
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Current registry dependency Git blob identity does not match",
            ):
                fixture.verify(target_version="1.0.0")

    def test_historical_registry_git_blob_must_match_repository_object_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            sha256_git_blob = buh_release.git_blob_object_id(source, hex_length=64)
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
                git_blob_sha=sha256_git_blob,
            )
            fixture.add_release("1.0.0", previous=None)
            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8").replace(
                    sha256_git_blob,
                    buh_release.git_blob_object_id(source, hex_length=40),
                ),
                encoding="utf-8",
            )
            fixture.git("add", "ops/release/apps.toml")
            fixture.git("commit", "-m", "correct dependency Git object format")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Historical registry .* dependency Git blob identity does not match",
            ):
                fixture.verify()

    def test_third_party_artifact_git_blob_must_match_repository_object_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
                git_blob_sha=buh_release.git_blob_object_id(source, hex_length=40),
            )
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            manifest = _read_test_manifest(release_dir)
            artifact = next(
                item
                for item in manifest["artifacts"]
                if item["component"] == "vendor-lib"
            )
            artifact["git_blob_sha"] = buh_release.git_blob_object_id(
                source, hex_length=64
            )
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "third-party artifact Git blob identity does not match",
            ):
                fixture.verify()

    def test_reused_dependency_may_remove_its_source_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
                git_blob_sha=buh_release.git_blob_object_id(source, hex_length=40),
            )
            fixture.add_release("1.0.0", previous=None)
            source.unlink()
            fixture.git("add", "-A", "vendor")
            fixture.git("commit", "-m", "remove reusable dependency source")
            fixture.git("push", "origin", "HEAD:main")

            self.assertEqual(fixture.verify()["latest"]["platform_version"], "1.0.0")

    def test_reused_dependency_may_gain_registry_git_blob_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
            )
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)

            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8")
                + f'git_blob_sha = "{buh_release.git_blob_object_id(source, hex_length=40)}"\n',
                encoding="utf-8",
            )
            fixture.git("add", "ops/release/apps.toml")
            fixture.git("commit", "-m", "pin reusable dependency Git blob")
            platform_change = _platform_feature("dependency-pin.toml")
            fixture.stage_change(platform_change)
            fixture.add_release(
                "1.1.0",
                previous=previous,
                changes=[platform_change],
            )

            report = fixture.verify()
            self.assertEqual(report["latest"]["platform_version"], "1.1.0")

    def test_target_preflight_rejects_dependency_source_in_release_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "releases/platform/vendor"
            vendor_dir.mkdir(parents=True)
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
            )
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "dependency source is not an exact regular file",
            ):
                fixture.verify(target_version="1.0.0")

    def test_target_preflight_requires_100644_version_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.chmod(0o755)
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.git("commit", "-m", "make version file executable")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Registered version file is not an exact 100644 blob",
            ):
                fixture.verify(target_version="1.0.0")

    def test_target_preflight_requires_100644_change_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fragment = fixture.repo / "changes/dummy-fix.toml"
            fragment.parent.mkdir()
            fragment.write_text(
                'app = "dummy"\nkind = "fix"\nsummary = "Fix dummy"\n',
                encoding="utf-8",
            )
            fragment.chmod(0o755)
            fixture.git("add", "changes/dummy-fix.toml")
            fixture.git("commit", "-m", "add executable change fragment")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Prospective change fragment is not an exact 100644 blob",
            ):
                fixture.verify(target_version="1.0.0")

    def test_target_preflight_requires_100644_provided_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor_dir = fixture.repo / "vendor"
            vendor_dir.mkdir()
            source = _make_wheel(vendor_dir, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
            )
            source.chmod(0o755)
            fixture.git("add", source.relative_to(fixture.repo).as_posix())
            fixture.git("commit", "-m", "make provided dependency executable")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Provided dependency source is not an exact 100644 blob",
            ):
                fixture.verify(target_version="1.0.0")

    def test_target_preflight_rejects_symlink_parent_of_compatibility_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            (fixture.repo / "alias").symlink_to("platform", target_is_directory=True)
            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8").replace(
                    'compatibility_file = "platform/compatibility.toml"',
                    'compatibility_file = "alias/compatibility.toml"',
                ),
                encoding="utf-8",
            )
            fixture.git("add", "alias", "ops/release/apps.toml")
            fixture.git("commit", "-m", "route compatibility through symlink")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Historical release input is a symlink: alias",
            ):
                fixture.verify(target_version="1.0.0")

    def test_target_preflight_allows_unselected_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            (fixture.repo / "documentation-link").symlink_to("README.md")
            fixture.git("add", "documentation-link")
            fixture.git("commit", "-m", "add unrelated documentation symlink")
            fixture.git("push", "origin", "HEAD:main")

            report = fixture.verify(target_version="1.0.0")

            self.assertEqual(report["target"]["platform_version"], "1.0.0")
            self.assertTrue(report["target"]["absent"])

    def test_historical_release_survives_later_version_file_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)

            old_version = "apps/dummy/src/dummy/__init__.py"
            new_version = "apps/dummy/src/dummy/version.py"
            fixture.git("mv", old_version, new_version)
            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8").replace(
                    'version_file = "src/dummy/__init__.py"',
                    'version_file = "src/dummy/version.py"',
                ),
                encoding="utf-8",
            )
            fixture.git("add", "ops/release/apps.toml", new_version)
            fixture.git("commit", "-m", "move dummy version file")

            app_change = _app_change(
                "dummy",
                "dummy-feature.toml",
                summary="Update dummy after version-file move",
            )
            platform_change = _platform_feature("registry-feature.toml")
            for change in (app_change, platform_change):
                fragment = fixture.repo / "changes" / change["fragment"]
                fragment.parent.mkdir(exist_ok=True)
                fragment.write_text(
                    f'app = "{change["app_id"]}"\n'
                    f'kind = "{change["kind"]}"\n'
                    f'summary = "{change["summary"]}"\n',
                    encoding="utf-8",
                )
            fixture.git("add", "changes")
            fixture.git("commit", "-m", "declare dummy and registry features")
            for change in (app_change, platform_change):
                (fixture.repo / "changes" / change["fragment"]).unlink()
            fixture.git("add", "-A", "changes")
            version_file = fixture.repo / new_version
            version_file.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
            fixture.git("add", new_version)
            fixture.add_release(
                "1.1.0",
                previous=previous,
                app_version="0.2.0",
                changes=[app_change, platform_change],
            )

            report = fixture.verify()
            self.assertEqual(report["latest"]["platform_version"], "1.1.0")

    def test_historical_release_survives_current_registry_app_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8")
                .replace('id = "dummy"', 'id = "renamed"')
                .replace('distribution = "test-dummy"', 'distribution = "test-renamed"')
                .replace('import_name = "dummy"', 'import_name = "renamed"')
                .replace('django_app = "dummy"', 'django_app = "renamed"'),
                encoding="utf-8",
            )
            fixture.git("add", "ops/release/apps.toml")
            fixture.git("commit", "-m", "rename current registry application")
            fixture.git("push", "origin", "HEAD:main")

            report = fixture.verify()
            self.assertEqual(report["latest"]["platform_version"], "1.0.0")

    def test_unchanged_app_fragment_is_rejected_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            fixture.source_change("platform-change.txt")
            app_change = _dummy_change("stale-dummy.toml")
            platform_change = _platform_feature("platform-feature.toml")
            fixture.stage_changes([app_change, platform_change])
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.1.0",
                previous=previous,
                app_version="0.1.1",
                changes=[app_change, platform_change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "Unchanged application has a stale change fragment"
            ):
                fixture.verify()

    def test_changed_app_without_fragment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            source_file = fixture.repo / "apps/dummy/src/dummy/feature.py"
            source_file.write_text("ENABLED = True\n", encoding="utf-8")
            (fixture.repo / "platform-change.txt").write_text(
                "changed platform\n", encoding="utf-8"
            )
            fixture.git("add", "apps/dummy/src/dummy/feature.py", "platform-change.txt")
            fixture.git("commit", "-m", "change app and platform inputs")
            platform_change = {
                "fragment": "platform-fix.toml",
                "app_id": "platform",
                "kind": "fix",
                "bump": "patch",
                "summary": "Correct platform inputs",
            }
            fixture.stage_change(platform_change)
            fixture.add_release(
                "1.0.1",
                previous=previous,
                changes=[platform_change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "Changed application is missing a change fragment"
            ):
                fixture.verify()

    def test_changed_platform_inputs_without_platform_fragment_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            source_file = fixture.repo / "apps/dummy/src/dummy/feature.py"
            source_file.write_text("ENABLED = True\n", encoding="utf-8")
            (fixture.repo / "platform-change.txt").write_text(
                "changed platform\n", encoding="utf-8"
            )
            fixture.git("add", "apps/dummy/src/dummy/feature.py", "platform-change.txt")
            fixture.git("commit", "-m", "change app and platform inputs")
            app_change = _dummy_change("dummy-fix.toml")
            fixture.stage_change(app_change)
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.1.1"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.0.1",
                previous=previous,
                app_version="0.1.1",
                changes=[app_change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "platform inputs are missing a platform fragment"
            ):
                fixture.verify()

    def test_unchanged_platform_inputs_reject_stale_platform_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            source_file = fixture.repo / "apps/dummy/src/dummy/feature.py"
            source_file.write_text("ENABLED = True\n", encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/feature.py")
            fixture.git("commit", "-m", "change only application inputs")
            app_change = _app_change(
                "dummy", "dummy-feature.toml", summary="Add dummy feature"
            )
            platform_change = _platform_feature("stale-platform.toml")
            fixture.stage_changes([app_change, platform_change])
            version_file = fixture.repo / "apps/dummy/src/dummy/__init__.py"
            version_file.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
            fixture.git("add", "apps/dummy/src/dummy/__init__.py")
            fixture.add_release(
                "1.1.0",
                previous=previous,
                app_version="0.2.0",
                changes=[app_change, platform_change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "platform inputs have a stale platform fragment"
            ):
                fixture.verify()

    def test_historical_build_digest_forgery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            manifest = _read_test_manifest(release_dir)
            manifest["build"]["tool_sha256"] = "0" * 64
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError, "build digests do not match"
            ):
                fixture.verify()

    def test_owned_artifact_registry_identity_forgery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            manifest = _read_test_manifest(release_dir)
            manifest["artifacts"][0]["distribution"] = "forged-distribution"
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError, "Owned artifact identity differs"
            ):
                fixture.verify()

    def test_manifest_artifacts_must_use_assembler_component_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor = fixture.repo / "vendor"
            vendor.mkdir()
            source = _make_wheel(vendor, "vendor-lib", "2.0.0")
            fixture.register_dependency(
                component="vendor-lib",
                distribution="vendor-lib",
                version="2.0.0",
                source=source.relative_to(fixture.repo).as_posix(),
                filename=source.name,
                sha256=buh_release.sha256_file(source),
            )
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            manifest = _read_test_manifest(release_dir)
            manifest["artifacts"].reverse()
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "not in canonical component order",
            ):
                fixture.verify()

    def test_built_owned_artifact_cannot_add_git_blob_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            manifest = _read_test_manifest(release_dir)
            manifest["artifacts"][0]["git_blob_sha"] = "a" * 40
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Built owned artifact contains non-assembler Git metadata",
            ):
                fixture.verify()

    def test_install_plan_registry_identity_forgery_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.add_release("1.0.0", previous=None)
            release_dir = fixture.repo / "releases/platform/v1.0.0"
            install_path = release_dir / "INSTALL_PLAN.json"
            install_plan = json.loads(install_path.read_text(encoding="utf-8"))
            install_plan["django_apps"] = ["forged.application"]
            install_path.write_bytes(_canonical(install_plan))
            manifest = _read_test_manifest(release_dir)
            manifest["install_plan"]["sha256"] = hashlib.sha256(
                install_path.read_bytes()
            ).hexdigest()
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.0.0")

            with self.assertRaisesRegex(
                ledger.LedgerError, "Django applications differ"
            ):
                fixture.verify()

    def test_reused_owned_artifact_must_equal_immutable_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)
            fixture.source_change("platform-change.txt")
            platform_change = _platform_feature()
            fixture.stage_change(platform_change)
            fixture.add_release(
                "1.1.0", previous=previous, changes=[platform_change]
            )
            release_dir = fixture.repo / "releases/platform/v1.1.0"
            manifest = _read_test_manifest(release_dir)
            manifest["artifacts"][0]["sha256"] = "f" * 64
            install_path = release_dir / "INSTALL_PLAN.json"
            install_plan = json.loads(install_path.read_text(encoding="utf-8"))
            install_plan["wheels"][0]["sha256"] = "f" * 64
            install_path.write_bytes(_canonical(install_plan))
            manifest["install_plan"]["sha256"] = hashlib.sha256(
                install_path.read_bytes()
            ).hexdigest()
            (release_dir / "RELEASE.json").write_bytes(_canonical(manifest))
            fixture.republish_amended_release("1.1.0")

            with self.assertRaisesRegex(
                ledger.LedgerError, "differs from its immutable predecessor"
            ):
                fixture.verify()

    def test_built_owned_wheel_cannot_collide_with_legacy_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            legacy = fixture.repo / "releases/legacy/v0"
            legacy.mkdir(parents=True)
            wheel = _make_wheel(
                legacy,
                "test-dummy",
                "0.1.0",
                requires_dist=("external-package>=1",),
            )
            fixture.git("add", wheel.relative_to(fixture.repo).as_posix())
            fixture.git("commit", "-m", "add legacy conflicting wheel")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "previously released with different bytes",
            ):
                fixture.verify()

    def test_built_owned_wheel_cannot_collide_with_prior_third_party(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            vendor = fixture.repo / "vendor"
            vendor.mkdir()
            dependency_wheel = _make_wheel(
                vendor,
                "future-lib",
                "0.1.0",
                requires_dist=("external-package>=1",),
            )
            fixture.register_dependency(
                component="future-lib",
                distribution="future-lib",
                version="0.1.0",
                source=dependency_wheel.relative_to(fixture.repo).as_posix(),
                filename=dependency_wheel.name,
                sha256=buh_release.sha256_file(dependency_wheel),
            )
            _, first_manifest = fixture.add_release("1.0.0", previous=None)
            previous = fixture.predecessor("1.0.0", first_manifest)

            registry = fixture.repo / "ops/release/apps.toml"
            registry.write_text(
                registry.read_text(encoding="utf-8").split(
                    "\n[[dependencies]]\n", 1
                )[0]
                + "\n",
                encoding="utf-8",
            )
            dependency_wheel.unlink()
            fixture.git("add", "-A", "ops/release/apps.toml", "vendor")
            fixture.git("commit", "-m", "retire bundled future library")
            fixture.register_second_app(
                distribution="future-lib",
                version="0.0.0",
            )
            app_change = _app_change("second", "second-feature.toml")
            platform_change = _platform_feature("registry-feature.toml")
            fixture.stage_changes([app_change, platform_change])
            second_version = fixture.repo / "apps/second/src/second/__init__.py"
            second_version.write_text('__version__ = "0.1.0"\n', encoding="utf-8")
            fixture.git("add", "apps/second/src/second/__init__.py")
            fixture.add_release(
                "1.1.0",
                previous=previous,
                app_versions={"dummy": "0.1.0", "second": "0.1.0"},
                changes=[app_change, platform_change],
            )

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "previously released with different bytes",
            ):
                fixture.verify()

    def test_snapshot_ignores_export_attribute_transformations(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            attributes = fixture.repo / ".gitattributes"
            ignored = fixture.repo / "apps/dummy/export-ignored.txt"
            substituted = fixture.repo / "apps/dummy/export-substituted.txt"
            attributes.write_text(
                "apps/dummy/export-ignored.txt export-ignore\n"
                "apps/dummy/export-substituted.txt export-subst\n",
                encoding="utf-8",
            )
            ignored.write_text("must remain\n", encoding="utf-8")
            substituted.write_text("$Format:%H$\n", encoding="utf-8")
            fixture.git(
                "add",
                ".gitattributes",
                "apps/dummy/export-ignored.txt",
                "apps/dummy/export-substituted.txt",
            )
            fixture.git("commit", "-m", "add export attribute regression inputs")

            with ledger._materialized_source_commit(
                fixture.repo, fixture.head, "releases/platform"
            ) as (snapshot, _, _):
                self.assertEqual(
                    (snapshot / "apps/dummy/export-ignored.txt").read_text(),
                    "must remain\n",
                )
                self.assertEqual(
                    (snapshot / "apps/dummy/export-substituted.txt").read_text(),
                    "$Format:%H$\n",
                )
            fixture.add_release("1.0.0", previous=None)
            self.assertEqual(fixture.verify()["latest"]["platform_version"], "1.0.0")

    def test_snapshot_file_cap_excludes_append_only_release_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            payload = fixture.repo / "releases/platform/v1.0.0"
            payload.mkdir(parents=True)
            for index in range(20):
                (payload / f"payload-{index}.txt").write_text(
                    f"payload {index}\n", encoding="utf-8"
                )
            fixture.git("add", "releases/platform/v1.0.0")
            fixture.git("commit", "-m", "add large immutable release payload")

            with mock.patch.object(ledger, "MAX_SNAPSHOT_FILES", 6):
                with ledger._materialized_source_commit(
                    fixture.repo, fixture.head, "releases/platform"
                ) as (snapshot, _, omitted):
                    self.assertFalse(
                        (snapshot / "releases/platform/v1.0.0/payload-0.txt")
                        .read_bytes()
                    )
                    self.assertEqual(len(omitted), 20)

    def test_checkout_eol_attribute_on_planner_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            attributes = fixture.repo / ".gitattributes"
            attributes.write_text(
                "apps/dummy/** text eol=crlf\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes")
            fixture.git("commit", "-m", "add checkout line-ending transform")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "checkout-transforming Git attribute",
            ):
                fixture.verify()

    def test_target_preflight_rejects_checkout_transform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            attributes = fixture.repo / ".gitattributes"
            attributes.write_text(
                "apps/dummy/** text eol=crlf\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes")
            fixture.git("commit", "-m", "add prospective checkout transform")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "checkout-transforming Git attribute",
            ):
                fixture.verify(target_version="1.0.0")

    def test_checkout_filter_attribute_on_planner_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            attributes = fixture.repo / ".gitattributes"
            attributes.write_text(
                "apps/dummy/** filter=untrusted-release-filter\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes")
            fixture.git("commit", "-m", "add checkout filter transform")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "checkout-transforming Git attribute filter",
            ):
                fixture.verify()

    def test_legacy_crlf_attribute_on_planner_input_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            attributes = fixture.repo / ".gitattributes"
            attributes.write_text("apps/dummy/** crlf\n", encoding="utf-8")
            fixture.git("add", ".gitattributes")
            fixture.git("commit", "-m", "add legacy CRLF transform")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "checkout-transforming Git attribute crlf",
            ):
                fixture.verify()

    def test_literal_filter_sentinel_assignment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.git("config", "filter.unset.smudge", "sed s/source/filtered/")
            attributes = fixture.repo / ".gitattributes"
            attributes.write_text(
                "apps/dummy/** filter=unset\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes")
            fixture.git("commit", "-m", "add ambiguous filter sentinel")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "ambiguous literal sentinel assignment",
            ):
                fixture.verify()

    def test_release_payload_ident_attribute_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            attributes = fixture.repo / ".gitattributes"
            attributes.write_text(
                "releases/platform/** ident\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes")
            fixture.git("commit", "-m", "add release payload ident transform")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "release payload.*checkout-transforming Git attribute ident",
            ):
                fixture.verify()

    def test_legacy_wheel_checkout_transform_is_rejected_historically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            legacy = fixture.repo / "releases/legacy/v0"
            legacy.mkdir(parents=True)
            wheel = _make_wheel(legacy, "legacy-lib", "2.0.0")
            with wheel.open("ab") as stream:
                stream.write(b"$Id$\n")
            (fixture.repo / ".gitattributes").write_text(
                "releases/legacy/**/*.whl ident\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes", "releases/legacy")
            fixture.git("commit", "-m", "add transformed legacy wheel history")
            fixture.add_release("1.0.0", previous=None)

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Immutable legacy wheel history has a checkout-transforming Git "
                "attribute ident",
            ):
                fixture.verify()

    def test_target_preflight_rejects_legacy_wheel_checkout_transform(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            legacy = fixture.repo / "releases/legacy/v0"
            legacy.mkdir(parents=True)
            wheel = _make_wheel(legacy, "legacy-lib", "2.0.0")
            with wheel.open("ab") as stream:
                stream.write(b"$Id$\n")
            (fixture.repo / ".gitattributes").write_text(
                "releases/legacy/**/*.whl ident\n",
                encoding="utf-8",
            )
            fixture.git("add", ".gitattributes", "releases/legacy")
            fixture.git("commit", "-m", "add prospective transformed legacy wheel")
            fixture.git("push", "origin", "HEAD:main")

            with self.assertRaisesRegex(
                ledger.LedgerError,
                "Immutable legacy wheel history has a checkout-transforming Git "
                "attribute ident",
            ):
                fixture.verify(target_version="1.0.0")

    def test_bootstrap_target_must_equal_registry_initial_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp), initial_version="3.2.1")
            with self.assertRaisesRegex(
                ledger.LedgerError, "must equal the registry initial_version"
            ):
                fixture.verify(target_version="3.2.2")

    def test_rejects_malformed_remote_release_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            fixture.git(
                "push", "origin", "HEAD:refs/heads/release/platform-v01.2.3"
            )

            with self.assertRaisesRegex(
                ledger.LedgerError, "Malformed platform release ref"
            ):
                fixture.verify()

    def test_target_must_be_newer_and_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp), initial_version="2.0.0")
            fixture.add_release("2.0.0", previous=None)
            with self.assertRaisesRegex(
                ledger.LedgerError, "newer than the verified release ledger"
            ):
                fixture.verify(target_version="2.0.0")

    def test_remote_failure_does_not_disclose_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            secret = "this-token-must-never-appear"
            fixture.git("remote", "set-url", "origin", str(Path(temp) / "missing"))
            with self.assertRaises(ledger.LedgerError) as raised:
                ledger.verify_ledger(
                    fixture.repo,
                    fixture.head,
                    environ={**os.environ, "GITHUB_TOKEN": secret},
                )
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn(secret, (fixture.repo / ".git/config").read_text())

    def test_closing_remote_snapshot_detects_mid_verification_ref_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = GitLedgerFixture(Path(temp))
            original_source = fixture.head
            fixture.add_release("1.0.0", previous=None)
            moved = False

            def verify_and_move(path: Path) -> dict:
                nonlocal moved
                if not moved:
                    fixture.git(
                        "push",
                        "--force",
                        "origin",
                        f"{original_source}:refs/heads/release/platform-v1.0.0",
                    )
                    moved = True
                return _read_test_manifest(path)

            with mock.patch.object(
                ledger, "verify_release_dir", side_effect=verify_and_move
            ), self.assertRaisesRegex(
                ledger.LedgerError,
                "Remote platform release refs changed during ledger verification",
            ):
                ledger.verify_ledger(
                    fixture.repo,
                    fixture.head,
                    environ=fixture.environment,
                )


if __name__ == "__main__":
    unittest.main()
