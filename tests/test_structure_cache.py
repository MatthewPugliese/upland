"""The newer structures cache wins when the two copies disagree."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "optimizer"))

from structure_cache import load_preferred_structures, prefer_structures  # noqa: E402


class PreferStructuresTests(unittest.TestCase):
    def test_newer_record_wins_and_names_match_the_database(self):
        newer = {"1": [{"buildingName": "Wheel Alignment center"}]}
        older = {"1": [], "2": [{"buildingName": "Micro House"}]}
        merged = prefer_structures(newer, older)
        self.assertEqual(merged["1"][0]["buildingName"], "Wheel Alignment Center")
        self.assertEqual(merged["2"][0]["buildingName"], "Micro House")
        self.assertEqual(newer["1"][0]["buildingName"], "Wheel Alignment center")

    def test_an_explicit_empty_lot_in_the_newer_file_stays_empty(self):
        newer = {"1": []}
        older = {"1": [{"buildingName": "Town House"}]}
        self.assertEqual(prefer_structures(newer, older)["1"], [])

    def test_missing_files_return_none(self):
        missing = Path("/tmp/upland-structures-missing.json")
        self.assertIsNone(load_preferred_structures("Nowhere", [missing]))


if __name__ == "__main__":
    unittest.main()
