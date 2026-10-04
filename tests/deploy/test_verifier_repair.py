
"""Single-file activation is gated, backed up and restores its exact input on failure."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ops.deploy import verifier_repair as repair
from ops.deploy.contracts import DeploymentError
from ops.deploy.docker_host import DockerHost
from tests.deploy.test_docker_host import simulated_root_owned_lstat
from tests.deploy.test_verifier_corrections import retained_host
from tests.deploy.verifier_fixtures import nickname_case


class PreviousSlotReviewTests(unittest.TestCase):
    """Exercise the real traffic flag, retained loader and independent probes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host, self.attempt = retained_host(Path(self.temp.name))
        self.host.previous_web_slots = (f"buh-web-previous-{self.attempt}-1",)
        self.host.candidate_web_slots = (f"buh-web-candidate-{self.attempt}-1",)
        self.roles = [*self.host.config.auth_services, self.host.config.database_service,
                      self.host.config.redis_service, self.host.config.proxy_service]
        self.ids = {role: str(number) * 64 for number, role in enumerate(self.roles, 1)}
        self.started = "2026-10-01T00:00:00.123456789Z"
        self.host.restart_baselines = {identity: 0 for identity in self.ids.values()}
        self.host.restart_baseline_services = {identity: role for role, identity in self.ids.items()}
        with mock.patch.object(self.host, "_verify_previous_static_fallback"), mock.patch.object(
                self.host, "_activate_upstream"), mock.patch.object(self.host, "_public_smoke_checks"):
            self.host._protect_static_collection_traffic()
        self.save_plan()
        self.baseline = {"containers": [
            {"id": identity, "image_id": self.image(role)} for role, identity in self.ids.items()]}
        _, discord = nickname_case()
        discord["container_id"] = self.ids[self.host.config.auth_services[2]]
        self.assessment = {
            "attempt_id": self.attempt, "hold_sha256": repair.digest(self.path.read_bytes()),
            "host_evidence_sha256": "a" * 64, "host_observed_at": "2026-10-03T19:05:00+00:00",
            "host_containers": [{"service": role, "id": identity[:12], "state": "running",
                                 "oom_killed": False, "health": "healthy", "restarts": 0,
                                 "started_at": self.started} for role, identity in self.ids.items()],
            "discord_429": discord,
        }
        self.prior = {
            "attempt_id": self.attempt, "supported_recovery_completed": False,
            "deployment_attempted": False, "started_at": "2026-10-03T18:56:59+00:00",
            "before_host": {"hold_sha256": self.assessment["hold_sha256"],
                            "live_auth_services": {role: [{"container_id": self.ids[role],
                                                          "image_id": self.image(role)}]
                                                   for role in self.host.config.auth_services}},
        }
        self.slot = {"name": self.host.previous_web_slots[0], "container_id": "8" * 64,
                     "image_id": self.image(self.host.config.gunicorn_service),
                     "restart_count": 0, "started_at": self.started}
        self.route = self.host._render_upstream(
            self.host.previous_web_slots, backup_targets=(self.host.config.gunicorn_service,))
        self.set_routing_proof(list(self.host.previous_web_slots), [self.host.config.gunicorn_service])
        self.host._upstream_path().write_bytes(self.route.encode())
        self.configuration = self.nginx_config(self.route)
        self.container_details = {identity: self.details(identity, role, self.image(role))
                                  for role, identity in self.ids.items()}
        self.container_details[self.slot["name"]] = self.details(
            self.slot["container_id"], self.slot["name"], self.slot["image_id"])
        self.enterContext(mock.patch.object(repair, "private_read", side_effect=lambda path: Path(path).read_bytes()))
        self.fallback = self.enterContext(mock.patch.object(self.host, "_verify_previous_static_fallback"))
        self.run = self.enterContext(mock.patch.object(
            self.host, "_run", side_effect=lambda args, **kwargs: self.container_details[args[-1]]))
        self.discover = self.enterContext(mock.patch.object(
            self.host, "_running_service_containers", side_effect=lambda role, **kwargs: (self.ids[role],)))
        self.proxy = self.enterContext(mock.patch.object(self.host, "_proxy_exec", side_effect=self.proxy_read))

    def image(self, role):
        return self.host.previous_images.get(role, ("sha256:" + "c" * 64, ""))[0]

    def save_plan(self):
        self.host._save_recovery_plan(self.attempt, "candidate-slot-start-1")
        self.path = self.host.config.state_dir / "active-recovery.json"
        self.plan = json.loads(self.path.read_bytes())
        # The retained incident used the old schema without saved counts.
        self.plan.pop("restart_baselines")
        self.plan.pop("restart_baseline_services")
        raw = json.dumps(self.plan).encode()
        self.path.write_bytes(raw)
        (self.host.backup_path / "RECOVERY.json").write_bytes(raw)

    def details(self, identity, name, image):
        return f"{identity}|/{name}|{image}|running|false|0|{self.started}|healthy"

    def nginx_config(self, upstream):
        return upstream + ("server { server_name auth.b-uh.com; location / { "
                           "proxy_pass http://buh_platform_v2_active; } }")

    def set_routing_proof(self, targets, backups):
        self.assessment["previous_slot_routing"] = {
            "schema_version": 1, "attempt_id": self.attempt,
            "plan_sha256": self.assessment["hold_sha256"],
            "host_evidence_sha256": self.assessment["host_evidence_sha256"],
            "slots": [copy.deepcopy(self.slot)], "targets": targets, "backup_targets": backups,
            "upstream_sha256": repair.digest(self.host._render_upstream(targets, backup_targets=backups).encode()),
        }

    def proxy_read(self, *args, **kwargs):
        if args[0] == "sha256sum":
            return repair.digest(self.route.encode()) + "  upstream.conf"
        self.assertEqual(args, ("nginx", "-T"))
        return self.configuration

    def review(self):
        return repair.build_review(self.host, self.plan, self.assessment, self.baseline, self.prior)

    def test_previous_safety_traffic_is_accepted_with_exact_independent_evidence_without_clearing_flags(self):
        original = self.path.read_bytes()
        result = self.review()
        self.assertEqual(result["attempt_id"], self.attempt)
        self.assertEqual(result["plan_sha256"], repair.digest(original))
        self.assertEqual(result["discord_429"], self.assessment["discord_429"])
        self.assertTrue(self.plan["flags"]["traffic_switch_started"])
        self.assertTrue(self.host.traffic_switch_started)
        self.assertFalse(self.host.workers_replacement_started)
        self.assertFalse(self.host.gunicorn_replacement_started)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((self.host.backup_path / "RECOVERY.json").read_bytes(), original)
        self.fallback.assert_called_once()
        self.assertEqual(len(self.run.call_args_list), len(self.roles) + 1)
        self.assertEqual([call.args[:2] for call in self.proxy.call_args_list],
                         [("sha256sum", self.host.config.nginx_upstream_container_file), ("nginx", "-T")])
        review_path = self.host.backup_path / "VERIFIER-REVIEW.json"
        review_path.write_text(json.dumps(result))
        review_path.chmod(0o600)
        with simulated_root_owned_lstat(self.path), simulated_root_owned_lstat(review_path):
            loaded, plan = DockerHost.load_incomplete_plan(self.host.config)
        self.assertTrue(loaded.traffic_switch_started)
        self.assertTrue(plan["flags"]["traffic_switch_started"])
        self.assertEqual(loaded.restart_baselines, self.host.restart_baselines)
        self.assertEqual(self.path.read_bytes(), original)

    def test_supported_restored_old_gunicorn_routes_are_also_proved_without_candidate_fallback(self):
        for backups in (list(self.host.previous_web_slots), []):
            with self.subTest(backups=backups):
                self.set_routing_proof([self.host.config.gunicorn_service], backups)
                self.route = self.host._render_upstream((self.host.config.gunicorn_service,), backup_targets=backups)
                self.host._upstream_path().write_bytes(self.route.encode())
                self.configuration = self.nginx_config(self.route)
                self.review()

    def test_reviewed_safety_switch_keeps_the_original_traffic_first_rollback(self):
        self.review()
        order = []
        with mock.patch.object(self.host, "_activate_upstream", side_effect=lambda targets, **kwargs: order.append(tuple(targets))), mock.patch.object(
                self.host, "_restore_candidate_configuration", side_effect=lambda: order.append("configuration")), mock.patch.object(
                self.host, "_verify_restored", side_effect=lambda *_: order.append("verification")), mock.patch.object(
                self.host, "_public_smoke_checks"), mock.patch.object(self.host, "_remove_web_slots", side_effect=lambda *_: order.append("slots")), mock.patch.object(
                self.host, "_discard_previous_image_pins"), mock.patch.object(self.host, "complete_recovery_plan"):
            self.host.rollback(None, self.plan["phase"])
        self.assertEqual(order, [self.host.previous_web_slots, "configuration",
                                 (self.host.config.gunicorn_service,), "verification",
                                 (self.host.config.gunicorn_service,), "slots"])
        self.assertTrue(self.host.traffic_switch_started)

    def test_candidate_mixed_unknown_and_missing_routing_are_rejected_even_when_bytes_match(self):
        candidate = self.host.candidate_web_slots[0]
        for targets, backups in (([candidate], []), ([self.slot["name"], candidate], []),
                                 ([self.slot["name"]], [candidate]), (["unknown-slot"], [])):
            with self.subTest(targets=targets, backups=backups):
                self.set_routing_proof(targets, backups)
                self.route = self.host._render_upstream(targets, backup_targets=backups)
                self.host._upstream_path().write_bytes(self.route.encode())
                self.configuration = self.nginx_config(self.route)
                with self.assertRaises(DeploymentError):
                    self.review()
        self.assessment.pop("previous_slot_routing")
        with self.assertRaises(DeploymentError):
            self.review()

    def test_candidate_unknown_decoy_or_mismatched_effective_upstream_cannot_pass_a_safe_host_file(self):
        candidate = self.host._render_upstream(self.host.candidate_web_slots)
        for configuration in (self.nginx_config(candidate), self.configuration + candidate,
                              self.configuration.replace("http://buh_platform_v2_active", "http://unknown"),
                              self.configuration.replace("location /", "location /decoy")):
            with self.subTest(configuration=configuration):
                self.configuration = configuration
                with self.assertRaises(DeploymentError):
                    self.review()

    def test_missing_failed_or_changed_independent_probes_are_rejected(self):
        with mock.patch.object(self.host, "_verify_previous_static_fallback", side_effect=DeploymentError("snapshot changed")):
            with self.assertRaises(DeploymentError):
                self.review()
        with mock.patch.object(self.host, "_proxy_exec", return_value="b" * 64):
            with self.assertRaises(DeploymentError):
                self.review()
        self.host._upstream_path().write_bytes(self.host._render_upstream(self.host.candidate_web_slots).encode())
        with self.assertRaises(DeploymentError):
            self.review()

    def test_replacements_and_changed_phase_or_attempt_plan_evidence_are_rejected(self):
        cases = [("flags", "workers_replacement_started", True),
                 ("flags", "gunicorn_replacement_started", True),
                 (None, "phase", "worker-replacement-started"), (None, "attempt_id", "gh-999-1")]
        for section, name, value in cases:
            with self.subTest(name=name):
                altered = copy.deepcopy(self.plan)
                (altered[section] if section else altered)[name] = value
                raw = json.dumps(altered).encode()
                self.path.write_bytes(raw)
                (self.host.backup_path / "RECOVERY.json").write_bytes(raw)
                assessment = copy.deepcopy(self.assessment)
                prior = copy.deepcopy(self.prior)
                assessment["hold_sha256"] = repair.digest(raw)
                assessment["previous_slot_routing"]["plan_sha256"] = repair.digest(raw)
                prior["before_host"]["hold_sha256"] = repair.digest(raw)
                with self.assertRaises(DeploymentError):
                    repair.build_review(self.host, altered, assessment, self.baseline, prior)
        original_raw = json.dumps(self.plan).encode()
        self.path.write_bytes(original_raw)
        (self.host.backup_path / "RECOVERY.json").write_bytes(original_raw)
        for name in ("attempt_id", "plan_sha256", "host_evidence_sha256", "upstream_sha256"):
            with self.subTest(proof=name):
                original = self.assessment["previous_slot_routing"][name]
                self.assessment["previous_slot_routing"][name] = "gh-999-1" if name == "attempt_id" else "b" * 64
                with self.assertRaises(DeploymentError):
                    self.review()
                self.assessment["previous_slot_routing"][name] = original
        self.prior["attempt_id"] = "gh-999-1"
        with self.assertRaises(DeploymentError):
            self.review()

    def test_pre_switch_review_remains_supported_without_a_routing_exception(self):
        self.plan["flags"]["traffic_switch_started"] = False
        self.host.traffic_switch_started = False
        raw = json.dumps(self.plan).encode()
        self.path.write_bytes(raw)
        (self.host.backup_path / "RECOVERY.json").write_bytes(raw)
        self.assessment["hold_sha256"] = repair.digest(raw)
        self.prior["before_host"]["hold_sha256"] = repair.digest(raw)
        self.assessment.pop("previous_slot_routing")
        self.review()
        self.proxy.assert_not_called()
        self.run.assert_not_called()

    def test_changed_slot_and_live_service_identity_image_restart_start_oom_or_health_are_rejected(self):
        for key in (self.slot["name"], *self.ids.values()):
            original = self.container_details[key]
            for part, value in ((0, "f" * 64), (2, "sha256:" + "f" * 64), (3, "exited"),
                                (4, "true"), (5, "1"), (6, "2026-10-03T19:10:00.123456789Z"),
                                (7, "unhealthy")):
                with self.subTest(container=key, part=part):
                    fields = original.split("|")
                    fields[part] = value
                    self.container_details[key] = "|".join(fields)
                    with self.assertRaises(DeploymentError):
                        self.review()
            self.container_details[key] = original
        with mock.patch.object(self.host, "_running_service_containers", return_value=("f" * 64,)):
            with self.assertRaises(DeploymentError):
                self.review()

    def test_altered_retained_bytes_or_backup_cannot_reuse_the_approved_digest(self):
        for path in (self.path, self.host.backup_path / "RECOVERY.json"):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(DeploymentError):
                    self.review()
                path.write_bytes(original)

    def test_plan_changed_during_independent_probes_still_blocks_review(self):
        def changed(*args, **kwargs):
            self.path.write_bytes(self.path.read_bytes() + b" ")
        with mock.patch.object(self.host, "_verify_previous_static_fallback", side_effect=changed):
            with self.assertRaises(DeploymentError):
                self.review()

    def test_slot_names_count_ids_and_reviewed_previous_image_cannot_be_rebound(self):
        proof = self.assessment["previous_slot_routing"]
        for field, value in (("name", self.host.candidate_web_slots[0]), ("container_id", "f" * 64),
                             ("image_id", "sha256:" + "f" * 64), ("restart_count", 1),
                             ("restart_count", True), ("started_at", "2026-10-03T19:10:00.123456789Z")):
            with self.subTest(field=field, value=value):
                original = proof["slots"][0][field]
                proof["slots"][0][field] = value
                with self.assertRaises(DeploymentError):
                    self.review()
                proof["slots"][0][field] = original
        proof["slots"] = []
        with self.assertRaises(DeploymentError):
            self.review()


class VerifierActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        target = self.library / "ops/deploy/docker_host.py"
        target.parent.mkdir(parents=True)
        self.old, self.new = b"VALUE = 1\n", b"VALUE = 2\n"
        target.write_bytes(self.old)
        self.target = target
        self.candidate = self.root / "candidate.py"
        self.candidate.write_bytes(self.new)
        self.review = self.root / "review.json"
        self.review.write_bytes(b'{"attempt_id":"gh-123-1"}')
        self.backups = self.root / "receiver-backups"
        self.backups.mkdir()
        self.host_backup = self.root / "retained-attempt"
        self.host_backup.mkdir()
        self.host = SimpleNamespace(backup_path=self.host_backup,
                                    retained_verifier_review={"attempt_id": "gh-123-1"})
        self.receipt = self.root / "REPAIR.json"
        self.original_bytes = b'{"source_commit":"synthetic-original"}'
        real_private = repair.private_read
        def read(path, *args, **kwargs):
            if str(path) == "/etc/buh-platform-v2/INSTALL.json":
                return self.original_bytes
            return Path(path).read_bytes()
        self.enterContext(mock.patch.object(repair, "private_read", side_effect=read))
        self.enterContext(mock.patch.object(repair, "LIBRARY", self.library))
        self.enterContext(mock.patch.object(repair, "RECEIPT", self.receipt))
        self.enterContext(mock.patch.object(repair, "BASE_DOCKER_SHA256", repair.digest(self.old)))
        self.enterContext(mock.patch.object(repair, "_verify_root_owned_ancestors"))
        self.enterContext(mock.patch.object(repair, "installed_identity", return_value={}))
        self.real_private = real_private
        real_path = repair.Path
        def paths(value):
            return self.backups if str(value) == "/var/backups/buh-receiver-upgrade" else real_path(value)
        self.enterContext(mock.patch.object(repair, "Path", side_effect=paths))
        def atomic(path, raw, mode, **kwargs):
            Path(path).write_bytes(raw)
            Path(path).chmod(mode)
        self.enterContext(mock.patch.object(repair, "_atomic_bytes", side_effect=atomic))

    def activate(self):
        return repair.install_single_file(self.host, self.candidate, repair.digest(self.new),
                                          self.review, "a" * 40)

    def test_only_runtime_override_changes_and_original_install_and_evidence_are_retained(self):
        held = self.host_backup / "RECOVERY.json"
        held.write_bytes(b"original retained plan")
        result = self.activate()
        self.assertEqual(self.target.read_bytes(), self.new)
        self.assertEqual(held.read_bytes(), b"original retained plan")
        backup = Path(result["backup_path"])
        self.assertEqual((backup / "docker_host.py").read_bytes(), self.old)
        self.assertEqual((backup / "INSTALL.json").read_bytes(), self.original_bytes)
        self.assertEqual((backup / "VERIFIER-REVIEW.json").read_bytes(), self.review.read_bytes())
        self.assertFalse(result["deployment_performed"])
        self.assertFalse(result["memberaudit_mutation_performed"])
        self.assertEqual(json.loads(self.receipt.read_bytes())["sha256"], repair.digest(self.new))

    def test_failed_installed_validation_restores_the_exact_old_runtime_and_keeps_backups(self):
        with mock.patch.object(repair, "installed_identity", side_effect=DeploymentError("synthetic mismatch")):
            with self.assertRaises(DeploymentError):
                self.activate()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(next(self.backups.glob("*/docker_host.py")).read_bytes(), self.old)

    def test_source_mismatch_or_existing_receipt_stops_before_replacement(self):
        self.candidate.write_bytes(b"unexpected")
        with self.assertRaises(DeploymentError):
            self.activate()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.candidate.write_bytes(self.new)
        self.receipt.write_bytes(b"existing evidence")
        with self.assertRaises(DeploymentError):
            self.activate()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.assertEqual(self.receipt.read_bytes(), b"existing evidence")

    def test_all_independent_checks_are_read_and_any_failure_remains_visible(self):
        success = mock.Mock(return_value=None)
        fail = mock.Mock(side_effect=DeploymentError("synthetic fatal"))
        warning = mock.Mock(return_value=("recovered warning",))
        host = SimpleNamespace(restored_health_checks=lambda *_: [
            ("services-and-restarts", fail), ("django", success), ("retained-interval-logs", warning)])
        checks, warnings = repair.all_checks(host)
        self.assertEqual([item["passed"] for item in checks], [False, True, True])
        self.assertEqual(warnings, ["recovered warning"])
        success.assert_called_once()

    def test_absolute_and_parent_traversal_inputs_fail_before_read(self):
        for path in ("relative.json", "/root/../tmp/assessment.json"):
            with self.subTest(path=path), self.assertRaises(DeploymentError):
                self.real_private(path)


class CompletedMemberAuditReferenceTests(unittest.TestCase):
    def test_incomplete_or_wrong_completed_population_cannot_be_recovered_or_cleared(self):
        for completed in ({}, {"recovery_complete": False},
                          {"recovery_complete": True, "recovery_source_commit": "other"}):
            with self.subTest(completed=completed), self.assertRaises(DeploymentError):
                repair.memberaudit_refs(completed, [])

    def test_no_auth_or_token_update_operations_exist_in_read_only_preservation_program(self):
        self.assertIn("SET TRANSACTION READ ONLY", repair.MA_READ_CODE)
        for forbidden in (".save(", ".delete(", ".update(", ".refresh(", "bulk_refresh(", "has_token_error ="):
            self.assertNotIn(forbidden, repair.MA_READ_CODE)


class SupportedCleanupOrderingTests(unittest.TestCase):
    def test_fresh_memberaudit_or_structures_failure_preserves_all_safety_resources(self):
        for failure in ("memberaudit", "structures"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                host, _ = retained_host(Path(temp))
                calls = []
                def member(current):
                    calls.append("memberaudit")
                    if failure == "memberaudit":
                        raise DeploymentError("synthetic changed recovered section")
                def structures(current):
                    calls.append("structures")
                    if failure == "structures":
                        raise DeploymentError("synthetic unhealthy Structures owner")
                host.__class__ = repair.reconciliation_host(DockerHost, member, structures)
                with mock.patch.object(host, "_restore_candidate_configuration"), mock.patch.object(
                        DockerHost, "_verify_restored", side_effect=lambda *_: calls.append("health")), mock.patch.object(
                        host, "_remove_web_slots") as cleanup, mock.patch.object(
                        host, "_discard_previous_image_pins") as pins, mock.patch.object(
                        host, "complete_recovery_plan") as complete, self.assertRaises(DeploymentError):
                    host.rollback(None, "candidate-slot-start-1")
                cleanup.assert_not_called()
                pins.assert_not_called()
                complete.assert_not_called()
                self.assertEqual(calls[0:2], ["health", "memberaudit"])
                self.assertTrue((host.config.state_dir / "active-recovery.json").exists())

    def test_all_health_and_preservation_gates_precede_supported_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            host, _ = retained_host(Path(temp))
            order = []
            host.__class__ = repair.reconciliation_host(
                DockerHost, lambda *_: order.append("memberaudit"), lambda *_: order.append("structures"))
            with mock.patch.object(host, "_restore_candidate_configuration"), mock.patch.object(
                    DockerHost, "_verify_restored", side_effect=lambda *_: order.append("health")), mock.patch.object(
                    host, "_remove_web_slots", side_effect=lambda *_: order.append("slots")), mock.patch.object(
                    host, "_discard_previous_image_pins", side_effect=lambda *_: order.append("pins")), mock.patch.object(
                    host, "complete_recovery_plan", side_effect=lambda *_: order.append("hold")):
                host.rollback(None, "candidate-slot-start-1")
            self.assertEqual(order, ["health", "memberaudit", "structures", "slots", "pins", "hold"])
