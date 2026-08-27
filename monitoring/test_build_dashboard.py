import json
import sqlite3
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
        self.assertIsInstance(dashboard["templating"]["list"][0]["query"], str)
        self.assertIsInstance(dashboard["templating"]["list"][1]["query"], str)
        titles = {panel["title"] for panel in dashboard["panels"]}
        self.assertIn("Trip index", titles)
        self.assertIn("Trip route", titles)
        archive_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 41)
        self.assertEqual(archive_panel["datasource"]["uid"], "freematics-history")
        self.assertEqual(archive_panel["datasource"]["type"], "frser-sqlite-datasource")
        archive_sql = archive_panel["targets"][0]["queryText"]
        self.assertIn("FROM trip", archive_sql)
        self.assertIn("timeline_start_ms BETWEEN", archive_sql)
        self.assertIn("$__from", archive_sql)
        self.assertIn("$__to", archive_sql)
        self.assertIn("${device:sqlstring}", archive_sql)
        route = next(panel for panel in dashboard["panels"] if panel["title"] == "Trip route")
        self.assertEqual(route["datasource"]["uid"], "freematics-history")
        self.assertTrue(all("${trip:sqlstring}" in target["queryText"] for target in route["targets"]))
        self.assertTrue(all("timeline_ms" in target["queryText"] for target in route["targets"]))

    def test_live_view_is_prometheus_only(self) -> None:
        dashboard = build_dashboard("live")
        datasources = {
            panel.get("datasource", {}).get("uid")
            for panel in dashboard["panels"]
            if isinstance(panel.get("datasource"), dict)
        }
        self.assertEqual(datasources, {"freematics-prometheus"})

    def test_historical_sql_compiles_against_archive_schema(self) -> None:
        schema_path = MONITORING.parent / "collector" / "history_schema.sql"
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            dashboard = build_dashboard("trips")
            history_targets = [
                target
                for panel in dashboard["panels"]
                if panel.get("datasource", {}).get("uid") == "freematics-history"
                for target in panel.get("targets", [])
            ]
            self.assertGreaterEqual(len(history_targets), 20)
            for target in history_targets:
                sql = target["queryText"]
                for variable, value in {
                    "${device:sqlstring}": "'ZKUCALJ0'",
                    "${trip:sqlstring}": "'20260827-001247'",
                    "$__from": "0",
                    "$__to": "9999999999999",
                }.items():
                    sql = sql.replace(variable, value)
                connection.execute(sql).fetchall()
        finally:
            connection.close()

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
