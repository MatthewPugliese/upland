"""Floor price uses stored listings and does not call Upland when they exist."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "webapp"))

import listings_db as db  # noqa: E402
import floor_price  # noqa: E402


class FloorSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "listings.db"
        self.cache = root / "floor"
        self.cache.mkdir()
        self.conn = db.connect(self.db_path)
        self.old_db = db.DEFAULT_DB
        self.old_cache = floor_price.CACHE_DIR
        db.DEFAULT_DB = self.db_path
        floor_price.CACHE_DIR = self.cache
        self.old_price = floor_price._public_api_price
        self.old_file = floor_price._load_neighborhood_cache
        self.old_resolve = floor_price._resolve_neighborhood

        def boom(*args, **kwargs):
            raise AssertionError("live floor lookup")

        floor_price._public_api_price = boom
        floor_price._load_neighborhood_cache = lambda hood, allow_unknown=False: []
        floor_price._resolve_neighborhood = lambda name: {
            "name": "Gardens",
            "city_name": "Cape Town",
            "city_id": 133,
        }

    def tearDown(self):
        db.DEFAULT_DB = self.old_db
        floor_price.CACHE_DIR = self.old_cache
        floor_price._public_api_price = self.old_price
        floor_price._load_neighborhood_cache = self.old_file
        floor_price._resolve_neighborhood = self.old_resolve
        self.conn.close()

    def _row(self, prop_id, price, **overrides):
        rec = {
            "property_id": prop_id,
            "address": f"{prop_id} GLEN CRES",
            "city_id": 133,
            "city": "Cape Town",
            "neighborhood": "GARDENS",
            "currency": "UPX",
            "price": price,
            "mint_price": 100,
            "owner_username": "seller",
            "last_seen": "2026-11-09T00:00:00Z",
        }
        rec.update(overrides)
        return rec

    def test_stored_rows_set_the_floor_without_a_live_lookup(self):
        db.upsert_listing(self.conn, self._row("high", 25000, address="1 INVERMARK CRES"))
        db.upsert_listing(self.conn, self._row("low", 15999, address="22 WOODBURN CRES"))
        db.upsert_listing(self.conn, self._row(
            "usd", 8, currency="USD", address="4 INVERMARK CRES", mint_price=100,
        ))
        self.conn.commit()

        result = floor_price.get_floor_prices("Gardens")
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(result["floor_upx"], 15999)
        self.assertEqual(result["floor_upx_address"], "22 WOODBURN CRES")
        self.assertEqual(result["floor_upx_id"], "low")
        self.assertEqual(result["floor_usd"], 8)
        self.assertEqual(result["floor_usd_id"], "usd")
        self.assertIn("not a full city scan", result["note"])
        cached = json.loads((self.cache / "GARDENS.json").read_text())
        self.assertEqual(cached["coverage"], "partial")
        self.assertEqual(cached["floor_upx_id"], "low")

        import app as webapp
        with webapp.app.test_request_context("/floor"):
            from flask import render_template
            html = render_template("floor_price_results.html", result=result)
        self.assertIn("/valuation?query=low", html)
        self.assertIn("/valuation?query=usd", html)
        self.assertIn("neighborhood=Gardens", html)
        self.assertIn("city=Cape%20Town", html)

    def test_no_stored_rows_does_not_scan_a_city(self):
        self.conn.commit()
        result = floor_price.get_floor_prices("Gardens")
        self.assertIn("city scan was not started", result["error"])

    def test_neighborhood_file_sets_the_floor_without_a_city_scan(self):
        self.conn.commit()
        floor_price._load_neighborhood_cache = lambda hood, allow_unknown=False: [
            {"id": "1", "address": "22 WOODBURN CRES", "status": "For sale", "mintPrice": 1000},
            {"id": "2", "address": "1 INVERMARK CRES", "status": "Owned", "mintPrice": 1000},
        ]

        def price(prop_id):
            if str(prop_id) == "1":
                return 15999, None, "UPX", "seller"
            raise AssertionError("priced a row that is not for sale")

        floor_price._public_api_price = price
        result = floor_price.get_floor_prices("Gardens")
        self.assertEqual(result["coverage"], "neighborhood")
        self.assertEqual(result["floor_upx"], 15999)
        self.assertEqual(result["floor_upx_address"], "22 WOODBURN CRES")
        self.assertEqual(result["floor_upx_id"], "1")
        self.assertEqual(result["total_for_sale"], 1)
        self.assertIn("city scan was not started", result["note"])
