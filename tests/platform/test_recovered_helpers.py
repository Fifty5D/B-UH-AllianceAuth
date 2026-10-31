"""Pin recovered helper provenance and explicitly reviewed Member Audit changes."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import tomllib
import unittest
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[2]
VERSION_LINE = re.compile(rb'(?m)^__version__ = "[0-9]+\.[0-9]+\.[0-9]+"$')

# The original wheel remains authoritative for every unlisted byte. These are
# the reviewed sticky-section recovery delta, not a blanket app exemption.
# Hashes describe Git/CI's LF source bytes. The release planner owns versions.
MEMBERAUDIT_RECOVERY_SOURCE = {
    "services.py": "a06a0bf55fcff5e85d3bb96619abe003638313f937e154f3aa57cf6ebd8c9611",
    "tests/test_autoreg.py": "22b074abe17a9facbf0396aa397d78165c8a31d18caf581bc0dc0c21f6b4d242",
    "recovery.py": "8ace78b15048036e7537fddc684e224beed08decdb5031756ec22b1e63c46f63",
    "tasks.py": "571c11dd3fb56f508cd41e8ef4397721afc5018c70f5c0980165db535ca68ddf",
    "management/commands/buh_memberaudit_recover.py": (
        "c787c1b5a284fb82d86384b42fdce8a74760c0740833f085796e3a31bcf8d440"
    ),
}


class RecoveredHelperSourceTests(unittest.TestCase):
    def test_existing_vps_host_agent_source_is_pinned(self):
        agent = ROOT / "apps/vps-health/src/host-agent/buh-vps-health-agent.py"
        self.assertEqual(
            hashlib.sha256(agent.read_bytes()).hexdigest(),
            "a4b37e4e38dd5b683afec767a9bb1c7973e137b87ceeaad7f6e0ed7378ad4f40",
        )

    def test_recovered_source_matches_installed_wheels(self):
        cases = (
            (
                "memberaudit-autoreg",
                "buh_memberaudit_autoreg",
                "aa_buh_memberaudit_autoreg-0.1.0-py3-none-any.whl",
                "e26b225324996c679dddae29d45abf1afce573494fa0f293e755f2072631f0ce",
            ),
            (
                "vps-health",
                "buh_vps_health",
                "aa_buh_vps_health-0.2.0-py3-none-any.whl",
                "b2f10c7b25d7134e83c32abb75fcf85198a4de829a59d4da81d199bebfce67dc",
            ),
        )
        for app, package, filename, digest in cases:
            with self.subTest(app=app):
                wheel = ROOT / "tests/deploy/fixtures" / filename
                self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), digest)
                source = ROOT / "apps" / app / "src" / package
                with zipfile.ZipFile(wheel) as archive:
                    members = {
                        name.removeprefix(f"{package}/"): archive.read(name)
                        for name in archive.namelist()
                        if name.startswith(f"{package}/")
                    }
                actual = {
                    str(path.relative_to(source)).replace("\\", "/"): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                }
                # Release synchronization advances the builder-owned version in
                # __init__.py. Keep the original recovered wheel as a byte pin
                # for every other source line.
                for files in (actual, members):
                    self.assertEqual(len(VERSION_LINE.findall(files["__init__.py"])), 1)
                    files["__init__.py"] = VERSION_LINE.sub(
                        b'__version__ = "<release version>"',
                        files["__init__.py"],
                    )
                reviewed = MEMBERAUDIT_RECOVERY_SOURCE if app == "memberaudit-autoreg" else {}
                additions = set(reviewed) - set(members)
                self.assertEqual(set(actual), set(members) | additions)
                for name, content in actual.items():
                    with self.subTest(app=app, source=name):
                        if name in reviewed:
                            self.assertEqual(hashlib.sha256(content).hexdigest(), reviewed[name])
                            if name in members:
                                self.assertNotEqual(content, members[name])
                        else:
                            self.assertEqual(content, members[name])

                with (ROOT / "apps" / app / "pyproject.toml").open("rb") as stream:
                    dependencies = tomllib.load(stream)["project"]["dependencies"]
                self.assertIn("allianceauth>=5.4,<5.5", dependencies)


class MemberAuditRecoveryBehaviorTests(unittest.TestCase):
    """Exercise the reviewed module without a database, tokens, or EVE calls."""

    def setUp(self):
        self.statuses = MagicMock()
        self.statuses.filter.return_value.values_list.return_value = ["skills", "roles"]
        self.character = SimpleNamespace(
            is_disabled=False,
            eve_character_id=7,
            eve_character=SimpleNamespace(character_id=90000001),
            update_status_set=self.statuses,
            refresh_from_db=MagicMock(),
        )
        self.characters = MagicMock()
        self.characters.filter.return_value.first.return_value = self.character
        self.ownerships = MagicMock()
        self.ownerships.filter.return_value.first.return_value = SimpleNamespace(
            pk=17, user_id=27, owner_hash="synthetic-owner"
        )
        self.ownerships.filter.return_value.exists.return_value = True
        self.tokens = MagicMock()
        scoped = self.tokens.filter.return_value.require_scopes.return_value
        scoped.exists.return_value = True
        scoped.values_list.return_value = [37]
        scoped.require_valid.return_value.exists.return_value = True
        self.scoped = scoped
        self.task = MagicMock()
        self.task.apply_async.return_value = SimpleNamespace(id="synthetic-task")
        self.tasks = SimpleNamespace(
            update_character_skills=self.task,
            esi_status=SimpleNamespace(unavailable_sections=lambda: set()),
        )

        def module(name, **values):
            result = ModuleType(name)
            result.__dict__.update(values)
            return result

        dependencies = {
            "allianceauth": module("allianceauth"),
            "allianceauth.authentication": module("allianceauth.authentication"),
            "allianceauth.authentication.models": module(
                "allianceauth.authentication.models",
                CharacterOwnership=SimpleNamespace(objects=self.ownerships),
            ),
            "esi": module("esi"),
            "esi.models": module("esi.models", Token=SimpleNamespace(objects=self.tokens)),
            "memberaudit": module("memberaudit", tasks=self.tasks),
            "memberaudit.app_settings": module(
                "memberaudit.app_settings", MEMBERAUDIT_TASKS_NORMAL_PRIORITY=6
            ),
            "memberaudit.models": module(
                "memberaudit.models",
                Character=SimpleNamespace(
                    objects=self.characters,
                    UpdateSection=SimpleNamespace(enabled_sections=lambda: {"skills"}),
                    esi_scopes=lambda: ["synthetic-skill-scope"],
                ),
            ),
        }
        patched = patch.dict(sys.modules, dependencies)
        patched.start()
        self.addCleanup(patched.stop)
        source = ROOT / "apps/memberaudit-autoreg/src/buh_memberaudit_autoreg/recovery.py"
        spec = importlib.util.spec_from_file_location("reviewed_memberaudit_recovery", source)
        self.recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.recovery)

    def test_preview_does_not_validate_or_dispatch(self):
        result = self.recovery.recover_character(7)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["sections"], ["skills"])
        self.scoped.require_valid.assert_not_called()
        self.task.apply_async.assert_not_called()

    def test_supported_task_retries_only_failed_enabled_section_without_clearing_flags(self):
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["scheduled"], ["skills"])
        self.scoped.require_valid.assert_called_once_with()
        self.task.apply_async.assert_called_once_with(
            kwargs={"character_pk": 7, "force_update": False}, priority=6
        )
        self.tokens.filter.assert_called_once_with(
            character_id=90000001, user_id=27, character_owner_hash="synthetic-owner"
        )
        self.statuses.update.assert_not_called()
        self.statuses.update_or_create.assert_not_called()

    def test_disabled_or_orphan_characters_keep_history_and_do_not_validate(self):
        self.character.is_disabled = True
        self.assertEqual(self.recovery.recover_character(7, apply=True)["status"], "disabled")
        self.character.is_disabled = False
        self.ownerships.filter.return_value.first.return_value = None
        self.assertEqual(self.recovery.recover_character(7, apply=True)["status"], "orphan")
        self.tokens.filter.assert_not_called()
        self.task.apply_async.assert_not_called()

    def test_invalid_authorization_keeps_failed_section_unscheduled(self):
        self.scoped.require_valid.return_value.exists.return_value = False
        result = self.recovery.recover_character(7, apply=True)
        self.assertEqual(result["status"], "authorization_required")
        self.task.apply_async.assert_not_called()
        self.statuses.update.assert_not_called()
        self.statuses.update_or_create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
