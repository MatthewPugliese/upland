"""Empty-lot builds sort ahead of demolitions."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "optimizer"))

from known_builds import apply_missing_builds, missing_builds  # noqa: E402
from recommender import ACTION_SORT  # noqa: E402


class ActionSortTests(unittest.TestCase):
    def test_empty_lot_builds_come_before_demolitions(self):
        rows = [
            {"is_mine": True, "action": "DEMOLISH → BUILD", "su_gain": 8},
            {"is_mine": True, "action": "BUILD", "su_gain": 4},
            {"is_mine": False, "action": "BUILD", "su_gain": 20},
            {"is_mine": True, "action": "KEEP", "su_gain": 0},
        ]
        rows.sort(key=lambda row: (
            0 if row["is_mine"] else 1,
            ACTION_SORT.get(row["action"], 9),
            -(row["su_gain"] or 0),
        ))
        self.assertEqual(
            [(row["action"], row["su_gain"]) for row in rows],
            [
                ("BUILD", 4),
                ("DEMOLISH → BUILD", 8),
                ("KEEP", 0),
                ("BUILD", 20),
            ],
        )


class PageOrderTests(unittest.TestCase):
    def test_score_page_opens_with_builds_before_demolitions(self):
        html = (Path(__file__).resolve().parent.parent / "webapp" / "templates" / "score.html").read_text()
        self.assertIn("The first rows follow the build order above.", html)
        self.assertIn("Demolitions stay under the other empty-lot builds.", html)
        self.assertIn("a.build_order", html)
        self.assertIn("const ACTION_RANK = {'BUILD': 0, 'DEMOLISH → BUILD': 1, 'KEEP': 2};", html)
        self.assertIn("let _sortCol = null;", html)
        self.assertNotIn('data-col="su_gain" class="sort-desc"', html)
        self.assertNotIn('class="sort-desc" data-col="su_gain"', html)

    def test_html_report_opens_with_builds_before_demolitions(self):
        source = (Path(__file__).resolve().parent.parent / "optimizer" / "report.py").read_text()
        self.assertIn("sortCol: null", source)
        self.assertIn("ACTION_RANK[a.action]", source)
        self.assertNotIn("SU Gain ↓", source)


class KnownBuildTests(unittest.TestCase):
    def test_114_seaview_is_missing_until_the_cache_records_it(self):
        props = [{"id": "1", "address": "114 Seaview Ave"}]
        empty = missing_builds("Dongan_Hills", props, {"1": []})
        self.assertEqual(empty, [{
            "prop_id": "1",
            "address": "114 SEAVIEW AVE",
            "structure": "Barn & Nobles",
        }])
        recorded = missing_builds("Dongan Hills", props, {"1": [{"buildingName": "Barn & Nobles"}]})
        self.assertEqual(recorded, [])

    def test_apply_adds_the_building_once_and_leaves_the_file_alone(self):
        props = [{"id": "1", "address": "114 Seaview Ave"}]
        structures = {"1": []}
        apply_missing_builds("Dongan_Hills", props, structures)
        apply_missing_builds("Dongan Hills", props, structures)
        self.assertEqual(structures["1"], [{
            "buildingName": "Barn & Nobles",
            "constructionStatus": "completed",
        }])


if __name__ == "__main__":
    unittest.main()
