import json
import sqlite3
import sys
import unittest
from pathlib import Path


MONITORING = Path(__file__).parent
REPOSITORY = MONITORING.parent
sys.path.insert(0, str(MONITORING))
sys.path.insert(0, str(REPOSITORY / "tools"))

from build_dashboard import build_dashboard  # noqa: E402
from package_ota_release import (  # noqa: E402
    _credential_signatures,
    _configured_credentials,
    _configured_server_paths,
)


class DashboardViewsTest(unittest.TestCase):
    def test_generated_and_checked_in_exports_exclude_configured_private_values(self) -> None:
        if not (REPOSITORY / "local_config.h").exists():
            self.skipTest("private firmware configuration is unavailable for local-value scan")

        private_values = _configured_credentials() | _configured_server_paths()
        private_signatures = {
            signature
            for value in private_values
            for signature in _credential_signatures(value)
        }
        artifacts = {
            "combined": "grafana-dashboard.json",
            "live": "grafana-live.json",
            "trips": "grafana-trips.json",
        }
        for view, filename in artifacts.items():
            with self.subTest(view=view):
                generated = json.dumps(build_dashboard(view), sort_keys=True).encode()
                checked_in = (MONITORING / filename).read_bytes()
                content = generated + b"\n" + checked_in
                contains_private_value = any(
                    signature and signature in content
                    for signature in private_signatures
                )
                self.assertFalse(
                    contains_private_value,
                    "Grafana export contains a configured private value",
                )

    def test_published_dashboard_links_do_not_expose_private_archive_origins(self) -> None:
        for view in ("live", "trips", "combined"):
            with self.subTest(view=view):
                dashboard = build_dashboard(view)
                self.assertTrue(
                    all(not link["url"].startswith(("https://", "http://"))
                        for link in dashboard.get("links", []))
                )
                self.assertNotIn(
                    "Raw Freematics trip archive",
                    {link["title"] for link in dashboard.get("links", [])},
                )

    def test_live_view_is_current_and_does_not_require_trip_selection(self) -> None:
        dashboard = build_dashboard("live")
        self.assertTrue(dashboard["liveNow"])
        self.assertIn("2s", dashboard["timepicker"]["refresh_intervals"])
        self.assertEqual(dashboard["uid"], "freematics-live")
        self.assertEqual(dashboard["time"], {"from": "now-5m", "to": "now"})
        road_speed = next(panel for panel in dashboard["panels"] if panel["id"] == 21)
        self.assertEqual(road_speed["fieldConfig"]["defaults"]["custom"]["lineInterpolation"], "linear")
        chart_options = road_speed["fieldConfig"]["defaults"]["custom"]
        self.assertFalse(chart_options["insertNulls"])
        self.assertFalse(chart_options["spanNulls"])
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

    def test_live_voltage_chart_separates_supply_and_ecu_with_independent_stale_masks(self) -> None:
        dashboard = build_dashboard("live")
        panel = next(panel for panel in dashboard["panels"] if panel["id"] == 51)
        self.assertEqual(panel["title"], "Vehicle supply and ECU voltage")
        self.assertEqual(panel["fieldConfig"]["defaults"]["unit"], "volt")
        self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["spanNulls"])
        self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["insertNulls"])

        supply, ecu = panel["targets"]
        self.assertEqual(supply["legendFormat"], "Vehicle supply (Model B input)")
        self.assertEqual(ecu["legendFormat"], "ECU control-module voltage (PID 0x042)")
        self.assertIn("freematics_device_battery_voltage_volts", supply["expr"])
        self.assertIn("freematics_device_battery_voltage_age_seconds", supply["expr"])
        self.assertIn("> 1", supply["expr"])
        self.assertIn("and on(device_id,trip_id)", supply["expr"])
        self.assertNotIn("freematics_device_data_age_seconds", supply["expr"])
        self.assertNotIn("freematics_obd_value", supply["expr"])
        self.assertNotIn("pid=", supply["expr"])

        self.assertIn('freematics_obd_value{device_id="$device",pid="0x042"}', ecu["expr"])
        self.assertIn("freematics_obd_value_age_seconds", ecu["expr"])
        self.assertIn('pid!~"0x10C|0x10D"', ecu["expr"])
        self.assertIn("> 2", ecu["expr"])
        self.assertIn("> 0.5", ecu["expr"])
        self.assertNotIn("> 15", ecu["expr"])
        self.assertIn("on(device_id,trip_id,pid)", ecu["expr"])
        self.assertNotIn("freematics_device_battery_voltage_volts", ecu["expr"])
        self.assertIn("never substituted for each other", panel["description"])

        combined = build_dashboard("combined")
        self.assertNotIn(51, {item["id"] for item in combined["panels"]})

    def test_live_core_pid_charts_use_faster_freshness_limit_than_other_pids(self) -> None:
        dashboard = build_dashboard("live")
        speed_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 21)
        rpm = next(target for target in speed_panel["targets"] if target["legendFormat"] == "Engine RPM")
        speed = next(target for target in speed_panel["targets"] if target["legendFormat"] == "OBD speed (mph)")
        for series in (rpm, speed):
            self.assertIn('pid=~"0x10C|0x10D"', series["expr"])
            self.assertIn("> 0.5", series["expr"])
            self.assertNotIn("> 15", series["expr"])

        coolant_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 24)
        coolant = coolant_panel["targets"][0]
        self.assertIn('pid!~"0x10C|0x10D"', coolant["expr"])
        self.assertIn("> 2", coolant["expr"])
    def test_live_view_surfaces_obd_quality_metrics(self) -> None:
        dashboard = build_dashboard("live")
        quality_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 45)
        expressions = {target["expr"] for target in quality_panel["targets"]}
        queue = next(panel for panel in dashboard["panels"] if panel["id"] == 46)
        self.assertTrue(any("freematics_device_queue_readings" in target["expr"] for target in queue["targets"]))
        self.assertTrue(any("freematics_device_queue_bytes" in target["expr"] for target in queue["targets"]))
        self.assertTrue(any("freematics_obd_state{" in expression for expression in expressions))
        self.assertTrue(any("freematics_obd_last_latency_milliseconds{" in expression for expression in expressions))
        self.assertEqual(quality_panel["datasource"]["uid"], "freematics-prometheus")
        scan = next(panel for panel in dashboard["panels"] if panel["id"] == 47)
        self.assertIn("freematics_diagnostic_trouble_codes_state", scan["targets"][0]["expr"])
        queue = next(panel for panel in dashboard["panels"] if panel["id"] == 46)
        self.assertGreaterEqual(scan["gridPos"]["y"], queue["gridPos"]["y"] + queue["gridPos"]["h"])

        expected_layout = {
            21: {"h": 10, "w": 20, "x": 0, "y": 3},
            43: {"h": 3, "w": 4, "x": 20, "y": 3},
            51: {"h": 7, "w": 24, "x": 0, "y": 13},
            45: {"h": 7, "w": 24, "x": 0, "y": 59},
            46: {"h": 5, "w": 24, "x": 0, "y": 66},
            47: {"h": 3, "w": 6, "x": 0, "y": 71},
        }
        layout = {panel["id"]: panel["gridPos"] for panel in dashboard["panels"]}
        for panel_id, grid_pos in expected_layout.items():
            self.assertEqual(layout[panel_id], grid_pos)

        self.assertEqual(layout[47]["y"], layout[46]["y"] + layout[46]["h"])

        operating = next(panel for panel in dashboard["panels"] if panel["id"] == 28)
        self.assertTrue(
            all("freematics_device_data_age_seconds" in target["expr"] for target in operating["targets"])
        )
        acceleration = next(panel for panel in dashboard["panels"] if panel["id"] == 22)
        self.assertIn("freematics_device_data_age_seconds", acceleration["targets"][0]["expr"])
        thermal = next(panel for panel in dashboard["panels"] if panel["id"] == 24)
        self.assertIn("freematics_device_data_age_seconds", thermal["targets"][1]["expr"])
        health = next(panel for panel in dashboard["panels"] if panel["id"] == 30)
        self.assertTrue(all("freematics_device_data_age_seconds" in target["expr"] for target in health["targets"][:2]))
        self.assertIn("Degraded", json.dumps(quality_panel))
        self.assertIn("ISO 15765 11-bit 500 kbps", json.dumps(quality_panel))

    def test_combined_view_keeps_historical_device_series(self) -> None:
        dashboard = build_dashboard("combined")
        for panel_id in (22, 24, 30):
            panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
            self.assertTrue(all("freematics_device_data_age_seconds" not in target["expr"] for target in panel["targets"][:2]))


    def test_trips_view_has_historical_selector_and_route_evidence(self) -> None:
        dashboard = build_dashboard("trips")
        self.assertEqual(dashboard["uid"], "freematics-trips")
        self.assertEqual(dashboard["graphTooltip"], 2)
        self.assertEqual(dashboard["time"], {"from": "now-90d", "to": "now"})
        self.assertFalse(dashboard["liveNow"])
        self.assertEqual(dashboard["timepicker"]["refresh_intervals"], ["30s", "1m", "5m", "15m"])
        self.assertEqual([item["name"] for item in dashboard["templating"]["list"]], ["device", "trip"])
        self.assertIsInstance(dashboard["templating"]["list"][0]["query"], str)
        self.assertIsInstance(dashboard["templating"]["list"][1]["query"], str)
        self.assertFalse(dashboard["templating"]["list"][0]["current"]["selected"])
        self.assertFalse(dashboard["templating"]["list"][1]["current"]["selected"])
        self.assertNotIn("20260827-001247", json.dumps(dashboard))
        titles = {panel["title"] for panel in dashboard["panels"]}
        self.assertIn("Trip archive — click a trip to inspect", titles)
        self.assertIn("Trip route", titles)
        archive_panel = next(panel for panel in dashboard["panels"] if panel["id"] == 19)
        self.assertEqual(archive_panel["datasource"]["uid"], "freematics-history")
        self.assertEqual(archive_panel["datasource"]["type"], "frser-sqlite-datasource")
        archive_sql = archive_panel["targets"][0]["queryText"]
        self.assertIn("FROM trip", archive_sql)
        self.assertIn("timeline_start_ms", archive_sql)
        self.assertIn("${device:sqlstring}", archive_sql)
        for quality_field in ("gps_fix_count", "gps_poor_quality_count", "speed_disagreement_count"):
            self.assertIn(quality_field, archive_sql)
        integrity = next(panel for panel in dashboard["panels"] if panel["id"] == 48)
        self.assertEqual(integrity["datasource"]["uid"], "freematics-history")
        integrity_sql = integrity["targets"][0]["queryText"]
        for column in ("content_sha256", "byte_size", "processed_bytes", "sealed", "mutation_detected"):
            self.assertIn(column, integrity_sql)
        trip_index = next(panel for panel in dashboard["panels"] if panel["id"] == 19)
        trip_index_sql = trip_index["targets"][0]["queryText"]
        self.assertIn("ORDER BY trip_id DESC", trip_index_sql)
        self.assertIn('over_target_interval_count AS "Observed intervals >250 ms"', trip_index_sql)
        capture_evidence = next(panel for panel in dashboard["panels"] if panel["id"] == 42)
        self.assertIn("Observed intervals over 250 ms", capture_evidence["description"])
        self.assertIn('"Trip observed intervals >250 ms"', capture_evidence["targets"][0]["queryText"])
        self.assertIn("sample_over_target_intervals AS g", capture_evidence["targets"][0]["queryText"])
        self.assertIn('"Missing IDs before sample"', capture_evidence["targets"][0]["queryText"])
        self.assertIn("sample_capture_sequence_gaps AS cg", capture_evidence["targets"][0]["queryText"])
        route = next(panel for panel in dashboard["panels"] if panel["title"] == "Trip route")
        self.assertEqual(route["datasource"]["uid"], "freematics-history")
        self.assertTrue(all("${trip:sqlstring}" in target["queryText"] for target in route["targets"]))
        self.assertTrue(all("timeline_ms" in target["queryText"] for target in route["targets"]))
        self.assertEqual(route["targets"][0]["queryType"], "time series")
        self.assertEqual(route["targets"][0]["timeColumns"], ["time"])
        self.assertNotIn("transformations", route)

        expected_layout = {
            38: {"h": 5, "w": 24, "x": 0, "y": 58},
            31: {"h": 10, "w": 24, "x": 0, "y": 63},
            39: {"h": 7, "w": 12, "x": 0, "y": 73},
            40: {"h": 7, "w": 12, "x": 12, "y": 73},
            42: {"h": 8, "w": 24, "x": 0, "y": 80},
            44: {"h": 8, "w": 24, "x": 0, "y": 88},
            48: {"h": 8, "w": 24, "x": 0, "y": 96},
            50: {"h": 7, "w": 24, "x": 0, "y": 39},
            52: {"h": 7, "w": 24, "x": 0, "y": 46},
            53: {"h": 5, "w": 24, "x": 0, "y": 53},
        }
        layout = {panel["id"]: panel["gridPos"] for panel in dashboard["panels"]}
        for panel_id in (22, 24):
            panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
            self.assertNotIn("freematics_device_data_age_seconds", panel["targets"][0]["queryText"])
        for panel_id, grid_pos in expected_layout.items():
            self.assertEqual(layout[panel_id], grid_pos)

        self.assertEqual(
            {panel["id"]: panel["title"] for panel in dashboard["panels"] if panel["id"] in {23, 24, 25, 26, 39}},
            {
                23: "Engine load and throttle",
                24: "Engine temperatures",
                25: "Fuel level and trim",
                26: "Mass airflow",
                39: "Timing and equivalence ratio",
            },
        )
        gps_quality = next(panel for panel in dashboard["panels"] if panel["id"] == 27)
        self.assertIn("GPS speed (mph)", json.dumps(gps_quality["fieldConfig"]["overrides"]))
        fuel_rate = next(panel for panel in dashboard["panels"] if panel["id"] == 38)
        self.assertIn("Mass airflow", json.dumps(fuel_rate["fieldConfig"]["overrides"]))

    def test_historical_timeseries_do_not_bridge_missing_measurements(self) -> None:
        dashboard = build_dashboard("trips")
        historical_series = [
            panel for panel in dashboard["panels"]
            if panel["type"] == "timeseries"
            and panel.get("datasource", {}).get("uid") == "freematics-history"
        ]
        self.assertTrue(historical_series)
        for panel in historical_series:
            with self.subTest(panel=panel["title"]):
                custom = panel["fieldConfig"]["defaults"]["custom"]
                self.assertFalse(custom["spanNulls"])
                self.assertFalse(custom["insertNulls"])

    def test_historical_trip_charts_insert_null_rows_only_for_capture_gaps(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript((MONITORING.parent / "collector" / "history_schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timestamp_quality, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'TRIP', '/data/CAR/TRIP.txt', 1, 'partial', 1, 1)"
            )
            # 250 ms is nominal cadence and 300 ms can be scheduling jitter.
            # A 500 ms gap indicates a missing frame in older archives without
            # capture IDs; a sequence hole catches a loss at normal cadence.
            for sequence, timeline in enumerate((1000, 1250, 1550, 1800, 2300, 2550)):
                connection.execute(
                    "INSERT INTO sample(device_id, trip_id, sequence, device_monotonic_ms, timeline_ms, archive_mtime_ms, timestamp_quality, capture_session_id, capture_sequence) "
                    "VALUES ('CAR', 'TRIP', ?, ?, ?, 1, 'partial', 'SESSION', ?)",
                    (sequence, timeline, timeline, (1, 2, 3, 4, 5, 7)[sequence]),
                )
            dashboard = build_dashboard("trips")
            for panel_id in (21, 22, 23, 24, 25, 26, 27, 38, 39, 50):
                with self.subTest(panel_id=panel_id):
                    panel = next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)
                    sql = panel["targets"][0]["queryText"]
                    for variable, value in {
                        "${device:sqlstring}": "'CAR'",
                        "${trip:sqlstring}": "'TRIP'",
                        "$__from": "0",
                        "$__to": "9999",
                    }.items():
                        sql = sql.replace(variable, value)
                    rows = connection.execute(sql).fetchall()
                    gap_rows = [row for row in rows if row[0] == 2.05]
                    self.assertEqual(len(gap_rows), 1)
                    self.assertTrue(all(value is None for value in gap_rows[0][1:]))
                    sequence_gap_rows = [row for row in rows if row[0] == 2.425]
                    self.assertEqual(len(sequence_gap_rows), 1)
                    self.assertTrue(all(value is None for value in sequence_gap_rows[0][1:]))
                    self.assertFalse(any(row[0] == 1.4 for row in rows))
                    self.assertIn(1.0, [row[0] for row in rows])
                    self.assertIn(1.25, [row[0] for row in rows])
                    self.assertIn(1.55, [row[0] for row in rows])
                    self.assertIn(1.8, [row[0] for row in rows])
                    self.assertIn(2.3, [row[0] for row in rows])
                    self.assertIn(2.55, [row[0] for row in rows])
        finally:
            connection.close()

    def test_live_views_keep_their_existing_hover_behavior(self) -> None:
        for view in ("live", "combined"):
            with self.subTest(view=view):
                self.assertEqual(build_dashboard(view)["graphTooltip"], 1)

    def test_live_view_is_prometheus_only(self) -> None:
        dashboard = build_dashboard("live")
        datasources = {
            panel.get("datasource", {}).get("uid")
            for panel in dashboard["panels"]
            if isinstance(panel.get("datasource"), dict)
        }
        self.assertEqual(datasources, {"freematics-prometheus"})

    def test_trip_index_includes_unknown_display_time(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            schema_path = MONITORING.parent / "collector" / "history_schema.sql"
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timestamp_quality, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'UNKNOWN', '/data/CAR/UNKNOWN.txt', 1000, 'unknown', 1000, 1000)"
            )
            panel = next(panel for panel in build_dashboard("trips")["panels"] if panel["id"] == 19)
            sql = panel["targets"][0]["queryText"]
            sql = sql.replace("${device:sqlstring}", "'CAR'").replace("$__from", "0").replace("$__to", "9999")
            rows = connection.execute(sql).fetchall()
            self.assertEqual(rows[0][0], "UNKNOWN")
            self.assertIsNone(rows[0][2])
        finally:
            connection.close()

    def test_archive_integrity_query_returns_index_record(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            schema_path = MONITORING.parent / "collector" / "history_schema.sql"
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timestamp_quality, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'TRIP', '/data/CAR/TRIP.txt', 1000, 'partial', 1000, 1000)"
            )
            connection.execute(
                "INSERT INTO ingest_file(archive_path, content_sha256, byte_size, processed_bytes, sealed, mutation_detected, indexed_at_ms) "
                "VALUES ('/data/CAR/TRIP.txt', 'abc123', 100, 100, 1, 0, 1000)"
            )
            panel = next(panel for panel in build_dashboard("trips")["panels"] if panel["id"] == 48)
            sql = panel["targets"][0]["queryText"]
            sql = sql.replace("${device:sqlstring}", "'CAR'").replace("${trip:sqlstring}", "'TRIP'")
            rows = connection.execute(sql).fetchall()
            self.assertEqual(rows, [("/data/CAR/TRIP.txt", "abc123", 100, 100, 1, 0, "1970-01-01 00:00:01")])
        finally:
            connection.close()

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

    def test_historical_voltage_chart_uses_archive_timeline_and_keeps_missing_values_gapped(self) -> None:
        dashboard = build_dashboard("trips")
        panel = next(panel for panel in dashboard["panels"] if panel["id"] == 50)
        self.assertEqual(panel["title"], "Device input and ECU voltage")
        self.assertEqual(panel["datasource"]["uid"], "freematics-history")
        self.assertEqual(panel["targets"][0]["queryType"], "time series")
        self.assertEqual(panel["targets"][0]["timeColumns"], ["time"])
        self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["spanNulls"])
        self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["insertNulls"])
        self.assertEqual(panel["fieldConfig"]["defaults"]["custom"]["showPoints"], "always")

        sql = panel["targets"][0]["queryText"]
        self.assertIn("s.timeline_ms / 1000.0 AS time", sql)
        self.assertIn("m.pid = '0x024'", sql)
        self.assertIn("m.pid = '0x042'", sql)
        self.assertNotIn("collector_received_ms", sql)
        self.assertNotIn("archive_mtime_ms", sql)

        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript((MONITORING.parent / "collector" / "history_schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timeline_start_ms, timeline_end_ms, timestamp_quality, sample_count, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'TRIP', '/data/CAR/TRIP.txt', 1000, 1000, 3000, 'gnss', 3, 9000000, 9000000)"
            )
            for sequence, timeline_ms in enumerate((1000, 2000, 3000)):
                connection.execute(
                    "INSERT INTO sample(device_id, trip_id, sequence, device_monotonic_ms, capture_utc_ms, timeline_ms, collector_received_ms, archive_mtime_ms, timestamp_quality) "
                    "VALUES ('CAR', 'TRIP', ?, ?, ?, ?, ?, ?, 'gnss')",
                    (sequence, sequence * 1000, timeline_ms + 10, timeline_ms, 8000000 + sequence, 9000000),
                )
            connection.executemany(
                "INSERT INTO sample_metric(device_id, trip_id, sequence, pid, numeric_value) VALUES ('CAR', 'TRIP', ?, ?, ?)",
                ((0, "0x024", 1380), (0, "0x042", 14.1), (2, "0x024", 1240), (2, "0x042", 13.2)),
            )
            for variable, value in {
                "${device:sqlstring}": "'CAR'",
                "${trip:sqlstring}": "'TRIP'",
                "$__from": "0",
                "$__to": "9999999999999",
            }.items():
                sql = sql.replace(variable, value)
            rows = connection.execute(sql).fetchall()
            self.assertEqual(rows, [
                (1.0, 13.8, 14.1),
                (1.5, None, None),
                (2.0, None, None),
                (2.5, None, None),
                (3.0, 12.4, 13.2),
            ])
        finally:
            connection.close()

    def test_voltage_waveform_uses_device_capture_ticks_and_marks_missing_intervals(self) -> None:
        panel = next(panel for panel in build_dashboard("trips")["panels"] if panel["id"] == 52)
        self.assertEqual(panel["title"], "Device supply voltage — raw waveform")
        self.assertEqual(panel["datasource"]["uid"], "freematics-history")
        self.assertEqual(panel["targets"][0]["queryType"], "time series")
        self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["spanNulls"])
        self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["insertNulls"])
        self.assertEqual(panel["fieldConfig"]["defaults"]["custom"]["showPoints"], "always")

        sql = panel["targets"][0]["queryText"]
        self.assertIn("f.pid = '0x0A0'", sql)
        self.assertIn("s.device_monotonic_ms", sql)
        self.assertIn("LAG(point_time_ms)", sql)
        self.assertIn("point_time_ms - previous_time_ms >= 40", sql)
        self.assertNotIn("collector_received_ms", sql)
        self.assertNotIn("archive_mtime_ms", sql)

        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript((MONITORING.parent / "collector" / "history_schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timeline_start_ms, timeline_end_ms, timestamp_quality, sample_count, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'TRIP', '/data/CAR/TRIP.txt', 1, 10000, 10130, 'gnss', 3, 9000000, 9000000)"
            )
            sample_rows = (
                (0, 4294967290, 10000, "4294967295;1200"),
                (1, 5, 10016, "15;1198"),
                (2, 105, 10116, "55;1170"),
                (3, 205, 10216, "bad;1400"),
            )
            for sequence, monotonic, timeline, raw in sample_rows:
                connection.execute(
                    "INSERT INTO sample(device_id, trip_id, sequence, device_monotonic_ms, timeline_ms, collector_received_ms, archive_mtime_ms, timestamp_quality) "
                    "VALUES ('CAR', 'TRIP', ?, ?, ?, 8000000000000, 9000000, 'gnss')",
                    (sequence, monotonic, timeline),
                )
                connection.execute(
                    "INSERT INTO sample_field(device_id, trip_id, sequence, ordinal, pid, text_value) "
                    "VALUES ('CAR', 'TRIP', ?, 0, '0x0A0', ?)",
                    (sequence, raw),
                )
            for variable, value in {
                "${device:sqlstring}": "'CAR'",
                "${trip:sqlstring}": "'TRIP'",
                "$__from": "0",
                "$__to": "9999999999999",
            }.items():
                sql = sql.replace(variable, value)
            rows = connection.execute(sql).fetchall()
            self.assertEqual(rows, [
                (10.005, 12.0),
                (10.026, 11.98),
                (10.046, None),
                (10.066, 11.7),
            ])
        finally:
            connection.close()

    def test_waveform_loss_panel_reports_exact_counters_and_ignores_malformed_fields(self) -> None:
        panel = next(panel for panel in build_dashboard("trips")["panels"] if panel["id"] == 53)
        self.assertEqual(panel["title"], "Waveform loss counters")
        self.assertEqual(panel["datasource"]["uid"], "freematics-history")
        self.assertEqual(panel["targets"][0]["queryType"], "time series")
        self.assertIn("No records means the counter was not reported", panel["description"])
        self.assertIn("do not separate those causes", panel["description"])
        custom = panel["fieldConfig"]["defaults"]["custom"]
        self.assertFalse(custom["spanNulls"])
        self.assertFalse(custom["insertNulls"])
        self.assertEqual(custom["showPoints"], "never")

        sql = panel["targets"][0]["queryText"]
        for label in (
            "Dropped voltage (unverified append or buffer overflow)",
            "Dropped motion (unverified append or buffer overflow)",
            "Invalid voltage readings",
            "Invalid motion readings",
        ):
            self.assertIn(label, sql)
        self.assertNotIn("collector_received_ms", sql)
        self.assertNotIn("archive_mtime_ms", sql)

        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript((MONITORING.parent / "collector" / "history_schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, timeline_start_ms, timeline_end_ms, timestamp_quality, sample_count, archive_mtime_ms, updated_at_ms) "
                "VALUES ('CAR', 'TRIP', '/data/CAR/TRIP.txt', 1, 1000, 2000, 'gnss', 5, 9000000, 9000000)"
            )
            loss_fields = (
                (0, 1000, "0;0;0;0"),
                (1, 1250, "2;1;0;0"),
                (2, 1500, "2;1;1;0"),
                (3, 1750, "1;2;3;4;5"),
                (4, 2000, "bad;0;0;0"),
            )
            for sequence, timeline, raw in loss_fields:
                connection.execute(
                    "INSERT INTO sample(device_id, trip_id, sequence, device_monotonic_ms, timeline_ms, collector_received_ms, archive_mtime_ms, timestamp_quality) "
                    "VALUES ('CAR', 'TRIP', ?, ?, ?, 8000000000000, 9000000, 'gnss')",
                    (sequence, sequence * 250, timeline),
                )
                connection.execute(
                    "INSERT INTO sample_field(device_id, trip_id, sequence, ordinal, pid, text_value) "
                    "VALUES ('CAR', 'TRIP', ?, 0, '0x0A4', ?)",
                    (sequence, raw),
                )
            for variable, value in {
                "${device:sqlstring}": "'CAR'",
                "${trip:sqlstring}": "'TRIP'",
                "$__from": "0",
                "$__to": "9999999999999",
            }.items():
                sql = sql.replace(variable, value)
            rows = connection.execute(sql).fetchall()
            self.assertEqual(rows, [
                (1.0, 0, 0, 0, 0),
                (1.25, 2, 1, 0, 0),
                (1.5, 2, 1, 1, 0),
            ])
        finally:
            connection.close()

    def test_view_panel_ids_are_unique_and_generated_files_are_current(self) -> None:
        for view, filename in (
            ("combined", "grafana-dashboard.json"),
            ("live", "grafana-live.json"),
            ("trips", "grafana-trips.json"),
        ):
            dashboard = build_dashboard(view)
            panel_ids = [panel["id"] for panel in dashboard["panels"]]
            self.assertEqual(len(panel_ids), len(set(panel_ids)))
            with (MONITORING / filename).open(encoding="utf-8") as stream:
                generated = json.load(stream)
            self.assertEqual(generated, dashboard)
            if view == "combined":
                diagnostic = next(panel for panel in dashboard["panels"] if panel["id"] == 29)
                self.assertEqual(diagnostic["gridPos"], {"h": 5, "w": 6, "x": 6, "y": 39})

    def test_unknown_view_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_dashboard("unknown")


if __name__ == "__main__":
    unittest.main()
