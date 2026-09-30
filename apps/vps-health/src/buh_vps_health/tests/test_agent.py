import importlib.util
import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

AGENT_PATH = (
    Path(__file__).resolve().parents[2] / "host-agent" / "buh-vps-health-agent.py"
)
SPEC = importlib.util.spec_from_file_location("buh_vps_health_host_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class HostAgentTests(TestCase):
    def test_byte_and_json_parsers_accept_real_docker_shapes(self):
        self.assertEqual(agent.human_bytes("1.5GiB / 4GiB"), 1610612736)
        self.assertEqual(agent.percent("12.34%"), 12.34)
        self.assertEqual(
            agent.parse_json_lines('{"Name":"one"}\n{"Name":"two"}\n'),
            [{"Name": "one"}, {"Name": "two"}],
        )

    def test_token_and_operation_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("secret\n", encoding="utf-8")
            with mock.patch.object(agent, "TOKEN_PATH", token_path):
                self.assertFalse(agent.handle_request({"operation": "metrics"})["ok"])
                rejected = agent.handle_request(
                    {"token": "secret", "operation": "shell", "command": "id"}
                )
                self.assertFalse(rejected["ok"])
                self.assertIn("Unknown operation", rejected["error"])

    def test_worker_count_update_is_atomic_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / ".env"
            env.write_text("COMPOSE_PROJECT_NAME=aa-docker\nBUH_AUTH_WORKER_COUNT=2\n")
            with mock.patch.object(agent, "APP_DIR", root):
                agent.write_worker_count(7)
                self.assertEqual(agent.desired_workers(), 7)
            self.assertIn("BUH_AUTH_WORKER_COUNT=7", env.read_text())

    def test_restart_discovery_allows_future_workers_but_not_databases(self):
        result = mock.Mock(
            returncode=0,
            stdout=(
                "allianceauth_gunicorn\nallianceauth_worker\n"
                "allianceauth_worker_priority\nallianceauth_beat\n"
                "auth_mysql\nredis\nproxy\n"
            ),
        )
        with mock.patch.object(agent, "compose", return_value=result):
            services = agent.restart_service_names()
        self.assertIn("allianceauth_worker_priority", services)
        self.assertIn("nginx", services)
        self.assertNotIn("auth_mysql", services)
        self.assertNotIn("redis", services)

    def test_action_status_rejects_path_traversal(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(agent, "ACTIONS_DIR", Path(directory)),
        ):
            self.assertIsNone(agent.read_action("../../etc/passwd"))
            action_id = "00000000-0000-4000-8000-000000000001"
            (Path(directory) / f"{action_id}.json").write_text(
                json.dumps({"id": action_id}), encoding="utf-8"
            )
            self.assertEqual(agent.read_action(action_id)["id"], action_id)

    def test_update_scan_combines_read_only_sources(self):
        with (
            mock.patch.object(
                agent,
                "host_package_updates",
                return_value={"updates": [{"name": "openssl"}], "count": 1, "errors": []},
            ),
            mock.patch.object(
                agent,
                "python_package_updates",
                return_value={"installed_buh": [], "updates": [], "count": 0, "errors": []},
            ),
            mock.patch.object(
                agent,
                "local_wheel_updates",
                return_value={"updates": [], "count": 0, "errors": []},
            ),
            mock.patch.object(
                agent,
                "docker_image_updates",
                return_value={"images": [], "count": 2, "errors": []},
            ),
        ):
            result = agent.run_update_scan()
        self.assertEqual(result["summary"]["total_updates"], 3)
        self.assertEqual(result["summary"]["host_packages"], 1)
        self.assertEqual(result["summary"]["docker_images"], 2)
