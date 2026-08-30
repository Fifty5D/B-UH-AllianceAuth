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
SENSITIVE_IDENTIFIER = rf"(?:[A-Za-z0-9_.-]*{SENSITIVE_KEY}[A-Za-z0-9_.-]*)"


PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\bBot\s+[A-Za-z0-9._~+/=-]{12,}"), "Bot <redacted>"),
    (
        re.compile(
            rf"(?i)([\"']?{SENSITIVE_IDENTIFIER}[\"']?\s*[:=]\s*)([\"'])(.*?)\2"
        ),
        r"\1\2<redacted>\2",
    ),
    (
        re.compile(
            rf"(?i)([\"']?{SENSITIVE_IDENTIFIER}[\"']?)(\s*[:=]\s*)([^\s,;}}]+)"
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


VALIDATION_PATTERNS = (
    (
        "authorization-header",
        re.compile(r"(?i)\b(?:Bot|Bearer)\s+(?!<redacted>)[A-Za-z0-9._~+/=-]{12,}"),
    ),
    (
        "sensitive-setting",
        re.compile(
            rf"(?i)[\"']?{SENSITIVE_IDENTIFIER}[\"']?\s*[:=]\s*"
            r"(?![\"']?<redacted(?:-[a-z]+)?>)[\"']?[^\s,;}}]+"
        ),
    ),
    (
        "sensitive-query-parameter",
        re.compile(
            r"(?i)[?&](?:access_token|refresh_token|token|key|api_key|secret|"
            r"signature|sig|auth|sentry_key)=(?!<redacted>)[^&\s\"']+"
        ),
    ),
    (
        "discord-webhook",
        re.compile(
            r"https://(?:canary\.)?discord(?:app)?\.com/api/webhooks/(?!<redacted>)\S+",
            re.I,
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
)


def redact(text: str) -> str:
    # Some log formats nest one credential-bearing form inside another, such
    # as an Authorization header inside a Python dictionary. Iterate to a
    # fixed point so a replacement exposed by one pattern is handled by the
    # next pass. The hard limit prevents an accidental infinite loop.
    for _ in range(8):
        updated = text
        for pattern, replacement in PATTERNS:
            updated = pattern.sub(replacement, updated)
        if updated == text:
            return updated
        text = updated
    return text


def credential_shape_counts(text: str) -> dict[str, int]:
    return {
        label: len(pattern.findall(text))
        for label, pattern in VALIDATION_PATTERNS
        if pattern.search(text)
    }


def contains_credential_shape(text: str) -> bool:
    return bool(credential_shape_counts(text))


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
        counts = credential_shape_counts(text)
        if counts:
            summary = ", ".join(f"{label}={count}" for label, count in counts.items())
            print(
                f"Credential-shaped content remains after redaction ({summary}).",
                file=sys.stderr,
            )
            return 65
        return 0
    sys.stdout.write(redact(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
