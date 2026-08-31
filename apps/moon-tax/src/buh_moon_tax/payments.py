"""Director-controlled payment allocation and balance recalculation."""

from decimal import Decimal

from allianceauth.notifications import notify
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils.timezone import now

from .exemptions import ExemptionIndex
from .models import (
    ZERO,
    AssessmentAdjustment,
    PaymentAllocation,
    PaymentCandidate,
    TaxAssessment,
    TaxConfiguration,
    TaxPeriod,
)

AUTO_EXEMPT_PREFIX = "[AUTO-EXEMPT]"


def _status_for_assessment(assessment: TaxAssessment) -> str:
    if assessment.tax_due <= ZERO:
        if assessment.calculated_tax_due > ZERO and assessment.adjustment_total < ZERO:
            return TaxAssessment.Status.WAIVED
        return TaxAssessment.Status.EXEMPT
    if assessment.approved_paid >= assessment.tax_due:
        return TaxAssessment.Status.PAID
    if assessment.approved_paid > ZERO:
        return TaxAssessment.Status.PARTIAL
    return (
        TaxAssessment.Status.DUE
        if assessment.due_at <= now()
        else TaxAssessment.Status.OPEN
    )


def recalculate_assessment(assessment: TaxAssessment) -> None:
    adjustment_total = (
        assessment.adjustments.filter(reversed_at__isnull=True).aggregate(
            total=Sum("amount")
        )["total"]
        or ZERO
    )
    # Compatibility for programmatic callers and fixtures created before the
    # calculated/effective split. The data migration performs the same copy on
    # live rows.
    calculated = assessment.calculated_tax_due
    if calculated == ZERO and assessment.tax_due != ZERO and adjustment_total == ZERO:
        calculated = assessment.tax_due
    approved = (
        assessment.payment_allocations.filter(
            category=PaymentAllocation.Category.TAX
        ).aggregate(total=Sum("amount"))["total"]
        or ZERO
    )
    assessment.calculated_tax_due = calculated
    assessment.adjustment_total = adjustment_total
    assessment.tax_due = max(ZERO, calculated + adjustment_total)
    assessment.approved_paid = approved
    assessment.status = _status_for_assessment(assessment)
    assessment.save(
        update_fields=(
            "calculated_tax_due",
            "adjustment_total",
            "tax_due",
            "approved_paid",
            "status",
            "updated_at",
        )
    )


@transaction.atomic
def create_assessment_adjustment(
    assessment: TaxAssessment,
    *,
    action: str,
    amount: Decimal | None,
    reason: str,
    director,
) -> AssessmentAdjustment:
    """Create a reversible waiver/correction while retaining the original bill."""

    assessment = TaxAssessment.objects.select_for_update().get(pk=assessment.pk)
    recalculate_assessment(assessment)
    action = action.upper()
    reason = reason.strip()
    if not reason:
        raise ValueError("A reason is required for every bill adjustment.")

    if action == "WAIVE_BALANCE":
        if assessment.outstanding <= ZERO:
            raise ValueError("This bill has no outstanding balance to waive.")
        kind = AssessmentAdjustment.Kind.WAIVER
        signed_amount = -assessment.outstanding
    elif action == "VOID_BILL":
        if assessment.approved_paid > ZERO:
            raise ValueError(
                "A bill with allocated payments cannot be voided. Waive its remaining "
                "balance or reset the payment first."
            )
        if assessment.tax_due <= ZERO:
            raise ValueError("This bill already has no effective tax due.")
        kind = AssessmentAdjustment.Kind.VOID
        signed_amount = -assessment.tax_due
    elif action in {"CREDIT", "DEBIT"}:
        entered = Decimal(amount or ZERO)
        if entered <= ZERO:
            raise ValueError("Enter an adjustment amount greater than zero ISK.")
        kind = AssessmentAdjustment.Kind.CORRECTION
        signed_amount = -entered if action == "CREDIT" else entered
    else:
        raise ValueError("Unknown bill adjustment action.")

    adjustment = AssessmentAdjustment(
        assessment=assessment,
        kind=kind,
        amount=signed_amount,
        reason=reason,
        created_by=director,
    )
    adjustment.full_clean()
    adjustment.save()
    recalculate_assessment(assessment)
    recalculate_period(assessment.period)
    return adjustment


@transaction.atomic
def reverse_assessment_adjustment(
    adjustment: AssessmentAdjustment,
    *,
    reason: str,
    director,
) -> AssessmentAdjustment:
    """Reverse one adjustment without erasing who made either decision."""

    adjustment = AssessmentAdjustment.objects.select_for_update().select_related(
        "assessment__period"
    ).get(pk=adjustment.pk)
    if adjustment.reversed_at:
        raise ValueError("This adjustment has already been reversed.")
    reason = reason.strip()
    if not reason:
        raise ValueError("A reversal reason is required.")
    adjustment.reversed_at = now()
    adjustment.reversed_by = director
    adjustment.reversal_reason = reason
    adjustment.full_clean()
    adjustment.save(
        update_fields=("reversed_at", "reversed_by", "reversal_reason")
    )
    recalculate_assessment(adjustment.assessment)
    recalculate_period(adjustment.assessment.period)
    return adjustment


def recalculate_period(period: TaxPeriod) -> None:
    totals = period.assessments.aggregate(
        gross=Sum("gross_value"), tax=Sum("tax_due"), paid=Sum("approved_paid")
    )
    period.gross_value = totals["gross"] or ZERO
    period.tax_due = totals["tax"] or ZERO
    period.approved_paid = totals["paid"] or ZERO
    if period.status != TaxPeriod.Status.CANCELED:
        has_assessments = period.assessments.exists()
        has_outstanding = (
            period.assessments.filter(tax_due__gt=F("approved_paid")).exists()
            if has_assessments
            else False
        )
        # Never let an overpayment or later Director credit on one account hide
        # another account's unpaid bill in the same extraction.
        if has_assessments and not has_outstanding:
            period.status = TaxPeriod.Status.SETTLED
        elif period.due_at <= now():
            period.status = TaxPeriod.Status.DUE
        else:
            period.status = TaxPeriod.Status.OPEN
    period.save(
        update_fields=(
            "gross_value",
            "tax_due",
            "approved_paid",
            "status",
            "updated_at",
        )
    )


def _notify_payment_decision(payment: PaymentCandidate) -> None:
    config = TaxConfiguration.objects.filter(singleton_id=1).first()
    if not config or not config.notify_member_payment_decision or not payment.auth_user_id:
        return
    decision = payment.get_status_display()
    notify(
        user=payment.auth_user,
        title=f"Moon Tax payment {decision.lower()}",
        message=(
            f"A director reviewed the {payment.amount:,.2f} ISK payment from "
            f"{payment.payer_character_name or payment.auth_user}. Decision: {decision}."
        ),
        level="success" if payment.status == PaymentCandidate.Status.APPROVED else "info",
    )


@transaction.atomic
def recalculate_payment(payment: PaymentCandidate, director=None, *, notify_user=True):
    allocations = list(payment.allocations.all())
    allocated = sum((item.amount for item in allocations), ZERO)
    categories = {item.category for item in allocations}
    if not allocations:
        if payment.status not in {
            PaymentCandidate.Status.REJECTED,
            PaymentCandidate.Status.IGNORED,
            PaymentCandidate.Status.EXEMPT,
        }:
            payment.status = PaymentCandidate.Status.PENDING
    elif allocated < payment.amount:
        payment.status = PaymentCandidate.Status.PARTIAL
    elif categories == {PaymentAllocation.Category.DONATION}:
        payment.status = PaymentCandidate.Status.DONATION
    elif categories == {PaymentAllocation.Category.IGNORED}:
        payment.status = PaymentCandidate.Status.IGNORED
    elif categories.issubset(
        {PaymentAllocation.Category.CARRYOVER, PaymentAllocation.Category.OVERAGE}
    ):
        payment.status = PaymentCandidate.Status.OVERAGE
    else:
        payment.status = PaymentCandidate.Status.APPROVED

    if director is not None:
        payment.reviewed_by = director
        payment.reviewed_at = now()
    payment.save(update_fields=("status", "reviewed_by", "reviewed_at", "review_note"))

    assessments = {
        item.assessment for item in allocations if item.assessment_id is not None
    }
    for assessment in assessments:
        recalculate_assessment(assessment)
    for period in {assessment.period for assessment in assessments}:
        recalculate_period(period)
    if notify_user:
        _notify_payment_decision(payment)
    return payment


def _eligible_assessments(payment: PaymentCandidate, assessments=None):
    if assessments is None:
        queryset = TaxAssessment.objects.select_related("period").exclude(
            status__in=(TaxAssessment.Status.PAID, TaxAssessment.Status.EXEMPT)
        )
        if payment.auth_user_id:
            queryset = queryset.filter(auth_user_id=payment.auth_user_id)
        elif payment.payer_character_id:
            queryset = queryset.filter(character_id=payment.payer_character_id)
        else:
            return []
        assessments = queryset.order_by("due_at", "period__pop_at", "pk")
    else:
        if payment.auth_user_id:
            assessments = [
                item
                for item in assessments
                if item.auth_user_id == payment.auth_user_id
            ]
        elif payment.payer_character_id:
            assessments = [
                item
                for item in assessments
                if item.character_id == payment.payer_character_id
            ]
        else:
            return []
        assessments = sorted(
            assessments,
            key=lambda item: (item.due_at, item.period.pop_at, item.pk),
        )
    eligible = []
    for assessment in assessments:
        if assessment.status in {
            TaxAssessment.Status.PAID,
            TaxAssessment.Status.EXEMPT,
        }:
            continue
        allowed_ids = {
            int(item["id"])
            for item in (assessment.period.allowed_recipients or [])
            if item.get("id")
        }
        if (
            payment.source == PaymentCandidate.Source.MANUAL
            or not allowed_ids
            or payment.recipient_id in allowed_ids
        ):
            eligible.append(assessment)
    return eligible


def _lock_payment_context(payment: PaymentCandidate, *, include_subject: bool):
    """Serialize one decision and every bill it can mutate in stable PK order."""

    payment = PaymentCandidate.objects.select_for_update().get(pk=payment.pk)
    affected_ids = set(
        payment.allocations.filter(assessment_id__isnull=False).values_list(
            "assessment_id", flat=True
        )
    )
    bill_filter = Q(pk__in=affected_ids)
    if include_subject:
        if payment.auth_user_id:
            bill_filter |= Q(auth_user_id=payment.auth_user_id)
        elif payment.payer_character_id:
            bill_filter |= Q(character_id=payment.payer_character_id)
    assessments = list(
        TaxAssessment.objects.select_for_update()
        .select_related("period")
        .filter(bill_filter)
        .order_by("pk")
    )
    return payment, assessments, affected_ids


def _clear_payment_allocations(
    payment: PaymentCandidate,
    *,
    locked_assessments,
    affected_ids,
) -> None:
    """Remove allocations and immediately repair every affected bill and period."""

    affected = [item for item in locked_assessments if item.pk in affected_ids]
    payment.allocations.all().delete()
    for assessment in affected:
        recalculate_assessment(assessment)
    for period in {assessment.period for assessment in affected}:
        recalculate_period(period)


@transaction.atomic
def exclude_payment_for_exemption(payment, exemption) -> PaymentCandidate:
    """Remove an exempt payer's payment from accounting while retaining evidence."""

    relinked_user_id = payment.auth_user_id
    payment, locked_assessments, affected_ids = _lock_payment_context(
        payment, include_subject=False
    )
    payment.auth_user_id = relinked_user_id
    evidence = dict(payment.evidence or {})
    exemption_evidence = dict(evidence.get("moon_tax_exemption") or {})
    if payment.status != PaymentCandidate.Status.EXEMPT:
        exemption_evidence = {
            "excluded_at": now().isoformat(),
            "exemption_id": exemption.pk,
            "reason": exemption.reason,
            "previous_status": payment.status,
            "previous_reviewed_by_id": payment.reviewed_by_id,
            "previous_reviewed_at": (
                payment.reviewed_at.isoformat() if payment.reviewed_at else None
            ),
            "previous_review_note": payment.review_note,
            "removed_allocations": [
                {
                    "assessment_id": allocation.assessment_id,
                    "category": allocation.category,
                    "amount": str(allocation.amount),
                    "notes": allocation.notes,
                }
                for allocation in payment.allocations.all()
            ],
        }
    exemption_evidence.update(
        {"exemption_id": exemption.pk, "reason": exemption.reason}
    )
    evidence["moon_tax_exemption"] = exemption_evidence
    _clear_payment_allocations(
        payment,
        locked_assessments=locked_assessments,
        affected_ids=affected_ids,
    )
    payment.evidence = evidence
    payment.status = PaymentCandidate.Status.EXEMPT
    payment.reviewed_by = None
    payment.reviewed_at = now()
    payment.review_note = (
        f"{AUTO_EXEMPT_PREFIX} Exemption #{exemption.pk}: {exemption.reason}"
    )
    payment.save(
        update_fields=(
            "auth_user",
            "evidence",
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_note",
        )
    )
    return payment


@transaction.atomic
def restore_system_exempt_payment(payment) -> PaymentCandidate:
    """Return only a system-excluded payment to review after exemption changes."""

    relinked_user_id = payment.auth_user_id
    payment, locked_assessments, affected_ids = _lock_payment_context(
        payment, include_subject=False
    )
    payment.auth_user_id = relinked_user_id
    if not payment.review_note.startswith(AUTO_EXEMPT_PREFIX):
        return payment
    _clear_payment_allocations(
        payment,
        locked_assessments=locked_assessments,
        affected_ids=affected_ids,
    )
    evidence = dict(payment.evidence or {})
    exemption_evidence = dict(evidence.get("moon_tax_exemption") or {})
    exemption_evidence["returned_to_review_at"] = now().isoformat()
    evidence["moon_tax_exemption"] = exemption_evidence
    payment.evidence = evidence
    payment.status = PaymentCandidate.Status.PENDING
    payment.reviewed_by = None
    payment.reviewed_at = None
    payment.review_note = ""
    payment.save(
        update_fields=(
            "auth_user",
            "evidence",
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_note",
        )
    )
    return payment


def reconcile_payment_exemptions(
    exemption_index=None,
    *,
    payments=None,
) -> dict[str, int]:
    """Re-evaluate the requested imported payments against current exemptions."""

    index = exemption_index or ExemptionIndex()
    counts = {"excluded": 0, "restored": 0, "relinked": 0}
    payments = payments if payments is not None else PaymentCandidate.objects.all()
    payments = payments.exclude(source=PaymentCandidate.Source.MANUAL).order_by(
        "occurred_at", "pk"
    )
    for payment in payments.iterator():
        linked_user_id = index.user_id_for(
            payment.payer_character_id, payment.auth_user_id
        )
        relinked = linked_user_id != payment.auth_user_id
        if relinked:
            payment.auth_user_id = linked_user_id
            counts["relinked"] += 1
        exemption = index.match(
            user_id=linked_user_id,
            character_id=payment.payer_character_id,
            day=payment.occurred_at.date(),
        )
        if exemption:
            note = f"{AUTO_EXEMPT_PREFIX} Exemption #{exemption.pk}: {exemption.reason}"
            if (
                payment.status != PaymentCandidate.Status.EXEMPT
                or payment.review_note != note
                or relinked
            ):
                exclude_payment_for_exemption(payment, exemption)
                counts["excluded"] += 1
        elif (
            payment.status == PaymentCandidate.Status.EXEMPT
            and payment.review_note.startswith(AUTO_EXEMPT_PREFIX)
        ):
            restore_system_exempt_payment(payment)
            counts["restored"] += 1
        elif relinked:
            payment.save(update_fields=("auth_user",))
    return counts


@transaction.atomic
def decide_payment(
    payment: PaymentCandidate,
    *,
    disposition: str,
    director,
    note: str = "",
) -> PaymentCandidate:
    """Apply one explicit director decision; nothing is auto-approved."""

    payment, locked_assessments, affected_ids = _lock_payment_context(
        payment, include_subject=True
    )
    if payment.status == PaymentCandidate.Status.EXEMPT:
        raise ValueError(
            "A system-excluded exempt payment cannot be manually reclassified."
        )
    _clear_payment_allocations(
        payment,
        locked_assessments=locked_assessments,
        affected_ids=affected_ids,
    )

    payment.reviewed_by = director
    payment.reviewed_at = now()
    payment.review_note = note

    if disposition == "pending":
        payment.status = PaymentCandidate.Status.PENDING
        payment.save(
            update_fields=("status", "reviewed_by", "reviewed_at", "review_note")
        )
        return payment
    if disposition == "reject":
        payment.status = PaymentCandidate.Status.REJECTED
        payment.save(
            update_fields=("status", "reviewed_by", "reviewed_at", "review_note")
        )
        _notify_payment_decision(payment)
        return payment

    remaining = Decimal(payment.amount)
    if disposition in {"oldest_carryover", "oldest_overage"}:
        for assessment in _eligible_assessments(payment, locked_assessments):
            recalculate_assessment(assessment)
            available = assessment.outstanding
            if available <= ZERO:
                continue
            amount = min(remaining, available)
            PaymentAllocation.objects.create(
                payment=payment,
                assessment=assessment,
                category=PaymentAllocation.Category.TAX,
                amount=amount,
                notes="Director approved: oldest unpaid extraction first.",
                created_by=director,
            )
            remaining -= amount
            if remaining <= ZERO:
                break
        if remaining > ZERO:
            category = (
                PaymentAllocation.Category.CARRYOVER
                if disposition == "oldest_carryover"
                else PaymentAllocation.Category.OVERAGE
            )
            PaymentAllocation.objects.create(
                payment=payment,
                category=category,
                amount=remaining,
                notes=(
                    "Director-approved unallocated credit for future taxes."
                    if category == PaymentAllocation.Category.CARRYOVER
                    else "Director accepted amount above currently unpaid taxes."
                ),
                created_by=director,
            )
    elif disposition == "donation":
        PaymentAllocation.objects.create(
            payment=payment,
            category=PaymentAllocation.Category.DONATION,
            amount=remaining,
            notes="Director classified this payment as a donation.",
            created_by=director,
        )
    elif disposition == "ignore":
        PaymentAllocation.objects.create(
            payment=payment,
            category=PaymentAllocation.Category.IGNORED,
            amount=remaining,
            notes="Director explicitly ignored this payment for moon tax.",
            created_by=director,
        )
    else:
        raise ValueError(f"Unknown payment disposition: {disposition}")

    payment.save(update_fields=("reviewed_by", "reviewed_at", "review_note"))
    return recalculate_payment(payment, director, notify_user=True)
