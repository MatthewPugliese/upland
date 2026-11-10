"""Economy summary reports a median property sale and the newest stored sale."""

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))

import economy  # noqa: E402


class EconomyMedianTests(unittest.TestCase):
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
                marketplace TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                upx_amount REAL,
                usd_amount REAL
            )
        """)
        now = datetime.now(timezone.utc)
        rows = [
            ("a", now - timedelta(hours=1), "upx", "property", 10, None),
            ("b", now - timedelta(hours=2), "upx", "property", 30, None),
            ("c", now - timedelta(hours=3), "upx", "property", 20, None),
            ("d", now - timedelta(hours=4), "upx", "asset", 999999, None),
            ("e", now - timedelta(days=2), "usd", "property", None, 8),
        ]
        conn.executemany(
            "INSERT INTO transactions (trx_id, timestamp, marketplace, asset_type, upx_amount, usd_amount) VALUES (?,?,?,?,?,?)",
            [(trx, ts.strftime("%Y-%m-%dT%H:%M:%S"), market, asset, upx, usd) for trx, ts, market, asset, upx, usd in rows],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        economy.DB_PATH = self.old_db

    def test_median_ignores_asset_sales(self):
        result = economy.summary("30d")
        self.assertEqual(result["median_upx"], 20)
        self.assertTrue(result["latest_sale"].startswith(
            (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H")
        ))

    def test_all_time_skips_the_median(self):
        result = economy.summary("all")
        self.assertIsNone(result["median_upx"])
        self.assertIsNotNone(result["latest_sale"])

    def test_summary_cache_stays_on_its_own_database_and_period(self):
        first = economy.summary("30d")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO transactions (trx_id, timestamp, marketplace, asset_type, upx_amount) VALUES (?,?,?,?,?)",
            ("z", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "upx", "property", 100000),
        )
        conn.commit()
        conn.close()
        self.assertEqual(economy.summary("30d")["median_upx"], first["median_upx"])
        self.assertIsNone(economy.summary("all")["median_upx"])
        other = str(Path(self.tmp.name) / "other.db")
        with open(self.db_path, "rb") as src, open(other, "wb") as dst:
            dst.write(src.read())
        economy.DB_PATH = other
        # 10, 20, 30, 100000 → median of an even count is the mean of the middle pair.
        self.assertEqual(economy.summary("30d")["median_upx"], 25)

    def test_property_volume_is_compared_with_the_previous_window(self):
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM transactions")
        conn.executemany(
            "INSERT INTO transactions (trx_id, timestamp, marketplace, asset_type, upx_amount) VALUES (?,?,?,?,?)",
            [
                ("now", (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"), "upx", "property", 100),
                ("old", (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S"), "upx", "property", 40),
                ("spark", (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S"), "upx", "asset", 999),
            ],
        )
        conn.commit()
        conn.close()
        other = str(Path(self.tmp.name) / "change.db")
        with open(self.db_path, "rb") as src, open(other, "wb") as dst:
            dst.write(src.read())
        economy.DB_PATH = other
        result = economy.summary("7d")
        self.assertEqual(result["property_upx_volume"], 100)
        self.assertEqual(result["previous_property_upx_volume"], 40)
        self.assertEqual(result["property_upx_change_pct"], 150.0)
        self.assertIsNone(economy.summary("all")["property_upx_change_pct"])


if __name__ == "__main__":
    unittest.main()
