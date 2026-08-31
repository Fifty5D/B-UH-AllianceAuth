#!/usr/bin/env python3
"""Deterministic, fail-closed ESI substitute for disposable CI environments."""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).with_name("fixtures")
REQUESTS: list[dict] = []

ROUTES = (
    (re.compile(r"^/latest/markets/10000002/orders/?$"), "market-orders.json"),
    (
        re.compile(r"^/latest/corporations/\d+/mining/extractions/?$"),
        "mining-extractions.json",
    ),
    (
        re.compile(r"^/latest/corporations/\d+/mining/observers/?$"),
        "mining-observers.json",
    ),
    (
        re.compile(r"^/latest/corporations/\d+/mining/observers/\d+/?$"),
        "mining-ledger.json",
    ),
    (re.compile(r"^/latest/characters/\d+/wallet/journal/?$"), "wallet-journal.json"),
    (re.compile(r"^/latest/characters/\d+/contracts/?$"), "contracts.json"),
    (
        re.compile(r"^/latest/characters/\d+/contracts/\d+/items/?$"),
        "contract-items.json",
    ),
)


class Handler(BaseHTTPRequestHandler):
    server_version = "BUH-Fake-ESI/1"

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        REQUESTS.append({"method": "GET", "path": parsed.path, "query": query})
        if parsed.path == "/health":
            return self._json(200, {"ok": True, "service": "fake-esi"})
        if parsed.path == "/__requests__":
            return self._json(200, REQUESTS)
        if parsed.path == "/meta/openapi.json":
            return self._fixture(
                200,
                "openapi.json",
                {
                    "Cache-Control": "public, max-age=300",
                    "ETag": '"buh-openapi-2025-12-16"',
                },
            )

        scenario = query.get("scenario", [""])[0]
        if scenario in {"forbidden", "rate-limited", "unavailable"}:
            status = {"forbidden": 403, "rate-limited": 420, "unavailable": 503}[scenario]
            return self._fixture(status, f"errors/{scenario}.json")

        for pattern, fixture in ROUTES:
            if pattern.fullmatch(parsed.path):
                headers = {
                    "Cache-Control": "public, max-age=300",
                    "ETag": '"buh-fixture-v1"',
                    "Expires": "Mon, 31 Aug 2026 01:00:00 GMT",
                    "X-Pages": "1",
                    "X-Esi-Error-Limit-Remain": "100",
                }
                return self._fixture(200, fixture, headers)
        return self._json(
            501,
            {
                "error": "unmatched fake ESI route",
                "path": parsed.path,
                "network_proxying": False,
            },
        )

    def log_message(self, format, *args):
        if os.environ.get("BUH_FAKE_ESI_LOG") == "1":
            super().log_message(format, *args)

    def _fixture(self, status, name, headers=None):
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        return self._json(status, payload, headers)

    def _json(self, status, payload, headers=None):
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    host = os.environ.get("BUH_FAKE_ESI_HOST", "0.0.0.0")
    port = int(os.environ.get("BUH_FAKE_ESI_PORT", "8080"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()
