import json
import socket
from pathlib import Path

from django.conf import settings


class AgentUnavailable(RuntimeError):
    pass


def _socket_path():
    return getattr(
        settings, "BUH_VPS_HEALTH_AGENT_SOCKET", "/run/buh-vps-health/agent.sock"
    )


def _token():
    path = Path(
        getattr(settings, "BUH_VPS_HEALTH_TOKEN_FILE", "/run/buh-vps-health/token")
    )
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentUnavailable(f"Host-agent token is unavailable: {exc}") from exc


def request_agent(operation, *, timeout=3.0, **payload):
    message = {"token": _token(), "operation": operation, **payload}
    raw = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    chunks = []
    total = 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(_socket_path())
            connection.sendall(raw)
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > 4 * 1024 * 1024:
                    raise AgentUnavailable(
                        "Host-agent response exceeded the safety limit."
                    )
                if b"\n" in chunk:
                    break
    except (OSError, TimeoutError) as exc:
        raise AgentUnavailable(f"Host agent is unavailable: {exc}") from exc
    try:
        response = json.loads(b"".join(chunks).splitlines()[0])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AgentUnavailable("Host agent returned an invalid response.") from exc
    if not response.get("ok"):
        raise AgentUnavailable(
            response.get("error") or "Host agent rejected the request."
        )
    return response


def get_metrics():
    return request_agent("metrics", timeout=4.0)["metrics"]


def submit_action(action_id, action, requester, worker_count=None):
    return request_agent(
        "action",
        action_id=str(action_id),
        action=action,
        requester=requester[:150],
        worker_count=worker_count,
    )


def action_status(agent_action_id):
    return request_agent("action_status", action_id=agent_action_id)
