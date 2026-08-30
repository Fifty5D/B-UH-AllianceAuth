#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie|"
            r"client[_-]?secret|refresh[_-]?token|access[_-]?token)\b"
            r"(\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
        r"\1<redacted>:<redacted>@",
    ),
    (
        re.compile(r"https://(?:canary\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I),
        "https://discord.com/api/webhooks/<redacted>",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "<redacted-jwt>",
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: buh-redact-diagnostics INPUT_FILE", file=sys.stderr)
        return 64
    source = Path(sys.argv[1])
    sys.stdout.write(redact(source.read_text(encoding="utf-8", errors="replace")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
