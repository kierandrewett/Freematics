import json
import sys
import unittest
from pathlib import Path


MONITORING = Path(__file__).parent
sys.path.insert(0, str(MONITORING))

from build_dashboard import build_dashboard  # noqa: E402


class DashboardViewsTest(unittest.TestCase):
    def test_live_view_is_current_and_does_not_require_trip_selection(self) -> None:
        dashboard = build_dashboard("live")
        self.assertEqual(dashboard["uid"], "freematics-live")
        self.assertEqual(dashboard["time"], {"from": "now-5m", "to": "now"})
        self.assertEqual([item["name"] for item in dashboard["templating"]["list"]], ["device"])
        self.assertNotIn("Trip index", {panel["title"] for panel in dashboard["panels"]})
        self.assertNotIn("Trip route", {panel["title"] for panel in dashboard["panels"]})
        expressions = [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]
        self.assertTrue(expressions)
        self.assertTrue(all("$trip" not in expression for expression in expressions))

    def test_trips_view_has_historical_selector_and_route_evidence(self) -> None:
        dashboard = build_dashboard("trips")
        self.assertEqual(dashboard["uid"], "freematics-trips")
        self.assertEqual(dashboard["time"], {"from": "now-90d", "to": "now"})
        self.assertEqual([item["name"] for item in dashboard["templating"]["list"]], ["device", "trip"])
        titles = {panel["title"] for panel in dashboard["panels"]}
        self.assertIn("Trip index", titles)
        self.assertIn("Trip route", titles)
        route = next(panel for panel in dashboard["panels"] if panel["title"] == "Trip route")
        self.assertTrue(all("$trip" in target["expr"] for target in route["targets"]))

    def test_view_panel_ids_are_unique_and_generated_files_are_current(self) -> None:
        for view, filename in (("live", "grafana-live.json"), ("trips", "grafana-trips.json")):
            dashboard = build_dashboard(view)
            panel_ids = [panel["id"] for panel in dashboard["panels"]]
            self.assertEqual(len(panel_ids), len(set(panel_ids)))
            with (MONITORING / filename).open(encoding="utf-8") as stream:
                generated = json.load(stream)
            self.assertEqual(generated, dashboard)

    def test_unknown_view_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_dashboard("unknown")


if __name__ == "__main__":
    unittest.main()
