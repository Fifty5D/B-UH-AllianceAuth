"""Read existing sync evidence and check it against the installed schedules."""

from datetime import datetime, timezone
import json

from .contracts import DeploymentError

QUEUE_GRACE_SECONDS = 900


class SyncEvidenceError(DeploymentError):
    def __init__(self, findings):
        self.findings = findings[:100]
        self.truncated = len(findings) > 100
        super().__init__("Current sync evidence failed; inspect the named fields")


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp needs an offset")
    return parsed.astimezone(timezone.utc)


PROBE_CODE = """
import json
from django.conf import settings
from django.utils.timezone import now
from django_celery_beat.models import PeriodicTask
from structures.models import Owner as S
from moonmining.models import Owner as M, Refinery as R
from buh_moon_tax.models import TaxConfiguration, AuditRun
from memberaudit.models import Character, CharacterUpdateStatus

def schedules(task):
    return list(PeriodicTask.objects.filter(task=task).values(
        'enabled', 'last_run_at', 'interval__every', 'interval__period',
        'crontab_id', 'clocked_id', 'solar_id')[:21])

result = {
    'captured_at': now().isoformat(), 'log_timezone': settings.TIME_ZONE,
    'structures_owners': list(S.objects.order_by('pk').values('pk', 'is_active',
        'is_up', 'is_included_in_service_status', 'structures_last_update_at',
        'assets_last_update_at', 'notifications_last_update_at',
        'forwarding_last_update_at')[:501]),
    'moonmining_owners': list(M.objects.order_by('pk').values('pk', 'is_enabled',
        'last_update_at', 'last_update_ok')[:501]),
    'refineries': list(R.objects.order_by('pk').values('pk', 'owner_id',
        'ledger_last_update_at', 'ledger_last_update_ok')[:501]),
    'moon_tax_config': list(TaxConfiguration.objects.filter(singleton_id=1)
        .values('audit_interval_hours')[:2]),
    'moon_tax_schedule': schedules('buh_moon_tax.tasks.run_scheduled_audit'),
    'completed_audits': list(AuditRun.objects.filter(status='COMPLETE')
        .order_by('-queued_at').values('queued_at', 'source_refresh_requested_at',
        'finished_at')[:1]),
    'asset_stale_minutes': Character.UpdateSection.time_until_section_updates_are_stale()
        .get(Character.UpdateSection.ASSETS),
    'memberaudit_schedule': schedules('memberaudit.tasks.run_regular_updates'),
    'asset_characters': list(Character.objects.order_by('pk').values('pk',
        'is_disabled', 'eve_character__character_id')[:5001]),
    'asset_status': list(CharacterUpdateStatus.objects.filter(section='assets',
        character__is_disabled=False).order_by('character_id').values('character_id',
        'is_success', 'has_token_error', 'run_finished_at')[:5001]),
}
print('BUH_SYNC='+json.dumps(result, default=str))
"""


def live_sync_probe(host):
    output = host._manage_live(
        "shell", "-c", PROBE_CODE, context="Read-only current data sync evidence"
    )
    records = [line[9:] for line in output.splitlines() if line.startswith("BUH_SYNC=")]
    if len(records) != 1:
        raise DeploymentError("Current sync evidence is missing or ambiguous")
    return json.loads(records[0])


def validate_sync(status, review, cutoff, *, now):
    """Collect every failure; never silently replace an unknown schedule."""
    findings = []

    def fail(model, pk, field, reason, **details):
        findings.append(dict(model=model, pk=pk, field=field, reason=reason, **details))

    def clock(model, row, field, maximum, *, after=cutoff):
        value = row.get(field)
        try:
            updated = stamp(value)
            age = (now - updated).total_seconds()
            if not after < updated <= now or age > maximum:
                fail(
                    model,
                    row.get("pk"),
                    field,
                    "timestamp-outside-required-window",
                    updated_at=value,
                    age_seconds=round(age, 3),
                    maximum_age_seconds=maximum,
                )
        except (TypeError, ValueError):
            fail(model, row.get("pk"), field, "missing-or-invalid-timestamp")

    def interval(key):
        rows = status.get(key, [])
        enabled = [row for row in rows if row.get("enabled") is True]
        if len(rows) > 20 or len(enabled) != 1:
            fail(key, None, "enabled", "expected-one-enabled-periodic-task")
            return None
        row = enabled[0]
        every, period = row.get("interval__every"), row.get("interval__period")
        factors = {"seconds": 1, "minutes": 60, "hours": 3600}
        if (
            type(every) is not int
            or every <= 0
            or period not in factors
            or any(
                row.get(field) is not None
                for field in ("crontab_id", "clocked_id", "solar_id")
            )
        ):
            fail(key, None, "interval", "unsupported-or-ambiguous-schedule")
            return None
        seconds = every * factors[period]
        if not 60 <= seconds <= 3600:
            fail(key, None, "interval", "schedule-outside-reviewed-hourly-bound")
            return None
        clock(key, row, "last_run_at", 2 * seconds + QUEUE_GRACE_SECONDS)
        return seconds

    clock("snapshot", status, "captured_at", 120)
    if status.get("log_timezone") != "UTC":
        fail("snapshot", None, "log_timezone", "UTC-log-timestamps-required")
    dispatch = interval("moon_tax_schedule")
    config = status.get("moon_tax_config", [])
    hours = config[0].get("audit_interval_hours") if len(config) == 1 else None
    if type(hours) is not int or not 1 <= hours <= 24:
        fail(
            "moon_tax_config",
            None,
            "audit_interval_hours",
            "expected-one-valid-audit-interval",
        )
    ledger_age = (
        hours * 3600 + dispatch + QUEUE_GRACE_SECONDS
        if type(hours) is int and 1 <= hours <= 24 and dispatch
        else None
    )
    audits = status.get("completed_audits", [])
    ledger_after = cutoff
    if len(audits) != 1 or ledger_age is None:
        fail(
            "completed_audits",
            None,
            "source_refresh_requested_at",
            "completed-audit-evidence-required",
        )
    else:
        row = audits[0]
        for field in ("queued_at", "source_refresh_requested_at", "finished_at"):
            clock("completed_audits", row, field, ledger_age)
        try:
            queued, requested, finished = (
                stamp(row[field])
                for field in ("queued_at", "source_refresh_requested_at", "finished_at")
            )
            if not queued <= requested <= finished:
                fail("completed_audits", None, "finished_at", "audit-chronology-invalid")
            ledger_after = max(cutoff, requested)
        except (KeyError, TypeError, ValueError):
            pass  # Named missing-clock findings are already retained.

    fields = {
        "structures_owners": (
            {"is_active", "is_up", "is_included_in_service_status"},
            {
                "structures_last_update_at": 7200,
                "assets_last_update_at": 7200,
                "notifications_last_update_at": 1800,
                "forwarding_last_update_at": 1800,
            },
        ),
        "moonmining_owners": ({"is_enabled", "last_update_ok"}, {"last_update_at": 1800}),
        "refineries": ({"ledger_last_update_ok"}, {"ledger_last_update_at": ledger_age}),
    }
    for name, (flags, clocks) in fields.items():
        rows = status.get(name, [])
        if len(rows) != len(review[name]) or {row.get("pk") for row in rows} != set(
            review[name]
        ):
            fail(name, None, "pk", "reviewed-population-changed")
        for row in rows[:500]:
            for field in sorted(flags):
                if row.get(field) is not True:
                    fail(
                        name,
                        row.get("pk"),
                        field,
                        "required-success-flag-not-true",
                        value=row.get(field),
                    )
            if (
                name == "refineries"
                and row.get("owner_id") not in review["moonmining_owners"]
            ):
                fail(name, row.get("pk"), "owner_id", "reviewed-owner-changed")
            for field, maximum in clocks.items():
                if maximum is not None:
                    clock(
                        name,
                        row,
                        field,
                        maximum,
                        after=ledger_after if name == "refineries" else cutoff,
                    )

    assets_dispatch = interval("memberaudit_schedule")
    minutes = status.get("asset_stale_minutes")
    asset_age = None
    if type(minutes) is not int or not 1 <= minutes <= 1440 or assets_dispatch is None:
        fail(
            "asset_status",
            None,
            "asset_stale_minutes",
            "valid-section-and-dispatch-interval-required",
        )
    else:
        asset_age = minutes * 60 + assets_dispatch + QUEUE_GRACE_SECONDS
    chars, assets = status.get("asset_characters", []), status.get("asset_status", [])
    active = {row["pk"]: row for row in chars if row.get("is_disabled") is False}
    if (
        not active
        or len(chars) > 5000
        or len(assets) > 5000
        or len({row.get("pk") for row in chars}) != len(chars)
        or len(assets) != len(active)
        or {row.get("character_id") for row in assets} != set(active)
    ):
        fail("asset_status", None, "character_id", "active-assets-population-incomplete")
    for row in assets[:5000]:
        pk = row.get("character_id")
        if row.get("is_success") is not True or row.get("has_token_error") is not False:
            fail(
                "asset_status", pk, "is_success", "asset-update-unsuccessful-or-token-error"
            )
        if asset_age is not None:
            clock("asset_status", {**row, "pk": pk}, "run_finished_at", asset_age)
    for binding in review.get("asset_recoveries", []):
        former, identity = binding["previous_character_pk"], binding["eve_character_id"]
        matches = [
            row
            for row in active.values()
            if row.get("eve_character__character_id") == identity
        ]
        reused = [
            row
            for row in chars
            if row.get("pk") == former
            and row.get("eve_character__character_id") != identity
        ]
        if len(matches) != 1 or reused:
            fail(
                "asset_characters",
                former,
                "eve_character__character_id",
                "reviewed-identity-not-resolved",
            )

    if findings:
        raise SyncEvidenceError(findings)
    return {
        "ledger_max_age_seconds": ledger_age,
        "audit_interval_hours": hours,
        "audit_dispatch_seconds": dispatch,
        "queue_grace_seconds": QUEUE_GRACE_SECONDS,
        "asset_max_age_seconds": asset_age,
        "active_asset_characters": len(active),
    }
