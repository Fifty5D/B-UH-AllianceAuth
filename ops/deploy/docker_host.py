"""Docker Compose host adapter for an AllianceAuth Platform v2 deployment."""

from __future__ import annotations

import gzip
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    IMAGE_DIGEST_RE,
    SHA256_RE,
    DeploymentError,
    ReceiverConfig,
    ValidatedBundle,
    canonical_json_bytes,
    sha256_file,
)


BEGIN_V2 = "# BEGIN B-UH PLATFORM V2"
END_V2 = "# END B-UH PLATFORM V2"
BEGIN_LEGACY = "# BEGIN B-UH MOON TAX PLATFORM"
END_LEGACY = "# END B-UH MOON TAX PLATFORM"
FATAL_LOG_RE = re.compile(
    r"ModuleNotFoundError|Worker failed to boot|ImproperlyConfigured|"
    r"ImportError:|SyntaxError:|django\.db\.migrations\.exceptions",
    re.IGNORECASE,
)
SAFE_DATABASE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SAFE_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_IMAGE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]{0,511}$")
MAX_COMMAND_OUTPUT = 4 * 1024 * 1024
MAX_SERVICE_REPLICAS = 128


def _semver(value: str) -> tuple[int, int, int]:
    try:
        result = tuple(int(part) for part in value.split("."))
    except (AttributeError, ValueError) as exc:
        raise DeploymentError(f"Invalid platform version: {value!r}") from exc
    if len(result) != 3:
        raise DeploymentError(f"Invalid platform version: {value!r}")
    return result  # type: ignore[return-value]


def _safe_error_output(output: str) -> str:
    lines = []
    for raw_line in output.splitlines()[-80:]:
        line = raw_line[:500]
        line = re.sub(
            r"(?i)(password|secret|token|authorization|private[_ -]?key)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            line,
        )
        lines.append(line)
    return "\n".join(lines)


def _atomic_text(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class DockerHost:
    """Apply a verified release to the existing `/opt/aa-docker` layout."""

    def __init__(self, config: ReceiverConfig):
        self.config = config
        self.backup_path: Path | None = None
        self.original_dockerfile: Path | None = None
        self.original_local_settings: Path | None = None
        self.staged_release: Path | None = None
        self.previous_image_id: str | None = None
        self.previous_image_references: tuple[str, ...] = ()
        self.auth_replica_counts: dict[str, int] = {}
        self.live_replacement_started = False

    @property
    def compose_prefix(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.config.env_file),
            "-f",
            str(self.config.compose_file),
        ]

    def _run(
        self,
        arguments: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout: int | None = None,
        context: str,
    ) -> str:
        try:
            result = subprocess.run(
                list(arguments),
                cwd=self.config.app_dir,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout or self.config.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"{context} could not complete") from exc
        output_bytes = result.stdout or b""
        if len(output_bytes) > MAX_COMMAND_OUTPUT:
            output_bytes = output_bytes[-MAX_COMMAND_OUTPUT:]
        output = output_bytes.decode("utf-8", errors="replace")
        if result.returncode != 0:
            safe = _safe_error_output(output)
            suffix = f"\n{safe}" if safe else ""
            raise DeploymentError(f"{context} failed with exit {result.returncode}{suffix}")
        return output

    def _run_to_file(
        self, arguments: Sequence[str], destination: Path, *, context: str
    ) -> None:
        try:
            with destination.open("xb") as output:
                result = subprocess.run(
                    list(arguments),
                    cwd=self.config.app_dir,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=self.config.command_timeout_seconds,
                    check=False,
                )
                output.flush()
                os.fsync(output.fileno())
        except (OSError, subprocess.TimeoutExpired) as exc:
            destination.unlink(missing_ok=True)
            raise DeploymentError(f"{context} could not complete") from exc
        if result.returncode != 0:
            destination.unlink(missing_ok=True)
            error = _safe_error_output(
                (result.stderr or b"").decode("utf-8", errors="replace")
            )
            raise DeploymentError(
                f"{context} failed with exit {result.returncode}"
                + (f"\n{error}" if error else "")
            )

    def _run_from_file(
        self, arguments: Sequence[str], source: Path, *, context: str
    ) -> None:
        try:
            with source.open("rb") as input_stream:
                result = subprocess.run(
                    list(arguments),
                    cwd=self.config.app_dir,
                    stdin=input_stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.config.command_timeout_seconds,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"{context} could not complete") from exc
        if result.returncode != 0:
            output = _safe_error_output(
                (result.stdout or b"").decode("utf-8", errors="replace")
            )
            raise DeploymentError(
                f"{context} failed with exit {result.returncode}"
                + (f"\n{output}" if output else "")
            )

    def _compose(self, *arguments: str, context: str, timeout: int | None = None) -> str:
        return self._run(
            [*self.compose_prefix, *arguments], timeout=timeout, context=context
        )

    def _running_service_containers(self, service: str, *, context: str) -> tuple[str, ...]:
        """Return every running Compose container for a service, fail closed."""

        containers = tuple(
            line.strip()
            for line in self._compose("ps", "-q", service, context=context).splitlines()
            if line.strip()
        )
        if not containers:
            raise DeploymentError(f"{context} found no running containers for {service}")
        if (
            len(containers) > MAX_SERVICE_REPLICAS
            or len(set(containers)) != len(containers)
            or any(not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in containers)
        ):
            raise DeploymentError(
                f"{context} returned invalid container identities for {service}"
            )
        return containers

    def _auth_scale_arguments(self) -> tuple[str, ...]:
        """Pin the live Auth replica topology during replacement and rollback."""

        if set(self.auth_replica_counts) != set(self.config.auth_services):
            raise DeploymentError("Live AllianceAuth replica counts were not captured")
        arguments: list[str] = []
        for service in self.config.auth_services:
            count = self.auth_replica_counts[service]
            if not 1 <= count <= MAX_SERVICE_REPLICAS:
                raise DeploymentError(
                    f"Live service {service} has an invalid replica count"
                )
            arguments.extend(("--scale", f"{service}={count}"))
        return tuple(arguments)

    def _manage_image(self, *arguments: str, context: str) -> str:
        return self._compose(
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "python3",
            self.config.gunicorn_service,
            self.config.manage_py,
            *arguments,
            context=context,
        )

    def _manage_live(self, *arguments: str, context: str) -> str:
        return self._compose(
            "exec",
            "-T",
            self.config.gunicorn_service,
            "python3",
            self.config.manage_py,
            *arguments,
            context=context,
        )

    def _load_current(self) -> Mapping[str, Any] | None:
        path = self.config.state_dir / "current.json"
        if not path.exists():
            return None
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024:
            raise DeploymentError("Current deployment state is not a safe file")
        try:
            data = json.loads(path.read_text(encoding="ascii"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeploymentError("Current deployment state is unreadable") from exc
        required = {
            "schema_version",
            "platform_version",
            "release_commit",
            "source_commit",
            "manifest_sha256",
            "verified_at",
        }
        if not isinstance(data, dict) or set(data) != required or data["schema_version"] != 1:
            raise DeploymentError("Current deployment state has an unsupported schema")
        if canonical_json_bytes(data) != path.read_bytes():
            raise DeploymentError("Current deployment state is not canonical")
        if not all(
            isinstance(data[key], str)
            for key in required - {"schema_version"}
        ):
            raise DeploymentError("Current deployment state has invalid values")
        for key in ("release_commit", "source_commit"):
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", data[key]):
                raise DeploymentError(f"Current deployment state has an invalid {key}")
        if not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00",
            data["verified_at"],
        ):
            raise DeploymentError("Current deployment state has an invalid timestamp")
        if not SHA256_RE.fullmatch(data["manifest_sha256"]):
            raise DeploymentError("Current deployment state has an invalid manifest digest")
        return data

    @staticmethod
    def _secure_private_directory(path: Path, context: str) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        details = path.lstat()
        if not stat.S_ISDIR(details.st_mode) or path.is_symlink():
            raise DeploymentError(f"{context} is not a real directory")
        if details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o077:
            raise DeploymentError(f"{context} must be root-owned with mode 0700")

    def _read_env_value(self, key: str) -> str:
        path = self.config.app_dir / self.config.env_file
        found: str | None = None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if separator and name.strip() == key:
                if found is not None:
                    raise DeploymentError(f"{key} occurs more than once in the environment")
                found = value.strip()
                if (
                    len(found) >= 2
                    and found[0] == found[-1]
                    and found[0] in {"'", '"'}
                ):
                    found = found[1:-1]
        if not found:
            raise DeploymentError(f"{key} is not configured")
        return found

    def _validate_release_transition(self, bundle: ValidatedBundle) -> None:
        current = self._load_current()
        previous = bundle.manifest["previous_release"]
        if current is None:
            baseline = bundle.manifest["compatibility"]["values"].get(
                "production_baseline"
            )
            policy = bundle.manifest["compatibility"]["values"].get("policy")
            expected = {
                "platform_version": self.config.legacy_platform_version,
                "release_commit": self.config.legacy_release_commit,
                "deployment_generation": "legacy-v1",
            }
            if not isinstance(baseline, dict) or any(
                baseline.get(key) != value for key, value in expected.items()
            ):
                raise DeploymentError("First Platform v2 release targets the wrong legacy baseline")
            if _semver(bundle.request.platform_version) <= _semver(
                self.config.legacy_platform_version
            ):
                raise DeploymentError(
                    "First Platform v2 release must be newer than the legacy baseline"
                )
            if previous is not None and not (
                isinstance(policy, dict)
                and policy.get(
                    "legacy_bootstrap_may_skip_uninstalled_v2_releases"
                )
                is True
            ):
                raise DeploymentError(
                    "First Platform v2 release with a predecessor is not authorized "
                    "for legacy bootstrap"
                )
            return
        if _semver(bundle.request.platform_version) <= _semver(
            current["platform_version"]
        ):
            raise DeploymentError("Platform versions must increase monotonically")
        if not isinstance(previous, dict):
            raise DeploymentError("An established Platform v2 host requires previous_release")
        expected_previous = {
            "platform_version": current["platform_version"],
            "source_commit": current["source_commit"],
            "manifest_sha256": current["manifest_sha256"],
        }
        if previous != expected_previous:
            raise DeploymentError("Release predecessor does not match the verified live state")

    def _validate_runtime_image(self, bundle: ValidatedBundle) -> None:
        values = bundle.manifest["compatibility"]["values"]
        runtime = values.get("production_runtime")
        if not isinstance(runtime, dict) or set(runtime) != {"base_image"}:
            raise DeploymentError(
                "Release compatibility does not record one production base image"
            )
        expected = runtime["base_image"]
        if not isinstance(expected, str) or not IMAGE_DIGEST_RE.fullmatch(expected):
            raise DeploymentError("Release production base image is not digest pinned")
        configured = self._read_env_value("AA_DOCKER_TAG")
        if configured != expected:
            raise DeploymentError(
                "The host AA_DOCKER_TAG does not match the release compatibility contract"
            )

    def validate(self, bundle: ValidatedBundle) -> None:
        if os.geteuid() != 0:
            raise DeploymentError("Platform v2 receiver must run as root")
        app_dir = self.config.app_dir
        if not app_dir.is_dir() or app_dir.is_symlink():
            raise DeploymentError("AllianceAuth application directory is unavailable")
        for relative, name in (
            (self.config.compose_file, "Compose file"),
            (self.config.env_file, "environment file"),
            (self.config.custom_dockerfile, "custom Dockerfile"),
            (self.config.local_settings, "local settings"),
        ):
            path = app_dir / relative
            if not path.is_file() or path.is_symlink():
                raise DeploymentError(f"{name} is not a regular file")
        if self.config.state_dir.resolve().is_relative_to(app_dir.resolve()):
            raise DeploymentError("Deployment state must live outside the build context")
        if self.config.backup_dir.resolve().is_relative_to(app_dir.resolve()):
            raise DeploymentError("Database backups must live outside the build context")
        self._secure_private_directory(self.config.state_dir, "Deployment state directory")
        self._secure_private_directory(self.config.backup_dir, "Backup directory")
        if shutil.disk_usage(app_dir).free < 1024 * 1024 * 1024:
            raise DeploymentError("Less than 1 GiB is free in the AllianceAuth filesystem")
        self._validate_release_transition(bundle)
        self._validate_runtime_image(bundle)

        setup_commands = bundle.install_plan["setup_commands"]
        missing_setup = set(setup_commands) - set(self.config.setup_arguments)
        if missing_setup:
            raise DeploymentError(
                f"Receiver config lacks setup arguments for: {sorted(missing_setup)}"
            )
        local_text = (app_dir / self.config.local_settings).read_text(encoding="utf-8")
        missing_apps = [
            app
            for app in bundle.install_plan["django_apps"]
            if f'"{app}"' not in local_text and f"'{app}'" not in local_text
        ]
        if missing_apps:
            raise DeploymentError(
                f"local.py does not declare release applications: {missing_apps}"
            )
        services = {
            line.strip()
            for line in self._compose(
                "config", "--services", context="Docker Compose validation"
            ).splitlines()
            if line.strip()
        }
        required_services = set(self.config.auth_services) | {
            self.config.database_service,
            self.config.redis_service,
            self.config.proxy_service,
        }
        if not required_services <= services:
            raise DeploymentError(
                f"Docker Compose lacks required services: {sorted(required_services - services)}"
            )

    def _stage_release(self, bundle: ValidatedBundle) -> Path:
        release_root = (
            self.config.app_dir / "conf" / "buh-platform-v2" / "releases"
        )
        release_root.mkdir(mode=0o755, parents=True, exist_ok=True)
        destination = release_root / f"v{bundle.request.platform_version}"
        if destination.exists():
            if not destination.is_dir() or destination.is_symlink():
                raise DeploymentError("Staged release path is not a regular directory")
            source_files = {path.name: sha256_file(path) for path in bundle.release_dir.iterdir()}
            staged_files = {path.name: sha256_file(path) for path in destination.iterdir()}
            if source_files != staged_files:
                raise DeploymentError("Existing staged release has different bytes")
            return destination
        temporary = release_root / f".v{bundle.request.platform_version}.{os.getpid()}"
        if temporary.exists():
            raise DeploymentError("Temporary staged release path already exists")
        try:
            shutil.copytree(bundle.release_dir, temporary)
            for path in temporary.iterdir():
                if not path.is_file() or path.is_symlink():
                    raise DeploymentError("Staged release contains a non-regular file")
                path.chmod(0o644)
            os.replace(temporary, destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return destination

    def _dockerfile_block(self, bundle: ValidatedBundle) -> str:
        artifacts = {item["filename"]: item for item in bundle.manifest["artifacts"]}
        ordered = sorted(
            artifacts.values(),
            key=lambda item: (
                item["origin"] == "built",
                item["kind"] == "owned",
                item["component"],
            ),
        )
        relative_root = (
            f"conf/buh-platform-v2/releases/v{bundle.request.platform_version}"
        )
        lines = [BEGIN_V2]
        for artifact in ordered:
            filename = artifact["filename"]
            digest = artifact["sha256"]
            lines.extend(
                [
                    f"COPY {relative_root}/{filename} /tmp/buh-platform-v2/{filename}",
                    "RUN printf '%s  %s\\n' \\",
                    f"      '{digest}' '/tmp/buh-platform-v2/{filename}' \\",
                    "      | sha256sum --check --strict - \\",
                    "    && python3 -m pip install --no-cache-dir --no-deps \\",
                    "      --force-reinstall \\",
                    f"      /tmp/buh-platform-v2/{filename}",
                ]
            )
        lines.append(
            "LABEL com.b-uh.platform.version="
            f'"{bundle.request.platform_version}" '
            f'com.b-uh.platform.source="{bundle.manifest["source_commit"]}"'
        )
        lines.append(END_V2)
        return "\n".join(lines) + "\n"

    def _write_candidate_dockerfile(self, bundle: ValidatedBundle) -> None:
        path = self.config.app_dir / self.config.custom_dockerfile
        text = path.read_text(encoding="utf-8")
        for begin, end in ((BEGIN_V2, END_V2), (BEGIN_LEGACY, END_LEGACY)):
            if text.count(begin) != text.count(end) or text.count(begin) > 1:
                raise DeploymentError("Custom Dockerfile release markers are malformed")
        if BEGIN_V2 in text and BEGIN_LEGACY in text:
            raise DeploymentError("Custom Dockerfile contains both deployment generations")
        block = self._dockerfile_block(bundle)
        if BEGIN_V2 in text:
            pattern = re.compile(
                rf"(?ms)^{re.escape(BEGIN_V2)}\n.*?^{re.escape(END_V2)}\n?"
            )
            # A callable replacement preserves Dockerfile backslashes verbatim.
            # Passing ``block`` directly makes ``re.sub`` interpret sequences
            # such as ``\\n`` inside the generated shell command.
            updated = pattern.sub(lambda _match: block, text)
        elif BEGIN_LEGACY in text:
            pattern = re.compile(
                rf"(?ms)^{re.escape(BEGIN_LEGACY)}\n.*?^{re.escape(END_LEGACY)}\n?"
            )
            updated = pattern.sub(lambda _match: block, text)
        else:
            updated = text.rstrip() + "\n\n" + block
        _atomic_text(path, updated, path.stat().st_mode & 0o777)

    def _expected_versions(self, bundle: ValidatedBundle) -> dict[str, str]:
        return {
            wheel["distribution"]: wheel["version"]
            for wheel in bundle.install_plan["wheels"]
        }

    def _version_probe(self, service: str, bundle: ValidatedBundle, *, live: bool) -> None:
        expected = self._expected_versions(bundle)
        program = (
            "import importlib.metadata,json;"
            f"names={json.dumps(sorted(expected))};"
            "print(json.dumps({name:importlib.metadata.version(name) for name in names},"
            "sort_keys=True,separators=(',',':')))"
        )
        if live:
            output = self._compose(
                "exec",
                "-T",
                service,
                "python3",
                "-c",
                program,
                context=f"Live package verification in {service}",
            )
        else:
            output = self._compose(
                "run",
                "--rm",
                "--no-deps",
                "--entrypoint",
                "python3",
                service,
                "-c",
                program,
                context=f"Candidate package verification in {service}",
            )
        actual: Any = None
        for line in reversed(output.splitlines()):
            try:
                actual = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(actual, dict):
                break
        if actual != expected:
            raise DeploymentError(f"Package versions differ in {service}")

    def _require_shared_image(self) -> None:
        image_ids: set[str] = set()
        for service in self.config.auth_services:
            containers = self._running_service_containers(
                service, context=f"Live image container discovery for {service}"
            )
            expected = self.auth_replica_counts.get(service)
            if expected is not None and len(containers) != expected:
                raise DeploymentError(f"Live service {service} changed replica count")
            for container in containers:
                image_id = self._run(
                    ["docker", "inspect", "--format", "{{.Image}}", container],
                    context=f"Live image identity for {service}",
                ).strip()
                if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
                    raise DeploymentError(
                        f"Live service {service} returned an unsafe image identity"
                    )
                image_ids.add(image_id)
        if len(image_ids) != 1:
            raise DeploymentError("Live AllianceAuth replicas do not share one image")

    def _capture_live_image(self) -> None:
        """Remember the exact image, references, and replica topology used by live Auth."""

        image_ids: set[str] = set()
        image_references: set[str] = set()
        replica_counts: dict[str, int] = {}
        seen_containers: set[str] = set()
        for service in self.config.auth_services:
            containers = self._running_service_containers(
                service, context=f"Live container discovery for {service}"
            )
            if seen_containers.intersection(containers):
                raise DeploymentError(
                    "A live container resolved to more than one Auth service"
                )
            seen_containers.update(containers)
            replica_counts[service] = len(containers)
            for container in containers:
                details = self._run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.State.Status}}|{{.Image}}|{{.Config.Image}}",
                        container,
                    ],
                    context=f"Live image discovery for {service}",
                ).strip()
                parts = details.split("|")
                if len(parts) != 3 or parts[0] != "running":
                    raise DeploymentError(f"Live service {service} has an unstable replica")
                image_id, reference = parts[1:]
                if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
                    raise DeploymentError(
                        f"Live service {service} returned an unsafe image identity"
                    )
                if not SAFE_IMAGE_REFERENCE_RE.fullmatch(reference) or "@" in reference:
                    raise DeploymentError(
                        f"Live service {service} returned an unsafe image reference"
                    )
                image_ids.add(image_id)
                image_references.add(reference)
        if len(image_ids) != 1:
            raise DeploymentError("Live AllianceAuth replicas do not share one image")
        self.previous_image_id = image_ids.pop()
        self.previous_image_references = tuple(sorted(image_references))
        self.auth_replica_counts = replica_counts

    def _require_candidate_image(self) -> None:
        if not self.previous_image_references:
            raise DeploymentError("Candidate image references were not captured")
        image_ids = {
            self._run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                context="Candidate image identity verification",
            ).strip()
            for reference in self.previous_image_references
        }
        if len(image_ids) != 1 or not all(
            SAFE_IMAGE_ID_RE.fullmatch(image_id) for image_id in image_ids
        ):
            raise DeploymentError(
                "AllianceAuth services do not resolve one safe candidate image"
            )

    def _restore_candidate_configuration(self) -> bool:
        """Restore host files and retag the exact pre-attempt image without a restart."""

        if (
            self.original_dockerfile is None
            or self.original_local_settings is None
            or self.backup_path is None
        ):
            return False
        dockerfile = self.config.app_dir / self.config.custom_dockerfile
        local_settings = self.config.app_dir / self.config.local_settings
        shutil.copy2(self.original_dockerfile, dockerfile)
        shutil.copy2(self.original_local_settings, local_settings)
        if self.previous_image_id is not None:
            if not self.previous_image_references:
                raise DeploymentError("The previous image has no restorable reference")
            for reference in self.previous_image_references:
                self._run(
                    ["docker", "image", "tag", self.previous_image_id, reference],
                    context="Previous AllianceAuth image reference restoration",
                )
        return True

    def prepare_candidate(self, bundle: ValidatedBundle) -> None:
        self.backup_path = self.config.backup_dir / bundle.request.attempt_id
        self.backup_path.mkdir(mode=0o700, parents=True, exist_ok=False)
        dockerfile = self.config.app_dir / self.config.custom_dockerfile
        local_settings = self.config.app_dir / self.config.local_settings
        self.original_dockerfile = self.backup_path / "custom.dockerfile"
        self.original_local_settings = self.backup_path / "local.py"
        shutil.copy2(dockerfile, self.original_dockerfile)
        shutil.copy2(local_settings, self.original_local_settings)
        self.original_dockerfile.chmod(0o600)
        self.original_local_settings.chmod(0o600)

        self._capture_live_image()
        self.staged_release = self._stage_release(bundle)
        self._write_candidate_dockerfile(bundle)
        self._compose(
            "config", "--quiet", context="Candidate Docker Compose validation"
        )
        self._compose(
            "build",
            *self.config.auth_services,
            context="Cached candidate image build",
            timeout=self.config.command_timeout_seconds,
        )
        self._require_candidate_image()
        self._version_probe(self.config.gunicorn_service, bundle, live=False)
        self._manage_image("check", "--no-color", context="Candidate Django checks")
        self._manage_image(
            "migrate", "--plan", "--no-color", context="Candidate migration plan"
        )

    def _database_container(self) -> str:
        container = self._compose(
            "ps",
            "-q",
            self.config.database_service,
            context="Database container discovery",
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise DeploymentError("Database service did not resolve to one container")
        return container

    def _container_environment(self, container: str) -> dict[str, str]:
        output = self._run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                container,
            ],
            context="Database environment discovery",
        )
        result: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                result[key] = value
        return result

    @staticmethod
    def _database_shell() -> str:
        return (
            'password="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
            'test -n "$password"; '
            'export MYSQL_PWD="$password"; exec mariadb --user=root --batch '
            '--skip-column-names --execute="$1"'
        )

    def _database_query(self, container: str, sql: str) -> str:
        return self._run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-ceu",
                self._database_shell(),
                "buh-db-query",
                sql,
            ],
            context="Database verification query",
        )

    def _evidence_counts(self, container: str, database: str) -> dict[str, int]:
        if not SAFE_DATABASE_RE.fullmatch(database):
            raise DeploymentError("Database name is unsafe")
        table_sql = (
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{database}' AND "
            "(TABLE_NAME='django_migrations' OR TABLE_NAME LIKE 'buh\\_%') "
            "ORDER BY TABLE_NAME"
        )
        tables = [line.strip() for line in self._database_query(container, table_sql).splitlines()]
        if (
            not tables
            or len(tables) > 1000
            or any(not SAFE_DATABASE_RE.fullmatch(table) for table in tables)
        ):
            raise DeploymentError("Database evidence table set is invalid")
        counts: dict[str, int] = {}
        for table in tables:
            output = self._database_query(
                container, f"SELECT COUNT(*) FROM `{database}`.`{table}`"
            ).strip()
            if not re.fullmatch(r"[0-9]+", output):
                raise DeploymentError(f"Could not count database evidence table {table}")
            counts[table] = int(output)
        return counts

    def backup(self, bundle: ValidatedBundle) -> Mapping[str, Any]:
        if self.backup_path is None:
            raise DeploymentError("Configuration backup was not prepared")
        container = self._database_container()
        environment = self._container_environment(container)
        database = environment.get("MARIADB_DATABASE") or environment.get("MYSQL_DATABASE")
        if not database or not SAFE_DATABASE_RE.fullmatch(database):
            raise DeploymentError("Database container has no safe application database name")
        image_id = self._run(
            ["docker", "inspect", "--format", "{{.Image}}", container],
            context="Database image discovery",
        ).strip()
        if not SAFE_IMAGE_ID_RE.fullmatch(image_id):
            raise DeploymentError("Database container image is not content addressed")

        raw_path = self.backup_path / "database.sql"
        dump_script = (
            'database="${MARIADB_DATABASE:-${MYSQL_DATABASE:-}}"; '
            'password="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
            'test -n "$database"; test -n "$password"; '
            'export MYSQL_PWD="$password"; exec mariadb-dump --user=root '
            '--single-transaction --quick --routines --triggers --events --hex-blob '
            '"$database"'
        )
        self._run_to_file(
            ["docker", "exec", container, "sh", "-ceu", dump_script],
            raw_path,
            context="Pre-migration database backup",
        )
        if raw_path.stat().st_size < 128:
            raise DeploymentError("Database backup is unexpectedly small")
        source_counts = self._evidence_counts(container, database)

        restore_name = f"buh-restore-{bundle.request.workflow_run_id}-{os.getpid()}"
        restore_password = secrets.token_urlsafe(32)
        self._run(
            [
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                restore_name,
                "--network",
                "none",
                "--tmpfs",
                f"/var/lib/mysql:rw,noexec,nosuid,size={self.config.restore_tmpfs_mb}m",
                "--env",
                f"MARIADB_ROOT_PASSWORD={restore_password}",
                "--env",
                f"MARIADB_DATABASE={database}",
                image_id,
            ],
            context="Ephemeral database restore container startup",
        )
        try:
            ready = False
            for _ in range(self.config.health_attempts):
                try:
                    self._run(
                        [
                            "docker",
                            "exec",
                            restore_name,
                            "healthcheck.sh",
                            "--connect",
                            "--innodb_initialized",
                        ],
                        timeout=30,
                        context="Ephemeral database readiness",
                    )
                except DeploymentError:
                    time.sleep(self.config.health_interval_seconds)
                    continue
                ready = True
                break
            if not ready:
                raise DeploymentError("Ephemeral database restore container was not ready")
            restore_script = (
                'password="${MARIADB_ROOT_PASSWORD:-}"; test -n "$password"; '
                'export MYSQL_PWD="$password"; '
                'exec mariadb --user=root "$MARIADB_DATABASE"'
            )
            self._run_from_file(
                ["docker", "exec", "-i", restore_name, "sh", "-ceu", restore_script],
                raw_path,
                context="Ephemeral database restoration",
            )
            restored_counts = self._evidence_counts(restore_name, database)
            if restored_counts != source_counts:
                raise DeploymentError("Restored accounting evidence differs from the source")
        finally:
            try:
                self._run(
                    ["docker", "rm", "--force", restore_name],
                    timeout=60,
                    context="Ephemeral restore cleanup",
                )
            except DeploymentError:
                pass

        compressed_path = self.backup_path / "database.sql.gz"
        with raw_path.open("rb") as source, compressed_path.open("xb") as raw_output:
            with gzip.GzipFile(
                filename="database.sql", mode="wb", fileobj=raw_output, mtime=0
            ) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        compressed_path.chmod(0o600)
        raw_path.unlink()
        metadata = {
            "schema_version": 1,
            "filename": compressed_path.name,
            "sha256": sha256_file(compressed_path),
            "size": compressed_path.stat().st_size,
            "database_image_id": image_id,
            "evidence_tables": len(source_counts),
            "evidence_rows": sum(source_counts.values()),
        }
        metadata_path = self.backup_path / "BACKUP.json"
        _atomic_text(
            metadata_path,
            canonical_json_bytes(metadata).decode("ascii"),
            0o600,
        )
        return metadata

    def migrate(self, bundle: ValidatedBundle) -> None:
        policy = bundle.manifest["compatibility"]["values"].get("policy", {})
        if policy.get("database_migrations_must_be_rollback_compatible") is not True:
            raise DeploymentError("Release does not require rollback-compatible migrations")
        self._manage_image(
            "migrate", "--noinput", "--no-color", context="Database migration"
        )
        self._manage_image(
            "migrate", "--noinput", "--no-color", context="Idempotent migration check"
        )
        self._manage_image(
            "collectstatic",
            "--noinput",
            "--no-color",
            context="Static asset collection",
        )
        for command in bundle.install_plan["setup_commands"]:
            self._manage_image(
                command,
                *self.config.setup_arguments[command],
                context=f"Setup command {command}",
            )

    def swap(self, bundle: ValidatedBundle) -> None:
        scale_arguments = self._auth_scale_arguments()
        # Compose can partially replace containers before returning an error.
        # Mark the side effect before invoking it so rollback restores the old
        # image and full replica topology even when the journal is still at
        # ``migrated``.
        self.live_replacement_started = True
        self._compose(
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--force-recreate",
            *scale_arguments,
            *self.config.auth_services,
            context="AllianceAuth container replacement",
        )
        self._compose(
            "restart", self.config.proxy_service, context="Proxy restart"
        )

    def _containers_healthy(
        self,
        expected_replicas: Mapping[str, int],
        *,
        zero_restart_services: set[str],
    ) -> bool:
        try:
            for service, expected_count in expected_replicas.items():
                if not 1 <= expected_count <= MAX_SERVICE_REPLICAS:
                    return False
                ids = self._running_service_containers(
                    service, context=f"Container discovery for {service}"
                )
                if len(ids) != expected_count:
                    return False
                for container in ids:
                    state = self._run(
                        [
                            "docker",
                            "inspect",
                            "--format",
                            "{{.State.Status}}|{{.RestartCount}}|"
                            "{{if .State.Health}}{{.State.Health.Status}}"
                            "{{else}}none{{end}}",
                            container,
                        ],
                        context=f"Container health for {service}",
                    ).strip()
                    parts = state.split("|")
                    if (
                        len(parts) != 3
                        or parts[0] != "running"
                        or not parts[1].isdigit()
                        or parts[2] not in {"none", "healthy"}
                        or (
                            service in zero_restart_services
                            and parts[1] != "0"
                        )
                    ):
                        return False
            return True
        except DeploymentError:
            return False

    def health(self, bundle: ValidatedBundle) -> None:
        # Also validates that every configured Auth service was captured before
        # replacement. Infrastructure services deliberately remain singleton.
        self._auth_scale_arguments()
        expected_replicas = {
            **self.auth_replica_counts,
            self.config.database_service: 1,
            self.config.redis_service: 1,
            self.config.proxy_service: 1,
        }
        for _ in range(self.config.health_attempts):
            if self._containers_healthy(
                expected_replicas,
                zero_restart_services=set(self.config.auth_services),
            ):
                break
            time.sleep(self.config.health_interval_seconds)
        else:
            raise DeploymentError("Required containers did not become stable")

        self._require_shared_image()
        self._version_probe(self.config.gunicorn_service, bundle, live=True)
        self._manage_live("check", "--no-color", context="Live Django checks")
        migrations = self._manage_live(
            "showmigrations", "--plan", "--no-color", context="Live migration state"
        )
        if re.search(r"(?m)^\s*\[ \]", migrations):
            raise DeploymentError("Live deployment has pending migrations")
        self._compose(
            "exec",
            "-T",
            self.config.redis_service,
            "redis-cli",
            "ping",
            context="Redis health check",
        )
        celery = self._compose(
            "exec",
            "-T",
            self.config.worker_service,
            "celery",
            "--app=myauth",
            "inspect",
            "ping",
            "--timeout=5",
            context="Celery worker heartbeat",
        )
        if "pong" not in celery.lower():
            raise DeploymentError("Celery worker did not return a heartbeat")
        logs = self._compose(
            "logs",
            "--since=10m",
            "--tail=500",
            "--no-color",
            *self.config.auth_services,
            context="Fatal startup log scan",
        )
        if FATAL_LOG_RE.search(logs):
            raise DeploymentError("A fatal startup pattern occurred in AllianceAuth logs")

        for check in self.config.smoke_checks:
            request = urllib.request.Request(
                check.url,
                headers={"User-Agent": "B-UH-Platform-v2-health/1"},
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except (OSError, urllib.error.URLError) as exc:
                raise DeploymentError("An HTTPS smoke route was unreachable") from exc
            if status not in check.statuses:
                raise DeploymentError(
                    f"An HTTPS smoke route returned unexpected status {status}"
                )

    def finalize(self, bundle: ValidatedBundle) -> None:
        if self.staged_release is None:
            raise DeploymentError("Release was not staged")
        marker = {
            "schema_version": 1,
            "platform_version": bundle.request.platform_version,
            "release_commit": bundle.request.release_commit,
            "manifest_sha256": bundle.request.manifest_sha256,
        }
        path = self.staged_release.parent.parent / "CURRENT.json"
        _atomic_text(path, canonical_json_bytes(marker).decode("ascii"), 0o644)

    def rollback(self, bundle: ValidatedBundle, last_state: str | None) -> str:
        if not self._restore_candidate_configuration():
            return "No candidate configuration was written; production was unchanged."
        if self.live_replacement_started or last_state in {"swapped", "healthy"}:
            scale_arguments = self._auth_scale_arguments()
            self._compose(
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                *scale_arguments,
                *self.config.auth_services,
                context="Rollback container replacement",
            )
            self._compose(
                "restart", self.config.proxy_service, context="Rollback proxy restart"
            )
            return (
                "Previous application image and containers were restored. Database migrations were "
                "not reversed; retain and verify the pre-migration backup before any restore."
            )
        if last_state in {"backed_up", "migrated"}:
            return (
                "The live containers were never swapped. Candidate configuration was "
                "restored; database migrations remain in place and are required to be "
                "backward compatible."
            )
        return "Candidate configuration was restored; live containers were never replaced."
