"""Collection for-sale answers from a partial snapshot without calling Upland."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scraper"))
sys.path.insert(0, str(ROOT / "webapp"))

import listings_db as db  # noqa: E402
import forsale_finder  # noqa: E402


class PartialSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db_path = root / "listings.db"
        self.cache = root / "forsale"
        self.cache.mkdir()
        self.conn = db.connect(self.db_path)
        self.old_db = db.DEFAULT_DB
        self.old_cache = forsale_finder.FORSALE_CACHE_DIR
        self.old_get = forsale_finder.upland_get
        db.DEFAULT_DB = self.db_path
        forsale_finder.FORSALE_CACHE_DIR = self.cache
        self.calls = []

        def boom(*args, **kwargs):
            self.calls.append(args)
            raise AssertionError("live scan")

        forsale_finder.upland_get = boom

    def tearDown(self):
        db.DEFAULT_DB = self.old_db
        forsale_finder.FORSALE_CACHE_DIR = self.old_cache
        forsale_finder.upland_get = self.old_get
        self.conn.close()

    def _row(self, prop_id, **overrides):
        rec = {
            "property_id": prop_id,
            "address": f"{prop_id} MAIN ST",
            "city_id": 1,
            "city": "Park City",
            "neighborhood": "OLD TOWN",
            "currency": "UPX",
            "price": 150,
            "mint_price": 100,
            "owner_username": "seller",
            "last_seen": "2026-08-24T00:00:00Z",
        }
        rec.update(overrides)
        return rec

    def _save_partial(self):
        db.save_target(self.conn, {
            "key": "reprice:props-cache",
            "kind": "reprice",
            "status": "partial",
            "finished_at": db.utc_now(),
            "note": "Not a full city scan.",
        })
        self.conn.commit()

    def _entry(self, coll_id, **overrides):
        entry = {
            "id": coll_id,
            "req_type": "neighborhood",
            "city_id": 1,
            "parsed_req": {"neighborhood": "Old Town"},
        }
        entry.update(overrides)
        return entry

    def _ask(self, coll_id, **overrides):
        return forsale_finder.find_forsale_for_collection(
            self._entry(coll_id, **overrides), set(),
        )

    def test_partial_rows_are_labeled_and_do_not_call_upland(self):
        db.upsert_listing(self.conn, self._row("cheap", price=110))
        db.upsert_listing(self.conn, self._row("mine", price=120))
        db.upsert_listing(self.conn, self._row("other", neighborhood="ROSEBANK", price=90))
        self._save_partial()
        answer = forsale_finder.find_forsale_for_collection(
            self._entry(7), {"mine"},
        )
        self.assertEqual(answer["coverage"], "partial")
        self.assertIn("not a full city scan", answer["note"])
        self.assertEqual([row["id"] for row in answer["listings"]], ["cheap"])
        self.assertEqual(answer["listings"][0]["price_upx"], 110)
        self.assertEqual(self.calls, [])
        cached = json.loads((self.cache / "coll_7.json").read_text())
        self.assertEqual(cached["coverage"], "partial")
        self.assertIn("listings", cached)
        again = forsale_finder.find_forsale_for_collection(self._entry(7), {"mine"})
        self.assertEqual(again["coverage"], "partial")
        self.assertEqual(self.calls, [])

    def test_a_partial_miss_is_not_stored_as_a_finished_empty_scan(self):
        db.upsert_listing(self.conn, self._row("1"))
        self._save_partial()
        answer = self._ask(8, parsed_req={"neighborhood": "CHELSEA"})
        self.assertEqual(answer["listings"], [])
        self.assertEqual(answer["coverage"], "partial")
        self.assertIn("not a full city scan", answer["note"])
        self.assertEqual(self.calls, [])
        cached = json.loads((self.cache / "coll_8.json").read_text())
        self.assertIsInstance(cached, dict)
        self.assertEqual(cached["coverage"], "partial")
        self.assertEqual(cached["listings"], [])

    def test_a_fresh_empty_snapshot_stays_complete(self):
        db.save_target(self.conn, {
            "key": "hood:1:OLD TOWN",
            "kind": "neighborhood",
            "city_id": 1,
            "neighborhood": "OLD TOWN",
            "status": "complete",
            "finished_at": db.utc_now(),
        })
        self.conn.commit()
        answer = self._ask(4)
        self.assertEqual(answer["coverage"], "complete")
        self.assertIsNone(answer["note"])
        self.assertEqual(answer["listings"], [])
        self.assertEqual(self.calls, [])

    def test_a_partial_street_match_uses_the_address(self):
        db.upsert_listing(self.conn, self._row("stobe", address="83 STOBE AVE"))
        db.upsert_listing(self.conn, self._row("main", address="1 MAIN ST"))
        self._save_partial()
        answer = self._ask(
            5,
            req_type="street",
            parsed_req={"street": "STOBE AVE"},
        )
        self.assertEqual(answer["coverage"], "partial")
        self.assertEqual([row["id"] for row in answer["listings"]], ["stobe"])
        self.assertEqual(self.calls, [])

    def test_an_older_list_cache_still_loads(self):
        (self.cache / "coll_3.json").write_text(json.dumps([
            {"id": "cached", "price_upx": 1, "address": "1 MAIN ST"},
        ]))
        answer = self._ask(3)
        self.assertEqual(answer["coverage"], "complete")
        self.assertEqual(answer["listings"][0]["id"], "cached")
        self.assertEqual(self.calls, [])

    def test_a_place_requirement_still_leaves_the_snapshot(self):
        db.upsert_listing(self.conn, self._row("1"))
        self._save_partial()
        with self.assertRaises(AssertionError):
            self._ask(9, req_type="place", parsed_req={"type": "place", "place": "ZZZ No Cache"})
        self.assertTrue(self.calls)
