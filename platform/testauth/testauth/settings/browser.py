"""Browser profile; identical services plus deterministic test support."""

from .integration import *  # noqa: F403

DEFAULT_THEME = "allianceauth.theme.darkly.auth_hooks.DarklyThemeHook"
INSTALLED_APPS = [*INSTALLED_APPS, "allianceauth.theme.bootstrap"]  # noqa: F405
