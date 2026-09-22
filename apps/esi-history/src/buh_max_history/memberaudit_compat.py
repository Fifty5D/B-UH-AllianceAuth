"""Compatibility fixes for the pinned Member Audit integration."""

import logging
from collections.abc import Mapping
from typing import Any

import memberaudit
from memberaudit.core import esi_status

logger = logging.getLogger(__name__)

_SUPPORTED_MEMBERAUDIT_VERSION = "5.0.4"
_PATCH_MARKER = "__buh_memberaudit_esi_status_guard__"
# MetaStatusRoutestatus enums in Member Audit's pinned 2025-12-16 OpenAPI.
_STATUS_METHODS = frozenset({"GET", "POST", "PUT", "DELETE"})
_ROUTE_STATUSES = frozenset({"Unknown", "OK", "Degraded", "Down", "Recovering"})


def _validated_relevant_status(status: Any) -> dict[str, list[dict[str, str]]] | None:
    """Keep only unavailable routes used by Member Audit.

    The ESI status document includes every HTTP method. Member Audit 5.0.4
    constructs its GET/POST-only endpoint type for every unavailable route,
    including unrelated DELETE routes. Validate the response before filtering so
    malformed evidence still causes Member Audit to abort the update.
    """
    if not isinstance(status, Mapping):
        return None
    routes = status.get("routes")
    if not isinstance(routes, list) or not routes:
        return None

    required = {
        (endpoint.method, endpoint.path)
        for endpoints in esi_status._REQUIRED_ENDPOINTS_FOR_SECTIONS.values()
        for endpoint in endpoints
    }
    unavailable: list[dict[str, str]] = []
    for route in routes:
        if not isinstance(route, Mapping):
            return None
        method = route.get("method")
        path = route.get("path")
        route_status = route.get("status")
        if (
            not isinstance(method, str)
            or method not in _STATUS_METHODS
            or not isinstance(route_status, str)
            or route_status not in _ROUTE_STATUSES
            or not isinstance(path, str)
            or not path.startswith("/")
            or any(character.isspace() for character in path)
        ):
            return None
        if route_status == "Down" and (method, path) in required:
            unavailable.append(
                {"method": method, "path": path, "status": route_status}
            )
    return {"routes": unavailable}


def install_memberaudit_esi_status_guard() -> bool:
    """Install the narrow Member Audit 5.0.4 ESI status parser fix once."""
    if memberaudit.__version__ != _SUPPORTED_MEMBERAUDIT_VERSION:
        logger.warning(
            "Member Audit %s is outside the ESI status compatibility guard",
            memberaudit.__version__,
        )
        return False

    current = esi_status._determine_unavailable_sections
    if getattr(current, _PATCH_MARKER, False):
        return False

    def determine_unavailable_sections(status):
        relevant_status = _validated_relevant_status(status)
        if relevant_status is None:
            return None
        if not relevant_status["routes"]:
            return set()
        return current(relevant_status)

    setattr(determine_unavailable_sections, _PATCH_MARKER, True)
    esi_status._determine_unavailable_sections = determine_unavailable_sections
    return True
