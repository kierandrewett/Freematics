#!/usr/bin/env python3
"""Build an idempotent, timestamped SQLite projection of Freematics archives.

The collector's raw ``.txt`` files are the source of truth.  This projection is
deliberately rebuildable and stores both the device monotonic clock and the
collector receipt approximation.  Prometheus is not used for this history:
pull-scraped gauges cannot accept a later backlog at its original timestamp.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from telemetry_catalog import metric_catalog
except ImportError:  # pragma: no cover - supports package imports
    from .telemetry_catalog import metric_catalog

TRIP_ID_RE = re.compile(r"^(\d{8})-(\d{6})$")
FIELD_RE = re.compile(r"(?:^|,)([0-9A-Fa-f]+)[:=]([^,\r\n]*)")
FRAME_RE = re.compile(r"(?:^|,)0[:=](\d+)(?=,|$)", re.MULTILINE)
CATALOGUE_RE = re.compile(
    r'OBD_PID\(0x([0-9A-Fa-f]+),\s*([A-Za-z0-9_]+),\s*"([^"]*)",\s*"([^"]*)",\s*(\d+)\)'
)
SEAL_AFTER_SECONDS = 60
GAP_THRESHOLD_MS = 3_000
GPS_HDOP_POOR_THRESHOLD = 5.0
SPEED_DISAGREEMENT_THRESHOLD_KPH = 10.0
DTC_CODE_SLOTS = 16
DTC_GROUPS = (
    ("stored", "300", "301"),
    ("pending", "320", "321"),
    ("permanent", "340", "341"),
)
DTC_PREFIXES = "PCBU"
DTC_SYSTEMS = ("powertrain", "chassis", "body", "network")
_REQUIRED_COLUMNS = {
    "trip": {
        "device_id", "trip_id", "archive_path", "collector_login_ms",
        "start_capture_ms", "end_capture_ms", "timeline_start_ms", "timeline_end_ms",
        "time_basis", "timestamp_quality", "sample_count", "data_bytes", "gap_count",
        "archive_mtime_ms", "updated_at_ms",
    },
    "sample": {
        "device_id", "trip_id", "sequence", "device_monotonic_ms", "capture_utc_ms",
        "timeline_ms", "time_basis", "collector_received_ms", "archive_mtime_ms",
        "timestamp_quality", "latitude", "longitude", "gps_speed_kph",
        "gps_heading_degrees", "gps_hdop", "gps_satellites",
    },
    "sample_metric": {"device_id", "trip_id", "sequence", "pid", "numeric_value", "text_value"},
}
_REQUIRED_PRIMARY_KEYS = {
    "trip": ("device_id", "trip_id"),
    "sample": ("device_id", "trip_id", "sequence"),
    "sample_metric": ("device_id", "trip_id", "sequence", "pid"),
}


@dataclass(frozen=True)
class Frame:
    device_monotonic_ms: int
    fields: dict[str, str]
    ordered_fields: tuple[tuple[str, str], ...]


def trip_start_ms(trip_id: str) -> int:
    match = TRIP_ID_RE.fullmatch(trip_id)
    if not match:
        return 0
    date, clock = match.groups()
    try:
        parsed = datetime.strptime(date + clock, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1_000)


def normalise_pid(raw_pid: str) -> str:
    return f"0x{int(raw_pid, 16):03X}"


def numeric(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.split("*", 1)[0].strip()
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def parse_frames(raw: str, include_final: bool = False) -> list[Frame]:
    """Parse complete PID-0-delimited frames from one archive snapshot.

    A frame is complete when the following PID 0 has arrived. The final frame
    is included only for a file known to be sealed; this prevents a partially
    written request from becoming permanent history.
    """

    starts = list(FRAME_RE.finditer(raw))
    frames: list[Frame] = []
    end = len(starts) if include_final else max(0, len(starts) - 1)
    for index in range(end):
        start = starts[index].start()
        if raw[start] == ",":
            start += 1
        stop = starts[index + 1].start() if index + 1 < len(starts) else len(raw)
        segment = raw[start:stop]
        match = FRAME_RE.match(segment)
        if not match:
            continue
        fields: dict[str, str] = {}
        ordered_fields: list[tuple[str, str]] = []
        for field in FIELD_RE.finditer(segment):
            pid = field.group(1).upper()
            if pid == "0":
                continue
            value = field.group(2).split("*", 1)[0]
            fields[pid] = value
            ordered_fields.append((pid, value))
        frames.append(Frame(int(match.group(1)), fields, tuple(ordered_fields)))
    return frames


def gnss_capture_ms(fields: dict[str, str]) -> int | None:
    """Decode the device's YYMMDD/YYYYMMDD + HHMMSScc fields.

    The firmware stores the date as an integer and the time as HHMMSScc.  A
    missing or malformed pair is deliberately treated as unavailable; the
    collector login time must never be presented as the sample's capture time.
    """
    raw_date = fields.get("11")
    raw_time = fields.get("10")
    if raw_date is None or raw_time is None:
        return None
    try:
        date_text = str(int(raw_date))
        time_text = str(int(raw_time)).zfill(8)
        if len(date_text) == 6:
            year = 2000 + int(date_text[:2])
            month = int(date_text[2:4])
            day = int(date_text[4:6])
        elif len(date_text) == 8:
            year = int(date_text[:4])
            month = int(date_text[4:6])
            day = int(date_text[6:8])
        else:
            return None
        hour = int(time_text[:2])
        minute = int(time_text[2:4])
        second = int(time_text[4:6])
        centisecond = int(time_text[6:8])
        parsed = datetime(year, month, day, hour, minute, second, centisecond * 10_000, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp() * 1_000)


def frame_timestamps(frames: list[Frame]) -> tuple[list[int | None], list[str]]:
    """Return capture timestamps and evidence quality for each frame.

    GNSS-bearing frames are authoritative.  Frames without GNSS can be
    interpolated/extrapolated from a neighbouring GNSS anchor using the device
    monotonic clock and are marked ``anchored``.  With no anchor, timestamps
    stay NULL and are marked ``unknown`` rather than being fabricated from the
    collector-created trip filename.
    """
    captures: list[int | None] = [gnss_capture_ms(frame.fields) for frame in frames]
    qualities = ["gnss" if capture is not None else "unknown" for capture in captures]
    anchors = [index for index, capture in enumerate(captures) if capture is not None]
    if not anchors:
        return captures, qualities
    for index, frame in enumerate(frames):
        if captures[index] is not None:
            continue
        previous = max((anchor for anchor in anchors if anchor < index), default=None)
        following = min((anchor for anchor in anchors if anchor > index), default=None)
        anchor = previous if previous is not None else following
        if anchor is None or captures[anchor] is None:
            continue
        delta = frame.device_monotonic_ms - frames[anchor].device_monotonic_ms
        # A reboot/reset invalidates monotonic interpolation across the reset.
        if previous is not None and frame.device_monotonic_ms < frames[previous].device_monotonic_ms:
            continue
        if following is not None and frame.device_monotonic_ms > frames[following].device_monotonic_ms:
            continue
        captures[index] = captures[anchor] + delta
        qualities[index] = "anchored"
    return captures, qualities


def display_timestamps(
    frames: list[Frame], captures: list[int | None], qualities: list[str], login_ms: int
) -> tuple[list[int], list[str]]:
    """Build a navigable timeline without confusing it with capture UTC.

    Legacy archives have a reliable collector-created session start and a
    monotonic device offset but no date.  They are therefore plotted on a
    session-relative timeline and labelled ``collector_session``.  The strict
    ``capture_utc_ms`` field remains NULL for those rows.
    """
    if not frames:
        return [], []
    first = frames[0].device_monotonic_ms
    timeline = [capture if capture is not None else login_ms + frame.device_monotonic_ms - first for frame, capture in zip(frames, captures)]
    basis = [quality if quality != "unknown" else "collector_session" for quality in qualities]
    return timeline, basis


def gps_value(fields: dict[str, str], pid: str) -> float | None:
    return numeric(fields.get(pid))

def tracking_quality(frames: list[Frame]) -> tuple[int, int, int]:
    """Count fixes, poor-HDOP samples, and OBD/GNSS speed disagreements."""

    gps_fixes = 0
    poor_hdop = 0
    speed_disagreements = 0
    for frame in frames:
        fields = frame.fields
        latitude = gps_value(fields, "A")
        longitude = gps_value(fields, "B")
        if latitude is not None and longitude is not None:
            gps_fixes += 1
        hdop = gps_value(fields, "12")
        if hdop is not None and hdop * 0.1 > GPS_HDOP_POOR_THRESHOLD:
            poor_hdop += 1
        obd_speed = gps_value(fields, "10D")
        gps_speed = gps_value(fields, "D")
        if obd_speed is not None and gps_speed is not None and abs(obd_speed - gps_speed) > SPEED_DISAGREEMENT_THRESHOLD_KPH:
            speed_disagreements += 1
    return gps_fixes, poor_hdop, speed_disagreements


def acceleration_values(fields: dict[str, str]) -> tuple[float | None, float | None, float | None]:
    """Decode the semicolon-delimited MEMS acceleration field when complete."""
    raw = fields.get("20")
    if raw is None:
        return None, None, None
    values = [numeric(part) for part in raw.split(";")]
    if len(values) != 3 or any(value is None for value in values):
        # Keep malformed vectors in sample_metric.text_value instead of making
        # a partial vector look like measured acceleration.
        return None, None, None
    return values[0], values[1], values[2]


def diagnostic_code(raw_code: int) -> tuple[str, str]:
    """Format the uint16 DTC representation used by teleserver.c."""
    raw_code &= 0xFFFF
    family = raw_code >> 14
    return (
        f"{DTC_PREFIXES[family]}{(raw_code >> 12) & 0x3:X}{raw_code & 0xFFF:03X}",
        DTC_SYSTEMS[family],
    )


def diagnostic_rows(fields: dict[str, str]):
    """Yield decoded DTC detail while retaining raw fields in sample_metric."""
    for status, count_pid, base_pid in DTC_GROUPS:
        count = numeric(fields.get(count_pid))
        slot_limit = DTC_CODE_SLOTS
        if count is not None:
            slot_limit = max(0, min(DTC_CODE_SLOTS, int(count)))
        for slot in range(slot_limit):
            raw = numeric(fields.get(f"{int(base_pid, 16) + slot:X}"))
            if raw is None or int(raw) == 0:
                continue
            value = int(raw) & 0xFFFF
            code, system = diagnostic_code(value)
            yield status, slot, value, code, system


def catalogue_category(pid: int) -> str:
    if pid in {0x01, 0x02, 0x03, 0x1C, 0x1E, 0x51}:
        return "status"
    if pid in {0x05, 0x0F, 0x3C, 0x3D, 0x3E, 0x3F, 0x46, 0x5C}:
        return "temperature"
    if pid in {0x06, 0x07, 0x08, 0x09, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2D, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x44}:
        return "emissions"
    if pid in {0x0A, 0x0B, 0x22, 0x23, 0x32, 0x33, 0x53, 0x54, 0x59}:
        return "pressure"
    if pid in {0x10, 0x12, 0x2C, 0x2E, 0x45, 0x47, 0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x5A, 0x61, 0x62}:
        return "air_and_demand"
    if pid in {0x1F, 0x21, 0x30, 0x31, 0x4D, 0x4E, 0xA6}:
        return "service"
    if pid in {0x42, 0x43, 0x52, 0x5B, 0x5D, 0x5E, 0x63}:
        return "fuel_and_power"
    return "powertrain"


class HistoryIndexer:
    def __init__(
        self,
        archive_root: Path,
        database: Path,
        now_ms: Callable[[], int] | None = None,
        *,
        rebuild: bool = False,
    ) -> None:
        self.archive_root = archive_root
        self.database = database
        self.now_ms = now_ms or (lambda: int(time.time() * 1_000))
        self.rebuild = rebuild
    @staticmethod
    def _schema_issue(connection: sqlite3.Connection) -> str | None:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in {"sqlite_sequence"}
        }
        if not tables:
            return None
        for table, columns in _REQUIRED_COLUMNS.items():
            if table not in tables:
                return f"missing table {table}"
            actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = sorted(columns - actual)
            if missing:
                return f"{table} is missing columns: {', '.join(missing)}"
            primary_key = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})") if row[5])
            if primary_key != _REQUIRED_PRIMARY_KEYS[table]:
                return f"{table} has primary key {primary_key!r}, expected {_REQUIRED_PRIMARY_KEYS[table]!r}"
        return None

    def _prepare_database(self) -> None:
        if not self.database.exists():
            return
        with closing(sqlite3.connect(self.database)) as connection:
            issue = self._schema_issue(connection)
            if issue is not None and self.rebuild:
                connection.execute("PRAGMA wal_checkpoint(FULL)")
        if issue is None:
            return
        if not self.rebuild:
            raise RuntimeError(
                f"incompatible history database: {issue}; use --rebuild with a verified backup before rebuilding"
            )
        backup = self.database.with_name(f"{self.database.name}.backup-{self.now_ms()}")
        if backup.exists():
            raise RuntimeError(f"history backup already exists: {backup}")
        shutil.copy2(self.database, backup)
        self.database.unlink()
        for suffix in ("-wal", "-shm"):
            self.database.with_name(self.database.name + suffix).unlink(missing_ok=True)


    def initialise(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_database()
        with closing(sqlite3.connect(self.database)) as connection:
            self._ensure_sample_columns(connection)
            self._ensure_trip_columns(connection)
            connection.executescript((Path(__file__).with_name("history_schema.sql")).read_text())
            self._populate_catalogue(connection)
            connection.commit()

    @staticmethod
    def _ensure_sample_columns(connection: sqlite3.Connection) -> None:
        """Apply additive columns so an existing projection can be rebuilt safely."""
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sample)")}
        if not columns:
            return
        for name in ("acceleration_x_g", "acceleration_y_g", "acceleration_z_g"):
            if name not in columns:
                connection.execute(f"ALTER TABLE sample ADD COLUMN {name} REAL")

    @staticmethod
    def _ensure_trip_columns(connection: sqlite3.Connection) -> None:
        """Apply additive trip quality columns to an existing current schema."""

        columns = {row[1] for row in connection.execute("PRAGMA table_info(trip)")}
        if not columns:
            return
        for name in ("gps_fix_count", "gps_poor_quality_count", "speed_disagreement_count"):
            if name not in columns:
                connection.execute(f"ALTER TABLE trip ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")

    def _populate_catalogue(self, connection: sqlite3.Connection) -> None:
        """Populate standard and device metric metadata from one catalogue."""

        priorities: dict[int, int] = {}
        catalogue_path = Path(__file__).parent.parent / "obd_pids.h"
        if catalogue_path.exists():
            for raw_pid, _name, _description, _unit, priority in CATALOGUE_RE.findall(catalogue_path.read_text()):
                pid = 0x100 | int(raw_pid, 16)
                priorities[pid] = int(priority)

        for pid, definition in metric_catalog().items():
            if pid >= 0x100:
                category = catalogue_category(pid - 0x100)
            else:
                category = definition.namespace.rsplit("/", 1)[-1]
            connection.execute(
                """INSERT INTO metric_catalogue(pid, name, description, unit, priority, category)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(pid) DO UPDATE SET name=excluded.name,
                     description=excluded.description, unit=excluded.unit,
                     priority=excluded.priority, category=excluded.category""",
                (
                    normalise_pid(f"{pid:X}"),
                    definition.key,
                    definition.description,
                    definition.unit,
                    priorities.get(pid, 3),
                    category,
                ),
            )

    def index_once(self) -> int:
        self.initialise()
        files = sorted(self.archive_root.glob("*/????/??/??/*.txt"))
        indexed = 0
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            for archive in files:
                if self._index_file(connection, archive):
                    indexed += 1
            connection.commit()
        return indexed

    def _index_file(self, connection: sqlite3.Connection, archive: Path) -> bool:
        trip_id = archive.stem
        device_id = archive.parent.parent.parent.parent.name
        if not TRIP_ID_RE.fullmatch(trip_id) or not device_id:
            return False
        raw_bytes = archive.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        stat = archive.stat()
        previous = connection.execute(
            "SELECT content_sha256, byte_size, sealed, mutation_detected FROM ingest_file WHERE archive_path = ?",
            (str(archive),),
        ).fetchone()
        mutation = int(bool(previous and previous[2] and (previous[0] != digest or previous[1] != stat.st_size)))
        sealed = int(self.now_ms() - int(stat.st_mtime * 1_000) >= SEAL_AFTER_SECONDS * 1_000)
        if previous and previous[0] == digest and previous[1] == stat.st_size and previous[2] == sealed:
            # The archive is append-only. Avoid rewriting all sample rows (and
            # advancing updated_at/indexed_at) when a polling pass saw no new
            # bytes.
            return False
        frames = parse_frames(raw_bytes.decode("utf-8", errors="replace"), include_final=bool(sealed))
        login_ms = trip_start_ms(trip_id)
        captures, qualities = frame_timestamps(frames)
        timestamp_quality = (
            "unknown"
            if not frames or all(quality == "unknown" for quality in qualities)
            else "gnss"
            if all(quality == "gnss" for quality in qualities)
            else "partial"
        )
        timelines, time_bases = display_timestamps(frames, captures, qualities, login_ms)
        gap_count = sum(
            1 for previous_frame, frame in zip(frames, frames[1:])
            if frame.device_monotonic_ms - previous_frame.device_monotonic_ms > GAP_THRESHOLD_MS
        )
        gps_fix_count, gps_poor_quality_count, speed_disagreement_count = tracking_quality(frames)

        known_captures = [capture for capture in captures if capture is not None]
        capture_start_ms = known_captures[0] if known_captures else None
        capture_end_ms = known_captures[-1] if known_captures else None

        # Samples carry a foreign key to their trip.  Upsert the trip shell
        # before replacing its sample rows; the final aggregate values are
        # written again below once the rows have been materialised.
        connection.execute(
            """INSERT INTO trip(
                device_id, trip_id, archive_path, collector_login_ms,
                start_capture_ms, end_capture_ms, timestamp_quality,
                sample_count, data_bytes, gap_count, gps_fix_count,
                gps_poor_quality_count, speed_disagreement_count, timeline_start_ms,
                timeline_end_ms, time_basis, archive_mtime_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, trip_id) DO UPDATE SET
                archive_path=excluded.archive_path,
                collector_login_ms=excluded.collector_login_ms,
                start_capture_ms=excluded.start_capture_ms,
                end_capture_ms=excluded.end_capture_ms,
                timestamp_quality=excluded.timestamp_quality,
                sample_count=excluded.sample_count, data_bytes=excluded.data_bytes,
                gap_count=excluded.gap_count, gps_fix_count=excluded.gps_fix_count,
                gps_poor_quality_count=excluded.gps_poor_quality_count,
                speed_disagreement_count=excluded.speed_disagreement_count,
                timeline_start_ms=excluded.timeline_start_ms,
                timeline_end_ms=excluded.timeline_end_ms,
                time_basis=excluded.time_basis,
                archive_mtime_ms=excluded.archive_mtime_ms,
                updated_at_ms=excluded.updated_at_ms""",
            (
                device_id,
                trip_id,
                str(archive),
                login_ms,
                capture_start_ms,
                capture_end_ms,
                timestamp_quality,
                len(frames),
                stat.st_size,
                gap_count,
                gps_fix_count,
                gps_poor_quality_count,
                speed_disagreement_count,
                timelines[0] if timelines else None,
                timelines[-1] if timelines else None,
                "unknown" if not frames or all(basis == "unknown" for basis in time_bases)
                else "gnss" if all(basis == "gnss" for basis in time_bases)
                else "partial",
                int(stat.st_mtime * 1_000),
                self.now_ms(),
            ),
        )

        connection.execute("DELETE FROM sample_metric WHERE device_id = ? AND trip_id = ?", (device_id, trip_id))
        connection.execute("DELETE FROM sample WHERE device_id = ? AND trip_id = ?", (device_id, trip_id))
        for sequence, (frame, capture_ms, quality) in enumerate(zip(frames, captures, qualities)):
            fields = frame.fields
            acceleration_x_g, acceleration_y_g, acceleration_z_g = acceleration_values(fields)
            hdop = gps_value(fields, "12")
            connection.execute(
                """INSERT INTO sample(
                    device_id, trip_id, sequence, device_monotonic_ms, capture_utc_ms,
                    timeline_ms, time_basis, collector_received_ms, archive_mtime_ms,
                    timestamp_quality, latitude, longitude,
                    gps_speed_kph, gps_heading_degrees, gps_hdop, gps_satellites,
                    acceleration_x_g, acceleration_y_g, acceleration_z_g
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    device_id,
                    trip_id,
                    sequence,
                    frame.device_monotonic_ms,
                    capture_ms,
                    timelines[sequence],
                    time_bases[sequence],
                    None,
                    int(stat.st_mtime * 1_000),
                    quality,
                    gps_value(fields, "A"),
                    gps_value(fields, "B"),
                    gps_value(fields, "D"),
                    gps_value(fields, "E"),
                    hdop * 0.1 if hdop is not None else None,
                    int(gps_value(fields, "F")) if gps_value(fields, "F") is not None else None,
                    acceleration_x_g,
                    acceleration_y_g,
                    acceleration_z_g,
                ),
            )
            for raw_pid, value in fields.items():
                parsed = numeric(value)
                connection.execute(
                    "INSERT INTO sample_metric(device_id, trip_id, sequence, pid, numeric_value, text_value) VALUES (?, ?, ?, ?, ?, ?)",
                    (device_id, trip_id, sequence, normalise_pid(raw_pid), parsed, value if parsed is None else None),
                )
            for ordinal, (raw_pid, value) in enumerate(frame.ordered_fields):
                parsed = numeric(value)
                connection.execute(
                    "INSERT INTO sample_field(device_id, trip_id, sequence, ordinal, pid, numeric_value, text_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        device_id,
                        trip_id,
                        sequence,
                        ordinal,
                        normalise_pid(raw_pid),
                        parsed,
                        value if parsed is None else None,
                    ),
                )
            for status, slot, raw_code, code, system in diagnostic_rows(fields):
                connection.execute(
                    "INSERT INTO diagnostic_code(device_id, trip_id, sequence, status, slot, raw_code, code, system) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (device_id, trip_id, sequence, status, slot, raw_code, code, system),
                )

        start_ms = capture_start_ms
        end_ms = capture_end_ms
        connection.execute(
            "UPDATE trip SET start_capture_ms = ?, end_capture_ms = ?, sample_count = ?, timeline_start_ms = ?, timeline_end_ms = ?, updated_at_ms = ? WHERE device_id = ? AND trip_id = ?",
            (start_ms, end_ms, len(frames), timelines[0] if timelines else None, timelines[-1] if timelines else None, self.now_ms(), device_id, trip_id),
        )
        connection.execute(
            """INSERT INTO ingest_file(archive_path, content_sha256, byte_size, processed_bytes, sealed, mutation_detected, indexed_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_path) DO UPDATE SET
                content_sha256=excluded.content_sha256, byte_size=excluded.byte_size,
                processed_bytes=excluded.processed_bytes, sealed=excluded.sealed,
                mutation_detected=MAX(ingest_file.mutation_detected, excluded.mutation_detected),
                indexed_at_ms=excluded.indexed_at_ms""",
            (str(archive), digest, stat.st_size, stat.st_size, sealed, mutation, self.now_ms()),
        )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=Path("/data"))
    parser.add_argument("--database", type=Path, default=Path("/history/history.sqlite"))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Back up and replace an incompatible SQLite projection before indexing",
    )
    args = parser.parse_args()
    indexer = HistoryIndexer(args.archive_root, args.database, rebuild=args.rebuild)
    while True:
        print(f"[HISTORY] indexed {indexer.index_once()} archive files", flush=True)
        if args.once:
            return
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
