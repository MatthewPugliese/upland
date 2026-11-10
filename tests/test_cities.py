"""Stored asks rolled up by city against recent neighborhood sales."""

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


class CityAskTests(unittest.TestCase):
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
            ("low", 100, "GARDENS", "Cape Town", "UPX"),
            ("mid", 200, "GARDENS", "cape town", "UPX"),
            ("high", 400, "GARDENS", "Cape Town", "UPX"),
            ("usd", 9, "GARDENS", "Cape Town", "USD"),
            ("near", 330, "GARDENS", "Cape Town", "UPX"),
            ("q1", 50, "QUIET", "Omaha", "UPX"),
            ("q2", 80, "QUIET", "Omaha", "UPX"),
        ]
        for prop_id, price, hood, city, currency in rows:
            db.upsert_listing(self.listings, {
                "property_id": prop_id,
                "address": prop_id + " MAIN",
                "city_id": 1,
                "city": city,
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

    def test_cities_sort_by_the_closest_ask(self):
        from cities import build_cities
        page = build_cities(db_path=self.listings_path)
        self.assertEqual([row["city"] for row in page["cities"]], ["Cape Town", "Omaha"])
        cape = page["cities"][0]
        self.assertEqual(cape["upx_asks"], 4)
        self.assertEqual(cape["usd_asks"], 1)
        self.assertEqual(cape["lowest_ask"], 100)
        self.assertEqual(cape["median_ask"], 265)
        self.assertEqual(cape["closest_pct"], -66.7)
        self.assertEqual(cape["closest_id"], "low")
        self.assertEqual(cape["within_near"], 1)
        self.assertEqual(cape["compared"], 4)
        self.assertIsNone(page["cities"][1]["closest_pct"])
        self.assertEqual(page["cities"][1]["upx_asks"], 2)

        import app as webapp
        with webapp.app.test_request_context("/cities"):
            from flask import render_template
            html = render_template("cities.html", page=page)
        self.assertIn("Stored asks by city", html)
        self.assertIn("low MAIN", html)
        self.assertIn("-66.7%", html)
        self.assertIn("Omaha", html)

    def test_missing_snapshot_is_an_empty_page(self):
        from cities import build_cities
        page = build_cities(db_path=self.listings_path.with_name("missing.db"))
        self.assertFalse(page["db_exists"])
        self.assertEqual(page["cities"], [])
