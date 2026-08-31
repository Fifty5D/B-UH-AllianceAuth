"""URL routes for Structure Operations."""

from django.urls import path

from . import views

app_name = "buh_structure_ops"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("schedule/", views.schedule, name="schedule"),
    path("schedule/feed.json", views.schedule_feed, name="schedule_feed"),
    path("schedule/events/add/", views.schedule_event_add, name="schedule_event_add"),
    path(
        "schedule/events/<int:event_id>/update/",
        views.schedule_event_update,
        name="schedule_event_update",
    ),
    path(
        "schedule/events/<int:event_id>/delete/",
        views.schedule_event_delete,
        name="schedule_event_delete",
    ),
    path("refresh/", views.refresh_now, name="refresh_now"),
    path(
        "alerts/<int:alert_id>/acknowledge/",
        views.acknowledge_alert,
        name="acknowledge_alert",
    ),
    path(
        "structures/<int:structure_id>/preferences/",
        views.update_preference,
        name="update_preference",
    ),
    path(
        "setup/corporations/add/",
        views.add_tracked_corporation,
        name="add_tracked_corporation",
    ),
    path(
        "setup/corporations/<int:corporation_id>/remove/",
        views.remove_tracked_corporation,
        name="remove_tracked_corporation",
    ),
    path("export.csv", views.export_csv, name="export_csv"),
]
