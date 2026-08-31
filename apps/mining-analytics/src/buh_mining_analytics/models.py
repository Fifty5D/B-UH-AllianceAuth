"""Permission and admin proxy models for the application."""

from django.contrib.auth.models import Group
from django.db import models


class MiningAnalyticsPermission(models.Model):
    """Unmanaged model used only to create app permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            (
                "basic_access",
                "Can access Mining Analytics and view own characters",
            ),
            (
                "view_corporation",
                "Can view Mining Analytics for members of the same corporation",
            ),
            (
                "view_all",
                "Can view Mining Analytics for all registered characters",
            ),
            (
                "export_data",
                "Can export Mining Analytics data within allowed scope",
            ),
            (
                "manage_access",
                "Can manage Mining Analytics access groups in Django admin",
            ),
        )


class MiningAccessGroup(Group):
    """Safe proxy used to manage only Mining Analytics group permissions."""

    class Meta:
        proxy = True
        default_permissions = ()
        verbose_name = "Mining Analytics access group"
        verbose_name_plural = "Mining Analytics access groups"
