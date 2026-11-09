"""What-if simulator: fit rules and score deltas, with no caches and no network."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

import what_if  # noqa: E402


def _score(lu=100, essential_su=10.0, names=None):
    names = list(names or ["Bodega"])
    return {
        "total_lu": lu,
        "total_su": essential_su,
        "office_count": 2,
        "employment_buildings": 1,
        "props_with_structs": 10,
        "total_props": 20,
        "categories": [
            {"key": "essential", "label": "Essential", "su": essential_su,
             "variety_names": names, "ratio": round(essential_su / lu, 3),
             "target": 0.10, "pct_of_target": 100, "variety": len(names)},
            {"key": "entertainment", "label": "Entertainment", "su": 0,
             "variety_names": [], "ratio": 0, "target": 0.10, "pct_of_target": 0, "variety": 0},
            {"key": "public", "label": "Public Services", "su": 0,
             "variety_names": [], "ratio": 0, "target": 0.10, "pct_of_target": 0, "variety": 0},
            {"key": "transportation", "label": "Transportation", "su": 0,
             "variety_names": [], "ratio": 0, "target": 0.05, "pct_of_target": 0, "variety": 0},
        ],
    }


class EmptyLotTests(unittest.TestCase):
    def test_skips_built_lots_missing_ids_and_lots_without_a_size(self):
        props = [
            {"id": 1, "address": "1 Main St"},
            {"id": 2, "address": "2 Main St"},
            {"id": 3, "address": "3 Main St"},
            {"id": 4, "address": "4 Main St"},
        ]
        structs = {
            "1": [{"buildingName": "Bodega"}],
            "2": [],
            "3": [],
        }
        dims = {
            "_ts": 1,
            "2 MAIN ST": {"up2": 20, "width_up": 5, "eff_width": 4.2, "depth_up": 8},
            "4 MAIN ST": {"up2": 30, "width_up": 6, "eff_width": 6, "depth_up": 8},
        }
        lots = what_if.list_empty_lots(props, structs, dims, owned={"2"})
        self.assertEqual([lot["prop_id"] for lot in lots], ["2"])
        self.assertTrue(lots[0]["is_mine"])
        self.assertEqual(lots[0]["width"], 4.2)

    def test_address_match_is_case_insensitive(self):
        lots = what_if.list_empty_lots(
            [{"id": "9", "address": "83 stobe ave"}],
            {"9": []},
            {"83 STOBE AVE": {"up2": 22, "width_up": 5, "eff_width": 4.8, "depth_up": 10}},
        )
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["up2"], 22)


class FitTests(unittest.TestCase):
    def test_effective_width_rejects_a_structure_the_raw_width_would_allow(self):
        lot = {"up2": 40, "width": 4.0, "depth": 20}
        names = [row["name"] for row in what_if.fitting_structures(lot, {})]
        self.assertNotIn("Dollar Store", names)  # min width 5.0
        self.assertIn("Bodega", names)  # min width 4.0

    def test_depth_rejects_classic_hotel(self):
        reason = what_if.fit_failure(40, 8, 10, "Classic Hotel")
        self.assertIn("depth", reason)
        self.assertIsNone(what_if.fit_failure(40, 8, 16, "Classic Hotel"))

    def test_unknown_structure(self):
        self.assertIn("not in the structure database", what_if.fit_failure(100, 10, 20, "Moon Base"))


class ProjectionTests(unittest.TestCase):
    def test_service_adds_su_and_a_new_type(self):
        result = what_if.project_score(_score(), "Bike Shop", existing=0)
        essential = next(row for row in result["after"]["categories"] if row["key"] == "essential")
        self.assertEqual(essential["su"], 14)  # 10 + 4
        self.assertIn("Bike Shop", essential["variety_names"])
        self.assertTrue(result["adds"]["new_type"])
        self.assertIn("new type", result["summary"])
        self.assertEqual(result["after"]["total_lu"], 100)

    def test_repeat_type_does_not_grow_variety(self):
        result = what_if.project_score(_score(names=["Bodega"]), "Bodega", existing=3)
        essential = next(row for row in result["after"]["categories"] if row["key"] == "essential")
        self.assertEqual(essential["variety"], 1)
        self.assertFalse(result["adds"]["new_type"])
        self.assertIn("already 3", result["summary"])

    def test_house_dilutes_every_ratio(self):
        before = _score(lu=100, essential_su=10)
        result = what_if.project_score(before, "Town House", existing=0)
        self.assertEqual(result["after"]["total_lu"], 103)
        self.assertEqual(result["after"]["total_su"], result["before"]["total_su"])
        for before_cat, after_cat in zip(result["before"]["categories"], result["after"]["categories"]):
            if before_cat["su"]:
                self.assertLess(after_cat["ratio"], before_cat["ratio"])
        self.assertIn("Essential SU/LU falls", result["summary"])

    def test_house_says_when_rounding_hides_the_change(self):
        result = what_if.project_score(_score(lu=10000, essential_su=150), "Town House", existing=0)
        self.assertIn("smaller than the 0.001", result["summary"])

    def test_office_does_not_change_resident_score(self):
        result = what_if.project_score(_score(), "Small Office", existing=0)
        self.assertEqual(result["before"]["total_su"], result["after"]["total_su"])
        self.assertEqual(result["before"]["total_lu"], result["after"]["total_lu"])
        self.assertEqual(result["after"]["office_count"], 3)
        self.assertIn("Commerce", result["summary"])


class ScopeTests(unittest.TestCase):
    def _patches(self, owned):
        props = [{"id": "1", "address": "1 Main St"}]
        structs = {"1": []}
        dims = {"1 MAIN ST": {"up2": 40, "width_up": 8, "eff_width": 8, "depth_up": 16}}
        return (
            patch.object(what_if, "get_cached_structures", return_value=structs),
            patch.object(what_if, "get_cached_props", return_value=props),
            patch.object(what_if, "load_dims", return_value=dims),
            patch.object(what_if, "load_owned_ids", return_value=owned),
        )

    def test_omitted_scope_uses_every_lot_when_none_are_yours(self):
        structs, props, dims, owned = self._patches(set())
        with structs, props, dims, owned:
            data = what_if.build_what_if("Test Hood")
        self.assertEqual(data["scope"], "all")
        self.assertEqual(data["mine_count"], 0)
        self.assertEqual(len(data["lots"]), 1)
        self.assertGreater(data["lots"][0]["fit_count"], 0)

    def test_asking_for_yours_when_you_have_none_says_so(self):
        structs, props, dims, owned = self._patches(set())
        with structs, props, dims, owned:
            data = what_if.build_what_if("Test Hood", scope="mine")
        self.assertEqual(data["lots"], [])
        self.assertIn("No empty lots of yours", data["note"])


class CacheGapTests(unittest.TestCase):
    def test_known_build_is_counted_and_not_offered_as_empty(self):
        props = [
            {"id": "1", "address": "114 Seaview Ave"},
            {"id": "2", "address": "302 Buel Ave"},
        ]
        structs = {"1": [], "2": []}
        dims = {
            "114 SEAVIEW AVE": {"up2": 51, "width_up": 4.9, "eff_width": 4.9, "depth_up": 10.4},
            "302 BUEL AVE": {"up2": 50, "width_up": 5.6, "eff_width": 5.6, "depth_up": 10},
        }
        with patch.object(what_if, "get_cached_structures", return_value=structs), \
             patch.object(what_if, "get_cached_props", return_value=props), \
             patch.object(what_if, "load_dims", return_value=dims), \
             patch.object(what_if, "load_owned_ids", return_value={"1", "2"}), \
             patch.object(what_if, "compute_score", return_value=_score()):
            data = what_if.build_what_if("Dongan Hills", prop_id="2", structure="Funeral Home")
        self.assertNotIn("114 Seaview Ave", [lot["address"] for lot in data["lots"]])
        self.assertEqual(data["cache_gaps"], [
            {"address": "114 SEAVIEW AVE", "structure": "Barn & Nobles"},
        ])
        barn = next(row for row in data["structures"] if row["name"] == "Barn & Nobles")
        self.assertEqual(barn["existing"], 1)
        essential = next(row for row in data["result"]["before"]["categories"] if row["key"] == "essential")
        public = next(row for row in data["result"]["after"]["categories"] if row["key"] == "public")
        self.assertEqual(essential["su"], 14)  # cached 10 plus the uncached Barn & Nobles
        self.assertIn("Barn & Nobles", essential["variety_names"])
        self.assertEqual(public["su"], 3)

    def test_recorded_building_is_not_counted_twice(self):
        props = [{"id": "1", "address": "114 Seaview Ave"}]
        structs = {"1": [{"buildingName": "Barn & Nobles"}]}
        self.assertEqual(what_if.pending_builds("Dongan Hills", props, structs), [])


if __name__ == "__main__":
    unittest.main()
