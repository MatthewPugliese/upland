"""Collection for-sale rows link the seller and can sort by recent sales."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))

import app as webapp  # noqa: E402


class CollectionForSaleLinkTests(unittest.TestCase):
    def test_forsale_script_links_the_seller_and_sorts_by_sales(self):
        result = {
            "total_properties": 0,
            "collections_on": 0,
            "total_contributing": 0,
            "completable": [],
            "almost": [],
            "contributing": [],
            "current_monthly_yield": 0,
            "annual_rate_pct": 12.25,
            "annual_rate": 0.1225,
            "current_hourly_yield": 0,
            "yield_source": "estimate",
            "active_collections": [],
        }
        with webapp.app.test_request_context("/collections"):
            from flask import render_template
            html = render_template("collections_results.html", username="pugs08", result=result)
        self.assertIn('value="sales"', html)
        self.assertIn(">vs sales</option>", html)
        self.assertIn("sortBy === \"sales\"", html)
        self.assertIn("/listings?owner=", html)
        self.assertIn("escapeHtml(l.owner_username)", html)
        self.assertIn("/listings?neighborhood=", html)
        self.assertIn("markup-pos", html)
