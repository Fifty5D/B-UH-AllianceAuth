#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


SENSITIVE_KEY = (
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|"
    r"set[_-]?cookie|client[_-]?secret|refresh[_-]?token|access[_-]?token|"
    r"session(?:id)?|csrf(?:token)?)"
)


PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\bBot\s+[A-Za-z0-9._~+/=-]{12,}"), "Bot <redacted>"),
    (
        re.compile(
            rf"(?i)([\"']?{SENSITIVE_KEY}[\"']?\s*[:=]\s*)([\"'])(.*?)\2"
        ),
        r"\1\2<redacted>\2",
    ),
    (
        re.compile(
            rf"(?i)\b({SENSITIVE_KEY})\b(\s*[:=]\s*)([^\s,;}}]+)"
        ),
        r"\1\2<redacted>",
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|token|key|api_key|secret|"
            r"signature|sig|auth|sentry_key)=)[^&\s\"']+"
        ),
        r"\1<redacted>",
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


def contains_credential_shape(text: str) -> bool:
    checks = (
        re.compile(r"(?i)\b(?:Bot|Bearer)\s+(?!<redacted>)[A-Za-z0-9._~+/=-]{12,}"),
        re.compile(
            rf"(?i)[\"']?{SENSITIVE_KEY}[\"']?\s*[:=]\s*"
            r"(?![\"']?<redacted>)[\"']?[^\s,;}}]+"
        ),
        re.compile(
            r"(?i)[?&](?:access_token|refresh_token|token|key|api_key|secret|"
            r"signature|sig|auth|sentry_key)=(?!<redacted>)[^&\s\"']+"
        ),
        re.compile(r"https://(?:canary\.)?discord(?:app)?\.com/api/webhooks/(?!<redacted>)\S+", re.I),
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    )
    return any(pattern.search(text) for pattern in checks)


def main() -> int:
    args = sys.argv[1:]
    validate = False
    if args and args[0] == "--validate":
        validate = True
        args = args[1:]
    if len(args) != 1:
        print("usage: buh-redact-diagnostics [--validate] INPUT_FILE", file=sys.stderr)
        return 64
    source = Path(args[0])
    text = source.read_text(encoding="utf-8", errors="replace")
    if validate:
        if contains_credential_shape(text):
            print("Credential-shaped content remains after redaction.", file=sys.stderr)
            return 65
        return 0
    sys.stdout.write(redact(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
