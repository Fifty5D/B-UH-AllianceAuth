from django.urls import path

from . import history_views, views

app_name = "buh_max_history"

urlpatterns = [
    path("history/", history_views.history, name="history"),
    path("public-history/", history_views.public_history, name="public_history"),
    path(
        "public-revisions/<int:revision_id>/download/",
        history_views.download_revision,
        name="download_revision",
    ),
    path("", views.dashboard, name="dashboard"),
    path("jobs/start/<str:kind>/", views.start_job, name="start_job"),
    path("jobs/<uuid:job_id>/", views.job_detail, name="job_detail"),
    path(
        "snapshots/<int:snapshot_id>/download/",
        views.download_snapshot,
        name="download_snapshot",
    ),
    path(
        "public-files/<int:file_id>/download/",
        views.download_public_file,
        name="download_public_file",
    ),
]
