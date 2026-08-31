import json
import os
import urllib.error
import urllib.request
from unittest import TestCase


class FakeEsiContractTests(TestCase):
    root_url = os.environ.get("BUH_FAKE_ESI_ROOT", "http://fake-esi:8080/")
    base_url = os.environ.get("BUH_FAKE_ESI_URL", "http://fake-esi:8080/latest/")

    def get(self, path):
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=3) as response:
            return response.status, dict(response.headers), json.load(response)

    def test_market_orders_are_deterministic_and_paginated(self):
        status, headers, payload = self.get("markets/10000002/orders/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Pages"], "1")
        self.assertTrue(payload[0]["is_buy_order"])

    def test_unmatched_routes_fail_closed(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("not-a-real-endpoint/")
        self.assertEqual(caught.exception.code, 501)

    def test_pinned_openapi_document_is_available_at_the_django_esi_path(self):
        with urllib.request.urlopen(
            f"{self.root_url}meta/openapi.json", timeout=3
        ) as response:
            payload = json.load(response)
        self.assertEqual(payload["info"]["version"], "2025-12-16")
        self.assertIn("/markets/{region_id}/orders", payload["paths"])

    def test_moon_tax_pricing_client_uses_the_fixture_server(self):
        from buh_moon_tax.pricing import EsiJitaBuyClient

        orders = EsiJitaBuyClient().orders(
            62454,
            region_id=10000002,
            location_id=60003760,
        )
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["price"], 1760.0)

    def test_django_esi_client_loads_the_reduced_spec_and_calls_market(self):
        from esi.openapi_clients import ESIClientProvider

        client = ESIClientProvider(
            compatibility_date="2025-12-16",
            ua_appname="BuhSourceTests",
            ua_version="1.0.0",
            tags=["Market"],
        ).client
        orders = client.Market.GetMarketsRegionIdOrders(
            order_type="buy",
            region_id=10000002,
            type_id=62454,
        ).results(use_etag=False, use_cache=False, store_cache=False)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].location_id, 60003760)
