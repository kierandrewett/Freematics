#!/usr/bin/env python3
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from history_indexer import (
    DTC_CODE_SLOTS,
    DTC_GROUPS,
    Frame,
    HistoryIndexer,
    device_clock_capture_ms,
    display_timestamps,
    frame_timestamps,
    parse_frames,
)


class HistoryIndexerTest(unittest.TestCase):
    def test_device_clock_fields_enforce_integer_ranges(self) -> None:
        self.assertEqual(
            device_clock_capture_ms({"90": "1704067200", "91": "0"}),
            1_704_067_200_000,
        )
        self.assertEqual(
            device_clock_capture_ms({"90": "4294967295", "91": "999"}),
            4_294_967_295_999,
        )
        invalid = (
            {"90": "1704067199", "91": "0"},
            {"90": "4294967296", "91": "0"},
            {"90": "1704067200.0", "91": "0"},
            {"90": "1704067200", "91": "-1"},
            {"90": "1704067200", "91": "1000"},
            {"90": "1704067200", "91": "1.5"},
            {"90": " 1704067200", "91": "0"},
            {"90": "1704067200"},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assertIsNone(device_clock_capture_ms(fields))

    def test_invalid_device_clock_fields_fall_back_to_gnss(self) -> None:
        frame = Frame(
            100,
            {"90": "1704067199", "91": "0", "11": "290926", "10": "12101300"},
            (),
        )
        captures, qualities = frame_timestamps([frame])
        self.assertEqual(captures, [1_790_683_813_000])
        self.assertEqual(qualities, ["gnss"])

    def test_device_clock_and_vehicle_voltage_survive_journal_replay_without_gnss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "ZKUCALJ0/2026/10/03/20261003-170000.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text(
                "0:100,90:1791030012,91:345,24:1380,10C:900,"
                "0:350,90:1791030012,91:595,24:1240,10C:902,"
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    "SELECT capture_utc_ms, timestamp_quality FROM sample ORDER BY sequence"
                ).fetchall()
                self.assertEqual(rows, [(1791030012345, "device_clock"),
                                        (1791030012595, "device_clock")])
                voltage_rows = connection.execute(
                    "SELECT s.capture_utc_ms, m.numeric_value FROM sample AS s "
                    "JOIN sample_metric AS m USING (device_id, trip_id, sequence) "
                    "WHERE m.pid = '0x024' ORDER BY s.sequence"
                ).fetchall()
                self.assertEqual(
                    voltage_rows,
                    [(1791030012345, 1380.0), (1791030012595, 1240.0)],
                )

    def test_held_gnss_fix_preserves_250ms_capture_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "ZKUCALJ0/2026/09/29/20260929-121013.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("".join(
                f"0:{1000 + i * 250},11:290926,10:12101300,93:{i * 250},10C:900,40C:{i * 250},"
                for i in range(5)
            ))
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1000) + 1000)
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute("SELECT capture_utc_ms, timestamp_quality FROM sample ORDER BY sequence").fetchall()
                self.assertEqual(len(rows), 4)
                self.assertEqual([row[0] - rows[0][0] for row in rows], [0, 250, 500, 750])
                self.assertEqual([row[1] for row in rows], ["gnss", "anchored", "anchored", "anchored"])
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample_metric WHERE pid='0x40C'").fetchone()[0], 4)

    def test_single_digit_day_gnss_date_keeps_capture_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "ZKUCALJ0/2026/10/03/20261003-143613.txt"
            archive.parent.mkdir(parents=True)
            # Firmware serializes DDMMYY as an integer, so 03-10-26 arrives as 31026.
            archive.write_text(
                "0:100,11:31026,10:14330100,93:0,10C:680,"
                "0:350,11:31026,10:14330100,93:250,10C:681,"
                "0:600,11:31026,10:14330100,93:500,10C:682,"
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    "SELECT capture_utc_ms, timestamp_quality FROM sample ORDER BY sequence"
                ).fetchall()
                self.assertEqual(len(rows), 3)
                self.assertEqual(rows[0][1], "gnss")
                self.assertEqual(rows[1][1:], ("anchored",))
                self.assertEqual(rows[2][1:], ("anchored",))
                self.assertEqual([row[0] - rows[0][0] for row in rows], [0, 250, 500])

    def test_replay_is_idempotent_and_preserves_capture_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "ZKUCALJ0/2026/08/27/20260827-001247.txt"
            archive.parent.mkdir(parents=True)
            # The first frame is complete only once the following PID 0 exists.
            # This old-style archive has no GNSS date/time pair, so its UTC
            # capture time must remain unknown.
            archive.write_text("0:100,10C:900,10D:20,A:51.0,B:-1.0\n")
            database = Path(directory) / "history.sqlite"
            # Keep the fixture younger than the 60-second sealing window.
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 1_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)
                self.assertGreaterEqual(connection.execute("SELECT COUNT(*) FROM metric_catalogue").fetchone()[0], 80)
                self.assertEqual(
                    connection.execute("SELECT name, category FROM metric_catalogue WHERE pid='0x089'").fetchone(),
                    ("obd_state", "obd"),
                )

            archive.write_text("0:100,10C:900,10D:20,A:51.0,B:-1.0,0:600,10C:1200,10D:40,A:51.1,B:-1.1\n")
            # Keep the test file active so the final incomplete frame is held back.
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 1)
                first = connection.execute("SELECT capture_utc_ms, timestamp_quality FROM sample").fetchone()
                self.assertIsNone(first[0])
                self.assertEqual(first[1], "unknown")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample_metric").fetchone()[0], 4)

            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 1)

            # A subsequent frame seals the previous final frame; no duplicate rows.
            archive.write_text(archive.read_text() + "0:1100,10C:1400,10D:50\n")
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample_metric WHERE pid='0x10C'").fetchone()[0], 2)

    def test_gnss_date_and_time_anchor_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "ZKUCALJ0/2026/08/27/20260827-001247.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text(
                "0:100,11:270826,10:00124700,A:51.0,B:-1.0,0:600,10C:1200,A:51.1,B:-1.1,0:1100,11:270826,10:00124800,A:51.2,B:-1.2\n"
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 1_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    "SELECT sequence, capture_utc_ms, timestamp_quality FROM sample ORDER BY sequence"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][2], "gnss")
                self.assertEqual(rows[1][2], "anchored")
                self.assertEqual(rows[0][1], 1787789567000)
                self.assertEqual(rows[1][1], 1787789567500)

    def test_gap_view_reports_device_clock_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "ZKUCALJ0/2026/08/27/20260827-001247.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("0:100,10C:900,0:5000,10C:1200,0:5500,10C:1300\n")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 1_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                gap = connection.execute(
                    "SELECT previous_sequence, sequence, gap_ms FROM sample_gaps"
                ).fetchone()
                self.assertEqual(gap, (0, 1, 4900))

    def test_scaled_hdop_distance_acceleration_and_diagnostics_are_projected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026/08/27/20260827-001247.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text(
                "0:100,12:1,030:12.3,20:0.1;0.2;0.3,300:1,301:4660,A:51.0,B:-1.0,D:30,10D:50,0:600,030:13.0,20:-0.4;-0.5;-0.6\n"
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root, database, now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                sample = connection.execute(
                    "SELECT gps_hdop, acceleration_x_g, acceleration_y_g, acceleration_z_g FROM sample WHERE sequence = 0"
                ).fetchone()
                self.assertEqual(sample, (0.1, 0.1, 0.2, 0.3))
                distance = connection.execute(
                    "SELECT numeric_value FROM sample_metric WHERE pid = '0x030' ORDER BY sequence"
                ).fetchall()
                self.assertEqual(distance, [(12.3,), (13.0,)])
                dtc = connection.execute(
                    "SELECT status, slot, raw_code, code, system FROM diagnostic_code"
                ).fetchone()
                self.assertEqual(dtc, ("stored", 0, 4660, "P1234", "powertrain"))
                categories = connection.execute(
                    "SELECT pid, category FROM metric_catalogue WHERE pid IN ('0x310', '0x330', '0x350') ORDER BY pid"
                ).fetchall()
                self.assertEqual(categories, [("0x310", "diagnostics"), ("0x330", "diagnostics"), ("0x350", "diagnostics")])
                quality = connection.execute(
                    "SELECT gps_fix_count, gps_poor_quality_count, speed_disagreement_count FROM trip"
                ).fetchone()
                self.assertEqual(quality, (1, 0, 1))
                vector = connection.execute(
                    "SELECT acceleration_x_g FROM sample WHERE sequence = 1"
                ).fetchone()
                self.assertEqual(vector, (-0.4,))

    def test_dtc_status_fields_do_not_overlap_code_slots(self) -> None:
        for _status, _count_pid, base_pid, status_pid in DTC_GROUPS:
            emitted = {int(base_pid, 16) + slot for slot in range(DTC_CODE_SLOTS)}
            self.assertNotIn(int(status_pid, 16), emitted)

    def test_same_second_trip_ids_are_isolated_per_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            for device, rpm in (("CAR_A", "900"), ("CAR_B", "1400")):
                archive = root / device / "2026/08/27/20260827-001247.txt"
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_text(f"0:100,10C:{rpm},0:600,10C:{rpm}\n")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: int(time.time() * 1_000))
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM trip").fetchone()[0], 2)
                rows = connection.execute(
                    "SELECT device_id, numeric_value FROM sample_metric WHERE pid='0x10C' ORDER BY device_id"
                ).fetchall()
                self.assertEqual(rows, [("CAR_A", 900.0), ("CAR_B", 1400.0)])

    def test_unchanged_file_is_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27" / "20260827-120000.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("0:100,10C:900,0:600,10C:1200\n")
            database = Path(directory) / "history.sqlite"
            clock = [2_000_000]
            indexer = HistoryIndexer(root, database, now_ms=lambda: clock[0])
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                first = connection.execute(
                    "SELECT updated_at_ms FROM trip"
                ).fetchone()
                first_file = connection.execute(
                    "SELECT indexed_at_ms FROM ingest_file"
                ).fetchone()
            clock[0] += 10_000
            self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                second = connection.execute(
                    "SELECT updated_at_ms FROM trip"
                ).fetchone()
                second_file = connection.execute(
                    "SELECT indexed_at_ms FROM ingest_file"
                ).fetchone()
            self.assertEqual(first, second)
            self.assertEqual(first_file, second_file)


    def test_equals_delimiter_is_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27" / "20260827-120000.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("0=100,10C=900,0=600,10C=1200\n")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000)
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                values = connection.execute("SELECT numeric_value FROM sample_metric WHERE pid='0x10C' ORDER BY sequence").fetchall()
            self.assertEqual(values, [(900.0,), (1200.0,)])

    def test_duplicate_pid_fields_remain_in_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27" / "20260827-120000.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("0:100,10C:900,10C:901,0:600,10C:1200\n")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000)
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                values = connection.execute(
                    "SELECT ordinal, pid, numeric_value FROM sample_field WHERE sequence = 0 ORDER BY ordinal"
                ).fetchall()
            self.assertEqual(values, [(0, "0x10C", 900.0), (1, "0x10C", 901.0)])

    def test_incompatible_schema_requires_explicit_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            root.mkdir()
            database = Path(directory) / "history.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE trip (trip_id TEXT PRIMARY KEY)")
                connection.commit()

            indexer = HistoryIndexer(root, database, now_ms=lambda: 1234)
            with self.assertRaisesRegex(RuntimeError, "incompatible history database"):
                indexer.index_once()
            self.assertTrue(database.exists())

            rebuilding = HistoryIndexer(root, database, now_ms=lambda: 1234, rebuild=True)
            self.assertEqual(rebuilding.index_once(), 0)
            self.assertTrue(database.with_name("history.sqlite.backup-1234").exists())



    def test_initialise_replaces_stale_field_timeline_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            indexer.initialise()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP VIEW field_timeline")
                connection.execute("CREATE VIEW field_timeline AS SELECT 1 AS stale")
                connection.commit()
            indexer.initialise()
            with closing(sqlite3.connect(database)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(field_timeline)")}
                self.assertIn("pid", columns)
                self.assertNotIn("stale", columns)

    def test_parser_rejects_out_of_range_frame_timestamp(self) -> None:
        frames = parse_frames("0:4294967296,10C:1,0:100,10C:2", include_final=True)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].device_monotonic_ms, 100)

    def test_parser_keeps_maximum_valid_device_tick(self) -> None:
        frames = parse_frames("0:4294967295,10C:1,0:100,10C:2", include_final=True)
        self.assertEqual([frame.device_monotonic_ms for frame in frames], [4294967295, 100])

    def test_display_timeline_preserves_clock_rollover_interval(self) -> None:
        frames = [Frame(0xFFFFFF00, {}, ()), Frame(0xFFFFFFFF, {}, ()), Frame(0, {}, ())]
        timelines, _ = display_timestamps(frames, [None] * 3, ["unknown"] * 3, 1_000_000)
        self.assertEqual(timelines, [1_000_000, 1_000_255, 1_000_256])

    def test_display_timeline_stays_monotonic_after_clock_reset(self) -> None:
        frames = [
            Frame(100, {}, ()),
            Frame(5000, {}, ()),
            Frame(500, {}, ()),
            Frame(1000, {}, ()),
        ]
        timelines, bases = display_timestamps(frames, [None] * len(frames), ["unknown"] * len(frames), 1_000_000)
        self.assertEqual(timelines, [1_000_000, 1_004_900, 1_004_900, 1_005_400])
        self.assertEqual(bases, ["collector_session"] * len(frames))

if __name__ == "__main__":

    unittest.main()
