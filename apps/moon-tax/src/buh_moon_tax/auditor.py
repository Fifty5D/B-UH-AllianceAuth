"""Extraction-period reconciliation across Moon Mining and Member Audit sources."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from allianceauth.notifications import notify
from django.db import transaction
from django.db.models import Q, Sum
from django.utils.timezone import now
from memberaudit.models import Character, CharacterContract
from moonmining.models import Extraction, MiningLedgerRecord

from .access_roles import director_users
from .exemptions import ExemptionIndex
from .models import (
    ZERO,
    AuditRun,
    BillingPreference,
    CompressionRule,
    MiningLine,
    NotificationState,
    PaymentCandidate,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxAssessment,
    TaxConfiguration,
    TaxPeriod,
)
from .payments import (
    recalculate_assessment,
    recalculate_period,
    reconcile_payment_exemptions,
)
from .pricing import create_price_snapshots

CENT = Decimal("0.01")
RATE_DIVISOR = Decimal(100)
FINISHED_CONTRACT_STATUSES = (
    CharacterContract.STATUS_FINISHED,
    CharacterContract.STATUS_FINISHED_CONTRACTOR,
    CharacterContract.STATUS_FINISHED_ISSUER,
)


def _money(value) -> Decimal:
    return Decimal(value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def _entity_name(entity, fallback: str) -> str:
    name = getattr(entity, "name", None)
    return str(name or fallback)


def _user_display(user) -> str:
    try:
        main = user.profile.main_character
        if main:
            return main.character_name
    except AttributeError:
        pass
    return user.username


def _effective_policy(structure_id: int, pop_at):
    return (
        StructureTaxPolicy.objects.filter(
            structure_id=structure_id,
            enabled=True,
            effective_from__lte=pop_at,
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=pop_at))
        .order_by("-effective_from", "-pk")
        .first()
    )


def _effective_rate(config, structure_id: int, pop_at) -> Decimal:
    policy = _effective_policy(structure_id, pop_at)
    return Decimal(policy.tax_rate if policy else config.default_tax_rate)


def _recipient_snapshot(config, policy) -> list[dict]:
    recipients = policy.payment_recipients.filter(enabled=True) if policy else None
    if recipients is None or not recipients.exists():
        recipients = config.default_payment_recipients.filter(enabled=True)
    values = [
        {"kind": item.kind, "id": item.entity_id, "name": item.name}
        for item in recipients.order_by("kind", "name")
    ]
    if not values:
        if config.payment_character_id:
            values.append(
                {
                    "kind": PaymentRecipient.Kind.CHARACTER,
                    "id": config.payment_character_id,
                    "name": config.payment_character_name,
                }
            )
        if config.payment_corporation_id:
            values.append(
                {
                    "kind": PaymentRecipient.Kind.CORPORATION,
                    "id": config.payment_corporation_id,
                    "name": config.payment_corporation_name,
                }
            )
    return values


def synchronize_periods(audit_run: AuditRun, config: TaxConfiguration) -> list[TaxPeriod]:
    """Create or update periods without deleting historical periods."""

    current = now()
    periods = []
    extractions = Extraction.objects.selected_related_defaults().exclude(
        status=Extraction.Status.CANCELED
    )
    for extraction in extractions:
        pop_at = extraction.fractured_at or extraction.chunk_arrival_at
        if not pop_at or pop_at > current:
            continue
        closes_at = pop_at + dt.timedelta(days=config.mining_window_days)
        status = TaxPeriod.Status.OPEN if closes_at > current else TaxPeriod.Status.DUE
        moon = extraction.refinery.moon
        owner = extraction.refinery.owner
        corporation = owner.corporation
        policy = _effective_policy(extraction.refinery_id, pop_at)
        defaults = {
            "structure_name": extraction.refinery.name,
            "corporation_id": corporation.corporation_id,
            "corporation_name": corporation.corporation_name,
            "moon_id": moon.id if moon else None,
            "moon_name": moon.name if moon else "",
            "extraction_started_at": extraction.started_at,
            "pop_at": pop_at,
            "closes_at": closes_at,
            "due_at": closes_at,
            "tax_rate": Decimal(
                policy.tax_rate if policy else config.default_tax_rate
            ),
            "allowed_recipients": _recipient_snapshot(config, policy),
            "last_audit_run": audit_run,
        }
        period, created = TaxPeriod.objects.get_or_create(
            structure_id=extraction.refinery_id,
            source_started_at=extraction.started_at,
            defaults={**defaults, "status": status},
        )
        if not created:
            # Source timing/name corrections remain safe. Settled and canceled periods
            # never regress to open merely because another audit ran.
            was_closed = period.closes_at <= current
            for field, value in defaults.items():
                if was_closed and field in {"tax_rate", "allowed_recipients"}:
                    continue
                setattr(period, field, value)
            if period.status not in {
                TaxPeriod.Status.SETTLED,
                TaxPeriod.Status.CANCELED,
            }:
                period.status = status
            period.save()
        periods.append(period)
    return periods


def _period_for_record(periods_by_refinery, record):
    """Assign a day-level ESI row to the newest eligible pop at that refinery."""

    for period in periods_by_refinery.get(record.refinery_id, []):
        if period.pop_at.date() <= record.day <= period.closes_at.date():
            return period
    return None


def _prepare_ledger_rows(periods, warnings):
    periods_by_refinery = defaultdict(list)
    for period in periods:
        periods_by_refinery[period.structure_id].append(period)
    for values in periods_by_refinery.values():
        values.sort(key=lambda period: period.pop_at, reverse=True)

    rules = {
        rule.raw_type_id: rule
        for rule in CompressionRule.objects.filter(enabled=True)
    }
    prepared = []
    ledger = MiningLedgerRecord.objects.select_related(
        "refinery",
        "character",
        "corporation",
        "ore_type",
        "user",
    ).order_by("day", "pk")
    missing_rule_types = set()
    for record in ledger:
        period = _period_for_record(periods_by_refinery, record)
        if not period:
            continue
        rule = rules.get(record.ore_type_id)
        compressed_quantity = ZERO
        if rule:
            compressed_quantity = (
                Decimal(record.quantity) * rule.multiplier
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        else:
            missing_rule_types.add((record.ore_type_id, record.ore_type.name))
        prepared.append(
            {
                "record": record,
                "period": period,
                "rule": rule,
                "compressed_quantity": compressed_quantity,
            }
        )

    # Resolve all existing preserved lines in one bounded query instead of one
    # query per observer row. The ESI ledger is a recent window, so the day range
    # keeps this lookup small even when years of archive data are retained.
    existing_by_key = {}
    if prepared:
        period_ids = {item["period"].pk for item in prepared}
        days = [item["record"].day for item in prepared]
        existing_lines = MiningLine.objects.filter(
            period_id__in=period_ids,
            ledger_day__range=(min(days), max(days)),
        )
        existing_by_key = {
            (
                line.period_id,
                line.ledger_day,
                line.character_id,
                line.raw_type_id,
            ): line
            for line in existing_lines.iterator(chunk_size=2000)
        }

    price_needs = defaultdict(lambda: {"name": "", "quantity": ZERO})
    current = now()
    for item in prepared:
        record = item["record"]
        period = item["period"]
        existing = existing_by_key.get(
            (period.pk, record.day, record.character_id, record.ore_type_id)
        )
        frozen = bool(
            existing and existing.pricing_complete and period.closes_at <= current
        )
        item["existing"] = existing
        item["frozen"] = frozen
        rule = item["rule"]
        if rule and not frozen:
            price_needs[rule.compressed_type_id]["name"] = rule.compressed_type_name
            price_needs[rule.compressed_type_id]["quantity"] += item[
                "compressed_quantity"
            ]
    if missing_rule_types:
        names = ", ".join(
            f"{name} ({type_id})" for type_id, name in sorted(missing_rule_types)
        )
        warnings.append(f"Missing editable compression mappings: {names}")
    return prepared, dict(price_needs)


def synchronize_mining_lines(
    audit_run: AuditRun,
    periods,
    warnings,
    exemption_index=None,
) -> int:
    """Upsert visible ESI rows and preserve rows that have fallen out of ESI history."""

    exemption_index = exemption_index or ExemptionIndex()
    prepared, price_needs = _prepare_ledger_rows(periods, warnings)
    snapshots, price_warnings = create_price_snapshots(audit_run, price_needs)
    warnings.extend(price_warnings)
    count = 0
    for item in prepared:
        record = item["record"]
        period = item["period"]
        rule = item["rule"]
        existing = item["existing"]
        linked_user_id = exemption_index.user_id_for(
            record.character_id, record.user_id
        )
        exemption = exemption_index.match(
            user_id=linked_user_id,
            character_id=record.character_id,
            day=record.day,
        )
        defaults = {
            "character_name": _entity_name(record.character, str(record.character_id)),
            "recorded_corporation_id": record.corporation_id,
            "recorded_corporation_name": _entity_name(
                record.corporation, str(record.corporation_id)
            ),
            "auth_user_id": linked_user_id,
            "raw_type_name": record.ore_type.name,
            "raw_quantity": record.quantity,
            "tax_rate": period.tax_rate,
            "is_exempt": bool(exemption),
            "exemption_reason": exemption.reason if exemption else "",
            "last_audit_run": audit_run,
        }
        if rule:
            defaults.update(
                {
                    "compressed_type_id": rule.compressed_type_id,
                    "compressed_type_name": rule.compressed_type_name,
                    "compressed_quantity": item["compressed_quantity"],
                }
            )
        else:
            defaults.update(
                {
                    "compressed_type_id": None,
                    "compressed_type_name": "",
                    "compressed_quantity": ZERO,
                    "compressed_unit_price": ZERO,
                    "gross_value": ZERO,
                    "tax_due": ZERO,
                    "pricing_complete": False,
                }
            )

        if item["frozen"]:
            unit_price = existing.compressed_unit_price
            pricing_complete = existing.pricing_complete
        elif rule:
            snapshot = snapshots[rule.compressed_type_id]
            unit_price = snapshot.weighted_unit_price
            pricing_complete = snapshot.complete
        else:
            unit_price = ZERO
            pricing_complete = False
        gross = _money(item["compressed_quantity"] * unit_price)
        tax_due = (
            ZERO
            if exemption
            else _money(gross * Decimal(period.tax_rate) / RATE_DIVISOR)
        )
        defaults.update(
            {
                "compressed_unit_price": unit_price,
                "gross_value": gross,
                "tax_due": tax_due,
                "pricing_complete": pricing_complete,
            }
        )
        MiningLine.objects.update_or_create(
            period=period,
            ledger_day=record.day,
            character_id=record.character_id,
            raw_type_id=record.ore_type_id,
            defaults=defaults,
        )
        count += 1
    return count


def recheck_mining_line_exemptions(exemption_index=None) -> int:
    """Re-evaluate preserved history and every alt after exemption changes."""

    exemption_index = exemption_index or ExemptionIndex()
    changed = []
    changed_at = now()
    for line in MiningLine.objects.all().iterator():
        linked_user_id = exemption_index.user_id_for(
            line.character_id, line.auth_user_id
        )
        exemption = exemption_index.match(
            user_id=linked_user_id,
            character_id=line.character_id,
            day=line.ledger_day,
        )
        expected_exempt = bool(exemption)
        expected_reason = exemption.reason if exemption else ""
        expected_tax = (
            ZERO
            if exemption
            else _money(line.gross_value * Decimal(line.tax_rate) / RATE_DIVISOR)
        )
        if (
            line.auth_user_id != linked_user_id
            or line.is_exempt != expected_exempt
            or line.exemption_reason != expected_reason
            or line.tax_due != expected_tax
        ):
            line.auth_user_id = linked_user_id
            line.is_exempt = expected_exempt
            line.exemption_reason = expected_reason
            line.tax_due = expected_tax
            line.updated_at = changed_at
            changed.append(line)
    if changed:
        MiningLine.objects.bulk_update(
            changed,
            (
                "auth_user",
                "is_exempt",
                "exemption_reason",
                "tax_due",
                "updated_at",
            ),
            batch_size=500,
        )
    return len(changed)


def _combine_for(user_id: int, config: TaxConfiguration, preference_map) -> bool:
    preference = preference_map.get(user_id)
    return (
        preference.combine_characters
        if preference is not None
        else config.combine_characters_by_default
    )


def rebuild_assessments(periods, config, warnings) -> int:
    """Aggregate preserved lines by account or character without losing allocations."""

    preference_map = {
        item.user_id: item for item in BillingPreference.objects.select_related("user")
    }
    count = 0
    for period in periods:
        locked_modes = {}
        locked_characters = set()
        decided_assessments = period.assessments.filter(
            Q(payment_allocations__isnull=False) | Q(adjustments__isnull=False),
        ).distinct()
        for existing_assessment in decided_assessments:
            # A character bill is the immutable accounting anchor for any decision
            # made while that pilot was unlinked (or billed separately).  If Auth
            # ownership changes later, keep that character in the same bucket and
            # update the row's current owner instead of creating a second account
            # bill while preserving the decided row as stale.
            if (
                not existing_assessment.combined_characters
                and existing_assessment.character_id
            ):
                locked_characters.add(existing_assessment.character_id)
            if not existing_assessment.auth_user_id:
                continue
            previous = locked_modes.setdefault(
                existing_assessment.auth_user_id,
                existing_assessment.combined_characters,
            )
            if previous != existing_assessment.combined_characters:
                warnings.append(
                    f"Period {period.pk} has conflicting approved billing modes for "
                    f"Auth user {existing_assessment.auth_user_id}; director review is required."
                )
        buckets = defaultdict(lambda: {"lines": [], "user": None, "character_id": None})
        for line in period.mining_lines.select_related("auth_user").all():
            combine = bool(
                line.auth_user_id
                and line.character_id not in locked_characters
                and locked_modes.get(
                    line.auth_user_id,
                    _combine_for(line.auth_user_id, config, preference_map),
                )
            )
            if combine:
                key = f"user:{line.auth_user_id}"
                buckets[key]["user"] = line.auth_user
            else:
                key = f"character:{line.character_id}"
                buckets[key]["user"] = line.auth_user
                buckets[key]["character_id"] = line.character_id
            buckets[key]["lines"].append(line)

        active_keys = set()
        for key, bucket in buckets.items():
            active_keys.add(key)
            lines = bucket["lines"]
            user = bucket["user"]
            gross = sum((line.gross_value for line in lines), ZERO)
            tax = sum((line.tax_due for line in lines), ZERO)
            combined = key.startswith("user:")
            display = _user_display(user) if combined else lines[0].character_name
            assessment, _ = TaxAssessment.objects.update_or_create(
                period=period,
                billing_key=key,
                defaults={
                    "auth_user": user,
                    "character_id": bucket["character_id"],
                    "display_name": display,
                    "combined_characters": combined,
                    "gross_value": _money(gross),
                    "calculated_tax_due": _money(tax),
                    "tax_due": _money(tax),
                    "due_at": period.due_at,
                },
            )
            recalculate_assessment(assessment)
            count += 1

        stale = period.assessments.exclude(billing_key__in=active_keys)
        for assessment in stale:
            if assessment.payment_allocations.exists() or assessment.adjustments.exists():
                warnings.append(
                    f"Preserved retired billing row {assessment.pk} because it has "
                    "approved allocations or Director adjustments."
                )
                assessment.status = TaxAssessment.Status.REVIEW
                assessment.save(update_fields=("status", "updated_at"))
            else:
                assessment.delete()
        recalculate_period(period)
    return count


def _payment_targets(config):
    targets = {
        item.entity_id: item.name
        for item in PaymentRecipient.objects.filter(enabled=True)
        .filter(
            Q(default_for_configurations__isnull=False)
            | Q(structure_policies__isnull=False)
        )
        .distinct()
    }
    if config.payment_character_id:
        targets[config.payment_character_id] = config.payment_character_name
    if config.payment_corporation_id:
        targets[config.payment_corporation_id] = config.payment_corporation_name
    # Closed extraction periods retain an immutable destination snapshot. Keep
    # importing recent evidence for those IDs even if a recipient is later
    # removed from the live checklist.
    for snapshot in TaxPeriod.objects.values_list("allowed_recipients", flat=True):
        for recipient in snapshot or []:
            if recipient.get("id"):
                targets[int(recipient["id"])] = recipient.get("name") or str(
                    recipient["id"]
                )
    return targets


def _wallet_payment_source(entry, contracts_by_id) -> str | None:
    if entry.context_id_type != entry.CONTEXT_ID_TYPE_CONTRACT_ID:
        return PaymentCandidate.Source.WALLET
    contract = contracts_by_id.get(entry.context_id)
    if contract and not contract.items.exists():
        return PaymentCandidate.Source.CONTRACT
    return None


def import_payment_candidates(audit_run: AuditRun, config) -> int:
    """Import candidate evidence only; director approval is intentionally separate."""

    targets = _payment_targets(config)
    if not targets:
        return 0
    imported = 0
    characters = Character.objects.select_related(
        "eve_character",
        "eve_character__character_ownership",
        "eve_character__character_ownership__user",
    )
    for character in characters:
        user = character.user
        contracts = list(
            character.contracts.filter(
                status__in=FINISHED_CONTRACT_STATUSES
            ).prefetch_related("items")
        )
        contracts_by_id = {contract.contract_id: contract for contract in contracts}
        entries = character.wallet_journal.filter(
            amount__lt=0,
            second_party_id__in=targets,
        ).select_related("second_party")
        seen_contract_ids = set()
        for entry in entries:
            source = _wallet_payment_source(entry, contracts_by_id)
            if source is None:
                continue
            if source == PaymentCandidate.Source.CONTRACT and entry.context_id:
                seen_contract_ids.add(entry.context_id)
            _, created = PaymentCandidate.objects.get_or_create(
                source_key=f"wallet:{character.character_id}:{entry.entry_id}",
                defaults={
                    "source": source,
                    "source_id": entry.entry_id,
                    "payer_character_id": character.character_id,
                    "payer_character_name": character.name,
                    "auth_user": user,
                    "recipient_id": entry.second_party_id,
                    "recipient_name": targets[entry.second_party_id],
                    "amount": _money(abs(entry.amount)),
                    "occurred_at": entry.date,
                    "reference": (entry.reason or entry.description or "")[:255],
                    "evidence": {
                        "journal_entry_id": entry.entry_id,
                        "ref_type": entry.ref_type,
                        "context_id": entry.context_id,
                        "context_id_type": entry.context_id_type,
                        "description": entry.description,
                        "reason": entry.reason,
                    },
                    "imported_by_audit": audit_run,
                },
            )
            imported += int(created)

        for contract in contracts:
            if contract.contract_id in seen_contract_ids or contract.items.exists():
                continue
            target_id = None
            amount = ZERO
            if (
                contract.issuer_id == character.character_id
                and (contract.assignee_id in targets or contract.acceptor_id in targets)
                and Decimal(contract.reward or ZERO) > ZERO
            ):
                target_id = (
                    contract.acceptor_id
                    if contract.acceptor_id in targets
                    else contract.assignee_id
                )
                amount = Decimal(contract.reward)
            elif (
                contract.issuer_id in targets
                and (
                    contract.assignee_id == character.character_id
                    or contract.acceptor_id == character.character_id
                )
                and Decimal(contract.price or ZERO) > ZERO
            ):
                target_id = contract.issuer_id
                amount = Decimal(contract.price)
            if not target_id or amount <= ZERO:
                continue
            _, created = PaymentCandidate.objects.get_or_create(
                source_key=f"contract:{character.character_id}:{contract.contract_id}",
                defaults={
                    "source": PaymentCandidate.Source.CONTRACT,
                    "source_id": contract.contract_id,
                    "payer_character_id": character.character_id,
                    "payer_character_name": character.name,
                    "auth_user": user,
                    "recipient_id": target_id,
                    "recipient_name": targets[target_id],
                    "amount": _money(amount),
                    "occurred_at": contract.date_completed or contract.date_accepted,
                    "reference": (contract.title or "Completed ISK contract")[:255],
                    "evidence": {
                        "contract_id": contract.contract_id,
                        "status": contract.status,
                        "price": str(contract.price or ZERO),
                        "reward": str(contract.reward or ZERO),
                        "issuer_id": contract.issuer_id,
                        "assignee_id": contract.assignee_id,
                        "acceptor_id": contract.acceptor_id,
                        "item_count": 0,
                    },
                    "imported_by_audit": audit_run,
                },
            )
            imported += int(created)
    return imported


def _deduped_notify(event_key: str, users, title: str, message: str, level="info"):
    payload_hash = hashlib.sha256(
        json.dumps({"title": title, "message": message}, sort_keys=True).encode()
    ).hexdigest()
    state = NotificationState.objects.filter(event_key=event_key).first()
    if state and state.payload_hash == payload_hash:
        return 0
    sent = 0
    for user in users:
        notify(user=user, title=title, message=message, level=level)
        sent += 1
    NotificationState.objects.update_or_create(
        event_key=event_key,
        defaults={"last_sent_at": now(), "payload_hash": payload_hash},
    )
    return sent


def dispatch_notifications(config, audit_run) -> int:
    sent = 0
    if config.notify_member_new_tax:
        newly_due = TaxAssessment.objects.select_related("auth_user", "period").filter(
            auth_user__isnull=False,
            status__in=(TaxAssessment.Status.DUE, TaxAssessment.Status.PARTIAL),
            first_notified_at__isnull=True,
        )
        for assessment in newly_due:
            notify(
                user=assessment.auth_user,
                title=f"New moon tax: {assessment.outstanding:,.2f} ISK due",
                message=(
                    f"{assessment.period.moon_name or assessment.period.structure_name} "
                    f"closed on {assessment.period.closes_at:%Y-%m-%d %H:%M} EVE time. "
                    f"Your current amount due is {assessment.outstanding:,.2f} ISK."
                ),
                level="warning",
            )
            assessment.first_notified_at = now()
            assessment.save(update_fields=("first_notified_at", "updated_at"))
            sent += 1

    directors = list(director_users())
    if config.notify_director_unpaid:
        totals = TaxAssessment.objects.filter(
            status__in=(TaxAssessment.Status.DUE, TaxAssessment.Status.PARTIAL)
        ).aggregate(count=models_count("pk"), due=Sum("tax_due"), paid=Sum("approved_paid"))
        count = totals["count"] or 0
        if count:
            outstanding = _money((totals["due"] or ZERO) - (totals["paid"] or ZERO))
            sent += _deduped_notify(
                "director:unpaid-summary",
                directors,
                f"Moon Tax: {count} unpaid or partial bills",
                f"The current approved outstanding total is {outstanding:,.2f} ISK.",
                "warning",
            )
    if config.notify_director_missing_data:
        unlinked = MiningLine.objects.filter(auth_user__isnull=True).values(
            "character_id"
        ).distinct().count()
        unpriced = MiningLine.objects.filter(pricing_complete=False).count()
        if unlinked or unpriced:
            sent += _deduped_notify(
                "director:missing-data",
                directors,
                "Moon Tax data needs review",
                f"Unlinked/outside miners: {unlinked}. Unpriced ledger rows: {unpriced}. "
                f"Audit #{audit_run.pk} contains the detailed warnings.",
                "danger",
            )
    return sent


def models_count(field):
    """Local import keeps the rest of this module's aggregation list concise."""

    from django.db.models import Count

    return Count(field)


@transaction.atomic
def reconcile(audit_run: AuditRun) -> dict:
    """Run one deterministic reconciliation against the currently stored sources."""

    audit_run.status = AuditRun.Status.RUNNING
    audit_run.started_at = audit_run.started_at or now()
    audit_run.save(update_fields=("status", "started_at"))
    warnings = list(audit_run.warnings or [])
    config, _ = TaxConfiguration.objects.get_or_create(singleton_id=1)
    exemption_index = ExemptionIndex()
    exemption_state_hash = exemption_index.state_hash()
    full_exemption_recheck = config.exemption_state_hash != exemption_state_hash
    periods = synchronize_periods(audit_run, config)
    mining_count = synchronize_mining_lines(
        audit_run,
        periods,
        warnings,
        exemption_index,
    )
    exemption_line_count = (
        recheck_mining_line_exemptions(exemption_index)
        if full_exemption_recheck
        else 0
    )
    assessment_count = rebuild_assessments(periods, config, warnings)
    payment_count = import_payment_candidates(audit_run, config)
    payment_scope = None
    if not full_exemption_recheck:
        payment_scope = PaymentCandidate.objects.filter(imported_by_audit=audit_run)
    payment_exemptions = reconcile_payment_exemptions(
        exemption_index,
        payments=payment_scope,
    )
    config.exemption_state_hash = exemption_state_hash
    config.exemption_rechecked_at = now()
    config.save(
        update_fields=(
            "exemption_state_hash",
            "exemption_rechecked_at",
            "updated_at",
        )
    )
    notification_count = dispatch_notifications(config, audit_run)
    summary = {
        "periods": len(periods),
        "mining_lines_processed": mining_count,
        "mining_exemptions_updated": exemption_line_count,
        "assessments": assessment_count,
        "new_payment_candidates": payment_count,
        "payments_excluded_by_exemption": payment_exemptions["excluded"],
        "payments_restored_after_exemption": payment_exemptions["restored"],
        "full_exemption_history_recheck": full_exemption_recheck,
        "notifications": notification_count,
        "unlinked_miners": MiningLine.objects.filter(auth_user__isnull=True)
        .values("character_id")
        .distinct()
        .count(),
        "pending_payments": PaymentCandidate.objects.filter(
            status=PaymentCandidate.Status.PENDING
        ).count(),
    }
    audit_run.summary = summary
    audit_run.warnings = warnings
    audit_run.finished_at = now()
    audit_run.status = AuditRun.Status.WARNING if warnings else AuditRun.Status.COMPLETE
    audit_run.save(
        update_fields=("summary", "warnings", "finished_at", "status")
    )
    return summary
