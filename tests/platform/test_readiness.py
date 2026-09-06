import unittest

from ops.readiness import ReadinessError, needs_preview, validate


SHA = "a" * 40


def evidence():
    return {
        "schema_version": 1,
        "pr": 42,
        "head_sha": SHA,
        "observed_head_sha": SHA,
        "labels": [],
        "changed_files": ["apps/example/models.py"],
        "checks": [{"status": "completed", "conclusion": "success", "head_sha": SHA}],
        "preview": None,
        "review_findings": [],
    }


class ReadinessTests(unittest.TestCase):
    def test_ui_paths_and_override_require_preview(self):
        self.assertTrue(needs_preview(["apps/x/templates/x/index.html"], []))
        self.assertTrue(needs_preview(["docs/change.md"], ["ui-preview"]))
        self.assertFalse(needs_preview(["apps/x/models.py"], []))

    def test_non_ui_exact_head_can_be_ready(self):
        self.assertTrue(validate(evidence())["ready"])

    def test_stale_head_and_non_success_check_fail_closed(self):
        value = evidence()
        value["observed_head_sha"] = "b" * 40
        with self.assertRaisesRegex(ReadinessError, "stale"):
            validate(value)
        value = evidence()
        value["checks"][0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ReadinessError, "failed"):
            validate(value)

    def test_ui_preview_is_bound_to_pr_head_and_manifest(self):
        value = evidence()
        value["changed_files"] = ["apps/x/static/x/app.js"]
        for preview in (None, {"conclusion": "success", "head_sha": SHA,
                              "manifest_head_sha": "b" * 40, "pr": 42}):
            value["preview"] = preview
            with self.assertRaises(ReadinessError):
                validate(value)
        value["preview"] = {"conclusion": "success", "head_sha": SHA,
                            "manifest_head_sha": SHA, "pr": 42}
        self.assertTrue(validate(value)["preview_required"])

    def test_unresolved_serious_review_finding_fails(self):
        value = evidence()
        value["review_findings"] = [{"severity": "high", "resolved": False}]
        with self.assertRaisesRegex(ReadinessError, "serious"):
            validate(value)
