"""Owned homes with no stored plant model."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

import what_if  # noqa: E402


class GreeneryTests(unittest.TestCase):
    def test_flags_owned_homes_without_a_plant(self):
        structs = {
            "1": [{"buildingName": "Town House", "buildingType": "residential"}],
            "2": [
                {"buildingName": "Town House", "buildingType": "residential"},
                {"buildingName": "Pine", "buildingType": "decoration"},
            ],
            "3": [{"buildingName": "Bodega", "buildingType": "service"}],
            "4": [{"buildingName": "Town House", "buildingType": "residential"}],
        }
        props = [
            {"id": "1", "address": "1 A St"},
            {"id": "2", "address": "2 B St"},
            {"id": "3", "address": "3 C St"},
            {"id": "4", "address": "9 Other St"},
        ]
        with patch.object(what_if, "get_cached_structures", return_value=structs), \
             patch.object(what_if, "get_cached_props", return_value=props), \
             patch.object(what_if, "load_owned_ids", return_value={"1", "2", "3"}):
            report = what_if.greenery_report("Dongan Hills")
        self.assertEqual([row["address"] for row in report["gaps"]], ["1 A St"])
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["planted"], 1)
        self.assertIn("Maple", report["note"])

    def test_says_when_ownership_is_missing(self):
        with patch.object(what_if, "get_cached_structures", return_value={}), \
             patch.object(what_if, "load_owned_ids", return_value=set()):
            report = what_if.greenery_report("Dongan Hills")
        self.assertEqual(report["gaps"], [])
        self.assertIn("owned-property cache", report["note"])
