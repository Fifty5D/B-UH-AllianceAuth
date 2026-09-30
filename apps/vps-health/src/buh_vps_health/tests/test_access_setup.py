from app_utils.testdata_factories import UserFactory
from buh_structure_ops.models import AccessRoleConfiguration
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase

from buh_vps_health.access import PERMISSION_CODES, has_app_access, permission_map
from buh_vps_health.models import HealthConfiguration


class AccessAndSetupTests(TestCase):
    def setUp(self):
        self.member = Group.objects.create(name="Member")
        self.director = Group.objects.create(name="Director")
        AccessRoleConfiguration.objects.create(
            singleton_id=1,
            member_group=self.member,
            director_group=self.director,
        )

    def test_setup_uses_existing_director_and_grants_every_permission(self):
        call_command("buh_vps_health_setup")
        self.assertTrue(HealthConfiguration.objects.filter(singleton_id=1).exists())
        self.assertEqual(
            self.director.permissions.filter(
                content_type__app_label="buh_vps_health"
            ).count(),
            len(PERMISSION_CODES),
        )

    def test_update_preserves_customized_permission_matrix(self):
        view = Permission.objects.get(
            content_type__app_label="buh_vps_health", codename="view_vps_health"
        )
        self.director.permissions.add(view)
        call_command("buh_vps_health_setup")
        self.assertEqual(
            list(
                self.director.permissions.filter(
                    content_type__app_label="buh_vps_health"
                ).values_list("codename", flat=True)
            ),
            ["view_vps_health"],
        )
        call_command("buh_vps_health_setup", repair_default_access=True)
        self.assertEqual(
            self.director.permissions.filter(
                content_type__app_label="buh_vps_health"
            ).count(),
            len(PERMISSION_CODES),
        )

    def test_update_can_grant_only_new_update_permissions(self):
        view = Permission.objects.get(
            content_type__app_label="buh_vps_health", codename="view_vps_health"
        )
        self.director.permissions.add(view)
        call_command("buh_vps_health_setup", grant_update_access=True)
        self.assertEqual(
            set(
                self.director.permissions.filter(
                    content_type__app_label="buh_vps_health"
                ).values_list("codename", flat=True)
            ),
            {"view_vps_health", "check_updates", "view_update_results"},
        )

    def test_permissions_are_independently_visible(self):
        user = UserFactory(permissions=["buh_vps_health.view_vps_health"])
        self.assertTrue(has_app_access(user))
        matrix = permission_map(user)
        self.assertTrue(matrix["view_vps_health"])
        self.assertFalse(matrix["restart_auth_services"])
        self.assertFalse(matrix["view_host_metrics"])
