"""Regression tests for compatibility with the pinned Member Audit release."""

from contextlib import ExitStack
from unittest.mock import patch

from django.test import override_settings

from app_utils.testing import NoSocketsTestCase
from memberaudit import tasks
from memberaudit.core import esi_status
from memberaudit.models import Character
from memberaudit.tests.testdata.factories_2 import CharacterFactory

from buh_max_history.memberaudit_compat import install_memberaudit_esi_status_guard


class MemberAuditStatusParserTests(NoSocketsTestCase):
    def test_required_down_get_and_post_routes_remain_unavailable(self):
        sections = esi_status._determine_unavailable_sections(
            {
                "routes": [
                    {
                        "method": "GET",
                        "path": "/characters/{character_id}/loyalty/points",
                        "status": "Down",
                    },
                    {
                        "method": "POST",
                        "path": "/characters/{character_id}/assets/names",
                        "status": "Down",
                    },
                ]
            }
        )

        self.assertEqual(
            sections,
            {Character.UpdateSection.LOYALTY, Character.UpdateSection.ASSETS},
        )

    def test_malformed_route_fails_closed(self):
        malformed_routes = [
            {"method": "GET", "status": "Down"},
            {"method": "BREW", "path": "/characters/{character_id}", "status": "Down"},
            {"method": "GET", "path": "/characters/{character_id}", "status": "Dwon"},
            {"method": "GET", "path": "relative", "status": "Down"},
            {"method": [], "path": "/characters/{character_id}", "status": "Down"},
            {"method": "GET", "path": "/characters/{character id}", "status": "Down"},
        ]

        for route in malformed_routes:
            with self.subTest(route=route):
                sections = esi_status._determine_unavailable_sections(
                    {"routes": [route]}
                )

                self.assertIsNone(sections)

    def test_installation_is_idempotent(self):
        installed = esi_status._determine_unavailable_sections

        self.assertFalse(install_memberaudit_esi_status_guard())
        self.assertIs(esi_status._determine_unavailable_sections, installed)

    def test_unsupported_memberaudit_version_is_not_patched(self):
        def future_parser(status):
            return status

        with (
            patch.object(esi_status, "_determine_unavailable_sections", future_parser),
            patch(
                "buh_max_history.memberaudit_compat.memberaudit.__version__", "5.0.5"
            ),
        ):
            self.assertFalse(install_memberaudit_esi_status_guard())
            self.assertIs(esi_status._determine_unavailable_sections, future_parser)


@override_settings(CELERY_ALWAYS_EAGER=True, CELERY_EAGER_PROPAGATES_EXCEPTIONS=True)
class MemberAuditUpdateDispatchTests(NoSocketsTestCase):
    def tearDown(self):
        esi_status.clear_cache()
        super().tearDown()

    def test_unrelated_down_delete_does_not_abort_all_section_updates(self):
        character = CharacterFactory()
        status = {
            "routes": [
                {
                    "method": "DELETE",
                    "path": "/characters/{character_id}/fittings/{fitting_id}",
                    "status": "Down",
                }
            ]
        }

        with (
            patch.object(esi_status, "_fetch_status", return_value=status),
            patch.object(
                esi_status,
                "unavailable_sections",
                side_effect=esi_status._fetch_unavailable_sections,
            ),
            ExitStack() as stack,
        ):
            section_tasks = [
                stack.enter_context(
                    patch.object(tasks, f"update_character_{section.value}")
                )
                for section in Character.UpdateSection.enabled_sections()
            ]
            result = tasks.update_character(character.pk, ignore_stale=True)

        self.assertTrue(result)
        self.assertTrue(section_tasks)
        for section_task in section_tasks:
            section_task.apply_async.assert_called_once()

    def test_malformed_status_aborts_before_section_updates_are_queued(self):
        character = CharacterFactory()
        malformed_routes = [
            {
                "method": [],
                "path": "/characters/{character_id}/fittings/{fitting_id}",
                "status": "Down",
            },
            {
                "method": "DELETE",
                "path": "/characters/{character_id}/fittings/{fitting_id}",
                "status": {},
            },
        ]

        for route in malformed_routes:
            with self.subTest(route=route):
                with (
                    patch.object(
                        esi_status, "_fetch_status", return_value={"routes": [route]}
                    ),
                    patch.object(
                        esi_status,
                        "unavailable_sections",
                        side_effect=esi_status._fetch_unavailable_sections,
                    ),
                    ExitStack() as stack,
                ):
                    section_tasks = [
                        stack.enter_context(
                            patch.object(tasks, f"update_character_{section.value}")
                        )
                        for section in Character.UpdateSection.enabled_sections()
                    ]
                    result = tasks.update_character(character.pk, ignore_stale=True)

                self.assertFalse(result)
                for section_task in section_tasks:
                    section_task.apply_async.assert_not_called()
