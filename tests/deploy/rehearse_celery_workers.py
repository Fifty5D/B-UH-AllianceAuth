#!/usr/bin/env python3
"""Exercise the real DockerHost checker against disposable Celery/Redis workers.

No fake inspect or reply data: Docker metadata and the worker's pinned Celery
library independently supply the identities, then real broker inspection must
match. Only the test application name and disposable Compose project differ.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.deploy.contracts import DeploymentError, ReceiverConfig  # noqa: E402
from ops.deploy.docker_host import DockerHost  # noqa: E402


class TestDockerHost(DockerHost):
    @property
    def compose_prefix(self):
        project = os.environ.get("BUH_COMPOSE_PROJECT", "")
        if re.fullmatch(r"buh-[a-z0-9-]{8,100}", project) is None:
            raise DeploymentError("Disposable test project identity is required")
        return [
            "docker",
            "compose",
            "--project-name",
            project,
            "-f",
            str(ROOT / "platform/testenv/compose.yml"),
        ]

    def _compose(self, *arguments, **kwargs):
        return super()._compose(
            *("--app=testauth" if item == "--app=myauth" else item for item in arguments),
            **kwargs,
        )


def main():
    config = replace(
        ReceiverConfig.load(ROOT / "ops/deploy/receiver-config.example.json"),
        app_dir=ROOT,
        worker_service="worker",
        gunicorn_service="web",
        beat_service="beat",
        auth_services=("web", "worker", "worker_services", "beat"),
        required_celery_queues=("celery", "services"),
        required_celery_tasks=("testauth.test_support.tasks.celery_roundtrip",),
    )
    host = TestDockerHost(config)
    host.auth_replica_counts = {"web": 1, "beat": 1, "worker": 3, "worker_services": 1}

    def verify_real_workers():
        library_nodes = set()
        for service in ("worker", "worker_services"):
            for container in host._running_service_containers(
                service, context="Synthetic replica discovery"
            ):
                # Independently use the running container's real library + hostname.
                pattern = "worker_%n" if service == "worker" else "worker_services_%n"
                report = json.loads(
                    host._run(
                        [
                            "docker",
                            "exec",
                            container,
                            "python3",
                            "-c",
                            "import json,sys,celery; from celery.bin.worker import Hostname; "
                            "print(json.dumps({'version':celery.__version__,"
                            "'node':Hostname().convert(sys.argv[1],None,None)}))",
                            pattern,
                        ],
                        context="Pinned Celery CLI identity",
                    )
                )
                if report["version"] != "5.6.3":
                    raise AssertionError("Connected worker did not run pinned Celery 5.6.3")
                library_nodes.add(report["node"])
        expected = host._expected_celery_nodes()
        if len(library_nodes) != 4 or expected != library_nodes:
            raise AssertionError(
                "DockerHost identities disagree with actual Celery workers"
            )
        host._celery_health()
        return expected

    before = verify_real_workers()
    # This is the same isolated CI Compose project that created these workers.
    host._compose("stop", "worker_services", context="Synthetic missing worker")
    try:
        host._celery_inspect("ping", expected_nodes=before)
    except DeploymentError as exc:
        if "missing=" not in str(exc):
            raise
    else:
        raise AssertionError("A genuinely missing services worker was accepted")
    host._compose(
        "up",
        "-d",
        "--wait",
        "--no-build",
        "--force-recreate",
        "--scale",
        "worker=3",
        "worker",
        "worker_services",
        context="Synthetic scaled worker replacement",
    )
    after = verify_real_workers()
    if before & after:
        raise AssertionError(
            "The recreated workers did not acquire fresh container identities"
        )
    try:
        host._celery_inspect("ping", expected_nodes=before)
    except DeploymentError as exc:
        if "unexpected=" not in str(exc):
            raise
    else:
        raise AssertionError("Stale pre-replacement worker identities were accepted")
    print(
        json.dumps(
            {
                "result": "passed",
                "celery": "5.6.3",
                "replicas": [3, 1],
                "real_broker": "redis",
                "missing_worker_rejected": True,
                "replacement_identities_verified": True,
            }
        )
    )


if __name__ == "__main__":
    main()
