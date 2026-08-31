"""Access-level and dedicated admin integration tests."""

from allianceauth.groupmanagement.models import ReservedGroupName
from app_utils.testdata_factories import UserFactory
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from buh_structure_ops.access import (
    has_admin_access,
    has_manager_access,
    has_view_access,
)
from buh_structure_ops.models import AccessRoleConfiguration


class TestAccessLevels(TestCase):
    def test_viewer_manager_and_admin_are_strictly_layered(self):
        viewer = UserFactory(permissions=["buh_structure_ops.view_structure_ops"])
        manager = UserFactory(permissions=["buh_structure_ops.manage_structure_ops"])
        administrator = UserFactory(
            permissions=["buh_structure_ops.admin_structure_ops"]
        )
        self.assertTrue(has_view_access(viewer))
        self.assertFalse(has_manager_access(viewer))
        self.assertTrue(has_view_access(manager))
        self.assertTrue(has_manager_access(manager))
        self.assertFalse(has_admin_access(manager))
        self.assertTrue(has_view_access(administrator))
        self.assertTrue(has_manager_access(administrator))
        self.assertTrue(has_admin_access(administrator))

    def test_setup_command_configures_member_director_and_removes_legacy_group(self):
        legacy = Group.objects.create(name="Structure Operations Administrators")
        legacy_admin = UserFactory()
        legacy.user_set.add(legacy_admin)
        legacy.permissions.add(
            Permission.objects.get(
                content_type__app_label="buh_structure_ops",
                codename="admin_structure_ops",
            )
        )
        call_command("buh_structure_ops_setup", admin_character="")
        self.assertFalse(Group.objects.filter(pk=legacy.pk).exists())
        member = Group.objects.get(name="Member")
        director = Group.objects.get(name="Director")
        self.assertTrue(member.user_set.filter(pk=legacy_admin.pk).exists())
        self.assertTrue(director.user_set.filter(pk=legacy_admin.pk).exists())
        self.assertTrue(
            director.permissions.filter(
                content_type__app_label="buh_structure_ops",
                codename="admin_structure_ops",
            ).exists()
        )
        self.assertTrue(
            member.permissions.filter(
                content_type__app_label="moonmining", codename="basic_access"
            ).exists()
        )
        self.assertFalse(
            member.permissions.filter(
                content_type__app_label="moonmining",
                codename="view_extraction_amounts",
            ).exists()
        )

    def test_setup_preserves_a_repurposed_legacy_group(self):
        legacy = Group.objects.create(name="Structure Operations Managers")
        unrelated = Permission.objects.exclude(
            content_type__app_label__in=(
                "buh_structure_ops",
                "structures",
                "moonmining",
            )
        ).first()
        legacy.permissions.add(
            unrelated,
            Permission.objects.get(
                content_type__app_label="buh_structure_ops",
                codename="manage_structure_ops",
            ),
        )

        call_command("buh_structure_ops_setup", admin_character="")

        legacy.refresh_from_db()
        self.assertTrue(legacy.permissions.filter(pk=unrelated.pk).exists())
        self.assertFalse(
            legacy.permissions.filter(
                content_type__app_label="buh_structure_ops"
            ).exists()
        )

    def test_setup_adopts_reserved_director_role_and_repairs_suffix(self):
        ReservedGroupName.objects.create(
            name="Director", reason="Existing Discord role", created_by="Fifty5D"
        )
        accidental_alias = Group.objects.create(name="Director")
        self.assertEqual(accidental_alias.name, "Director_1")
        director_user = UserFactory()
        accidental_alias.user_set.add(director_user)
        accidental_alias.permissions.add(
            Permission.objects.get(
                content_type__app_label="buh_structure_ops",
                codename="admin_structure_ops",
            )
        )

        call_command("buh_structure_ops_setup", admin_character="")

        accidental_alias.refresh_from_db()
        self.assertEqual(accidental_alias.name, "Director")
        self.assertFalse(
            ReservedGroupName.objects.filter(name__iexact="Director").exists()
        )
        self.assertFalse(Group.objects.filter(name__iexact="Director_1").exists())
        self.assertTrue(accidental_alias.user_set.filter(pk=director_user.pk).exists())
        config = AccessRoleConfiguration.objects.get(singleton_id=1)
        self.assertEqual(config.director_group_id, accidental_alias.pk)


class TestAccessGroupAdmin(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = UserFactory(is_staff=True, is_superuser=True)
        cls.staff = UserFactory(is_staff=True)
        cls.group = Group.objects.create(name="Structure Team")
        cls.viewer = Permission.objects.get(
            content_type__app_label="buh_structure_ops",
            codename="view_structure_ops",
        )
        cls.manager = Permission.objects.get(
            content_type__app_label="buh_structure_ops",
            codename="manage_structure_ops",
        )
        cls.unrelated = Permission.objects.exclude(
            content_type__app_label="buh_structure_ops"
        ).first()
        cls.group.permissions.add(cls.viewer, cls.unrelated)

    def test_admin_section_is_visible_and_scoped(self):
        self.client.force_login(self.superuser)
        response = self.client.get(
            reverse(
                "admin:buh_structure_ops_structureopsaccessgroup_change",
                args=(self.group.pk,),
            )
        )
        self.assertEqual(response.status_code, 200)
        queryset = response.context["adminform"].form.fields["ops_permissions"].queryset
        self.assertEqual(
            set(queryset.values_list("content_type__app_label", flat=True)),
            {"buh_structure_ops"},
        )
        self.assertIn("members", response.context["adminform"].form.fields)
        self.assertIn("name", response.context["adminform"].form.fields)
        self.assertIn(
            "adopt_reserved_role", response.context["adminform"].form.fields
        )
        self.assertIn(
            "navigation_permissions", response.context["adminform"].form.fields
        )
        self.assertIn("moon_permissions", response.context["adminform"].form.fields)
        self.assertIn(
            "schedule_permissions", response.context["adminform"].form.fields
        )

    def test_saving_preserves_unrelated_permissions(self):
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse(
                "admin:buh_structure_ops_structureopsaccessgroup_change",
                args=(self.group.pk,),
            ),
            {
                "name": self.group.name,
                "adopt_reserved_role": "",
                "members": [],
                "ops_permissions": [self.manager.pk],
                "navigation_permissions": [],
                "moon_permissions": [],
                "schedule_permissions": [],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.group.permissions.filter(pk=self.unrelated.pk).exists())
        self.assertEqual(
            set(
                self.group.permissions.filter(
                    content_type__app_label="buh_structure_ops"
                ).values_list("codename", flat=True)
            ),
            {"manage_structure_ops"},
        )

    def test_admin_can_add_and_remove_people_directly(self):
        member = UserFactory()
        self.client.force_login(self.superuser)
        url = reverse(
            "admin:buh_structure_ops_structureopsaccessgroup_change",
            args=(self.group.pk,),
        )
        response = self.client.post(
            url,
            {
                "name": self.group.name,
                "adopt_reserved_role": "",
                "members": [member.pk],
                "ops_permissions": [self.viewer.pk],
                "navigation_permissions": [],
                "moon_permissions": [],
                "schedule_permissions": [],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.group.user_set.filter(pk=member.pk).exists())

        response = self.client.post(
            url,
            {
                "name": self.group.name,
                "adopt_reserved_role": "",
                "members": [],
                "ops_permissions": [self.viewer.pk],
                "navigation_permissions": [],
                "moon_permissions": [],
                "schedule_permissions": [],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.group.user_set.filter(pk=member.pk).exists())

    def test_admin_can_rename_group_and_adopt_existing_discord_role(self):
        ReservedGroupName.objects.create(
            name="Director", reason="Existing Discord role", created_by="Fifty5D"
        )
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse(
                "admin:buh_structure_ops_structureopsaccessgroup_change",
                args=(self.group.pk,),
            ),
            {
                "name": "Director",
                "adopt_reserved_role": "on",
                "members": [],
                "ops_permissions": [self.viewer.pk],
                "navigation_permissions": [],
                "moon_permissions": [],
                "schedule_permissions": [],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Director")
        self.assertFalse(
            ReservedGroupName.objects.filter(name__iexact="Director").exists()
        )

    def test_admin_can_select_existing_groups_for_member_and_director_defaults(self):
        members = Group.objects.create(name="Line Members")
        directors = Group.objects.create(name="Leadership")
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("admin:buh_structure_ops_accessroleconfiguration_add"),
            {
                "member_group": members.pk,
                "director_group": directors.pk,
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        config = AccessRoleConfiguration.objects.get(singleton_id=1)
        self.assertEqual(config.member_group, members)
        self.assertEqual(config.director_group, directors)
        self.assertTrue(
            members.permissions.filter(
                content_type__app_label="buh_structure_ops",
                codename="view_schedule_moons",
            ).exists()
        )
        self.assertTrue(
            directors.permissions.filter(
                content_type__app_label="buh_structure_ops",
                codename="admin_structure_ops",
            ).exists()
        )

    def test_custom_role_name_persists_through_future_setup_runs(self):
        members = Group.objects.create(name="Line Members")
        self.client.force_login(self.superuser)
        response = self.client.post(
            reverse("admin:buh_structure_ops_accessroleconfiguration_add"),
            {
                "member_group": members.pk,
                "director_group": self.group.pk,
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            reverse(
                "admin:buh_structure_ops_structureopsaccessgroup_change",
                args=(self.group.pk,),
            ),
            {
                "name": "Leadership",
                "adopt_reserved_role": "",
                "members": [],
                "ops_permissions": [],
                "navigation_permissions": [],
                "moon_permissions": [],
                "schedule_permissions": [],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)

        call_command("buh_structure_ops_setup", admin_character="")

        config = AccessRoleConfiguration.objects.get(singleton_id=1)
        self.assertEqual(config.director_group_id, self.group.pk)
        self.assertEqual(config.director_group.name, "Leadership")
        self.assertFalse(Group.objects.filter(name="Director").exists())
        self.assertTrue(
            config.director_group.permissions.filter(
                content_type__app_label="buh_structure_ops",
                codename="admin_structure_ops",
            ).exists()
        )

    def test_plain_staff_cannot_manage_access(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("admin:buh_structure_ops_structureopsaccessgroup_changelist")
        )
        self.assertEqual(response.status_code, 403)
