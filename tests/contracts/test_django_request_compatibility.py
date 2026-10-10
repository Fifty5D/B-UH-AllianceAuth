"""Exercise the Auth request boundary affected by Django's October security patch.

Upstream owns the parser/algorithm regression matrix. These checks cover the
installed Auth middleware, login rendering and form/Accept parsing we consume.
"""

from unittest import mock

from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils.translation import trans_real


class AuthLocaleCompatibilityTests(TestCase):
    def test_long_language_cookies_are_bounded_before_the_auth_login_cache_lookup(self):
        # Observe the cached lookup reached through the real middleware chain,
        # without filling a cache or generating a resource-exhaustion workload.
        cached_lookup = trans_real._get_supported_language_variant
        with mock.patch.object(
            trans_real, "_get_supported_language_variant", wraps=cached_lookup
        ) as lookup:
            for suffix in ("a", "b"):
                self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en-" + suffix * 600
                response = self.client.get(reverse("authentication:login"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Language"], "en")

        self.assertTrue(lookup.call_args_list)
        for call in lookup.call_args_list:
            self.assertLessEqual(len(call.args[0]), trans_real.LANGUAGE_CODE_MAX_LENGTH)


class DjangoRequestCompatibilityTests(SimpleTestCase):
    def test_form_decoding_and_content_negotiation_keep_the_consumed_header_contract(self):
        for content_type in (
            "application/x-www-form-urlencoded; charset=UTF-8",
            "application/x-www-form-urlencoded; charset*=UTF-8''utf-8",
        ):
            with self.subTest(content_type=content_type):
                request = RequestFactory().post(
                    "/synthetic-form/",
                    data="comment=caf%C3%A9&decision=accept",
                    content_type=content_type,
                    HTTP_ACCEPT='application/json, text/plain; note="alpha;beta"; q=0.5',
                )
                self.assertEqual(request.POST["comment"], "café")
                self.assertEqual(request.POST["decision"], "accept")
                self.assertEqual(
                    request.get_preferred_type(["application/json", "text/plain"]),
                    "application/json",
                )
