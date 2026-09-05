"""The visual update must preserve administrator CSS and normal setup behavior."""

from io import StringIO
from unittest.mock import patch

from allianceauth.custom_css.models import CustomCSS
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from buh_structure_ops.console_theme import (
    END,
    START,
    STYLESHEET,
    replace_theme,
    sync_console_theme,
)


class TestThemeBlock(SimpleTestCase):
    def test_existing_customizations_survive_replacement_and_repeat_install(self):
        prefix = "/* Site owner customization */\n.navbar { height: 70px; }\n"
        suffix = "\n/* Later customization */\n.footer { color: red; }"
        original = prefix + START + "\n.old { color: red; }\n" + END + suffix
        updated = replace_theme(original, ".new { color: teal; }\n")
        self.assertEqual(
            updated,
            prefix + START + "\n.new { color: teal; }\n" + END + suffix,
        )
        self.assertEqual(replace_theme(updated, ".new { color: teal; }\n"), updated)

    def test_ambiguous_markers_fail_without_guessing_which_css_to_replace(self):
        for existing in (START, END, END + START, START + START + END, START + END + END):
            with self.subTest(existing=existing), self.assertRaises(ValueError):
                replace_theme(existing, ".new { color: teal; }")


class TestThemeInstallation(TestCase):
    def test_install_preserves_existing_css_and_refreshes_the_rendered_style(self):
        original = ".owner-customization { color: purple; }"
        CustomCSS.objects.create(pk=1, css=original)
        # Populate the same cache used by the production template tag.
        CustomCSS.get_solo()
        self.assertTrue(sync_console_theme())
        saved = CustomCSS.get_solo()
        self.assertTrue(saved.css.startswith(original + "\n\n"))
        self.assertIn(".owner-customization{color:purple}", saved.css_compressed)
        self.assertIn("#buh-archive", saved.css_compressed)
        self.assertIn("#buh-vps-health", saved.css_compressed)
        self.assertFalse(sync_console_theme())

    def test_dry_run_does_not_create_or_change_custom_css(self):
        self.assertTrue(sync_console_theme(dry_run=True))
        self.assertFalse(CustomCSS.objects.exists())
        original = CustomCSS.objects.create(pk=1, css=".owner { color: blue; }")
        self.assertTrue(sync_console_theme(dry_run=True))
        original.refresh_from_db()
        self.assertEqual(original.css, ".owner { color: blue; }")

    @patch("buh_structure_ops.management.commands.buh_structure_ops_setup.synchronize_tracked_corporations", return_value=0)
    def test_release_setup_applies_theme_and_dry_run_preserves_existing_theme(self, _sync):
        call_command("buh_structure_ops_setup", admin_character="", stdout=StringIO())
        original = CustomCSS.objects.get(pk=1).css
        self.assertIn(STYLESHEET.read_text().rstrip(), original)
        with patch.object(type(STYLESHEET), "read_text", return_value=".new { color: teal; }"):
            call_command(
                "buh_structure_ops_setup", admin_character="", dry_run=True,
                stdout=StringIO(),
            )
        self.assertEqual(CustomCSS.objects.get(pk=1).css, original)
