"""Moon Tax URL routes."""

from django.urls import path

from . import views

app_name = "buh_moon_tax"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("periods/<int:period_id>/", views.period_detail, name="period"),
    path("people/accounts/<int:user_id>/", views.member_detail, name="member_detail"),
    path(
        "people/characters/<int:character_id>/",
        views.character_detail,
        name="character_detail",
    ),
    path(
        "people/accounts/<int:user_id>/billing/",
        views.save_billing_preference,
        name="save_billing_preference",
    ),
    path("payments/", views.payment_review, name="payments"),
    path("payments/decide/", views.decide_payment_legacy, name="decide_payment"),
    path(
        "payments/<int:payment_id>/decide/",
        views.decide_payment_view,
        name="decide_payment_direct",
    ),
    path(
        "payments/bulk-decide/",
        views.bulk_decide_payments,
        name="bulk_decide_payments",
    ),
    path("audit/run/", views.run_audit, name="run_audit"),
    path("audit/<int:audit_id>/status/", views.audit_status, name="audit_status"),
    path("preview/", views.member_preview_picker, name="member_preview"),
    path("preview/start/", views.start_member_preview, name="start_member_preview"),
    path("preview/stop/", views.stop_member_preview, name="stop_member_preview"),
    path("policy/", views.policy_workspace, name="policy"),
    path("policy/defaults/", views.save_policy_defaults, name="save_policy_defaults"),
    path("policy/structure/", views.save_structure_policy, name="save_structure_policy"),
    path(
        "assessments/<int:assessment_id>/adjust/",
        views.adjust_assessment,
        name="adjust_assessment",
    ),
    path(
        "adjustments/<int:adjustment_id>/reverse/",
        views.reverse_adjustment,
        name="reverse_adjustment",
    ),
    path("export.csv", views.export_csv, name="export_csv"),
]
