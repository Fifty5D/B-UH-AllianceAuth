from django.urls import path

from . import views

app_name = "buh_vps_health"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/live/", views.live_metrics, name="live_metrics"),
    path("api/history/", views.history, name="history"),
    path("actions/restart-auth/", views.restart_auth, name="restart_auth"),
    path("actions/set-workers/", views.set_workers, name="set_workers"),
    path("actions/check-updates/", views.check_updates, name="check_updates"),
    path("actions/<uuid:action_id>/", views.action_detail, name="action_detail"),
]
