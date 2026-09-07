"""Skip Discord nickname updates that Discord cannot perform for the guild owner."""

from __future__ import annotations

import logging
import re
from functools import wraps
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


logger = logging.getLogger(__name__)
_OWNER_SETTING = "BUH_DISCORD_GUILD_OWNER_ID"
_GUARD_ATTRIBUTE = "__buh_discord_guild_owner_nickname_guard__"
_DISCORD_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_MAX_DISCORD_ID = (1 << 64) - 1


def _configured_owner_id() -> int | None:
    raw = getattr(settings, _OWNER_SETTING, None)
    if raw is None or raw == "":
        return None
    encoded = str(raw)
    if _DISCORD_ID_RE.fullmatch(encoded) is None:
        raise ImproperlyConfigured(
            f"{_OWNER_SETTING} must be a positive decimal Discord user ID"
        )
    owner_id = int(encoded)
    if owner_id > _MAX_DISCORD_ID:
        raise ImproperlyConfigured(
            f"{_OWNER_SETTING} must fit in an unsigned 64-bit Discord snowflake"
        )
    return owner_id


def install_discord_owner_nickname_guard(discord_user_type: type[Any] | None = None) -> bool:
    """Guard Alliance Auth's nickname method for one configured Discord owner ID.

    The upstream Discord service still owns scheduling, retries, role updates, and
    username synchronization. Returning success for the guild owner prevents an
    impossible nickname mutation from being retried or mistaken for account removal.
    """

    owner_id = _configured_owner_id()
    if owner_id is None:
        return False
    if discord_user_type is None:
        from allianceauth.services.modules.discord.models import DiscordUser

        discord_user_type = DiscordUser

    original = discord_user_type.update_nickname
    if getattr(original, _GUARD_ATTRIBUTE, None) == owner_id:
        return False
    if getattr(original, _GUARD_ATTRIBUTE, None) is not None:
        raise ImproperlyConfigured(
            f"{_OWNER_SETTING} changed after the Discord nickname guard was installed"
        )

    @wraps(original)
    def update_nickname(discord_user, nickname: str | None = None):
        if int(discord_user.uid) == owner_id:
            logger.warning(
                "Skipping Discord nickname update for guild owner %s; "
                "Discord does not permit guild-owner nickname changes",
                discord_user.user,
            )
            return True
        return original(discord_user, nickname=nickname)

    setattr(update_nickname, _GUARD_ATTRIBUTE, owner_id)
    discord_user_type.update_nickname = update_nickname
    return True
