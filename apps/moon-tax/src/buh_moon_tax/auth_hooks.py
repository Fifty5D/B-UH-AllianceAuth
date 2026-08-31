"""Alliance Auth menu and URL hooks."""

from allianceauth import hooks
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.services.hooks import UrlHook

from . import urls
from .access import has_app_access
from .app_settings import APP_NAME


class MoonTaxMenuItem(MenuItemHook):
    def __init__(self):
        super().__init__(
            APP_NAME,
            "fa-solid fa-coins",
            "buh_moon_tax:dashboard",
            order=1060,
            navactive=["buh_moon_tax:"],
        )

    def render(self, request):
        return super().render(request) if has_app_access(request.user) else ""


@hooks.register("menu_item_hook")
def register_menu():
    return MoonTaxMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "buh_moon_tax", r"^moon-tax/")
