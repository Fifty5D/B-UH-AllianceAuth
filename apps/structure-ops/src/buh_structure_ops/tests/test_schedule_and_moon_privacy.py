"""Granular Moon Mining redaction and operations schedule tests."""

import datetime as dt

from app_utils.testdata_factories import UserMainFactory
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils.timezone import now
from moonmining.models import Extraction
from moonmining.tests.testdata.factories import ExtractionFactory
from structures.tests.testdata.factories import StructureFactory

from buh_structure_ops.models import ScheduleEvent, TrackedCorporation, WarState


def add_permissions(user, *values):
    for value in values:
        app_label, codename = value.split(".", 1)
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        )


class TestMoonMiningPrivacy(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.extraction = ExtractionFactory(
            started_at=now() - dt.timedelta(days=2),
            chunk_arrival_at=now() + dt.timedelta(days=2),
            auto_fracture_at=now() + dt.timedelta(days=2, hours=3),
            status=Extraction.Status.STARTED,
        )
        cls.member = UserMainFactory()
        add_permissions(
            cls.member,
            "moonmining.basic_access",
            "moonmining.extractions_access",
        )
        cls.director = UserMainFactory()
        add_permissions(
            cls.director,
            "moonmining.basic_access",
            "moonmining.extractions_access",
            "moonmining.view_extraction_amounts",
            "moonmining.view_extraction_details",
            "moonmining.view_moon_details",
        )

    def _data(self, user):
        self.client.force_login(user)
        response = self.client.get(
            reverse("moonmining:extractions_data", args=("upcoming",))
        )
        self.assertEqual(response.status_code, 200)
        return response.json()[0]

    def test_member_receives_no_amounts_or_action_buttons(self):
        row = self._data(self.member)
        self.assertIsNone(row["volume"])
        self.assertIsNone(row["value"])
        self.assertIsNone(row["mined_value"])
        self.assertEqual(row["details"], "")

    def test_member_cannot_bypass_hidden_buttons_with_direct_urls(self):
        self.client.force_login(self.member)
        response = self.client.get(
            reverse("moonmining:extraction_details", args=(self.extraction.pk,))
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(
            reverse(
                "moonmining:moon_details", args=(self.extraction.refinery.moon_id,)
            )
        )
        self.assertEqual(response.status_code, 302)

    def test_director_receives_amounts_and_both_actions(self):
        row = self._data(self.director)
        self.assertIsNotNone(row["volume"])
        self.assertIn("modalExtractionDetails", row["details"])
        self.assertIn("modalMoonDetails", row["details"])


class TestOperationsSchedule(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.extraction = ExtractionFactory(
            started_at=now() - dt.timedelta(days=1),
            chunk_arrival_at=now() + dt.timedelta(days=3),
            auto_fracture_at=now() + dt.timedelta(days=3, hours=3),
            status=Extraction.Status.STARTED,
        )
        TrackedCorporation.objects.create(
            corporation_id=cls.extraction.refinery.owner_id,
            corporation_name=cls.extraction.refinery.owner.corporation.corporation_name,
        )
        cls.structure = StructureFactory(
            fuel_expires_at=now() + dt.timedelta(days=5),
            state_timer_end=now() + dt.timedelta(days=7),
        )
        TrackedCorporation.objects.create(
            corporation_id=cls.structure.owner_id,
            corporation_name=cls.structure.owner.corporation.corporation_name,
        )
        WarState.objects.create(
            signature="permission-test-war",
            corporation_id=cls.structure.owner_id,
            declared_at=now() - dt.timedelta(days=1),
            ends_at=now() + dt.timedelta(days=9),
            is_active=True,
            last_event_type="WarDeclared",
        )
        cls.custom = ScheduleEvent.objects.create(
            title="Director planning meeting",
            starts_at=now() + dt.timedelta(days=4),
            color=ScheduleEvent.Color.TEAL,
            created_by=UserMainFactory(),
            updated_by=UserMainFactory(),
        )
        cls.member = UserMainFactory()
        add_permissions(
            cls.member,
            "buh_structure_ops.view_schedule",
            "buh_structure_ops.view_schedule_moons",
        )
        cls.director = UserMainFactory()
        add_permissions(
            cls.director,
            "buh_structure_ops.view_schedule",
            "buh_structure_ops.view_schedule_moons",
            "buh_structure_ops.view_schedule_custom",
            "buh_structure_ops.add_schedule_event",
            "buh_structure_ops.change_schedule_event",
            "buh_structure_ops.delete_schedule_event",
        )
        cls.outsider = UserMainFactory()
        cls.fuel_viewer = UserMainFactory()
        add_permissions(
            cls.fuel_viewer,
            "buh_structure_ops.view_schedule",
            "buh_structure_ops.view_schedule_fuel",
        )
        cls.timer_viewer = UserMainFactory()
        add_permissions(
            cls.timer_viewer,
            "buh_structure_ops.view_schedule",
            "buh_structure_ops.view_schedule_structure_timers",
        )
        cls.war_viewer = UserMainFactory()
        add_permissions(
            cls.war_viewer,
            "buh_structure_ops.view_schedule",
            "buh_structure_ops.view_schedule_wars",
        )

    def _feed(self, user):
        self.client.force_login(user)
        start = (now() - dt.timedelta(days=2)).isoformat()
        end = (now() + dt.timedelta(days=30)).isoformat()
        response = self.client.get(
            reverse("buh_structure_ops:schedule_feed"),
            {"start": start, "end": end},
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["events"]

    def test_schedule_tab_requires_its_own_permission(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("buh_structure_ops:schedule"))
        self.assertEqual(response.status_code, 403)

    def test_member_feed_contains_moons_but_not_custom_events(self):
        events = self._feed(self.member)
        self.assertTrue(any(item["kind"] == "moon" for item in events))
        self.assertFalse(any(item["kind"] == "custom" for item in events))

    def test_director_feed_contains_custom_events_and_edit_controls(self):
        events = self._feed(self.director)
        custom = next(item for item in events if item["kind"] == "custom")
        self.assertEqual(custom["title"], self.custom.title)
        self.assertTrue(custom["updateUrl"])
        self.assertTrue(custom["deleteUrl"])

    def test_live_timer_feeds_are_independently_permissioned(self):
        cases = (
            (self.fuel_viewer, "fuel"),
            (self.timer_viewer, "timer"),
            (self.war_viewer, "war"),
        )
        for user, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                events = self._feed(user)
                self.assertTrue(events)
                self.assertEqual({item["kind"] for item in events}, {expected_kind})

    def test_custom_event_add_update_delete_are_independently_protected(self):
        payload = {
            "title": "Fuel truck arrival",
            "description": "Stage blocks in the structure hangar.",
            "location": "Ainsan",
            "starts_at": (now() + dt.timedelta(days=6)).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": "",
            "color": "gold",
            "link": "",
        }
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("buh_structure_ops:schedule_event_add"), payload
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.director)
        response = self.client.post(
            reverse("buh_structure_ops:schedule_event_add"), payload
        )
        self.assertEqual(response.status_code, 302)
        item = ScheduleEvent.objects.get(title="Fuel truck arrival")
        payload["title"] = "Fuel convoy arrival"
        response = self.client.post(
            reverse("buh_structure_ops:schedule_event_update", args=(item.pk,)),
            payload,
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.title, "Fuel convoy arrival")
        response = self.client.post(
            reverse("buh_structure_ops:schedule_event_delete", args=(item.pk,))
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ScheduleEvent.objects.filter(pk=item.pk).exists())
