"""Portfolio monthly yield uses the last earnings collection."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))

import collection_income  # noqa: E402
import collection_optimizer  # noqa: E402
import portfolio_analyzer  # noqa: E402


class PortfolioIncomeTests(unittest.TestCase):
    def setUp(self):
        self.old_props = collection_optimizer.load_user_properties
        self.old_details = portfolio_analyzer.fetch_property_details
        self.old_income = collection_income.latest_collection_income
        collection_optimizer.load_user_properties = lambda username, eos: [{
            "id": "1",
            "address": "10 MAIN ST",
            "neighborhood": "LOOP",
            "city": "Chicago",
            "mintPrice": 10000,
        }]
        portfolio_analyzer.fetch_property_details = lambda props, path: {
            "1": {"up2": 50, "buildings": []},
        }

    def tearDown(self):
        collection_optimizer.load_user_properties = self.old_props
        portfolio_analyzer.fetch_property_details = self.old_details
        collection_income.latest_collection_income = self.old_income

    def test_summary_uses_the_collection_payout(self):
        collection_income.latest_collection_income = lambda eos, cache_path=None: {
            "monthly": 181614,
            "hourly": 248.79,
            "payout": 81792.15,
            "as_of": "Oct 30, 2026",
            "previous_payout": 12694.56,
            "previous_as_of": "Oct 16, 2026",
        }
        result = portfolio_analyzer.build_portfolio("pugs08", "vo1dsqp3qmce")
        summary = result["summary"]
        self.assertEqual(summary["est_monthly_yield"], 181614)
        self.assertEqual(summary["est_hourly_yield"], 248.79)
        self.assertEqual(summary["yield_source"], "collection")
        self.assertEqual(summary["income_as_of"], "Oct 30, 2026")
        self.assertEqual(summary["income_previous_payout"], 12694.56)
        self.assertEqual(summary["income_previous_as_of"], "Oct 16, 2026")

    def test_missing_payout_falls_back_to_mint(self):
        collection_income.latest_collection_income = lambda eos, cache_path=None: None
        result = portfolio_analyzer.build_portfolio("pugs08", "vo1dsqp3qmce")
        summary = result["summary"]
        self.assertEqual(summary["yield_source"], "mint")
        self.assertEqual(summary["est_monthly_yield"], round(10000 * 0.1225 / 12))

    def test_page_shows_the_collection_payout(self):
        collection_income.latest_collection_income = lambda eos, cache_path=None: {
            "monthly": 181614,
            "hourly": 248.79,
            "payout": 81792.15,
            "as_of": "Oct 30, 2026",
            "previous_payout": 12694.56,
            "previous_as_of": "Oct 16, 2026",
        }
        import app as webapp
        client = webapp.app.test_client()
        response = client.post("/portfolio/run", data={
            "username": "pugs08",
            "eos_account": "vo1dsqp3qmce",
            "annual_rate": "12.25",
        })
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("181,614", page)
        self.assertIn("Oct 30, 2026", page)
        self.assertIn("81,792 UPX", page)
        self.assertIn("The one before that was 12,695 UPX on Oct 16, 2026", page)
        self.assertIn('href="/valuation?query=1"', page)
        self.assertIn('href="/listings?neighborhood=LOOP"', page)
        self.assertNotIn("flat 12.25% annual rate against mint value (same model", page)


if __name__ == "__main__":
    unittest.main()
