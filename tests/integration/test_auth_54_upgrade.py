"""Rehearse the Alliance Auth 5.2 -> 5.4 schema transition on MariaDB."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AllianceAuth54UpgradeContract(TransactionTestCase):
    def test_existing_menu_and_announcement_survive_additive_migrations(self):
        if connection.vendor != "mysql":
            self.skipTest("This upgrade contract requires disposable MariaDB.")
        latest = [
            ("admin_status", "0004_alter_applicationannouncement_managers"),
            ("menu", "0003_menuitem_permission_mode_menuitem_permissions"),
        ]
        old = [
            ("admin_status", "0002_alter_applicationannouncement_announcement_hash"),
            ("menu", "0002_alter_menuitem_hook_hash"),
        ]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        old_apps = executor.loader.project_state(old).apps
        MenuItem = old_apps.get_model("menu", "MenuItem")
        Announcement = old_apps.get_model("admin_status", "ApplicationAnnouncement")
        menu = MenuItem._base_manager.create(text="B-UH synthetic upgrade menu")
        announcement = Announcement._base_manager.create(
            application_name="B-UH synthetic upgrade",
            announcement_number=1,
            announcement_text="Preserve this synthetic announcement",
            announcement_url="https://example.invalid/upgrade",
        )
        try:
            MigrationExecutor(connection).migrate(latest)
            from allianceauth.admin_status.models import ApplicationAnnouncement
            from allianceauth.menu.models import MenuItem as CurrentMenuItem

            new_menu = CurrentMenuItem._base_manager.get(pk=menu.pk)
            new_announcement = ApplicationAnnouncement._base_manager.get(
                pk=announcement.pk
            )
            self.assertEqual(new_menu.text, "B-UH synthetic upgrade menu")
            self.assertEqual(new_menu.permission_mode, "any")
            self.assertEqual(new_menu.permissions.count(), 0)
            self.assertEqual(
                new_announcement.announcement_text,
                "Preserve this synthetic announcement",
            )
            MigrationExecutor(connection).migrate(latest)
        finally:
            MigrationExecutor(connection).migrate(latest)
