from django.urls import path

from . import views

app_name = "buh_max_history"

urlpatterns = [
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
