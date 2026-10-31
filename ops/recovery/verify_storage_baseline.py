"""Verify released, Git-owned and installed archive code; reads production only.

The probe reads package files via importlib.metadata, without Django startup,
database access, credentials, production payloads, or runtime changes.
"""

import argparse
import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


BASE = "5171737c1279a82005ff83e60468a1c570e48cee"
WHEEL_SHA256 = "adf116b02f024344da47190103078697523f71ae60412934f5e09506a36f80f5"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="b-uh")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    wheel = root / "releases/platform/v0.8.3/aa_buh_max_history-2.1.2-py3-none-any.whl"
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL_SHA256:
        raise RuntimeError("Released baseline wheel differs from verified production identity")
    with zipfile.ZipFile(wheel) as archive:
        expected = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist() if name.startswith("buh_max_history/") and not name.endswith("/")
        }
    mismatches = []
    for name, digest in expected.items():
        source = subprocess.check_output(
            ["git", "show", f"{BASE}:apps/esi-history/src/{name}"], cwd=root,
        )
        if hashlib.sha256(source).hexdigest() != digest:
            mismatches.append(name)
    script = """
import hashlib,json
from importlib.metadata import distribution
dist=distribution('aa-buh-max-history')
names=NAMES
print(json.dumps({'version':dist.version,'files':{name:hashlib.sha256(dist.locate_file(name).read_bytes()).hexdigest() for name in names}}))
""".replace("NAMES", repr(sorted(expected)))
    probe = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", args.host, "docker exec -i allianceauth_gunicorn python -B -"],
        input=script, capture_output=True, text=True, timeout=60, check=False,
    )
    if probe.returncode:
        raise RuntimeError("Read-only installed-code hash probe failed")
    installed = json.loads(probe.stdout)
    installed_mismatches = sorted(name for name, digest in expected.items() if installed["files"].get(name) != digest)
    report = {
        "source_repository": "https://github.com/Fifty5D/B-UH-AllianceAuth",
        "baseline_commit": BASE, "wheel_sha256": WHEEL_SHA256,
        "installed_version": installed["version"], "package_files_checked": len(expected),
        "git_vs_wheel_mismatches": sorted(mismatches),
        "installed_vs_wheel_mismatches": installed_mismatches,
        "reads_only": True,
    }
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return bool(mismatches or installed_mismatches or installed["version"] != "2.1.2")


if __name__ == "__main__":
    raise SystemExit(main())
