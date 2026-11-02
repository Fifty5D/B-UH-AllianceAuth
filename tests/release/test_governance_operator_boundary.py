"""Responsibility wording must not broaden the existing GitHub identity gate."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops" / "release"))

import platform_approval as approval  # noqa: E402


class GovernanceOperatorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(owner="Fifty5D")
        self.actor = "existing-authorized-actor"

    def test_only_owner_and_exact_configured_identity_are_allowed(self):
        self.assertTrue(approval._allowed_operator("Fifty5D", self.config, self.actor))
        self.assertTrue(approval._allowed_operator(self.actor, self.config, self.actor))

    def test_product_role_and_reviewer_names_grant_no_authorization(self):
        for login in (
            "Codex", "ChatGPTWork", "operator", "independent-reviewer", "other-user", None
        ):
            with self.subTest(login=login):
                self.assertFalse(approval._allowed_operator(login, self.config, self.actor))

    def test_missing_configured_actor_still_allows_only_owner(self):
        self.assertTrue(approval._allowed_operator("Fifty5D", self.config, ""))
        self.assertFalse(approval._allowed_operator(self.actor, self.config, ""))
        self.assertFalse(approval._allowed_operator(None, self.config, ""))

    def test_identity_comparison_remains_exact(self):
        self.assertFalse(approval._allowed_operator(self.actor.upper(), self.config, self.actor))

    def test_malformed_configured_identity_fails_closed(self):
        for actor in ("ChatGPT Work", "owner/another", "actor@example.invalid"):
            with self.subTest(actor=actor), self.assertRaises(approval.ApprovalError):
                approval._allowed_operator("Fifty5D", self.config, actor)


if __name__ == "__main__":
    unittest.main()
