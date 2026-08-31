from decimal import Decimal

from django.test import SimpleTestCase

from buh_moon_tax.pricing import weighted_buy_value


class WeightedJitaBuyTests(SimpleTestCase):
    def test_uses_best_buy_orders_and_depth(self):
        result = weighted_buy_value(
            [
                {"price": 8, "volume_remain": 100},
                {"price": 10, "volume_remain": 4},
                {"price": 9, "volume_remain": 10},
            ],
            Decimal(12),
        )
        self.assertEqual(result["priced"], Decimal(12))
        self.assertEqual(result["total"], Decimal("112.00"))
        self.assertEqual(result["weighted"], Decimal("9.33333333"))
        self.assertTrue(result["complete"])
        self.assertEqual(result["orders_used"], 2)

    def test_marks_incomplete_depth_instead_of_inventing_a_price(self):
        result = weighted_buy_value(
            [{"price": 5, "volume_remain": 3}], Decimal(5)
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["priced"], Decimal(3))
        self.assertEqual(result["shortfall"], Decimal(2))
        self.assertEqual(result["total"], Decimal("15.00"))
