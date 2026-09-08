"""Exact, reviewed policy for the one bounded production release-gap recovery."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


POLICY_PATH = Path(__file__).resolve().parents[1] / "deploy" / "coordinated-recovery.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class RecoveryPolicyError(ValueError):
    """The reviewed recovery policy or a claimed identity is invalid."""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def version_tuple(value: Any) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise RecoveryPolicyError("Recovery policy contains an invalid platform version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validate_release_identity(value: Any, *, context: str) -> dict[str, str]:
    fields = {
        "manifest_sha256",
        "platform_version",
        "release_commit",
        "release_ref",
        "source_commit",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RecoveryPolicyError(f"{context} has invalid fields")
    version = value.get("platform_version")
    version_tuple(version)
    if value.get("release_ref") != f"release/platform-v{version}":
        raise RecoveryPolicyError(f"{context} has an invalid release ref")
    for field in ("release_commit", "source_commit"):
        if not isinstance(value.get(field), str) or COMMIT_RE.fullmatch(value[field]) is None:
            raise RecoveryPolicyError(f"{context} has an invalid {field}")
    digest = value.get("manifest_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise RecoveryPolicyError(f"{context} has an invalid manifest digest")
    return {field: value[field] for field in sorted(fields)}


def _validate_host_baseline(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "auth_services",
        "compose_files_after_activation",
        "compose_files_before_activation",
        "discord_owner",
        "front_proxy",
        "installed_receiver",
        "local_settings",
        "nginx",
    }:
        raise RecoveryPolicyError("Recovery host baseline has invalid fields")
    services = value["auth_services"]
    expected_services = {
        "allianceauth_gunicorn",
        "allianceauth_worker",
        "allianceauth_worker_services",
        "allianceauth_beat",
    }
    if not isinstance(services, dict) or set(services) != expected_services:
        raise RecoveryPolicyError("Recovery host baseline has invalid Auth services")
    for service, identity in services.items():
        if (
            not isinstance(identity, dict)
            or set(identity) != {"image_id", "replicas"}
            or not isinstance(identity.get("image_id"), str)
            or IMAGE_RE.fullmatch(identity["image_id"]) is None
            or type(identity.get("replicas")) is not int
            or not 1 <= identity["replicas"] <= 128
        ):
            raise RecoveryPolicyError(
                f"Recovery host baseline has an invalid {service} identity"
            )
    if value["compose_files_before_activation"] != [
        "docker-compose.yml",
        "docker-compose.buh-vps-health.yml",
    ]:
        raise RecoveryPolicyError("Recovery pre-activation Compose baseline changed")
    if value["compose_files_after_activation"] != [
        "docker-compose.yml",
        "docker-compose.buh-vps-health.yml",
        "docker-compose.buh-platform-v2.yml",
    ]:
        raise RecoveryPolicyError("Recovery activated Compose baseline changed")
    owner = value["discord_owner"]
    if (
        not isinstance(owner, dict)
        or set(owner) != {"auth_username", "discord_user_id", "guild_id"}
        or owner.get("auth_username") != "Fifty5D"
        or owner.get("discord_user_id") != "318985508913020930"
        or owner.get("guild_id") != "1521272563626672198"
    ):
        raise RecoveryPolicyError("Recovery Discord owner baseline changed")
    settings = value["local_settings"]
    if settings != {
        "gid": 61000,
        "mode": "0640",
        "path": "conf/local.py",
        "uid": 0,
    }:
        raise RecoveryPolicyError("Recovery local-settings baseline changed")
    receiver = value["installed_receiver"]
    if not isinstance(receiver, dict) or set(receiver) != {
        "config",
        "files",
        "install_record",
    }:
        raise RecoveryPolicyError("Recovery installed-receiver baseline changed")
    if receiver["config"] != {
        "gid": 0,
        "mode": "0600",
        "path": "/etc/buh-platform-v2/receiver.json",
        "schema_version": 1,
        "sha256": "b57e6db83bccbedcf944985469f4f621e0b34b4eb34274b68c2dff210444bb04",
        "uid": 0,
    }:
        raise RecoveryPolicyError("Recovery receiver-config baseline changed")
    if receiver["install_record"] != {
        "gid": 0,
        "mode": "0600",
        "path": "/etc/buh-platform-v2/INSTALL.json",
        "sha256": "98c1dafcc50c410a8caae7531f4b2f87b1caa055a0fdd3ca6e97973c7ce9f743",
        "source_commit": "afd2d37e09856c34be1d18f76383898d87a384a7",
        "uid": 0,
    }:
        raise RecoveryPolicyError("Recovery receiver-install baseline changed")
    receiver_files = receiver["files"]
    expected_receiver_files = {
        "/usr/local/lib/buh-platform-v2/ops/deploy/contracts.py": {
            "gid": 0,
            "mode": "0644",
            "sha256": "62a02fcd36393bb110c15f2489e66d42e043ad21ee211e37ab03c80a584a2701",
            "uid": 0,
        },
        "/usr/local/lib/buh-platform-v2/ops/deploy/docker_host.py": {
            "gid": 0,
            "mode": "0644",
            "sha256": "c538f0737c1fd81561250ae24dc81b94e82caf8de4c76e589d865d484120c6d0",
            "uid": 0,
        },
        "/usr/local/sbin/buh-deploy-dispatch": {
            "gid": 0,
            "mode": "0755",
            "sha256": "6684f39ab4413540545bafa8fba1908f9fd412dbf4c9500cb77eff86a2e359ac",
            "uid": 0,
        },
        "/usr/local/sbin/buh-platform-v2-receiver": {
            "gid": 0,
            "mode": "0755",
            "sha256": "41efdd024619c2c4048e7964ef9220bddf52fd09284004837a8293f126b26d48",
            "uid": 0,
        },
    }
    if receiver_files != expected_receiver_files:
        raise RecoveryPolicyError("Recovery receiver-file baseline changed")
    for key, service, container in (
        ("nginx", "nginx", "aa-docker-nginx-1"),
        ("front_proxy", "proxy", "aa-docker-proxy-1"),
    ):
        item = value[key]
        if (
            not isinstance(item, dict)
            or set(item) != {"container", "image_id", "service"}
            or item.get("service") != service
            or item.get("container") != container
            or not isinstance(item.get("image_id"), str)
            or IMAGE_RE.fullmatch(item["image_id"]) is None
        ):
            raise RecoveryPolicyError(f"Recovery {key} baseline changed")


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RecoveryPolicyError("Reviewed recovery policy is unavailable") from exc
    if not raw or len(raw) > 64 * 1024:
        raise RecoveryPolicyError("Reviewed recovery policy has an unsafe size")
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryPolicyError("Reviewed recovery policy is unreadable") from exc
    if raw != canonical_json_bytes(value):
        raise RecoveryPolicyError("Reviewed recovery policy is not canonical")
    if not isinstance(value, dict) or set(value) != {
        "baseline",
        "host_baseline",
        "policy_id",
        "published_releases",
        "repository",
        "schema_version",
        "server_evidence",
    }:
        raise RecoveryPolicyError("Reviewed recovery policy has invalid fields")
    if (
        value["schema_version"] != 1
        or value["policy_id"] != "production-v0.5.6-published-gap-20260907"
        or value["repository"] != "Fifty5D/B-UH-AllianceAuth"
    ):
        raise RecoveryPolicyError("Reviewed recovery policy has the wrong identity")
    evidence = value["server_evidence"]
    if evidence != {
        "captured_at": "2026-09-07T22:17:17.791522+00:00",
        "sha256": "df9865e121e068cc65c3e6b05cb287f1b87f05772e54ecd4c7d07fa61137fc38",
    }:
        raise RecoveryPolicyError("Reviewed server evidence identity changed")
    baseline = validate_release_identity(value["baseline"], context="Recovery baseline")
    releases = value["published_releases"]
    if not isinstance(releases, list) or len(releases) != 3:
        raise RecoveryPolicyError("Reviewed recovery chain has an invalid length")
    identities = [
        validate_release_identity(item, context=f"Recovery release {index}")
        for index, item in enumerate(releases)
    ]
    if identities[0] != baseline:
        raise RecoveryPolicyError("Recovery chain does not start at its baseline")
    versions = [version_tuple(item["platform_version"]) for item in identities]
    if versions != sorted(set(versions)):
        raise RecoveryPolicyError("Recovery release chain is not strictly increasing")
    _validate_host_baseline(value["host_baseline"])
    return value


def policy_sha256(path: Path = POLICY_PATH) -> str:
    policy = load_policy(path)
    return hashlib.sha256(canonical_json_bytes(policy)).hexdigest()


def manifest_recovery(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Return the release-manifest opt-in for the next recovery release."""

    releases = policy["published_releases"]
    return {
        "baseline": dict(policy["baseline"]),
        "intervening_releases": [dict(item) for item in releases[1:]],
        "policy_id": policy["policy_id"],
        "policy_sha256": hashlib.sha256(canonical_json_bytes(policy)).hexdigest(),
    }


def validate_manifest_recovery(value: Any) -> dict[str, Any]:
    """Validate a manifest-carried one-release opt-in without trusting local policy."""

    if not isinstance(value, dict) or set(value) != {
        "baseline",
        "intervening_releases",
        "policy_id",
        "policy_sha256",
    }:
        raise RecoveryPolicyError("Deployment recovery metadata has invalid fields")
    policy_id = value.get("policy_id")
    digest = value.get("policy_sha256")
    if (
        not isinstance(policy_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,99}", policy_id) is None
        or not isinstance(digest, str)
        or SHA256_RE.fullmatch(digest) is None
    ):
        raise RecoveryPolicyError("Deployment recovery metadata has an invalid identity")
    baseline = validate_release_identity(
        value.get("baseline"), context="Deployment recovery baseline"
    )
    intervening = value.get("intervening_releases")
    if not isinstance(intervening, list) or not 1 <= len(intervening) <= 8:
        raise RecoveryPolicyError("Deployment recovery chain has an invalid length")
    releases = [
        validate_release_identity(item, context=f"Deployment recovery release {index}")
        for index, item in enumerate(intervening)
    ]
    versions = [version_tuple(baseline["platform_version"])] + [
        version_tuple(item["platform_version"]) for item in releases
    ]
    if versions != sorted(set(versions)):
        raise RecoveryPolicyError("Deployment recovery chain is not strictly increasing")
    return {
        "baseline": baseline,
        "intervening_releases": releases,
        "policy_id": policy_id,
        "policy_sha256": digest,
    }


def recovery_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
