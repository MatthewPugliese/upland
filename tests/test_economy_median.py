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


if __name__ == "__main__":
    unittest.main()
