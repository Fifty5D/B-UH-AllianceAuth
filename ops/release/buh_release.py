#!/usr/bin/env python3
"""Build deterministic, append-only B-UH AllianceAuth release bundles.

The module intentionally uses only the Python standard library. External build
tools are invoked as subprocesses, and production publishing/deployment remains
outside this trust boundary.
"""

from __future__ import annotations

import argparse
import base64
import csv
import dataclasses
import email.parser
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
import zipfile
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_RELEASE_FILES = 128
MAX_RELEASE_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_WHEEL_MEMBERS = 10_000
MAX_WHEEL_UNCOMPRESSED = 256 * 1024 * 1024
MAX_WHEEL_RATIO = 250

SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
VERSION_ASSIGNMENT_RE = re.compile(
    rb"(?m)^(?P<prefix>__version__\s*=\s*[\"'])[^\"'\r\n]+(?P<suffix>[\"']\s*)$"
)
DIST_NORMALIZE_RE = re.compile(r"[-_.]+")

KIND_TO_BUMP = {
    "fix": "patch",
    "security": "patch",
    "performance": "patch",
    "internal": "patch",
    "feature": "minor",
    "breaking": "major",
}
BUMP_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}

IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
PLATFORM_INPUT_IGNORED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}


class ReleaseError(ValueError):
    """Raised when a release input fails closed validation."""


@dataclasses.dataclass(frozen=True, order=True)
class SemVer:
    """A deliberately strict SemVer core version (no prereleases in releases)."""

    major: int
    minor: int
    patch: int

    _PATTERN = re.compile(
        r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        if not isinstance(value, str):
            raise ReleaseError(f"Invalid strict semantic version: {value!r}")
        match = cls._PATTERN.fullmatch(value)
        if not match:
            raise ReleaseError(f"Invalid strict semantic version: {value!r}")
        return cls(*(int(part) for part in match.groups()))

    def bump(self, kind: str) -> "SemVer":
        if kind == "none":
            return self
        if kind == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        if kind == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if kind == "major":
            return SemVer(self.major + 1, 0, 0)
        raise ReleaseError(f"Unknown version bump: {kind!r}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclasses.dataclass(frozen=True)
class AppConfig:
    app_id: str
    path: str
    distribution: str
    import_name: str
    django_app: str
    version_file: str
    dependencies: tuple[str, ...]
    setup_commands: tuple[str, ...]

    def as_fingerprint_dict(self) -> dict[str, Any]:
        return {
            "id": self.app_id,
            "path": self.path,
            "distribution": self.distribution,
            "import_name": self.import_name,
            "django_app": self.django_app,
            "version_file": self.version_file,
            "dependencies": list(self.dependencies),
            "setup_commands": list(self.setup_commands),
        }


@dataclasses.dataclass(frozen=True)
class DependencyConfig:
    component: str
    distribution: str
    version: str
    filename: str
    sha256: str
    source: str
    git_blob_sha: str | None

    def as_plan_dict(self, action: str) -> dict[str, Any]:
        value: dict[str, Any] = {
            "component": self.component,
            "kind": "third-party",
            "distribution": self.distribution,
            "version": self.version,
            "filename": self.filename,
            "sha256": self.sha256,
            "source": self.source,
            "action": action,
        }
        if self.git_blob_sha:
            value["git_blob_sha"] = self.git_blob_sha
        return value


@dataclasses.dataclass(frozen=True)
class Registry:
    platform_id: str
    initial_version: str
    release_root: str
    compatibility_file: str
    shared_build_inputs: tuple[str, ...]
    platform_build_inputs: tuple[str, ...]
    apps: tuple[AppConfig, ...]
    dependencies: tuple[DependencyConfig, ...]
    sha256: str

    @property
    def by_id(self) -> dict[str, AppConfig]:
        return {app.app_id: app for app in self.apps}


@dataclasses.dataclass(frozen=True)
class Change:
    fragment: str
    app_id: str
    kind: str
    bump: str
    summary: str

    def as_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class InternalRequirement:
    distribution: str
    specifiers: tuple[tuple[str, tuple[int, ...], int | None, bool], ...]
    raw: str


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob_object_id(path: Path, *, hex_length: int) -> str:
    if hex_length not in {40, 64}:
        raise ReleaseError(f"Unsupported Git object id length: {hex_length}")
    size = path.stat().st_size
    digest = hashlib.sha1() if hex_length == 40 else hashlib.sha256()
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_git_blob_object_id(path: Path, expected: str) -> None:
    if not isinstance(expected, str) or not GIT_OBJECT_RE.fullmatch(expected):
        raise ReleaseError(f"Invalid Git object id for {path.name}")
    if git_blob_object_id(path, hex_length=len(expected)) != expected:
        raise ReleaseError(f"Git blob object id does not match bytes: {path.name}")


def _strict_keys(
    value: Mapping[str, Any], allowed: set[str], context: str, required: set[str] = frozenset()
) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ReleaseError(f"{context} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ReleaseError(f"{context} is missing keys: {', '.join(sorted(missing))}")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            result = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"Could not read TOML {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ReleaseError(f"TOML root must be a table: {path}")
    return result


def _safe_relative_path(value: str, context: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ReleaseError(f"Unsafe {context}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ReleaseError(f"Unsafe {context}: {value!r}")
    return value


def _safe_glob_pattern(value: str, context: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ReleaseError(f"Unsafe {context}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ReleaseError(f"Unsafe {context}: {value!r}")
    if any(part in {"", "."} for part in path.parts):
        raise ReleaseError(f"Unsafe {context}: {value!r}")
    return value


def _validate_safe_filename(value: str, context: str = "filename") -> str:
    if not isinstance(value, str) or not SAFE_FILENAME_RE.fullmatch(value):
        raise ReleaseError(f"Unsafe {context}: {value!r}")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or PurePosixPath(value).name != value:
        raise ReleaseError(f"Non-canonical {context}: {value!r}")
    return value


def normalize_distribution(value: str) -> str:
    if not isinstance(value, str):
        raise ReleaseError(f"Invalid distribution name: {value!r}")
    normalized = DIST_NORMALIZE_RE.sub("-", value).lower()
    if not normalized or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise ReleaseError(f"Invalid distribution name: {value!r}")
    return normalized


def _parse_constraint_version(
    value: str, *, allow_wildcard: bool
) -> tuple[tuple[int, ...], int | None, bool]:
    wildcard = value.endswith(".*")
    if wildcard:
        if not allow_wildcard:
            raise ReleaseError(f"Unsupported wildcard version constraint: {value!r}")
        value = value[:-2]
    match = re.fullmatch(
        r"((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2})"
        r"(?:\.post(0|[1-9][0-9]*))?",
        value,
    )
    if not match:
        raise ReleaseError(f"Unsupported internal version constraint: {value!r}")
    release_text, post_text = match.groups()
    if wildcard and post_text is not None:
        raise ReleaseError(f"Wildcard may not follow a post release: {value!r}")
    return (
        tuple(int(part) for part in release_text.split(".")),
        int(post_text) if post_text is not None else None,
        wildcard,
    )


def _parse_internal_requirement(
    value: str, internal_distributions: set[str]
) -> InternalRequirement | None:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseError(f"Invalid Requires-Dist value: {value!r}")
    requirement, separator, marker = value.partition(";")
    match = re.fullmatch(
        r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
        r"(\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?\s*(.*?)\s*",
        requirement,
    )
    if not match:
        # External dependency syntax is pip's concern. Internal dependency
        # syntax is handled below and must remain deliberately constrained.
        return None
    name, extras, specifier_text = match.groups()
    distribution = normalize_distribution(name)
    if distribution not in internal_distributions:
        return None
    if extras:
        raise ReleaseError(
            f"Internal requirement may not select extras: {value!r}"
        )
    if separator or marker:
        raise ReleaseError(
            f"Internal requirement may not use environment markers: {value!r}"
        )
    specifiers: list[tuple[str, tuple[int, ...], int | None, bool]] = []
    if specifier_text:
        for raw_specifier in specifier_text.split(","):
            specifier = raw_specifier.strip()
            specifier_match = re.fullmatch(
                r"(<=|>=|==|!=|~=|<|>)\s*([^\s,]+)", specifier
            )
            if not specifier_match:
                raise ReleaseError(
                    f"Unsupported internal requirement constraint: {value!r}"
                )
            operator, constraint = specifier_match.groups()
            release, post, wildcard = _parse_constraint_version(
                constraint, allow_wildcard=operator in {"==", "!="}
            )
            if operator == "~=" and len(release) < 2:
                raise ReleaseError(
                    f"Compatible-release constraint needs two components: {value!r}"
                )
            specifiers.append((operator, release, post, wildcard))
    return InternalRequirement(
        distribution=distribution,
        specifiers=tuple(specifiers),
        raw=value,
    )


def _constraint_satisfied(
    version: str,
    specifiers: Sequence[tuple[str, tuple[int, ...], int | None, bool]],
) -> bool:
    target_release, target_post, target_wildcard = _parse_constraint_version(
        version, allow_wildcard=False
    )
    if target_wildcard:
        raise ReleaseError(f"Bundle version may not be a wildcard: {version!r}")
    target_release = target_release + (0,) * (3 - len(target_release))
    target_key = (*target_release, 1 if target_post is not None else 0, target_post or 0)
    for operator, release, post, wildcard in specifiers:
        if wildcard:
            equal = target_release[: len(release)] == release
            if (operator == "==" and not equal) or (operator == "!=" and equal):
                return False
            continue
        padded = release + (0,) * (3 - len(release))
        constraint_key = (*padded, 1 if post is not None else 0, post or 0)
        if operator == "==" and target_key != constraint_key:
            return False
        if operator == "!=" and target_key == constraint_key:
            return False
        if operator == ">=" and target_key < constraint_key:
            return False
        if operator == "<=" and target_key > constraint_key:
            return False
        if operator == ">" and target_key <= constraint_key:
            return False
        if operator == "<" and target_key >= constraint_key:
            return False
        if operator == "~=":
            upper_release = (
                (release[0] + 1, 0, 0)
                if len(release) == 2
                else (release[0], release[1] + 1, 0)
            )
            upper_key = (*upper_release, 0, 0)
            if target_key < constraint_key or target_key >= upper_key:
                return False
    return True


def _source_requires_dist(repo_root: Path, app: AppConfig) -> tuple[str, ...]:
    path = repo_root / app.path / "pyproject.toml"
    data = _read_toml(path)
    project = data.get("project")
    if not isinstance(project, dict):
        raise ReleaseError(f"{app.app_id} pyproject.toml lacks [project]")
    name = project.get("name")
    if not isinstance(name, str) or normalize_distribution(name) != normalize_distribution(
        app.distribution
    ):
        raise ReleaseError(f"{app.app_id} project name does not match its registry entry")
    dynamic = project.get("dynamic", [])
    if not isinstance(dynamic, list) or not all(isinstance(item, str) for item in dynamic):
        raise ReleaseError(f"{app.app_id} has invalid dynamic project metadata")
    if "dependencies" in dynamic:
        raise ReleaseError(
            f"{app.app_id} dependencies must be statically reviewable in pyproject.toml"
        )
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise ReleaseError(f"{app.app_id} project dependencies must be a string array")
    return tuple(dependencies)


def _validate_internal_dependency_contracts(
    app_plans: Sequence[Mapping[str, Any]],
    requirements_by_component: Mapping[str, Sequence[str]],
    *,
    require_registry_match: bool,
    bundled_dependencies: Sequence[Mapping[str, Any]] = (),
) -> None:
    by_distribution: dict[str, Mapping[str, Any]] = {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for app in app_plans:
        app_id = app.get("id")
        distribution = app.get("distribution")
        version = app.get("version")
        if (
            not isinstance(app_id, str)
            or not isinstance(distribution, str)
            or not isinstance(version, str)
        ):
            raise ReleaseError("Invalid app identity in dependency contract")
        normalized = normalize_distribution(distribution)
        if normalized in by_distribution or app_id in by_id:
            raise ReleaseError("Internal dependency contract repeats an app identity")
        SemVer.parse(version)
        by_distribution[normalized] = app
        by_id[app_id] = app

    for dependency in bundled_dependencies:
        component = dependency.get("component", dependency.get("id"))
        distribution = dependency.get("distribution")
        version = dependency.get("version")
        if (
            not isinstance(component, str)
            or not isinstance(distribution, str)
            or not isinstance(version, str)
        ):
            raise ReleaseError("Invalid bundled dependency identity")
        normalized = normalize_distribution(distribution)
        if normalized in by_distribution or component in by_id:
            raise ReleaseError("Bundle dependency repeats an artifact identity")
        _parse_constraint_version(version, allow_wildcard=False)
        by_distribution[normalized] = {
            "id": component,
            "distribution": distribution,
            "version": version,
        }

    internal_distributions = set(by_distribution)
    for app_id, app in by_id.items():
        requirements = requirements_by_component.get(app_id)
        if requirements is None:
            raise ReleaseError(f"Missing dependency metadata for {app_id}")
        declared_owned: set[str] = set()
        declared_bundle: set[str] = set()
        for raw_requirement in requirements:
            parsed = _parse_internal_requirement(raw_requirement, internal_distributions)
            if parsed is None:
                continue
            dependency = by_distribution[parsed.distribution]
            dependency_id = dependency["id"]
            if dependency_id == app_id or dependency_id in declared_bundle:
                raise ReleaseError(
                    f"{app_id} repeats or self-references internal dependency {dependency_id}"
                )
            declared_bundle.add(dependency_id)
            if dependency_id in by_id:
                declared_owned.add(dependency_id)
            if not _constraint_satisfied(dependency["version"], parsed.specifiers):
                raise ReleaseError(
                    f"{app_id} requires {parsed.raw}, but planned {dependency_id} "
                    f"is {dependency['version']}"
                )
        if require_registry_match:
            expected = app.get("dependencies")
            if not isinstance(expected, list) or not all(
                isinstance(item, str) for item in expected
            ):
                raise ReleaseError(f"Invalid registry dependency graph for {app_id}")
            expected_set = set(expected)
            if declared_owned != expected_set:
                raise ReleaseError(
                    f"{app_id} dependency metadata differs from the registry; "
                    f"missing={sorted(expected_set - declared_owned)}, "
                    f"unexpected={sorted(declared_owned - expected_set)}"
                )


def load_registry(path: Path) -> Registry:
    raw_bytes = path.read_bytes()
    data = _read_toml(path)
    _strict_keys(
        data,
        {"schema_version", "platform", "apps", "dependencies"},
        "application registry",
        {"schema_version", "platform", "apps"},
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise ReleaseError("Unsupported application registry schema")

    platform = data["platform"]
    if not isinstance(platform, dict):
        raise ReleaseError("application registry platform must be a table")
    _strict_keys(
        platform,
        {
            "id",
            "initial_version",
            "release_root",
            "compatibility_file",
            "shared_build_inputs",
            "platform_build_inputs",
        },
        "application registry platform",
        {
            "id",
            "initial_version",
            "release_root",
            "compatibility_file",
            "platform_build_inputs",
        },
    )
    platform_id = platform["id"]
    if not isinstance(platform_id, str) or not SAFE_ID_RE.fullmatch(platform_id):
        raise ReleaseError("Invalid platform id")
    initial_version = str(SemVer.parse(platform["initial_version"]))
    release_root = _safe_relative_path(platform["release_root"], "release root")
    compatibility_file = _safe_relative_path(
        platform["compatibility_file"], "compatibility file"
    )
    shared_inputs = platform.get("shared_build_inputs", [])
    if not isinstance(shared_inputs, list) or not all(
        isinstance(item, str) for item in shared_inputs
    ):
        raise ReleaseError("shared_build_inputs must be a string array")
    shared_inputs_tuple = tuple(
        _safe_relative_path(item, "shared build input") for item in shared_inputs
    )
    platform_inputs = platform["platform_build_inputs"]
    if not isinstance(platform_inputs, list) or not platform_inputs or not all(
        isinstance(item, str) for item in platform_inputs
    ):
        raise ReleaseError("platform_build_inputs must be a non-empty string array")
    platform_inputs_tuple = tuple(
        _safe_glob_pattern(item, "platform build input") for item in platform_inputs
    )
    if len(platform_inputs_tuple) != len(set(platform_inputs_tuple)):
        raise ReleaseError("platform_build_inputs contains duplicates")

    apps_raw = data["apps"]
    if not isinstance(apps_raw, list) or not apps_raw:
        raise ReleaseError("application registry must contain at least one app")
    apps: list[AppConfig] = []
    seen_ids: set[str] = set()
    seen_distributions: set[str] = set()
    for index, raw in enumerate(apps_raw):
        if not isinstance(raw, dict):
            raise ReleaseError(f"application registry app {index} must be a table")
        _strict_keys(
            raw,
            {
                "id",
                "path",
                "distribution",
                "import_name",
                "django_app",
                "version_file",
                "dependencies",
                "setup_commands",
            },
            f"application registry app {index}",
            {
                "id",
                "path",
                "distribution",
                "import_name",
                "django_app",
                "version_file",
            },
        )
        app_id = raw["id"]
        if not isinstance(app_id, str) or not SAFE_ID_RE.fullmatch(app_id):
            raise ReleaseError(f"Invalid app id: {app_id!r}")
        distribution = raw["distribution"]
        if not isinstance(distribution, str):
            raise ReleaseError(f"Invalid distribution for {app_id}")
        normalized_distribution = normalize_distribution(distribution)
        if app_id in seen_ids or normalized_distribution in seen_distributions:
            raise ReleaseError(f"Duplicate app id or distribution: {app_id}")
        seen_ids.add(app_id)
        seen_distributions.add(normalized_distribution)
        dependencies = raw.get("dependencies", [])
        setup_commands = raw.get("setup_commands", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in dependencies
        ):
            raise ReleaseError(f"Invalid dependencies for {app_id}")
        if not isinstance(setup_commands, list) or not all(
            isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
            for item in setup_commands
        ):
            raise ReleaseError(f"Invalid setup commands for {app_id}")
        import_name = raw["import_name"]
        django_app = raw["django_app"]
        if not isinstance(import_name, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*", import_name
        ):
            raise ReleaseError(f"Invalid import name for {app_id}")
        if not isinstance(django_app, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*", django_app
        ):
            raise ReleaseError(f"Invalid Django app for {app_id}")
        apps.append(
            AppConfig(
                app_id=app_id,
                path=_safe_relative_path(raw["path"], f"path for {app_id}"),
                distribution=distribution,
                import_name=import_name,
                django_app=django_app,
                version_file=_safe_relative_path(
                    raw["version_file"], f"version file for {app_id}"
                ),
                dependencies=tuple(dependencies),
                setup_commands=tuple(setup_commands),
            )
        )

    app_ids = {app.app_id for app in apps}
    for app in apps:
        unknown = set(app.dependencies) - app_ids
        if unknown or app.app_id in app.dependencies:
            raise ReleaseError(
                f"Invalid internal dependencies for {app.app_id}: {sorted(unknown)}"
            )
    _topological_apps(tuple(apps))

    dependencies_raw = data.get("dependencies", [])
    if not isinstance(dependencies_raw, list):
        raise ReleaseError("registry dependencies must be an array of tables")
    dependencies: list[DependencyConfig] = []
    seen_dependency_ids: set[str] = set()
    seen_artifact_distributions = set(seen_distributions)
    for index, raw in enumerate(dependencies_raw):
        if not isinstance(raw, dict):
            raise ReleaseError(f"registry dependency {index} must be a table")
        _strict_keys(
            raw,
            {
                "id",
                "distribution",
                "version",
                "filename",
                "sha256",
                "source",
                "git_blob_sha",
            },
            f"registry dependency {index}",
            {"id", "distribution", "version", "filename", "sha256", "source"},
        )
        component = raw["id"]
        if (
            not isinstance(component, str)
            or not SAFE_ID_RE.fullmatch(component)
            or component in seen_dependency_ids
            or component in app_ids
        ):
            raise ReleaseError(f"Invalid or duplicate dependency id: {component!r}")
        seen_dependency_ids.add(component)
        distribution = raw["distribution"]
        if not isinstance(distribution, str):
            raise ReleaseError(f"Invalid dependency distribution: {component}")
        normalized_distribution = normalize_distribution(distribution)
        if normalized_distribution in seen_artifact_distributions:
            raise ReleaseError(
                f"Duplicate release distribution: {normalized_distribution}"
            )
        seen_artifact_distributions.add(normalized_distribution)
        filename = raw["filename"]
        sha256 = raw["sha256"]
        source = raw["source"]
        git_blob_sha = raw.get("git_blob_sha")
        _validate_safe_filename(filename, f"dependency filename for {component}")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise ReleaseError(f"Invalid dependency SHA-256: {component}")
        if git_blob_sha is not None and (
            not isinstance(git_blob_sha, str) or not GIT_OBJECT_RE.fullmatch(git_blob_sha)
        ):
            raise ReleaseError(f"Invalid dependency Git object id: {component}")
        dependencies.append(
            DependencyConfig(
                component=component,
                distribution=distribution,
                version=str(raw["version"]),
                filename=filename,
                sha256=sha256,
                source=_safe_relative_path(source, f"dependency source for {component}"),
                git_blob_sha=git_blob_sha,
            )
        )
    return Registry(
        platform_id=platform_id,
        initial_version=initial_version,
        release_root=release_root,
        compatibility_file=compatibility_file,
        shared_build_inputs=shared_inputs_tuple,
        platform_build_inputs=platform_inputs_tuple,
        apps=tuple(apps),
        dependencies=tuple(dependencies),
        sha256=_sha256_bytes(raw_bytes),
    )


def load_compatibility(path: Path) -> tuple[dict[str, Any], str]:
    data = _read_toml(path)
    if type(data.get("schema_version")) is not int or data.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("Unsupported compatibility schema")
    runtime = data.get("runtime")
    if not isinstance(runtime, dict):
        raise ReleaseError("compatibility.toml requires a [runtime] table")
    required = {"python", "allianceauth", "mariadb", "redis"}
    missing = required - set(runtime)
    if missing:
        raise ReleaseError(
            f"compatibility runtime is missing: {', '.join(sorted(missing))}"
        )
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in runtime.items()):
        raise ReleaseError("compatibility runtime values must be strings")
    return data, _sha256_bytes(_canonical_json_bytes(data))


def read_app_version(repo_root: Path, app: AppConfig) -> str:
    path = repo_root / app.path / app.version_file
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"Could not read version file for {app.app_id}: {exc}") from exc
    matches = list(VERSION_ASSIGNMENT_RE.finditer(data))
    if len(matches) != 1:
        raise ReleaseError(
            f"{path} must contain exactly one simple __version__ assignment"
        )
    value = data[matches[0].start("prefix") : matches[0].end("suffix")].decode("utf-8")
    version_match = re.search(r"[\"']([^\"']+)[\"']", value)
    if not version_match:
        raise ReleaseError(f"Could not parse version for {app.app_id}")
    return str(SemVer.parse(version_match.group(1)))


def _iter_source_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        raise ReleaseError(f"Source directory does not exist: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ReleaseError(f"Symlinks are not allowed in release inputs: {path}")
        if path.is_file():
            yield path


def _digest_named_files(files: Iterable[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    seen: set[str] = set()
    for name, content in sorted(files, key=lambda item: item[0]):
        normalized = unicodedata.normalize("NFC", name)
        folded = normalized.casefold()
        if normalized != name or folded in seen:
            raise ReleaseError(f"Duplicate or non-canonical release input: {name}")
        seen.add(folded)
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def app_input_digest(repo_root: Path, app: AppConfig, shared_inputs: Sequence[str]) -> str:
    app_root = repo_root / app.path
    version_path = (app_root / app.version_file).resolve()
    items: list[tuple[str, bytes]] = []
    for path in _iter_source_files(app_root):
        content = path.read_bytes()
        if path.resolve() == version_path:
            content, count = VERSION_ASSIGNMENT_RE.subn(
                rb"\g<prefix>0.0.0\g<suffix>", content
            )
            if count != 1:
                raise ReleaseError(f"Could not normalize version file for {app.app_id}")
        items.append((f"app/{path.relative_to(app_root).as_posix()}", content))
    for shared in shared_inputs:
        path = repo_root / shared
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f"Shared build input must be a regular file: {shared}")
        items.append((f"shared/{shared}", path.read_bytes()))
    items.append(("registry/app.json", _canonical_json_bytes(app.as_fingerprint_dict())))
    return _digest_named_files(items)


def tool_input_digest(repo_root: Path, registry_path: Path) -> str:
    tool_root = registry_path.parent
    items: list[tuple[str, bytes]] = []
    for path in _iter_source_files(tool_root):
        if path.name == "README.md":
            continue
        if path.suffix not in {".py", ".toml", ".json"}:
            continue
        items.append((path.relative_to(repo_root).as_posix(), path.read_bytes()))
    return _digest_named_files(items)


def platform_input_digest(repo_root: Path, patterns: Sequence[str]) -> str:
    """Hash the explicit source CI, test-runtime, and build configuration set."""

    repo_root = repo_root.resolve()
    matched: dict[str, bytes] = {}
    folded_names: set[str] = set()
    for pattern in patterns:
        _safe_glob_pattern(pattern, "platform build input")
        pattern_files = 0
        for path in sorted(repo_root.glob(pattern), key=lambda item: item.as_posix()):
            try:
                relative = path.relative_to(repo_root)
            except ValueError as exc:
                raise ReleaseError(
                    f"Platform build input escaped the repository: {path}"
                ) from exc
            if any(
                part in PLATFORM_INPUT_IGNORED_PARTS
                or part.endswith(".egg-info")
                or part.endswith((".pyc", ".pyo"))
                for part in relative.parts
            ):
                continue
            if path.is_symlink():
                raise ReleaseError(f"Platform build input may not be a symlink: {relative}")
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(repo_root):
                raise ReleaseError(
                    f"Platform build input escaped the repository: {relative}"
                )
            for parent in path.parents:
                if parent == repo_root:
                    break
                if parent.is_symlink():
                    raise ReleaseError(
                        f"Platform build input has a symlink parent: {relative}"
                    )
            name = relative.as_posix()
            normalized = unicodedata.normalize("NFC", name)
            folded = normalized.casefold()
            if normalized != name:
                raise ReleaseError(f"Non-canonical platform build input: {name}")
            if name not in matched and folded in folded_names:
                raise ReleaseError(f"Platform build inputs collide: {name}")
            if name not in matched:
                matched[name] = path.read_bytes()
                folded_names.add(folded)
            pattern_files += 1
        if pattern_files == 0:
            raise ReleaseError(f"Platform build input pattern matched no files: {pattern}")
    return _digest_named_files(
        (f"platform/{name}", content) for name, content in matched.items()
    )


def load_changes(directory: Path, known_apps: set[str]) -> tuple[Change, ...]:
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ReleaseError(f"Changes path is not a directory: {directory}")
    changes: list[Change] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or path.name == "README.md":
            continue
        if not path.is_file() or path.suffix != ".toml" or not SAFE_FILENAME_RE.fullmatch(path.name):
            raise ReleaseError(f"Unexpected change-fragment entry: {path.name}")
        data = _read_toml(path)
        single_entry = "app" in data or "kind" in data
        if single_entry:
            _strict_keys(
                data,
                {"schema_version", "summary", "app", "kind"},
                f"change fragment {path.name}",
                {"summary", "app", "kind"},
            )
            entries = [{"app": data["app"], "kind": data["kind"]}]
        else:
            _strict_keys(
                data,
                {"schema_version", "summary", "changes"},
                f"change fragment {path.name}",
                {"summary", "changes"},
            )
            entries = data["changes"]
        fragment_schema = data.get("schema_version", SCHEMA_VERSION)
        if type(fragment_schema) is not int or fragment_schema != SCHEMA_VERSION:
            raise ReleaseError(f"Unsupported schema in change fragment {path.name}")
        summary = data["summary"]
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > 300
            or any(ord(char) < 32 and char not in "\t" for char in summary)
        ):
            raise ReleaseError(f"Invalid summary in change fragment {path.name}")
        if not isinstance(entries, list) or not entries:
            raise ReleaseError(f"Change fragment {path.name} has no changes")
        seen_in_fragment: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ReleaseError(f"Invalid change {index} in {path.name}")
            _strict_keys(
                entry,
                {"app", "kind"},
                f"change {index} in {path.name}",
                {"app", "kind"},
            )
            app_id = entry["app"]
            kind = entry["kind"]
            if not isinstance(app_id, str) or app_id not in known_apps | {"platform"}:
                raise ReleaseError(f"Unknown app {app_id!r} in {path.name}")
            if app_id in seen_in_fragment:
                raise ReleaseError(f"Duplicate app {app_id!r} in {path.name}")
            if not isinstance(kind, str) or kind not in KIND_TO_BUMP:
                raise ReleaseError(f"Unknown change kind {kind!r} in {path.name}")
            seen_in_fragment.add(app_id)
            changes.append(
                Change(
                    fragment=path.name,
                    app_id=app_id,
                    kind=kind,
                    bump=KIND_TO_BUMP[kind],
                    summary=summary.strip(),
                )
            )
    return tuple(changes)


def _highest_bump(changes: Iterable[Change]) -> str:
    return max((change.bump for change in changes), key=BUMP_RANK.__getitem__, default="none")


def _topological_apps(apps: Sequence[AppConfig]) -> tuple[AppConfig, ...]:
    by_id = {app.app_id: app for app in apps}
    incoming = {app.app_id: len(app.dependencies) for app in apps}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for app in apps:
        for dependency in app.dependencies:
            outgoing[dependency].add(app.app_id)
    queue = deque(sorted(app_id for app_id, count in incoming.items() if count == 0))
    ordered: list[AppConfig] = []
    while queue:
        app_id = queue.popleft()
        ordered.append(by_id[app_id])
        for dependent in sorted(outgoing[app_id]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                queue.append(dependent)
    if len(ordered) != len(apps):
        raise ReleaseError("Application dependency graph contains a cycle")
    return tuple(ordered)


def _downstream_apps(apps: Sequence[AppConfig], changed: set[str]) -> list[str]:
    outgoing: dict[str, set[str]] = defaultdict(set)
    for app in apps:
        for dependency in app.dependencies:
            outgoing[dependency].add(app.app_id)
    affected = set(changed)
    queue = deque(sorted(changed))
    while queue:
        current = queue.popleft()
        for dependent in sorted(outgoing[current]):
            if dependent not in affected:
                affected.add(dependent)
                queue.append(dependent)
    return sorted(affected)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Could not read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"JSON root must be an object: {path}")
    return value


def _previous_artifacts(previous: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if previous is None:
        return {}
    artifacts = previous.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReleaseError("Previous manifest has invalid artifacts")
    result: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("component"), str):
            raise ReleaseError("Previous manifest has malformed artifact")
        component = artifact["component"]
        if component in result:
            raise ReleaseError(f"Previous manifest repeats component {component}")
        result[component] = dict(artifact)
    return result


def create_plan(
    *,
    repo_root: Path,
    registry_path: Path,
    compatibility_path: Path | None,
    changes_dir: Path,
    previous_manifest_path: Path | None,
    source_commit: str,
    test_run: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic, change-aware release plan."""

    if not COMMIT_RE.fullmatch(source_commit):
        raise ReleaseError("source_commit must be a full 40- or 64-character Git id")
    repo_root = repo_root.resolve()
    registry = load_registry(registry_path)
    compatibility_path = compatibility_path or (repo_root / registry.compatibility_file)
    compatibility, compatibility_sha = load_compatibility(compatibility_path)
    tool_sha = tool_input_digest(repo_root, registry_path)
    platform_sha = platform_input_digest(
        repo_root, registry.platform_build_inputs
    )
    changes = load_changes(changes_dir, set(registry.by_id))

    previous: dict[str, Any] | None = None
    previous_manifest_sha: str | None = None
    if previous_manifest_path is not None:
        previous = _load_json(previous_manifest_path)
        validate_manifest_data(previous)
        previous_manifest_sha = sha256_file(previous_manifest_path)
        if previous["platform_id"] != registry.platform_id:
            raise ReleaseError("Previous release belongs to a different platform")
    prior_artifacts = _previous_artifacts(previous)
    bootstrap = previous is None

    app_changes: dict[str, list[Change]] = defaultdict(list)
    for change in changes:
        app_changes[change.app_id].append(change)

    app_plans: list[dict[str, Any]] = []
    directly_changed: set[str] = set()
    for app in registry.apps:
        current_version = read_app_version(repo_root, app)
        input_sha = app_input_digest(repo_root, app, registry.shared_build_inputs)
        prior = prior_artifacts.get(app.app_id)
        if bootstrap:
            changed = True
            bump = _highest_bump(app_changes[app.app_id])
            target_version = str(SemVer.parse(current_version).bump(bump))
        else:
            if prior is None:
                changed = True
                prior_version = None
            else:
                prior_version = prior.get("version")
                if prior.get("kind") != "owned":
                    raise ReleaseError(f"Prior artifact for {app.app_id} is not owned")
                if current_version != prior_version:
                    raise ReleaseError(
                        f"{app.app_id} source version {current_version} does not match "
                        f"the previous release {prior_version}; versions are builder-owned"
                    )
                changed = prior.get("input_sha256") != input_sha
            bump = _highest_bump(app_changes[app.app_id])
            if changed and bump == "none":
                raise ReleaseError(
                    f"Changed app {app.app_id} requires a change fragment"
                )
            if not changed and bump != "none":
                raise ReleaseError(
                    f"Unchanged app {app.app_id} has a stale change fragment"
                )
            target_version = str(SemVer.parse(current_version).bump(bump))
        if changed:
            directly_changed.add(app.app_id)
        app_plans.append(
            {
                "id": app.app_id,
                "path": app.path,
                "distribution": app.distribution,
                "import_name": app.import_name,
                "django_app": app.django_app,
                "version_file": f"{app.path}/{app.version_file}",
                "dependencies": list(app.dependencies),
                "setup_commands": list(app.setup_commands),
                "current_version": current_version,
                "version": target_version,
                "bump": bump,
                "input_sha256": input_sha,
                "changed": changed,
                "build": changed,
                "changes": [change.as_dict() for change in app_changes[app.app_id]],
            }
        )

    _validate_internal_dependency_contracts(
        app_plans,
        {
            app.app_id: _source_requires_dist(repo_root, app)
            for app in registry.apps
        },
        require_registry_match=True,
        bundled_dependencies=[
            {
                "component": dependency.component,
                "distribution": dependency.distribution,
                "version": dependency.version,
            }
            for dependency in registry.dependencies
        ],
    )

    previous_build = previous.get("build", {}) if previous else {}
    platform_inputs_changed = bool(
        previous
        and (
            previous_build.get("registry_sha256") != registry.sha256
            or previous_build.get("compatibility_sha256") != compatibility_sha
            or previous_build.get("tool_sha256") != tool_sha
            or previous_build.get("platform_sha256") != platform_sha
        )
    )
    platform_entries = app_changes["platform"]
    if not bootstrap and platform_inputs_changed and not platform_entries:
        raise ReleaseError("Platform/release inputs changed without a platform fragment")
    if not bootstrap and not platform_inputs_changed and platform_entries:
        raise ReleaseError("Stale platform change fragment without platform input changes")

    release_changes = [change for change in changes if change.app_id != "platform"]
    all_bumps = list(release_changes) + list(platform_entries)
    platform_bump = "none" if bootstrap else _highest_bump(all_bumps)
    release_required = bootstrap or bool(directly_changed) or platform_inputs_changed
    if not bootstrap and release_required and platform_bump == "none":
        raise ReleaseError("A release change did not produce a platform version bump")

    previous_platform = previous["platform_version"] if previous else None
    base_platform = previous_platform or registry.initial_version
    platform_version = (
        registry.initial_version
        if bootstrap
        else str(SemVer.parse(base_platform).bump(platform_bump))
    )
    if previous and SemVer.parse(platform_version) <= SemVer.parse(previous_platform):
        if release_required:
            raise ReleaseError("Next platform version must be greater than the previous release")

    dependency_artifacts: list[dict[str, Any]] = []
    for dependency in registry.dependencies:
        prior = prior_artifacts.get(dependency.component)
        reusable = bool(
            prior
            and prior.get("kind") == "third-party"
            and normalize_distribution(prior.get("distribution", ""))
            == normalize_distribution(dependency.distribution)
            and prior.get("version") == dependency.version
            and prior.get("filename") == dependency.filename
            and prior.get("sha256") == dependency.sha256
        )
        planned_dependency = dependency.as_plan_dict("reuse" if reusable else "provide")
        if reusable and prior.get("git_blob_sha"):
            planned_dependency["git_blob_sha"] = prior["git_blob_sha"]
        dependency_artifacts.append(planned_dependency)

    return {
        "schema_version": SCHEMA_VERSION,
        "platform_id": registry.platform_id,
        "source_commit": source_commit,
        "test_run": test_run,
        "bootstrap": bootstrap,
        "release_required": release_required,
        "release_root": registry.release_root,
        "previous_platform_version": previous_platform,
        "previous_manifest": (
            {
                "path": str(previous_manifest_path),
                "sha256": previous_manifest_sha,
                "source_commit": previous.get("source_commit"),
            }
            if previous
            else None
        ),
        "platform_version": platform_version,
        "platform_bump": platform_bump,
        "build": {
            "registry_sha256": registry.sha256,
            "compatibility_sha256": compatibility_sha,
            "tool_sha256": tool_sha,
            "platform_sha256": platform_sha,
        },
        "compatibility": compatibility,
        "apps": app_plans,
        "build_matrix": [
            {"id": app["id"], "path": app["path"], "version": app["version"]}
            for app in app_plans
            if app["build"]
        ],
        "affected_tests": _downstream_apps(registry.apps, directly_changed),
        "dependency_artifacts": dependency_artifacts,
        "changes": [change.as_dict() for change in changes],
        "consumed_fragments": sorted({change.fragment for change in changes}),
    }


def write_plan(plan: Mapping[str, Any], path: Path) -> None:
    if path.exists():
        raise ReleaseError(f"Refusing to overwrite plan: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(plan))


def load_plan(path: Path) -> dict[str, Any]:
    plan = _load_json(path)
    if type(plan.get("schema_version")) is not int or plan.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseError("Unsupported release-plan schema")
    if not isinstance(plan.get("apps"), list):
        raise ReleaseError("Release plan has no applications")
    return plan


@dataclasses.dataclass(frozen=True)
class WheelInfo:
    filename: str
    distribution: str
    version: str
    size: int
    sha256: str
    requires_dist: tuple[str, ...]


def _wheel_member_key(name: str) -> str:
    if "\\" in name or any(ord(char) < 32 for char in name):
        raise ReleaseError(f"Unsafe wheel member: {name!r}")
    normalized = unicodedata.normalize("NFC", name)
    path = PurePosixPath(normalized)
    if (
        normalized != name
        or path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or str(path) != name.rstrip("/")
    ):
        raise ReleaseError(f"Unsafe wheel member: {name!r}")
    return normalized.casefold().rstrip("/")


def _record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def inspect_wheel(
    path: Path,
    *,
    expected_distribution: str | None = None,
    expected_version: str | None = None,
) -> WheelInfo:
    """Validate wheel structure, metadata, RECORD, names, and bounded sizes."""

    _validate_safe_filename(path.name, "wheel filename")
    if path.suffix != ".whl" or not path.is_file() or path.is_symlink():
        raise ReleaseError(f"Wheel is not a regular .whl file: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES:
        raise ReleaseError(f"Wheel has unsupported size: {path.name}")
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError(f"Invalid wheel archive {path.name}: {exc}") from exc
    with archive:
        members = archive.infolist()
        if not members or len(members) > MAX_WHEEL_MEMBERS:
            raise ReleaseError(f"Wheel member count is invalid: {path.name}")
        total_uncompressed = sum(member.file_size for member in members)
        if total_uncompressed > MAX_WHEEL_UNCOMPRESSED:
            raise ReleaseError(f"Wheel expands beyond the safe limit: {path.name}")
        seen: set[str] = set()
        file_names: set[str] = set()
        for member in members:
            key = _wheel_member_key(member.filename)
            if key in seen:
                raise ReleaseError(f"Wheel repeats a normalized path: {member.filename}")
            seen.add(key)
            if not member.is_dir():
                file_names.add(member.filename)
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if unix_mode and stat.S_ISLNK(unix_mode):
                raise ReleaseError(f"Wheel contains a symlink: {member.filename}")
            if member.file_size > MAX_FILE_BYTES:
                raise ReleaseError(f"Wheel member is too large: {member.filename}")
            if (
                member.file_size > 1024 * 1024
                and member.compress_size > 0
                and member.file_size / member.compress_size > MAX_WHEEL_RATIO
            ):
                raise ReleaseError(f"Wheel member compression ratio is unsafe: {member.filename}")

        metadata_names = sorted(
            name for name in file_names if name.endswith(".dist-info/METADATA")
        )
        record_names = sorted(
            name for name in file_names if name.endswith(".dist-info/RECORD")
        )
        wheel_names = sorted(name for name in file_names if name.endswith(".dist-info/WHEEL"))
        if len(metadata_names) != 1 or len(record_names) != 1 or len(wheel_names) != 1:
            raise ReleaseError(f"Wheel must contain one METADATA, WHEEL, and RECORD: {path.name}")
        dist_info = metadata_names[0].rsplit("/", 1)[0]
        if record_names[0].rsplit("/", 1)[0] != dist_info or wheel_names[0].rsplit(
            "/", 1
        )[0] != dist_info:
            raise ReleaseError(f"Wheel has multiple dist-info identities: {path.name}")

        metadata_bytes = archive.read(metadata_names[0])
        message = email.parser.BytesParser().parsebytes(metadata_bytes)
        distribution = message.get("Name")
        version = message.get("Version")
        if not distribution or not version:
            raise ReleaseError(f"Wheel metadata lacks Name or Version: {path.name}")
        requires_dist = tuple(message.get_all("Requires-Dist", []))
        if not all(isinstance(item, str) and item.strip() for item in requires_dist):
            raise ReleaseError(f"Wheel has invalid Requires-Dist metadata: {path.name}")
        normalized_distribution = normalize_distribution(distribution)
        if expected_distribution and normalized_distribution != normalize_distribution(
            expected_distribution
        ):
            raise ReleaseError(
                f"Wheel {path.name} is {distribution}, expected {expected_distribution}"
            )
        if expected_version is not None and version != expected_version:
            raise ReleaseError(
                f"Wheel {path.name} is version {version}, expected {expected_version}"
            )

        dist_info_stem = PurePosixPath(dist_info).name[: -len(".dist-info")]
        expected_stem = f"{normalized_distribution.replace('-', '_')}-{version}"
        if dist_info_stem.casefold() != expected_stem.casefold():
            raise ReleaseError(f"Wheel dist-info does not match METADATA: {path.name}")
        filename_prefix = f"{normalized_distribution.replace('-', '_')}-{version}-"
        if not path.name.casefold().startswith(filename_prefix.casefold()):
            raise ReleaseError(f"Wheel filename does not match METADATA: {path.name}")

        try:
            record_text = archive.read(record_names[0]).decode("utf-8", errors="strict")
        except (UnicodeError, KeyError) as exc:
            raise ReleaseError(f"Wheel RECORD is unreadable: {path.name}") from exc
        record_entries: dict[str, tuple[str, str]] = {}
        try:
            rows = csv.reader(io.StringIO(record_text, newline=""))
            for row in rows:
                if len(row) != 3:
                    raise ReleaseError(f"Malformed RECORD row in {path.name}")
                member_name, member_hash, member_size = row
                if member_name in record_entries:
                    raise ReleaseError(f"Duplicate RECORD entry in {path.name}: {member_name}")
                _wheel_member_key(member_name)
                record_entries[member_name] = (member_hash, member_size)
        except csv.Error as exc:
            raise ReleaseError(f"Malformed RECORD in {path.name}: {exc}") from exc
        if set(record_entries) != file_names:
            missing = sorted(file_names - set(record_entries))
            extras = sorted(set(record_entries) - file_names)
            raise ReleaseError(
                f"RECORD coverage mismatch in {path.name}; missing={missing}, extras={extras}"
            )
        for member_name in sorted(file_names):
            member_hash, member_size = record_entries[member_name]
            if member_name == record_names[0]:
                if member_hash or member_size:
                    raise ReleaseError(f"RECORD must not hash itself in {path.name}")
                continue
            content = archive.read(member_name)
            if member_hash != _record_hash(content) or member_size != str(len(content)):
                raise ReleaseError(f"RECORD verification failed: {member_name}")

    return WheelInfo(
        filename=path.name,
        distribution=distribution,
        version=version,
        size=size,
        sha256=sha256_file(path),
        requires_dist=requires_dist,
    )


def _reject_immutable_wheel_identity_collision(
    repo_root: Path, candidate: WheelInfo
) -> None:
    releases_root = repo_root.resolve() / "releases"
    if not releases_root.exists():
        return
    if not releases_root.is_dir() or releases_root.is_symlink():
        raise ReleaseError("Repository releases path must be a regular directory")
    history = sorted(releases_root.rglob("*.whl"), key=lambda path: path.as_posix())
    if len(history) > 10_000:
        raise ReleaseError("Immutable wheel history exceeds the safe scan limit")
    candidate_distribution = normalize_distribution(candidate.distribution)
    for historical_path in history:
        if historical_path.is_symlink() or not historical_path.is_file():
            raise ReleaseError(
                f"Immutable release history contains an unsafe wheel: {historical_path}"
            )
        historical = inspect_wheel(historical_path)
        if (
            normalize_distribution(historical.distribution) == candidate_distribution
            and historical.version == candidate.version
            and historical.sha256 != candidate.sha256
        ):
            relative = historical_path.relative_to(repo_root.resolve()).as_posix()
            raise ReleaseError(
                f"Wheel identity {candidate.distribution} {candidate.version} already "
                f"exists with different bytes at {relative}; bump the app version"
            )


def _replace_version(path: Path, version: str) -> None:
    SemVer.parse(version)
    content = path.read_bytes()
    replacement = rf"\g<prefix>{version}\g<suffix>".encode("ascii")
    updated, count = VERSION_ASSIGNMENT_RE.subn(replacement, content)
    if count != 1:
        raise ReleaseError(f"Could not set exactly one version in {path}")
    path.write_bytes(updated)


def build_app(
    *,
    repo_root: Path,
    registry_path: Path,
    plan: Mapping[str, Any],
    app_id: str,
    output_dir: Path,
    python: str = sys.executable,
) -> Path:
    """Build one planned app from a temporary copy, leaving source untouched."""

    registry = load_registry(registry_path)
    app = registry.by_id.get(app_id)
    if app is None:
        raise ReleaseError(f"Unknown app: {app_id}")
    planned = next((item for item in plan["apps"] if item.get("id") == app_id), None)
    if not planned or not planned.get("build"):
        raise ReleaseError(f"App is not scheduled to build: {app_id}")
    expected_digest = app_input_digest(repo_root, app, registry.shared_build_inputs)
    if expected_digest != planned.get("input_sha256"):
        raise ReleaseError(f"Source changed after planning for {app_id}")
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = set(output_dir.glob("*.whl"))
    with tempfile.TemporaryDirectory(prefix=f"buh-build-{app_id}-") as temp:
        source_copy = Path(temp) / app_id
        shutil.copytree(
            repo_root / app.path,
            source_copy,
            ignore=shutil.ignore_patterns(*IGNORED_PARTS, "*.pyc", "*.pyo", "*.egg-info"),
        )
        _replace_version(source_copy / app.version_file, planned["version"])
        environment = os.environ.copy()
        environment.setdefault("PYTHONHASHSEED", "0")
        environment.setdefault("SOURCE_DATE_EPOCH", "315532800")
        command = [
            python,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir.resolve()),
        ]
        try:
            subprocess.run(
                command,
                cwd=source_copy,
                env=environment,
                check=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReleaseError(f"Build failed for {app_id}: {exc}") from exc
    created = sorted(set(output_dir.glob("*.whl")) - existing)
    matching: list[Path] = []
    for wheel in created:
        try:
            inspect_wheel(
                wheel,
                expected_distribution=app.distribution,
                expected_version=planned["version"],
            )
        except ReleaseError:
            continue
        matching.append(wheel)
    if len(matching) != 1:
        raise ReleaseError(f"Expected one built wheel for {app_id}, found {len(matching)}")
    return matching[0]


def _validate_artifact_data(artifact: Mapping[str, Any], context: str) -> None:
    allowed = {
        "component",
        "kind",
        "distribution",
        "version",
        "filename",
        "size",
        "sha256",
        "input_sha256",
        "import_name",
        "git_blob_sha",
        "origin",
        "reused_from",
    }
    required = {
        "component",
        "kind",
        "distribution",
        "version",
        "filename",
        "size",
        "sha256",
        "origin",
    }
    _strict_keys(artifact, allowed, context, required)
    component = artifact["component"]
    if not isinstance(component, str) or not SAFE_ID_RE.fullmatch(component):
        raise ReleaseError(f"Invalid component in {context}")
    if artifact["kind"] not in {"owned", "third-party"}:
        raise ReleaseError(f"Invalid artifact kind in {context}")
    if not isinstance(artifact["distribution"], str):
        raise ReleaseError(f"Invalid distribution in {context}")
    normalize_distribution(artifact["distribution"])
    if not isinstance(artifact["version"], str) or not artifact["version"]:
        raise ReleaseError(f"Invalid version in {context}")
    _validate_safe_filename(artifact["filename"], f"filename in {context}")
    if (
        type(artifact["size"]) is not int
        or not 0 < artifact["size"] <= MAX_FILE_BYTES
    ):
        raise ReleaseError(f"Invalid artifact size in {context}")
    if not isinstance(artifact["sha256"], str) or not SHA256_RE.fullmatch(
        artifact["sha256"]
    ):
        raise ReleaseError(f"Invalid SHA-256 in {context}")
    if artifact["origin"] not in {"built", "provided", "reused"}:
        raise ReleaseError(f"Invalid artifact origin in {context}")
    if (
        artifact["kind"] == "owned"
        and artifact["origin"] == "provided"
    ) or (
        artifact["kind"] == "third-party"
        and artifact["origin"] == "built"
    ):
        raise ReleaseError(f"Artifact kind and origin conflict in {context}")
    if artifact["kind"] == "owned":
        if not isinstance(artifact.get("input_sha256"), str) or not SHA256_RE.fullmatch(
            artifact["input_sha256"]
        ):
            raise ReleaseError(f"Owned artifact lacks input digest in {context}")
        if not isinstance(artifact.get("import_name"), str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_.]*", artifact["import_name"]
        ):
            raise ReleaseError(f"Owned artifact lacks import name in {context}")
    elif "input_sha256" in artifact or "import_name" in artifact:
        raise ReleaseError(f"Third-party artifact contains owned metadata in {context}")
    if "git_blob_sha" in artifact and (
        not isinstance(artifact["git_blob_sha"], str)
        or not GIT_OBJECT_RE.fullmatch(artifact["git_blob_sha"])
    ):
        raise ReleaseError(f"Invalid Git object id in {context}")
    if artifact["origin"] == "reused":
        SemVer.parse(artifact.get("reused_from"))
    elif "reused_from" in artifact:
        raise ReleaseError(f"Non-reused artifact names a source release in {context}")


def validate_manifest_data(manifest: Mapping[str, Any]) -> None:
    _strict_keys(
        manifest,
        {
            "schema_version",
            "platform_id",
            "platform_version",
            "source_commit",
            "previous_release",
            "build",
            "compatibility",
            "artifacts",
            "install_plan",
            "changes",
        },
        "release manifest",
        {
            "schema_version",
            "platform_id",
            "platform_version",
            "source_commit",
            "previous_release",
            "build",
            "compatibility",
            "artifacts",
            "install_plan",
            "changes",
        },
    )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != SCHEMA_VERSION
    ):
        raise ReleaseError("Unsupported release manifest schema")
    if not isinstance(manifest["platform_id"], str) or not SAFE_ID_RE.fullmatch(
        manifest["platform_id"]
    ):
        raise ReleaseError("Invalid platform id in release manifest")
    SemVer.parse(manifest["platform_version"])
    if not isinstance(manifest["source_commit"], str) or not COMMIT_RE.fullmatch(
        manifest["source_commit"]
    ):
        raise ReleaseError("Invalid source commit in release manifest")
    previous = manifest["previous_release"]
    if previous is not None:
        if not isinstance(previous, dict):
            raise ReleaseError("previous_release must be an object or null")
        _strict_keys(
            previous,
            {"platform_version", "source_commit", "manifest_sha256"},
            "previous release",
            {"platform_version", "source_commit", "manifest_sha256"},
        )
        if SemVer.parse(previous["platform_version"]) >= SemVer.parse(
            manifest["platform_version"]
        ):
            raise ReleaseError("Previous release version is not older")
        if not isinstance(previous["source_commit"], str) or not COMMIT_RE.fullmatch(
            previous["source_commit"]
        ):
            raise ReleaseError("Invalid previous release source commit")
        if not isinstance(previous["manifest_sha256"], str) or not SHA256_RE.fullmatch(
            previous["manifest_sha256"]
        ):
            raise ReleaseError("Invalid previous manifest SHA-256")
    build = manifest["build"]
    if not isinstance(build, dict):
        raise ReleaseError("build must be an object")
    _strict_keys(
        build,
        {
            "registry_sha256",
            "compatibility_sha256",
            "tool_sha256",
            "platform_sha256",
            "test_run",
        },
        "build metadata",
        {
            "registry_sha256",
            "compatibility_sha256",
            "tool_sha256",
            "platform_sha256",
            "test_run",
        },
    )
    for key in (
        "registry_sha256",
        "compatibility_sha256",
        "tool_sha256",
        "platform_sha256",
    ):
        if not isinstance(build[key], str) or not SHA256_RE.fullmatch(build[key]):
            raise ReleaseError(f"Invalid {key} in build metadata")
    if build["test_run"] is not None and (
        not isinstance(build["test_run"], str)
        or len(build["test_run"]) > 200
        or any(ord(char) < 32 for char in build["test_run"])
    ):
        raise ReleaseError("Invalid test run identity")
    compatibility = manifest["compatibility"]
    if not isinstance(compatibility, dict):
        raise ReleaseError("compatibility must be an object")
    _strict_keys(
        compatibility,
        {"sha256", "values"},
        "compatibility metadata",
        {"sha256", "values"},
    )
    if not isinstance(compatibility["sha256"], str) or not SHA256_RE.fullmatch(
        compatibility["sha256"]
    ):
        raise ReleaseError("Invalid compatibility digest")
    if not isinstance(compatibility["values"], dict):
        raise ReleaseError("Compatibility values must be an object")
    if _sha256_bytes(_canonical_json_bytes(compatibility["values"])) != compatibility["sha256"]:
        raise ReleaseError("Compatibility values do not match their digest")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseError("Release manifest must contain artifacts")
    seen_components: set[str] = set()
    seen_files: set[str] = set()
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            raise ReleaseError(f"Artifact {index} must be an object")
        _validate_artifact_data(artifact, f"artifact {index}")
        component = artifact["component"]
        folded = unicodedata.normalize("NFC", artifact["filename"]).casefold()
        if component in seen_components or folded in seen_files:
            raise ReleaseError("Release manifest repeats an artifact component or filename")
        seen_components.add(component)
        seen_files.add(folded)
    install_plan = manifest["install_plan"]
    if not isinstance(install_plan, dict):
        raise ReleaseError("install_plan reference must be an object")
    _strict_keys(
        install_plan,
        {"filename", "sha256"},
        "install plan reference",
        {"filename", "sha256"},
    )
    if (
        install_plan["filename"] != "INSTALL_PLAN.json"
        or not isinstance(install_plan["sha256"], str)
        or not SHA256_RE.fullmatch(install_plan["sha256"])
    ):
        raise ReleaseError("Invalid install plan reference")
    changes = manifest["changes"]
    if not isinstance(changes, list):
        raise ReleaseError("Manifest changes must be an array")
    seen_changes: set[tuple[str, str]] = set()
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            raise ReleaseError(f"Manifest change {index} must be an object")
        _strict_keys(
            change,
            {"fragment", "app_id", "kind", "bump", "summary"},
            f"manifest change {index}",
            {"fragment", "app_id", "kind", "bump", "summary"},
        )
        _validate_safe_filename(change["fragment"], "change fragment filename")
        if not change["fragment"].endswith(".toml"):
            raise ReleaseError("Manifest change fragment must be a TOML filename")
        app_id = change["app_id"]
        kind = change["kind"]
        if not isinstance(app_id, str) or not (
            app_id == "platform" or SAFE_ID_RE.fullmatch(app_id)
        ):
            raise ReleaseError(f"Invalid app id in manifest change {index}")
        if not isinstance(kind, str) or kind not in KIND_TO_BUMP:
            raise ReleaseError(f"Invalid kind in manifest change {index}")
        if change["bump"] != KIND_TO_BUMP[kind]:
            raise ReleaseError(f"Incorrect bump in manifest change {index}")
        summary = change["summary"]
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or summary != summary.strip()
            or len(summary) > 300
            or any(ord(char) < 32 and char not in "\t" for char in summary)
        ):
            raise ReleaseError(f"Invalid summary in manifest change {index}")
        identity = (change["fragment"], app_id)
        if identity in seen_changes:
            raise ReleaseError("Manifest repeats a fragment/app change")
        seen_changes.add(identity)


def validate_install_plan_data(plan: Mapping[str, Any]) -> None:
    _strict_keys(
        plan,
        {
            "schema_version",
            "platform_version",
            "compatibility_sha256",
            "wheels",
            "django_apps",
            "setup_commands",
        },
        "install plan",
        {
            "schema_version",
            "platform_version",
            "compatibility_sha256",
            "wheels",
            "django_apps",
            "setup_commands",
        },
    )
    if type(plan["schema_version"]) is not int or plan["schema_version"] != SCHEMA_VERSION:
        raise ReleaseError("Unsupported install-plan schema")
    SemVer.parse(plan["platform_version"])
    if not isinstance(plan["compatibility_sha256"], str) or not SHA256_RE.fullmatch(
        plan["compatibility_sha256"]
    ):
        raise ReleaseError("Invalid compatibility digest in install plan")
    wheels = plan["wheels"]
    if not isinstance(wheels, list) or not wheels:
        raise ReleaseError("Install plan has no wheels")
    seen_files: set[str] = set()
    seen_components: set[str] = set()
    seen_distributions: set[str] = set()
    for index, wheel in enumerate(wheels):
        if not isinstance(wheel, dict):
            raise ReleaseError(f"Install-plan wheel {index} must be an object")
        _strict_keys(
            wheel,
            {"component", "distribution", "version", "filename", "sha256"},
            f"install-plan wheel {index}",
            {"component", "distribution", "version", "filename", "sha256"},
        )
        component = wheel["component"]
        distribution = wheel["distribution"]
        version = wheel["version"]
        filename = wheel["filename"]
        if not isinstance(component, str) or not SAFE_ID_RE.fullmatch(component):
            raise ReleaseError("Install plan contains an invalid component")
        if not isinstance(distribution, str):
            raise ReleaseError("Install plan contains an invalid distribution")
        normalized_distribution = normalize_distribution(distribution)
        if not isinstance(version, str) or not version:
            raise ReleaseError("Install plan contains an invalid version")
        _validate_safe_filename(filename, "install-plan wheel filename")
        folded_filename = filename.casefold()
        if (
            folded_filename in seen_files
            or component in seen_components
            or normalized_distribution in seen_distributions
        ):
            raise ReleaseError("Install plan repeats a wheel identity")
        seen_files.add(folded_filename)
        seen_components.add(component)
        seen_distributions.add(normalized_distribution)
        if not isinstance(wheel["sha256"], str) or not SHA256_RE.fullmatch(
            wheel["sha256"]
        ):
            raise ReleaseError("Install plan contains an invalid wheel hash")
    django_apps = plan["django_apps"]
    if not isinstance(django_apps, list) or not all(
        isinstance(item, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", item)
        for item in django_apps
    ):
        raise ReleaseError("Install plan django_apps must be a valid string array")
    setup_commands = plan["setup_commands"]
    if not isinstance(setup_commands, list) or not all(
        isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
        for item in setup_commands
    ):
        raise ReleaseError("Install plan setup_commands must be a valid string array")


def _copy_checked(source: Path, destination: Path, expected: Mapping[str, Any]) -> WheelInfo:
    if source.name != expected["filename"]:
        raise ReleaseError(f"Artifact filename mismatch: {source.name}")
    info = inspect_wheel(
        source,
        expected_distribution=expected["distribution"],
        expected_version=expected["version"],
    )
    if info.sha256 != expected["sha256"] or info.size != expected["size"]:
        raise ReleaseError(f"Reusable artifact bytes changed: {source.name}")
    shutil.copyfile(source, destination)
    os.chmod(destination, 0o644)
    return info


def _find_built_wheel(
    wheel_dir: Path, distribution: str, version: str, already_used: set[Path]
) -> tuple[Path, WheelInfo]:
    matches: list[tuple[Path, WheelInfo]] = []
    for path in sorted(wheel_dir.glob("*.whl")):
        if path in already_used:
            continue
        try:
            info = inspect_wheel(
                path,
                expected_distribution=distribution,
                expected_version=version,
            )
        except ReleaseError:
            continue
        matches.append((path, info))
    if len(matches) != 1:
        raise ReleaseError(
            f"Expected exactly one wheel for {distribution} {version}, found {len(matches)}"
        )
    already_used.add(matches[0][0])
    return matches[0]


def _ordered_app_ids(app_plans: Sequence[Mapping[str, Any]]) -> list[str]:
    configs = tuple(
        AppConfig(
            app_id=item["id"],
            path=item["path"],
            distribution=item["distribution"],
            import_name=item["import_name"],
            django_app=item["django_app"],
            version_file=item["version_file"],
            dependencies=tuple(item["dependencies"]),
            setup_commands=tuple(item["setup_commands"]),
        )
        for item in app_plans
    )
    return [app.app_id for app in _topological_apps(configs)]


def assemble_release(
    *,
    plan: Mapping[str, Any],
    wheel_dir: Path,
    previous_release_dir: Path | None,
    output_dir: Path,
    repo_root: Path | None = None,
) -> Path:
    """Assemble a new immutable release directory without touching prior ones."""

    if not plan.get("release_required"):
        raise ReleaseError("The plan contains no release")
    if output_dir.exists():
        raise ReleaseError(f"Refusing to overwrite release output: {output_dir}")
    if repo_root is None:
        raise ReleaseError("Release assembly requires the repository root")
    repo_root = repo_root.resolve()
    output_dir.mkdir(parents=True, mode=0o755)
    built_used: set[Path] = set()
    artifacts: list[dict[str, Any]] = []

    prior_by_component: dict[str, dict[str, Any]] = {}
    if plan.get("previous_manifest"):
        manifest_path = Path(plan["previous_manifest"]["path"])
        previous_manifest = _load_json(manifest_path)
        validate_manifest_data(previous_manifest)
        if sha256_file(manifest_path) != plan["previous_manifest"]["sha256"]:
            raise ReleaseError("Previous manifest changed after planning")
        prior_by_component.update(_previous_artifacts(previous_manifest))

    for app in plan["apps"]:
        if app["build"]:
            source, info = _find_built_wheel(
                wheel_dir, app["distribution"], app["version"], built_used
            )
            _reject_immutable_wheel_identity_collision(repo_root, info)
            destination = output_dir / info.filename
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o644)
            artifact: dict[str, Any] = {
                "component": app["id"],
                "kind": "owned",
                "distribution": app["distribution"],
                "version": app["version"],
                "filename": info.filename,
                "size": info.size,
                "sha256": info.sha256,
                "input_sha256": app["input_sha256"],
                "import_name": app["import_name"],
                "origin": "built",
            }
        else:
            prior = prior_by_component.get(app["id"])
            if prior is None or previous_release_dir is None:
                raise ReleaseError(f"No reusable artifact for {app['id']}")
            if (
                prior.get("kind") != "owned"
                or prior.get("version") != app["version"]
                or prior.get("input_sha256") != app["input_sha256"]
                or normalize_distribution(prior.get("distribution", ""))
                != normalize_distribution(app["distribution"])
            ):
                raise ReleaseError(f"Reusable artifact identity mismatch for {app['id']}")
            source = previous_release_dir / prior["filename"]
            destination = output_dir / prior["filename"]
            info = _copy_checked(source, destination, prior)
            artifact = dict(prior)
            artifact["origin"] = "reused"
            artifact["reused_from"] = plan["previous_platform_version"]
            artifact["size"] = info.size
            artifact["sha256"] = info.sha256
        artifacts.append(artifact)

    for dependency in sorted(
        plan.get("dependency_artifacts", []), key=lambda item: item["component"]
    ):
        action = dependency.get("action")
        if action == "reuse":
            prior = prior_by_component.get(dependency["component"])
            if prior is None or previous_release_dir is None:
                raise ReleaseError(
                    f"No prior dependency artifact for {dependency['component']}"
                )
            if any(
                prior.get(key) != dependency.get(key)
                for key in ("distribution", "version", "filename", "sha256")
            ):
                raise ReleaseError(
                    f"Reusable dependency identity mismatch for {dependency['component']}"
                )
            source = previous_release_dir / prior["filename"]
            destination = output_dir / prior["filename"]
            info = _copy_checked(source, destination, prior)
            artifact = dict(prior)
            artifact["origin"] = "reused"
            artifact["reused_from"] = plan["previous_platform_version"]
            artifact["size"] = info.size
        elif action == "provide":
            source = repo_root / dependency["source"]
            info = inspect_wheel(
                source,
                expected_distribution=dependency["distribution"],
                expected_version=dependency["version"],
            )
            if source.name != dependency["filename"] or info.sha256 != dependency["sha256"]:
                raise ReleaseError(
                    f"Provided dependency failed identity checks: {dependency['component']}"
                )
            if dependency.get("git_blob_sha"):
                _verify_git_blob_object_id(source, dependency["git_blob_sha"])
            destination = output_dir / dependency["filename"]
            shutil.copyfile(source, destination)
            os.chmod(destination, 0o644)
            artifact = {
                "component": dependency["component"],
                "kind": "third-party",
                "distribution": dependency["distribution"],
                "version": dependency["version"],
                "filename": dependency["filename"],
                "size": info.size,
                "sha256": info.sha256,
                "origin": "provided",
            }
            if dependency.get("git_blob_sha"):
                artifact["git_blob_sha"] = dependency["git_blob_sha"]
        else:
            raise ReleaseError(
                f"Invalid dependency action for {dependency.get('component')}: {action!r}"
            )
        artifacts.append(artifact)

    filenames = [artifact["filename"].casefold() for artifact in artifacts]
    if len(filenames) != len(set(filenames)):
        raise ReleaseError("Release artifacts collide by filename")

    _validate_internal_dependency_contracts(
        plan["apps"],
        {
            artifact["component"]: inspect_wheel(
                output_dir / artifact["filename"],
                expected_distribution=artifact["distribution"],
                expected_version=artifact["version"],
            ).requires_dist
            for artifact in artifacts
            if artifact["kind"] == "owned"
        },
        require_registry_match=True,
        bundled_dependencies=[
            artifact for artifact in artifacts if artifact["kind"] == "third-party"
        ],
    )

    by_component = {artifact["component"]: artifact for artifact in artifacts}
    third_party = sorted(
        (artifact for artifact in artifacts if artifact["kind"] == "third-party"),
        key=lambda item: (normalize_distribution(item["distribution"]), item["version"]),
    )
    ordered_owned = [
        by_component[app_id] for app_id in _ordered_app_ids(plan["apps"])
    ]
    install_artifacts = third_party + ordered_owned
    install_plan = {
        "schema_version": SCHEMA_VERSION,
        "platform_version": plan["platform_version"],
        "compatibility_sha256": plan["build"]["compatibility_sha256"],
        "wheels": [
            {
                "component": artifact["component"],
                "distribution": artifact["distribution"],
                "version": artifact["version"],
                "filename": artifact["filename"],
                "sha256": artifact["sha256"],
            }
            for artifact in install_artifacts
        ],
        "django_apps": [
            app["django_app"]
            for app_id in _ordered_app_ids(plan["apps"])
            for app in plan["apps"]
            if app["id"] == app_id
        ],
        "setup_commands": [
            command
            for app_id in _ordered_app_ids(plan["apps"])
            for app in plan["apps"]
            if app["id"] == app_id
            for command in app["setup_commands"]
        ],
    }
    validate_install_plan_data(install_plan)
    install_path = output_dir / "INSTALL_PLAN.json"
    install_path.write_bytes(_canonical_json_bytes(install_plan))

    previous_release = None
    if plan.get("previous_manifest"):
        previous_release = {
            "platform_version": plan["previous_platform_version"],
            "source_commit": plan["previous_manifest"]["source_commit"],
            "manifest_sha256": plan["previous_manifest"]["sha256"],
        }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "platform_id": plan["platform_id"],
        "platform_version": plan["platform_version"],
        "source_commit": plan["source_commit"],
        "previous_release": previous_release,
        "build": {
            **plan["build"],
            "test_run": plan.get("test_run"),
        },
        "compatibility": {
            "sha256": plan["build"]["compatibility_sha256"],
            "values": plan["compatibility"],
        },
        "artifacts": sorted(artifacts, key=lambda item: item["component"]),
        "install_plan": {
            "filename": install_path.name,
            "sha256": sha256_file(install_path),
        },
        "changes": plan["changes"],
    }
    validate_manifest_data(manifest)
    manifest_path = output_dir / "RELEASE.json"
    manifest_path.write_bytes(_canonical_json_bytes(manifest))

    notes: list[str] = [
        f"# B-UH platform {plan['platform_version']}",
        "",
        f"Source commit: `{plan['source_commit']}`",
        "",
        "## Changes",
        "",
    ]
    if plan["changes"]:
        for change in sorted(
            plan["changes"], key=lambda item: (item["app_id"], item["fragment"])
        ):
            notes.append(f"- **{change['app_id']} ({change['kind']}):** {change['summary']}")
    else:
        notes.append("- Source-first baseline; application behavior and versions are preserved.")
    notes.extend(
        [
            "",
            "## Artifacts",
            "",
            *[
                f"- `{item['filename']}` — {item['sha256']} ({item['origin']})"
                for item in sorted(artifacts, key=lambda artifact: artifact["filename"])
            ],
            "",
        ]
    )
    (output_dir / "README.md").write_text("\n".join(notes), encoding="utf-8")

    checked_files = sorted(
        path for path in output_dir.iterdir() if path.is_file() and path.name != "SHA256SUMS"
    )
    sums = "".join(f"{sha256_file(path)}  {path.name}\n" for path in checked_files)
    (output_dir / "SHA256SUMS").write_text(sums, encoding="ascii")
    verify_release_dir(output_dir)
    return output_dir


def _parse_checksum_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(f"Could not read SHA256SUMS: {exc}") from exc
    if not text.endswith("\n"):
        raise ReleaseError("SHA256SUMS must end with a newline")
    result: dict[str, str] = {}
    previous_name: str | None = None
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,254})", line)
        if not match:
            raise ReleaseError(f"Malformed SHA256SUMS line: {line!r}")
        digest, filename = match.groups()
        _validate_safe_filename(filename, "checksum filename")
        if filename == "SHA256SUMS" or filename in result:
            raise ReleaseError("SHA256SUMS contains itself or a duplicate")
        if previous_name is not None and filename <= previous_name:
            raise ReleaseError("SHA256SUMS must be sorted by filename")
        previous_name = filename
        result[filename] = digest
    if not result:
        raise ReleaseError("SHA256SUMS is empty")
    return result


def verify_release_dir(release_dir: Path) -> dict[str, Any]:
    """Fail closed unless a complete release directory is internally consistent."""

    if not release_dir.is_dir() or release_dir.is_symlink():
        raise ReleaseError(f"Release directory is not a regular directory: {release_dir}")
    entries = sorted(release_dir.iterdir(), key=lambda item: item.name)
    if not entries or len(entries) > MAX_RELEASE_FILES:
        raise ReleaseError("Release file count exceeds the safe limit")
    total_size = 0
    folded_names: set[str] = set()
    for path in entries:
        _validate_safe_filename(path.name, "release filename")
        folded = unicodedata.normalize("NFC", path.name).casefold()
        if folded in folded_names:
            raise ReleaseError(f"Release filenames collide: {path.name}")
        folded_names.add(folded)
        if not path.is_file() or path.is_symlink():
            raise ReleaseError(f"Release contains a non-regular file: {path.name}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ReleaseError(f"Release file is too large: {path.name}")
        total_size += size
    if total_size > MAX_RELEASE_BYTES:
        raise ReleaseError("Release expands beyond the safe total size")

    required = {"RELEASE.json", "INSTALL_PLAN.json", "README.md", "SHA256SUMS"}
    missing = required - {path.name for path in entries}
    if missing:
        raise ReleaseError(f"Release is missing required files: {sorted(missing)}")
    checksums = _parse_checksum_file(release_dir / "SHA256SUMS")
    actual_checked = {path.name for path in entries if path.name != "SHA256SUMS"}
    if set(checksums) != actual_checked:
        raise ReleaseError("Every release payload must occur exactly once in SHA256SUMS")
    for filename, expected in checksums.items():
        if sha256_file(release_dir / filename) != expected:
            raise ReleaseError(f"SHA-256 mismatch for {filename}")

    manifest_path = release_dir / "RELEASE.json"
    manifest = _load_json(manifest_path)
    if manifest_path.read_bytes() != _canonical_json_bytes(manifest):
        raise ReleaseError("RELEASE.json is not canonical JSON")
    validate_manifest_data(manifest)
    plan_path = release_dir / "INSTALL_PLAN.json"
    install_plan = _load_json(plan_path)
    if plan_path.read_bytes() != _canonical_json_bytes(install_plan):
        raise ReleaseError("INSTALL_PLAN.json is not canonical JSON")
    validate_install_plan_data(install_plan)
    if (
        manifest["install_plan"]["sha256"] != sha256_file(plan_path)
        or install_plan["platform_version"] != manifest["platform_version"]
        or install_plan["compatibility_sha256"] != manifest["compatibility"]["sha256"]
    ):
        raise ReleaseError("Install plan does not match the release manifest")

    artifact_by_file = {item["filename"]: item for item in manifest["artifacts"]}
    expected_files = required | set(artifact_by_file)
    if {path.name for path in entries} != expected_files:
        extras = sorted({path.name for path in entries} - expected_files)
        raise ReleaseError(f"Release contains undeclared files: {extras}")
    owned_wheel_info: dict[str, WheelInfo] = {}
    for filename, artifact in artifact_by_file.items():
        path = release_dir / filename
        if path.stat().st_size != artifact["size"] or sha256_file(path) != artifact["sha256"]:
            raise ReleaseError(f"Artifact does not match manifest: {filename}")
        info = inspect_wheel(
            path,
            expected_distribution=artifact["distribution"],
            expected_version=artifact["version"],
        )
        if artifact.get("git_blob_sha"):
            _verify_git_blob_object_id(path, artifact["git_blob_sha"])
        if artifact["kind"] == "owned":
            owned_wheel_info[artifact["component"]] = info

    owned_artifacts = [
        {
            "id": artifact["component"],
            "distribution": artifact["distribution"],
            "version": artifact["version"],
        }
        for artifact in manifest["artifacts"]
        if artifact["kind"] == "owned"
    ]
    _validate_internal_dependency_contracts(
        owned_artifacts,
        {
            component: info.requires_dist
            for component, info in owned_wheel_info.items()
        },
        require_registry_match=False,
        bundled_dependencies=[
            artifact
            for artifact in manifest["artifacts"]
            if artifact["kind"] == "third-party"
        ],
    )

    planned_wheels = install_plan["wheels"]
    if {item["filename"] for item in planned_wheels} != set(artifact_by_file):
        raise ReleaseError("Install plan and manifest list different wheels")
    for wheel in planned_wheels:
        artifact = artifact_by_file[wheel["filename"]]
        for key in ("component", "distribution", "version", "filename", "sha256"):
            if wheel[key] != artifact[key]:
                raise ReleaseError(f"Install plan identity mismatch for {wheel['filename']}")
    return manifest


def validate_isolated_install(
    release_dir: Path, *, python: str = sys.executable
) -> dict[str, Any]:
    """Install every declared wheel into a fresh offline virtual environment.

    This proves that the exact verified wheel set is installable without
    consulting a package index. Dependencies outside the release bundle are
    deliberately ignored here; the compatibility test environment owns their
    integration coverage. Application imports and Django setup commands are
    likewise left to that environment because importing them can require live
    settings and running setup commands can mutate state.
    """

    release_dir = release_dir.resolve()
    manifest = verify_release_dir(release_dir)
    install_plan = _load_json(release_dir / manifest["install_plan"]["filename"])
    expected = {
        wheel["distribution"]: wheel["version"] for wheel in install_plan["wheels"]
    }
    if len(expected) != len(install_plan["wheels"]):
        raise ReleaseError("Install plan repeats a distribution")

    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PIP_REQUIRE_VIRTUALENV": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    wheel_paths = [
        str(release_dir / wheel["filename"]) for wheel in install_plan["wheels"]
    ]
    validation_program = (
        "import importlib.metadata,json,sys;"
        "expected=json.loads(sys.argv[1]);"
        "actual={name:importlib.metadata.version(name) for name in expected};"
        "assert actual==expected,(actual,expected);"
        "print(json.dumps(actual,sort_keys=True))"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="buh-release-install-") as temp:
            environment_root = Path(temp) / "venv"
            subprocess.run(
                [python, "-m", "venv", str(environment_root)],
                check=True,
                text=True,
                capture_output=True,
                env=environment,
            )
            environment_python = (
                environment_root / "Scripts" / "python.exe"
                if os.name == "nt"
                else environment_root / "bin" / "python"
            )
            if not environment_python.is_file():
                raise ReleaseError("Virtual environment did not contain Python")
            subprocess.run(
                [
                    str(environment_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--no-deps",
                    "--no-compile",
                    *wheel_paths,
                ],
                check=True,
                text=True,
                capture_output=True,
                env=environment,
            )
            checked = subprocess.run(
                [
                    str(environment_python),
                    "-I",
                    "-c",
                    validation_program,
                    json.dumps(expected, sort_keys=True, separators=(",", ":")),
                ],
                check=True,
                text=True,
                capture_output=True,
                env=environment,
            )
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or exc.stdout or "").strip()[-4000:]
        suffix = f": {diagnostic}" if diagnostic else ""
        raise ReleaseError(
            f"Isolated release installation failed with exit {exc.returncode}{suffix}"
        ) from exc
    try:
        installed = json.loads(checked.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError("Isolated installation returned invalid validation data") from exc
    if installed != expected:
        raise ReleaseError("Isolated installation reported unexpected distributions")
    return {
        "platform_version": manifest["platform_version"],
        "artifact_count": len(install_plan["wheels"]),
        "distributions": installed,
    }


def version_updates(plan: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return exact source edits a one-commit publisher must include."""

    updates = []
    for app in plan["apps"]:
        if app["current_version"] != app["version"]:
            updates.append(
                {
                    "app": app["id"],
                    "path": app["version_file"],
                    "from": app["current_version"],
                    "to": app["version"],
                }
            )
    return updates


def validate_append_only_changes(
    changes: Iterable[tuple[str, str]], *, new_release_path: str
) -> None:
    """Validate Git name-status pairs before an atomic release commit.

    Existing release entries may never be changed, removed, renamed, or added
    to. The only permitted release-tree change is adding files beneath exactly
    one previously nonexistent version directory.
    """

    new_release_path = _safe_relative_path(new_release_path.rstrip("/"), "new release path")
    if not re.search(r"/v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$", new_release_path):
        raise ReleaseError("New release path must end in a strict vMAJOR.MINOR.PATCH")
    prefix = new_release_path + "/"
    additions = 0
    for status, path in changes:
        path = _safe_relative_path(path, "changed path")
        release_related = path == "releases" or path.startswith("releases/")
        if not release_related:
            continue
        if status != "A" or not path.startswith(prefix):
            raise ReleaseError(f"Existing release history is append-only: {status} {path}")
        additions += 1
    if additions == 0:
        raise ReleaseError("Release commit does not add its new release directory")


def _resolve_path(root: Path, value: str | None, default: Path | None = None) -> Path | None:
    if value is None:
        return default
    path = Path(value)
    return path if path.is_absolute() else root / path


def _cmd_plan(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    registry_path = _resolve_path(root, args.registry, root / "ops/release/apps.toml")
    changes_dir = _resolve_path(root, args.changes, root / "changes")
    previous = _resolve_path(root, args.previous)
    compatibility = _resolve_path(root, args.compatibility)
    assert registry_path is not None and changes_dir is not None
    plan = create_plan(
        repo_root=root,
        registry_path=registry_path,
        compatibility_path=compatibility,
        changes_dir=changes_dir,
        previous_manifest_path=previous,
        source_commit=args.source_sha,
        test_run=args.test_run,
    )
    output = Path(args.output)
    write_plan(plan, output)
    print(
        json.dumps(
            {
                "release_required": plan["release_required"],
                "platform_version": plan["platform_version"],
                "build_matrix": plan["build_matrix"],
                "affected_tests": plan["affected_tests"],
            },
            sort_keys=True,
        )
    )


def _cmd_build(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    registry = _resolve_path(root, args.registry, root / "ops/release/apps.toml")
    assert registry is not None
    wheel = build_app(
        repo_root=root,
        registry_path=registry,
        plan=load_plan(Path(args.plan)),
        app_id=args.app,
        output_dir=Path(args.output_dir),
        python=args.python,
    )
    print(wheel)


def _cmd_assemble(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    release = assemble_release(
        plan=load_plan(Path(args.plan)),
        wheel_dir=Path(args.wheel_dir),
        previous_release_dir=Path(args.previous_dir) if args.previous_dir else None,
        output_dir=Path(args.output_dir),
        repo_root=root,
    )
    print(release)


def _cmd_verify(args: argparse.Namespace) -> None:
    manifest = verify_release_dir(Path(args.release_dir))
    print(
        json.dumps(
            {
                "platform_version": manifest["platform_version"],
                "source_commit": manifest["source_commit"],
                "artifacts": len(manifest["artifacts"]),
            },
            sort_keys=True,
        )
    )


def _cmd_validate_install(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            validate_isolated_install(
                Path(args.release_dir), python=args.python
            ),
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="plan a change-aware release")
    plan.add_argument("--root", default=".")
    plan.add_argument("--registry")
    plan.add_argument("--compatibility")
    plan.add_argument("--changes")
    plan.add_argument("--previous")
    plan.add_argument("--source-sha", required=True)
    plan.add_argument("--test-run")
    plan.add_argument("--output", required=True)
    plan.set_defaults(func=_cmd_plan)

    build = subparsers.add_parser("build", help="build one app from a release plan")
    build.add_argument("--root", default=".")
    build.add_argument("--registry")
    build.add_argument("--plan", required=True)
    build.add_argument("--app", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--python", default=sys.executable)
    build.set_defaults(func=_cmd_build)

    assemble = subparsers.add_parser("assemble", help="assemble and verify a full release")
    assemble.add_argument("--root", default=".")
    assemble.add_argument("--plan", required=True)
    assemble.add_argument("--wheel-dir", required=True)
    assemble.add_argument("--previous-dir")
    assemble.add_argument("--output-dir", required=True)
    assemble.set_defaults(func=_cmd_assemble)

    verify = subparsers.add_parser("verify", help="verify an assembled release")
    verify.add_argument("--release-dir", required=True)
    verify.set_defaults(func=_cmd_verify)

    validate_install = subparsers.add_parser(
        "validate-install", help="install the verified bundle in a fresh offline venv"
    )
    validate_install.add_argument("--release-dir", required=True)
    validate_install.add_argument("--python", default=sys.executable)
    validate_install.set_defaults(func=_cmd_validate_install)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReleaseError as exc:
        parser.exit(2, f"release error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
