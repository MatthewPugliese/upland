"""Valuation still estimates when the public API does not answer."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "webapp"))

import listings_db as db  # noqa: E402
import valuation  # noqa: E402


class LocalValuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "listings.db"
        self.conn = db.connect(self.db_path)
        self.old_db = db.DEFAULT_DB
        self.old_area = valuation.AREA_CACHE_PATH
        self.old_opt = valuation.OPTIMIZER_DIR
        self.old_live = valuation.fetch_property_live
        self.old_resolve = valuation.resolve_property
        self.old_comps = valuation.find_comps
        self.old_areas = valuation.fetch_areas
        db.DEFAULT_DB = self.db_path
        valuation.AREA_CACHE_PATH = root / "area.json"
        valuation.OPTIMIZER_DIR = root / "optimizer"
        valuation.fetch_property_live = lambda prop_id: None
        valuation.resolve_property = lambda query: {"matches": [{
            "prop_id": "42",
            "address": "22 WOODBURN CRES",
            "neighborhood": "Gardens",
            "city": "Cape Town",
        }]}
        valuation.find_comps = lambda *args, **kwargs: {
            "comps": [{
                "property_id": "99",
                "timestamp": "2026-10-01T00:00:00",
                "action": "n5",
                "upx_amount": 20000,
                "usd_amount": None,
                "address": "1 MAIN ST",
                "city": "Cape Town",
                "neighborhood": "GARDENS",
            }],
            "scope": "neighborhood",
            "window_days": 90,
        }
        valuation.fetch_areas = lambda prop_ids: {"99": 100.0}

    def tearDown(self):
        db.DEFAULT_DB = self.old_db
        valuation.AREA_CACHE_PATH = self.old_area
        valuation.OPTIMIZER_DIR = self.old_opt
        valuation.fetch_property_live = self.old_live
        valuation.resolve_property = self.old_resolve
        valuation.find_comps = self.old_comps
        valuation.fetch_areas = self.old_areas
        self.conn.close()

    def _listing(self):
        db.upsert_listing(self.conn, {
            "property_id": "42",
            "address": "22 WOODBURN CRES",
            "city_id": 133,
            "city": "Cape Town",
            "neighborhood": "GARDENS",
            "currency": "UPX",
            "price": 15999,
            "mint_price": 13840,
            "owner_username": "seller",
            "last_seen": "2026-11-09T00:00:00Z",
        })
        self.conn.commit()

    def test_stored_size_and_ask_still_produce_an_estimate(self):
        valuation.AREA_CACHE_PATH.write_text(json.dumps({"42": {"area": 50, "ts": 0}}))
        self._listing()

        result = valuation.estimate_value("22 woodburn", include_floor=False)

        self.assertNotIn("error", result)
        self.assertEqual(result["property"]["details_source"], "local")
        self.assertEqual(result["property"]["area"], 50)
        self.assertEqual(result["property"]["listing_price"], 15999)
        self.assertEqual(result["property"]["listing_currency"], "UPX")
        self.assertEqual(result["property"]["structure_count"], 0)
        self.assertIn("Structures on the lot are not included", result["property"]["details_note"])
        self.assertEqual(result["upx_valuation"]["estimated_value"], 10000)
        self.assertEqual(result["upx_valuation"]["listing_pct_diff"], 60.0)

    def test_a_lot_size_cache_supplies_area(self):
        cache = valuation.OPTIMIZER_DIR / "cache"
        cache.mkdir(parents=True)
        (cache / "Gardens_api_dims_cache.json").write_text(json.dumps({
            "22 WOODBURN CRES": {"up2": 80, "width_up": 8},
        }))

        result = valuation.estimate_value("22 woodburn", include_floor=False)

        self.assertEqual(result["property"]["area"], 80)
        self.assertEqual(result["upx_valuation"]["estimated_value"], 16000)
        self.assertNotIn("size is not stored", result["property"]["details_note"])

    def test_a_missing_size_has_no_total_and_does_not_invent_one(self):
        result = valuation.estimate_value("22 woodburn", include_floor=False)

        self.assertEqual(result["property"]["area"], 0)
        self.assertIsNone(result["upx_valuation"]["estimated_value"])
        self.assertEqual(result["upx_valuation"]["median_per_up2"], 200)
        self.assertIn("size is not stored", result["property"]["details_note"])

    def test_a_local_floor_does_not_start_another_scan(self):
        self._listing()
        valuation.AREA_CACHE_PATH.write_text(json.dumps({"42": {"area": 50, "ts": 0}}))

        def boom(*args, **kwargs):
            raise AssertionError("live floor")

        import floor_price
        old = floor_price.get_floor_prices
        floor_price.get_floor_prices = boom
        try:
            result = valuation.estimate_value("22 woodburn", include_floor=True)
        finally:
            floor_price.get_floor_prices = old

        self.assertEqual(result["floor"]["coverage"], "partial")
        self.assertEqual(result["floor"]["floor_upx"], 15999)
        self.assertEqual(result["floor"]["floor_upx_address"], "22 WOODBURN CRES")
