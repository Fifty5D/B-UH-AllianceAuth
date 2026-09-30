"""Reproduce the production core downgrade on a disposable CI Docker host."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.deploy.docker_host import (  # noqa: E402
    LOCKED_PRODUCTION_DEPENDENCIES,
    DockerHost,
)

FIXTURES = ROOT / "tests/deploy/fixtures"
EXPECTED_SHA256 = {
    "aa-docker-custom-pre-v2.dockerfile":
        "c6104339e50c91d06b2d59d2d91bfd18e948333b7b44aa85395ecd5c419f9d00",
    "aa-docker-requirements.txt":
        "67086dbf48233c30d85f09ede14821b7ad16c3a423a3f0e4c975e8d344e961a5",
    "aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl":
        "e26b225324996c679dddae29d45abf1afce573494fa0f293e755f2072631f0ce",
}
PACKAGE = "aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl"
OLD_INSTALL = f"RUN pip install /tmp/{PACKAGE}"
NEW_INSTALL = f"RUN pip install --no-deps /tmp/{PACKAGE}"
PROBE = (
    "import allianceauth,importlib.metadata as m,json;"
    "print(json.dumps({'installed':m.version('allianceauth'),"
    "'imported':allianceauth.__version__,"
    "'packaging':m.version('packaging')},sort_keys=True))"
)
LOCKED_PROBE = (
    "import importlib.metadata as m,json;"
    f"names={json.dumps(sorted(LOCKED_PRODUCTION_DEPENDENCIES))};"
    "print(json.dumps({name:m.version(name) for name in names},sort_keys=True))"
)


def _run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def _probe(image: str, program: str = PROBE) -> dict[str, str]:
    output = _run(
        "docker", "run", "--rm", "--entrypoint", "python3", image,
        "-c", program, capture=True,
    )
    return json.loads(output.strip().splitlines()[-1])


def main() -> None:
    for filename, expected in EXPECTED_SHA256.items():
        actual = hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f"Production build fixture changed: {filename}")

    compatibility = tomllib.loads(
        (ROOT / "platform/compatibility.toml").read_text(encoding="utf-8")
    )
    base = compatibility["production_runtime"]["base_image"]
    expected_core = compatibility["runtime"]["allianceauth"]
    expected_packaging = compatibility["runtime"]["packaging"]
    expected_django_esi = compatibility["runtime"]["django_esi"]
    if expected_core != "5.4.0":
        raise SystemExit("This exact regression rehearsal expects AllianceAuth 5.4.0")
    if expected_django_esi != "9.6.0":
        raise SystemExit("This exact regression rehearsal expects django-esi 9.6.0")
    base_probe = _probe(base)
    if (base_probe["installed"], base_probe["imported"]) != (
        expected_core, expected_core
    ):
        raise SystemExit("The digest-pinned base image is not AllianceAuth 5.4.0")

    original = (FIXTURES / "aa-docker-custom-pre-v2.dockerfile").read_text(
        encoding="utf-8"
    )
    # The first installed helper is the observed downgrade. These bytes are the
    # corresponding prefix of the effective host custom.dockerfile, not the
    # unused /opt/aa-docker/Dockerfile.
    prefix, separator, _tail = original.partition("# B-UH Mining Analytics")
    if not separator or prefix.count(OLD_INSTALL) != 1:
        raise SystemExit("Effective production Dockerfile prefix changed")
    prefix = prefix.rstrip() + "\n"
    bundle = SimpleNamespace(manifest={"compatibility": {"values": compatibility}})

    with tempfile.TemporaryDirectory(prefix="buh-effective-build-") as temporary:
        context = Path(temporary)
        conf = context / "conf"
        conf.mkdir()
        shutil.copyfile(FIXTURES / "aa-docker-requirements.txt", conf / "requirements.txt")
        shutil.copyfile(FIXTURES / PACKAGE, conf / PACKAGE)
        for mode, dockerfile in (
            ("legacy", prefix),
            (
                "corrected",
                prefix.replace(OLD_INSTALL, NEW_INSTALL)
                + DockerHost._packaging_install_line(expected_packaging)
                + "\n"
                + "\n".join(DockerHost._production_dependencies_install_lines(bundle))
                + "\n",
            ),
        ):
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            image = f"buh-auth54-effective-build:{mode}"
            try:
                _run(
                    "docker", "build", "--pull", "--progress=plain",
                    "--build-arg", f"AA_DOCKER_TAG={base}",
                    "--tag", image, str(context),
                )
                actual = _probe(image)
                if mode == "legacy":
                    if actual["installed"] == expected_core:
                        raise SystemExit("Legacy helper did not reproduce the downgrade")
                elif actual != {
                    "installed": expected_core,
                    "imported": expected_core,
                    "packaging": expected_packaging,
                }:
                    raise SystemExit("Corrected effective build has wrong core or packaging")
                if mode == "corrected":
                    locked = _probe(image, LOCKED_PROBE)
                    expected = {
                        name: version
                        for name, (version, _) in LOCKED_PRODUCTION_DEPENDENCIES.items()
                    }
                    if locked != expected:
                        raise SystemExit("Corrected effective build has wrong locked dependencies")
                    print(f"corrected locked dependencies: {locked}")
                print(f"{mode} effective build: {actual}")
            finally:
                subprocess.run(
                    ["docker", "image", "rm", "--force", image],
                    check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )


if __name__ == "__main__":
    main()
