import datetime as dt
import importlib.util
import json
import stat
from pathlib import Path
import tempfile
import types
import unittest
from contextlib import nullcontext
from unittest.mock import patch, MagicMock

SPEC = importlib.util.spec_from_file_location(
    "structures_recovery",
    Path(__file__).resolve().parents[2] / "ops" / "recovery" / "structures_recovery.py",
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def test_explicit_owner_selection_rejects_empty_and_non_integer_ids(self):
        self.assertEqual(recovery.validate_owner_ids([42, 41, 42]), [41, 42])
        for value in [[], [True], [0], [-1], ["41"], {"owner_ids": [41]}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                recovery.validate_owner_ids(value)

    def test_allowlist_requires_private_root_owned_regular_file(self):
        source = MagicMock()
        source.lstat.return_value = types.SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_uid=0
        )
        source.read_text.return_value = '{"owner_ids": [42, 41]}'
        self.assertEqual(recovery.load_owner_ids(source), [41, 42])
        for mode, user in [
            (stat.S_IFREG | 0o644, 0),
            (stat.S_IFREG | 0o600, 1000),
            (stat.S_IFLNK | 0o600, 0),
        ]:
            source.reset_mock()
            source.lstat.return_value = types.SimpleNamespace(st_mode=mode, st_uid=user)
            with self.subTest(mode=mode, user=user), self.assertRaises(ValueError):
                recovery.load_owner_ids(source)
            source.read_text.assert_not_called()

    def row(self, **overrides):
        return {
            "owner_id": 41,
            "enabled": False,
            "disabled_reason": recovery.TOKEN_DISABLED_REASON,
            "membership_ok": True,
            "permission_ok": True,
            "scope_complete_tokens": 1,
            **overrides,
        }

    def test_only_allowed_token_disabled_authorized_rows_eligible(self):
        self.assertTrue(recovery.eligible_character(self.row(), [41]))
        for changes in [
            {"owner_id": 42},
            {"enabled": True},
            {"disabled_reason": "This character is not a director or CEO"},
            {"disabled_reason": "Manually disabled"},
            {"membership_ok": False},
            {"permission_ok": False},
            {"scope_complete_tokens": 0},
        ]:
            with self.subTest(changes=changes):
                self.assertFalse(recovery.eligible_character(self.row(**changes), [41]))

    def test_recovery_budget_is_bounded_and_cooldown_survives_restart(self):
        now = dt.datetime(2026, 10, 31, 23, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(recovery.within_retry_budget({}, now, 24, 2))
        self.assertFalse(recovery.within_retry_budget({"attempts": 2}, now, 24, 2))
        self.assertFalse(
            recovery.within_retry_budget(
                {"attempts": 1, "last_attempt": (now - dt.timedelta(hours=23)).isoformat()},
                now,
                24,
                2,
            )
        )
        self.assertTrue(
            recovery.within_retry_budget(
                {"attempts": 1, "last_attempt": (now - dt.timedelta(hours=25)).isoformat()},
                now,
                24,
                2,
            )
        )

    def test_transport_never_relays_raw_private_snapshot_on_failure(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": b"SECRET_ACCESS_TOKEN",
                "stderr": b"SECRET_REFRESH_TOKEN",
            },
        )()
        with patch.object(recovery.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                recovery.container_call("auth", "snapshot", {})
        self.assertNotIn("SECRET", str(caught.exception))

    def test_transport_parses_only_marked_result(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": (
                    "startup noise\n" + recovery.RESULT_MARKER + '{"ok":true}\n'
                ).encode(),
                "stderr": b"",
            },
        )()
        with patch.object(recovery.subprocess, "run", return_value=result) as call:
            self.assertEqual(recovery.container_call("auth", "inspect", {}), {"ok": True})
        self.assertEqual(call.call_args.args[0][-2:], ["--inside", "inspect"])
        self.assertIn("timeout", call.call_args.kwargs)

    def apply_fixture(self, token=None, probe_error=None, probe_mutation=None):
        owner, character, tokens = MagicMock(), MagicMock(), MagicMock()
        owner.pk = 41
        owner.is_active = True
        owner.corporation.corporation_id = 99
        ownership = character.character_ownership
        ownership.pk = character.character_ownership_id = 123
        ownership.user_id = 451
        ownership.character_id = 345
        ownership.owner_hash = "fixture-owner-hash"
        ownership.character.character_id = 9900041
        ownership.character.corporation_id = 99
        ownership.user.has_perm.return_value = True
        character.is_enabled = False
        character.disabled_reason = recovery.TOKEN_DISABLED_REASON
        owners, characters = MagicMock(), MagicMock()
        owners.objects.get.return_value = owner
        owners.objects.select_for_update.return_value.select_related.return_value.get.return_value = owner
        characters.objects.select_related.return_value.get.return_value = character
        characters.objects.select_for_update.return_value.get.return_value = character
        if token is not None:
            token = types.SimpleNamespace(
                pk=999,
                user_id=ownership.user_id,
                character_owner_hash=ownership.owner_hash,
                character_id=ownership.character.character_id,
            )
        tokens.objects.filter.return_value.require_scopes.return_value.require_valid.return_value.first.return_value = token
        tokens.objects.select_for_update.return_value.filter.return_value.require_scopes.return_value.first.return_value = token
        structures = types.ModuleType("structures")
        structures.providers = MagicMock()
        structures.app_settings = types.SimpleNamespace(STRUCTURES_FEATURE_STARBASES=True)
        structures.tasks = MagicMock()
        if probe_error:
            structures.providers.esi.client.Corporation.GetCorporationsCorporationIdStarbases.side_effect = probe_error
        if probe_mutation:
            structures.providers.esi.client.Corporation.GetCorporationsCorporationIdStarbases.return_value.results.side_effect = (
                lambda **_: probe_mutation(ownership, tokens)
            )
        database = types.ModuleType("django.db")
        database.transaction = types.SimpleNamespace(atomic=nullcontext)
        authentication = types.ModuleType("allianceauth.authentication.models")
        authentication.CharacterOwnership = MagicMock()
        authentication.CharacterOwnership.objects.select_for_update.return_value.select_related.return_value.get.return_value = ownership
        row = self.row(owner_active=True, character_pk=1)
        with (
            patch.object(
                recovery, "django_context", return_value=(owners, characters, tokens)
            ),
            patch.object(recovery, "inspect_characters", return_value=[row]),
            patch.dict(
                "sys.modules",
                {
                    "structures": structures,
                    "django.db": database,
                    "allianceauth.authentication.models": authentication,
                },
            ),
        ):
            result = recovery.inside("apply", {"owner_ids": [41]})
        return result, owner, ownership, tokens

    def test_revoked_token_stays_disabled_and_requires_reauthorization(self):
        result, owner, _, _ = self.apply_fixture(token=None)
        self.assertEqual(result["outcomes"][0]["result"], "needs_reauthorization")
        owner.add_character.assert_not_called()

    def test_role_denied_never_enables_character_or_forges_health(self):
        class Forbidden(Exception):
            status_code = 403

        result, owner, _, _ = self.apply_fixture(token=object(), probe_error=Forbidden())
        self.assertEqual(result["outcomes"][0]["result"], "role_denied")
        owner.add_character.assert_not_called()
        owner.update_is_up.assert_not_called()

    def test_authorized_recovery_uses_supported_add_character_and_owner_hash(self):
        result, owner, ownership, tokens = self.apply_fixture(token=object())
        self.assertEqual(result["outcomes"][0]["result"], "restored")
        owner.add_character.assert_called_once_with(ownership)
        self.assertEqual(
            tokens.objects.filter.call_args.kwargs["character_owner_hash"],
            "fixture-owner-hash",
        )
        owner.update_is_up.assert_not_called()

    def test_in_place_user_transfer_during_role_probe_stays_disabled(self):
        def transfer(ownership, _tokens):
            ownership.user_id += 1

        result, owner, _, _ = self.apply_fixture(token=object(), probe_mutation=transfer)
        self.assertEqual(result["outcomes"][0]["result"], "authorization_changed")
        owner.add_character.assert_not_called()

    def test_in_place_owner_hash_change_during_role_probe_stays_disabled(self):
        def transfer(ownership, _tokens):
            ownership.owner_hash = "replacement-owner-hash"

        result, owner, _, _ = self.apply_fixture(token=object(), probe_mutation=transfer)
        self.assertEqual(result["outcomes"][0]["result"], "authorization_changed")
        owner.add_character.assert_not_called()

    def test_validated_token_deleted_during_role_probe_stays_disabled(self):
        def delete(_ownership, tokens):
            tokens.objects.select_for_update.return_value.filter.return_value.require_scopes.return_value.first.return_value = None

        result, owner, _, _ = self.apply_fixture(token=object(), probe_mutation=delete)
        self.assertEqual(result["outcomes"][0]["result"], "authorization_changed")
        owner.add_character.assert_not_called()

    @unittest.skipUnless(hasattr(recovery.os, "fchmod"), "POSIX-only private modes")
    def test_private_snapshot_is_atomic_and_mode_600(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "private" / "snapshot.json"
            recovery.private_write(target, {"secret": "fixture-only"})
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(target.read_text()), {"secret": "fixture-only"})


if __name__ == "__main__":
    unittest.main()
