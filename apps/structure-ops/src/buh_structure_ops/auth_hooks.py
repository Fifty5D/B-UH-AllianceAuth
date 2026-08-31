"""Alliance Auth menu and URL hooks."""

from allianceauth import hooks
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.services.hooks import UrlHook

from . import urls
from .access import has_schedule_access, has_view_access
from .app_settings import APP_NAME


class StructureOpsMenuItem(MenuItemHook):
    def __init__(self):
        super().__init__(
            APP_NAME,
            "fa-solid fa-industry",
            "buh_structure_ops:dashboard",
            order=1050,
            navactive=["buh_structure_ops:"],
        )

    def render(self, request):
        return super().render(request) if has_view_access(request.user) else ""


class StructureOpsScheduleMenuItem(MenuItemHook):
    def __init__(self):
        super().__init__(
            "Operations Schedule",
            "fa-solid fa-calendar-days",
            "buh_structure_ops:schedule",
            order=1051,
            navactive=["buh_structure_ops:schedule"],
        )

    def render(self, request):
        if has_schedule_access(request.user) and not has_view_access(request.user):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return StructureOpsMenuItem()


@hooks.register("menu_item_hook")
def register_schedule_menu():
    return StructureOpsScheduleMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "buh_structure_ops", r"^structure-operations/")
