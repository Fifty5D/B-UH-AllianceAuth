"""Execution regressions for release-workflow workspace preparation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
_OUTPUT_STUB = """\
from pathlib import Path
import sys

arguments = sys.argv[1:]
output = Path(arguments[arguments.index("--output") + 1])
output.write_text('{"ready":true}\\n', encoding="ascii")
"""


def _step_script(workflow_name: str, job_name: str, step_name: str) -> str:
    workflow = yaml.safe_load(
        (WORKFLOWS / workflow_name).read_text(encoding="utf-8")
    )
    steps = workflow["jobs"][job_name]["steps"]
    return next(step["run"] for step in steps if step["name"] == step_name)


def _bash_executable() -> str:
    discovered = shutil.which("bash")
    if discovered:
        return discovered
    windows_git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if windows_git_bash.is_file():
        return str(windows_git_bash)
    raise AssertionError("bash is required to execute release-workflow regressions")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    drive = resolved.drive.removesuffix(":").lower()
    suffix = resolved.as_posix()[2:]
    return f"/{drive}{suffix}"


def _execute_step(script: str, root: Path, environment: dict[str, str]):
    rendered = re.sub(r"\$\{\{.*?\}\}", "fixture", script)
    rendered = rendered.replace("python3", '"${TEST_PYTHON}"')
    process_environment = os.environ.copy()
    process_environment.update(environment)
    process_environment["TEST_PYTHON"] = _bash_path(Path(sys.executable))
    return subprocess.run(
        [_bash_executable(), "-c", rendered],
        cwd=root,
        env=process_environment,
        check=False,
        capture_output=True,
        text=True,
    )


class ReleaseWorkflowWorkspaceTests(TestCase):
    def _workspace(self, root: Path, *, approval_stub: bool = False) -> None:
        readiness = root / "ops" / "readiness.py"
        readiness.parent.mkdir(parents=True)
        readiness.write_text(_OUTPUT_STUB, encoding="utf-8")
        if approval_stub:
            approval = root / "ops" / "release" / "platform_approval.py"
            approval.parent.mkdir(parents=True)
            approval.write_text(_OUTPUT_STUB, encoding="utf-8")

    def test_source_gate_reverification_creates_build_after_checkout(self):
        script = _step_script(
            "build-platform-release.yml",
            "source_gate",
            "Reverify exact published feature readiness",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._workspace(root)
            self.assertFalse((root / "build").exists())
            completed = _execute_step(
                script,
                root,
                {
                    "AUTOMATIC_RELEASE": "true",
                    "EXPECTED_FEATURE_READINESS": '{"ready":true}',
                    "EXPECTED_SOURCE_SHA": "b" * 40,
                    "FEATURE_HEAD_SHA": "a" * 40,
                    "FEATURE_PR_NUMBER": "46",
                    "GITHUB_OUTPUT": str(root / "github-output.txt"),
                    "GITHUB_TOKEN": "synthetic-token",
                    "REPOSITORY": "example/repository",
                    "REPOSITORY_OWNER": "example",
                    "WORK_ACTOR": "",
                },
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=(completed.stdout + completed.stderr)[-4000:],
            )
            self.assertEqual(
                (root / "build" / "feature-readiness.json").read_text(
                    encoding="ascii"
                ),
                '{"ready":true}\n',
            )

    def test_ready_job_reverification_creates_build_in_fresh_checkout(self):
        script = _step_script(
            "auto-platform-release.yml",
            "ready",
            "Reverify evidence and publish the readiness marker",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._workspace(root, approval_stub=True)
            self.assertFalse((root / "build").exists())
            completed = _execute_step(
                script,
                root,
                {
                    "FEATURE_HEAD_SHA": "a" * 40,
                    "FEATURE_PR_NUMBER": "46",
                    "FEATURE_READINESS": '{"ready":true}',
                    "GITHUB_TOKEN": "synthetic-token",
                    "REPOSITORY": "example/repository",
                    "REPOSITORY_OWNER": "example",
                    "SOURCE_COMMIT": "b" * 40,
                    "WORK_ACTOR": "",
                },
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=(completed.stdout + completed.stderr)[-4000:],
            )
            expected = '{"ready":true}\n'
            for name in (
                "feature-readiness.json",
                "current-feature-readiness.json",
                "platform-approval-ready.json",
            ):
                self.assertEqual(
                    (root / "build" / name).read_text(encoding="ascii"),
                    expected,
                )
