#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._-]{40,200}$")


def discord_token_assignments(source: str) -> list[tuple[str, ast.AST]]:
    tree = ast.parse(source)
    matches: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                upper_name = name.upper()
                if "DISCORD" in upper_name and "TOKEN" in upper_name:
                    matches.append((name, node.value))
    return matches


def choose_assignment(source: str) -> tuple[str, ast.Constant]:
    matches = discord_token_assignments(source)
    exact = [item for item in matches if item[0].upper() == "DISCORD_BOT_TOKEN"]
    selected = exact if exact else matches
    if len(selected) != 1:
        names = ", ".join(sorted(name for name, _ in matches)) or "none"
        raise ValueError(f"Expected one Discord token setting; found: {names}")
    name, value = selected[0]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise ValueError(f"{name} is not a quoted string literal; refusing an automatic edit")
    return name, value


def replace_token_source(source: str, token: str) -> tuple[str, str]:
    name, value = choose_assignment(source)
    lines = source.splitlines(keepends=True)
    if value.lineno != value.end_lineno:
        raise ValueError(f"{name} uses a multiline string; refusing an automatic edit")
    line_index = value.lineno - 1
    line = lines[line_index]
    lines[line_index] = line[: value.col_offset] + repr(token) + line[value.end_col_offset :]
    updated = "".join(lines)
    ast.parse(updated)
    return updated, name


def validate_token_shape(token: str) -> None:
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("The supplied value does not look like a Discord bot token")


def validate_token_with_discord(token: str) -> None:
    request = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "B-UH-AllianceAuth-Token-Rotation/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise ValueError(f"Discord rejected the new token with status {response.status}")
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Discord rejected the new token with status {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not validate the new token with Discord: {exc.reason}") from None


def atomic_write(path: Path, content: str) -> None:
    current = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(current.st_mode))
        os.chown(temporary, current.st_uid, current.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("settings_file", type=Path)
    parser.add_argument("token_file", type=Path)
    parser.add_argument("--skip-discord-validation", action="store_true")
    args = parser.parse_args()

    token = args.token_file.read_text(encoding="utf-8").strip()
    validate_token_shape(token)
    if not args.skip_discord_validation:
        validate_token_with_discord(token)

    source = args.settings_file.read_text(encoding="utf-8")
    updated, setting_name = replace_token_source(source, token)
    atomic_write(args.settings_file, updated)
    print(setting_name)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"Token rotation refused: {exc}", file=sys.stderr)
        raise SystemExit(1)
