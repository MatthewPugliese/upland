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

    def test_median_needs_five_sized_sales(self):
        samples = [("2026-11-01T00:00:00", 10)] * 4
        self.assertIsNone(valuation._median_rate(samples, "2026-01-01T00:00:00"))
        samples.append(("2026-11-01T00:00:00", 30))
        self.assertEqual(valuation._median_rate(samples, "2026-01-01T00:00:00"), 10)
