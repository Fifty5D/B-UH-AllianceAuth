from django.urls import path

from . import views

urlpatterns = [
    path("health/", views.health, name="buh_test_health"),
    path("login/", views.login_as, name="buh_test_login"),
    path("logout/", views.logout_test_user, name="buh_test_logout"),
]
