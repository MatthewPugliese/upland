"""Economy feed can show property, spark/equipment, or NFT sales."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))

import economy  # noqa: E402


class EconomyFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db = economy.DB_PATH
        self.db_path = str(Path(self.tmp.name) / "economy.db")
        economy.DB_PATH = self.db_path
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY,
                trx_id TEXT UNIQUE,
                timestamp TEXT NOT NULL,
                address TEXT,
                city TEXT,
                neighborhood TEXT,
                buyer TEXT,
                seller TEXT,
                upx_amount REAL,
                usd_amount REAL,
                marketplace TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                action TEXT,
                property_id TEXT
            )
        """)
        now = datetime.now(timezone.utc)
        rows = [
            (1, "p1", now - timedelta(hours=5), "1 MAIN", "upx", "property", "n5", 10, None),
            (2, "p2", now - timedelta(hours=4), "2 MAIN", "upx", "property", "n5", 30, None),
            (3, "a1", now - timedelta(hours=3), None, "upx", "asset", "n111", 5, None),
            (4, "n1", now - timedelta(hours=2), None, "upx", "nft", "n112", 7, None),
            (5, "u1", now - timedelta(hours=1), "3 MAIN", "usd", "property", "n52", None, 8),
        ]
        conn.executemany(
            """INSERT INTO transactions
               (id, trx_id, timestamp, address, marketplace, asset_type, action, upx_amount, usd_amount)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (i, trx, ts.strftime("%Y-%m-%dT%H:%M:%S"), addr, market, asset, action, upx, usd)
                for i, trx, ts, addr, market, asset, action, upx, usd in rows
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        economy.DB_PATH = self.old_db

    def test_default_feed_is_properties(self):
        rows = economy.feed()
        self.assertEqual([row["id"] for row in rows], [5, 2, 1])

    def test_asset_and_nft_filters(self):
        assets = economy.feed(asset="asset")
        self.assertEqual([row["action"] for row in assets], ["n111"])
        nfts = economy.feed(asset="nft")
        self.assertEqual([row["action"] for row in nfts], ["n112"])
        self.assertEqual([row["id"] for row in economy.feed(asset="nope")], [5, 2, 1])

    def test_poll_stays_on_the_selected_type(self):
        self.assertEqual([row["id"] for row in economy.latest_since(0)], [1, 2, 5])
        self.assertEqual([row["id"] for row in economy.latest_since(0, asset="asset")], [3])
        self.assertEqual([row["id"] for row in economy.latest_since(3, asset="nft")], [4])

    def test_summary_splits_upx_sales(self):
        result = economy.summary("30d")
        self.assertEqual(result["property_upx_trades"], 2)
        self.assertEqual(result["asset_upx_trades"], 1)
        self.assertEqual(result["nft_upx_trades"], 1)
        self.assertEqual(result["upx_trades"], 4)
        self.assertEqual(result["usd_trades"], 1)
        self.assertEqual(result["median_upx"], 20)

    def test_page_has_the_sale_type_filter(self):
        import app as webapp
        client = webapp.app.test_client()
        page = client.get("/economy").get_data(as_text=True)
        self.assertIn("Spark / equipment", page)
        self.assertIn("data-asset=\"nft\"", page)
        self.assertIn("does not say which is which", page)
        self.assertIn("property sales only · click a row to filter feed", page)
        self.assertIn("includes spark and NFT", page)
        self.assertIn("vs the previous", page)
        self.assertIn("/valuation?query=", page)
        self.assertIn("loadBreakdowns", page)
        self.assertIn("property sales only · which neighborhoods are hot vs depressed", page)
        self.assertIn("/listings?neighborhood=", page)
        self.assertIn("/floor?neighborhood=", page)
        body = client.get("/api/economy/feed?asset=nft").get_json()
        self.assertEqual([row["id"] for row in body["transactions"]], [4])
        fallback = client.get("/api/economy/feed?asset=bogus").get_json()
        self.assertEqual([row["id"] for row in fallback["transactions"]], [5, 2, 1])

    def test_feed_uses_cached_usernames(self):
        import app as webapp
        import username_lookup
        old = username_lookup.lookup_many
        username_lookup.lookup_many = lambda accounts: {"vwt3u3kmbncg": "pugs08"}
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE transactions SET buyer=?, seller=? WHERE id=3", ("vwt3u3kmbncg", "zx4aqmh2epmf"))
        conn.commit()
        conn.close()
        try:
            row = webapp.app.test_client().get("/api/economy/feed?asset=asset").get_json()["transactions"][0]
        finally:
            username_lookup.lookup_many = old
        self.assertEqual(row["buyer_name"], "pugs08")
        self.assertIsNone(row["seller_name"])

    def test_chart_separates_property_upx_from_spark_and_nft(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE hourly_aggregates (
                hour TEXT, marketplace TEXT, trade_count INTEGER, volume REAL
            )
        """)
        conn.execute(
            "INSERT INTO hourly_aggregates VALUES (?,?,?,?)",
            ("2026-11-09T12:00:00", "upx", 4, 52),
        )
        conn.commit()
        conn.close()
        points = economy.timeseries("30d")
        self.assertEqual(sum(point["property_upx_volume"] for point in points), 40)
        self.assertEqual(sum(point["other_upx_volume"] for point in points), 12)
        all_points = economy.timeseries("all")
        self.assertTrue(all_points)
        self.assertNotIn("property_upx_volume", all_points[0])


if __name__ == "__main__":
    unittest.main()
