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
        odometer = next(panel for panel in dashboard["panels"] if panel["id"] == 43)
        self.assertIn('pid="0x1A6"', odometer["targets"][0]["expr"])
        expressions = [target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])]
        self.assertTrue(expressions)
        self.assertTrue(all("$trip" not in expression for expression in expressions))
        historical_link = next(link for link in dashboard["links"] if link["title"] == "Historical trips view")
        self.assertNotIn("var-trip", historical_link["url"])
        dtc_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 6)
        self.assertIn("freematics_diagnostic_trouble_codes_age_seconds", dtc_panel["targets"][0]["expr"])
        self.assertIn("300", dtc_panel["targets"][0]["expr"])
    def test_live_view_surfaces_obd_quality_metrics(self) -> None:
        dashboard = build_dashboard("live")
        panel = next(panel for panel in dashboard["panels"] if panel["id"] == 45)
        expressions = {target["expr"] for target in panel["targets"]}
        queue = next(panel for panel in dashboard["panels"] if panel["id"] == 46)
        self.assertTrue(any("freematics_device_queue_readings" in target["expr"] for target in queue["targets"]))
        self.assertTrue(any("freematics_device_queue_bytes" in target["expr"] for target in queue["targets"]))
        self.assertTrue(any("freematics_obd_state{" in expression for expression in expressions))
        self.assertTrue(any("freematics_obd_last_latency_milliseconds{" in expression for expression in expressions))
        self.assertEqual(panel["datasource"]["uid"], "freematics-prometheus")
        scan = next(panel for panel in dashboard["panels"] if panel["id"] == 47)
        self.assertIn("freematics_diagnostic_trouble_codes_state", scan["targets"][0]["expr"])
        queue = next(panel for panel in dashboard["panels"] if panel["id"] == 46)
        self.assertGreaterEqual(scan["gridPos"]["y"], queue["gridPos"]["y"] + queue["gridPos"]["h"])


    def test_trips_view_has_historical_selector_and_route_evidence(self) -> None:
        dashboard = build_dashboard("trips")
        self.assertEqual(dashboard["uid"], "freematics-trips")
        self.assertEqual(dashboard["time"], {"from": "now-90d", "to": "now"})
        self.assertEqual([item["name"] for item in dashboard["templating"]["list"]], ["device", "trip"])
        self.assertIsInstance(dashboard["templating"]["list"][0]["query"], str)
        self.assertIsInstance(dashboard["templating"]["list"][1]["query"], str)
        self.assertFalse(dashboard["templating"]["list"][0]["current"]["selected"])
        self.assertFalse(dashboard["templating"]["list"][1]["current"]["selected"])
        self.assertNotIn("20260827-001247", json.dumps(dashboard))
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
        for quality_field in ("gps_fix_count", "gps_poor_quality_count", "speed_disagreement_count"):
            self.assertIn(quality_field, archive_sql)
        route = next(panel for panel in dashboard["panels"] if panel["title"] == "Trip route")
        self.assertEqual(route["datasource"]["uid"], "freematics-history")
        self.assertTrue(all("${trip:sqlstring}" in target["queryText"] for target in route["targets"]))
        self.assertTrue(all("timeline_ms" in target["queryText"] for target in route["targets"]))
        self.assertEqual(route["targets"][0]["queryType"], "time series")
        self.assertEqual(route["targets"][0]["timeColumns"], ["time"])
        self.assertNotIn("transformations", route)


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

    def test_historical_cards_use_range_stored_distance_and_latest_sequence(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            schema_path = MONITORING.parent / "collector" / "history_schema.sql"
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timeline_start_ms, timeline_end_ms, timestamp_quality, sample_count, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'TRIP', '/data/CAR/TRIP.txt', 1000, 1000, 4000, 'partial', 4, 1000, 1000)"
            )
            for sequence, timeline, acceleration in ((0, 1000, 0.2), (1, 2000, -0.6), (2, 3000, 0.4), (3, 4000, 0.1)):
                connection.execute(
                    "INSERT INTO sample(device_id, trip_id, sequence, device_monotonic_ms, timeline_ms, time_basis, archive_mtime_ms, timestamp_quality, acceleration_x_g) "
                    "VALUES ('CAR', 'TRIP', ?, ?, ?, 'collector_session', 1000, 'unknown', ?)",
                    (sequence, timeline, timeline, acceleration),
                )
            metrics = ((0, "0x030", 4.0), (2, "0x030", 5.0), (0, "0x12F", 12.0), (1, "0x12F", 8.0), (2, "0x12F", 9.0), (0, "0x10C", 900.0), (2, "0x10C", 1400.0))
            connection.executemany(
                "INSERT INTO sample_metric(device_id, trip_id, sequence, pid, numeric_value) VALUES ('CAR', 'TRIP', ?, ?, ?)",
                metrics,
            )
            connection.execute(
                "INSERT INTO sample_metric(device_id, trip_id, sequence, pid, text_value) VALUES ('CAR', 'TRIP', 3, '0x10C', 'bad')"
            )
            connection.execute(
                "INSERT INTO diagnostic_code(device_id, trip_id, sequence, status, slot, raw_code, code, system) VALUES ('CAR', 'TRIP', 1, 'stored', 0, 4660, 'P234', 'powertrain')"
            )
            dashboard = build_dashboard("trips")

            def run(panel_id: int, from_ms: int = 0, to_ms: int = 9999):
                panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
                sql = panel["targets"][0]["queryText"]
                for variable, value in {
                    "${device:sqlstring}": "'CAR'",
                    "${trip:sqlstring}": "'TRIP'",
                    "$__from": str(from_ms),
                    "$__to": str(to_ms),
                }.items():
                    sql = sql.replace(variable, value)
                return connection.execute(sql).fetchall()

            self.assertAlmostEqual(run(9)[0][0], 5.0 * 0.621371)
            self.assertEqual(run(13, 1500, 2500), [(8.0,)])
            self.assertEqual(run(14, 1500, 2500), [(8.0,)])
            self.assertEqual(run(15, 1500, 2500), [(0.0,)])
            self.assertEqual(run(16)[0][0], 0.4)
            self.assertEqual(run(17)[0][0], 0.6)
            self.assertEqual(run(40), [("0x030", 5.0), ("0x10C", 1400.0), ("0x12F", 9.0)])
            self.assertEqual(run(44)[0][3:5], ("P234", "powertrain"))
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
