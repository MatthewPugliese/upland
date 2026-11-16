"""Ask versus cached UP², with no Upland API."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

import valuation  # noqa: E402


def _ask(**extra):
    row = {
        "id": "1",
        "address": "1 MAIN ST",
        "neighborhood": "Dongan Hills",
        "city": "Staten Island",
        "currency": "UPX",
        "price": 15000,
        "price_upx": 15000,
    }
    row.update(extra)
    return row


class SizeEstimateTests(unittest.TestCase):
    def test_compares_the_ask_with_cached_size(self):
        listings = [_ask()]
        rate = {"upx_per_up2": 100, "sales": 6, "window_days": 90, "scope": "neighborhood"}
        with patch.object(valuation, "size_rate", return_value=rate), \
             patch.object(valuation, "_cached_up2", return_value=100):
            valuation.annotate_size_estimates(listings)
        self.assertEqual(listings[0]["size_fair_upx"], 10000)
        self.assertEqual(listings[0]["vs_size_pct"], 50.0)
        self.assertEqual(listings[0]["size_up2"], 100)
        self.assertEqual(listings[0]["size_scope"], "neighborhood")

    def test_leaves_the_cell_blank_without_a_cached_size(self):
        listings = [_ask()]
        rate = {"upx_per_up2": 100, "sales": 6, "window_days": 90, "scope": "neighborhood"}
        with patch.object(valuation, "size_rate", return_value=rate), \
             patch.object(valuation, "_cached_up2", return_value=None):
            valuation.annotate_size_estimates(listings)
        self.assertIsNone(listings[0]["vs_size_pct"])

    def test_ignores_a_usd_ask(self):
        listings = [_ask(currency="USD", price=20, price_upx=None)]
        with patch.object(valuation, "size_rate") as rate:
            valuation.annotate_size_estimates(listings)
        rate.assert_not_called()
        self.assertIsNone(listings[0]["vs_size_pct"])

    def test_stored_table_does_not_open_the_economy_database(self):
        listings = [_ask()]
        table = {
            "neighborhoods": {
                "dongan hills": {
                    "upx_per_up2": 100, "sales": 6, "window_days": 90, "scope": "neighborhood",
                },
            },
            "cities": {},
        }
        with patch.object(valuation, "size_rate") as rate, \
             patch.object(valuation, "_cached_up2", return_value=100):
            valuation.annotate_stored_sizes(listings, table)
        rate.assert_not_called()
        self.assertEqual(listings[0]["size_fair_upx"], 10000)
        self.assertEqual(listings[0]["vs_size_pct"], 50.0)
        self.assertEqual(listings[0]["size_scope"], "neighborhood")

    def test_stored_table_falls_back_to_the_city_rate(self):
        listings = [_ask()]
        table = {
            "neighborhoods": {},
            "cities": {
                "staten island": {
                    "upx_per_up2": 50, "sales": 8, "window_days": 180, "scope": "city",
                },
            },
        }
        with patch.object(valuation, "_cached_up2", return_value=100):
            valuation.annotate_stored_sizes(listings, table)
        self.assertEqual(listings[0]["size_fair_upx"], 5000)
        self.assertEqual(listings[0]["vs_size_pct"], 200.0)
        self.assertEqual(listings[0]["size_scope"], "city")

    def test_missing_rate_file_leaves_the_cell_blank(self):
        listings = [_ask()]
        with patch.object(valuation, "size_rate") as rate:
            valuation.annotate_stored_sizes(listings, None)
        rate.assert_not_called()
        self.assertIsNone(listings[0]["vs_size_pct"])

    def test_rates_from_sales_needs_five_and_prefers_ninety_days(self):
        clock = valuation.datetime(2026, 11, 16, tzinfo=valuation.timezone.utc)
        sized = {"1": {"area": 100, "neighborhood": "Dongan Hills", "city": "Staten Island"}}
        recent = [
            {"property_id": "1", "timestamp": "2026-11-01T00:00:00", "upx_amount": amount}
            for amount in (1000, 2000, 3000, 4000, 5000)
        ]
        table = valuation.rates_from_sales(recent, sized, now=clock)
        self.assertEqual(table["neighborhoods"]["dongan hills"]["window_days"], 90)
        self.assertEqual(table["neighborhoods"]["dongan hills"]["upx_per_up2"], 30)
        self.assertEqual(table["cities"]["staten island"]["scope"], "city")
        thin = recent[:4]
        self.assertEqual(valuation.rates_from_sales(thin, sized, now=clock)["neighborhoods"], {})

    def test_repeated_price_counts_once(self):
        clock = valuation.datetime(2026, 11, 16, tzinfo=valuation.timezone.utc)
        sized = {
            "1": {"area": 100, "neighborhood": "Bulls Head", "city": "Staten Island"},
            "2": {"area": 100, "neighborhood": "Bulls Head", "city": "Staten Island"},
        }
        sales = [
            {"property_id": "1", "timestamp": f"2026-11-{day:02d}T00:00:00", "upx_amount": 100000}
            for day in range(1, 13)
        ]
        sales += [
            {"property_id": "2", "timestamp": "2026-11-02T00:00:00", "upx_amount": amount}
            for amount in (1000, 2000, 3000, 4000)
        ]
        # Property 2 is one lot, so four prices are four comps plus the repeated 100000.
        table = valuation.rates_from_sales(sales, sized, now=clock)
        rate = table["neighborhoods"]["bulls head"]
        self.assertEqual(rate["sales"], 5)
        self.assertEqual(rate["upx_per_up2"], 30)

    def test_median_needs_five_sized_sales(self):
        samples = [("2026-11-01T00:00:00", 10)] * 4
        self.assertIsNone(valuation._median_rate(samples, "2026-01-01T00:00:00"))
        samples.append(("2026-11-01T00:00:00", 30))
        self.assertEqual(valuation._median_rate(samples, "2026-01-01T00:00:00"), 10)
