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


class InsideDispatchTests(unittest.TestCase):
    def dispatch(self, change_hash=False):
        owner, character, ownership = MagicMock(), MagicMock(), MagicMock()
        owner.pk = 41
        owner.is_active = True
        owner.corporation.corporation_id = 99
        character.is_enabled = True
        character.disabled_reason = ""
        character.character_ownership_id = ownership.pk = 123
        ownership.user_id = 451
        ownership.owner_hash = "synthetic-owner"
        ownership.character_id = 345
        ownership.character.character_id = 9900041
        ownership.character.corporation_id = 99
        ownership.user.has_perm.return_value = True
        pending = {
            "character_pk": 1,
            "authorization_fingerprint": recovery.authorization_fingerprint(ownership),
        }
        if change_hash:
            ownership.owner_hash = "replacement-owner"
        owners, characters, tokens = MagicMock(), MagicMock(), MagicMock()
        owners.objects.select_for_update.return_value.select_related.return_value.get.return_value = owner
        characters.objects.select_for_update.return_value.get.return_value = character
        tokens.objects.filter.return_value.require_scopes.return_value.exists.return_value = True
        structures = types.ModuleType("structures")
        structures.tasks = MagicMock()
        publish = structures.tasks.update_structures_for_owner.apply_async
        publish.return_value.id = "synthetic-task"
        database = types.ModuleType("django.db")
        database.transaction = types.SimpleNamespace(atomic=nullcontext)
        authentication = types.ModuleType("allianceauth.authentication.models")
        authentication.CharacterOwnership = MagicMock()
        authentication.CharacterOwnership.objects.select_for_update.return_value.select_related.return_value.get.return_value = ownership
        with patch.dict(
            "sys.modules",
            {
                "structures": structures,
                "django.db": database,
                "allianceauth.authentication.models": authentication,
            },
        ):
            result = recovery.dispatch_inside(
                {
                    "owner_id": 41,
                    "pending": pending,
                    "task": "structures",
                    "task_id": "synthetic-task",
                },
                owners,
                characters,
                tokens,
            )
        return result, publish

    def test_publish_disables_celery_internal_retries_after_ambiguous_failure(self):
        result, publish = self.dispatch()
        self.assertEqual(result["result"], "accepted")
        publish.assert_called_once_with(args=[41], task_id="synthetic-task", retry=False)

    def test_pending_dispatch_does_not_publish_for_replaced_ownership(self):
        result, publish = self.dispatch(change_hash=True)
        self.assertEqual(result["result"], "not_submitted")
        publish.assert_not_called()


class ReconciliationAndHostTests(unittest.TestCase):
    def row(self, enabled=False):
        return {
            "owner_id": 41,
            "character_pk": 1,
            "authorization_fingerprint": "synthetic-identity",
            "matching_token_ids": [101],
            "owner_active": True,
            "membership_ok": True,
            "permission_ok": True,
            "scope_complete_tokens": 1,
            "enabled": enabled,
            "disabled_reason": "" if enabled else recovery.TOKEN_DISABLED_REASON,
        }

    def args(self, directory, **values):
        return types.SimpleNamespace(
            **{
                "state_dir": Path(directory),
                "container": "auth",
                "cooldown_hours": 24,
                "max_owner_attempts": 2,
                "action": "recover",
                "owner_ids": [41],
                "schedule": True,
                "alliance_main_owner_id": None,
                **values,
            }
        )

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def evidence(self, record, task, decision="not_submitted"):
        target = record if task == "enablement" else record["tasks"][task]
        source = MagicMock()
        source.lstat.return_value = types.SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600, st_uid=0
        )
        source.read_text.return_value = json.dumps(
            {
                "owner_id": 41,
                "recovery_id": record["recovery_id"],
                "task_id": record["recovery_id"]
                if task == "enablement"
                else target["task_id"],
                "publication_id": record["enablement_attempt_id"]
                if task == "enablement"
                else target["publication_id"],
                "decision": decision,
                "evidence": "Synthetic reviewed broker evidence",
            }
        )
        return source

    def test_reconciliation_cannot_reuse_old_review_for_later_publication(self):
        now = dt.datetime.now(dt.timezone.utc)
        record = recovery.prepare_dispatch(self.row())
        target = record["tasks"]["structures"]
        target.update(state="uncertain", publication_id="first-attempt", attempts=1)
        pending = {"41": record}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(recovery, "private_write", side_effect=self.write),
        ):
            args = self.args(
                directory,
                dispatch_task="structures",
                dispatch_evidence_file=self.evidence(record, "structures"),
            )
            recovery.reconcile_dispatch(args, [41], pending, now)
            self.assertEqual(target["state"], "prepared")
            target.update(state="uncertain", publication_id="later-attempt", attempts=1)
            before = json.dumps(pending, sort_keys=True)
            with self.assertRaises(ValueError):
                recovery.reconcile_dispatch(args, [41], pending, now)
            self.assertEqual(json.dumps(pending, sort_keys=True), before)

    def test_reconciliation_rejects_stale_owner_task_and_recovery_evidence(self):
        record = recovery.prepare_dispatch(self.row())
        record["tasks"]["structures"].update(state="uncertain", publication_id="attempt")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(recovery, "private_write", side_effect=self.write),
        ):
            for field, invalid in [
                ("owner_id", 42),
                ("task_id", "other-task"),
                ("recovery_id", "other-recovery"),
                ("publication_id", "other-attempt"),
            ]:
                source = self.evidence(record, "structures")
                data = json.loads(source.read_text.return_value)
                data[field] = invalid
                source.read_text.return_value = json.dumps(data)
                args = self.args(
                    directory, dispatch_task="structures", dispatch_evidence_file=source
                )
                with self.subTest(field=field), self.assertRaises(ValueError):
                    recovery.reconcile_dispatch(
                        args, [41], {"41": record}, dt.datetime.now(dt.timezone.utc)
                    )
                self.assertEqual(record["tasks"]["structures"]["state"], "uncertain")

    def test_enablement_reconciliation_requires_current_matching_authorization(self):
        record = recovery.prepare_dispatch(self.row())
        record["enablement"] = "uncertain"
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(recovery, "private_write", side_effect=self.write),
        ):
            args = self.args(
                directory,
                dispatch_task="enablement",
                dispatch_evidence_file=self.evidence(record, "enablement", "accepted"),
            )
            with (
                patch.object(
                    recovery,
                    "container_call",
                    return_value={
                        "characters": [
                            {
                                **self.row(True),
                                "authorization_fingerprint": "other-identity",
                            }
                        ]
                    },
                ),
                self.assertRaises(ValueError),
            ):
                recovery.reconcile_dispatch(
                    args, [41], {"41": record}, dt.datetime.now(dt.timezone.utc)
                )
            self.assertEqual(record["enablement"], "uncertain")

    def test_pending_intent_does_not_bypass_exhausted_authorization_budget(self):
        row = self.row()
        record = recovery.prepare_dispatch(row)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(recovery, "private_write", side_effect=self.write),
            patch("builtins.print"),
        ):
            args = self.args(directory)
            self.write(args.state_dir / "pending-dispatch.json", {"41": record})
            self.write(
                args.state_dir / "retry-budget.json",
                {
                    "41": {
                        "authorization_fingerprint": row["authorization_fingerprint"],
                        "matching_token_ids": [101],
                        "attempts": 2,
                    }
                },
            )
            with patch.object(
                recovery, "container_call", return_value={"characters": [row]}
            ) as call:
                self.assertEqual(recovery.run_host(args, MagicMock()), 1)
            self.assertEqual([entry.args[1] for entry in call.call_args_list], ["inspect"])
            self.assertEqual(
                json.loads((args.state_dir / "retry-budget.json").read_text())["41"][
                    "attempts"
                ],
                2,
            )

    def test_unknown_enablement_result_survives_restart_without_task_publication(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(recovery, "private_write", side_effect=self.write),
            patch("builtins.print"),
        ):
            args = self.args(directory)

            def fail_after_commit(_container, action, _payload):
                if action == "inspect":
                    return {"characters": [self.row()]}
                if action == "snapshot":
                    return {"synthetic": True}
                raise RuntimeError("Synthetic lost enablement acknowledgement")

            with (
                patch.object(recovery, "container_call", side_effect=fail_after_commit),
                self.assertRaises(RuntimeError),
            ):
                recovery.run_host(args, MagicMock())
            pending = json.loads((args.state_dir / "pending-dispatch.json").read_text())
            self.assertEqual(pending["41"]["enablement"], "prepared")
            with patch.object(
                recovery, "container_call", return_value={"characters": [self.row(True)]}
            ) as call:
                self.assertEqual(recovery.run_host(args, MagicMock()), 1)
            self.assertEqual([entry.args[1] for entry in call.call_args_list], ["inspect"])
            self.assertEqual(
                json.loads((args.state_dir / "pending-dispatch.json").read_text())["41"][
                    "enablement"
                ],
                "uncertain",
            )


class BudgetAndStateLockTests(unittest.TestCase):
    def record(self, **changes):
        return {
            "attempts": 2,
            "last_attempt": "2026-10-31T23:00:00+00:00",
            "authorization_fingerprint": "synthetic-owner-identity",
            "matching_token_ids": [101, 102],
            **changes,
        }

    def grant(self, **changes):
        return {
            "authorization_fingerprint": "synthetic-owner-identity",
            "matching_token_ids": [101, 102],
            **changes,
        }

    def test_genuinely_new_matching_token_resets_exhausted_budget(self):
        original = self.record()
        updated = recovery.authorization_budget(
            original, self.grant(matching_token_ids=[101, 102, 103])
        )
        self.assertEqual(updated["attempts"], 0)
        self.assertFalse(updated.get("last_attempt"))
        self.assertEqual(original, self.record(), "input budget must not be mutated")

    def test_refreshing_same_token_does_not_reset_budget(self):
        original = self.record()
        updated = recovery.authorization_budget(
            original,
            self.grant(
                cached_unexpired_tokens=2,
                token_created="2026-11-01T00:00:00+00:00",
            ),
        )
        self.assertEqual(updated["attempts"], original["attempts"])
        self.assertEqual(updated["last_attempt"], original["last_attempt"])

    def test_token_removal_and_reappearance_do_not_reset_budget(self):
        original = self.record()
        reduced = recovery.authorization_budget(
            original, self.grant(matching_token_ids=[101])
        )
        restored = recovery.authorization_budget(reduced, self.grant())
        self.assertEqual(restored["attempts"], original["attempts"])
        self.assertEqual(restored["last_attempt"], original["last_attempt"])

    def test_identity_change_or_ineligibility_preserves_entire_budget(self):
        original = self.record()
        for changes in [
            {"authorization_fingerprint": "different-owner-identity"},
            {"membership_ok": False},
            {"permission_ok": False},
            {"owner_active": False},
            {"matching_token_ids": []},
        ]:
            with self.subTest(changes=changes):
                row = self.grant(matching_token_ids=[101, 102, 103])
                row.update(changes)
                self.assertEqual(recovery.authorization_budget(original, row), original)

    def test_legacy_budget_adopts_identity_without_erasing_attempts(self):
        original = {"attempts": 2, "last_attempt": "2026-10-31T23:00:00+00:00"}
        updated = recovery.authorization_budget(original, self.grant())
        self.assertEqual(updated["attempts"], original["attempts"])
        self.assertEqual(updated["last_attempt"], original["last_attempt"])
        self.assertEqual(updated["authorization_fingerprint"], "synthetic-owner-identity")

    @unittest.skipUnless(__import__("os").name == "posix", "POSIX flock required")
    def test_state_directory_and_lock_file_symlinks_are_rejected(self):
        import os

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "actual"
            target.mkdir(mode=0o700)
            alias = directory / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaises((ValueError, OSError, RuntimeError)):
                with recovery.state_lock(alias):
                    self.fail("symlink state directory acquired the recovery lock")
            target_file = directory / "actual-lock"
            target_file.write_text("untouched")
            os.symlink(target_file, target / ".recovery.lock")
            with self.assertRaises((ValueError, OSError, RuntimeError)):
                with recovery.state_lock(target):
                    self.fail("symlink lock file acquired the recovery lock")
            self.assertEqual(target_file.read_text(), "untouched")

    @unittest.skipUnless(__import__("os").name == "posix", "POSIX flock required")
    def test_concurrent_host_runs_never_overlap_inside_state_lock(self):
        import subprocess
        import sys

        source = """
import importlib.util, os, pathlib, sys, time
spec = importlib.util.spec_from_file_location('recovery', sys.argv[1])
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
state, events = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
for iteration in range(3):
    with recovery.state_lock(state):
        with events.open('a') as stream:
            stream.write('enter\\n')
            stream.flush()
        time.sleep(0.025)
        with events.open('a') as stream:
            stream.write('exit\\n')
            stream.flush()
"""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            events = directory / "events"
            workers = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        source,
                        str(Path(recovery.__file__).resolve()),
                        str(directory / "state"),
                        str(events),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(3)
            ]
            try:
                for worker in workers:
                    stdout, stderr = worker.communicate(timeout=10)
                    self.assertEqual(worker.returncode, 0, stderr or stdout)
            finally:
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                        worker.communicate()
            active = 0
            maximum = 0
            for event in events.read_text().splitlines():
                active += 1 if event == "enter" else -1
                maximum = max(maximum, active)
                self.assertGreaterEqual(active, 0)
            self.assertEqual(active, 0)
            self.assertEqual(maximum, 1)
            self.assertEqual(len(events.read_text().splitlines()), 18)


class DispatchReliabilityTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 31, 23, 0, tzinfo=dt.timezone.utc)

    def character(self):
        return {
            "owner_id": 41,
            "character_pk": 41,
            "authorization_fingerprint": "synthetic-identity",
            "matching_token_ids": [101],
            "owner_active": True,
            "membership_ok": True,
            "permission_ok": True,
            "scope_complete_tokens": 1,
            "enabled": True,
            "disabled_reason": "",
        }

    def arguments(self, directory):
        return types.SimpleNamespace(
            state_dir=Path(directory),
            container="auth",
            cooldown_hours=24,
            max_owner_attempts=2,
        )

    def pending(self, enablement="confirmed"):
        record = recovery.prepare_dispatch(self.character())
        record["enablement"] = enablement
        return {"41": record}

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def accepted(self, _container, action, payload):
        self.assertEqual(action, "dispatch")
        return {
            "result": "accepted",
            "task_id": payload["task_id"],
            "publication_id": payload["pending"]["tasks"][payload["task"]][
                "publication_id"
            ],
        }

    def partial_failure(self, failed_task):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(temporary)
            pending = self.pending()

            def submit(container, action, payload):
                if payload["task"] == failed_task:
                    raise ConnectionError("synthetic acknowledgement loss")
                return self.accepted(container, action, payload)

            with (
                patch.object(recovery, "private_write", side_effect=self.write_json),
                patch.object(recovery, "container_call", side_effect=submit) as call,
            ):
                recovery.dispatch_pending(args, pending, [self.character()], [41], self.now)
            self.assertEqual(call.call_count, 2)
            accepted_task = "notifications" if failed_task == "structures" else "structures"
            persisted = json.loads((args.state_dir / "pending-dispatch.json").read_text())
            self.assertEqual(persisted["41"]["tasks"][failed_task]["state"], "uncertain")
            self.assertEqual(persisted["41"]["tasks"][accepted_task]["state"], "accepted")
            with (
                patch.object(recovery, "private_write", side_effect=self.write_json),
                patch.object(recovery, "container_call") as restart,
            ):
                recovery.dispatch_pending(
                    args,
                    persisted,
                    [self.character()],
                    [41],
                    self.now + dt.timedelta(days=2),
                )
            restart.assert_not_called()

    def test_first_submission_exception_keeps_second_accepted_without_replay(self):
        self.partial_failure("structures")

    def test_second_submission_exception_preserves_first_accepted_without_replay(self):
        self.partial_failure("notifications")

    def test_crash_after_acknowledgement_does_not_republish_uncertain_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(temporary)
            pending = self.pending()

            def interrupted_write(path, value):
                if (
                    path.name == "pending-dispatch.json"
                    and value.get("41", {})
                    .get("tasks", {})
                    .get("structures", {})
                    .get("state")
                    == "accepted"
                ):
                    raise SystemExit("synthetic interruption before durable ack")
                self.write_json(path, value)

            with (
                patch.object(recovery, "private_write", side_effect=interrupted_write),
                patch.object(
                    recovery, "container_call", side_effect=self.accepted
                ) as first,
                self.assertRaises(SystemExit),
            ):
                recovery.dispatch_pending(args, pending, [self.character()], [41], self.now)
            self.assertEqual(first.call_count, 1)
            persisted = json.loads((args.state_dir / "pending-dispatch.json").read_text())
            self.assertEqual(persisted["41"]["tasks"]["structures"]["state"], "publishing")
            with (
                patch.object(recovery, "private_write", side_effect=self.write_json),
                patch.object(
                    recovery, "container_call", side_effect=self.accepted
                ) as restart,
            ):
                recovery.dispatch_pending(
                    args,
                    persisted,
                    [self.character()],
                    [41],
                    self.now + dt.timedelta(days=2),
                )
            self.assertEqual(
                [call.args[2]["task"] for call in restart.call_args_list],
                ["notifications"],
            )
            self.assertEqual(persisted["41"]["tasks"]["structures"]["state"], "uncertain")
            self.assertEqual(persisted["41"]["tasks"]["notifications"]["state"], "accepted")

    def test_prepared_enablement_with_enabled_character_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(temporary)
            pending = self.pending(enablement="prepared")
            with (
                patch.object(recovery, "private_write", side_effect=self.write_json),
                patch.object(recovery, "container_call") as submit,
            ):
                recovery.dispatch_pending(args, pending, [self.character()], [41], self.now)
            submit.assert_not_called()
            persisted = json.loads((args.state_dir / "pending-dispatch.json").read_text())
            self.assertEqual(persisted["41"]["enablement"], "uncertain")
            self.assertTrue(
                all(
                    task["state"] == "prepared"
                    for task in persisted["41"]["tasks"].values()
                )
            )

    def test_proven_not_submitted_retries_are_bounded_and_preserve_other_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(temporary)
            pending = self.pending()

            def submit(container, action, payload):
                if payload["task"] == "structures":
                    return {"result": "not_submitted"}
                return self.accepted(container, action, payload)

            with (
                patch.object(recovery, "private_write", side_effect=self.write_json),
                patch.object(recovery, "container_call", side_effect=submit) as call,
            ):
                for hours in (0, 1, 25, 50):
                    recovery.dispatch_pending(
                        args,
                        pending,
                        [self.character()],
                        [41],
                        self.now + dt.timedelta(hours=hours),
                    )
            self.assertEqual(
                [item.args[2]["task"] for item in call.call_args_list],
                ["structures", "notifications", "structures"],
            )
            self.assertEqual(pending["41"]["tasks"]["notifications"]["state"], "accepted")
            self.assertEqual(pending["41"]["tasks"]["structures"]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
