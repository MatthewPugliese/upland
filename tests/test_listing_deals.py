"""UPX asks are compared with recent neighborhood sale medians."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "webapp"))

import economy  # noqa: E402
import floor_price  # noqa: E402
import listings_db as db  # noqa: E402
import listings_view  # noqa: E402


class ListingDealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db = economy.DB_PATH
        self.eco_path = str(Path(self.tmp.name) / "economy.db")
        economy.DB_PATH = self.eco_path
        conn = sqlite3.connect(self.eco_path)
        conn.execute("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                neighborhood TEXT,
                marketplace TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                upx_amount REAL
            )
        """)
        now = datetime.now(timezone.utc)
        # Five Gardens sales so the median is 300. Two Other sales stay hidden.
        sales = [("gardens", amount) for amount in (100, 200, 300, 400, 500)]
        sales += [("other", 10), ("other", 20)]
        conn.executemany(
            "INSERT INTO transactions (timestamp, neighborhood, marketplace, asset_type, upx_amount) VALUES (?,?,?,?,?)",
            [
                ((now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S"), hood, "upx", "property", amount)
                for hood, amount in sales
            ],
        )
        conn.commit()
        conn.close()

        self.listings_path = Path(self.tmp.name) / "listings.db"
        self.listings = db.connect(self.listings_path)
        for prop_id, price, hood in (
            ("cheap", 150, "GARDENS"),
            ("rich", 600, "GARDENS"),
            ("thin", 10, "OTHER"),
        ):
            db.upsert_listing(self.listings, {
                "property_id": prop_id,
                "address": prop_id + " MAIN",
                "city_id": 1,
                "city": "Cape Town",
                "neighborhood": hood,
                "currency": "UPX",
                "price": price,
                "mint_price": 100,
                "owner_username": "seller",
                "last_seen": "2026-11-09T00:00:00Z",
            })
        self.listings.commit()

    def tearDown(self):
        self.listings.close()
        economy.DB_PATH = self.old_db

    def test_median_ignores_a_thin_neighborhood_and_ignores_case(self):
        medians = economy.neighborhood_sale_medians(["Gardens", "Other"])
        self.assertEqual(medians["Gardens"]["median_upx"], 300)
        self.assertEqual(medians["Gardens"]["sales"], 5)
        self.assertNotIn("Other", medians)

    def test_listing_page_marks_asks_under_the_median_and_sorts_them_first(self):
        page = listings_view.build_listings_page(
            {"sort": "deal", "currency": "any", "limit": 10},
            db_path=self.listings_path,
        )
        self.assertEqual([row["id"] for row in page["listings"]], ["cheap", "rich", "thin"])
        self.assertEqual(page["listings"][0]["vs_median_pct"], -50.0)
        self.assertEqual(page["listings"][1]["vs_median_pct"], 100.0)
        self.assertIsNone(page["listings"][2]["vs_median_pct"])
        self.assertEqual(page["deals"]["under_median"], 1)

        import app as webapp
        with webapp.app.test_request_context("/listings"):
            from flask import render_template
            html = render_template("listings.html", page=page)
        self.assertIn("Asks under recent sales", html)
        self.assertIn("-50%", html)
        self.assertIn("Newest stored ask: 2026-11-09T00:00:00Z", html)

    def test_floor_result_uses_the_same_median(self):
        result = floor_price._attach_sale_comparison({
            "neighborhood": "gardens",
            "city": "Cape Town",
            "total_for_sale": 2,
            "coverage": "partial",
            "floor_upx": 150,
            "floor_upx_address": "CHEAP MAIN",
            "floor_usd": None,
            "upx_listings": [
                {"price": 150, "address": "CHEAP MAIN", "prop_id": "cheap", "mint_price": 100, "markup_pct": 50, "owner_username": "seller"},
                {"price": 300, "address": "EVEN MAIN", "prop_id": "even", "mint_price": 100, "markup_pct": 200, "owner_username": "seller"},
            ],
            "usd_listings": [],
        })
        self.assertEqual(result["sale_median_upx"], 300)
        self.assertEqual(result["upx_listings"][0]["vs_median_pct"], -50.0)
        self.assertEqual(result["asks_compared"], 2)
        self.assertEqual(result["asks_at_or_under"], 2)

    def test_deals_page_keeps_asks_at_least_10_percent_under(self):
        import listings_db as db
        from deals import build_deals
        db.upsert_listing(self.listings, {
            "property_id": "close",
            "address": "CLOSE MAIN",
            "city_id": 1,
            "city": "Cape Town",
            "neighborhood": "GARDENS",
            "currency": "UPX",
            "price": 285,
            "mint_price": 100,
            "owner_username": "seller",
            "last_seen": "2026-11-09T00:00:00Z",
        })
        db.upsert_listing(self.listings, {
            "property_id": "even",
            "address": "even MAIN",
            "city_id": 1,
            "city": "Cape Town",
            "neighborhood": "GARDENS",
            "currency": "UPX",
            "price": 300,
            "mint_price": 100,
            "owner_username": "seller",
            "last_seen": "2026-11-09T00:00:00Z",
        })
        db.upsert_listing(self.listings, {
            "property_id": "band",
            "address": "band MAIN",
            "city_id": 1,
            "city": "Cape Town",
            "neighborhood": "GARDENS",
            "currency": "UPX",
            "price": 375,
            "mint_price": 100,
            "owner_username": "seller",
            "last_seen": "2026-11-09T00:00:00Z",
        })
        db.upsert_listing(self.listings, {
            "property_id": "far",
            "address": "far MAIN",
            "city_id": 1,
            "city": "Cape Town",
            "neighborhood": "GARDENS",
            "currency": "UPX",
            "price": 390,
            "mint_price": 100,
            "owner_username": "seller",
            "last_seen": "2026-11-09T00:00:00Z",
        })
        self.listings.commit()
        page = build_deals(db_path=self.listings_path)
        self.assertEqual([row["id"] for row in page["deals"]], ["cheap"])
        self.assertEqual(page["at_median"], 1)
        self.assertEqual([row["id"] for row in page["matches"]], ["even"])
        self.assertEqual([row["id"] for row in page["near"]], ["close", "band"])
        self.assertEqual(page["deals"][0]["vs_median_pct"], -50.0)

        import app as webapp
        with webapp.app.test_request_context("/deals"):
            from flask import render_template
            html = render_template("deals.html", page=page)
        self.assertIn("cheap MAIN", html)
        self.assertIn("-50%", html)
        self.assertIn("1 compared ask matches the recent median", html)
        self.assertIn("even MAIN", html)
        self.assertIn("CLOSE MAIN", html)
        self.assertIn("band MAIN", html)
        self.assertIn("+25.0%", html)
        self.assertNotIn("far MAIN", html)
        self.assertNotIn("rich MAIN", html)
