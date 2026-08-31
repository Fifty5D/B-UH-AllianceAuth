from django.core.checks import run_checks
from django.test import SimpleTestCase


class AdminSystemCheckTests(SimpleTestCase):
    def test_registered_admin_definitions_pass_django_checks(self):
        errors = run_checks(tags=["admin"])
        self.assertEqual([], errors, "\n".join(str(error) for error in errors))
