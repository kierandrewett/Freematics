#!/usr/bin/env python3
"""End-to-end tests for the versioned waveform archive projection."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from history_indexer import HistoryIndexer
from telemetry_catalog import readable_metrics
from waveforms import waveform_trip_summary, waveform_window


class WaveformWindowTest(unittest.TestCase):
    def test_catalogue_decodes_waveform_fields_without_discarding_raw_values(self) -> None:
        metrics = readable_metrics([
            {"pid": "0x0A0", "value": "1000;1234"},
            {"pid": "0x0A4", "value": "1;2;3;4"},
            {"pid": "0x0A5", "value": "1"},
        ])
        self.assertEqual(metrics["waveform_voltage"]["value"], {"device_monotonic_ms": 1000, "centivolts": 1234})
        self.assertEqual(metrics["waveform_losses"]["value"]["invalid_motion"], 4)
        self.assertEqual(metrics["waveform_format"]["value"], 1)

    def indexed_window(self, raw: str) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory[str]]:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name) / "data"
        archive = root / "CAR" / "2026" / "10" / "02" / "20261002-120000.txt"
        archive.parent.mkdir(parents=True)
        archive.write_text(raw)
        database = Path(directory.name) / "history.sqlite"
        indexer = HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1000) + 61_000)
        self.assertEqual(indexer.index_once(), 1)
        return sqlite3.connect(database), directory

    def test_decodes_repeated_ordered_groups_and_aligns_each_clock(self) -> None:
        conn, directory = self.indexed_window(
            "0:1000,11:121026,10:12000000,A5:1,A0:990;1234,A1:980,A2:0.100000;0.200000;1.000000,"
            "A3:1.000000;2.000000;3.000000,A1:1010,A2:0.300000;0.400000;1.100000,"
            "A3:4.000000;5.000000;6.000000,A4:2;3;4;5,0:1250,A5:1,A0:1255;1200,A4:4;5;6;7"
        )
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 10)
        finally:
            conn.close()
        self.assertEqual(result["format_version"], 1)
        self.assertEqual([point["device_monotonic_ms"] for point in result["voltage"]], [990, 1255])
        self.assertEqual([point["offset_ms"] for point in result["voltage"]], [-10, 5])
        self.assertEqual(result["voltage"][0]["capture_utc_ms"], int(datetime(2026, 10, 12, 12, tzinfo=timezone.utc).timestamp() * 1000) - 10)
        self.assertEqual([point["device_monotonic_ms"] for point in result["motion"]], [980, 1010])
        self.assertEqual(result["motion"][0]["acceleration_g"], {"x": 0.1, "y": 0.2, "z": 1.0})
        self.assertEqual(result["loss_reports"][0]["losses"], {
            "voltage_overflow": 2, "motion_overflow": 3,
            "invalid_voltage": 4, "invalid_motion": 5,
        })
        self.assertEqual(result["quality"]["motion_interval_ms"]["count"], 1)
        self.assertEqual(result["quality"]["motion_interval_ms"]["maximum"], 30)
        self.assertEqual(result["quality"]["loss_counter_increase"], {
            "voltage_overflow": 2, "motion_overflow": 2,
            "invalid_voltage": 2, "invalid_motion": 2,
        })
        self.assertEqual(result["quality"]["loss_counter_provenance"]["initial_counter_before_window"], "unknown")

    def test_wraparound_keeps_nearby_acquisition_time(self) -> None:
        conn, directory = self.indexed_window(
            "0:4294967290,A5:1,A0:5;1234,A1:3,A2:1;2;3,A3:4;5;6"
        )
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 1)
            summary = waveform_trip_summary(conn, "CAR", "20261002-120000")
        finally:
            conn.close()
        self.assertEqual(result["voltage"][0]["offset_ms"], 11)
        self.assertEqual(result["motion"][0]["offset_ms"], 9)

    def test_rejects_incomplete_nonadjacent_and_invalid_groups(self) -> None:
        conn, directory = self.indexed_window(
            "0:1000,A5:1,A1:990,A2:1;2;3,10C:700,A3:4;5;6,A1:995,"
            "A2:1;2;bad,A3:4;5;6,A0:1000;nan,0:1250,A5:2,A0:1250;1200"
        )
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 10)
        finally:
            conn.close()
        self.assertEqual(result["motion"], [])
        self.assertEqual(result["voltage"], [])
        codes = {issue["code"] for issue in result["issues"]}
        self.assertTrue({"motion_group_not_adjacent", "invalid_motion_vector", "invalid_voltage", "unsupported_format"} <= codes)
        self.assertEqual(result["quality"]["coverage"]["state"], "missing")

    def test_legacy_frames_report_missing_coverage_and_pagination(self) -> None:
        conn, directory = self.indexed_window("0:1000,10C:700,0:1250,A5:1,A0:1240;1222")
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 1)
        finally:
            conn.close()
        self.assertEqual(result["voltage"], [])
        self.assertEqual(result["next_sequence"], 1)
        self.assertEqual(result["quality"]["coverage"]["state"], "missing")
        self.assertEqual(result["quality"]["coverage"]["frames_without_format"], 1)

    def test_loss_marker_does_not_claim_sensor_coverage_and_limits_motion_encoding(self) -> None:
        conn, directory = self.indexed_window(
            "0:1000,A5:1,A4:1;2;3;4,0:1250,A5:1,A1:1240,A2:65;0;1,A3:0;0;0"
        )
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 10)
        finally:
            conn.close()
        self.assertEqual(result["quality"]["coverage"]["state"], "missing")
        self.assertEqual(result["quality"]["coverage"]["sensors"]["voltage"]["empty_format_frames"], 2)
        self.assertEqual(result["quality"]["coverage"]["sensors"]["motion"]["readings"], 0)
        self.assertIn("invalid_motion_vector", {issue["code"] for issue in result["issues"]})

    def test_delayed_fifo_readings_are_retained_but_marked_out_of_window(self) -> None:
        conn, directory = self.indexed_window(
            "0:5000,A5:1,A0:1000;1200,A1:1000,A2:1;0;1,A3:0;0;0,A1:5000,A2:-1;0;1,A3:0;0;0"
        )
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 10)
            summary = waveform_trip_summary(conn, "CAR", "20261002-120000")
        finally:
            conn.close()
        self.assertEqual(result["voltage"][0]["acquisition_offset_quality"], "delayed_backlog")
        self.assertEqual(result["quality"]["acquisition_offset_quality"]["motion"]["delayed_backlog"], 1)
        self.assertEqual(summary["motion"]["extreme_candidates"], [])

    def test_middle_loss_counter_reset_makes_delta_unknown(self) -> None:
        conn, directory = self.indexed_window(
            "0:1000,A5:1,A4:10;10;10;10,0:1250,A5:1,A4:1;1;1;1,0:1500,A5:1,A4:20;20;20;20"
        )
        self.addCleanup(directory.cleanup)
        try:
            result = waveform_window(conn, "CAR", "20261002-120000", 0, 10)
        finally:
            conn.close()
        self.assertEqual(result["quality"]["loss_counter_resets"], 1)
        self.assertIsNone(result["quality"]["loss_counter_increase"])

    def test_trip_without_waveform_format_is_unavailable(self) -> None:
        conn, directory = self.indexed_window("0:1000,10C:700,0:1250,10C:720")
        self.addCleanup(directory.cleanup)
        try:
            summary = waveform_trip_summary(conn, "CAR", "20261002-120000")
        finally:
            conn.close()
        self.assertEqual(summary["availability"], "unavailable")
        self.assertEqual(summary["coverage"]["voltage"]["state"], "missing")

    def test_trip_summary_is_bounded_and_keeps_candidates_non_diagnostic(self) -> None:
        conn, directory = self.indexed_window(
            "0:1000,A5:1,A0:990;1200,A1:980,A2:0;0;1,A3:0;0;0,A1:1000,A2:1;0;1,A3:0;0;0,A4:1;2;3;4,"
            "0:1250,A5:1,A0:1240;1300,A1:1230,A2:0;1;1,A3:0;0;0,A1:1250,A2:0;2;1,A3:0;0;0,A4:2;4;6;8"
        )
        self.addCleanup(directory.cleanup)
        try:
            summary = waveform_trip_summary(conn, "CAR", "20261002-120000")
        finally:
            conn.close()
        self.assertEqual(summary["availability"], "available")
        self.assertEqual(summary["voltage"]["minimum_volts"], 12.0)
        self.assertEqual(summary["motion"]["cadence"]["gaps_over_50ms"], 1)
        self.assertTrue(summary["motion"]["extreme_candidates"])
        self.assertEqual(summary["motion"]["extreme_candidates"][0]["acceleration_axis_span_g"], 1.0)
        self.assertEqual(summary["data_quality"]["loss_counter_increase"]["invalid_motion"], 4)
        self.assertEqual(summary["data_quality"]["loss_counter_provenance"]["initial_counter_before_trip"], "unknown")

    def test_replayed_frame_stays_raw_but_is_deduplicated_in_summary(self) -> None:
        first = "0:1000,A5:1,A0:990;1200,A1:980,A2:-1;0;1,A3:0;0;0,A1:1000,A2:1;0;1,A3:0;0;0"
        second = "0:1250,A5:1,A0:1240;1220,A1:1230,A2:-1;0;1,A3:0;0;0,A1:1250,A2:1;0;1,A3:0;0;0"
        conn, directory = self.indexed_window(f"{first},{second},{first},{second}")
        self.addCleanup(directory.cleanup)
        try:
            raw = waveform_window(conn, "CAR", "20261002-120000", 0, 10)
            summary = waveform_trip_summary(conn, "CAR", "20261002-120000")
        finally:
            conn.close()
        self.assertEqual(len(raw["motion"]), 8)
        self.assertEqual(summary["motion"]["readings"], 4)
        self.assertEqual(summary["motion"]["cadence"]["non_monotonic"], 0)
        self.assertEqual(summary["data_quality"]["duplicate_frames"], 2)
        self.assertEqual(summary["frames"]["after_replay_deduplication"], 2)
        self.assertEqual(summary["coverage"]["motion"]["state"], "observed")


if __name__ == "__main__":
    unittest.main()
