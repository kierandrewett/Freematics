#!/usr/bin/env python3
"""Integration tests for contextual condition baselines.

Failure cases covered before implementation:
* unsealed, future, gapped and time-unverified archives cannot form a reference;
* a held OBD value with an excessive paired age cannot become a fresh reading;
* no interpolation or resampling can turn sparse data into a comparison;
* fewer than five comparable sealed trips reports insufficient coverage;
* a difference from prior data is reported as a measurement, never as a fault.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from condition_baselines import _context, _trip_samples, acquisition_quality, contextual_baselines
from history_indexer import HistoryIndexer
from trip_intelligence import evidence_for_trip


def frame(tick, date, clock, rpm, speed, load, coolant, maf, trim, volts, motion,
          ages=True):
    values = [f"0:{tick}"]
    if date:
        values.extend((f"11:{date}", f"10:{clock:08d}", "93:0"))
    values.extend([
        f"10C:{rpm}", f"10D:{speed}", f"104:{load}", f"105:{coolant}",
        f"110:{maf}", f"106:{trim}", f"107:1", f"24:{volts}",
        f"20:{motion};0;1", "89:1",
    ])
    if ages:
        values.extend(("40C:250", "40D:250", "404:500", "405:500", "410:500",
                       "406:500", "407:500", "94:500", "95:100"))
    return ",".join(values)


def trip_text(offset, variation=0, ages=True, gap=False, date="011026"):
    rows = []
    tick = 0
    # Cold operation, then warm idle, then warm driving. Every frame carries
    # paired acquisition ages so the test uses the real indexed evidence path.
    sections = ((850, 0, 18, 60, 3.0, 1, 1400, 0.02),
                (800, 0, 20, 90, 2.5, 2, 1400, 0.03),
                (2000, 35, 42, 90, 10.0, 3, 1400, 0.04))
    for rpm, speed, load, coolant, maf, trim, volts, motion in sections:
        for point in range(12):
            if gap and len(rows) == 6:
                tick += 5000
            rows.append(frame(tick, date, 12000000 + offset * 10000 + tick // 10,
                              rpm + (point % 3) * 2 + variation,
                              speed, load, coolant, maf + variation * .1,
                              trim + variation, volts + variation * 2,
                              motion + variation * .001, ages))
            tick += 250
    return ",".join(rows)


class ConditionBaselinesTest(unittest.TestCase):
    def build_history(self, root, database):
        for index in range(6):
            day = index + 10
            archive = root / "CAR" / "2026" / "10" / str(day) / f"202610{day}-120000.txt"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(trip_text(index, index, date=f"{day}1026"))
        current = root / "CAR" / "2026" / "10" / "16" / "20261016-120000.txt"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text(trip_text(7, 8, date="161026"))
        future = root / "CAR" / "2026" / "10" / "17" / "20261017-120000.txt"
        future.parent.mkdir(parents=True, exist_ok=True)
        future.write_text(trip_text(8, 99, date="171026"))
        gapped = root / "CAR" / "2026" / "09" / "30" / "20260930-120000.txt"
        gapped.parent.mkdir(parents=True, exist_ok=True)
        gapped.write_text(trip_text(9, 99, gap=True, date="300926"))
        unverified = root / "CAR" / "2026" / "09" / "29" / "20260929-120000.txt"
        unverified.parent.mkdir(parents=True, exist_ok=True)
        unverified.write_text(trip_text(10, 99, ages=False, date=""))
        sealed_now = max(int(path.stat().st_mtime * 1000) for path in root.rglob("*.txt")) + 61000
        indexer = HistoryIndexer(root, database, now_ms=lambda: sealed_now)
        indexer.index_once()
        # Add an archive with a valid earlier capture date after the first
        # index pass. Its mtime is later than the indexing clock, so it is
        # projected but remains unsealed and cannot become a reference.
        unsealed = root / "CAR" / "2026" / "09" / "28" / "20260928-120000.txt"
        unsealed.parent.mkdir(parents=True, exist_ok=True)
        unsealed.write_text(trip_text(11, 99, date="280926"))
        os.utime(unsealed, ((sealed_now + 60_000) / 1000, (sealed_now + 60_000) / 1000))
        indexer.index_once()
        return current.stem

    def test_compares_only_prior_sealed_same_device_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                result = contextual_baselines(history, "CAR", current)
                trip = history.execute("SELECT * FROM trip WHERE device_id='CAR' AND trip_id=?", (current,)).fetchone()
                evidence = evidence_for_trip(history, trip)
                unsealed_state = history.execute("SELECT sealed FROM ingest_file WHERE archive_path LIKE '%20260928-120000.txt'").fetchone()[0]
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["telemetry_health"]["kind"], "collection_health_not_vehicle_diagnosis")
            self.assertEqual(result["telemetry_health"]["frame_gaps_over_one_second"], 0)
            self.assertEqual(evidence["contextual_condition_baseline"]["status"], "ok")
            self.assertEqual(result["eligible_prior_trips"], 6)
            self.assertEqual(unsealed_state, 0)
            self.assertEqual(result["contexts"]["warm_idle"]["status"], "compared")
            voltage = result["contexts"]["warm_idle"]["metrics"]["supply_voltage_volts"]
            self.assertGreater(voltage["delta_from_reference_median"], 0)
            self.assertIn("outside_reference_trip_median_band", voltage)
            self.assertTrue(voltage["outside_reference_trip_median_band"])
            self.assertEqual(result["contexts"]["warm_driving"]["matched_bins"], ["rpm_2000_load_40_speed_40"])
            self.assertEqual(result["contexts"]["warm_driving"]["bins"]["rpm_2000_load_40_speed_40"]["status"], "compared")

    def test_reports_age_and_history_limits_without_promoting_held_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.execute("UPDATE sample_metric SET numeric_value=60000 WHERE trip_id=? AND sequence=0 AND pid='0x40C'", (current,))
                result = contextual_baselines(history, "CAR", current)
            self.assertGreater(result["current_acquisition_coverage"]["engine_rpm"]["stale_or_unverified"], 0)

    def test_equal_values_with_new_timestamps_meet_successive_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                history.execute("UPDATE sample_metric SET numeric_value=800 WHERE trip_id=? AND pid='0x10C' AND sequence BETWEEN 12 AND 23", (current,))
                result = contextual_baselines(history, "CAR", current)
            rpm = result["contexts"]["warm_idle"]["coverage"]["engine_rpm"]
            self.assertEqual(rpm["fresh"], 12)
            self.assertEqual(rpm["max_successive_acquisition_gap_ms"], 250)
            self.assertEqual(rpm["observed_successive_acquisition"], "met")

    def test_held_timestamp_is_deduplicated_and_does_not_prove_one_second_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                history.execute("""
                    UPDATE sample_metric SET numeric_value=(
                        SELECT device_monotonic_ms - 3000 FROM sample
                        WHERE sample.device_id=sample_metric.device_id
                          AND sample.trip_id=sample_metric.trip_id
                          AND sample.sequence=sample_metric.sequence)
                    WHERE trip_id=? AND pid='0x40C' AND sequence BETWEEN 12 AND 23
                """, (current,))
                result = contextual_baselines(history, "CAR", current)
            rpm = result["current_acquisition_coverage"]["engine_rpm"]
            self.assertEqual(rpm["fresh"], 25)
            self.assertGreater(rpm["stale_or_unverified"], 0)
            self.assertEqual(rpm["observed_successive_acquisition"], "not_met")

    def test_insufficient_references_and_current_context_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                history.execute("UPDATE ingest_file SET sealed=0 WHERE archive_path LIKE '%20261010-120000.txt' OR archive_path LIKE '%20261011-120000.txt'")
                history.execute("UPDATE sample_metric SET numeric_value=60000 WHERE trip_id=? AND pid='0x40C' AND sequence BETWEEN 12 AND 23", (current,))
                result = contextual_baselines(history, "CAR", current)
            self.assertEqual(result["eligible_prior_trips"], 4)
            self.assertEqual(result["contexts"]["cold_idle"]["status"], "insufficient_reference_history")
            self.assertEqual(result["contexts"]["warm_idle"]["status"], "insufficient_current_coverage")

    def test_device_tick_reset_starts_a_new_acquisition_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            archive = root / "RESET" / "2026" / "10" / "20" / "20261020-120000.txt"
            archive.parent.mkdir(parents=True)
            rows = [frame(1000 + point * 250, "201026", 12000000 + point * 25, 800, 0, 20, 90, 2.5, 1, 1400, .02)
                    for point in range(4)]
            rows.extend(frame(point * 250, "201026", 12000100 + point * 25, 800, 0, 20, 90, 2.5, 1, 1400, .02)
                        for point in range(4))
            archive.write_text(",".join(rows))
            HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1000) + 61000).index_once()
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                samples = _trip_samples(history, "RESET", archive.stem)
                quality = acquisition_quality(history, "RESET", archive.stem, samples)
            self.assertEqual([sample["epoch"] for sample in samples], [0, 0, 0, 0, 1, 1, 1, 1])
            self.assertEqual(quality["acquisition_coverage"]["engine_rpm"]["one_second_full_coverage"], "not_met")

    def test_uint32_tick_wrap_keeps_one_epoch_and_uses_modular_acquisition_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            archive = root / "WRAP" / "2026" / "10" / "21" / "20261021-120000.txt"
            archive.parent.mkdir(parents=True)
            ticks = (0xFFFFFF00, 0xFFFFFFFA, 100, 350)
            rows = [frame(tick, "211026", 12000000 + index * 25, 800, 0, 20, 90, 2.5, 1, 1400, .02)
                    for index, tick in enumerate(ticks)]
            archive.write_text(",".join(rows))
            HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1000) + 61000).index_once()
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                samples = _trip_samples(history, "WRAP", archive.stem)
                quality = acquisition_quality(history, "WRAP", archive.stem)
            self.assertEqual([sample["epoch"] for sample in samples], [0, 0, 0, 0])
            self.assertTrue(all(sample["quality"]["engine_rpm"] == "fresh" for sample in samples))
            self.assertEqual(quality["acquisition_coverage"]["engine_rpm"]["one_second_full_coverage"], "met")

    def test_held_half_second_acquisitions_meet_full_one_second_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                history.execute("""
                    UPDATE sample_metric SET numeric_value=(
                        SELECT CASE WHEN sequence % 2 = 0 THEN 0 ELSE 250 END FROM sample
                        WHERE sample.device_id=sample_metric.device_id AND sample.trip_id=sample_metric.trip_id
                          AND sample.sequence=sample_metric.sequence)
                    WHERE trip_id=? AND pid='0x40C' AND sequence BETWEEN 12 AND 23
                """, (current,))
                result = contextual_baselines(history, "CAR", current)
            rpm = result["contexts"]["warm_idle"]["coverage"]["engine_rpm"]
            self.assertGreater(rpm["held_duplicate"], 0)
            self.assertEqual(rpm["one_second_full_coverage"], "met")

    def test_terminal_held_age_over_one_second_fails_full_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                history.execute("UPDATE sample_metric SET numeric_value=1200 WHERE trip_id=? AND pid='0x40C' AND sequence=23", (current,))
                quality = acquisition_quality(history, "CAR", current)
            rpm = quality["acquisition_coverage"]["engine_rpm"]
            self.assertEqual(rpm["one_second_full_coverage"], "not_met")

    def test_context_excludes_engine_off_creep_and_stationary_high_rpm(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            archive = root / "CONTEXT" / "2026" / "10" / "22" / "20261022-120000.txt"
            archive.parent.mkdir(parents=True)
            rows = [
                frame(0, "221026", 12000000, 0, 0, 0, 60, 2, 1, 1400, .02),
                frame(250, "221026", 12000025, 1700, 0, 20, 60, 2, 1, 1400, .02),
                frame(500, "221026", 12000050, 800, 3, 20, 60, 2, 1, 1400, .02),
                frame(750, "221026", 12000075, 1000, 25, 30, 60, 5, 1, 1400, .02),
            ]
            archive.write_text(",".join(rows))
            HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1000) + 61000).index_once()
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                samples = _trip_samples(history, "CONTEXT", archive.stem)
            self.assertEqual([_context(sample) for sample in samples], [None, None, None, "cold_driving"])

    def test_stale_samples_make_full_one_second_coverage_not_met(self):
        with tempfile.TemporaryDirectory() as directory:
            root, database = Path(directory) / "data", Path(directory) / "history.sqlite"
            current = self.build_history(root, database)
            with closing(sqlite3.connect(database)) as history:
                history.row_factory = sqlite3.Row
                history.execute("UPDATE sample_metric SET numeric_value=60000 WHERE trip_id=? AND pid='0x40C' AND sequence=12", (current,))
                result = contextual_baselines(history, "CAR", current)
            rpm = result["current_acquisition_coverage"]["engine_rpm"]
            self.assertEqual(rpm["observed_successive_acquisition"], "met")
            self.assertEqual(rpm["one_second_full_coverage"], "not_met")
