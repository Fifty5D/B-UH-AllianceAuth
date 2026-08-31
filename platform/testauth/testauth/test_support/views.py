"""Strictly test-only health and authentication endpoints."""

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.core.cache import cache
from django.db import connection
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


def _require_test_support() -> None:
    if not getattr(settings, "BUH_TEST_SUPPORT_ENABLED", False):
        raise Http404


@require_GET
def health(request):
    _require_test_support()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        database_ok = cursor.fetchone()[0] == 1
    cache.set("buh-health", "ok", 10)
    return JsonResponse(
        {"ok": database_ok and cache.get("buh-health") == "ok", "database": database_ok}
    )


@csrf_exempt
@require_POST
def login_as(request):
    _require_test_support()
    role = request.POST.get("role", "")
    username = {
        "director": "test-director",
        "member": "test-member",
        "restricted": "test-restricted",
        "separate": "test-separate",
        "none": "test-no-access",
    }.get(role)
    if username is None:
        return JsonResponse({"ok": False, "error": "unknown role"}, status=400)
    user = get_user_model().objects.get(username=username)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return JsonResponse({"ok": True, "role": role})


@csrf_exempt
@require_POST
def logout_test_user(request):
    _require_test_support()
    logout(request)
    return JsonResponse({"ok": True})
