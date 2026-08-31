"""URLs exposed only by the disposable source-based Auth environment."""

from allianceauth import urls as allianceauth_urls
from django.urls import include, path

urlpatterns = [
    path("__test__/", include("testauth.test_support.urls")),
    path("", include(allianceauth_urls)),
]

handler500 = "allianceauth.views.Generic500Redirect"
handler404 = "allianceauth.views.Generic404Redirect"
handler403 = "allianceauth.views.Generic403Redirect"
handler400 = "allianceauth.views.Generic400Redirect"
