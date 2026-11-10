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


class ValuationLinkTests(unittest.TestCase):
    def test_a_query_link_runs_the_estimate(self):
        import app as webapp
        calls = []

        def fake(query, include_floor=True):
            calls.append(query)
            return {
                "property": {
                    "address": "22 WOODBURN CRES",
                    "neighborhood": "Gardens",
                    "city": "Cape Town",
                    "area": 50,
                    "details_source": "local",
                    "is_listed": False,
                    "details_note": "",
                },
                "upx_valuation": {
                    "estimated_value": 1000,
                    "comp_count": 1,
                    "confidence": "Low",
                    "confidence_slug": "low",
                    "scope": "neighborhood",
                    "window_days": 90,
                    "median_per_up2": 20,
                    "comps": [{
                        "property_id": "99",
                        "address": "10 MAIN ST",
                        "timestamp": "2026-11-01T00:00:00",
                        "area": 40,
                        "upx_amount": 800,
                        "per_up2": 20,
                    }],
                },
                "usd_valuation": {
                    "estimated_value": None,
                    "comp_count": 0,
                    "confidence": "Low",
                    "confidence_slug": "low",
                },
                "floor": None,
            }

        old = valuation.estimate_value
        valuation.estimate_value = fake
        try:
            client = webapp.app.test_client()
            blank = client.get("/valuation")
            self.assertEqual(calls, [])
            self.assertIn("Estimate Value", blank.get_data(as_text=True))
            page = client.get("/valuation?query=42").get_data(as_text=True)
        finally:
            valuation.estimate_value = old

        self.assertEqual(calls, ["42"])
        self.assertIn("22 WOODBURN CRES", page)
        self.assertIn("/valuation?query=99", page)

    def test_listing_and_floor_addresses_link_to_valuation(self):
        import app as webapp
        with webapp.app.test_request_context("/listings"):
            from flask import render_template
            listings = render_template("listings.html", page={
                "db_exists": True,
                "filters": {
                    "currency": "UPX", "city": "", "neighborhood": "",
                    "sort": "markup", "limit": 100,
                },
                "cities": [],
                "neighborhoods": [],
                "coverage": [{"status": "partial"}],
                "summary": {"listings": 1, "upx": 1, "usd": 0, "freshest_listing": None},
                "listings": [{
                    "id": "42",
                    "address": "22 WOODBURN CRES",
                    "city": "Cape Town",
                    "neighborhood": "Gardens",
                    "currency": "UPX",
                    "price": 100,
                    "mint_price": 80,
                    "markup_pct": 25,
                    "owner_username": "",
                }],
            })
            floor = render_template("floor_price_results.html", result={
                "neighborhood": "Gardens",
                "city": "Cape Town",
                "total_for_sale": 1,
                "priced_count": 1,
                "truncated": False,
                "coverage": "partial",
                "note": "",
                "floor_upx": 100,
                "floor_upx_address": "22 WOODBURN CRES",
                "floor_usd": None,
                "floor_usd_address": None,
                "upx_listings": [{
                    "prop_id": "42",
                    "address": "22 WOODBURN CRES",
                    "price": 100,
                    "mint_price": 80,
                    "markup_pct": 25,
                    "owner_username": "",
                }],
                "usd_listings": [],
            })
        self.assertIn('href="/valuation?query=42"', listings)
        self.assertIn('href="/valuation?query=42"', floor)


class CompPartyTests(unittest.TestCase):
    def test_comp_rows_keep_the_buyer_and_seller(self):
        import sqlite3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        eco_path = root / "economy.db"
        pc_path = root / "property_cache.db"
        eco = sqlite3.connect(eco_path)
        eco.row_factory = sqlite3.Row
        eco.execute("""
            CREATE TABLE transactions (
                property_id TEXT, timestamp TEXT, action TEXT,
                upx_amount REAL, usd_amount REAL, asset_type TEXT,
                buyer TEXT, seller TEXT
            )
        """)
        eco.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)",
            ("99", "2026-10-01T00:00:00", "n5", 20000, None, "property", "buyeracct", "selleracct"),
        )
        pc = sqlite3.connect(pc_path)
        pc.execute("""
            CREATE TABLE properties (
                prop_id TEXT, address TEXT, city TEXT, neighborhood TEXT
            )
        """)
        pc.execute(
            "INSERT INTO properties VALUES (?,?,?,?)",
            ("99", "1 MAIN ST", "Cape Town", "GARDENS"),
        )
        pc.commit()
        pc.close()
        eco.execute("ATTACH DATABASE ? AS pc", (str(pc_path),))
        rows = valuation._query_comps(
            eco, "p.neighborhood = ?", ("GARDENS",), "2026-01-01T00:00:00",
        )
        eco.close()
        self.assertEqual(rows[0]["buyer"], "buyeracct")
        self.assertEqual(rows[0]["seller"], "selleracct")
        self.assertEqual(rows[0]["address"], "1 MAIN ST")

    def test_known_username_is_attached_and_linked(self):
        import username_lookup
        comps = [{"buyer": "buyeracct", "seller": "selleracct"}]
        old = username_lookup.lookup_many
        username_lookup.lookup_many = lambda accounts: {"selleracct": "Ada"}
        try:
            valuation._attach_party_names(comps)
        finally:
            username_lookup.lookup_many = old
        self.assertEqual(comps[0]["seller_name"], "Ada")
        self.assertIsNone(comps[0]["buyer_name"])

        import app as webapp
        result = {
            "property": {
                "address": "22 WOODBURN CRES",
                "neighborhood": "Gardens",
                "city": "Cape Town",
                "area": 10,
                "details_source": "local",
                "details_note": None,
                "is_listed": False,
                "structure_count": 0,
            },
            "floor": None,
            "upx_valuation": {
                "comp_count": 1,
                "scope": "neighborhood",
                "window_days": 90,
                "median_per_up2": 2000,
                "estimated_value": 20000,
                "confidence": "Medium",
                "confidence_slug": "medium",
                "comps": [{
                    "property_id": "99",
                    "address": "1 MAIN ST",
                    "timestamp": "2026-10-01T00:00:00",
                    "area": 10,
                    "upx_amount": 20000,
                    "per_up2": 2000,
                    "buyer": "buyeracct",
                    "buyer_name": None,
                    "seller": "selleracct",
                    "seller_name": "Ada",
                }],
            },
            "usd_valuation": {
                "comp_count": 0,
                "estimated_value": None,
                "confidence": "Very low — insufficient data",
                "confidence_slug": "very-low",
                "comps": [],
            },
        }
        with webapp.app.test_request_context("/valuation"):
            from flask import render_template
            html = render_template("valuation_results.html", result=result)
        self.assertIn("owner=Ada", html)
        self.assertIn("currency=UPX", html)
        self.assertIn("buyeracct", html)
        self.assertIn("neighborhood=Gardens", html)
        self.assertIn("city=Cape%20Town", html)
