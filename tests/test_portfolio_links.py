"""Portfolio neighborhoods open the stored asks compared with recent sales."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))


class PortfolioLinkTests(unittest.TestCase):
    def test_neighborhood_opens_asks_versus_sales(self):
        import app as webapp
        result = {
            "summary": {
                "total_properties": 1,
                "neighborhood_count": 1,
                "total_mint_value": 100,
                "total_up2": 10,
                "pct_developed": 0,
                "developed_count": 0,
                "est_monthly_yield": 0,
                "est_hourly_yield": 0,
                "yield_source": "estimate",
                "annual_rate_pct": 12.25,
                "total_sparks_invested": 0,
                "pct_sparks_estimated": 0,
            },
            "neighborhoods": [{
                "neighborhood": "ROSEBANK",
                "count": 1,
                "mint": 100,
                "up2": 10,
                "developed": 0,
                "pct_developed": 0,
            }],
            "structures": [],
            "undeveloped": [],
        }
        with webapp.app.test_request_context("/portfolio"):
            from flask import render_template
            html = render_template("portfolio_results.html", username="pugs08", result=result)
        self.assertIn("/listings?neighborhood=ROSEBANK&amp;currency=UPX&amp;sort=deal", html)
        self.assertIn("(asks vs sales)", html)
