#!/usr/bin/env python3
"""Generate and verify the source-test supply-chain lock contract.

Normal CI uses ``verify`` and installs committed hash locks.  ``refresh-locks``
and ``refresh-images`` are maintainer/update-workflow operations: they contact
PyPI or the configured public container registries and only ever propose source
changes for review.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path
from typing import Iterable
from zipfile import ZipFile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
COMPATIBILITY = ROOT / "platform" / "compatibility.toml"
REQUIREMENTS = ROOT / "platform" / "requirements"
IMAGE_ENV = ROOT / "platform" / "testenv" / "env.example"
IMAGE_CONSUMERS = (
    IMAGE_ENV,
    ROOT / "platform" / "testenv" / "Dockerfile",
    ROOT / "platform" / "testenv" / "Dockerfile.playwright",
    ROOT / "platform" / "testenv" / "compose.yml",
)
LOCKS = {
    "build": REQUIREMENTS / "build.lock",
    "production": REQUIREMENTS / "production.lock",
    "test": REQUIREMENTS / "test.lock",
}
INPUTS = {
    "build": REQUIREMENTS / "build.txt",
    "production": REQUIREMENTS / "production.txt",
    "test": REQUIREMENTS / "test.txt",
}
LOCK_ENTRY = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s*\\?$")
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s*\\)?$")
IMAGE_DIGEST = re.compile(r"sha256:([0-9a-f]{64})$")
ACCEPT_MANIFESTS = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
REGISTRY_HOSTS = frozenset(
    {"auth.docker.io", "registry-1.docker.io", "mcr.microsoft.com"}
)


class SupplyChainError(RuntimeError):
    """A fail-closed supply-chain contract violation."""


def _compatibility() -> dict:
    with COMPATIBILITY.open("rb") as stream:
        return tomllib.load(stream)


def _exact_inputs(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        raise SupplyChainError(f"Recursive requirement include: {path}")
    seen.add(path)
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            nested = _exact_inputs(path.parent / line[3:].strip(), seen)
            overlap = set(result) & set(nested)
            if overlap:
                raise SupplyChainError(
                    f"Duplicate included requirements: {', '.join(sorted(overlap))}"
                )
            result.update(nested)
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise SupplyChainError(f"Requirement input is not an exact pin: {raw_line!r}")
        name = canonicalize_name(match.group(1))
        if name in result:
            raise SupplyChainError(f"Duplicate direct requirement: {name}")
        result[name] = match.group(2)
    seen.remove(path)
    return result


def _lock_entries(path: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    entries: dict[str, tuple[str, tuple[str, ...]]] = {}
    current: str | None = None
    version = ""
    hashes: list[str] = []

    def finish() -> None:
        nonlocal current, version, hashes
        if current is None:
            return
        if not hashes:
            raise SupplyChainError(f"{path}: {current} has no SHA-256 hashes")
        if hashes != sorted(set(hashes)):
            raise SupplyChainError(f"{path}: {current} hashes are duplicate or unsorted")
        entries[current] = (version, tuple(hashes))
        current = None
        version = ""
        hashes = []

    previous_name = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line and not raw_line[0].isspace():
            finish()
            match = LOCK_ENTRY.fullmatch(raw_line)
            if not match:
                raise SupplyChainError(f"{path}: unsafe lock entry: {raw_line!r}")
            current = canonicalize_name(match.group(1))
            version = match.group(2)
            if current in entries:
                raise SupplyChainError(f"{path}: duplicate lock entry: {current}")
            if previous_name and current <= previous_name:
                raise SupplyChainError(f"{path}: lock entries are not sorted")
            previous_name = current
        else:
            if current is None:
                raise SupplyChainError(f"{path}: orphan lock continuation: {raw_line!r}")
            match = HASH.fullmatch(stripped)
            if not match:
                raise SupplyChainError(f"{path}: unsafe lock continuation: {raw_line!r}")
            hashes.append(match.group(1))
    finish()
    if not entries:
        raise SupplyChainError(f"Empty dependency lock: {path}")
    return entries


def _target_marker_environment(contract: dict) -> dict[str, str]:
    python = contract["supply_chain"]["lock_python"]
    environment = default_environment()
    environment.update(
        {
            "python_full_version": python,
            "python_version": ".".join(python.split(".")[:2]),
            "implementation_name": "cpython",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "sys_platform": "linux",
            "extra": "",
        }
    )
    return environment


def _active(requirement: Requirement, environment: dict[str, str]) -> bool:
    return requirement.marker is None or requirement.marker.evaluate(environment)


def _app_version(path: Path, module: str) -> Version:
    init = path / "src" / module / "__init__.py"
    match = re.search(
        r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$",
        init.read_text(encoding="utf-8"),
    )
    if not match:
        raise SupplyChainError(f"Cannot read owned application version from {init}")
    return Version(match.group(1))


def _owned_requirements(contract: dict) -> tuple[dict[str, Version], list[Requirement]]:
    owned: dict[str, Version] = {}
    requirements: list[Requirement] = []
    for app in contract["applications"].values():
        app_path = ROOT / app["path"]
        with (app_path / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        distribution = canonicalize_name(project["name"])
        owned[distribution] = _app_version(app_path, app["module"])
        requirements.extend(Requirement(value) for value in project.get("dependencies", []))
    return owned, requirements


def _bundled_contract(
    contract: dict,
) -> tuple[dict[str, Version], list[Requirement]]:
    requirements: list[Requirement] = []
    expected = {
        canonicalize_name(name.replace("_", "-")): Version(version)
        for name, version in contract["third_party"].items()
    }
    with (ROOT / "ops" / "release" / "apps.toml").open("rb") as stream:
        registry = tomllib.load(stream)
    dependencies = registry.get("dependencies")
    if not isinstance(dependencies, list):
        raise SupplyChainError("Release registry has no dependency array")
    registered = {
        canonicalize_name(item["distribution"]): item for item in dependencies
    }
    if set(registered) != set(expected):
        raise SupplyChainError(
            "Release registry dependencies differ from compatibility.toml"
        )
    found: dict[str, Version] = {}
    for name, expected_version in expected.items():
        item = registered[name]
        version = Version(item["version"])
        if version != expected_version:
            raise SupplyChainError(
                f"Release registry version for {name} differs from compatibility.toml"
            )
        source = item.get("source")
        digest = item.get("sha256")
        if not isinstance(source, str) or not isinstance(digest, str):
            raise SupplyChainError(f"Release registry has invalid source/hash for {name}")
        wheel = (ROOT / source).resolve()
        try:
            wheel.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise SupplyChainError(f"Bundled wheel escaped the repository: {source}") from exc
        if not wheel.is_file() or wheel.is_symlink():
            raise SupplyChainError(f"Bundled wheel is not a regular file: {source}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SupplyChainError(f"Bundled wheel has an invalid SHA-256: {source}")
        actual_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if actual_digest != digest:
            raise SupplyChainError(f"Bundled wheel checksum mismatch: {source}")
        with ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise SupplyChainError(f"Wheel has invalid metadata layout: {wheel}")
            metadata = BytesParser(policy=compat32).parsebytes(
                archive.read(metadata_names[0])
            )
        metadata_name = canonicalize_name(metadata["Name"])
        metadata_version = Version(metadata["Version"])
        if metadata_name != name or metadata_version != version:
            raise SupplyChainError(f"Bundled wheel metadata mismatch: {source}")
        found[name] = metadata_version
        requirements.extend(
            Requirement(value) for value in metadata.get_all("Requires-Dist", [])
        )
    if found != expected:
        raise SupplyChainError(
            f"Bundled wheels differ from compatibility.toml: expected {expected}, found {found}"
        )
    return found, requirements


def _external_constraints(contract: dict) -> list[str]:
    environment = _target_marker_environment(contract)
    owned, app_requirements = _owned_requirements(contract)
    bundled, bundled_requirements = _bundled_contract(contract)
    provided = {**owned, **bundled}
    all_requirements = app_requirements + bundled_requirements
    external: set[str] = set()
    for requirement in all_requirements:
        if not _active(requirement, environment):
            continue
        name = canonicalize_name(requirement.name)
        if name in provided:
            if provided[name] not in requirement.specifier:
                raise SupplyChainError(
                    f"Provided dependency {requirement} rejects version {provided[name]}"
                )
            continue
        external.add(str(requirement))
    return sorted(external, key=str.casefold)


def _verify_dependency_contract(
    contract: dict, lock: dict[str, tuple[str, tuple[str, ...]]]
) -> None:
    environment = _target_marker_environment(contract)
    owned, app_requirements = _owned_requirements(contract)
    bundled, bundled_requirements = _bundled_contract(contract)
    provided = {**owned, **bundled}
    for requirement in app_requirements + bundled_requirements:
        if not _active(requirement, environment):
            continue
        name = canonicalize_name(requirement.name)
        if name in provided:
            version = provided[name]
        else:
            if name not in lock:
                raise SupplyChainError(f"Lock omits required dependency: {requirement}")
            version = Version(lock[name][0])
        if version not in requirement.specifier:
            raise SupplyChainError(
                f"Locked/source version {version} does not satisfy {requirement}"
            )


def _verify_build_systems(
    contract: dict, build_lock: dict[str, tuple[str, tuple[str, ...]]]
) -> None:
    expected = _exact_inputs(INPUTS["build"])
    for name, version in expected.items():
        if name not in build_lock or build_lock[name][0] != version:
            raise SupplyChainError(
                f"build.lock does not preserve build pin {name}=={version}"
            )
    for app in contract["applications"].values():
        pyproject = ROOT / app["path"] / "pyproject.toml"
        with pyproject.open("rb") as stream:
            build = tomllib.load(stream)["build-system"]
        actual: dict[str, str] = {}
        for value in build["requires"]:
            requirement = Requirement(value)
            name = canonicalize_name(requirement.name)
            if requirement.extras or requirement.marker is not None or requirement.url:
                raise SupplyChainError(
                    f"{pyproject}: unsafe build dependency form: {value}"
                )
            if name in actual:
                raise SupplyChainError(
                    f"{pyproject}: duplicate normalized build dependency: {name}"
                )
            versions = list(requirement.specifier)
            if len(versions) != 1 or versions[0].operator != "==":
                raise SupplyChainError(f"{pyproject}: build dependency is not exact: {value}")
            actual[name] = versions[0].version
        expected_app = {
            name: expected[name] for name in ("setuptools", "wheel")
        }
        if actual != expected_app:
            raise SupplyChainError(
                f"{pyproject}: build dependencies differ from build.txt: {actual}"
            )


def _image_values() -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in IMAGE_ENV.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name or not value or name in result:
            raise SupplyChainError(f"Malformed image registry line: {raw_line!r}")
        result[name] = value
    return result


def _split_image(reference: str) -> tuple[str, str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_@+-]{1,255}", reference):
        raise SupplyChainError(f"Container image has unsafe syntax: {reference!r}")
    tagged, separator, digest = reference.rpartition("@")
    if not separator or not IMAGE_DIGEST.fullmatch(digest):
        raise SupplyChainError(f"Container image is not SHA-256 pinned: {reference}")
    name, separator, tag = tagged.rpartition(":")
    if not separator or not name or not tag or tag.casefold() == "latest":
        raise SupplyChainError(f"Container image lacks a reviewed non-latest tag: {reference}")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise SupplyChainError(f"Container image has an unsafe tag: {reference}")
    docker_hub_name = re.fullmatch(
        r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*",
        name,
    )
    mcr_name = re.fullmatch(
        r"mcr\.microsoft\.com/[a-z0-9]+(?:[._-][a-z0-9]+)*",
        name,
    )
    if not docker_hub_name and not mcr_name:
        raise SupplyChainError(f"Container image uses an unapproved registry/name: {reference}")
    return name, tag, digest


class _SameOriginHTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if new.scheme != "https" or new.hostname != old.hostname:
            raise SupplyChainError(
                f"Registry redirect left its HTTPS origin: {req.full_url} -> {newurl}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(request: urllib.request.Request):
    requested = urllib.parse.urlsplit(request.full_url)
    if requested.scheme != "https" or requested.hostname not in REGISTRY_HOSTS:
        raise SupplyChainError(f"Unapproved registry URL: {request.full_url}")
    opener = urllib.request.build_opener(_SameOriginHTTPSRedirect())
    try:
        response = opener.open(request, timeout=30)
    except Exception as exc:  # urllib exposes several transport-specific subclasses
        raise SupplyChainError(f"Registry request failed for {request.full_url}: {exc}") from exc
    final = urllib.parse.urlsplit(response.geturl())
    if final.scheme != "https" or final.hostname not in REGISTRY_HOSTS:
        response.close()
        raise SupplyChainError(f"Registry response escaped the allowlist: {response.geturl()}")
    return response


def _allowed_sdists(contract: dict) -> tuple[str, ...]:
    raw = contract["supply_chain"]["allowed_source_distributions"]
    if not isinstance(raw, list) or not raw:
        raise SupplyChainError("allowed_source_distributions must be a non-empty array")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", value
        ):
            raise SupplyChainError(f"Unsafe source-distribution exception: {value!r}")
        if canonicalize_name(value) != value:
            raise SupplyChainError(f"Non-canonical source-distribution exception: {value}")
        values.append(value)
    if values != sorted(set(values)):
        raise SupplyChainError("Source-distribution exceptions must be unique and sorted")
    return tuple(values)


def _remote_digest(reference: str) -> str:
    name, tag, _ = _split_image(reference)
    first = name.split("/", 1)[0]
    if "." not in first and ":" not in first and first != "localhost":
        repository = name if "/" in name else f"library/{name}"
        query = urllib.parse.urlencode(
            {
                "service": "registry.docker.io",
                "scope": f"repository:{repository}:pull",
            }
        )
        token_request = urllib.request.Request(
            f"https://auth.docker.io/token?{query}",
            headers={"Accept": "application/json"},
        )
        with _request(token_request) as response:
            token = json.loads(response.read())["token"]
        url = f"https://registry-1.docker.io/v2/{repository}/manifests/{tag}"
        headers = {"Accept": ACCEPT_MANIFESTS, "Authorization": f"Bearer {token}"}
    else:
        registry, repository = name.split("/", 1)
        if registry != "mcr.microsoft.com":
            raise SupplyChainError(f"Unapproved image registry: {registry}")
        url = f"https://{registry}/v2/{repository}/manifests/{tag}"
        headers = {"Accept": ACCEPT_MANIFESTS}
    request = urllib.request.Request(url, method="HEAD", headers=headers)
    with _request(request) as response:
        digest = response.headers.get("Docker-Content-Digest", "")
    if not IMAGE_DIGEST.fullmatch(digest):
        raise SupplyChainError(f"Registry returned an invalid manifest digest for {name}:{tag}")
    return digest


def verify() -> None:
    contract = _compatibility()
    _allowed_sdists(contract)
    build_lock = _lock_entries(LOCKS["build"])
    production_lock = _lock_entries(LOCKS["production"])
    test_lock = _lock_entries(LOCKS["test"])
    _verify_build_systems(contract, build_lock)
    for kind in ("production", "test"):
        direct = _exact_inputs(INPUTS[kind])
        lock = production_lock if kind == "production" else test_lock
        for name, version in direct.items():
            if name not in lock or lock[name][0] != version:
                raise SupplyChainError(
                    f"{kind}.lock does not preserve direct pin {name}=={version}"
                )
    for name, (version, _hashes) in production_lock.items():
        if name not in test_lock or test_lock[name][0] != version:
            raise SupplyChainError(
                f"test.lock is not a compatible superset of production.lock: {name}=={version}"
            )
    _verify_dependency_contract(contract, production_lock)

    expected_python = contract["supply_chain"]["lock_python"]
    if contract["runtime"]["python"] != expected_python:
        raise SupplyChainError("Runtime Python and lock target Python differ")
    for name, reference in _image_values().items():
        _split_image(reference)
        occurrences = sum(
            path.read_text(encoding="utf-8").count(reference) for path in IMAGE_CONSUMERS
        )
        if occurrences < 2:
            raise SupplyChainError(f"Pinned image is not consumed by test environment: {name}")


def _uv_version(contract: dict) -> None:
    expected = contract["tooling"]["uv"]
    try:
        actual = importlib.metadata.version("uv")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SupplyChainError("The exactly pinned uv package must be installed") from exc
    if actual != expected:
        raise SupplyChainError(f"uv {actual} is installed; the contract requires {expected}")


def _compile_lock(
    *,
    contract: dict,
    sources: Iterable[Path],
    output: Path,
    constraints: Path | None = None,
) -> bytes:
    command = [
        sys.executable,
        "-m",
        "uv",
        "pip",
        "compile",
        *(str(path) for path in sources),
    ]
    if constraints is not None:
        command.append(str(constraints))
    command.extend(
        (
            "--output-file",
            str(output),
            "--format",
            "requirements.txt",
            "--python-version",
            contract["supply_chain"]["lock_python"],
            "--python-platform",
            contract["supply_chain"]["lock_platform"],
            "--only-binary",
            ":all:",
            "--generate-hashes",
            "--no-emit-index-url",
            "--no-annotate",
            "--custom-compile-command",
            "python ops/supply_chain.py refresh-locks",
            "--refresh",
            "--quiet",
        )
    )
    for distribution in _allowed_sdists(contract):
        command.extend(("--no-binary", distribution))
    try:
        subprocess.run(command, cwd=ROOT, check=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SupplyChainError(f"Dependency lock resolution failed: {exc}") from exc
    return output.read_bytes()


def refresh_locks(*, check: bool) -> None:
    contract = _compatibility()
    _uv_version(contract)
    constraints = _external_constraints(contract)
    with tempfile.TemporaryDirectory(prefix="buh-lock-") as temporary:
        temp = Path(temporary)
        constraint_path = temp / "owned-and-bundled.txt"
        constraint_path.write_text("\n".join(constraints) + "\n", encoding="utf-8")
        generated = {
            "build": _compile_lock(
                contract=contract,
                sources=(INPUTS["build"],),
                output=temp / "build.lock",
            ),
            "production": _compile_lock(
                contract=contract,
                sources=(INPUTS["production"],),
                constraints=constraint_path,
                output=temp / "production.lock",
            ),
            "test": _compile_lock(
                contract=contract,
                sources=(INPUTS["test"],),
                constraints=constraint_path,
                output=temp / "test.lock",
            ),
        }
    changed = [
        name
        for name, data in generated.items()
        if not LOCKS[name].is_file() or LOCKS[name].read_bytes() != data
    ]
    if check and changed:
        raise SupplyChainError(f"Dependency locks require refresh: {', '.join(changed)}")
    if not check:
        for name, data in generated.items():
            LOCKS[name].write_bytes(data)
    verify()


def refresh_images(*, check: bool) -> None:
    values = _image_values()
    replacements: dict[str, str] = {}
    for reference in values.values():
        name, tag, old_digest = _split_image(reference)
        new_digest = _remote_digest(reference)
        if new_digest != old_digest:
            replacements[reference] = f"{name}:{tag}@{new_digest}"
    if check and replacements:
        raise SupplyChainError(
            "Container tags moved and require a reviewed digest update: "
            + ", ".join(sorted(replacements))
        )
    if not check:
        for path in IMAGE_CONSUMERS:
            text = path.read_text(encoding="utf-8")
            for old, new in replacements.items():
                text = text.replace(old, new)
            path.write_text(text, encoding="utf-8")
    verify()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("verify", help="verify committed locks and image pins offline")
    locks = subparsers.add_parser("refresh-locks", help="resolve locks from PyPI metadata")
    locks.add_argument("--check", action="store_true", help="fail instead of writing changes")
    images = subparsers.add_parser(
        "refresh-images", help="resolve current tags from their official registries"
    )
    images.add_argument("--check", action="store_true", help="fail instead of writing changes")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify()
        elif args.command == "refresh-locks":
            refresh_locks(check=args.check)
        else:
            refresh_images(check=args.check)
    except SupplyChainError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
