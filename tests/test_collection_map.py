"""Collection maps plot local properties and do not scan a city."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))

import collection_map  # noqa: E402
import forsale_finder  # noqa: E402


HOOD = {
    "id": 5,
    "name": "Dongan Hills",
    "cityId": 8,
    "requirements": "Own 3 properties in Dongan Hills in Staten Island",
    "amount": 3,
}
STREET = {
    "id": 6,
    "name": "Main Street",
    "cityId": 8,
    "requirements": "Own 2 properties on MAIN ST in Staten Island",
    "amount": 2,
}
SAME = {
    "id": 7,
    "name": "Same City",
    "cityId": 8,
    "requirements": "Own 3 properties in the same city",
    "amount": 3,
}


def _prop(pid, status):
    return {
        "id": pid,
        "address": f"{pid} MAIN",
        "status": status,
        "mintPrice": 100,
        "neighborhood": {"name": "DONGAN HILLS"},
        "city": {"name": "Staten Island"},
    }


class CollectionMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = {
            "maps": collection_map.MAPS_DIR,
            "load": collection_map.load_collections,
            "coords": collection_map._fetch_coords,
            "cache": collection_map._load_neighborhood_cache,
            "listings": collection_map._listing_candidates,
            "owned": collection_map._owned_candidates,
            "upland_get": forsale_finder.upland_get,
            "street_search": forsale_finder._search_city_by_street,
        }
        collection_map.MAPS_DIR = Path(self.tmp.name)
        collection_map.load_collections = lambda: [HOOD, STREET, SAME]
        collection_map._fetch_coords = lambda ids: {pid: (40.5, -74.1) for pid in ids}
        collection_map._listing_candidates = lambda parsed, city_id: []
        collection_map._owned_candidates = lambda parsed, city_id, owned_ids: []

        def _boom(*_args, **_kwargs):
            raise AssertionError("city scan")

        forsale_finder.upland_get = _boom
        forsale_finder._search_city_by_street = _boom

    def tearDown(self):
        collection_map.MAPS_DIR = self.old["maps"]
        collection_map.load_collections = self.old["load"]
        collection_map._fetch_coords = self.old["coords"]
        collection_map._load_neighborhood_cache = self.old["cache"]
        collection_map._listing_candidates = self.old["listings"]
        collection_map._owned_candidates = self.old["owned"]
        forsale_finder.upland_get = self.old["upland_get"]
        forsale_finder._search_city_by_street = self.old["street_search"]

    def test_marker_colors(self):
        self.assertEqual(collection_map.marker_style("1", "Unknown", {"1"})[0], "green")
        self.assertEqual(collection_map.marker_style("2", "For sale", set())[0], "orange")
        self.assertEqual(collection_map.marker_style("3", "Unknown", set())[0], "gray")
        self.assertEqual(collection_map.marker_style("4", "Owned", set())[0], "red")

    def test_unknown_neighborhood_file_is_plotted(self):
        collection_map._load_neighborhood_cache = lambda hood, allow_unknown=False: [
            _prop("1", "Unknown"),
            _prop("2", "Unknown"),
            _prop("3", "Owned"),
            _prop("4", "Unknown"),
        ]
        collection_map._listing_candidates = lambda parsed, city_id: [{
            "id": "2",
            "address": "2 MAIN",
            "neighborhood": "DONGAN HILLS",
            "city": "Staten Island",
            "price": 1500,
            "currency": "UPX",
            "mint_price": 100,
        }]
        result = collection_map.build_collection_map(5, {"1"}, "pugs08")
        self.assertNotIn("error", result)
        self.assertEqual(result["coverage"], "neighborhood")
        self.assertEqual(result["owned_count"], 1)
        self.assertEqual(result["listed_count"], 1)
        self.assertEqual(result["unlisted_count"], 1)
        self.assertEqual(result["unknown_count"], 1)
        self.assertIn("Gray pins", result["note"])
        saved = Path(self.tmp.name) / "collection_5_pugs08.html"
        self.assertTrue(saved.exists())
        text = saved.read_text()
        self.assertIn("Dongan Hills", text)
        self.assertIn("Status not stored", text)

    def test_missing_neighborhood_file_uses_owned_and_snapshot(self):
        collection_map._load_neighborhood_cache = lambda hood, allow_unknown=False: []
        collection_map._owned_candidates = lambda parsed, city_id, owned_ids: [
            _prop("1", "Owned"),
        ]
        collection_map._listing_candidates = lambda parsed, city_id: [{
            "id": "9",
            "address": "9 MAIN",
            "neighborhood": "DONGAN HILLS",
            "city": "Staten Island",
            "price": 20,
            "currency": "UPX",
            "mint_price": 10,
        }]
        result = collection_map.build_collection_map(5, {"1"}, "pugs08")
        self.assertEqual(result["coverage"], "local")
        self.assertEqual(result["owned_count"], 1)
        self.assertEqual(result["listed_count"], 1)
        self.assertEqual(result["unlisted_count"], 0)
        self.assertIn("local snapshot", result["note"])

    def test_street_map_does_not_scan_the_city(self):
        collection_map._owned_candidates = lambda parsed, city_id, owned_ids: [
            _prop("8", "Owned"),
        ]
        result = collection_map.build_collection_map(6, {"8"}, "pugs08")
        self.assertEqual(result["coverage"], "local")
        self.assertEqual(result["owned_count"], 1)
        self.assertNotIn("error", result)

    def test_nothing_local_does_not_scan(self):
        collection_map._load_neighborhood_cache = lambda hood, allow_unknown=False: []
        result = collection_map.build_collection_map(5, set(), "pugs08")
        self.assertIn("city scan was not started", result["error"])
        self.assertFalse((Path(self.tmp.name) / "collection_5_pugs08.html").exists())

    def test_scope_without_a_place_is_rejected(self):
        result = collection_map.build_collection_map(7, {"1"}, "pugs08")
        self.assertIn("geographic scope", result["error"])

    def test_allow_unknown_keeps_placeholder_statuses(self):
        hood_dir = Path(self.tmp.name) / "neighborhoods"
        hood_dir.mkdir()
        (hood_dir / "TEST_HOOD_props_cache.json").write_text(json.dumps([
            _prop("1", "Unknown"),
        ]))
        old_cache = forsale_finder.CACHE_DIR
        forsale_finder.CACHE_DIR = Path(self.tmp.name)
        try:
            self.assertEqual(forsale_finder._load_neighborhood_cache("TEST HOOD"), [])
            kept = forsale_finder._load_neighborhood_cache("TEST HOOD", allow_unknown=True)
            self.assertEqual(len(kept), 1)
        finally:
            forsale_finder.CACHE_DIR = old_cache


if __name__ == "__main__":
    unittest.main()
