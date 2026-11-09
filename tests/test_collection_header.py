"""Collection summary uses the profile and the last earnings payout."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))

import collection_income  # noqa: E402
import collection_optimizer  # noqa: E402
import collection_tracker  # noqa: E402
import player_profile  # noqa: E402
import session_store  # noqa: E402


class ProfileParseTests(unittest.TestCase):
    def test_profile_keeps_property_ids_and_active_collections(self):
        parsed = player_profile.parse_profile({
            "properties": [{"property_id": 11}, {"property_id": 22}],
            "collections": [{"id": 5, "name": "Main", "city_id": 8}],
            "previouslyCompletedCollections": [{"id": 9, "name": "Old"}],
        })
        self.assertEqual(parsed["property_ids"], ["11", "22"])
        self.assertEqual(parsed["collections"], [{"id": 5, "name": "Main", "city_id": 8}])

    def test_empty_profile_is_not_a_portfolio(self):
        self.assertIsNone(player_profile.parse_profile({"properties": [], "collections": []}))


class IncomeRateTests(unittest.TestCase):
    def test_last_payout_matches_the_in_game_month(self):
        # pugs08 collected 81,792.15 UPX on 2026-10-30, previous collection 2026-10-16.
        # The game showed about 181,373 UPX/month.
        latest = collection_income._epoch("2026-10-30T14:49:19.500")
        previous = collection_income._epoch("2026-10-16T22:03:25.500")
        elapsed = (latest - previous) / 86400
        rated = collection_income.run_rate(81792.15, elapsed)
        self.assertAlmostEqual(rated["monthly"], 181373, delta=400)
        self.assertAlmostEqual(rated["hourly"], 248.9, delta=0.2)

    def test_rounds_stay_separate_across_days(self):
        actions = [
            {"@timestamp": "2026-10-30T14:49:19.500", "trx_id": "a"},
            {"@timestamp": "2026-10-30T14:49:17.000", "trx_id": "b"},
            {"@timestamp": "2026-10-16T22:03:25.500", "trx_id": "c"},
        ]
        rounds = collection_income.group_rounds(actions)
        self.assertEqual(len(rounds), 2)
        self.assertEqual([a["trx_id"] for a in rounds[0]], ["a", "b"])

    def test_income_uses_the_payout_and_caches_it(self):
        actions = [
            {"@timestamp": "2026-10-30T14:49:19.500", "trx_id": "new"},
            {"@timestamp": "2026-10-16T22:03:25.500", "trx_id": "old"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "income.json"
            first = collection_income.latest_collection_income(
                "vo1dsqp3qmce",
                cache_path=path,
                fetch_actions=lambda account: actions,
                fetch_payout=lambda trx_ids, account: 1000,
            )
            calls = {"n": 0}

            def boom(account):
                calls["n"] += 1
                return []

            second = collection_income.latest_collection_income(
                "vo1dsqp3qmce",
                cache_path=path,
                fetch_actions=boom,
                fetch_payout=lambda trx_ids, account: 1,
            )
        self.assertEqual(first["monthly"], second["monthly"])
        self.assertEqual(calls["n"], 0)
        self.assertEqual(first["as_of"], "Oct 30, 2026")


class HeaderTests(unittest.TestCase):
    def test_profile_and_payout_replace_the_mint_guess(self):
        result = {"completable": [{"id": 5, "name": "Main"}], "almost": [], "contributing": []}
        props = [{"id": "11", "mintPrice": 1000}]
        profile = {"property_ids": ["11", "22"], "collections": [{"id": 5, "name": "Main"}]}
        income = {"monthly": 181373, "hourly": 251.9, "payout": 81792.15, "as_of": "Oct 30, 2026"}
        collection_tracker.apply_tracker_header(result, props, 0.1225, profile, income)
        self.assertEqual(result["total_properties"], 2)
        self.assertEqual(result["collections_on"], 1)
        self.assertTrue(result["completable"][0]["on"])
        collection_tracker.apply_tracker_header(
            result, props, 0.1225, profile, income,
            all_collections=[{
                "id": 5, "name": "Catalog name", "yieldBoost": 2.5,
                "requirements": "Own 1 property on Main St.",
                "rarityLevel": 3,
            }],
        )
        self.assertEqual(result["active_collections"][0]["name"], "Main")
        self.assertEqual(result["active_collections"][0]["boost"], 2.5)
        self.assertEqual(result["active_collections"][0]["requirements"], "Own 1 property on Main St")
        self.assertEqual(result["current_monthly_yield"], 181373)
        self.assertEqual(result["yield_source"], "collection")

    def test_missing_profile_keeps_the_mint_estimate(self):
        result = {"completable": [], "almost": [], "contributing": []}
        props = [{"id": "11", "mintPrice": 12000}]
        collection_tracker.apply_tracker_header(result, props, 0.1225, None, None)
        self.assertEqual(result["total_properties"], 1)
        self.assertIsNone(result["collections_on"])
        self.assertEqual(result["current_monthly_yield"], round(12000 * 0.1225 / 12))
        self.assertEqual(result["yield_source"], "mint")


class OwnedListTests(unittest.TestCase):
    def test_profile_ids_are_the_owned_list(self):
        original = player_profile.fetch_player_profile
        lookup = collection_optimizer._lookup_property_cache_db
        fetch = collection_optimizer._fetch_properties_from_api
        enrich = collection_optimizer._enrich_mint_prices
        try:
            player_profile.fetch_player_profile = lambda username: {
                "property_ids": ["1", "2"],
                "collections": [],
            }
            collection_optimizer._lookup_property_cache_db = lambda ids: {
                "1": {"address": "10 MAIN ST", "neighborhood": "LOOP", "city": "Chicago"},
                "2": {"address": "12 MAIN ST", "neighborhood": "LOOP", "city": "Chicago"},
            }
            collection_optimizer._fetch_properties_from_api = lambda ids: (_ for _ in ()).throw(
                AssertionError("profile ids should already be in the cache")
            )
            collection_optimizer._enrich_mint_prices = lambda props: None
            props = collection_optimizer.load_user_properties("pugs08", "vo1dsqp3qmce")
        finally:
            player_profile.fetch_player_profile = original
            collection_optimizer._lookup_property_cache_db = lookup
            collection_optimizer._fetch_properties_from_api = fetch
            collection_optimizer._enrich_mint_prices = enrich
        self.assertEqual(sorted(p["id"] for p in props), ["1", "2"])


class PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_store = session_store.STORE_DIR
        session_store.STORE_DIR = Path(self.tmp.name)
        self.old_profile = player_profile.fetch_player_profile
        self.old_income = collection_income.latest_collection_income
        self.old_load_colls = collection_optimizer.load_collections
        self.old_load_props = collection_optimizer.load_user_properties

    def tearDown(self):
        player_profile.fetch_player_profile = self.old_profile
        collection_income.latest_collection_income = self.old_income
        collection_optimizer.load_collections = self.old_load_colls
        collection_optimizer.load_user_properties = self.old_load_props
        session_store.STORE_DIR = self.old_store

    def _post(self):
        import app as webapp
        client = webapp.app.test_client()
        return client.post("/collections/run", data={
            "username": "pugs08",
            "eos_account": "vo1dsqp3qmce",
            "annual_rate": "12.25",
        })

    def test_page_shows_profile_counts_and_real_earnings(self):
        player_profile.fetch_player_profile = lambda username: {
            "property_ids": ["1", "2"],
            "collections": [{"id": 5, "name": "Main"}],
        }
        collection_income.latest_collection_income = lambda eos, cache_path=None: {
            "monthly": 181373,
            "hourly": 251.9,
            "payout": 81792.15,
            "as_of": "Oct 30, 2026",
        }
        collection_optimizer.load_collections = lambda: [{
            "id": 5,
            "name": "Main St",
            "amount": 1,
            "rarityLevel": 1,
            "yieldBoost": 1.5,
            "oneTimeReward": 0,
            "requirements": "Own 1 property on MAIN ST in Chicago",
            "cityId": None,
        }]
        collection_optimizer.load_user_properties = lambda username, eos: [{
            "id": "1",
            "address": "10 MAIN ST",
            "neighborhood": "LOOP",
            "city": "Chicago",
            "mintPrice": 1000,
        }]

        response = self._post()
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Collections on", page)
        self.assertIn("181,373", page)
        self.assertIn("Oct 30, 2026", page)
        self.assertIn("81,792 UPX", page)
        self.assertNotIn("Current UPX/month (12.25%)", page)
        self.assertIn("1.5x", page)
        self.assertIn("Own 1 property on MAIN ST in Chicago", page)
        self.assertNotIn("10 MAIN ST", page)

    def test_page_falls_back_to_the_mint_estimate(self):
        player_profile.fetch_player_profile = lambda username: None
        collection_income.latest_collection_income = lambda eos, cache_path=None: None
        collection_optimizer.load_collections = lambda: []
        collection_optimizer.load_user_properties = lambda username, eos: [{
            "id": "1",
            "address": "10 MAIN ST",
            "neighborhood": "LOOP",
            "city": "Chicago",
            "mintPrice": 10000,
        }]

        response = self._post()
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Collections contributing", page)
        self.assertIn("Current UPX/month (12.25%)", page)
        self.assertIn("102", page)


if __name__ == "__main__":
    unittest.main()
