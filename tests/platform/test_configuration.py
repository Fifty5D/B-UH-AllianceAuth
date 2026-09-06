"""Cross-file guardrails for the permanent source-test platform."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path
from unittest import TestCase

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ENV_FILE = ROOT / "platform" / "testenv" / "env.example"
SOURCE_WORKFLOWS = (
    "source-ci.yml",
    "reusable-source-tests.yml",
    "source-compatibility.yml",
    "source-supply-chain.yml",
    "ui-preview.yml",
    "invalidate-readiness.yml",
)
PRODUCTION_V2_WORKFLOWS = (
    "auto-platform-release.yml",
    "build-platform-release.yml",
    "deploy-approved-platform-release.yml",
    "deploy-platform-v2.yml",
    "production-runtime-fingerprint.yml",
    "vps-diagnostics.yml",
)
LEGACY_WORKFLOWS = (
    "deploy-moon-tax.yml",
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects silently shadowed mapping keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise AssertionError(
                f"Duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_workflow(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)


def _env_values() -> dict[str, str]:
    values = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in values:
            raise AssertionError(f"Malformed or duplicate env.example entry: {raw_line!r}")
        values[key] = value
    return values


def _git_file_mode(path: Path) -> str:
    """Read the portable executable-bit contract from the Git index."""

    relative = path.relative_to(ROOT).as_posix()
    completed = subprocess.run(
        ["git", "ls-files", "--stage", "--", relative],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    fields = completed.stdout.rstrip("\n").split(maxsplit=3)
    if len(fields) != 4 or fields[3] != relative:
        raise AssertionError(f"Git index lacks one canonical entry for {relative}")
    return fields[0]


def _exact_requirements(path: Path) -> dict[str, str]:
    requirements = {}
    exact = re.compile(r"([A-Za-z0-9_.-]+)==([^\s;]+)")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-r ")):
            continue
        match = exact.fullmatch(line)
        if not match:
            raise AssertionError(f"Requirement is not an exact pin: {raw_line!r}")
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        if name in requirements:
            raise AssertionError(f"Duplicate requirement: {name}")
        requirements[name] = match.group(2)
    return requirements


class PlatformConfigurationContracts(TestCase):
    def setUp(self):
        with (ROOT / "platform" / "compatibility.toml").open("rb") as stream:
            self.compatibility = tomllib.load(stream)
        self.images = _env_values()

    def test_contract_schema_and_required_image_registry(self):
        self.assertEqual(self.compatibility["schema_version"], 1)
        self.assertIs(
            self.compatibility["policy"][
                "legacy_bootstrap_may_skip_uninstalled_v2_releases"
            ],
            True,
        )
        self.assertEqual(
            set(self.compatibility["production_baseline"]),
            {
                "platform_version",
                "release_commit",
                "release_path",
                "deployment_generation",
            },
        )
        for app in self.compatibility["applications"].values():
            self.assertNotIn("version", app)
        self.assertEqual(
            set(self.images),
            {
                "BUH_PYTHON_IMAGE",
                "BUH_MARIADB_IMAGE",
                "BUH_REDIS_IMAGE",
                "BUH_FAKE_ESI_IMAGE",
                "BUH_PLAYWRIGHT_IMAGE",
            },
        )
        safe_image = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:_@+-]{1,255}")
        for name, image in self.images.items():
            with self.subTest(name=name):
                self.assertRegex(image, safe_image)
                self.assertNotRegex(image, r"(?i)(?::|@)latest(?:$|[-.])")
                self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")

    def test_platform_release_fingerprint_covers_observer_boundary(self):
        with (ROOT / "ops" / "release" / "apps.toml").open("rb") as stream:
            registry = tomllib.load(stream)
        inputs = set(registry["platform"]["platform_build_inputs"])
        self.assertIn("ops/bootstrap-observer.sh", inputs)
        self.assertIn("ops/buh-github-observe-*", inputs)
        self.assertIn("ops/deploy/**/*", inputs)
        self.assertIn("ops/readiness.py", inputs)
        self.assertIn(".github/workflows/invalidate-readiness.yml", inputs)
        self.assertIn(".github/workflows/vps-diagnostics.yml", inputs)
        self.assertIn("setup/Upgrade-BUH-PlatformV2Receiver.ps1", inputs)

    def test_production_runtime_is_pinned_to_the_observed_allianceauth_image(self):
        runtime = self.compatibility["production_runtime"]
        self.assertEqual(set(runtime), {"base_image"})
        self.assertRegex(
            runtime["base_image"],
            r"^registry\.gitlab\.com/allianceauth/allianceauth/auth"
            r"@sha256:[0-9a-f]{64}$",
        )

    def test_reviewed_images_match_the_compatibility_matrix(self):
        runtime = self.compatibility["runtime"]
        self.assertRegex(
            self.images["BUH_PYTHON_IMAGE"],
            rf"^python:{re.escape(runtime['python'])}-",
        )
        self.assertRegex(
            self.images["BUH_FAKE_ESI_IMAGE"],
            rf"^python:{re.escape(runtime['python'])}-",
        )
        self.assertTrue(
            self.images["BUH_MARIADB_IMAGE"].startswith(
                f"mariadb:{runtime['mariadb']}"
            )
        )
        self.assertTrue(
            self.images["BUH_REDIS_IMAGE"].startswith(f"redis:{runtime['redis']}")
        )
        self.assertIn(
            f"playwright:v{runtime['playwright']}-",
            self.images["BUH_PLAYWRIGHT_IMAGE"],
        )

    def test_exact_requirement_pins_match_the_reviewed_matrix(self):
        runtime = self.compatibility["runtime"]
        production = _exact_requirements(
            ROOT / "platform" / "requirements" / "production.txt"
        )
        self.assertEqual(
            production,
            {
                "allianceauth": runtime["allianceauth"],
                "django": runtime["django"],
                "django-esi": runtime["django_esi"],
                "aa-memberaudit": runtime["memberaudit"],
                "django-eveuniverse": runtime["eveuniverse"],
                "celery": runtime["celery"],
                "django-redis": runtime["django_redis"],
                "redis": runtime["redis_python"],
                "pyyaml": runtime["pyyaml"],
                "requests": runtime["requests"],
                "packaging": runtime["packaging"],
            },
        )
        self.assertEqual(
            _exact_requirements(ROOT / "platform" / "requirements" / "build.txt"),
            {
                name: self.compatibility["tooling"][name]
                for name in ("pip", "setuptools", "wheel", "uv")
            },
        )
        self.assertEqual(
            _exact_requirements(ROOT / "platform" / "requirements" / "test.txt"),
            self.compatibility["tooling"],
        )

    def test_requirement_manifests_and_script_entry_points_are_canonical(self):
        requirements = ROOT / "platform" / "requirements"
        self.assertFalse(list(requirements.glob("*.in")))
        self.assertEqual(
            {path.name for path in requirements.glob("*.txt")},
            {"build.txt", "production.txt", "test.txt"},
        )
        self.assertEqual(
            {path.name for path in requirements.glob("*.lock")},
            {"build.lock", "production.lock", "test.lock"},
        )
        for name in (
            "run-fast.sh",
            "run-integration.sh",
            "run-upgrade.sh",
            "run-browser.sh",
        ):
            path = ROOT / "platform" / "testenv" / name
            with self.subTest(path=path):
                self.assertEqual(_git_file_mode(path), "100755")
        fast_script = (
            ROOT / "platform" / "testenv" / "run-fast.sh"
        ).read_text(encoding="utf-8")
        unit_settings = (
            ROOT / "platform" / "testauth" / "testauth" / "settings" / "unit.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("docker", fast_script.lower())
        self.assertIn("LocMemCache", unit_settings)
        test_urls = (
            ROOT / "platform" / "testauth" / "testauth" / "urls.py"
        ).read_text(encoding="utf-8")
        for app in ("buh_moon_tax", "buh_structure_ops", "buh_mining_analytics"):
            self.assertNotIn(f'include("{app}.urls")', test_urls)

    def test_docker_and_compose_defaults_match_the_reviewed_registry(self):
        dockerfile = (ROOT / "platform" / "testenv" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        browser_dockerfile = (
            ROOT / "platform" / "testenv" / "Dockerfile.playwright"
        ).read_text(encoding="utf-8")
        self.assertIn(
            f"ARG PYTHON_IMAGE={self.images['BUH_PYTHON_IMAGE']}", dockerfile
        )
        self.assertIn("--require-hashes", dockerfile)
        self.assertIn("platform/requirements/build.lock", dockerfile)
        self.assertIn("platform/requirements/production.lock", dockerfile)
        self.assertIn("--no-build-isolation", dockerfile)
        self.assertIn("--no-deps", dockerfile)
        self.assertIn(
            f"ARG PLAYWRIGHT_IMAGE={self.images['BUH_PLAYWRIGHT_IMAGE']}",
            browser_dockerfile,
        )

        compose = yaml.safe_load(
            (ROOT / "platform" / "testenv" / "compose.yml").read_text(
                encoding="utf-8"
            )
        )
        services = compose["services"]
        expected = {
            "db": ("BUH_MARIADB_IMAGE", "image"),
            "redis": ("BUH_REDIS_IMAGE", "image"),
            "fake-esi": ("BUH_FAKE_ESI_IMAGE", "image"),
        }
        for service, (variable, key) in expected.items():
            with self.subTest(service=service):
                self.assertEqual(
                    services[service][key],
                    f"${{{variable}:-{self.images[variable]}}}",
                )
        self.assertEqual(
            services["browser"]["build"]["args"]["PLAYWRIGHT_IMAGE"],
            f"${{BUH_PLAYWRIGHT_IMAGE:-{self.images['BUH_PLAYWRIGHT_IMAGE']}}}",
        )

    def test_disposable_network_is_internal_and_normal_browser_is_focused(self):
        compose = yaml.safe_load(
            (ROOT / "platform" / "testenv" / "compose.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(compose["networks"]["test-internal"]["internal"], True)
        for name, service in compose["services"].items():
            with self.subTest(service=name):
                self.assertEqual(service.get("networks"), ["test-internal"])
        self.assertEqual(
            compose["services"]["browser"]["command"],
            ["npx", "playwright", "test", "moon-tax.spec.ts", "console-theme.spec.ts"],
        )
        self.assertNotIn("ports", compose["services"]["redis"])
        self.assertEqual(
            compose["services"]["test"]["environment"][
                "BUH_TEST_SUPPORT_ENABLED"
            ],
            "1",
        )
        common_settings = (
            ROOT / "platform" / "testauth" / "testauth" / "settings" / "common.py"
        ).read_text(encoding="utf-8")
        browser_settings = (
            ROOT / "platform" / "testauth" / "testauth" / "settings" / "browser.py"
        ).read_text(encoding="utf-8")
        test_views = (
            ROOT
            / "platform"
            / "testauth"
            / "testauth"
            / "test_support"
            / "views.py"
        ).read_text(encoding="utf-8")
        seed_command = (
            ROOT
            / "platform"
            / "testauth"
            / "testauth"
            / "test_support"
            / "management"
            / "commands"
            / "buh_seed_test_platform.py"
        ).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("BUH_TEST_SUPPORT_ENABLED", "0")', common_settings)
        self.assertNotIn("BUH_TEST_SUPPORT_ENABLED", browser_settings)
        self.assertIn("if not getattr(settings, \"BUH_TEST_SUPPORT_ENABLED\", False)", test_views)
        self.assertIn('if not options["reset"]', seed_command)
        self.assertIn('database.get("HOST") != "db"', seed_command)
        self.assertIn('database.get("NAME") != "buh_test"', seed_command)

    def test_parallel_and_speculative_builds_have_isolated_cache_writes(self):
        source_ci = (WORKFLOWS / "source-ci.yml").read_text(encoding="utf-8")
        compatibility = (WORKFLOWS / "source-compatibility.yml").read_text(
            encoding="utf-8"
        )
        reusable = (WORKFLOWS / "reusable-source-tests.yml").read_text(
            encoding="utf-8"
        )
        bake = (ROOT / "platform" / "testenv" / "docker-bake.hcl").read_text(
            encoding="utf-8"
        )
        self.assertIn("format('merge-{0}', github.run_id)", source_ci)
        self.assertIn("format('compatibility-{0}', github.run_id)", compatibility)
        self.assertIn("BUH_CACHE_LANE: integration", reusable)
        self.assertIn("BUH_CACHE_LANE: upgrade", reusable)
        self.assertIn("BUH_CACHE_LANE: browser", reusable)
        for lane in ("fast", "integration", "upgrade", "browser"):
            self.assertIn(
                f"BUH_COMPOSE_PROJECT: buh-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-{lane}",
                reusable,
            )
            self.assertIn(
                f"BUH_TEST_IMAGE_TAG: ci-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-{lane}",
                reusable,
            )
        self.assertIn("${BUH_CACHE_SCOPE}-${BUH_CACHE_LANE}", bake)
        self.assertEqual(bake.count("${BUH_TEST_IMAGE_TAG}"), 3)
        self.assertEqual(bake.count("timeout=10m,ignore-error=true"), 3)

    def test_every_external_action_is_pinned_to_a_full_commit(self):
        action = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
        full_sha = re.compile(r"^[^@]+@[0-9a-f]{40}$")
        # Legacy v1 deploy/diagnostics workflows stay byte-for-byte available
        # during the Platform v2 rollout. New source-first workflows enforce the
        # stronger full-SHA policy without silently rewriting that rollback path.
        for name in (*SOURCE_WORKFLOWS, *PRODUCTION_V2_WORKFLOWS):
            workflow = WORKFLOWS / name
            for use in action.findall(workflow.read_text(encoding="utf-8")):
                if use.startswith("./"):
                    continue
                with self.subTest(workflow=workflow.name, use=use):
                    self.assertRegex(use, full_sha)

    def test_diagnostics_collection_cannot_delete_artifacts(self):
        workflow = _load_workflow(WORKFLOWS / "vps-diagnostics.yml")
        self.assertEqual(workflow["permissions"], {})
        purge = workflow["jobs"]["purge-artifacts"]
        collect = workflow["jobs"]["collect"]
        self.assertEqual(
            purge["permissions"], {"actions": "write", "issues": "write"}
        )
        self.assertEqual(
            collect["permissions"], {"contents": "read", "issues": "write"}
        )
        self.assertNotIn("actions", collect["permissions"])

    def test_deploy_and_read_only_observer_credentials_have_distinct_scopes(self):
        documentation = (ROOT / "docs/operations/release-and-deployment.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "Keep the deploy-capable `BUH_DEPLOY_SSH_KEY` only in",
            "The forced-command `BUH_OBSERVER_SSH_KEY` is a separate, read-only",
            "repository-level Actions secrets",
            "Never expose `BUH_DEPLOY_SSH_KEY` to either observer-only workflow",
        ):
            self.assertIn(required, documentation)

        deploy = _load_workflow(WORKFLOWS / "deploy-platform-v2.yml")
        self.assertEqual(deploy["jobs"]["deploy"]["environment"], "production")
        deploy_text = (WORKFLOWS / "deploy-platform-v2.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("BUH_DEPLOY_SSH_KEY", deploy_text)
        self.assertIn("BUH_OBSERVER_SSH_KEY", deploy_text)

        for name in ("production-runtime-fingerprint.yml", "vps-diagnostics.yml"):
            workflow = _load_workflow(WORKFLOWS / name)
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                for job in workflow["jobs"].values():
                    self.assertNotIn("environment", job)
                self.assertIn("BUH_OBSERVER_SSH_KEY", text)
                self.assertIn("BUH_VPS_KNOWN_HOSTS", text)
                self.assertNotIn("BUH_DEPLOY_SSH_KEY", text)

    def test_every_workflow_is_explicitly_classified(self):
        present = {
            path.name
            for path in WORKFLOWS.iterdir()
            if path.is_file() and path.suffix in {".yml", ".yaml"}
        }
        self.assertEqual(
            present,
            set(SOURCE_WORKFLOWS)
            | set(PRODUCTION_V2_WORKFLOWS)
            | set(LEGACY_WORKFLOWS),
        )

    def test_every_workflow_rejects_duplicate_yaml_keys(self):
        for name in (*SOURCE_WORKFLOWS, *PRODUCTION_V2_WORKFLOWS, *LEGACY_WORKFLOWS):
            with self.subTest(workflow=name):
                self.assertIsInstance(_load_workflow(WORKFLOWS / name), dict)

    def test_local_reusable_workflow_callers_match_declared_interfaces(self):
        workflows = {
            path.name: _load_workflow(path)
            for path in WORKFLOWS.glob("*.yml")
        }
        calls = 0
        for caller_name, caller in workflows.items():
            for job_name, job in caller.get("jobs", {}).items():
                target = job.get("uses")
                if not isinstance(target, str) or not target.startswith(
                    "./.github/workflows/"
                ):
                    continue
                calls += 1
                callee_name = Path(target).name
                with self.subTest(caller=caller_name, job=job_name, callee=callee_name):
                    self.assertIn(callee_name, workflows)
                    callee = workflows[callee_name]
                    triggers = callee.get("on", callee.get(True))
                    self.assertIsInstance(triggers, dict)
                    interface = triggers.get("workflow_call")
                    self.assertIsInstance(interface, dict)

                    declared_inputs = interface.get("inputs", {})
                    supplied_inputs = job.get("with", {})
                    self.assertIsInstance(declared_inputs, dict)
                    self.assertIsInstance(supplied_inputs, dict)
                    self.assertLessEqual(
                        set(supplied_inputs),
                        set(declared_inputs),
                        "caller supplies an undeclared reusable-workflow input",
                    )
                    required_inputs = {
                        name
                        for name, definition in declared_inputs.items()
                        if definition.get("required") is True
                    }
                    self.assertLessEqual(
                        required_inputs,
                        set(supplied_inputs),
                        "caller omits a required reusable-workflow input",
                    )

                    declared_secrets = interface.get("secrets", {})
                    supplied_secrets = job.get("secrets", {})
                    self.assertIsInstance(declared_secrets, dict)
                    if supplied_secrets != "inherit":
                        self.assertIsInstance(supplied_secrets, dict)
                        self.assertLessEqual(
                            set(supplied_secrets),
                            set(declared_secrets),
                            "caller supplies an undeclared reusable-workflow secret",
                        )
                        required_secrets = {
                            name
                            for name, definition in declared_secrets.items()
                            if definition.get("required") is True
                        }
                        self.assertLessEqual(
                            required_secrets,
                            set(supplied_secrets),
                            "caller omits a required reusable-workflow secret",
                        )
        self.assertEqual(calls, 5)

    def test_supply_chain_python_setup_uses_configuration_output(self):
        workflow = _load_workflow(WORKFLOWS / "source-supply-chain.yml")
        configuration = workflow["jobs"]["configuration"]
        drift = workflow["jobs"]["drift"]
        self.assertEqual(
            configuration["outputs"]["python"],
            "${{ steps.contract.outputs.python }}",
        )
        self.assertEqual(drift["needs"], "configuration")
        setup_python = [
            step
            for step in drift["steps"]
            if step.get("uses", "").startswith("actions/setup-python@")
        ]
        self.assertEqual(len(setup_python), 1)
        self.assertEqual(
            setup_python[0]["with"]["python-version"],
            "${{ needs.configuration.outputs.python }}",
        )

    def test_nonproduction_workflows_are_secret_free(self):
        for name in SOURCE_WORKFLOWS:
            text = (WORKFLOWS / name).read_text(encoding="utf-8")
            with self.subTest(workflow=name):
                self.assertNotIn("secrets.", text)
                self.assertNotRegex(text, r"(?m)^\s*environment:\s*production\s*$")
        preview = (WORKFLOWS / "ui-preview.yml").read_text(encoding="utf-8")
        browser = (WORKFLOWS / "reusable-source-tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Artifact tree contains a symlink", preview)
        self.assertIn("Artifact tree contains a symlink", browser)
        self.assertIn(
            "BUH_COMPOSE_PROJECT: buh-${{ github.run_id }}-${{ github.run_attempt }}-preview",
            preview,
        )
        self.assertIn(
            "BUH_TEST_IMAGE_TAG: ci-${{ github.run_id }}-${{ github.run_attempt }}-preview",
            preview,
        )
        self.assertNotIn('print("BUH_TEST_IMAGE_TAG=ci"', preview)

    def test_validate_pr_preserves_required_context_and_tests_exact_head(self):
        source = _load_workflow(WORKFLOWS / "source-ci.yml")
        reusable = _load_workflow(WORKFLOWS / "reusable-source-tests.yml")
        self.assertEqual(source["name"], "Validate PR")
        self.assertEqual(source["jobs"]["source-tests"]["name"], "Source test suite")
        self.assertEqual(
            reusable["jobs"]["required"]["name"], "Required source checks"
        )
        self.assertEqual(
            source["jobs"]["source-tests"]["with"]["source_sha"],
            "${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}",
        )
        checkouts = []
        for job in reusable["jobs"].values():
            for step in job.get("steps", []):
                if step.get("uses", "").startswith("actions/checkout@"):
                    checkouts.append(step)
        self.assertGreaterEqual(len(checkouts), 7)
        for checkout in checkouts:
            self.assertEqual(checkout["with"]["ref"], "${{ inputs.source_sha }}")
            self.assertFalse(checkout["with"]["persist-credentials"])

    def test_preview_permissions_relevance_theme_matrix_and_provenance(self):
        workflow = _load_workflow(WORKFLOWS / "ui-preview.yml")
        preview = workflow["jobs"]["preview"]
        text = (WORKFLOWS / "ui-preview.yml").read_text(encoding="utf-8")
        self.assertEqual(
            preview["permissions"], {"contents": "read", "pull-requests": "read"}
        )
        self.assertIn("github.event.label.name == 'ui-preview'", preview["if"])
        self.assertEqual(
            preview["steps"][1]["with"]["ref"],
            "${{ steps.target.outputs.head_sha }}",
        )
        for required in (
            "run-name: Preview UI / PR #${{ github.event.pull_request.number",
            "^\\\\.github/workflows/(ui-preview|invalidate-readiness)\\\\.ya?ml$",
            "^ops/readiness\\\\.py$",
            "^platform/test(auth|env)/",
            "^tests/platform/test_(readiness|configuration)\\\\.py$",
            "|js|jsx|mjs|",
            "gh api --paginate --slurp",
            "EXPECTED_CHANGED_FILES",
            "EXPECTED_CHANGED_FILES >= 3000",
            'actual_changed_files}" == "${EXPECTED_CHANGED_FILES}',
            "preview.spec.ts console-preview.spec.ts \\",
            "moon-tax.spec.ts console-theme.spec.ts",
            '[[ "$(git rev-parse HEAD)" == "${PREVIEW_REF}" ]]',
            "Preview artifact lacks desktop screenshots",
            "Preview artifact lacks mobile screenshots",
            "from ops.readiness import _verify_png",
            "_verify_png((root / relative).read_bytes(), relative)",
            '"darkly", "flatly", "materia", "bootstrap", "bootstrap-dark"',
            '"workflow": ".github/workflows/ui-preview.yml"',
            '"screenshots": screenshots',
            '"playwright_report": report',
            "retention-days: 3",
        ):
            self.assertIn(required, text)
        self.assertNotIn('playwright-report/" 2>>"${REPORT_DIR}/run.log" || true', text)

    def test_trusted_readiness_never_checks_out_pull_request_code(self):
        workflow = _load_workflow(WORKFLOWS / "invalidate-readiness.yml")
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(
            triggers,
            {
                "pull_request_target": {
                    "types": [
                        "opened",
                        "reopened",
                        "synchronize",
                        "edited",
                        "labeled",
                        "unlabeled",
                    ]
                },
                "pull_request_review": {
                    "types": ["submitted", "edited", "dismissed"]
                },
                "pull_request_review_comment": {
                    "types": ["created", "edited", "deleted"]
                },
                "issue_comment": {"types": ["created", "edited", "deleted"]},
                "workflow_run": {
                    "workflows": ["Validate PR", "Preview UI"],
                    "types": ["requested", "in_progress", "completed"],
                    "branches-ignore": ["main"],
                },
            },
        )
        policy = workflow["jobs"]["policy"]
        self.assertEqual(
            policy["permissions"],
            {
                "actions": "read",
                "checks": "read",
                "contents": "read",
                "issues": "write",
                "pull-requests": "read",
            },
        )
        checkouts = [
            step
            for step in policy["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        self.assertEqual(len(checkouts), 1)
        self.assertEqual(
            policy["steps"][0]["name"], "Immediately consume a readiness request"
        )
        consume = policy["steps"][0]
        self.assertIn("github.event.action == 'labeled'", consume["if"])
        self.assertIn("github.event.label.name == 'ready-for-work'", consume["if"])
        self.assertIn("github.event.sender.login != 'github-actions[bot]'", consume["if"])
        self.assertIn("labels/ready-for-work", consume["run"])
        self.assertIn("labels[]=needs-codex", consume["run"])
        self.assertIn('index("codex") != null', consume["run"])
        self.assertIn("EVENT_HEAD_REPOSITORY", consume["run"])
        self.assertIn("EVENT_BASE_REF", consume["run"])
        self.assertIn("'.base.ref'", consume["run"])
        self.assertIn('!= "main"', consume["run"])
        self.assertLess(
            consume["run"].index("labels/ready-for-work"),
            consume["run"].index('index("codex") != null'),
        )
        self.assertEqual(
            checkouts[0]["with"]["ref"],
            "${{ (github.event_name == 'workflow_run' || github.event_name == 'issue_comment') && github.sha || github.event.pull_request.base.sha }}",
        )
        self.assertFalse(checkouts[0]["with"]["persist-credentials"])
        step_by_name = {step["name"]: step for step in policy["steps"]}
        invalidate_if = step_by_name[
            "Invalidate current readiness before any evaluation"
        ]["if"]
        evaluate_if = step_by_name[
            "Evaluate exact-head checks, preview, and reviews"
        ]["if"]
        fail_closed_if = step_by_name[
            "Fail closed when invalidation or publication is incomplete"
        ]["if"]
        for review_event in (
            "github.event_name == 'pull_request_review'",
            "github.event_name == 'pull_request_review_comment'",
            "github.event_name == 'issue_comment'",
        ):
            self.assertIn(review_event, policy["if"])
            self.assertIn(review_event, evaluate_if)
        self.assertEqual(invalidate_if, "steps.target.outputs.eligible == 'true'")
        self.assertIn("steps.invalidate.outcome != 'success'", fail_closed_if)
        self.assertIn("steps.signal.outcome != 'success'", fail_closed_if)
        text = (WORKFLOWS / "invalidate-readiness.yml").read_text(encoding="utf-8")
        for required in (
            "python3 ops/readiness.py github",
            "github.event_name == 'workflow_run'",
            "types: [requested, in_progress, completed]",
            "branches-ignore: [main]",
            "format('ignored-{0}', github.run_id)",
            "|| 'state'",
            "workflows: [Validate PR, Preview UI]",
            "github.event_name == 'pull_request_review'",
            "github.event_name == 'pull_request_review_comment'",
            "github.event_name == 'issue_comment'",
            "github.event.issue.pull_request",
            "github.event.sender.login != 'github-actions[bot]'",
            "github.event.sender.login == 'github-actions[bot]'",
            "event_head_repository",
            "github.event.action == 'unlabeled'",
            "github.event.label.name == 'ui-preview'",
            "github.event.label.name == 'codex'",
            "github.event.action == 'edited'",
            'index("codex") != null',
            '"${current_head_repository}" != "${GITHUB_REPOSITORY}"',
            'current_base_ref="$(jq -r \'.base.ref\'',
            '"${current_base_ref}" == "main"',
            "steps.target.outputs.base_main == 'true'",
            "steps.target.outputs.publication_request == 'true'",
            "steps.readiness.outputs.qualified == 'true'",
            "steps.publish.outputs.artifact-id",
            "steps.publish.outputs.artifact-digest",
            "<!-- buh-readiness-record:v2 -->",
            'state: "qualified"',
            'state: "invalidated"',
            'review_digest="$(jq -er',
            "cmp --silent",
            "ready-for-work",
            "needs-codex",
            "ui-preview",
            "retention-days: 3",
        ):
            self.assertIn(required, text)
        self.assertLess(text.index('index("codex") != null'), text.index("gh label create"))
        invalidate_at = text.index("Invalidate current readiness before any evaluation")
        evaluate_at = text.index("Evaluate exact-head checks, preview, and reviews")
        upload_at = text.index("Upload the trusted exact-head readiness record")
        pointer_at = text.index("Publish the sole authoritative readiness pointer")
        signal_at = text.index("Reverify, clear needs-codex, and signal readiness last")
        self.assertLess(invalidate_at, evaluate_at)
        self.assertLess(evaluate_at, upload_at)
        self.assertLess(upload_at, pointer_at)
        self.assertLess(pointer_at, signal_at)
        consume_at = text.index("Immediately consume a readiness request")
        checkout_at = text.index("Check out only the trusted base policy")
        self.assertLess(consume_at, checkout_at)
        self.assertLess(
            text.index("labels/ready-for-work", consume_at), checkout_at
        )
        ready_posts = [
            index
            for index in range(len(text))
            if text.startswith("--field 'labels[]=ready-for-work'", index)
        ]
        self.assertEqual(len(ready_posts), 1)
        self.assertGreater(ready_posts[0], signal_at)
        self.assertLess(
            text.index("labels/needs-codex", signal_at), ready_posts[0]
        )
        self.assertLess(
            text.index("labels/ready-for-work", invalidate_at), evaluate_at
        )
        self.assertNotIn(
            "ref: ${{ github.event.pull_request.head.sha }}", text
        )

    def test_release_plan_and_legacy_recovery_artifact_remain_required(self):
        text = (WORKFLOWS / "reusable-source-tests.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "python3 ops/release/buh_release.py plan",
            "Release plan is not bound to the exact source head",
            "e70a8ee713d358e2f0e3c1dc5b23305c863aa628",
            "3e51f208eb715011d5af7626479af2d0f0c893da",
            "find . -maxdepth 1 -type f | wc -l",
            "find . -maxdepth 1 -type f -name '*.whl' | wc -l",
            "wc -l < SHA256SUMS",
            "sha256sum --check --strict SHA256SUMS",
            "bash -n buh-moon-tax-platform-remote-v0.3.3.sh",
            "VERSION=\"0.3.3\"",
            'install -o root -g root -m 0644 "/tmp/${TAX_WHEEL}"',
            'assert "class AssessmentAdjustment" in model',
            'assert "small_balance_threshold" in model',
            'assert \'name="decide_payment_direct"\' in urls',
            'assert \'name="member_detail"\' in urls',
            'assert \'name="character_detail"\' in urls',
            'assert "def state_hash" in exemptions',
            'assert \'"full_exemption_history_recheck"\' in auditor',
            'assert "select_for_update" in payments',
            'assert "def _billing_account_totals" in views',
            'assert "_outstanding_sum_expression()" in views',
            'assert "setupSortableTables" in javascript',
            'assert "setupRowNavigation" in javascript',
            'assert "event.submitter" in javascript',
            'assert "data-row-url" in dashboard',
            'assert "data-sortable-table" in dashboard',
            'assert "Totals by billing account" in dashboard',
            'assert "data-drop-decision" not in javascript',
            'assert "data-payment-drag" not in payment_page',
            'assert "Version: 0.3.2" in metadata',
            "buh_moon_tax/templates/buh_moon_tax/person_detail.html",
            "buh_moon_tax/migrations/0005_payment_workspace_and_bill_adjustments.py",
        ):
            self.assertIn(required, text)

    def test_required_source_lanes_remain_fail_closed(self):
        text = (WORKFLOWS / "reusable-source-tests.yml").read_text(
            encoding="utf-8"
        )
        for job in (
            "configuration",
            "release_ledger",
            "legacy_release",
            "fast",
            "integration",
            "upgrade",
            "browser",
            "required",
        ):
            self.assertRegex(text, rf"(?m)^  {re.escape(job)}:\s*$")
        self.assertIn(
            "needs: [configuration, release_ledger, legacy_release, fast, integration, upgrade, browser]",
            text,
        )
        self.assertIn('"release-ledger:${RELEASE_LEDGER_RESULT}"', text)
        self.assertIn('"legacy-release:${LEGACY_RELEASE_RESULT}"', text)
        self.assertIn('"upgrade:${UPGRADE_RESULT}"', text)
        self.assertIn('if: always()', text)
        self.assertIn('"${result}" != "success"', text)
        self.assertIn("Multiline workflow output is forbidden", text)

    def test_release_ledger_is_checked_early_and_pinned_through_publication(self):
        build = _load_workflow(WORKFLOWS / "build-platform-release.yml")
        self.assertEqual(
            build["concurrency"],
            {
                "group": (
                    "${{ inputs.publish_release_branch && "
                    "'buh-platform-release-publication' || "
                    "format('buh-platform-release-candidate-{0}', github.run_id) }}"
                ),
                "cancel-in-progress": False,
            },
        )

        candidate = build["jobs"]["candidate"]
        candidate_steps = candidate["steps"]
        candidate_names = [step["name"] for step in candidate_steps]
        checkout = candidate_steps[0]
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        ledger_index = candidate_names.index(
            "Verify the synchronized immutable release ledger"
        )
        plan_index = candidate_names.index("Plan changed applications and versions")
        pin_index = candidate_names.index(
            "Pin the planned release target to the verified ledger"
        )
        setup_index = candidate_names.index("Set up Python")
        dependency_index = candidate_names.index(
            "Install bounded native build prerequisites"
        )
        self.assertLess(ledger_index, plan_index)
        self.assertLess(plan_index, pin_index)
        self.assertLess(pin_index, setup_index)
        self.assertLess(pin_index, dependency_index)
        self.assertIn(
            "build/release-state.json",
            next(
                step
                for step in candidate_steps
                if step["name"] == "Upload the verified candidate"
            )["with"]["path"],
        )

        prepare_names = [
            step["name"]
            for step in build["jobs"]["prepare_publication"]["steps"]
        ]
        self.assertLess(
            prepare_names.index("Reverify the candidate's pinned release ledger"),
            prepare_names.index("Reconstruct and verify the delta candidate"),
        )
        publish = build["jobs"]["publish"]
        self.assertEqual(publish["permissions"], {"contents": "write"})
        publish_names = [step["name"] for step in publish["steps"]]
        self.assertLess(
            publish_names.index("Reverify the pinned release ledger before publication"),
            publish_names.index("Recompute the exact publication plan"),
        )
        self.assertLess(
            publish_names.index("Recompute the exact publication plan"),
            publish_names.index(
                "Validate payload and reconstruct the exact release commit"
            ),
        )
        recompute_plan = next(
            step
            for step in publish["steps"]
            if step["name"] == "Recompute the exact publication plan"
        )
        self.assertIn("ops/release/buh_release.py plan", recompute_plan["run"])
        self.assertIn(
            "--output build/publish-plan.json", recompute_plan["run"]
        )
        self.assertEqual(
            publish_names[-1],
            "Atomically create the absent release and synchronization branches",
        )
        self.assertEqual(
            set(publish["outputs"]),
            {"release_branch", "release_commit", "sync_branch"},
        )

        synchronize = build["jobs"]["synchronize_release"]
        self.assertEqual(
            synchronize["needs"], ["candidate", "publication_gate", "publish"]
        )
        self.assertEqual(
            synchronize["permissions"],
            {"contents": "read", "pull-requests": "write"},
        )
        self.assertEqual(synchronize["steps"][0]["with"]["fetch-depth"], 1)
        sync_step = synchronize["steps"][1]
        self.assertEqual(
            sync_step["name"],
            "Open or recover the exact synchronization pull request",
        )
        self.assertIn("ops/release/open_sync_pr.py", sync_step["run"])
        self.assertIn("--release-branch", sync_step["run"])
        self.assertIn("--sync-branch", sync_step["run"])
        release_tree_step = next(
            step
            for step in build["jobs"]["prepare_publication"]["steps"]
            if step["name"] == "Construct and validate the append-only release tree"
        )
        self.assertIn("path.read_bytes()", release_tree_step["run"])
        self.assertIn('_replace_version(path, update["to"])', release_tree_step["run"])
        self.assertNotIn("path.read_text(", release_tree_step["run"])
        self.assertNotIn("path.write_text(", release_tree_step["run"])
        build_text = (WORKFLOWS / "build-platform-release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "sha256sum metadata.json release-state.json release.patch >SHA256SUMS",
            build_text,
        )
        self.assertIn(
            "expected_files=(SHA256SUMS metadata.json release-state.json release.patch)",
            build_text,
        )
        self.assertEqual(build_text.count("ops/release/ledger.py verify"), 5)
        self.assertEqual(build_text.count("--target-version"), 4)
        self.assertIn(".previous_release == (", build_text)
        self.assertIn(
            "--slurpfile plan build/publish-plan.json",
            build_text,
        )
        self.assertIn(
            "$candidate.deployment_predecessor",
            build_text,
        )
        self.assertIn("manifest_sha256: $state.latest.manifest_sha256", build_text)
        self.assertIn("git \"${git_auth[@]}\" push --atomic --porcelain", build_text)
        self.assertIn("refs/heads/${RELEASE_BRANCH}", build_text)
        self.assertIn("refs/heads/${SYNC_BRANCH}", build_text)
        self.assertIn("platform-release-refs-before.tsv", build_text)
        self.assertIn("platform-release-refs-after.tsv", build_text)
        self.assertIn("expected_refs_after", build_text)
        self.assertIn("release_snapshot_failure", build_text)
        self.assertIn("read_remote_refs()", build_text)
        self.assertIn("timeout --signal=TERM 30s", build_text)
        self.assertIn(
            "timeout --signal=TERM --kill-after=10s 120s", build_text
        )
        self.assertIn("trap post_push_unexpected_error ERR TERM", build_text)
        helper_text = (ROOT / "ops" / "release" / "open_sync_pr.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Merge only after ChatGPT presents the evidence", helper_text)
        self.assertEqual(
            synchronize["outputs"],
            {
                "platform_version": "${{ needs.candidate.outputs.platform_version }}",
                "release_commit": "${{ needs.publish.outputs.release_commit }}",
                "source_commit": "${{ inputs.expected_source_sha }}",
                "sync_pr_number": "${{ steps.sync_pr.outputs.number }}",
                "sync_pr_url": "${{ steps.sync_pr.outputs.url }}",
            },
        )
        self.assertEqual(
            sync_step["env"]["GITHUB_TOKEN"],
            "${{ secrets.BUH_RELEASE_PR_TOKEN }}",
        )
        self.assertIn("BUH_RELEASE_PR_TOKEN is required", sync_step["run"])
        self.assertNotIn("/merges", build_text)
        self.assertNotIn("/merges", helper_text)

        source = _load_workflow(WORKFLOWS / "reusable-source-tests.yml")
        ledger = source["jobs"]["release_ledger"]
        self.assertEqual(ledger["permissions"], {"contents": "read"})
        self.assertEqual(ledger["steps"][0]["with"]["fetch-depth"], 0)
        self.assertIn(
            "release_ledger", source["jobs"]["required"]["needs"]
        )

    def test_platform_v2_deploy_is_reusable_guarded_and_fail_closed(self):
        text = (WORKFLOWS / "deploy-platform-v2.yml").read_text(encoding="utf-8")
        workflow = _load_workflow(WORKFLOWS / "deploy-platform-v2.yml")
        deploy = workflow["jobs"]["deploy"]
        self.assertEqual(deploy["environment"], "production")
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["concurrency"]["group"], "buh-production-platform-v2"
        )
        triggers = workflow.get("on", workflow.get(True))
        self.assertIn("workflow_call", triggers)
        self.assertIn("workflow_dispatch", triggers)
        normal_lineage_inputs = {
            "feature_pr_number",
            "feature_head_sha",
            "source_commit",
            "feature_readiness",
            "main_validation_run_id",
            "main_validation_run_attempt",
            "manifest_sha256",
            "preflight_run_id",
            "preflight_run_attempt",
            "preflight_artifact",
            "preflight_artifact_id",
            "preflight_artifact_digest",
            "sync_validation_run_id",
            "sync_validation_run_attempt",
        }
        self.assertTrue(
            normal_lineage_inputs.issubset(triggers["workflow_call"]["inputs"])
        )
        for name in normal_lineage_inputs:
            definition = triggers["workflow_call"]["inputs"][name]
            self.assertFalse(definition["required"], name)
            self.assertEqual(definition["default"], "", name)
            self.assertEqual(definition["type"], "string", name)
        self.assertTrue(
            normal_lineage_inputs.isdisjoint(triggers["workflow_dispatch"]["inputs"])
        )
        self.assertEqual(
            set(triggers["workflow_call"]["outputs"]),
            {"artifact_name", "manifest_sha256"},
        )
        self.assertEqual(
            set(deploy["outputs"]), {"artifact_name", "manifest_sha256"}
        )
        self.assertEqual(
            deploy["permissions"],
            {
                "actions": "read",
                "checks": "read",
                "contents": "read",
                "issues": "read",
                "pull-requests": "read",
            },
        )
        gate_text = deploy["steps"][0]["run"]
        self.assertIn("pull_request_target:deploy", gate_text)
        self.assertIn('[[ -n "${FEATURE_READINESS}" ]]', gate_text)
        self.assertIn('[[ "${PREFLIGHT_RUN_ID}" =~ ^[1-9][0-9]*$ ]]', gate_text)
        self.assertIn(
            '[[ "${PREFLIGHT_ARTIFACT_DIGEST}" =~ ^sha256:[0-9a-f]{64}$ ]]',
            gate_text,
        )
        self.assertIn(
            '[[ "${MANIFEST_SHA256}" =~ ^[0-9a-f]{64}$ ]]', gate_text
        )
        for name in (
            "MAIN_VALIDATION_RUN_ID",
            "MAIN_VALIDATION_RUN_ATTEMPT",
            "SYNC_VALIDATION_RUN_ID",
            "SYNC_VALIDATION_RUN_ATTEMPT",
        ):
            self.assertIn(f'[[ "${{{name}}}" =~ ^[1-9][0-9]*$ ]]', gate_text)
        self.assertIn(
            '"platform-v2-preflight-${PREFLIGHT_RUN_ID}-${PREFLIGHT_RUN_ATTEMPT}"',
            gate_text,
        )
        self.assertGreaterEqual(gate_text.count('[[ -z "${FEATURE_PR_NUMBER}"'), 2)
        step_names = [step["name"] for step in deploy["steps"]]
        self.assertEqual(
            step_names.index("Reverify live feature readiness at the production boundary")
            + 1,
            step_names.index("Configure guarded deploy and observer SSH identities"),
        )
        queued_authorize = step_names.index("Reauthorize the queued production operation")
        queued_download = step_names.index(
            "Download the still-retained production preflight artifact"
        )
        final_boundary = step_names.index(
            "Reverify live feature readiness at the production boundary"
        )
        self.assertLess(queued_authorize, queued_download)
        self.assertLess(queued_download, final_boundary)
        self.assertIn(
            "platform_approval.py authorize", deploy["steps"][queued_authorize]["run"]
        )
        self.assertIn(
            "platform_approval.py authorize", deploy["steps"][final_boundary]["run"]
        )
        self.assertIn(
            "platform_approval.py verify-artifact",
            deploy["steps"][final_boundary]["run"],
        )
        final_boundary_text = deploy["steps"][final_boundary]["run"]
        self.assertLess(
            final_boundary_text.rindex("ops/readiness.py published"),
            final_boundary_text.rindex("platform_approval.py authorize"),
        )
        self.assertIn(
            '[[ "${MANIFEST_SHA256}" == "${RELEASE_MANIFEST_SHA256}" ]]',
            final_boundary_text,
        )
        for field in (
            "feature_head_sha",
            "feature_pr_number",
            "main_validation_run_attempt",
            "main_validation_run_id",
            "manifest_sha256",
            "platform_version",
            "preflight_artifact",
            "preflight_artifact_digest",
            "preflight_artifact_id",
            "preflight_run_attempt",
            "preflight_run_id",
            "release_commit",
            "source_commit",
            "sync_validation_run_attempt",
            "sync_validation_run_id",
        ):
            self.assertIn(f'"{field}"', final_boundary_text)
        self.assertEqual(
            step_names.index("Configure guarded deploy and observer SSH identities") + 1,
            step_names.index("Send the verified archive through the forced command"),
        )
        for required in (
            "PREFLIGHT PLATFORM V2",
            "DEPLOY PLATFORM V2",
            "workflow_run:preflight",
            "pull_request_target:deploy",
            "workflow_dispatch:deploy",
            "BUH_CHATGPT_WORK_ACTOR",
            '[[ "${ACTOR}" == "${OWNER}" ]]',
            "release/platform-v",
            "production_runtime",
            "BUH_DEPLOY_SSH_KEY",
            "BUH_OBSERVER_SSH_KEY",
            "BUH_VPS_KNOWN_HOSTS",
            '"${MODE} platform-v2"',
            '"${RECEIVER_EXIT}" == "0"',
            '"${DIAGNOSTICS_EXIT}" == "0"',
            '"${ATTEMPT_EXIT}" == "0"',
            '"attempt gh-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
            "ops/buh-redact-diagnostics.py --validate",
            "ops/readiness.py published",
            "cmp --silent",
        ):
            self.assertIn(required, text)
        self.assertNotIn("continue-on-error", text)
        self.assertNotIn("StrictHostKeyChecking=no", text)

    def test_release_automation_stops_at_the_chatgpt_approval_boundary(self):
        automatic = _load_workflow(WORKFLOWS / "auto-platform-release.yml")
        automatic_text = (WORKFLOWS / "auto-platform-release.yml").read_text(
            encoding="utf-8"
        )
        triggers = automatic.get("on", automatic.get(True))
        self.assertEqual(
            triggers,
            {
                "workflow_run": {
                    "workflows": ["Validate PR"],
                    "types": ["completed"],
                    "branches": ["main"],
                }
            },
        )
        qualify = automatic["jobs"]["qualify"]
        self.assertEqual(
            qualify["outputs"]["release_needed"],
            "${{ steps.fragments.outputs.release_needed }}",
        )
        self.assertIn("changes/*.toml", automatic_text)
        self.assertIn("CURRENT_MAIN_SHA", automatic_text)
        self.assertIn("release_needed=false", automatic_text)
        self.assertIn("release_needed=true", automatic_text)
        release = automatic["jobs"]["release"]
        self.assertEqual(
            release["uses"], "./.github/workflows/build-platform-release.yml"
        )
        self.assertEqual(release["with"]["publish_release_branch"], True)
        self.assertEqual(release["with"]["automatic_release"], True)
        for name in (
            "source_validation_run_id",
            "source_validation_run_attempt",
            "feature_pr_number",
            "feature_head_sha",
            "feature_readiness",
        ):
            self.assertIn(name, release["with"])
        self.assertEqual(
            release["secrets"],
            {"BUH_RELEASE_PR_TOKEN": "${{ secrets.BUH_RELEASE_PR_TOKEN }}"},
        )
        self.assertNotEqual(release["secrets"], "inherit")
        preflight = automatic["jobs"]["preflight"]
        self.assertEqual(
            preflight["uses"], "./.github/workflows/deploy-platform-v2.yml"
        )
        self.assertEqual(preflight["with"]["mode"], "preflight")
        self.assertEqual(preflight["needs"], "release")
        ready = automatic["jobs"]["ready"]
        self.assertEqual(ready["needs"], ["release", "preflight"])
        self.assertEqual(
            ready["permissions"],
            {
                "actions": "read",
                "checks": "read",
                "contents": "read",
                "issues": "write",
                "pull-requests": "read",
            },
        )
        self.assertIn("platform_approval.py ready", automatic_text)
        for required in (
            "ops/readiness.py published",
            "--first-introduction-pr 44",
            "BUH_CHATGPT_WORK_ACTOR",
            "--main-validation-run-id",
            "--feature-readiness",
            "build/current-feature-readiness.json",
            "cmp --silent",
        ):
            self.assertIn(required, automatic_text)
        self.assertLess(
            automatic_text.rindex("ops/readiness.py published"),
            automatic_text.rindex("platform_approval.py ready"),
        )
        build_text = (WORKFLOWS / "build-platform-release.yml").read_text(
            encoding="utf-8"
        )
        build = _load_workflow(WORKFLOWS / "build-platform-release.yml")
        self.assertEqual(
            set(build["concurrency"]), {"group", "cancel-in-progress"}
        )
        build_triggers = build.get("on", build.get(True))
        self.assertIn("automatic_release", build_triggers["workflow_call"]["inputs"])
        self.assertNotIn(
            "automatic_release", build_triggers["workflow_dispatch"]["inputs"]
        )
        self.assertIn(
            "inputs.automatic_release && 'true' || 'false'",
            build_text,
        )
        for required in (
            "automatic_release",
            "workflow_dispatch",
            "authorization_mode=automatic",
            "authorization_mode=manual",
            "REQUESTED_SOURCE_ID",
            "Feature readiness changed after release qualification",
        ):
            self.assertIn(required, build_text)
        self.assertIn(
            "buh-platform-ready:v2",
            (ROOT / "ops" / "release" / "platform_approval.py").read_text(
                encoding="utf-8"
            ),
        )
        self.assertNotRegex(
            automatic_text,
            r"(?m)^\s+mode:\s*deploy\s*$",
        )

    def test_only_an_exact_chatgpt_approved_merge_can_start_production(self):
        workflow = _load_workflow(
            WORKFLOWS / "deploy-approved-platform-release.yml"
        )
        text = (WORKFLOWS / "deploy-approved-platform-release.yml").read_text(
            encoding="utf-8"
        )
        triggers = workflow.get("on", workflow.get(True))
        self.assertEqual(
            triggers,
            {"pull_request_target": {"branches": ["main"], "types": ["closed"]}},
        )
        self.assertNotIn("workflow_dispatch", triggers)
        authorize = workflow["jobs"]["authorize"]
        self.assertIn("github.event.pull_request.merged == true", authorize["if"])
        self.assertEqual(
            authorize["steps"][0]["with"]["ref"], "${{ github.sha }}"
        )
        self.assertFalse(authorize["steps"][0]["with"]["persist-credentials"])
        self.assertIn(
            'github.event.pull_request.merge_commit_sha',
            authorize["steps"][1]["run"],
        )
        artifact_download = next(
            step
            for step in authorize["steps"]
            if step["name"] == "Download the exact retained preflight artifact"
        )
        self.assertIn("artifact-ids", artifact_download["with"])
        self.assertTrue(artifact_download["with"]["merge-multiple"])
        deploy = workflow["jobs"]["deploy"]
        self.assertEqual(deploy["needs"], "authorize")
        self.assertEqual(
            authorize["steps"][-1]["name"],
            "Close authorization races before deploy",
        )
        self.assertEqual(
            authorize["outputs"]["feature_readiness"],
            "${{ steps.final_gate.outputs.feature_readiness }}",
        )
        self.assertEqual(
            workflow["jobs"]["report"]["permissions"],
            {
                "contents": "read",
                "issues": "write",
                "pull-requests": "write",
            },
        )
        self.assertEqual(deploy["with"]["mode"], "deploy")
        self.assertEqual(deploy["with"]["confirmation"], "DEPLOY PLATFORM V2")
        reusable = _load_workflow(WORKFLOWS / "deploy-platform-v2.yml")
        reusable_triggers = reusable.get("on", reusable.get(True))
        self.assertTrue(
            set(deploy["with"]).issubset(
                reusable_triggers["workflow_call"]["inputs"]
            )
        )
        self.assertEqual(
            deploy["permissions"],
            {
                "actions": "read",
                "checks": "read",
                "contents": "read",
                "issues": "read",
                "pull-requests": "read",
            },
        )
        for name in (
            "feature_pr_number",
            "feature_head_sha",
            "source_commit",
            "feature_readiness",
            "main_validation_run_id",
            "main_validation_run_attempt",
            "manifest_sha256",
            "preflight_run_id",
            "preflight_run_attempt",
            "preflight_artifact",
            "preflight_artifact_id",
            "preflight_artifact_digest",
            "sync_validation_run_id",
            "sync_validation_run_attempt",
        ):
            self.assertIn(name, deploy["with"])
            self.assertEqual(
                deploy["with"][name],
                "${{ needs.authorize.outputs." + name + " }}",
            )
        for required in (
            "platform_approval.py authorize",
            "platform_approval.py verify-artifact",
            "ops/readiness.py published",
            "artifact-ids:",
            "Current feature readiness differs from the approved record",
            "BUH_CHATGPT_WORK_ACTOR",
            "--first-introduction-pr 44",
            "buh-platform-deploy-result:v1",
            "pull_request_target is trusted only",
        ):
            self.assertIn(required, text)
        self.assertLess(
            text.rindex("platform_approval.py verify-artifact"),
            text.rindex("ops/readiness.py published"),
        )
        self.assertLess(
            text.rindex("ops/readiness.py published"),
            text.index("  deploy:\n"),
        )
        helper = (ROOT / "ops" / "release" / "platform_approval.py").read_text(
            encoding="utf-8"
        )
        for required in (
            "buh-chatgpt-approved:v2",
            "Exactly one GitHub Actions readiness marker",
            "Merge commit lacks the ChatGPT approval marker",
            "Synchronization PR was not merged with a merge commit",
            "Recorded sync PR Source CI evidence is invalid",
            "Recorded main Validate PR evidence is invalid",
            "preflight_artifact_digest",
            "feature_readiness",
            "Receiver evidence does not prove the exact preflight",
        ):
            self.assertIn(required, helper)

    def test_production_fingerprint_is_owner_only_and_read_only(self):
        text = (WORKFLOWS / "production-runtime-fingerprint.yml").read_text(
            encoding="utf-8"
        )
        workflow = _load_workflow(
            WORKFLOWS / "production-runtime-fingerprint.yml"
        )
        self.assertEqual(
            workflow["permissions"], {"contents": "read", "issues": "write"}
        )
        for required in (
            "/fingerprint platform-v2",
            "github.repository_owner",
            "BUH_OBSERVER_SSH_KEY",
            "StrictHostKeyChecking=yes",
            '"fingerprint platform-v2"',
            "production_runtime_base_image",
            "ops/buh-redact-diagnostics.py --validate",
        ):
            self.assertIn(required, text)
        self.assertNotIn("BUH_DEPLOY_SSH_KEY", text)
        self.assertNotIn("continue-on-error", text)
        self.assertNotIn("StrictHostKeyChecking=no", text)

    def test_release_publication_requires_a_deployable_runtime_contract(self):
        text = (WORKFLOWS / "build-platform-release.yml").read_text(
            encoding="utf-8"
        )
        workflow = _load_workflow(WORKFLOWS / "build-platform-release.yml")
        gate = workflow["jobs"]["publication_gate"]
        steps = gate["steps"]
        contract_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "Require a deployable production runtime contract"
        )
        checkout_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"]
            == "Check out the exact source for the runtime contract gate"
        )
        checkout = steps[checkout_index]
        self.assertEqual(gate["permissions"], {"contents": "read"})
        self.assertLess(checkout_index, contract_index)
        self.assertEqual(
            checkout["with"]["ref"], "${{ inputs.expected_source_sha }}"
        )
        self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertIn("Require a deployable production runtime contract", text)
        self.assertIn('contract.get("production_runtime")', text)
        self.assertIn("production_runtime.base_image", text)
        self.assertIn("Publication is blocked", text)

    def test_receiver_bootstrap_scripts_are_executable_and_do_not_edit_ssh(self):
        deploy_dir = ROOT / "ops/deploy"
        for name in (
            "buh-deploy-dispatch",
            "buh-platform-v2-receiver",
            "install-receiver.sh",
        ):
            path = deploy_dir / name
            self.assertEqual(_git_file_mode(path), "100755")
        installer = (deploy_dir / "install-receiver.sh").read_text()
        dispatcher = (deploy_dir / "buh-deploy-dispatch").read_text()
        self.assertNotIn("authorized_keys", installer)
        for observer_target in (
            "/usr/local/bin/buh-github-observe-entry",
            "/usr/local/sbin/buh-github-observe-root",
            "/usr/local/libexec/buh-redact-diagnostics",
        ):
            self.assertIn(observer_target, installer)
        self.assertIn('"deploy moon-tax"', dispatcher)
        self.assertIn('"preflight platform-v2"', dispatcher)
        self.assertIn('"deploy platform-v2"', dispatcher)


if __name__ == "__main__":
    import unittest

    unittest.main()
