"""Tests for analytics that do not require ESI or Celery."""

import datetime as dt
from unittest import TestCase

from buh_mining_analytics.analytics import MiningRecord, build_analytics


def record(
    date,
    character_id=1,
    character_name="Fifty5D",
    account_id=10,
    account_name="Fifty5D",
    ore_id=100,
    ore_name="Bitumens",
    system_name="Badivefi",
    quantity=1_000,
    unit_volume=0.1,
    unit_price=500,
):
    return MiningRecord(
        date=date,
        character_id=character_id,
        character_name=character_name,
        account_id=account_id,
        account_name=account_name,
        corporation_name="Bureau of Unified Harvesting",
        ore_id=ore_id,
        ore_name=ore_name,
        system_name=system_name,
        region_name="Tash-Murkon",
        quantity=quantity,
        volume=quantity * unit_volume,
        isk=quantity * unit_price,
        price_known=True,
        volume_known=True,
    )


class TestBuildAnalytics(TestCase):
    def setUp(self):
        self.start = dt.date(2026, 8, 1)
        self.end = dt.date(2026, 8, 7)

    def test_totals_records_and_streak(self):
        rows = [
            record(dt.date(2026, 8, 1), quantity=1_000),
            record(dt.date(2026, 8, 2), quantity=2_000),
            record(dt.date(2026, 8, 3), quantity=3_000),
            record(dt.date(2026, 8, 7), quantity=4_000),
        ]
        result = build_analytics(rows, self.start, self.end)

        self.assertEqual(result["kpis"]["total"]["units"], 10_000)
        self.assertEqual(result["kpis"]["total"]["volume"], 1_000)
        self.assertEqual(result["kpis"]["total"]["isk"], 5_000_000)
        self.assertEqual(result["kpis"]["active_days"], 4)
        self.assertEqual(result["kpis"]["longest_streak"], 3)
        self.assertEqual(result["kpis"]["current_streak"], 1)
        self.assertEqual(result["kpis"]["best_day"]["date"], "2026-08-07")

    def test_character_comparison_and_other_series(self):
        rows = []
        for index in range(14):
            rows.append(
                record(
                    self.start,
                    character_id=index + 1,
                    character_name=f"Miner {index + 1}",
                    account_id=index + 100,
                    account_name=f"Pilot {index + 1}",
                    quantity=14 - index,
                )
            )
        result = build_analytics(rows, self.start, self.end, comparison="character")

        self.assertEqual(result["trend"]["comparison"], "character")
        self.assertEqual(len(result["trend"]["series"]), 13)
        self.assertEqual(result["trend"]["series"][-1]["label"], "Other (2)")

    def test_previous_range_change(self):
        current = [record(self.start, quantity=200)]
        previous = [record(self.start - dt.timedelta(days=7), quantity=100)]
        result = build_analytics(
            current,
            self.start,
            self.end,
            previous_records=previous,
        )

        self.assertEqual(result["kpis"]["change"]["isk"], 100.0)
        self.assertEqual(result["kpis"]["change"]["volume"], 100.0)
        self.assertEqual(result["kpis"]["change"]["units"], 100.0)

    def test_weekly_and_monthly_buckets(self):
        weekly = build_analytics(
            [record(self.start)],
            self.start,
            self.start + dt.timedelta(days=180),
        )
        monthly = build_analytics(
            [record(self.start)],
            self.start,
            self.start + dt.timedelta(days=800),
        )

        self.assertEqual(weekly["trend"]["granularity"], "week")
        self.assertEqual(monthly["trend"]["granularity"], "month")

    def test_empty_selection_is_json_safe(self):
        result = build_analytics([], self.start, self.end)

        self.assertEqual(
            result["kpis"]["total"], {"isk": 0.0, "volume": 0.0, "units": 0}
        )
        self.assertEqual(result["kpis"]["price_coverage"], 100.0)
        self.assertEqual(result["personality"]["name"], "Industrial Generalist")
