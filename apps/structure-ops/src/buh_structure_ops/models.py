"""Local configuration, history, alert, and access models."""

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from . import app_settings


class StructureOpsPermission(models.Model):
    """Unmanaged model used only to create application permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("view_structure_ops", "Can view Structure Operations"),
            ("manage_structure_ops", "Can manage Structure Operations"),
            (
                "admin_structure_ops",
                "Can administer Structure Operations access and setup",
            ),
            ("view_schedule", "Can see the Operations Schedule tab"),
            ("view_schedule_moons", "Can see moon pop timers on the schedule"),
            ("view_schedule_fuel", "Can see fuel timers on the schedule"),
            (
                "view_schedule_structure_timers",
                "Can see reinforcement and structure timers on the schedule",
            ),
            ("view_schedule_wars", "Can see war timers on the schedule"),
            ("view_schedule_custom", "Can see custom schedule events"),
            ("add_schedule_event", "Can add custom schedule events"),
            ("change_schedule_event", "Can edit custom schedule events"),
            ("delete_schedule_event", "Can delete custom schedule events"),
        )


class StructureOpsAccessGroup(Group):
    """Safe admin proxy for editing only Structure Operations permissions."""

    class Meta:
        proxy = True
        default_permissions = ()
        verbose_name = "Structure Operations access group"
        verbose_name_plural = "Structure Operations access groups"


class AccessRoleConfiguration(models.Model):
    """Persistent selection of the existing Auth groups used for default access."""

    singleton_id = models.PositiveSmallIntegerField(
        primary_key=True, default=1, editable=False
    )
    member_group = models.ForeignKey(
        Group,
        verbose_name="Member-default Auth / Discord role",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Existing Auth group that receives the safe member defaults.",
    )
    director_group = models.ForeignKey(
        Group,
        verbose_name="Director-default Auth / Discord role",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Existing Auth group that receives full Structure Operations access.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Structure Operations role settings"
        verbose_name_plural = "Structure Operations role settings"

    def clean(self):
        super().clean()
        if (
            self.member_group_id
            and self.director_group_id
            and self.member_group_id == self.director_group_id
        ):
            from django.core.exceptions import ValidationError

            raise ValidationError(
                "The Member and Director access groups must be different."
            )

    def save(self, *args, **kwargs):
        self.singleton_id = 1
        return super().save(*args, **kwargs)

    def __str__(self):
        member = self.member_group.name if self.member_group_id else "Not selected"
        director = (
            self.director_group.name if self.director_group_id else "Not selected"
        )
        return f"Member: {member} · Director: {director}"


class TrackedCorporation(models.Model):
    """A corporation which must have both structure and refinery authorization."""

    corporation_id = models.PositiveBigIntegerField(unique=True)
    corporation_name = models.CharField(max_length=255)
    is_required = models.BooleanField(default=True)
    is_enabled = models.BooleanField(default=True)
    notes = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("corporation_name",)
        verbose_name = "tracked corporation"
        verbose_name_plural = "tracked corporations"

    def __str__(self):
        return f"{self.corporation_name} ({self.corporation_id})"


class StructurePreference(models.Model):
    """Manager-owned notes and refill target for one EVE structure."""

    structure_id = models.PositiveBigIntegerField(unique=True)
    target_fuel_days = models.PositiveSmallIntegerField(
        default=app_settings.FUEL_TARGET_DAYS,
        validators=[MinValueValidator(7), MaxValueValidator(180)],
    )
    notes = models.TextField(blank=True, default="")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("structure_id",)
        verbose_name = "structure preference"
        verbose_name_plural = "structure preferences"

    def __str__(self):
        return str(self.structure_id)


class ScheduleEvent(models.Model):
    """A manually managed event shown beside live EVE operations data."""

    class Color(models.TextChoices):
        TEAL = "teal", "Teal"
        BLUE = "blue", "Blue"
        VIOLET = "violet", "Violet"
        GOLD = "gold", "Gold"
        ORANGE = "orange", "Orange"
        RED = "red", "Red"
        SLATE = "slate", "Slate"

    title = models.CharField(max_length=140)
    description = models.TextField(blank=True, default="", max_length=4000)
    location = models.CharField(blank=True, default="", max_length=180)
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True, db_index=True)
    all_day = models.BooleanField(default=False)
    color = models.CharField(max_length=12, choices=Color.choices, default=Color.VIOLET)
    link = models.URLField(blank=True, default="", max_length=500)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="buh_schedule_events_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="buh_schedule_events_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("starts_at", "title")
        indexes = [models.Index(fields=("starts_at", "ends_at"))]
        verbose_name = "custom schedule event"
        verbose_name_plural = "custom schedule events"

    def __str__(self):
        return self.title


class StructureSnapshot(models.Model):
    """Point-in-time operational measurements for graphs and trend analysis."""

    structure_id = models.PositiveBigIntegerField(db_index=True)
    corporation_id = models.PositiveBigIntegerField(db_index=True)
    structure_name = models.CharField(max_length=255)
    captured_at = models.DateTimeField(db_index=True)
    fuel_expires_at = models.DateTimeField(null=True, blank=True)
    fuel_quantity = models.BigIntegerField(null=True, blank=True)
    fuel_blocks_per_day = models.PositiveIntegerField(null=True, blank=True)
    state = models.CharField(max_length=80, blank=True, default="")
    service_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("-captured_at",)
        indexes = [models.Index(fields=("structure_id", "-captured_at"))]
        constraints = [
            models.UniqueConstraint(
                fields=("structure_id", "captured_at"),
                name="buh_structure_snapshot_unique",
            )
        ]

    def __str__(self):
        return f"{self.structure_name} @ {self.captured_at}"


class AlertEvent(models.Model):
    """Deduplicated dashboard event and Alliance Auth notification state."""

    class Kind(models.TextChoices):
        FUEL = "fuel", "Fuel"
        EXTRACTION = "extraction", "Extraction"
        ATTACK = "attack", "Attack / reinforcement"
        WAR = "war", "War"
        SYNC = "sync", "Token / synchronization"

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        SUCCESS = "success", "Success"
        WARNING = "warning", "Warning"
        DANGER = "danger", "Critical"

    event_key = models.CharField(max_length=255, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    severity = models.CharField(max_length=12, choices=Severity.choices, db_index=True)
    corporation_id = models.PositiveBigIntegerField(
        null=True, blank=True, db_index=True
    )
    structure_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    title = models.CharField(max_length=254)
    message = models.TextField()
    source_id = models.CharField(max_length=100, blank=True, default="")
    occurred_at = models.DateTimeField(db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    occurrence_count = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    notification_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-occurred_at",)

    def __str__(self):
        return self.title


class WarState(models.Model):
    """Best-known active or ended war state derived from corporation notifications."""

    signature = models.CharField(max_length=255, unique=True)
    corporation_id = models.PositiveBigIntegerField(db_index=True)
    aggressor_id = models.PositiveBigIntegerField(null=True, blank=True)
    defender_id = models.PositiveBigIntegerField(null=True, blank=True)
    war_hq_structure_id = models.PositiveBigIntegerField(null=True, blank=True)
    war_hq_name = models.CharField(max_length=255, blank=True, default="")
    declared_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    last_event_type = models.CharField(max_length=100)
    last_notification_id = models.PositiveBigIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-is_active", "-updated_at")

    def __str__(self):
        return f"{self.corporation_id}: {self.last_event_type}"
