"""Portable recovery contract tests; no Django database or EVE calls.

Run from the package project: ``python -m unittest discover -s tests -v``.
The real recovery module is loaded with deliberately narrow model/task fakes.
"""

from contextlib import nullcontext
from enum import Enum
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


SOURCE = (
    Path(__file__).resolve().parents[2] / "apps" / "memberaudit-autoreg"
    / "src" / "buh_memberaudit_autoreg"
)


class Section(str, Enum):
    SKILLS = "skills"
    LOCATION = "location"
    ONLINE_STATUS = "online_status"
    ROLES = "roles"

    def __str__(self):
        return self.value

    @property
    def method_name(self):
        return "update_" + self.value

    @classmethod
    def enabled_sections(cls):
        return {cls.SKILLS, cls.LOCATION, cls.ONLINE_STATUS}


def attribute(obj, name):
    for part in name.split("__"):
        obj = getattr(obj, part)
    return obj


class Query:
    def __init__(self, rows=(), calls=None):
        self.rows = list(rows)
        self.calls = calls if calls is not None else []

    def filter(self, **kwargs):
        self.calls.append(("filter", kwargs))

        def matches(row):
            for key, value in kwargs.items():
                if key.endswith("__in"):
                    if attribute(row, key[:-4]) not in value:
                        return False
                elif attribute(row, key) != value:
                    return False
            return True

        return Query([row for row in self.rows if matches(row)], self.calls)

    def select_related(self, *args):
        return self

    def prefetch_related(self, *args):
        return self

    def order_by(self, *args):
        return self

    def distinct(self):
        return self

    def all(self):
        return self

    def only(self, *args):
        return self

    def get(self, **kwargs):
        rows = self.filter(**kwargs).rows
        if len(rows) != 1:
            raise LookupError("Expected exactly one synthetic model")
        return rows[0]

    def get_or_create(self, defaults=None, **kwargs):
        row = self.filter(**kwargs).first()
        if row is None:
            raise AssertionError("This recovery test must never create a model")
        return row, False

    def first(self):
        return self.rows[0] if self.rows else None

    def exists(self):
        return bool(self.rows)

    def count(self):
        return len(self.rows)

    def values_list(self, field, flat=False):
        values = [attribute(row, field) for row in self.rows]
        return values if flat else [(value,) for value in values]

    def require_scopes(self, scopes):
        self.calls.append(("require_scopes", set(scopes)))
        return Query([row for row in self.rows if set(scopes) <= row.granted_scopes], self.calls)

    def require_valid(self):
        self.calls.append(("require_valid", None))
        return Query([row for row in self.rows if row.valid], self.calls)

    def __iter__(self):
        return iter(self.rows)


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.required_scopes = {"scope-a", "scope-b"}
        self.user = SimpleNamespace(pk=50, id=50, is_active=True)
        self.eve_character = SimpleNamespace(pk=100, id=100, character_id=900001, character_name="Synthetic Pilot")
        self.ownership = SimpleNamespace(
            pk=11, character=self.eve_character, character_id=100,
            user=self.user, user_id=50, owner_hash="synthetic-owner",
        )
        self.statuses = [
            SimpleNamespace(pk=1, section="skills", has_token_error=True, is_success=False),
            SimpleNamespace(pk=2, section="location", has_token_error=False, is_success=True),
            SimpleNamespace(pk=3, section="roles", has_token_error=True, is_success=False),
        ]
        self.character = SimpleNamespace(
            pk=7, id=7, eve_character=self.eve_character, eve_character_id=100,
            character_id=900001, is_disabled=False, is_orphan=False,
            character_ownership=self.ownership, user=self.user,
            update_status_set=Query(self.statuses),
        )
        self.character.save = MagicMock(side_effect=AssertionError("Recovery must not edit character"))
        self.character.refresh_from_db = MagicMock()
        self.token = SimpleNamespace(
            pk=13, user=self.user, user_id=50, character_id=900001,
            character_owner_hash="synthetic-owner", granted_scopes=set(self.required_scopes), valid=True,
        )
        self.token.scopes = Query([SimpleNamespace(name=name) for name in self.required_scopes])
        self.tokens = Query([self.token])
        self.ownerships = Query([self.ownership])
        self.characters = Query([self.character])
        character_type = SimpleNamespace(
            objects=self.characters, UpdateSection=Section,
            esi_scopes=lambda: sorted(self.required_scopes), DoesNotExist=LookupError,
        )
        self.tasks = SimpleNamespace(**{
            "update_character_" + name: SimpleNamespace(apply_async=MagicMock(return_value=SimpleNamespace(id="synthetic-job")))
            for name in ("skills", "location", "online_status", "roles")
        })
        self.esi_status = SimpleNamespace(unavailable_sections=MagicMock(return_value=set()))
        self.tasks.esi_status = self.esi_status
        self.transaction = SimpleNamespace(atomic=nullcontext, on_commit=lambda fn: fn())
        deps = {
            "django": module("django"),
            "django.db": module("django.db", transaction=self.transaction),
            "django.core": module("django.core"),
            "django.core.exceptions": module("django.core.exceptions", ObjectDoesNotExist=LookupError),
            "esi": module("esi"),
            "esi.models": module("esi.models", Token=SimpleNamespace(objects=self.tokens)),
            "allianceauth": module("allianceauth"),
            "allianceauth.authentication": module("allianceauth.authentication"),
            "allianceauth.authentication.models": module("allianceauth.authentication.models", CharacterOwnership=SimpleNamespace(objects=self.ownerships)),
            "memberaudit": module("memberaudit", tasks=self.tasks),
            "memberaudit.models": module(
                "memberaudit.models", Character=character_type,
                ComplianceGroupDesignation=SimpleNamespace(objects=Query()),
            ),
            "memberaudit.app_settings": module("memberaudit.app_settings", MEMBERAUDIT_TASKS_NORMAL_PRIORITY=6),
            "memberaudit.providers": module("memberaudit.providers", esi_status=self.esi_status),
            "memberaudit.providers.esi_status": module("memberaudit.providers.esi_status", unavailable_sections=self.esi_status.unavailable_sections),
        }
        self.modules = patch.dict(sys.modules, deps)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location("recovery_under_test", SOURCE / "recovery.py")
        self.recovery = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.recovery
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(self.recovery)

    def assert_nothing_scheduled(self):
        for name, task in vars(self.tasks).items():
            if not name.startswith("update_character_"):
                continue
            task.apply_async.assert_not_called()

    def test_preview_never_validates_or_queues(self):
        result = self.recovery.recover_character(7)
        self.assertEqual(result["sections"], ["skills"])
        self.assertEqual(result["scheduled"], [])
        self.assertFalse(result["token_checked"])
        self.assertNotIn(("require_valid", None), self.tokens.calls)
        self.assert_nothing_scheduled()

    def test_apply_queues_only_failed_enabled_section_and_preserves_flags(self):
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], ["skills"])
        self.assertTrue(result["token_checked"])
        self.tasks.update_character_skills.apply_async.assert_called_once_with(
            kwargs={"character_pk": 7, "force_update": False}, priority=6,
        )
        self.tasks.update_character_location.apply_async.assert_not_called()
        self.tasks.update_character_roles.apply_async.assert_not_called()
        self.assertTrue(self.statuses[0].has_token_error)
        self.assertTrue(self.statuses[2].has_token_error)
        self.character.save.assert_not_called()

    def test_explicit_section_selection_cannot_include_healthy_rows(self):
        result = self.recovery.recover_character(7, apply=True, sections=["location"])
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()

    def test_disabled_character_is_retained_without_token_validation(self):
        self.character.is_disabled = True
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()
        self.assertNotIn(("require_valid", None), self.tokens.calls)
        self.assertTrue(self.statuses[0].has_token_error)

    def test_orphan_is_not_reenabled_or_scheduled(self):
        self.character.is_orphan = True
        self.character.character_ownership = None
        self.character.user = None
        self.ownerships.rows.clear()
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()
        self.character.save.assert_not_called()

    def test_wrong_user_token_is_not_used(self):
        self.token.user_id = 999
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()

    def test_wrong_owner_hash_token_is_not_used(self):
        self.token.character_owner_hash = "different-owner"
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()

    def test_missing_scopes_require_owner_authorization(self):
        self.token.granted_scopes.remove("scope-b")
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()

    def test_failed_supported_token_validation_preserves_error(self):
        self.token.valid = False
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assertTrue(result["token_checked"])
        self.assertIn(("require_valid", None), self.tokens.calls)
        self.assert_nothing_scheduled()
        self.assertTrue(self.statuses[0].has_token_error)

    def test_healthy_character_never_refreshes_tokens_or_queues(self):
        for row in self.statuses:
            row.has_token_error = False
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], [])
        self.assert_nothing_scheduled()
        self.assertNotIn(("require_valid", None), self.tokens.calls)

    def test_unavailable_esi_status_defers_without_clearing_error(self):
        self.esi_status.unavailable_sections.return_value = None
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "esi_status_unavailable")
        self.assert_nothing_scheduled()
        self.assertTrue(self.statuses[0].has_token_error)

    def test_unavailable_esi_section_defers_without_clearing_error(self):
        self.esi_status.unavailable_sections.return_value = {Section.SKILLS}
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "esi_sections_unavailable")
        self.assert_nothing_scheduled()
        self.assertTrue(self.statuses[0].has_token_error)

    def test_ownership_revoked_during_validation_does_not_schedule(self):
        def revoke_during_validation(query):
            self.ownerships.rows.clear()
            return query

        with patch.object(Query, "require_valid", revoke_during_validation):
            result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "ownership_changed")
        self.assert_nothing_scheduled()
        self.assertTrue(self.statuses[0].has_token_error)

    def test_character_disabled_during_validation_does_not_schedule(self):
        def disable_during_validation(query):
            self.character.is_disabled = True
            return query

        with patch.object(Query, "require_valid", disable_during_validation):
            result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "ownership_changed")
        self.assert_nothing_scheduled()
        self.assertTrue(self.statuses[0].has_token_error)

    def test_duplicate_pending_task_is_reported_without_clearing_error(self):
        self.tasks.update_character_skills.apply_async.return_value = None
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "already_queued")
        self.assertEqual(result["scheduled"], [])
        self.assertTrue(self.statuses[0].has_token_error)

    def test_queue_failure_is_reported_without_clearing_error(self):
        self.tasks.update_character_skills.apply_async.side_effect = OSError("synthetic queue failure")
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "queue_failed")
        self.assertEqual(result["error_type"], "OSError")
        self.assertEqual(result["scheduled"], [])
        self.assertNotIn("synthetic queue failure", str(result))
        self.assertTrue(self.statuses[0].has_token_error)

    def test_token_validation_exception_never_leaks_exception_text(self):
        def fail_validation(query):
            raise OSError("synthetic credential-shaped detail")

        with patch.object(Query, "require_valid", fail_validation):
            result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "validation_failed")
        self.assertEqual(result["error_type"], "OSError")
        self.assertNotIn("synthetic credential-shaped detail", str(result))
        self.assert_nothing_scheduled()

    def load_services(self):
        self.recover_callback = MagicMock()
        package = module("buh_memberaudit_autoreg")
        package.__path__ = [str(SOURCE)]
        recovery_module = module("buh_memberaudit_autoreg.recovery", recover_character=self.recover_callback)
        recovery_task_module = module(
            "buh_memberaudit_autoreg.tasks",
            recover_character_sections=SimpleNamespace(delay=self.recover_callback),
        )
        sys.modules["buh_memberaudit_autoreg"] = package
        sys.modules["buh_memberaudit_autoreg.recovery"] = recovery_module
        sys.modules["buh_memberaudit_autoreg.tasks"] = recovery_task_module
        self.addCleanup(sys.modules.pop, "buh_memberaudit_autoreg", None)
        self.addCleanup(sys.modules.pop, "buh_memberaudit_autoreg.recovery", None)
        self.addCleanup(sys.modules.pop, "buh_memberaudit_autoreg.tasks", None)
        name = "buh_memberaudit_autoreg.services"
        spec = importlib.util.spec_from_file_location(name, SOURCE / "services.py")
        services = importlib.util.module_from_spec(spec)
        sys.modules[name] = services
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(services)
        self.callbacks = []
        self.transaction.on_commit = self.callbacks.append
        return services

    def test_existing_registered_character_recovers_only_after_commit(self):
        services = self.load_services()
        result = services.register_token(self.token)
        self.assertEqual(result.status, services.RegistrationStatus.ALREADY_REGISTERED)
        self.recover_callback.assert_not_called()
        self.assertEqual(len(self.callbacks), 1)
        self.callbacks[0]()
        self.recover_callback.assert_called_once_with(7)
        self.character.save.assert_not_called()

    def test_existing_registration_dry_run_has_no_recovery_callback(self):
        services = self.load_services()
        services.register_token(self.token, dry_run=True)
        self.assertEqual(self.callbacks, [])
        self.recover_callback.assert_not_called()

    def test_existing_registration_queue_opt_out_has_no_recovery_callback(self):
        services = self.load_services()
        services.register_token(self.token, queue_updates=False)
        self.assertEqual(self.callbacks, [])
        self.recover_callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
