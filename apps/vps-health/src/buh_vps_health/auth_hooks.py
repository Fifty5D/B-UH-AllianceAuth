from allianceauth import hooks
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.services.hooks import UrlHook

from . import urls
from .access import has_app_access


class VpsHealthMenuItem(MenuItemHook):
    def __init__(self):
        super().__init__(
            "VPS Health",
            "fa-solid fa-server",
            "buh_vps_health:dashboard",
            order=1080,
            navactive=["buh_vps_health:"],
        )

    def render(self, request):
        return super().render(request) if has_app_access(request.user) else ""


@hooks.register("menu_item_hook")
def register_menu():
    return VpsHealthMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "buh_vps_health", r"^vps-health/")
