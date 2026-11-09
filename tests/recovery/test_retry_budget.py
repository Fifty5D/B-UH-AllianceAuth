"""Retry-budget behavior using only safe synthetic identifiers."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[2] / "ops" / "recovery" / "retry_budget.py"
spec = importlib.util.spec_from_file_location("retry_budget_under_test", path)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)


def plan(character_pk=7, sections=("skills",), auth=(13,)):
    return {
        "character_pk": character_pk,
        "sections": list(sections),
        "authorization_ids": list(auth),
        "status": "planned",
    }


def entry(attempts=1, last_attempt=100, auth=(13,)):
    return {"attempts": attempts, "last_attempt": last_attempt, "authorization_ids": list(auth)}


class RetryBudgetTests(unittest.TestCase):
    def test_first_attempt_is_reserved_before_dispatch(self):
        selected, state = budget.choose_retries([plan()], {}, now=100)
        self.assertEqual(selected, [plan()])
        self.assertEqual(state["7:skills"], entry(last_attempt=100))

    def test_cooldown_blocks_repeated_dispatch_until_boundary(self):
        prior = {"7:skills": entry(last_attempt=100)}
        selected, state = budget.choose_retries([plan()], prior, now=3699)
        self.assertEqual(selected, [])
        self.assertEqual(state, prior)
        selected, state = budget.choose_retries([plan()], state, now=3700)
        self.assertEqual(len(selected), 1)
        self.assertEqual(state["7:skills"]["attempts"], 2)

    def test_attempt_cap_survives_elapsed_cooldown(self):
        prior = {"7:skills": entry(attempts=3)}
        selected, state = budget.choose_retries([plan()], prior, now=100000)
        self.assertEqual(selected, [])
        self.assertEqual(state, prior)

    def test_new_authorization_id_set_resets_exhausted_budget(self):
        prior = {"7:skills": entry(attempts=3, last_attempt=1000)}
        selected, state = budget.choose_retries([plan(auth=(14,))], prior, now=1001)
        self.assertEqual(len(selected), 1)
        self.assertEqual(state["7:skills"], entry(last_attempt=1001, auth=(14,)))

    def test_same_authorization_rows_in_different_order_do_not_reset(self):
        prior = {"7:skills": entry(attempts=3, auth=(13, 14))}
        selected, state = budget.choose_retries([plan(auth=(14, 13, 14))], prior, now=100000)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["attempts"], 3)

    def test_token_removal_and_reappearance_do_not_reset_exhausted_budget(self):
        prior = {"7:skills": entry(attempts=3, auth=(13, 14))}
        selected, state = budget.choose_retries([plan(auth=(13,))], prior, now=10000)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"], prior["7:skills"])
        selected, state = budget.choose_retries([plan(auth=(13, 14))], state, now=20000)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["attempts"], 3)

    def test_absent_sections_preserve_old_budget_records(self):
        prior = {"7:skills": entry(), "8:location": entry()}
        selected, state = budget.choose_retries([], prior, now=100)
        self.assertEqual((selected, state), ([], prior))

    def test_omitted_failure_cannot_reset_exhausted_state_when_it_returns(self):
        prior = {"7:skills": entry(attempts=3)}
        selected, state = budget.choose_retries([], prior, now=10000)
        self.assertEqual(selected, [])
        self.assertEqual(state, prior)
        selected, state = budget.choose_retries([plan()], state, now=20000)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["attempts"], 3)

    def test_unauthorized_missing_ids_preserve_exhausted_state(self):
        prior = {"7:skills": entry(attempts=3)}
        unauthorized = plan(auth=())
        unauthorized["status"] = "authorization_required"
        selected, state = budget.choose_retries([unauthorized], prior, now=10000)
        self.assertEqual(selected, [])
        self.assertEqual(state, prior)
        selected, state = budget.choose_retries([plan()], state, now=20000)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["attempts"], 3)

    def test_fairness_reaches_later_candidates_instead_of_first_characters(self):
        plans = [plan(1), plan(2), plan(3)]
        prior = {"1:skills": entry(attempts=2, last_attempt=10), "2:skills": entry(last_attempt=20)}
        selected, state = budget.choose_retries(plans, prior, now=10000, limit=2)
        self.assertEqual([p["character_pk"] for p in selected], [3, 2])
        self.assertEqual(state["1:skills"]["attempts"], 2)

    def test_fairness_includes_candidates_after_the_first_hundred(self):
        plans = [plan(character_pk) for character_pk in range(1, 151)]
        prior = {
            f"{character_pk}:skills": entry(attempts=3)
            for character_pk in range(1, 150)
        }
        selected, state = budget.choose_retries(plans, prior, now=10000, limit=50)
        self.assertEqual([p["character_pk"] for p in selected], [150])
        self.assertEqual(len(state), 150)

    def test_section_limit_groups_selected_sections_by_character(self):
        selected, state = budget.choose_retries(
            [plan(7, ("skills", "location", "online_status")), plan(8)],
            {}, now=100, limit=2,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sections"], ["location", "online_status"])
        self.assertEqual(sum(len(p["sections"]) for p in selected), 2)
        self.assertEqual(state["7:skills"]["attempts"], 0)

    def test_override_ignores_cap_and_cooldown_but_reserves_attempt(self):
        prior = {"7:skills": entry(attempts=3, last_attempt=100)}
        selected, state = budget.choose_retries([plan()], prior, now=101, override=True)
        self.assertEqual(len(selected), 1)
        self.assertEqual(state["7:skills"], entry(attempts=4, last_attempt=101))

    def test_policy_never_mutates_inputs_and_state_roundtrips_json(self):
        plans = [plan()]
        prior = {"7:skills": entry()}
        originals = deepcopy((plans, prior))
        _, state = budget.choose_retries(plans, prior, now=10000)
        self.assertEqual((plans, prior), originals)
        self.assertEqual(json.loads(json.dumps(state)), state)

    def test_nonplanned_candidates_never_dispatch_even_with_override(self):
        candidate = plan()
        candidate["status"] = "authorization_required"
        selected, state = budget.choose_retries([candidate], {}, now=100, override=True)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["attempts"], 0)

    def test_no_authorization_nonplanned_row_does_not_dispatch(self):
        candidate = plan(auth=())
        candidate["status"] = "authorization_required"
        selected, state = budget.choose_retries([candidate], {}, now=100, override=True)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["authorization_ids"], [])
        self.assertEqual(state["7:skills"]["attempts"], 0)

    def test_zero_limit_reserves_nothing(self):
        selected, state = budget.choose_retries([plan()], {}, now=100, limit=0)
        self.assertEqual(selected, [])
        self.assertEqual(state["7:skills"]["attempts"], 0)


if __name__ == "__main__":
    unittest.main()
