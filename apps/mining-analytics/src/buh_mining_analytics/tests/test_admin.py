"""Tests for the dedicated Mining Analytics Django admin section."""

from app_utils.testdata_factories import UserFactory
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse


class TestMiningAccessGroupAdmin(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = UserFactory(is_staff=True, is_superuser=True)
        cls.group = Group.objects.create(name="Member")
        cls.basic = Permission.objects.get(
            content_type__app_label="buh_mining_analytics",
            codename="basic_access",
        )
        cls.corporation = Permission.objects.get(
            content_type__app_label="buh_mining_analytics",
            codename="view_corporation",
        )
        cls.export = Permission.objects.get(
            content_type__app_label="buh_mining_analytics",
            codename="export_data",
        )
        cls.unrelated = Permission.objects.exclude(
            content_type__app_label="buh_mining_analytics"
        ).first()
        cls.group.permissions.add(cls.basic, cls.unrelated)

    def setUp(self):
        self.client.force_login(self.superuser)

    def test_admin_index_contains_mining_access_section(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "B-UH Mining Analytics")
        self.assertContains(response, "Mining Analytics access groups")

    def test_change_form_only_offers_mining_permissions(self):
        response = self.client.get(
            reverse(
                "admin:buh_mining_analytics_miningaccessgroup_change",
                args=(self.group.pk,),
            )
        )
        self.assertEqual(response.status_code, 200)
        queryset = response.context["adminform"].form.fields[
            "mining_permissions"
        ].queryset
        self.assertTrue(queryset.exists())
        self.assertEqual(
            set(queryset.values_list("content_type__app_label", flat=True)),
            {"buh_mining_analytics"},
        )
        self.assertEqual(
            set(queryset.values_list("codename", flat=True)),
            {
                "basic_access",
                "view_corporation",
                "view_all",
                "export_data",
                "manage_access",
            },
        )

    def test_saving_preserves_unrelated_group_permissions(self):
        response = self.client.post(
            reverse(
                "admin:buh_mining_analytics_miningaccessgroup_change",
                args=(self.group.pk,),
            ),
            {
                "name": self.group.name,
                "mining_permissions": [self.corporation.pk, self.export.pk],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        codes = set(
            self.group.permissions.filter(
                content_type__app_label="buh_mining_analytics"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(codes, {"view_corporation", "export_data"})
        self.assertTrue(self.group.permissions.filter(pk=self.unrelated.pk).exists())

    def test_non_manager_cannot_open_admin_section(self):
        staff = UserFactory(is_staff=True, is_superuser=False)
        self.client.force_login(staff)
        response = self.client.get(
            reverse("admin:buh_mining_analytics_miningaccessgroup_changelist")
        )
        self.assertEqual(response.status_code, 403)

    def test_manage_access_permission_allows_staff_admin(self):
        staff = UserFactory(is_staff=True, is_superuser=False)
        permission = Permission.objects.get(
            content_type__app_label="buh_mining_analytics",
            codename="manage_access",
        )
        staff.user_permissions.add(permission)
        self.client.force_login(staff)
        response = self.client.get(
            reverse("admin:buh_mining_analytics_miningaccessgroup_changelist")
        )
        self.assertEqual(response.status_code, 200)
