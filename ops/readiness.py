"""Fail-closed pull-request readiness policy used by GitHub Actions and Work."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


UI_PATH = re.compile(
    r"(^|/)(templates?|static|themes?|browser)(/|$)|"
    r"\.(?:css|scss|sass|less|js|mjs|cjs|ts|tsx|html)$",
    re.IGNORECASE,
)
SERIOUS = {"critical", "high", "serious", "security"}


class ReadinessError(ValueError):
    """Readiness evidence is absent, stale, or unsafe."""


def needs_preview(paths: list[str], labels: list[str]) -> bool:
    return "ui-preview" in labels or any(UI_PATH.search(path) for path in paths)


def validate(evidence: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version", "pr", "head_sha", "observed_head_sha", "labels",
        "changed_files", "checks", "preview", "review_findings",
    }
    if set(evidence) - allowed:
        raise ReadinessError("unknown readiness evidence fields")
    if evidence.get("schema_version") != 1:
        raise ReadinessError("unsupported readiness evidence schema")
    head = evidence.get("head_sha")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReadinessError("invalid pull-request head SHA")
    if evidence.get("observed_head_sha") != head:
        raise ReadinessError("readiness evidence is stale")
    checks = evidence.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ReadinessError("required check evidence is missing")
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "completed":
            raise ReadinessError("a required check is pending or malformed")
        if check.get("conclusion") != "success" or check.get("head_sha") != head:
            raise ReadinessError("a required check failed or belongs to another head")
    labels = evidence.get("labels", [])
    paths = evidence.get("changed_files", [])
    if not all(isinstance(value, str) for value in [*labels, *paths]):
        raise ReadinessError("labels and changed paths must be strings")
    preview_required = needs_preview(paths, labels)
    preview = evidence.get("preview")
    if preview_required:
        if not isinstance(preview, dict):
            raise ReadinessError("UI preview evidence is missing")
        if preview.get("conclusion") != "success" or preview.get("head_sha") != head:
            raise ReadinessError("UI preview failed or belongs to another head")
        if preview.get("manifest_head_sha") != head:
            raise ReadinessError("UI preview manifest is stale")
        if preview.get("pr") != evidence.get("pr"):
            raise ReadinessError("UI preview belongs to another pull request")
    findings = evidence.get("review_findings", [])
    if not isinstance(findings, list):
        raise ReadinessError("review findings are malformed")
    for finding in findings:
        if not isinstance(finding, dict):
            raise ReadinessError("review finding is malformed")
        if finding.get("resolved") is not True and finding.get("severity") in SERIOUS:
            raise ReadinessError("an unresolved serious review finding remains")
    return {
        "schema_version": 1,
        "pr": evidence.get("pr"),
        "head_sha": head,
        "preview_required": preview_required,
        "ready": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(json.loads(args.evidence.read_text(encoding="utf-8")))
    rendered = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="ascii")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
