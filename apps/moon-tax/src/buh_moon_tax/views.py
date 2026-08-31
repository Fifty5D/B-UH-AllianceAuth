"""Permission-filtered Moon Tax dashboards and director workflow endpoints."""

from __future__ import annotations

import csv
from functools import wraps

from allianceauth.authentication.models import CharacterOwnership
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import (
    Case,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    Max,
    Q,
    Sum,
    Value,
    When,
)
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import localdate, now
from django.views.decorators.http import require_GET, require_POST

from . import __version__, app_settings
from .access import has, has_app_access, require, visibility
from .forms import (
    AdjustmentReversalForm,
    BillAdjustmentForm,
    BillingPreferenceForm,
    BulkPaymentDecisionForm,
    PaymentDecisionForm,
    PaymentFilterForm,
    PolicyDefaultsForm,
    StructurePolicyWorkspaceForm,
)
from .models import (
    ZERO,
    AssessmentAdjustment,
    AuditRun,
    BillingPreference,
    MiningLine,
    PaymentCandidate,
    PaymentRecipient,
    StructureTaxPolicy,
    TaxAssessment,
    TaxConfiguration,
    TaxExemption,
    TaxPeriod,
)
from .payments import (
    create_assessment_adjustment,
    decide_payment,
    reverse_assessment_adjustment,
)
from .tasks import ACTIVE_STATUSES, create_audit, expire_stale_audits, refresh_sources

PAYMENT_DECISION_MAP = {
    "OLDEST_CARRY": "oldest_carryover",
    "OLDEST_OVERAGE": "oldest_overage",
    "DONATION": "donation",
    "IGNORE": "ignore",
    "REJECT": "reject",
    "RESET_PENDING": "pending",
}

PREVIEW_SESSION_KEY = "buh_moon_tax_preview_user_id"


def app_access_required(view_func):
    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not has_app_access(request.user):
            raise PermissionDenied("You do not have access to Moon Tax.")
        return view_func(request, *args, **kwargs)

    return wrapped


def _preview_user(request: HttpRequest):
    """Return the selected read-only member POV for an authorized operator."""

    if not has(request.user, "preview_member_view"):
        request.session.pop(PREVIEW_SESSION_KEY, None)
        return None
    user_id = request.session.get(PREVIEW_SESSION_KEY)
    if not user_id:
        return None
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None or user.pk == request.user.pk:
        request.session.pop(PREVIEW_SESSION_KEY, None)
        return None
    return user


def _display_user(request: HttpRequest):
    return _preview_user(request) or request.user


def _page_context(request: HttpRequest) -> dict:
    preview = _preview_user(request)
    display_user = preview or request.user
    return {
        "app_version": __version__,
        "permissions": visibility(display_user),
        "operator_permissions": visibility(request.user),
        "member_preview": preview,
        "display_user": display_user,
    }


def _block_preview_mutation(request: HttpRequest) -> None:
    if _preview_user(request) is not None:
        raise PermissionDenied(
            "Member preview is read-only. Exit preview before making changes."
        )


def _assessment_scope(user):
    queryset = TaxAssessment.objects.select_related("period", "auth_user")
    if has(user, "view_all_tax"):
        if not has(user, "view_unlinked_miners"):
            queryset = queryset.filter(auth_user__isnull=False)
        return queryset
    if has(user, "view_own_tax"):
        return queryset.filter(auth_user=user)
    return queryset.none()


def _payment_scope(user):
    queryset = PaymentCandidate.objects.select_related(
        "auth_user",
        "auth_user__profile",
        "auth_user__profile__main_character",
        "reviewed_by",
    )
    if has(user, "review_payments") or has(user, "view_all_tax"):
        return queryset
    if has(user, "view_payment_evidence"):
        return queryset.filter(auth_user=user)
    return queryset.none()


def _reviewable_payment_scope(user):
    """System-excluded evidence can be viewed, but not manually reallocated."""

    return _payment_scope(user).exclude(status=PaymentCandidate.Status.EXEMPT)


def _payment_return_url(request: HttpRequest) -> str:
    """Return only to the local payment screen after a decision."""

    fallback = reverse("buh_moon_tax:payments")
    target = request.POST.get("return_to", "")
    if (
        target.startswith(fallback)
        and url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return target
    return fallback


def _decorate_payments(payments):
    """Attach stable UI labels without making templates traverse nullable profiles."""

    for payment in payments:
        user = payment.auth_user
        main_character = None
        if user is not None:
            profile = getattr(user, "profile", None)
            main_character = getattr(profile, "main_character", None)
        if main_character is not None:
            payment.member_label = main_character.character_name
            payment.member_secondary = user.username
            payment.member_key = f"user-{user.pk}"
        elif user is not None:
            payment.member_label = user.username
            payment.member_secondary = "Linked Auth account"
            payment.member_key = f"user-{user.pk}"
        else:
            payment.member_label = payment.payer_character_name or "Unlinked payer"
            payment.member_secondary = "Not linked to an Auth account"
            payment.member_key = f"character-{payment.payer_character_id or payment.pk}"
    return payments


def _outstanding_sum_expression():
    """Sum positive bill balances without letting overpayments cancel debt."""

    money = DecimalField(max_digits=30, decimal_places=2)
    balance = ExpressionWrapper(
        F("tax_due") - F("approved_paid"),
        output_field=money,
    )
    return Sum(
        Case(
            When(tax_due__gt=F("approved_paid"), then=balance),
            default=Value(ZERO, output_field=money),
            output_field=money,
        ),
        output_field=money,
    )


def _assessment_totals(assessments) -> dict:
    totals = assessments.aggregate(
        bills=Count("pk"),
        gross=Sum("gross_value"),
        tax=Sum("tax_due"),
        paid=Sum("approved_paid"),
        outstanding=_outstanding_sum_expression(),
    )
    for key in ("gross", "tax", "paid", "outstanding"):
        totals[key] = totals[key] or ZERO
    return totals


def _profile_url(auth_user_id: int | None, character_id: int | None) -> str | None:
    if auth_user_id:
        return reverse("buh_moon_tax:member_detail", args=(auth_user_id,))
    if character_id:
        return reverse("buh_moon_tax:character_detail", args=(character_id,))
    return None


def _billing_account_totals(assessments) -> list[dict]:
    """Group every visible extraction bill by its preserved billing identity."""

    rows = list(
        assessments.values(
            "billing_key",
            "auth_user_id",
            "combined_characters",
        )
        .annotate(
            character_id=Max("character_id"),
            display_name=Max("display_name"),
            bills=Count("pk"),
            gross=Sum("gross_value"),
            tax=Sum("tax_due"),
            paid=Sum("approved_paid"),
            outstanding=_outstanding_sum_expression(),
        )
        .order_by("-outstanding", "display_name")
    )
    for row in rows:
        for key in ("gross", "tax", "paid", "outstanding"):
            row[key] = row[key] or ZERO
        row["billing_label"] = (
            "Combined account"
            if row["combined_characters"]
            else "Individual character"
        )
        row["profile_url"] = _profile_url(
            row["auth_user_id"],
            row["character_id"],
        )
    return rows


def _periods_with_visible_totals(assessments, *, limit=12):
    """Attach totals from the caller's permission-filtered assessment scope."""

    periods = list(
        TaxPeriod.objects.filter(assessments__in=assessments)
        .distinct()
        .order_by("-pop_at")[:limit]
    )
    if not periods:
        return periods
    totals = {
        row["period_id"]: row
        for row in assessments.filter(period_id__in=[item.pk for item in periods])
        .values("period_id")
        .annotate(
            visible_gross=Sum("gross_value"),
            visible_tax=Sum("tax_due"),
            visible_paid=Sum("approved_paid"),
            visible_outstanding=_outstanding_sum_expression(),
        )
    }
    for period in periods:
        row = totals.get(period.pk, {})
        period.visible_gross = row.get("visible_gross") or ZERO
        period.visible_tax = row.get("visible_tax") or ZERO
        period.visible_paid = row.get("visible_paid") or ZERO
        period.visible_outstanding = row.get("visible_outstanding") or ZERO
    return periods


def _assessment_profile_url(assessment: TaxAssessment) -> str | None:
    return _profile_url(assessment.auth_user_id, assessment.character_id)


def _decorate_assessments(assessments, config=None):
    """Attach navigation and adjustment state used by every bill workspace."""

    threshold = config.small_balance_threshold if config else ZERO
    for assessment in assessments:
        assessment.profile_url = _assessment_profile_url(assessment)
        assessment.active_adjustments = [
            item for item in assessment.adjustments.all() if item.reversed_at is None
        ]
        assessment.can_quick_waive = (
            assessment.outstanding > ZERO and assessment.outstanding <= threshold
        )
    return assessments


def _assessment_return_url(request: HttpRequest, assessment: TaxAssessment) -> str:
    """Return only to this bill's overview, period, or person workspace."""

    fallback = reverse("buh_moon_tax:period", args=(assessment.period_id,))
    allowed = {
        reverse("buh_moon_tax:dashboard"),
        fallback,
    }
    profile_url = _assessment_profile_url(assessment)
    if profile_url:
        allowed.add(profile_url)
    target = request.POST.get("return_to", "")
    if (
        target in allowed
        and url_has_allowed_host_and_scheme(
            target,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return target
    return fallback


@app_access_required
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    page = _page_context(request)
    display_user = page["display_user"]
    assessments = _assessment_scope(display_user)
    totals = _assessment_totals(assessments)
    billing_accounts = _billing_account_totals(assessments)
    pending_payments = _payment_scope(display_user).filter(
        status=PaymentCandidate.Status.PENDING
    )
    latest_audit = (
        AuditRun.objects.order_by("-queued_at").first()
        if has(display_user, "run_tax_audit") or has(display_user, "view_all_tax")
        else None
    )
    periods = _periods_with_visible_totals(assessments)
    config = TaxConfiguration.objects.filter(singleton_id=1).first()
    assessment_rows = _decorate_assessments(
        list(
            assessments.prefetch_related(
                "adjustments__created_by", "adjustments__reversed_by"
            ).order_by("-period__pop_at", "display_name")[:25]
        ),
        config,
    )
    context = {
        "app_name": app_settings.APP_NAME,
        "app_version": __version__,
        **page,
        "totals": totals,
        "billing_accounts": billing_accounts,
        "periods": periods,
        "assessments": assessment_rows,
        "pending_payments": pending_payments.order_by("occurred_at")[:20],
        "pending_payment_count": pending_payments.count(),
        "latest_audit": latest_audit,
        "config": config,
    }
    return render(request, "buh_moon_tax/dashboard.html", context)


@app_access_required
@require_GET
def period_detail(request: HttpRequest, period_id: int) -> HttpResponse:
    page = _page_context(request)
    display_user = page["display_user"]
    period = get_object_or_404(TaxPeriod, pk=period_id)
    assessments = _assessment_scope(display_user).filter(period=period)
    if not assessments.exists():
        raise Http404
    lines = MiningLine.objects.filter(period=period)
    if not has(display_user, "view_all_tax"):
        lines = lines.filter(auth_user=display_user)
    elif not has(display_user, "view_unlinked_miners"):
        lines = lines.filter(auth_user__isnull=False)
    visible_totals = _assessment_totals(assessments)
    config = TaxConfiguration.objects.filter(singleton_id=1).first()
    assessment_rows = _decorate_assessments(
        list(
            assessments.prefetch_related(
                "adjustments__created_by", "adjustments__reversed_by"
            )
        ),
        config,
    )
    context = {
        "app_name": app_settings.APP_NAME,
        **page,
        "period": period,
        "assessments": assessment_rows,
        "visible_totals": visible_totals,
        "config": config,
        "lines": lines.select_related("auth_user").order_by(
            "character_name", "raw_type_name", "ledger_day"
        ),
    }
    return render(request, "buh_moon_tax/period_detail.html", context)


def _profile_totals(assessments) -> dict:
    """Aggregate every visible bill without loading the full history into memory."""

    return _assessment_totals(assessments)


def _decorate_exemptions(exemptions, *, account_label=None, characters=()):
    """Attach an explicit target and date-aware state for the person workspace."""

    today = localdate()
    character_names = {
        item.character_id: item.character_name for item in characters
    }
    states = {
        "current": ("Current", "exempt"),
        "future": ("Future", "open"),
        "expired": ("Expired", "waived"),
        "inactive": ("Inactive", ""),
    }
    for exemption in exemptions:
        if exemption.auth_user_id:
            exemption.target_label = f"Entire Auth account · {account_label}"
        else:
            character_name = (
                character_names.get(exemption.character_id)
                or exemption.character_name
                or f"EVE ID {exemption.character_id}"
            )
            exemption.target_label = f"Character · {character_name}"

        if not exemption.active:
            state = "inactive"
        elif exemption.effective_from > today:
            state = "future"
        elif exemption.effective_until and exemption.effective_until < today:
            state = "expired"
        else:
            state = "current"
        exemption.effective_state = state
        exemption.effective_state_label, exemption.effective_state_class = states[state]
    return exemptions


def _render_person_detail(
    request: HttpRequest,
    *,
    target_user=None,
    character_id: int | None = None,
) -> HttpResponse:
    page = _page_context(request)
    display_user = page["display_user"]
    assessments = _assessment_scope(display_user)
    if target_user is not None:
        assessments = assessments.filter(auth_user=target_user)
    else:
        assessments = assessments.filter(
            auth_user__isnull=True,
            character_id=character_id,
        )
    if not assessments.exists():
        raise Http404

    config = TaxConfiguration.objects.filter(singleton_id=1).first()
    totals = _profile_totals(assessments)
    ordered_assessments = assessments.prefetch_related(
        "adjustments__created_by", "adjustments__reversed_by"
    ).order_by("-period__pop_at", "display_name")
    bill_paginator = Paginator(ordered_assessments, 25)
    bill_page_obj = bill_paginator.get_page(request.GET.get("bills_page"))
    assessment_rows = _decorate_assessments(
        list(bill_page_obj.object_list),
        config,
    )

    if target_user is not None:
        main_character = getattr(
            getattr(target_user, "profile", None), "main_character", None
        )
        subject_name = (
            main_character.character_name if main_character else target_user.username
        )
        subject_secondary = f"Auth account · {target_user.username}"
        characters = [
            ownership.character
            for ownership in CharacterOwnership.objects.filter(
                user=target_user
            ).select_related("character")
        ]
        if main_character and all(
            item.character_id != main_character.character_id for item in characters
        ):
            characters.insert(0, main_character)
        character_ids = [item.character_id for item in characters]
        exemption_scope = Q(auth_user=target_user)
        if character_ids:
            exemption_scope |= Q(
                auth_user__isnull=True,
                character_id__in=character_ids,
            )
        exemptions = _decorate_exemptions(
            list(
                TaxExemption.objects.filter(exemption_scope).order_by(
                    "-effective_from", "-pk"
                )
            ),
            account_label=subject_name,
            characters=characters,
        )
        preference = BillingPreference.objects.filter(user=target_user).first()
        combine_characters = (
            preference.combine_characters
            if preference is not None
            else (config.combine_characters_by_default if config else True)
        )
        profile_kind = "account"
        payment_filter = Q(auth_user=target_user)
    else:
        main_character = None
        subject_name = assessment_rows[0].display_name
        subject_secondary = f"Unlinked character · EVE ID {character_id}"
        characters = []
        exemptions = _decorate_exemptions(
            list(
                TaxExemption.objects.filter(
                    auth_user__isnull=True,
                    character_id=character_id,
                ).order_by("-effective_from", "-pk")
            )
        )
        preference = None
        combine_characters = False
        profile_kind = "character"
        payment_filter = Q(auth_user__isnull=True, payer_character_id=character_id)

    recent_payments = []
    if has(display_user, "view_payment_evidence"):
        recent_payments = _decorate_payments(
            list(
                _payment_scope(display_user)
                .filter(payment_filter)
                .order_by("-occurred_at")[:20]
            )
        )

    return render(
        request,
        "buh_moon_tax/person_detail.html",
        {
            "app_name": app_settings.APP_NAME,
            **page,
            "profile_kind": profile_kind,
            "target_user": target_user,
            "target_character_id": character_id,
            "subject_name": subject_name,
            "subject_secondary": subject_secondary,
            "main_character": main_character,
            "characters": characters,
            "assessments": assessment_rows,
            "bill_page_obj": bill_page_obj,
            "totals": totals,
            "recent_payments": recent_payments,
            "exemptions": exemptions,
            "billing_preference": preference,
            "billing_mode": (
                BillingPreferenceForm.Mode.DEFAULT
                if preference is None
                else (
                    BillingPreferenceForm.Mode.COMBINED
                    if preference.combine_characters
                    else BillingPreferenceForm.Mode.SEPARATE
                )
            ),
            "combine_characters": combine_characters,
            "site_default_combine": (
                config.combine_characters_by_default if config else True
            ),
            "config": config,
        },
    )


@app_access_required
@require_GET
def member_detail(request: HttpRequest, user_id: int) -> HttpResponse:
    target_user = get_object_or_404(
        get_user_model().objects.select_related("profile__main_character"),
        pk=user_id,
    )
    return _render_person_detail(request, target_user=target_user)


@app_access_required
@require_GET
def character_detail(request: HttpRequest, character_id: int) -> HttpResponse:
    return _render_person_detail(request, character_id=character_id)


@app_access_required
@require_POST
def save_billing_preference(request: HttpRequest, user_id: int) -> HttpResponse:
    require(request.user, "manage_tax_policy")
    _block_preview_mutation(request)
    target_user = get_object_or_404(get_user_model(), pk=user_id)
    if not _assessment_scope(request.user).filter(auth_user=target_user).exists():
        raise Http404
    form = BillingPreferenceForm(request.POST)
    if not form.is_valid():
        messages.error(
            request,
            "Choose the site default, combined, or separate-character billing.",
        )
        return redirect("buh_moon_tax:member_detail", user_id=user_id)
    mode = form.cleaned_data["mode"]
    if mode == BillingPreferenceForm.Mode.DEFAULT:
        BillingPreference.objects.filter(user=target_user).delete()
        messages.success(
            request,
            "Custom billing mode removed. This account now follows the site default; "
            "the effective mode is applied on the next audit.",
        )
        return redirect("buh_moon_tax:member_detail", user_id=user_id)

    combined = mode == BillingPreferenceForm.Mode.COMBINED
    BillingPreference.objects.update_or_create(
        user=target_user,
        defaults={
            "combine_characters": combined,
            "updated_by": request.user,
        },
    )
    messages.success(
        request,
        "Billing mode saved. It applies on the next audit; periods with approved "
        "payments or Director adjustments keep their historical mode.",
    )
    return redirect("buh_moon_tax:member_detail", user_id=user_id)


@app_access_required
@require_GET
def payment_review(request: HttpRequest) -> HttpResponse:
    page = _page_context(request)
    display_user = page["display_user"]
    if not has(display_user, "view_payment_evidence"):
        raise PermissionDenied("This member view cannot see payment evidence.")
    payment_scope = _payment_scope(display_user)
    status_counts = {
        row["status"]: row["count"]
        for row in payment_scope.values("status").annotate(count=Count("pk"))
    }
    recipient_choices = list(
        payment_scope.order_by("recipient_name", "recipient_id")
        .values_list("recipient_id", "recipient_name")
        .distinct()
    )
    payments = payment_scope.prefetch_related("allocations__assessment__period")
    status = request.GET.get("status", "PENDING")
    valid_statuses = {choice for choice, _ in PaymentCandidate.Status.choices}
    if status in valid_statuses:
        payments = payments.filter(status=status)
    elif status != "ALL":
        status = "PENDING"
        payments = payments.filter(status=status)

    filter_form = PaymentFilterForm(
        request.GET or None,
        recipient_choices=recipient_choices,
    )
    sort = "oldest"
    per_page = 50
    if filter_form.is_valid():
        cleaned = filter_form.cleaned_data
        search = (cleaned.get("q") or "").strip()
        if search:
            payments = payments.filter(
                Q(payer_character_name__icontains=search)
                | Q(auth_user__username__icontains=search)
                | Q(
                    auth_user__profile__main_character__character_name__icontains=search
                )
                | Q(recipient_name__icontains=search)
                | Q(reference__icontains=search)
            )
        if cleaned.get("source"):
            payments = payments.filter(source=cleaned["source"])
        if cleaned.get("recipient"):
            payments = payments.filter(recipient_id=cleaned["recipient"])
        if cleaned.get("min_amount") is not None:
            payments = payments.filter(amount__gte=cleaned["min_amount"])
        if cleaned.get("max_amount") is not None:
            payments = payments.filter(amount__lte=cleaned["max_amount"])
        if cleaned.get("date_from"):
            payments = payments.filter(occurred_at__date__gte=cleaned["date_from"])
        if cleaned.get("date_to"):
            payments = payments.filter(occurred_at__date__lte=cleaned["date_to"])
        sort = cleaned.get("sort") or "oldest"
        per_page = int(cleaned.get("per_page") or 50)

    orderings = {
        "oldest": ("occurred_at", "pk"),
        "newest": ("-occurred_at", "-pk"),
        "highest": ("-amount", "occurred_at", "pk"),
        "lowest": ("amount", "occurred_at", "pk"),
        "member": ("payer_character_name", "occurred_at", "pk"),
    }
    payments = payments.order_by(*orderings.get(sort, orderings["oldest"]))
    summary = payments.aggregate(
        count=Count("pk"),
        total=Sum("amount"),
        linked_members=Count("auth_user", distinct=True),
        unlinked_members=Count(
            "payer_character_id",
            filter=Q(auth_user__isnull=True),
            distinct=True,
        ),
    )
    summary["total"] = summary["total"] or ZERO
    summary["members"] = (
        summary.pop("linked_members") + summary.pop("unlinked_members")
    )

    paginator = Paginator(payments, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_payments = _decorate_payments(list(page_obj.object_list))

    status_params = request.GET.copy()
    status_params.pop("page", None)
    status_links = []
    all_count = sum(status_counts.values())
    for value, label in (("ALL", "All"), *PaymentCandidate.Status.choices):
        status_params["status"] = value
        status_links.append(
            {
                "value": value,
                "label": label,
                "count": all_count if value == "ALL" else status_counts.get(value, 0),
                "query": status_params.urlencode(),
            }
        )

    pagination_params = request.GET.copy()
    pagination_params.pop("page", None)
    context = {
        "app_name": app_settings.APP_NAME,
        "app_version": __version__,
        **page,
        "payments": page_payments,
        "page_obj": page_obj,
        "summary": summary,
        "active_status": status,
        "status_links": status_links,
        "filter_form": filter_form,
        "pagination_query": pagination_params.urlencode(),
        "has_active_filters": any(
            request.GET.get(key)
            for key in (
                "q",
                "source",
                "recipient",
                "min_amount",
                "max_amount",
                "date_from",
                "date_to",
            )
        ),
    }
    return render(request, "buh_moon_tax/payments.html", context)


def _payment_json_requested(request: HttpRequest) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@app_access_required
@require_POST
def decide_payment_view(request: HttpRequest, payment_id: int) -> HttpResponse:
    require(request.user, "review_payments")
    _block_preview_mutation(request)
    posted = request.POST.copy()
    posted["payment_id"] = str(payment_id)
    form = PaymentDecisionForm(
        posted,
        payment_queryset=_reviewable_payment_scope(request.user),
    )
    if not form.is_valid():
        if _payment_json_requested(request):
            return JsonResponse(
                {"ok": False, "message": "Payment decision was not saved.", "errors": form.errors},
                status=400,
            )
        messages.error(
            request, "Payment decision was not saved. Refresh and try again."
        )
        return redirect(_payment_return_url(request))
    payment = form.cleaned_data["payment_id"]
    decision = form.cleaned_data["decision"]
    try:
        decide_payment(
            payment,
            disposition=PAYMENT_DECISION_MAP[decision],
            director=request.user,
            note=form.cleaned_data["note"],
        )
    except ValueError as error:
        # The service re-locks the payment after form validation. A concurrent
        # audit may have exempted it in that small gap; report the conflict
        # cleanly instead of returning a 500 or applying a stale decision.
        message = str(error)
        if _payment_json_requested(request):
            return JsonResponse(
                {"ok": False, "message": message},
                status=409,
            )
        messages.error(request, message)
        return redirect(_payment_return_url(request))
    messages.success(
        request,
        f"{dict(PaymentDecisionForm.Decision.choices)[decision]}: "
        f"{payment.amount:,.2f} ISK from "
        f"{payment.payer_character_name or 'the payer'}.",
    )
    if _payment_json_requested(request):
        payment.refresh_from_db()
        return JsonResponse(
            {
                "ok": True,
                "payment_id": payment.pk,
                "status": payment.status,
                "status_label": payment.get_status_display(),
                "message": (
                    f"{dict(PaymentDecisionForm.Decision.choices)[decision]}: "
                    f"{payment.amount:,.2f} ISK from "
                    f"{payment.payer_character_name or 'the payer'}."
                ),
                "redirect": _payment_return_url(request),
            }
        )
    return redirect(_payment_return_url(request))


@app_access_required
@require_POST
def decide_payment_legacy(request: HttpRequest) -> HttpResponse:
    """Backward-compatible route for bookmarks and the pre-v0.3 form."""

    try:
        payment_id = int(request.POST.get("payment_id", ""))
    except (TypeError, ValueError):
        messages.error(request, "Choose a payment and try again.")
        return redirect(_payment_return_url(request))
    return decide_payment_view(request, payment_id)


@app_access_required
@require_POST
def bulk_decide_payments(request: HttpRequest) -> HttpResponse:
    require(request.user, "review_payments")
    _block_preview_mutation(request)
    payment_scope = _reviewable_payment_scope(request.user)
    form = BulkPaymentDecisionForm(
        request.POST,
        payment_queryset=payment_scope,
    )
    if not form.is_valid():
        messages.error(
            request,
            "No payments were changed. Select at least one visible payment and try again.",
        )
        return redirect(_payment_return_url(request))

    selected_ids = list(
        form.cleaned_data["payment_ids"].values_list("pk", flat=True)
    )
    decision = form.cleaned_data["decision"]
    note = form.cleaned_data["note"]
    try:
        with transaction.atomic():
            selected = list(
                payment_scope.select_for_update()
                .filter(pk__in=selected_ids)
                .order_by("occurred_at", "pk")
            )
            total = sum((payment.amount for payment in selected), ZERO)
            for payment in selected:
                decide_payment(
                    payment,
                    disposition=PAYMENT_DECISION_MAP[decision],
                    director=request.user,
                    note=note,
                )
    except ValueError as error:
        messages.error(
            request,
            f"No payments were changed: {error}",
        )
        return redirect(_payment_return_url(request))
    messages.success(
        request,
        f"{dict(PaymentDecisionForm.Decision.choices)[decision]} for "
        f"{len(selected)} payment(s), totaling {total:,.2f} ISK.",
    )
    return redirect(_payment_return_url(request))


@app_access_required
@require_POST
def run_audit(request: HttpRequest) -> HttpResponse:
    require(request.user, "run_tax_audit")
    _block_preview_mutation(request)
    expired = expire_stale_audits()
    if expired:
        messages.warning(
            request,
            f"Released {expired} stale audit run(s) left behind by an earlier worker restart.",
        )
    active = AuditRun.objects.filter(
        status__in=ACTIVE_STATUSES
    ).order_by("-queued_at").first()
    if active:
        messages.info(request, f"Audit #{active.pk} is already {active.get_status_display().lower()}.")
        return redirect("buh_moon_tax:dashboard")
    audit = create_audit(AuditRun.Trigger.MANUAL, request.user)
    refresh_sources.delay(audit.pk)
    messages.success(
        request,
        f"Audit #{audit.pk} is queued. Source refreshes run first; the page will show its result.",
    )
    return redirect("buh_moon_tax:dashboard")


@app_access_required
@require_GET
def audit_status(request: HttpRequest, audit_id: int) -> JsonResponse:
    require(_display_user(request), "run_tax_audit")
    audit = get_object_or_404(AuditRun, pk=audit_id)
    return JsonResponse(
        {
            "id": audit.pk,
            "status": audit.status,
            "status_label": audit.get_status_display(),
            "summary": audit.summary,
            "warnings": audit.warnings,
            "error": audit.error,
            "finished_at": audit.finished_at.isoformat() if audit.finished_at else None,
        }
    )


@app_access_required
@require_GET
def member_preview_picker(request: HttpRequest) -> HttpResponse:
    require(request.user, "preview_member_view")
    query = (request.GET.get("q") or "").strip()
    users = get_user_model().objects.filter(is_active=True).exclude(pk=request.user.pk)
    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(profile__main_character__character_name__icontains=query)
        )
    else:
        users = users.none()
    results = list(users.select_related("profile__main_character").order_by("username")[:40])
    for user in results:
        main = getattr(getattr(user, "profile", None), "main_character", None)
        user.preview_label = main.character_name if main else user.username
        user.preview_secondary = user.username if main else "No main character"
        user.preview_has_access = has_app_access(user)
    return render(
        request,
        "buh_moon_tax/member_preview.html",
        {
            "app_name": app_settings.APP_NAME,
            **_page_context(request),
            "query": query,
            "results": results,
        },
    )


@app_access_required
@require_POST
def start_member_preview(request: HttpRequest) -> HttpResponse:
    require(request.user, "preview_member_view")
    user = get_object_or_404(
        get_user_model().objects.filter(is_active=True),
        pk=request.POST.get("user_id"),
    )
    if user.pk == request.user.pk:
        messages.info(request, "That is already your current view.")
        return redirect("buh_moon_tax:dashboard")
    request.session[PREVIEW_SESSION_KEY] = user.pk
    messages.info(request, f"Read-only member preview started for {user.username}.")
    return redirect("buh_moon_tax:dashboard")


@app_access_required
@require_POST
def stop_member_preview(request: HttpRequest) -> HttpResponse:
    request.session.pop(PREVIEW_SESSION_KEY, None)
    messages.success(request, "Member preview ended. Director controls are restored.")
    return redirect("buh_moon_tax:dashboard")


def _known_structures():
    structures = {}
    for row in TaxPeriod.objects.order_by("-pop_at").values(
        "structure_id",
        "structure_name",
        "corporation_id",
        "corporation_name",
    ):
        structures.setdefault(row["structure_id"], row)
    for row in StructureTaxPolicy.objects.order_by("-effective_from").values(
        "structure_id",
        "structure_name",
        "corporation_id",
        "corporation_name",
    ):
        structures.setdefault(row["structure_id"], row)
    return sorted(structures.values(), key=lambda item: item["structure_name"].lower())


@app_access_required
@require_GET
def policy_workspace(request: HttpRequest) -> HttpResponse:
    require(request.user, "manage_tax_policy")
    _block_preview_mutation(request)
    config, _ = TaxConfiguration.objects.get_or_create(singleton_id=1)
    current_time = now()
    structures = _known_structures()
    policies = {}
    for item in (
        StructureTaxPolicy.objects.filter(
            enabled=True,
            effective_from__lte=current_time,
        )
        .filter(Q(effective_until__isnull=True) | Q(effective_until__gt=current_time))
        .prefetch_related("payment_recipients")
        .order_by("structure_id", "-effective_from", "-pk")
    ):
        policies.setdefault(item.structure_id, item)
    for structure in structures:
        policy = policies.get(structure["structure_id"])
        structure["policy"] = policy
        structure["tax_rate"] = policy.tax_rate if policy else config.default_tax_rate
        structure["recipient_ids"] = (
            {item.pk for item in policy.payment_recipients.all()} if policy else set()
        )
        structure["inherits_recipients"] = not structure["recipient_ids"]
    return render(
        request,
        "buh_moon_tax/policy.html",
        {
            "app_name": app_settings.APP_NAME,
            **_page_context(request),
            "config": config,
            "defaults_form": PolicyDefaultsForm(instance=config),
            "structures": structures,
            "recipients": PaymentRecipient.objects.filter(enabled=True).order_by("kind", "name"),
        },
    )


@app_access_required
@require_POST
def save_policy_defaults(request: HttpRequest) -> HttpResponse:
    require(request.user, "manage_tax_policy")
    _block_preview_mutation(request)
    config, _ = TaxConfiguration.objects.get_or_create(singleton_id=1)
    form = PolicyDefaultsForm(request.POST, instance=config)
    if not form.is_valid():
        messages.error(request, "Default policy was not saved. Check the highlighted values.")
        return redirect("buh_moon_tax:policy")
    config = form.save(commit=False)
    config.updated_by = request.user
    config.save()
    form.save_m2m()
    messages.success(request, "Default tax rate, recipients, and waiver shortcut were saved.")
    return redirect("buh_moon_tax:policy")


@app_access_required
@require_POST
def save_structure_policy(request: HttpRequest) -> HttpResponse:
    require(request.user, "manage_tax_policy")
    _block_preview_mutation(request)
    form = StructurePolicyWorkspaceForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Athanor policy was not saved. Check the rate and recipients.")
        return redirect("buh_moon_tax:policy")
    structure_id = form.cleaned_data["structure_id"]
    known = {item["structure_id"]: item for item in _known_structures()}
    structure = known.get(structure_id)
    if not structure:
        raise Http404("Unknown Athanor")
    effective_from = now()
    with transaction.atomic():
        current_policies = list(
            StructureTaxPolicy.objects.select_for_update()
            .filter(
                structure_id=structure_id,
                enabled=True,
                effective_from__lte=effective_from,
            )
            .filter(
                Q(effective_until__isnull=True)
                | Q(effective_until__gt=effective_from)
            )
            .order_by("-effective_from", "-pk")
        )
        for current in current_policies:
            current.effective_until = effective_from
            current.save(update_fields=("effective_until", "updated_at"))
        policy = StructureTaxPolicy.objects.create(
            **structure,
            tax_rate=form.cleaned_data["tax_rate"],
            effective_from=effective_from,
            notes=form.cleaned_data["notes"],
            created_by=request.user,
        )
        if not form.cleaned_data["inherit_recipients"]:
            policy.payment_recipients.set(form.cleaned_data["payment_recipients"])
    messages.success(
        request,
        f"{structure['structure_name']} now uses {policy.tax_rate}% and the selected payment destinations. Existing extraction snapshots were preserved.",
    )
    return redirect("buh_moon_tax:policy")


@app_access_required
@require_POST
def adjust_assessment(request: HttpRequest, assessment_id: int) -> HttpResponse:
    require(request.user, "manage_bill_adjustments")
    _block_preview_mutation(request)
    assessment = get_object_or_404(
        _assessment_scope(request.user), pk=assessment_id
    )
    form = BillAdjustmentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Bill was not changed. Enter a reason and valid amount.")
        return redirect(_assessment_return_url(request, assessment))
    try:
        adjustment = create_assessment_adjustment(
            assessment,
            action=form.cleaned_data["action"],
            amount=form.cleaned_data.get("amount"),
            reason=form.cleaned_data["reason"],
            director=request.user,
        )
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f"Saved {adjustment.get_kind_display().lower()} of {abs(adjustment.amount):,.2f} ISK for {assessment.display_name}.",
        )
    return redirect(_assessment_return_url(request, assessment))


@app_access_required
@require_POST
def reverse_adjustment(request: HttpRequest, adjustment_id: int) -> HttpResponse:
    require(request.user, "manage_bill_adjustments")
    _block_preview_mutation(request)
    visible_assessments = _assessment_scope(request.user).values("pk")
    adjustment = get_object_or_404(
        AssessmentAdjustment.objects.select_related("assessment__period").filter(
            assessment_id__in=visible_assessments
        ),
        pk=adjustment_id,
    )
    form = AdjustmentReversalForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a reason before reversing an adjustment.")
    else:
        try:
            reverse_assessment_adjustment(
                adjustment,
                reason=form.cleaned_data["reason"],
                director=request.user,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "The bill adjustment was reversed and totals were recalculated.")
    return redirect(_assessment_return_url(request, adjustment.assessment))


def _csv_safe(value):
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


@app_access_required
@require_GET
def export_csv(request: HttpRequest) -> HttpResponse:
    display_user = _display_user(request)
    require(display_user, "export_tax_data")
    permissions = visibility(display_user)
    assessments = _assessment_scope(display_user)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="buh-moon-tax.csv"'
    writer = csv.writer(response)
    header = ["Period", "Payer / account", "Status", "Due (UTC)"]
    if permissions["view_mining_values"]:
        header += ["Mined value (ISK)", "Tax due (ISK)", "Approved paid (ISK)", "Outstanding (ISK)"]
    writer.writerow(header)
    for assessment in assessments.order_by("period__pop_at", "display_name"):
        row = [
            _csv_safe(str(assessment.period)),
            _csv_safe(assessment.display_name),
            assessment.get_status_display(),
            assessment.due_at.isoformat(),
        ]
        if permissions["view_mining_values"]:
            row += [
                assessment.gross_value,
                assessment.tax_due,
                assessment.approved_paid,
                assessment.outstanding,
            ]
        writer.writerow(row)
    return response
