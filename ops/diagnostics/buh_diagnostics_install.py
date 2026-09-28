#!/usr/bin/env python3
"""Verify and transactionally activate the independently hosted diagnostics.

The approved manifest is made *after* the immutable release exists. An operator
checks the archive and this program against its hashes before executing root
code; this program repeats those checks and records them on the host.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SOURCE_FILES = {
    "collector": "ops/diagnostics/buh_host_diagnostics.py",
    "publisher": "ops/diagnostics/buh_diagnostics_publish.py",
    "redactor": "ops/buh-redact-diagnostics.py",
    "installer": "ops/diagnostics/buh_diagnostics_install.py",
}
UNITS = ("buh-diagnostics.service", "buh-diagnostics.timer",
         "buh-diagnostics-publish.service", "buh-diagnostics-publish.timer")
TIMERS = ("buh-diagnostics.timer", "buh-diagnostics-publish.timer")
SERVICES = ("buh-diagnostics.service", "buh-diagnostics-publish.service")
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def atomic_file(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".buh-diag-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            if hasattr(os, "fchmod"):
                os.fchmod(target.fileno(), mode)
            else:
                os.chmod(temporary, mode)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fsync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def key_fingerprint(path):
    result = subprocess.run(["ssh-keygen", "-y", "-f", str(path)],
                            capture_output=True, text=True, timeout=15, check=True)
    parts = result.stdout.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("Diagnostics deploy key must be Ed25519")
    raw = base64.b64decode(parts[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def validate_services(value):
    required = {"allianceauth_gunicorn", "allianceauth_worker",
                "allianceauth_worker_services", "allianceauth_beat",
                "auth_mysql", "redis", "nginx"}
    allowed = required | {"proxy", "grafana"}
    if (not isinstance(value, dict) or set(value) !=
            {"schema_version", "compose_project", "services"}
            or value["schema_version"] != 1
            or not isinstance(value["compose_project"], str)
            or re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", value["compose_project"]) is None
            or not isinstance(value["services"], dict)
            or not required <= value["services"].keys()
            or not value["services"].keys() <= allowed
            or any(type(count) is not int or not 1 <= count <= 128
                   for count in value["services"].values())):
        raise ValueError("Invalid approved service and replica inventory")


def archive_source_files(archive, source_commit):
    with tarfile.open(archive, "r:*") as source:
        if source.pax_headers.get("comment") != source_commit:
            raise ValueError("Source archive does not identify the approved commit")
        return {name: source.extractfile(path).read()
                for name, path in SOURCE_FILES.items()}


def manifest_for(source_commit, archive, bundle, key, known_hosts, services):
    if COMMIT.fullmatch(source_commit) is None:
        raise ValueError("Source commit must be a full SHA-1 Git identifier")
    validate_services(services)
    archived = archive_source_files(archive, source_commit)
    files = {}
    for name, relative in SOURCE_FILES.items():
        path = bundle / relative
        files[name] = file_digest(path)
        if digest(archived[name]) != files[name]:
            raise ValueError("Bundle source differs from approved archive: " + name)
    return {"schema_version": 1, "source_commit": source_commit,
            "source_archive_sha256": file_digest(archive), "files": files,
            "deploy_key_sha256": file_digest(key),
            "deploy_key_fingerprint": key_fingerprint(key),
            "known_hosts_sha256": file_digest(known_hosts),
            "expected_services": services}


def verify_manifest(path, archive, bundle, key, known_hosts):
    raw = path.read_bytes()
    value = json.loads(raw)
    if raw != canonical(value) or set(value) != {
            "schema_version", "source_commit", "source_archive_sha256", "files",
            "deploy_key_sha256", "deploy_key_fingerprint", "known_hosts_sha256",
            "expected_services"} or value["schema_version"] != 1:
        raise ValueError("Diagnostics approval manifest is not canonical schema v1")
    if (not isinstance(value["files"], dict)
            or set(value["files"]) != set(SOURCE_FILES)
            or any(not isinstance(item, str) or SHA.fullmatch(item) is None
                   for item in (*value["files"].values(), value["source_archive_sha256"],
                                value["deploy_key_sha256"], value["known_hosts_sha256"]))):
        raise ValueError("Diagnostics manifest has invalid file hashes")
    observed = manifest_for(value["source_commit"], archive, bundle, key,
                            known_hosts, value["expected_services"])
    if value != observed or file_digest(Path(__file__)) != value["files"]["installer"]:
        raise ValueError("Diagnostics payload differs from the approved manifest")
    return value, digest(raw)


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/")

    @property
    def base(self):
        return self.root / "usr/local/libexec/buh-diagnostics"

    @property
    def current(self):
        return self.base / "current"

    @property
    def state(self):
        return self.root / "var/lib/buh-diagnostics-install"

    @property
    def marker(self):
        return self.state / "transaction.json"

    @property
    def receipt(self):
        return self.state / "receipt.json"

    @property
    def units(self):
        return self.root / "etc/systemd/system"


def unit_text(name):
    if name == "buh-diagnostics.service":
        return ("[Unit]\nDescription=Collect private B-UH diagnostics outside Auth\n"
                "Wants=docker.service\nAfter=docker.service\n\n[Service]\nType=oneshot\n"
                "User=root\nEnvironment=BUH_DIAGNOSTICS_REDACTOR=/usr/local/libexec/"
                "buh-diagnostics/current/redactor\n"
                "Environment=BUH_DIAGNOSTICS_EXPECTED_SERVICES=/usr/local/libexec/"
                "buh-diagnostics/current/expected-services.json\n"
                "ExecStart=/usr/bin/python3 -B /usr/local/libexec/buh-diagnostics/"
                "current/collector collect\nTimeoutStartSec=12min\n")
    if name == "buh-diagnostics-publish.service":
        return ("[Unit]\nDescription=Publish private B-UH diagnostics\n"
                "Wants=network-online.target\nAfter=network-online.target buh-diagnostics.service\n"
                "\n[Service]\nType=oneshot\nUser=root\n"
                "Environment=BUH_DIAGNOSTICS_DEPLOY_KEY=/usr/local/libexec/"
                "buh-diagnostics/current/deploy-key\n"
                "Environment=BUH_DIAGNOSTICS_KNOWN_HOSTS=/usr/local/libexec/"
                "buh-diagnostics/current/known-hosts\n"
                "ExecStart=/usr/bin/python3 -B /usr/local/libexec/buh-diagnostics/"
                "current/publisher\nTimeoutStartSec=12min\n")
    if name in TIMERS:
        offset = "00" if name == TIMERS[0] else "02"
        service = name.removesuffix(".timer") + ".service"
        return (f"[Unit]\nDescription=Run {service} every five minutes\n\n"
                f"[Timer]\nOnCalendar=*-*-* *:{offset}/5:00\nAccuracySec=30s\n"
                f"Persistent=true\nUnit={service}\n\n[Install]\nWantedBy=timers.target\n")
    raise ValueError("Unknown diagnostics unit")


def systemctl(*args, check=True):
    result = subprocess.run(["systemctl", *args], capture_output=True, text=True,
                            timeout=780, check=False)
    if check and result.returncode:
        raise RuntimeError("systemctl " + " ".join(args[:2]) + " failed")
    return result.returncode == 0


def switch_current(paths, target):
    temporary = paths.base / ".current-next"
    temporary.unlink(missing_ok=True)
    if target is None:
        paths.current.unlink(missing_ok=True)
    else:
        os.symlink(target, temporary)
        os.replace(temporary, paths.current)
    fsync_dir(paths.base)


def stage(paths, manifest, bundle, key, known_hosts, manifest_hash):
    paths.base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.base, 0o700)
    releases = paths.base / "releases"
    releases.mkdir(exist_ok=True, mode=0o700)
    target = releases / manifest_hash
    if target.exists():
        for name in ("collector", "publisher", "redactor", "installer"):
            if file_digest(target / name) != manifest["files"][name]:
                raise ValueError("Existing staged diagnostics payload differs")
        if file_digest(target / "deploy-key") != manifest["deploy_key_sha256"]:
            raise ValueError("Existing staged diagnostics key differs")
        if file_digest(target / "known-hosts") != manifest["known_hosts_sha256"]:
            raise ValueError("Existing staged diagnostics host pin differs")
        if (target / "expected-services.json").read_bytes() != canonical(
                manifest["expected_services"]):
            raise ValueError("Existing staged diagnostics inventory differs")
        return target
    staged = Path(tempfile.mkdtemp(prefix=".stage-", dir=releases))
    try:
        os.chmod(staged, 0o700)
        for name, relative in SOURCE_FILES.items():
            shutil.copyfile(bundle / relative, staged / name)
            os.chmod(staged / name, 0o700)
        for source, name in ((key, "deploy-key"), (known_hosts, "known-hosts")):
            shutil.copyfile(source, staged / name)
            os.chmod(staged / name, 0o600)
        atomic_file(staged / "expected-services.json",
                    canonical(manifest["expected_services"]))
        os.replace(staged, target)
        fsync_dir(releases)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return target


def recover(paths):
    if not paths.marker.exists():
        return False
    transaction = json.loads(paths.marker.read_text(encoding="utf-8"))
    if (not isinstance(transaction, dict) or set(transaction) != {
            "schema_version", "manifest_sha256", "previous_current",
            "previous_receipt", "new_units", "enabled_timers"}
            or transaction["schema_version"] != 1
            or not isinstance(transaction["manifest_sha256"], str)
            or SHA.fullmatch(transaction["manifest_sha256"]) is None
            or (transaction["previous_current"] is not None
                and (not isinstance(transaction["previous_current"], str)
                     or re.fullmatch(r"releases/[0-9a-f]{64}",
                                     transaction["previous_current"]) is None))
            or (transaction["previous_receipt"] is not None
                and not isinstance(transaction["previous_receipt"], dict))
            or not isinstance(transaction["new_units"], list)
            or not set(transaction["new_units"]) <= set(UNITS)
            or not isinstance(transaction["enabled_timers"], list)
            or not set(transaction["enabled_timers"]) <= set(TIMERS)):
        raise ValueError("Cannot recover unknown diagnostics transaction")
    for timer in TIMERS:
        systemctl("disable", "--now", timer, check=False)
    for service in SERVICES:
        systemctl("stop", service, check=False)
    switch_current(paths, transaction["previous_current"])
    for name in transaction["new_units"]:
        (paths.units / name).unlink(missing_ok=True)
    systemctl("daemon-reload")
    for timer in transaction["enabled_timers"]:
        systemctl("enable", "--now", timer)
    previous_receipt = transaction["previous_receipt"]
    if previous_receipt is None:
        paths.receipt.unlink(missing_ok=True)
    else:
        atomic_file(paths.receipt, canonical(previous_receipt))
    paths.marker.unlink()
    fsync_dir(paths.state)
    return True


def install(paths, manifest_path, archive, bundle, key, known_hosts, confirmation):
    manifest, manifest_hash = verify_manifest(manifest_path, archive, bundle, key,
                                              known_hosts)
    if confirmation != "INSTALL BUH DIAGNOSTICS " + manifest_hash:
        raise ValueError("Installation confirmation does not bind the exact manifest")
    paths.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.state, 0o700)
    if paths.marker.exists():
        recover(paths)
    if paths.receipt.exists():
        current = json.loads(paths.receipt.read_text(encoding="utf-8"))
        if current.get("manifest_sha256") == manifest_hash:
            raise ValueError("Approved diagnostics payload is already installed")
    stage(paths, manifest, bundle, key, known_hosts, manifest_hash)
    if paths.current.exists() and not paths.current.is_symlink():
        raise ValueError("Diagnostics current path is not a symlink")
    previous_current = os.readlink(paths.current) if paths.current.is_symlink() else None
    previous_receipt = (json.loads(paths.receipt.read_text(encoding="utf-8"))
                        if paths.receipt.exists() else None)
    new_units = [name for name in UNITS if not (paths.units / name).exists()]
    for name in UNITS:
        existing = paths.units / name
        if existing.exists() and existing.read_bytes() != unit_text(name).encode():
            raise ValueError("Existing diagnostics systemd unit differs from reviewed source")
    enabled_timers = [name for name in TIMERS if systemctl("is-enabled", name, check=False)]
    transaction = {"schema_version": 1, "manifest_sha256": manifest_hash,
                   "previous_current": previous_current,
                   "previous_receipt": previous_receipt,
                   "new_units": new_units, "enabled_timers": enabled_timers}
    atomic_file(paths.marker, canonical(transaction))
    try:
        for timer in TIMERS:
            systemctl("disable", "--now", timer, check=False)
        for service in SERVICES:
            systemctl("stop", service, check=False)
        for name in new_units:
            atomic_file(paths.units / name, unit_text(name).encode(), 0o644)
        systemctl("daemon-reload")
        switch_current(paths, "releases/" + manifest_hash)
        for service in SERVICES:
            systemctl("start", service)
        for timer in TIMERS:
            systemctl("enable", "--now", timer)
        receipt = {"schema_version": 1, "installed_at": datetime.now(UTC).isoformat(),
                   "manifest_sha256": manifest_hash,
                   "source_commit": manifest["source_commit"],
                   "source_archive_sha256": manifest["source_archive_sha256"],
                   "files": manifest["files"],
                   "deploy_key_fingerprint": manifest["deploy_key_fingerprint"],
                   "known_hosts_sha256": manifest["known_hosts_sha256"],
                   "expected_services": manifest["expected_services"]}
        atomic_file(paths.receipt, canonical(receipt))
        paths.marker.unlink()
        fsync_dir(paths.state)
        return receipt
    except Exception:
        recover(paths)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("manifest", "verify", "install"):
        item = sub.add_parser(command)
        item.add_argument("--archive", type=Path, required=True)
        item.add_argument("--bundle", type=Path, required=True)
        item.add_argument("--deploy-key", type=Path, required=True)
        item.add_argument("--known-hosts", type=Path, required=True)
        item.add_argument("--manifest", type=Path, required=True)
        if command == "manifest":
            item.add_argument("--source-commit", required=True)
            item.add_argument("--expected-services", type=Path, required=True)
        if command == "install":
            item.add_argument("--confirmation", required=True)
    sub.add_parser("recover")
    args = parser.parse_args(argv)
    if args.command == "manifest":
        services = json.loads(args.expected_services.read_text(encoding="utf-8"))
        value = manifest_for(args.source_commit, args.archive, args.bundle,
                             args.deploy_key, args.known_hosts, services)
        atomic_file(args.manifest, canonical(value))
        print(digest(canonical(value)))
        return 0
    if args.command == "verify":
        _, value = verify_manifest(args.manifest, args.archive, args.bundle,
                                   args.deploy_key, args.known_hosts)
        print(value)
        return 0
    if os.geteuid() != 0:
        parser.error("Installation and recovery require root")
    paths = Paths()
    if args.command == "recover":
        print("recovered" if recover(paths) else "no pending transaction")
        return 0
    receipt = install(paths, args.manifest, args.archive, args.bundle,
                      args.deploy_key, args.known_hosts, args.confirmation)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
