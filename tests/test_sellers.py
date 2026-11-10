"""Sellers with more than one stored ask, compared with recent sales."""

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


class SellerTests(unittest.TestCase):
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
        conn.executemany(
            "INSERT INTO transactions (timestamp, neighborhood, marketplace, asset_type, upx_amount) VALUES (?,?,?,?,?)",
            [
                ((now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"), hood, "upx", "property", amount)
                for hood, amount in sales
            ],
        )
        conn.commit()
        conn.close()

        self.listings_path = Path(self.tmp.name) / "listings.db"
        self.listings = db.connect(self.listings_path)
        rows = [
            ("a1", 150, "GARDENS", "Ada", "UPX"),
            ("a2", 600, "GARDENS", "ada", "UPX"),
            ("b1", 400, "RICHHOOD", "Bea", "UPX"),
            ("b2", 500, "RICHHOOD", "Bea", "UPX"),
            ("b3", 600, "RICHHOOD", "Bea", "UPX"),
            ("c1", 10, "QUIET", "Cam", "UPX"),
            ("c2", 20, "QUIET", "Cam", "UPX"),
            ("d1", 50, "GARDENS", "Dot", "UPX"),
            ("usd", 9, "GARDENS", "Ada", "USD"),
        ]
        for prop_id, price, hood, owner, currency in rows:
            db.upsert_listing(self.listings, {
                "property_id": prop_id,
                "address": prop_id + " MAIN",
                "city_id": 1,
                "city": "Cape Town",
                "neighborhood": hood,
                "currency": currency,
                "price": price,
                "mint_price": 100,
                "owner_username": owner,
                "last_seen": "2026-11-09T18:00:00Z",
            })
        self.listings.commit()

    def tearDown(self):
        self.listings.close()
        economy.DB_PATH = self.old_db

    def test_sellers_sort_by_the_ask_furthest_under_sales(self):
        from sellers import build_sellers
        page = build_sellers(db_path=self.listings_path)
        self.assertEqual(
            [row["owner_username"] for row in page["sellers"]],
            ["Ada", "Bea", "Cam"],
        )
        ada = page["sellers"][0]
        self.assertEqual(ada["asks"], 2)
        self.assertEqual(ada["best_vs_median_pct"], -50.0)
        self.assertEqual(ada["under_median"], 1)
        self.assertEqual(ada["compared"], 2)
        self.assertEqual(ada["best_id"], "a1")
        self.assertEqual(page["sellers"][1]["best_vs_median_pct"], 33.3)
        self.assertIsNone(page["sellers"][2]["best_vs_median_pct"])
        self.assertEqual(page["skipped_single"], 1)

        import app as webapp
        with webapp.app.test_request_context("/sellers"):
            from flask import render_template
            html = render_template("sellers.html", page=page)
        self.assertIn("Sellers vs recent sales", html)
        self.assertIn("play.upland.me/profile?username=Ada", html)
        self.assertIn("-50.0%", html)
        self.assertIn("a1 MAIN", html)
        self.assertNotIn("Dot", html)
        self.assertNotIn("d1 MAIN", html)

    def test_missing_snapshot_is_an_empty_page(self):
        from sellers import build_sellers
        page = build_sellers(db_path=self.listings_path.with_name("missing.db"))
        self.assertFalse(page["db_exists"])
        self.assertEqual(page["sellers"], [])
