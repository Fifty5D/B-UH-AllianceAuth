from app_utils.testdata_factories import UserFactory
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse


class AccessAdminTests(TestCase):
    def test_access_editor_preserves_unrelated_permissions(self):
        administrator = UserFactory(is_staff=True, is_superuser=True)
        member = UserFactory()
        group = Group.objects.create(name="Operations")
        unrelated = Permission.objects.exclude(
            content_type__app_label="buh_vps_health"
        ).first()
        view = Permission.objects.get(
            content_type__app_label="buh_vps_health", codename="view_vps_health"
        )
        group.permissions.add(unrelated)
        self.client.force_login(administrator)
        response = self.client.post(
            reverse(
                "admin:buh_vps_health_vpshealthaccessgroup_change", args=(group.pk,)
            ),
            {
                "name": group.name,
                "adopt_reserved_role": "",
                "members": [member.pk],
                "health_permissions": [view.pk],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(group.permissions.filter(pk=unrelated.pk).exists())
        self.assertTrue(group.permissions.filter(pk=view.pk).exists())
        self.assertTrue(group.user_set.filter(pk=member.pk).exists())
