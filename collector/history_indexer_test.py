#!/usr/bin/env python3
import sqlite3
import tempfile
import time
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from history_indexer import (
    DTC_CODE_SLOTS,
    DTC_GROUPS,
    Frame,
    HistoryIndexer,
    device_clock_capture_ms,
    display_timestamps,
    frame_timestamps,
    parse_inbox_record,
    parse_frames,
)


class HistoryIndexerTest(unittest.TestCase):
    @staticmethod
    def write_inbox_record(root: Path, device: str, session: str, sequence: int, payload: bytes) -> Path:
        path = root / "capture-inbox" / device / f"{session}-{sequence}.fqi"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = f"FQI1,{session},{sequence},{len(payload)},{zlib.crc32(payload):08x}\n".encode()
        path.write_bytes(header + payload)
        return path

    @staticmethod
    def write_receipt_sidecar(path: Path, session: str, sequence: int,
                              epoch_ms: int, plausible: int = 1) -> Path:
        prefix = f"FQR1,{session},{sequence},{epoch_ms},{plausible}".encode("ascii")
        sidecar = path.with_name(f"{session}-{sequence}.receipt")
        sidecar.write_bytes(prefix + f",{zlib.crc32(prefix):08x}\n".encode("ascii"))
        return sidecar

    def test_capture_inbox_uses_capture_identity_clock_and_valid_device_utc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            inbox_file = self.write_inbox_record(
                root, "CAR", "0123456789abcdef", 7,
                b"0:1000,10C:800,40C:20,90:1791030012,91:345,24:1380,",
            )
            inbox_file.touch()
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: 1_900_000_000_000)
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                sample = connection.execute(
                    "SELECT device_monotonic_ms,capture_utc_ms,timeline_ms,time_basis,"
                    "collector_received_ms,capture_session_id,capture_sequence "
                    "FROM sample"
                ).fetchone()
                self.assertEqual(sample, (1000, 1791030012345, 0, "device_clock",
                                          None,
                                          "0123456789abcdef", 7))
                fields = connection.execute(
                    "SELECT pid,numeric_value FROM sample_field ORDER BY ordinal"
                ).fetchall()
                self.assertEqual(fields, [("0x10C", 800.0), ("0x40C", 20.0),
                                          ("0x090", 1791030012.0), ("0x091", 345.0),
                                          ("0x024", 1380.0)])
                self.assertEqual(connection.execute(
                    "SELECT trip_id,missing_capture_sequence_count FROM trip"
                ).fetchone(), ("fqi-0123456789abcdef", 0))

    def test_capture_inbox_is_idempotent_and_keeps_unknown_utc_out_of_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            path = self.write_inbox_record(
                root, "CAR", "0123456789abcdef", 20, b"0:4294967200,10C:700,91:123,"
            )
            path.touch()
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: 1_900_000_000_000)
            self.assertEqual(indexer.index_once(), 1)
            self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    "SELECT capture_utc_ms,timeline_ms,time_basis,collector_received_ms "
                    "FROM sample"
                ).fetchone()
                self.assertEqual(row, (None, 0, "device_monotonic", None))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 1)

    def test_corrupt_inbox_records_are_retained_but_not_projected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            path = self.write_inbox_record(
                root, "CAR", "0123456789abcdef", 1, b"0:100,10C:700"
            )
            path.write_bytes(path.read_bytes()[:-1] + b"X")
            database = Path(directory) / "history.sqlite"
            HistoryIndexer(root, database).index_once()
            self.assertTrue(path.exists())
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)

    def test_rewritten_indexed_capture_is_rebuilt_without_directory_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "00000000000000ab"
            path = self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            original_directory_mtime = path.parent.stat().st_mtime_ns

            replacement = self.write_inbox_record(
                root, "CAR", session, 7, b"0:1000,10C:710"
            )
            self.assertEqual(path, replacement)
            self.assertEqual(path.parent.stat().st_mtime_ns, original_directory_mtime)
            self.assertEqual(indexer.index_once(), 1)

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE pid='0x10C'"
                ).fetchone(), (710.0,))
                self.assertEqual(connection.execute(
                    "SELECT source_size,source_mtime_ns,source_ctime_ns "
                    "FROM capture_inbox_record"
                ).fetchone(), (
                    path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns,
                ))

    def test_rewritten_indexed_capture_that_becomes_corrupt_removes_stale_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "00000000000000ac"
            path = self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)

            data = bytearray(path.read_bytes())
            data[-1] ^= 1  # Keep size and path stable but invalidate the payload CRC.
            path.write_bytes(data)
            self.assertEqual(indexer.index_once(), 1)

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sample WHERE capture_session_id=?", (session,)
                ).fetchone(), (0,))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM capture_inbox_record WHERE session_id=?", (session,)
                ).fetchone(), (0,))

    def test_capture_changed_during_parse_is_retried_from_the_next_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "00000000000000ad"
            path = self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:710")

            real_parse = parse_inbox_record
            rewrote = False

            def rewrite_before_read(candidate: Path):
                nonlocal rewrote
                if candidate == path and not rewrote:
                    rewrote = True
                    self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:720")
                return real_parse(candidate)

            with patch("history_indexer.parse_inbox_record", side_effect=rewrite_before_read):
                self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE pid='0x10C'"
                ).fetchone(), (700.0,))

            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE pid='0x10C'"
                ).fetchone(), (720.0,))

    def test_source_removed_during_rewrite_scan_preserves_ingested_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "00000000000000ae"
            path = self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:710")

            real_glob = Path.glob

            def remove_before_enumeration(directory_path: Path, pattern: str):
                if directory_path == path.parent and pattern == "*.fqi":
                    path.unlink(missing_ok=True)
                return real_glob(directory_path, pattern)

            with patch("history_indexer.Path.glob", new=remove_before_enumeration):
                self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE pid='0x10C'"
                ).fetchone(), (700.0,))

    def test_source_removed_during_rewrite_scan_preserves_its_record_in_multi_record_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "00000000000000af"
            changed = self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:700")
            self.write_inbox_record(root, "CAR", session, 8, b"0:1250,10C:800")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            self.write_inbox_record(root, "CAR", session, 7, b"0:1000,10C:710")

            real_glob = Path.glob

            def remove_changed_before_enumeration(directory_path: Path, pattern: str):
                if directory_path == changed.parent and pattern == "*.fqi":
                    changed.unlink(missing_ok=True)
                return real_glob(directory_path, pattern)

            with patch("history_indexer.Path.glob", new=remove_changed_before_enumeration):
                self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT sample_count FROM trip WHERE trip_id=?", (f"fqi-{session}",)
                ).fetchone(), (2,))
                self.assertEqual(connection.execute(
                    "SELECT sequence,numeric_value FROM sample_field WHERE pid='0x10C' "
                    "ORDER BY sequence"
                ).fetchall(), [(0, 700.0), (1, 800.0)])

    def test_recording_gap_counters_survive_inbox_decode_and_numeric_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            self.write_inbox_record(
                root, "CAR", "0123456789abcdef", 7,
                b"0:1000,8E:12,9E:2,9F:3,A6:4,A7:5",
            )
            database = Path(directory) / "history.sqlite"
            HistoryIndexer(root, database).index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT pid,numeric_value,text_value FROM sample_metric "
                    "WHERE pid IN ('0x08E','0x09E','0x09F','0x0A6','0x0A7') ORDER BY pid"
                ).fetchall(), [
                    ("0x08E", 12.0, None),
                    ("0x09E", 2.0, None),
                    ("0x09F", 3.0, None),
                    ("0x0A6", 4.0, None),
                    ("0x0A7", 5.0, None),
                ])

    def test_capture_sequence_holes_are_separate_from_clock_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            self.write_inbox_record(root, "CAR", "0000000000000001", 10, b"0:100,10C:700")
            self.write_inbox_record(root, "CAR", "0000000000000001", 12, b"0:200,10C:710")
            database = Path(directory) / "history.sqlite"
            HistoryIndexer(root, database).index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT missing_capture_sequence_count,gap_count FROM trip"
                ).fetchone(), (1, 0))
                self.assertEqual(connection.execute(
                    "SELECT previous_capture_sequence,capture_sequence,missing_sequences "
                    "FROM sample_capture_sequence_gaps"
                ).fetchone(), (10, 12, 1))

    def test_inbox_time_gap_uses_capture_clock_not_collector_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000003"
            self.write_inbox_record(
                root, "CAR", session, 40,
                b"0:1000,90:1791030012,91:345,10C:700",
            )
            self.write_inbox_record(
                root, "CAR", session, 41,
                b"0:5000,90:1791030016,91:345,10C:710",
            )
            database = Path(directory) / "history.sqlite"
            HistoryIndexer(root, database, now_ms=lambda: 1_900_000_000_000).index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT previous_device_monotonic_ms,device_monotonic_ms,"
                    "previous_capture_utc_ms,capture_utc_ms,gap_ms FROM sample_gaps"
                ).fetchone(), (1000, 5000, 1791030012345, 1791030016345, 4000))
                self.assertEqual(connection.execute(
                    "SELECT MIN(timeline_ms),MAX(timeline_ms),MIN(collector_received_ms) "
                    "FROM sample"
                ).fetchone(), (0, 4000, None))

    def test_receipt_sidecar_is_validated_and_duplicate_projection_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session, sequence, receipt_ms = "0123456789abcdef", 7, 1_800_000_123_000
            path = self.write_inbox_record(
                root, "CAR", session, sequence,
                b"0:1000,90:1791030012,91:345,10C:800",
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            self.write_receipt_sidecar(path, session, sequence, receipt_ms)
            # The sidecar may become visible just after the capture file. Its
            # directory fingerprint must trigger re-projection without touching FQI.
            self.assertEqual(indexer.index_once(), 1)
            self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                capture_ms, received_ms, timeline_ms = connection.execute(
                    "SELECT capture_utc_ms,collector_received_ms,timeline_ms FROM sample"
                ).fetchone()
            self.assertEqual(capture_ms, 1_791_030_012_345)
            self.assertEqual(received_ms, receipt_ms)
            self.assertEqual(timeline_ms, 0)

    def test_missing_or_invalid_receipt_sidecars_remain_unknown(self) -> None:
        cases = ("missing", "bad_crc", "out_of_range", "before_epoch_range", "subsecond",
                 "flag_epoch_mismatch", "untrusted_clock", "bad_length")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "data"
                session, sequence = "000000000000000a", 3
                path = self.write_inbox_record(root, "CAR", session, sequence,
                                               b"0:1000,90:1791030012,91:345")
                sidecar = path.with_name(f"{session}-{sequence}.receipt")
                if case != "missing":
                    epoch, flag = 1_800_000_000_000, 1
                    if case == "out_of_range":
                        epoch = 4_102_444_800_001
                    elif case == "before_epoch_range":
                        epoch = 1_704_067_199_999
                    elif case == "subsecond":
                        epoch += 1
                    elif case == "flag_epoch_mismatch":
                        epoch, flag = 1, 0
                    elif case == "untrusted_clock":
                        epoch, flag = 0, 0
                    self.write_receipt_sidecar(path, session, sequence, epoch, flag)
                    if case == "bad_crc":
                        raw = bytearray(sidecar.read_bytes())
                        raw[-9] = ord("0") if raw[-9] != ord("0") else ord("1")
                        sidecar.write_bytes(raw)
                    elif case == "bad_length":
                        sidecar.write_bytes(sidecar.read_bytes()[:-1])
                record = parse_inbox_record(path)
                self.assertIsNotNone(record)
                self.assertIsNone(record.collector_received_ms)

    def test_projection_upgrade_clears_legacy_fqi_mtime_receipt_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            path = self.write_inbox_record(root, "CAR", "000000000000000b", 4,
                                           b"0:1000,90:1791030012,91:345")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE sample SET collector_received_ms=?",
                    (int(path.stat().st_mtime * 1000),),
                )
                connection.execute(
                    "DELETE FROM history_projection_meta "
                    "WHERE key='capture_inbox_receipt_sidecar_version_seen'"
                )
                connection.commit()
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertIsNone(connection.execute(
                    "SELECT collector_received_ms FROM sample"
                ).fetchone()[0])

    def test_capture_sequence_wrap_has_no_false_hole_or_time_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000002"
            for sequence, tick in ((0xFFFFFFFE, 0xFFFFFF00), (0xFFFFFFFF, 0xFFFFFFFA),
                                   (0, 244), (1, 494)):
                self.write_inbox_record(
                    root, "CAR", session, sequence, f"0:{tick},10C:700".encode()
                )
            database = Path(directory) / "history.sqlite"
            HistoryIndexer(root, database).index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT capture_sequence,device_monotonic_ms FROM sample ORDER BY sequence"
                ).fetchall(), [(0xFFFFFFFE, 0xFFFFFF00), (0xFFFFFFFF, 0xFFFFFFFA), (0, 244), (1, 494)])
                self.assertEqual(connection.execute(
                    "SELECT missing_capture_sequence_count,gap_count FROM trip WHERE trip_id=?",
                    (f"fqi-{session}",),
                ).fetchone(), (0, 0))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sample_capture_sequence_gaps"
                ).fetchone()[0], 0)

    def test_append_only_inbox_adds_rows_without_rewriting_existing_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000004"
            first = self.write_inbox_record(
                root, "CAR", session, 10,
                b"0:1000,90:1791030012,91:345,10C:700,24:1380,300:1,301:4660",
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database, now_ms=lambda: 1_900_000_000_000)
            indexer.initialise()
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript("""
                    CREATE TABLE row_mutations(table_name TEXT, operation TEXT, sequence INTEGER);
                    CREATE TRIGGER sample_old_delete AFTER DELETE ON sample
                      WHEN OLD.sequence=0 BEGIN INSERT INTO row_mutations VALUES('sample','delete',OLD.sequence); END;
                    CREATE TRIGGER sample_old_insert AFTER INSERT ON sample
                      WHEN NEW.sequence=0 BEGIN INSERT INTO row_mutations VALUES('sample','insert',NEW.sequence); END;
                    CREATE TRIGGER metric_old_insert AFTER INSERT ON sample_metric
                      WHEN NEW.sequence=0 BEGIN INSERT INTO row_mutations VALUES('sample_metric','insert',NEW.sequence); END;
                    CREATE TRIGGER metric_old_delete AFTER DELETE ON sample_metric
                      WHEN OLD.sequence=0 BEGIN INSERT INTO row_mutations VALUES('sample_metric','delete',OLD.sequence); END;
                    CREATE TRIGGER field_old_insert AFTER INSERT ON sample_field
                      WHEN NEW.sequence=0 BEGIN INSERT INTO row_mutations VALUES('sample_field','insert',NEW.sequence); END;
                    CREATE TRIGGER field_old_delete AFTER DELETE ON sample_field
                      WHEN OLD.sequence=0 BEGIN INSERT INTO row_mutations VALUES('sample_field','delete',OLD.sequence); END;
                    CREATE TRIGGER dtc_old_insert AFTER INSERT ON diagnostic_code
                      WHEN NEW.sequence=0 BEGIN INSERT INTO row_mutations VALUES('diagnostic_code','insert',NEW.sequence); END;
                    CREATE TRIGGER dtc_old_delete AFTER DELETE ON diagnostic_code
                      WHEN OLD.sequence=0 BEGIN INSERT INTO row_mutations VALUES('diagnostic_code','delete',OLD.sequence); END;
                    CREATE TRIGGER inbox_old_insert AFTER INSERT ON capture_inbox_record
                      WHEN NEW.capture_sequence=10 BEGIN INSERT INTO row_mutations VALUES('capture_inbox_record','insert',NEW.capture_sequence); END;
                    CREATE TRIGGER inbox_old_delete AFTER DELETE ON capture_inbox_record
                      WHEN OLD.capture_sequence=10 BEGIN INSERT INTO row_mutations VALUES('capture_inbox_record','delete',OLD.capture_sequence); END;
                """)
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DELETE FROM row_mutations")
                connection.commit()
            self.write_inbox_record(
                root, "CAR", session, 11,
                b"0:1250,90:1791030012,91:595,10C:710,24:1240",
            )
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT * FROM row_mutations"
                ).fetchall(), [])
                self.assertEqual(connection.execute(
                    "SELECT sequence,capture_utc_ms,timeline_ms FROM sample ORDER BY sequence"
                ).fetchall(), [(0, 1791030012345, 0), (1, 1791030012595, 250)])
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_metric WHERE sequence=0 AND pid='0x10C'"
                ).fetchone(), (700.0,))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM diagnostic_code WHERE sequence=0"
                ).fetchone(), (1,))
                self.assertEqual(connection.execute(
                    "SELECT sample_count,missing_capture_sequence_count FROM trip"
                ).fetchone(), (2, 0))
            self.assertTrue(first.exists())

    def test_inbox_rebuild_preserves_binary64_numeric_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000007"
            precise = "0.12345678901234567"
            self.write_inbox_record(
                root, "CAR", session, 10,
                f"0:100,10C:{precise},300:1,301:4660".encode(),
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            # Out-of-order arrival forces the full-rebuild path, which
            # rehydrates the first value from its SQLite REAL projection.
            self.write_inbox_record(root, "CAR", session, 9, b"0:50,10C:690")
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                restored = connection.execute(
                    "SELECT m.numeric_value FROM sample_metric AS m "
                    "JOIN sample AS s ON s.device_id=m.device_id AND s.trip_id=m.trip_id "
                    "AND s.sequence=m.sequence WHERE s.capture_sequence=10 AND m.pid='0x10C'"
                ).fetchone()[0]
                self.assertEqual(restored, float(precise))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM diagnostic_code AS d "
                    "JOIN sample AS s ON s.device_id=d.device_id AND s.trip_id=d.trip_id "
                    "AND s.sequence=d.sequence WHERE s.capture_sequence=10"
                ).fetchone(), (1,))

    def test_inbox_append_continues_when_an_indexed_source_file_was_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000008"
            first = self.write_inbox_record(root, "CAR", session, 10, b"0:100,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            first.unlink()
            second = self.write_inbox_record(root, "CAR", session, 11, b"0:350,10C:710")

            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT sample_count,data_bytes,missing_capture_sequence_count FROM trip"
                ).fetchone(), (2, second.stat().st_size, 0))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sample WHERE capture_session_id=?", (session,)
                ).fetchone(), (2,))

    def test_steady_inbox_poll_parses_only_new_files_and_batches_field_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000009"
            for sequence in range(24):
                self.write_inbox_record(
                    root, "CAR", session, sequence,
                    f"0:{1000 + sequence * 250},10C:{700 + sequence},40C:20".encode(),
                )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            # Restarting the collector must not turn an ordinary append into
            # a full-session field rehydrate.
            indexer = HistoryIndexer(root, database)
            self.write_inbox_record(
                root, "CAR", session, 24, b"0:7000,10C:724,40C:20"
            )

            real_connect = sqlite3.connect
            statements: list[str] = []
            rehydrated_rows: list[tuple] = []
            identity_lookups: list[str] = []

            class TracedConnection:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self.connection = connection

                def execute(self, sql: str, parameters=()):
                    statements.append(sql)
                    if "select session_id,capture_sequence,source_path,source_dev" in sql.lower():
                        identity_lookups.append(sql)
                    cursor = self.connection.execute(sql, parameters)
                    if "from capture_inbox_record as r" in sql.lower():
                        rows = cursor.fetchall()
                        rehydrated_rows.extend(rows)
                        return rows
                    return cursor

                def __getattr__(self, name: str):
                    return getattr(self.connection, name)

            def traced_connect(*args, **kwargs):
                return TracedConnection(real_connect(*args, **kwargs))

            with patch("history_indexer.sqlite3.connect", side_effect=traced_connect), \
                 patch("history_indexer.parse_inbox_record", wraps=parse_inbox_record) as parse_record:
                self.assertEqual(indexer.index_once(), 1)

            self.assertEqual(parse_record.call_count, 1)
            # An append should load only the newly captured record fields.
            # Rehydrating every prior sample here makes each append slower as
            # the recording grows and can starve timely history synchronization.
            self.assertLessEqual(len(rehydrated_rows), 3)
            self.assertEqual(len(identity_lookups), 1)
            field_selects = [
                sql for sql in statements
                if sql.lstrip().lower().startswith("select") and "sample_field" in sql.lower()
            ]
            self.assertEqual(field_selects, [])
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT sample_count FROM trip WHERE trip_id=?", (f"fqi-{session}",)
                ).fetchone(), (25,))

            statements.clear()
            identity_lookups.clear()
            real_glob = Path.glob
            device_glob_calls: list[str] = []

            def traced_glob(path: Path, pattern: str):
                if path == root / "capture-inbox" / "CAR":
                    device_glob_calls.append(pattern)
                return real_glob(path, pattern)

            with patch("history_indexer.sqlite3.connect", side_effect=traced_connect), \
                 patch("history_indexer.Path.glob", new=traced_glob), \
                 patch("history_indexer.parse_inbox_record", wraps=parse_inbox_record) as parse_record:
                self.assertEqual(indexer.index_once(), 0)

            self.assertEqual(parse_record.call_count, 0)
            self.assertEqual(device_glob_calls, [])
            field_selects = [
                sql for sql in statements
                if sql.lstrip().lower().startswith("select") and "sample_field" in sql.lower()
            ]
            self.assertEqual(field_selects, [])
            # Idle checks stat the known immutable source paths using persisted
            # metadata, but do not hydrate rows, enumerate the directory, or parse files.
            self.assertEqual(len(identity_lookups), 1)

            partial_path = root / "capture-inbox" / "CAR" / f"{session}-25.fqi"
            partial_path.write_bytes(f"FQI1,{session},25,100,00000000\npartial".encode())
            self.assertEqual(indexer.index_once(), 0)
            directory = partial_path.parent
            directory_mtime_ns = directory.stat().st_mtime_ns
            self.write_inbox_record(
                root, "CAR", session, 25, b"0:7250,10C:725,40C:20"
            )
            self.assertEqual(directory.stat().st_mtime_ns, directory_mtime_ns)
            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT sample_count FROM trip WHERE trip_id=?", (f"fqi-{session}",)
                ).fetchone(), (26,))

    def test_inbox_source_removal_does_not_delete_existing_history_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            inbox_file = self.write_inbox_record(
                root, "CAR", "000000000000000A", 3, b"0:1000,10C:700"
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            inbox_file.unlink()
            inbox_file.parent.rmdir()
            self.assertEqual(indexer.index_once(), 0)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sample WHERE capture_session_id=?",
                    ("000000000000000a",),
                ).fetchone(), (1,))

    def test_in_place_capture_projection_reset_invalidates_directory_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "000000000000000b"
            self.write_inbox_record(root, "CAR", session, 3, b"0:1000,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            database_inode = database.stat().st_ino

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    "DELETE FROM trip WHERE device_id=? AND trip_id=?",
                    ("CAR", f"fqi-{session}"),
                )
                connection.execute(
                    "DELETE FROM capture_inbox_record WHERE device_id=? AND session_id=?",
                    ("CAR", session),
                )
                connection.execute(
                    "DELETE FROM ingest_file WHERE archive_path=?",
                    (f"capture-inbox://CAR/{session}",),
                )
                connection.commit()
            self.assertEqual(database.stat().st_ino, database_inode)

            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sample WHERE capture_session_id=?", (session,)
                ).fetchone(), (1,))

    def test_in_place_detail_projection_delete_rebuilds_from_inbox_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "000000000000000c"
            self.write_inbox_record(
                root, "CAR", session, 3, b"0:1000,10C:700,40C:20"
            )
            database = Path(directory) / "history.sqlite"
            self.assertEqual(HistoryIndexer(root, database).index_once(), 1)

            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "DELETE FROM sample_field WHERE trip_id=? AND pid='0x10C'",
                    (f"fqi-{session}",),
                )
                connection.commit()

            # A fresh indexer has no in-memory state; the persisted generation
            # mismatch must still force a complete projection rebuild.
            self.assertEqual(HistoryIndexer(root, database).index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE trip_id=? AND pid='0x10C'",
                    (f"fqi-{session}",),
                ).fetchone(), (700.0,))

    def test_projection_generation_change_during_scan_is_not_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "000000000000000d"
            self.write_inbox_record(root, "CAR", session, 0, b"0:1000,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            self.write_inbox_record(root, "CAR", session, 1, b"0:1250,10C:710")

            real_parse = parse_inbox_record
            deleted = False

            def parse_then_delete(path: Path):
                nonlocal deleted
                record = real_parse(path)
                if path.name.endswith("-1.fqi") and not deleted:
                    with closing(sqlite3.connect(database)) as concurrent_connection:
                        concurrent_connection.execute(
                            "DELETE FROM sample_field WHERE trip_id=? AND sequence=0",
                            (f"fqi-{session}",),
                        )
                        concurrent_connection.commit()
                    deleted = True
                return record

            with patch("history_indexer.parse_inbox_record", side_effect=parse_then_delete):
                # The generation changes after scanning starts, so this pass
                # must not advance its checkpoint over the incomplete view.
                self.assertEqual(indexer.index_once(), 0)
            self.assertTrue(deleted)

            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE trip_id=? "
                    "AND sequence=0 AND pid='0x10C'", (f"fqi-{session}",)
                ).fetchone(), (700.0,))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM sample WHERE capture_session_id=?", (session,)
                ).fetchone(), (2,))

    def test_schema_only_detail_projection_reset_is_rebuilt_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "000000000000000e"
            self.write_inbox_record(
                root, "CAR", session, 0, b"0:1000,10C:700,40C:20"
            )
            database = Path(directory) / "history.sqlite"
            self.assertEqual(HistoryIndexer(root, database).index_once(), 1)

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TABLE sample_field")
                connection.commit()

            # Schema initialization recreates the empty detail table. Its
            # persisted version checkpoint forces an inbox-backed rebuild.
            self.assertEqual(HistoryIndexer(root, database).index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT numeric_value FROM sample_field WHERE trip_id=? "
                    "AND sequence=0 AND pid='0x10C'", (f"fqi-{session}",)
                ).fetchone(), (700.0,))

    def test_inbox_out_of_order_and_conflicting_identity_use_full_rebuild(self) -> None:
        for scenario in ("out_of_order", "conflict"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "data"
                session = "abcdef0000000005"
                self.write_inbox_record(root, "CAR", session, 10, b"0:100,10C:700")
                database = Path(directory) / "history.sqlite"
                indexer = HistoryIndexer(root, database)
                indexer.index_once()
                with closing(sqlite3.connect(database)) as connection:
                    connection.executescript("""
                        CREATE TABLE deletes(sequence INTEGER);
                        CREATE TRIGGER observe_delete AFTER DELETE ON sample
                          BEGIN INSERT INTO deletes VALUES(OLD.sequence); END;
                    """)
                if scenario == "out_of_order":
                    self.write_inbox_record(root, "CAR", session, 9, b"0:50,10C:690")
                else:
                    conflict = root / "capture-inbox/CAR" / f"{session.upper()}-10.fqi"
                    payload = b"0:101,10C:701"
                    conflict.write_bytes(
                        f"FQI1,{session.upper()},10,{len(payload)},{zlib.crc32(payload):08x}\n".encode()
                        + payload
                    )
                indexer.index_once()
                with closing(sqlite3.connect(database)) as connection:
                    self.assertGreater(connection.execute("SELECT COUNT(*) FROM deletes").fetchone()[0], 0)

    def test_inbox_sequence_wrap_continues_incrementally_when_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "0000000000000006"
            self.write_inbox_record(root, "CAR", session, 0xFFFFFFFF, b"0:4294967200,10C:700")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            indexer.initialise()
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript("""
                    CREATE TABLE deletes(sequence INTEGER);
                    CREATE TRIGGER observe_delete AFTER DELETE ON sample
                      BEGIN INSERT INTO deletes VALUES(OLD.sequence); END;
                """)
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DELETE FROM deletes")
                connection.commit()
            self.write_inbox_record(root, "CAR", session, 0, b"0:100,10C:710")
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deletes").fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT capture_sequence FROM sample ORDER BY sequence"
                ).fetchall(), [(0xFFFFFFFF,), (0,)])
                self.assertEqual(connection.execute(
                    "SELECT missing_capture_sequence_count FROM trip"
                ).fetchone(), (0,))

    def test_incremental_append_updates_capture_and_interval_gap_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            session = "000000000000000b"
            self.write_inbox_record(
                root, "CAR", session, 10,
                b"0:1000,90:1791030012,91:345,10C:700",
            )
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(root, database)
            self.assertEqual(indexer.index_once(), 1)
            self.write_inbox_record(
                root, "CAR", session, 13,
                b"0:5000,90:1791030016,91:345,10C:710",
            )

            self.assertEqual(indexer.index_once(), 1)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT sample_count,missing_capture_sequence_count,gap_count,"
                    "over_target_interval_count,timeline_end_ms FROM trip"
                ).fetchone(), (2, 2, 1, 1, 4000))
                self.assertEqual(connection.execute(
                    "SELECT capture_utc_ms,timeline_ms FROM sample ORDER BY sequence"
                ).fetchall(), [(1791030012345, 0), (1791030016345, 4000)])

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

    def test_gap_view_reports_over_target_intervals_but_not_on_cadence_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026/08/27/20260827-001247.txt"
            archive.parent.mkdir(parents=True)
            # 251 ms is merely over target, while 500 ms spans a likely missed
            # frame; the projection labels both as observed over-target intervals.
            archive.write_text("0:100,10C:900,0:350,10C:910,0:601,10C:920,0:1101,10C:930,0:1351,10C:940\n")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 1_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                intervals = connection.execute(
                    "SELECT previous_sequence, sequence, gap_ms FROM sample_over_target_intervals"
                ).fetchall()
                gap_count = connection.execute(
                    "SELECT gap_count FROM trip"
                ).fetchone()[0]
                over_target_count = connection.execute(
                    "SELECT over_target_interval_count FROM trip"
                ).fetchone()[0]
                self.assertEqual(intervals, [(1, 2, 251), (2, 3, 500)])
                self.assertEqual(gap_count, 0)
                self.assertEqual(over_target_count, 2)

    def test_initialise_refreshes_both_cached_interval_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            database = Path(directory) / "history.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    (Path(__file__).with_name("history_schema.sql")).read_text()
                )
                connection.execute(
                    "INSERT INTO trip(device_id, trip_id, archive_path, collector_login_ms, "
                    "timestamp_quality, archive_mtime_ms, updated_at_ms, gap_count) "
                    "VALUES ('CAR', '20260827-001247', '/archive.txt', 1, 'unknown', 1, 1, 0)"
                )
                connection.executemany(
                    "INSERT INTO sample(device_id, trip_id, sequence, device_monotonic_ms, "
                    "archive_mtime_ms, timestamp_quality) VALUES ('CAR', '20260827-001247', ?, ?, 1, 'unknown')",
                    enumerate((100, 350, 850, 1100)),
                )
                connection.commit()

            HistoryIndexer(root, database).initialise()
            with closing(sqlite3.connect(database)) as connection:
                gap_count, over_target_count = connection.execute(
                    "SELECT gap_count, over_target_interval_count FROM trip WHERE device_id='CAR'"
                ).fetchone()
                self.assertEqual(gap_count, 0)
                self.assertEqual(over_target_count, 1)

    def test_gap_view_uses_wrap_safe_32bit_device_clock_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026/08/27/20260827-001247.txt"
            archive.parent.mkdir(parents=True)
            archive.write_text("0:4294967000,10C:900,0:500,10C:901\n")
            database = Path(directory) / "history.sqlite"
            indexer = HistoryIndexer(
                root,
                database,
                now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000,
            )
            indexer.index_once()
            with closing(sqlite3.connect(database)) as connection:
                gap = connection.execute(
                    "SELECT previous_sequence, sequence, gap_ms FROM sample_over_target_intervals"
                ).fetchone()
                self.assertEqual(gap, (0, 1, 796))

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
