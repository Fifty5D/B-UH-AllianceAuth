"""Conservative CI scope: only a proven prose-only diff can skip runtime lanes."""

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess


def prose_path(path: str) -> bool:
    return path in {"README.md", "AGENTS.md", "CONTRIBUTING.md"} or (
        path.startswith("docs/") and PurePosixPath(path).suffix == ".md"
    )


def docs_only(root: Path, base: str, head: str) -> bool:
    if not all(re.fullmatch(r"[0-9a-f]{40}", value or "") for value in (base, head)):
        return False
    if base == "0" * 40 or base == head:
        return False
    try:
        # Disable rename detection: both ends of a rename must be prose paths.
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", base, head, "--"],
            cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
        )
        paths = result.stdout.decode("utf-8").split("\0")[:-1]
        return bool(paths) and all(prose_path(path) for path in paths)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base", default="")
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    print("docs_only=" + str(docs_only(args.root, args.base, args.head)).lower())


if __name__ == "__main__":
    main()
