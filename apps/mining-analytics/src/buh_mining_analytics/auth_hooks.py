"""Alliance Auth menu and URL hooks."""

from allianceauth import hooks
from allianceauth.menu.hooks import MenuItemHook
from allianceauth.services.hooks import UrlHook

from . import urls
from .access import has_app_access
from .app_settings import APP_NAME


class MiningAnalyticsMenuItem(MenuItemHook):
    """Show the Mining Analytics menu only to authorized users."""

    def __init__(self):
        super().__init__(
            APP_NAME,
            "fa-solid fa-chart-column",
            "buh_mining_analytics:dashboard",
            order=1100,
            navactive=["buh_mining_analytics:"],
        )

    def render(self, request):
        if has_app_access(request.user):
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    """Register the Auth side-menu entry."""

    return MiningAnalyticsMenuItem()


@hooks.register("url_hook")
def register_urls():
    """Register application URLs."""

    return UrlHook(urls, "buh_mining_analytics", r"^mining-analytics/")
