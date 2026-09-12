"""Execution regressions for release-workflow workspace preparation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, mock

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
RECOVERY_ACTIVATION = "e0bd37fafcedee3135aa4c4d6bdfc7778d032d48"
RECOVERY_CONTINUATION = "57e0e29042bab3a989c80e5473ad552ec5b4505b"
PUBLISHED_V062 = "6074b965cbd2e6ab2630cd539ee455b8d419aef6"
RECOVERY_HARNESS_PATHS = (
    "tests/deploy/test_request_archive.py",
    "tests/platform/test_coordinated_recovery_rehearsal.py",
)
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


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {completed.stderr[-1200:]}"
        )
    return completed.stdout.strip()


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

    def test_release_plan_step_accepts_only_the_current_recovery_report(self):
        script = _step_script(
            "reusable-source-tests.yml",
            "release_ledger",
            "Validate the release plan and change fragments",
        )
        source = "a" * 40
        report = {
            "latest_release": {"platform_version": "0.6.2"},
            "next_release": {
                "deployment_predecessor": None,
                "platform_version": "0.6.3",
                "previous_platform_version": "0.6.2",
                "release_required": True,
            },
            "recovery_id": "published-platform-v0.6.2-validation-20260909",
            "schema_version": 6,
            "source_commit": source,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recovery_plan = root / "recovery-plan.json"

            def execute() -> subprocess.CompletedProcess[str]:
                recovery_plan.write_text(
                    json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                return _execute_step(
                    script,
                    root,
                    {
                        "BOOTSTRAP": "false",
                        "PREVIOUS_RELEASE_PATH": "",
                        "RECOVERY_PLAN": _bash_path(recovery_plan),
                        "SOURCE_SHA": source,
                        "TEST_RUN_URL": "https://example.invalid/recovery-test",
                    },
                )

            accepted = execute()
            self.assertEqual(
                accepted.returncode,
                0,
                msg=(accepted.stdout + accepted.stderr)[-2000:],
            )
            report["schema_version"] = 1
            rejected = execute()
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "Published-release recovery plan is invalid",
                rejected.stdout + rejected.stderr,
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

    def test_recovery_upload_digest_handoff_is_canonical_and_fail_closed(self):
        """Execute upload output -> approval -> protected check -> authorization."""

        from tests.release import test_platform_approval as fixture

        script = _step_script(
            "source-published-release-recovery.yml",
            "attest",
            "Canonicalize the verified upload digest",
        )

        def canonicalize(root: Path, bare: str):
            output = root / "github-output.txt"
            completed = _execute_step(
                script,
                root,
                {
                    "GITHUB_OUTPUT": _bash_path(output),
                    "UPLOAD_ARTIFACT_DIGEST": bare,
                },
            )
            values = {}
            if output.is_file():
                for line in output.read_text(encoding="utf-8").splitlines():
                    key, separator, value = line.partition("=")
                    if separator:
                        values[key] = value
            return completed, values

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bare = fixture.RECOVERY_ARTIFACT_DIGEST.removeprefix("sha256:")
            completed, values = canonicalize(root, bare)
            self.assertEqual(
                completed.returncode,
                0,
                msg=(completed.stdout + completed.stderr)[-2000:],
            )
            self.assertEqual(
                values, {"artifact_digest": fixture.RECOVERY_ARTIFACT_DIGEST}
            )

            ready_transport = fixture.RecoveryTransport(ready=True)
            ready_report = fixture.approval.ready_recovery(
                fixture._recovery_contract(),
                fixture.TOKEN,
                owner=fixture.OWNER,
                repository=fixture.REPOSITORY,
                api_url="https://api.github.com",
                server_url="https://github.com",
                output=root / "recovery-ready.json",
                feature_readiness=fixture._published_readiness(),
                activation_readiness=fixture._published_readiness(
                    pull_request=53,
                    feature_head=fixture.ACTIVATION_HEAD,
                    merge_source_commit=fixture.ACTIVATION,
                ),
                continuation_readiness=fixture._published_readiness(
                    pull_request=54,
                    feature_head=fixture.CONTINUATION_HEAD,
                    merge_source_commit=fixture.CONTINUATION,
                ),
                repair_readiness=fixture._published_readiness(
                    pull_request=55,
                    feature_head=fixture.REPAIR_HEAD,
                    merge_source_commit=fixture.REPAIR,
                ),
                publication_repair=fixture._publication_repair_evidence(),
                publication_readiness=fixture._published_readiness(
                    pull_request=56,
                    feature_head=fixture.PUBLICATION_HEAD,
                    merge_source_commit=fixture.PUBLICATION,
                ),
                validation_attestation=fixture._recovery_attestation(),
                validation_artifact_id=fixture.RECOVERY_ARTIFACT_ID,
                validation_artifact_name=fixture.RECOVERY_ARTIFACT,
                validation_artifact_digest=values["artifact_digest"],
                transport=ready_transport,
                sleeper=lambda _: None,
            )
            self.assertNotIn(
                ("POST", f"/repos/{fixture.REPOSITORY}/check-runs"),
                ready_transport.calls,
            )
            self.assertIn(
                (
                    "POST",
                    f"/repos/{fixture.REPOSITORY}/issues/{fixture.PR_NUMBER}/comments",
                ),
                ready_transport.calls,
            )
            self.assertEqual(
                ready_report["ready"]["recovery_validation"]["required_check"][
                    "check_run_id"
                ],
                fixture.RECOVERED_REQUIRED_CHECK,
            )
            self.assertEqual(
                ready_report["ready"]["recovery_validation"][
                    "publication_repair"
                ]["publication_repair_commit"],
                fixture.PUBLICATION,
            )

            with mock.patch.object(
                fixture.approval.validation_recovery,
                "load_contract",
                return_value=fixture._recovery_contract(),
            ):
                authorized = fixture.approval.authorize(
                    fixture._recovery_event(),
                    owner=fixture.OWNER,
                    repository=fixture.REPOSITORY,
                    actor=fixture.OWNER,
                    triggering_actor=fixture.OWNER,
                    run_attempt=1,
                    api_url="https://api.github.com",
                    server_url="https://github.com",
                    output=root / "authorized.json",
                    token=fixture.TOKEN,
                    transport=fixture.RecoveryTransport(
                        ready=False, payload=ready_report["ready"]
                    ),
                    sleeper=lambda _: None,
                )
            self.assertEqual(
                authorized["validation_mode"], "published-release-recovery"
            )
            self.assertEqual(authorized["release_commit"], fixture.RELEASE)

        for invalid in ("", "sha256:" + "d" * 64, "D" * 64, "d" * 63):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rejected_transport = fixture.RecoveryTransport(ready=True)
                completed, values = canonicalize(root, invalid)
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(values, {})
                self.assertFalse(rejected_transport.calls)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed, values = canonicalize(root, "c" * 64)
            self.assertEqual(completed.returncode, 0)
            mismatched_transport = fixture.RecoveryTransport(ready=True)
            with self.assertRaisesRegex(
                fixture.approval.ApprovalError,
                "artifact digest changed",
            ):
                fixture.approval.ready_recovery(
                    fixture._recovery_contract(),
                    fixture.TOKEN,
                    owner=fixture.OWNER,
                    repository=fixture.REPOSITORY,
                    api_url="https://api.github.com",
                    server_url="https://github.com",
                    output=root / "rejected.json",
                    feature_readiness=fixture._published_readiness(),
                    activation_readiness=fixture._published_readiness(
                        pull_request=53,
                        feature_head=fixture.ACTIVATION_HEAD,
                        merge_source_commit=fixture.ACTIVATION,
                    ),
                    continuation_readiness=fixture._published_readiness(
                        pull_request=54,
                        feature_head=fixture.CONTINUATION_HEAD,
                        merge_source_commit=fixture.CONTINUATION,
                    ),
                    repair_readiness=fixture._published_readiness(
                        pull_request=55,
                        feature_head=fixture.REPAIR_HEAD,
                        merge_source_commit=fixture.REPAIR,
                    ),
                    publication_repair=fixture._publication_repair_evidence(),
                    publication_readiness=fixture._published_readiness(
                        pull_request=56,
                        feature_head=fixture.PUBLICATION_HEAD,
                        merge_source_commit=fixture.PUBLICATION,
                    ),
                    validation_attestation=fixture._recovery_attestation(),
                    validation_artifact_id=fixture.RECOVERY_ARTIFACT_ID,
                    validation_artifact_name=fixture.RECOVERY_ARTIFACT,
                    validation_artifact_digest=values["artifact_digest"],
                    transport=mismatched_transport,
                    sleeper=lambda _: None,
                )
            self.assertNotIn(
                ("POST", f"/repos/{fixture.REPOSITORY}/check-runs"),
                mismatched_transport.calls,
            )
            self.assertNotIn(
                (
                    "POST",
                    f"/repos/{fixture.REPOSITORY}/issues/{fixture.PR_NUMBER}/comments",
                ),
                mismatched_transport.calls,
            )

    def test_deploy_stages_approval_code_from_the_exact_sync_merge(self):
        script = _step_script(
            "deploy-platform-v2.yml",
            "deploy",
            "Stage the merge-owned approval verifier",
        )
        required = (
            "buh_release.py",
            "ledger.py",
            "open_sync_pr.py",
            "platform_approval.py",
            "published-release-recovery-v0.6.2.json",
            "recovery_policy.py",
            "validation_recovery.py",
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            base = Path(temporary)
            origin = base / "origin"
            origin.mkdir()
            _git(origin, "init", "--quiet", "--initial-branch=main")
            _git(origin, "config", "user.name", "Workflow Fixture")
            _git(
                origin,
                "config",
                "user.email",
                "workflow-fixture@example.invalid",
            )
            release_dir = origin / "ops/release"
            release_dir.mkdir(parents=True)
            for name in required:
                (release_dir / name).write_text(
                    f"source:{name}\n", encoding="utf-8"
                )
            _git(origin, "add", "--all")
            _git(origin, "commit", "--quiet", "-m", "source")
            source = _git(origin, "rev-parse", "HEAD")

            _git(origin, "checkout", "--quiet", "-b", "published", source)
            (origin / "published.txt").write_text("immutable\n", encoding="utf-8")
            _git(origin, "add", "--all")
            _git(origin, "commit", "--quiet", "-m", "release")
            published = _git(origin, "rev-parse", "HEAD")

            _git(origin, "checkout", "--quiet", "main")
            (release_dir / "platform_approval.py").write_text(
                "activation-verifier\n", encoding="utf-8"
            )
            _git(origin, "add", "--all")
            _git(origin, "commit", "--quiet", "-m", "reviewed repair")
            repair = _git(origin, "rev-parse", "HEAD")

            _git(origin, "checkout", "--quiet", "-b", "sync-updated", published)
            _git(
                origin,
                "merge",
                "--quiet",
                "--no-ff",
                "main",
                "-m",
                "update synchronization branch",
            )
            sync_head = _git(origin, "rev-parse", "HEAD")
            self.assertEqual(
                _git(origin, "show", "-s", "--format=%P", sync_head).split(),
                [published, repair],
            )

            _git(origin, "checkout", "--quiet", "main")
            _git(
                origin,
                "merge",
                "--quiet",
                "--no-ff",
                "sync-updated",
                "-m",
                "approved synchronization merge",
            )
            merge = _git(origin, "rev-parse", "HEAD")
            self.assertEqual(
                _git(origin, "show", "-s", "--format=%P", merge).split(),
                [repair, sync_head],
            )

            checkout = base / "checkout"
            _git(base, "clone", "--quiet", str(origin), str(checkout))
            _git(checkout, "checkout", "--quiet", "--detach", published)
            runner_temp = base / "runner-temp"
            runner_temp.mkdir()
            output = base / "github-output.txt"
            completed = _execute_step(
                script,
                checkout,
                {
                    "APPROVAL_SOURCE_COMMIT": merge,
                    "GITHUB_OUTPUT": "../github-output.txt",
                    "RELEASE_COMMIT": published,
                    "RUNNER_TEMP": "../runner-temp",
                    "SYNC_HEAD_COMMIT": sync_head,
                },
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=(completed.stdout + completed.stderr)[-4000:],
            )
            staged = (
                runner_temp
                / "buh-platform-approval-source/ops/release/platform_approval.py"
            )
            self.assertEqual(
                staged.read_text(encoding="utf-8"), "activation-verifier\n"
            )
            output_value = output.read_text(encoding="utf-8").strip()
            self.assertTrue(output_value.startswith("tool="))
            self.assertTrue(output_value.endswith("/ops/release/platform_approval.py"))
            self.assertEqual(_git(checkout, "rev-parse", "HEAD"), published)

    def test_reusable_fast_step_applies_actual_harness_to_exact_v062(self):
        script = _step_script(
            "reusable-source-tests.yml",
            "fast",
            "Apply the bounded published-release test harness",
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            base = Path(temporary)
            origin = base / "origin"
            _git(
                base,
                "clone",
                "--quiet",
                "--no-checkout",
                "-c",
                "core.autocrlf=false",
                str(ROOT),
                str(origin),
            )
            _git(origin, "config", "user.name", "Recovery Workflow Fixture")
            _git(
                origin,
                "config",
                "user.email",
                "recovery-workflow@example.invalid",
            )
            _git(origin, "fetch", "--no-tags", str(ROOT), PUBLISHED_V062)
            _git(origin, "branch", "published-v0.6.2", PUBLISHED_V062)
            _git(
                origin,
                "checkout",
                "--quiet",
                "-b",
                "recovery-repair",
                RECOVERY_CONTINUATION,
            )
            contract = json.loads(
                (
                    ROOT
                    / "ops/release/published-release-recovery-v0.6.2.json"
                ).read_text(encoding="ascii")
            )
            for relative in contract["repair"]["allowed_paths"]:
                source = ROOT / relative
                destination = origin / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            _git(origin, "add", "--all")
            _git(origin, "commit", "--quiet", "-m", "reviewed recovery repair")
            feature = _git(origin, "rev-parse", "HEAD")
            feature_tree = _git(origin, "rev-parse", f"{feature}^{{tree}}")
            repair = _git(
                origin,
                "commit-tree",
                feature_tree,
                "-p",
                RECOVERY_CONTINUATION,
                "-p",
                feature,
                "-m",
                "synthetic reviewed digest repair",
            )
            _git(origin, "branch", "reviewed-digest-repair", repair)

            checkout = base / "checkout"
            _git(
                base,
                "clone",
                "--quiet",
                "-c",
                "core.autocrlf=false",
                str(origin),
                str(checkout),
            )
            _git(checkout, "checkout", "--quiet", "--detach", PUBLISHED_V062)
            runner_temp = base / "runner-temp"
            runner_temp.mkdir()
            original_manifest = (
                checkout / "releases/platform/v0.6.2/RELEASE.json"
            ).read_bytes()
            completed = _execute_step(
                script,
                checkout,
                {
                    "HARNESS_SHA": repair,
                    "RUNNER_TEMP": "../runner-temp",
                    "SOURCE_SHA": PUBLISHED_V062,
                },
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=(completed.stdout + completed.stderr)[-4000:],
            )
            self.assertEqual(
                _git(checkout, "diff", "--name-only").splitlines(),
                list(RECOVERY_HARNESS_PATHS),
            )
            for relative in RECOVERY_HARNESS_PATHS:
                self.assertEqual(
                    (checkout / relative).read_bytes(),
                    subprocess.run(
                        ["git", "show", f"{feature}:{relative}"],
                        cwd=origin,
                        check=True,
                        capture_output=True,
                    ).stdout,
                )
            support_root = checkout / contract["repair"]["test_support_root"]
            support_paths = contract["repair"]["test_support_paths"]
            self.assertEqual(
                _git(checkout, "ls-files", "--others", "--exclude-standard").splitlines(),
                [f"{contract['repair']['test_support_root']}/{path}" for path in support_paths],
            )
            for relative in support_paths:
                self.assertEqual(
                    (support_root / relative).read_bytes(),
                    subprocess.run(
                        ["git", "show", f"{feature}:{relative}"],
                        cwd=origin,
                        check=True,
                        capture_output=True,
                    ).stdout,
                )
            self.assertEqual(
                (checkout / "releases/platform/v0.6.2/RELEASE.json").read_bytes(),
                original_manifest,
            )
            combined = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "tests.platform.test_coordinated_recovery_rehearsal",
                ],
                cwd=checkout,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(
                combined.returncode,
                0,
                msg=(combined.stdout + combined.stderr)[-4000:],
            )
