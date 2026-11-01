import importlib.util
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

AGENT_PATH = Path(__file__).resolve().parents[2] / "host-agent" / "buh-vps-health-agent.py"
SPEC = importlib.util.spec_from_file_location("buh_vps_health_host_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class HostAgentTests(TestCase):
    def filesystem_metrics(
        self, *, mount_data=True, duplicate=False, error=None, unmount_during_stat=False
    ):
        with tempfile.TemporaryDirectory() as directory:
            mountinfo = Path(directory) / "mountinfo"
            lines = ["1 0 8:1 / / rw - ext4 /dev/system rw"]
            if mount_data:
                number = "8:1" if duplicate else "8:17"
                lines.append(f"2 1 {number} / /srv/buh-data rw - ext4 /dev/data rw")
            lines += [
                "3 1 0:9 / /var/lib/docker/rootfs/overlayfs/example rw - overlay overlay rw",
                "4 1 8:17 /mysql-data /opt/aa-docker/mysql-data rw - ext4 /dev/data rw",
                "5 1 8:17 /esi-archive /opt/aa-docker/conf/buh-esi-archive rw - ext4 /dev/data rw",
            ]
            mountinfo.write_text("\n".join(lines) + "\n")
            devices = {"/": 1, "/srv/buh-data": 1 if duplicate else 17}
            descriptors = {"/": 101, "/srv/buh-data": 102}
            by_descriptor = {101: devices["/"], 102: devices["/srv/buh-data"]}
            unmounted = False

            def path_stat(path):
                device = 1 if unmounted and path == "/srv/buh-data" else devices[path]
                return SimpleNamespace(st_dev=device)

            def filesystem_stat(descriptor):
                nonlocal unmounted
                path = "/" if descriptor == 101 else "/srv/buh-data"
                if path == error:
                    raise OSError("Synthetic unavailable filesystem")
                if descriptor == 102 and unmount_during_stat:
                    unmounted = True
                return SimpleNamespace(
                    f_blocks=1000 if descriptor == 101 else 2000,
                    f_bfree=700 if descriptor == 101 else 1000,
                    f_bavail=650 if descriptor == 101 else 900,
                    f_frsize=1,
                )

            with ExitStack() as patches:
                patches.enter_context(mock.patch.object(agent, "MOUNTINFO_PATH", mountinfo))
                patches.enter_context(
                    mock.patch.object(agent.os, "stat", side_effect=path_stat)
                )
                patches.enter_context(
                    mock.patch.object(
                        agent.os, "open", side_effect=lambda path, _flags: descriptors[path]
                    )
                )
                patches.enter_context(mock.patch.object(agent.os, "close"))
                patches.enter_context(
                    mock.patch.object(
                        agent.os,
                        "fstat",
                        side_effect=lambda fd: SimpleNamespace(st_dev=by_descriptor[fd]),
                    )
                )
                patches.enter_context(
                    mock.patch.object(
                        agent.os, "fstatvfs", side_effect=filesystem_stat, create=True
                    )
                )
                patches.enter_context(
                    mock.patch.object(agent.os, "major", return_value=8, create=True)
                )
                patches.enter_context(
                    mock.patch.object(
                        agent.os, "minor", side_effect=lambda value: value, create=True
                    )
                )
                patches.enter_context(
                    mock.patch.object(agent.os, "O_DIRECTORY", 0, create=True)
                )
                patches.enter_context(
                    mock.patch.object(agent.os, "O_NOFOLLOW", 0, create=True)
                )
                return agent.read_filesystems()

    def test_canonical_filesystems_do_not_count_docker_or_bind_aliases(self):
        rows = self.filesystem_metrics()
        self.assertEqual([row["id"] for row in rows], ["system", "data"])
        self.assertEqual([row["total_bytes"] for row in rows], [1000, 2000])
        self.assertEqual([row["percent"] for row in rows], [30, 50])
        self.assertEqual(rows[1]["free_bytes"], 1000)
        self.assertEqual(rows[1]["available_bytes"], 900)
        self.assertTrue(
            all(row["available"] and row["status"] == "mounted" for row in rows)
        )

    def test_missing_data_mount_never_reports_the_underlying_root_directory(self):
        rows = self.filesystem_metrics(mount_data=False)
        self.assertEqual(rows[1]["status"], "not_mounted")
        self.assertFalse(rows[1]["available"])
        self.assertIsNone(rows[1]["total_bytes"])
        self.assertIsNone(rows[1]["percent"])

    def test_same_device_is_deduplicated_even_when_system_statvfs_fails(self):
        rows = self.filesystem_metrics(duplicate=True, error="/")
        self.assertFalse(rows[0]["available"])
        self.assertEqual(rows[1]["status"], "duplicate")
        self.assertIsNone(rows[1]["total_bytes"])

    def test_data_stat_failure_keeps_the_system_filesystem(self):
        rows = self.filesystem_metrics(error="/srv/buh-data")
        self.assertTrue(rows[0]["available"])
        self.assertFalse(rows[1]["available"])
        self.assertEqual(rows[1]["status"], "unavailable")

    def test_unmount_during_statvfs_cannot_masquerade_as_data_capacity(self):
        rows = self.filesystem_metrics(unmount_during_stat=True)
        self.assertFalse(rows[1]["available"])
        self.assertIsNone(rows[1]["total_bytes"])

    def test_host_metrics_keeps_legacy_disk_fields_root_only(self):
        with (
            mock.patch.object(agent, "read_cpu", return_value=(10, 4)),
            mock.patch.object(agent, "read_memory", return_value={}),
            mock.patch.object(agent, "read_network", return_value=(0, 0)),
            mock.patch.object(agent, "read_filesystems", return_value=[{"id": "data"}]),
            mock.patch.object(agent.os, "getloadavg", return_value=(0, 0, 0), create=True),
            mock.patch.object(agent.Path, "read_text", return_value="100.0 0.0"),
            mock.patch.object(
                agent.shutil,
                "disk_usage",
                return_value=SimpleNamespace(total=1000, used=300),
            ) as usage,
        ):
            metrics = agent.host_metrics()
        usage.assert_called_once_with("/")
        self.assertEqual(
            (
                metrics["disk_total_bytes"],
                metrics["disk_used_bytes"],
                metrics["disk_percent"],
            ),
            (1000, 300, 30),
        )
        self.assertEqual(metrics["filesystems"], [{"id": "data"}])

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
