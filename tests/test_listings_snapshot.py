"""Network-free tests for the for-sale snapshot store and crawl planner."""

import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

import listings_db as db  # noqa: E402


def _owned(city_id, city, hoods):
    return [
        {"city_id": city_id, "city": city, "cache_city": city, "neighborhood": hood}
        for hood in hoods
    ]


class PlannerTests(unittest.TestCase):
    def test_small_city_is_one_full_scan(self):
        targets = db.plan_targets(_owned(1, "Park City", ["OLD TOWN", "OLD TOWN"]), {1: 4859})
        self.assertEqual([t["kind"] for t in targets], ["city"])
        self.assertEqual(targets[0]["key"], "city:1")
        self.assertFalse(targets[0]["expect_partial"])

    def test_large_city_defers_and_splits_by_neighborhood(self):
        targets = db.plan_targets(
            _owned(8, "Staten Island", ["DONGAN HILLS", "ROSEBANK"]),
            {8: 80_000},
        )
        kinds = [(t["kind"], t.get("neighborhood")) for t in targets]
        self.assertEqual(kinds, [
            ("neighborhood", "DONGAN HILLS"),
            ("neighborhood", "ROSEBANK"),
            ("deferred", None),
        ])
        self.assertIn("street", targets[-1]["note"])

    def test_include_large_is_partial_and_does_not_invent_a_full_scan(self):
        targets = db.plan_targets(
            _owned(2, "Chicago", ["LOOP"]),
            {2: 562_000},
            include_large=True,
        )
        self.assertEqual([t["kind"] for t in targets], ["city"])
        self.assertTrue(targets[0]["expect_partial"])

    def test_blank_neighborhood_is_not_a_target(self):
        targets = db.plan_targets(_owned(3, "Queens", ["", "ASTORIA"]), {3: 300_000})
        hoods = [t["neighborhood"] for t in targets if t["kind"] == "neighborhood"]
        self.assertEqual(hoods, ["ASTORIA"])

    def test_unknown_size_stays_a_city_scan(self):
        targets = db.plan_targets(_owned(4, "Trenton", ["MILL HILL"]), {4: None})
        self.assertEqual(targets[0]["kind"], "city")
        self.assertFalse(targets[0]["expect_partial"])

    def test_smallest_cities_come_first(self):
        owned = _owned(1, "Park City", ["A"]) + _owned(2, "Rutherford", ["B"])
        targets = db.plan_targets(owned, {1: 4000, 2: 800})
        self.assertEqual([t["city"] for t in targets], ["Rutherford", "Park City"])


class MatchTests(unittest.TestCase):
    def test_accent_and_case_fold(self):
        cities = [{"id": 9, "name": "São Paulo"}, {"id": 10, "name": "Sao Pedro"}]
        self.assertEqual(db.match_city("sao paulo", cities)["id"], 9)

    def test_ambiguous_or_partial_names_do_not_match(self):
        cities = [{"id": 1, "name": "Washington"}, {"id": 2, "name": "Washington Court House"}]
        self.assertIsNone(db.match_city("Washington Court", cities))
        self.assertEqual(db.match_city("Washington", cities)["id"], 1)

    def test_street_term_strips_suffix_and_rejects_short_tokens(self):
        self.assertEqual(db.street_search_term("83 STOBE AVE"), "STOBE")
        self.assertEqual(db.street_search_term("15 N MAIN STREET"), "N MAIN")
        self.assertIsNone(db.street_search_term("4 A ST"))

    def test_address_match_is_token_wise(self):
        self.assertTrue(db.address_has_street("83 STOBE AVE", "Stobe Ave"))
        self.assertFalse(db.address_has_street("4 VERANDA ST", "VERA"))
        self.assertTrue(db.address_has_street("4 VERA ST", "VERA ST"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "listings.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

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

    def test_markup_is_upx_only(self):
        self.assertEqual(db.markup_pct("UPX", 150, 100), 50.0)
        self.assertEqual(db.markup_pct("UPX", 80, 100), -20.0)
        self.assertIsNone(db.markup_pct("USD", 5, 100))
        self.assertIsNone(db.markup_pct("UPX", 150, 0))

    def test_failed_price_lookup_keeps_the_stored_price(self):
        db.upsert_listing(self.conn, self._row("1"))
        db.upsert_listing(self.conn, self._row(
            "1", price=None, currency=None, last_seen="2026-08-24T01:00:00Z",
        ))
        row = self.conn.execute("SELECT price, markup_pct, last_seen FROM current_listings").fetchone()
        self.assertEqual(row["price"], 150)
        self.assertEqual(row["markup_pct"], 50.0)
        self.assertEqual(row["last_seen"], "2026-08-24T01:00:00Z")

    def test_city_sweep_does_not_touch_another_city(self):
        db.upsert_listing(self.conn, self._row("old", last_seen="2026-08-23T00:00:00Z"))
        db.upsert_listing(self.conn, self._row("new", last_seen="2026-08-24T02:00:00Z"))
        db.upsert_listing(self.conn, self._row(
            "elsewhere", city_id=2, city="Rutherford", last_seen="2026-08-01T00:00:00Z",
        ))
        removed = db.delete_unseen(self.conn, city_id=1, neighborhood=None, started_at="2026-08-24T01:00:00Z")
        self.assertEqual(removed, 1)
        left = {row["property_id"] for row in self.conn.execute("SELECT property_id FROM current_listings")}
        self.assertEqual(left, {"new", "elsewhere"})

    def test_neighborhood_sweep_keeps_the_rest_of_the_city(self):
        db.upsert_listing(self.conn, self._row("dh", neighborhood="DONGAN HILLS", last_seen="2026-08-01T00:00:00Z"))
        db.upsert_listing(self.conn, self._row("rb", neighborhood="ROSEBANK", last_seen="2026-08-01T00:00:00Z"))
        removed = db.delete_unseen(
            self.conn, city_id=1, neighborhood="Dongan Hills", started_at="2026-08-24T00:00:00Z",
        )
        self.assertEqual(removed, 1)
        left = {row["property_id"] for row in self.conn.execute("SELECT property_id FROM current_listings")}
        self.assertEqual(left, {"rb"})

    def test_requirement_query_refuses_a_stale_or_partial_snapshot(self):
        db.upsert_listing(self.conn, self._row("1", address="83 STOBE AVE"))
        db.save_target(self.conn, {
            "key": "city:1", "kind": "city", "city_id": 1, "city": "Park City",
            "status": "partial", "finished_at": db.utc_now(),
        })
        self.assertIsNone(db.listings_for_requirement(self.conn, city_id=1, street="STOBE AVE"))
        partial = db.listings_for_requirement(
            self.conn, city_id=1, street="STOBE AVE", require_fresh=False,
        )
        self.assertEqual([row["id"] for row in partial], ["1"])
        self.assertFalse(db.target_is_fresh(self.conn, "city:1", 36))
        self.assertIn("not a full city scan", db.partial_snapshot_note(self.conn))
        self.assertIn("1 listing", db.partial_snapshot_note(self.conn))

        fresh = (db.parse_time(db.utc_now())).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.save_target(self.conn, {
            "key": "city:1", "kind": "city", "city_id": 1, "city": "Park City",
            "status": "complete", "finished_at": fresh,
        })
        rows = db.listings_for_requirement(self.conn, city_id=1, street="STOBE AVE")
        self.assertEqual([row["id"] for row in rows], ["1"])
        self.assertIsNone(db.listings_for_requirement(
            self.conn, city_id=1, street="STOBE AVE", max_age_hours=-1,
        ))

    def test_neighborhood_snapshot_does_not_answer_a_street_requirement(self):
        finished = db.utc_now()
        db.save_target(self.conn, {
            "key": "hood:1:OLD TOWN", "kind": "neighborhood", "city_id": 1,
            "neighborhood": "OLD TOWN", "status": "complete", "finished_at": finished,
        })
        db.upsert_listing(self.conn, self._row("1", address="83 STOBE AVE"))
        self.assertIsNone(db.listings_for_requirement(self.conn, city_id=1, street="STOBE"))
        found = db.listings_for_requirement(self.conn, city_id=1, neighborhood="Old Town")
        self.assertEqual([row["id"] for row in found], ["1"])

    def test_search_orders_lowest_markup_first(self):
        db.upsert_listing(self.conn, self._row("high", price=300))
        db.upsert_listing(self.conn, self._row("low", price=110))
        db.upsert_listing(self.conn, self._row("usd", currency="USD", price=5, mint_price=100))
        rows = db.search_listings(self.conn, currency="UPX", sort="markup")
        self.assertEqual([row["id"] for row in rows], ["low", "high"])
        self.assertEqual(rows[0]["markup_pct"], 10.0)

    def test_search_matches_seller_without_regard_to_case(self):
        db.upsert_listing(self.conn, self._row("ada", owner_username="Ada"))
        db.upsert_listing(self.conn, self._row("bea", owner_username="Bea"))
        rows = db.search_listings(self.conn, owner="ada")
        self.assertEqual([row["id"] for row in rows], ["ada"])

    def test_listings_page_keeps_the_seller_filter(self):
        import sys
        from pathlib import Path
        web = Path(__file__).resolve().parent.parent / "webapp"
        if str(web) not in sys.path:
            sys.path.insert(0, str(web))
        from listings_view import build_listings_page
        db.upsert_listing(self.conn, self._row("ada", owner_username="Ada"))
        self.conn.commit()
        page = build_listings_page(
            {"owner": "Ada", "currency": "UPX", "sort": "deal"},
            db_path=Path(self.tmp.name) / "listings.db",
        )
        self.assertEqual(page["filters"]["owner"], "Ada")
        self.assertEqual([row["id"] for row in page["listings"]], ["ada"])
        import app as webapp
        with webapp.app.test_request_context("/listings"):
            from flask import render_template
            html = render_template("listings.html", page=page)
        self.assertIn('value="Ada"', html)
        self.assertIn("owner=Ada", html)


class CacheSeedTests(unittest.TestCase):
    def test_collects_for_sale_rows_and_prefers_a_mint_price(self):
        import listings_snapshot as snap

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "A_props_cache.json").write_text(json.dumps([
            {"id": 1, "status": "For sale", "address": "1 MAIN ST",
             "city": {"id": 8, "name": "Staten Island"},
             "neighborhood": {"name": "DONGAN HILLS"}},
            {"id": 2, "status": "Owned", "address": "2 MAIN ST"},
        ]))
        (root / "B_props_cache.json").write_text(json.dumps([
            {"id": 1, "status": "For sale", "address": "1 MAIN ST", "mintPrice": 520,
             "city": {"id": 8, "name": "Staten Island"},
             "neighborhood": {"name": "DONGAN HILLS"}},
        ]))
        seeds = snap.collect_cached_forsale([root])
        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["mint_price"], 520)
        self.assertEqual(seeds[0]["city"], "Staten Island")
        self.assertEqual(seeds[0]["city_id"], 8)
        self.assertEqual(seeds[0]["neighborhood"], "DONGAN HILLS")


class FreshnessTests(unittest.TestCase):
    def test_old_complete_target_is_not_fresh(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = db.connect(Path(tmp.name) / "listings.db")
        self.addCleanup(conn.close)
        old = (db.parse_time(db.utc_now()) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.save_target(conn, {
            "key": "city:1", "kind": "city", "city_id": 1, "city": "Park City",
            "status": "complete", "finished_at": old,
        })
        self.assertFalse(db.target_is_fresh(conn, "city:1", 24))
        self.assertTrue(db.target_is_fresh(conn, "city:1", 72))


if __name__ == "__main__":
    unittest.main()
