from allianceauth import hooks
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.services.hooks import UrlHook

from . import urls
from .access import has_app_access


class HistoryArchiveMenuItem(MenuItemHook):
    def __init__(self):
        super().__init__(
            "ESI History Archive",
            "fa-solid fa-box-archive",
            "buh_max_history:dashboard",
            order=1075,
            navactive=["buh_max_history:"],
        )

    def render(self, request):
        return super().render(request) if has_app_access(request.user) else ""


@hooks.register("menu_item_hook")
def register_menu():
    return HistoryArchiveMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "buh_max_history", r"^esi-archive/")
