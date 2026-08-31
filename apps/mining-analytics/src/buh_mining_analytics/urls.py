"""URL routes for Mining Analytics."""

from django.urls import path

from . import views

app_name = "buh_mining_analytics"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("api/options/", views.options_api, name="options_api"),
    path("api/data/", views.data_api, name="data_api"),
    path("api/refresh/", views.refresh_api, name="refresh_api"),
    path("export.csv", views.export_csv, name="export_csv"),
]
