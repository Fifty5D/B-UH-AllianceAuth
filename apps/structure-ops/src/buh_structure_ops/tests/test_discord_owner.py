"""Tests for the Discord guild-owner nickname exclusion."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from buh_structure_ops.discord_owner import install_discord_owner_nickname_guard


class DiscordOwnerNicknameGuardTests(SimpleTestCase):
    @override_settings(BUH_DISCORD_GUILD_OWNER_ID="123456789012345678")
    def test_guild_owner_nickname_is_visibly_skipped(self):
        calls = []

        class DiscordUser:
            uid = 123456789012345678
            user = "owner"

            def update_nickname(self, nickname=None):
                calls.append(nickname)
                return False

        self.assertTrue(install_discord_owner_nickname_guard(DiscordUser))
        with self.assertLogs("buh_structure_ops.discord_owner", level="WARNING") as logs:
            self.assertTrue(DiscordUser().update_nickname("Owner Character"))
        self.assertEqual(calls, [])
        self.assertIn("Skipping Discord nickname update for guild owner owner", logs.output[0])

    @override_settings(BUH_DISCORD_GUILD_OWNER_ID=123456789012345678)
    def test_ordinary_member_keeps_upstream_nickname_and_error_handling(self):
        failure = ConnectionError("transient Discord failure")

        class DiscordUser:
            uid = 223456789012345678
            user = "member"

            def update_nickname(self, nickname=None):
                raise failure

            def update_groups(self):
                return "groups-unchanged"

        original_groups = DiscordUser.update_groups
        self.assertTrue(install_discord_owner_nickname_guard(DiscordUser))
        with self.assertRaises(ConnectionError) as raised:
            DiscordUser().update_nickname("Member Character")
        self.assertIs(raised.exception, failure)
        self.assertIs(DiscordUser.update_groups, original_groups)
        self.assertEqual(DiscordUser().update_groups(), "groups-unchanged")

    @override_settings(BUH_DISCORD_GUILD_OWNER_ID=None)
    def test_missing_owner_setting_leaves_nickname_method_unchanged(self):
        class DiscordUser:
            def update_nickname(self, nickname=None):
                return nickname

        original = DiscordUser.update_nickname
        self.assertFalse(install_discord_owner_nickname_guard(DiscordUser))
        self.assertIs(DiscordUser.update_nickname, original)

    @override_settings(BUH_DISCORD_GUILD_OWNER_ID="not-a-discord-id")
    def test_invalid_owner_setting_fails_closed(self):
        with self.assertRaisesRegex(
            ImproperlyConfigured,
            "BUH_DISCORD_GUILD_OWNER_ID must be a positive decimal Discord user ID",
        ):
            install_discord_owner_nickname_guard(object)
