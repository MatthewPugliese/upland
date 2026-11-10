"""Neighborhood asking prices compared with recent sale medians."""

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
import listings_db as db  # noqa: E402


class MarketTests(unittest.TestCase):
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
        sales = [("gardens", amount) for amount in (100, 200, 300, 400, 500)]
        sales += [("richhood", amount) for amount in (100, 200, 300, 400, 500)]
        sales += [("quiet", 10), ("quiet", 20)]
        sales += [("lastone", amount) for amount in (100, 200, 300, 400, 500)]
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
        rows = [
            ("g1", 100, "GARDENS", "UPX"),
            ("g2", 200, "gardens", "UPX"),
            ("g3", 400, "GARDENS", "UPX"),
            ("g-usd", 1, "GARDENS", "USD"),
            ("r1", 400, "RICHHOOD", "UPX"),
            ("r2", 500, "RICHHOOD", "UPX"),
            ("r3", 600, "RICHHOOD", "UPX"),
            ("r4", 900, "RICHHOOD", "UPX"),
            ("thin", 50, "OTHER", "UPX"),
            ("q1", 10, "QUIET", "UPX"),
            ("q2", 20, "QUIET", "UPX"),
            ("q3", 30, "QUIET", "UPX"),
        ]
        for prop_id, price, hood, currency in rows:
            db.upsert_listing(self.listings, {
                "property_id": prop_id,
                "address": prop_id + " MAIN",
                "city_id": 1,
                "city": "Cape Town",
                "neighborhood": hood,
                "currency": currency,
                "price": price,
                "mint_price": 100,
                "owner_username": "seller",
                "last_seen": "2026-11-09T18:00:00Z",
            })
        self.listings.commit()

    def tearDown(self):
        self.listings.close()
        economy.DB_PATH = self.old_db

    def test_markets_compare_median_and_lowest_ask(self):
        from markets import build_markets
        page = build_markets(db_path=self.listings_path)
        self.assertEqual([row["neighborhood"] for row in page["markets"]], ["GARDENS", "RICHHOOD"])
        gardens = page["markets"][0]
        self.assertEqual(gardens["asks"], 3)
        self.assertEqual(gardens["median_ask"], 200)
        self.assertEqual(gardens["lowest_ask"], 100)
        self.assertEqual(gardens["sale_median_upx"], 300)
        self.assertEqual(gardens["median_vs_sales_pct"], -33.3)
        self.assertEqual(gardens["floor_vs_sales_pct"], -66.7)
        self.assertEqual(gardens["at_or_under"], 2)
        self.assertEqual(gardens["near_over"], 0)
        self.assertEqual(gardens["up_to_double"], 1)
        self.assertEqual(gardens["over_double"], 0)
        rich = page["markets"][1]
        self.assertEqual(rich["at_or_under"], 0)
        self.assertEqual(rich["near_over"], 0)
        self.assertEqual(rich["up_to_double"], 3)
        self.assertEqual(rich["over_double"], 1)
        self.assertNotIn("prices", gardens)
        self.assertEqual(gardens["city"], "Cape Town")
        self.assertEqual(gardens["sales_7d"], 5)
        self.assertEqual(page["skipped_thin"], 1)
        self.assertEqual(page["no_sales"], 1)
        self.assertEqual(page["freshest_listing"], "2026-11-09T18:00:00Z")

        import app as webapp
        with webapp.app.test_request_context("/markets"):
            from flask import render_template
            html = render_template("markets.html", page=page)
        self.assertIn("Neighborhood asks vs recent sales", html)
        self.assertIn("-66.7%", html)
        self.assertIn("GARDENS", html)
        self.assertIn("RICHHOOD", html)
        self.assertNotIn("QUIET", html)
        self.assertNotIn("OTHER", html)
        self.assertIn("Ask spread", html)
        self.assertIn("1 25-100% over", html)
        self.assertIn("1 more than double", html)

    def test_median_query_includes_a_name_past_the_old_80_cap(self):
        names = [f"empty{i}" for i in range(80)] + ["Gardens"]
        medians = economy.neighborhood_sale_medians(names)
        self.assertEqual(medians["Gardens"]["median_upx"], 300)

    def test_median_query_leaves_out_the_name_after_400(self):
        names = [f"empty{i}" for i in range(400)] + ["lastone"]
        medians = economy.neighborhood_sale_medians(names)
        self.assertNotIn("lastone", medians)

    def test_missing_snapshot_is_an_empty_page(self):
        from markets import build_markets
        page = build_markets(db_path=self.listings_path.with_name("missing.db"))
        self.assertFalse(page["db_exists"])
        self.assertEqual(page["markets"], [])
