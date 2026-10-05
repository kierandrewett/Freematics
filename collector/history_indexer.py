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
import zlib
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
FIELD_RE = re.compile(r"(?:^|,)([0-9A-Fa-f]{1,4})[:=]([^,\r\n]*)")
FRAME_RE = re.compile(r"(?:^|,)0[:=](\d{1,10})(?=,|$)", re.MULTILINE)
MAX_ARCHIVE_RECORD_SIZE = 64 * 1024
MAX_ARCHIVE_FILE_SIZE = 64 * 1024 * 1024
MAX_INBOX_RECORD_SIZE = 8 * 1024 + 160
CATALOGUE_RE = re.compile(
    r'OBD_PID\(0x([0-9A-Fa-f]+),\s*([A-Za-z0-9_]+),\s*"([^"]*)",\s*"([^"]*)",\s*(\d+)\)'
)
SEAL_AFTER_SECONDS = 60
# Preserve the established quality-gate definition of a material capture gap.
GAP_THRESHOLD_MS = 3_000
# Separately expose every interval that exceeds the nominal 250 ms cadence.
OVER_TARGET_INTERVAL_THRESHOLD_MS = 250
GPS_HDOP_POOR_THRESHOLD = 5.0
SPEED_DISAGREEMENT_THRESHOLD_KPH = 10.0
DTC_CODE_SLOTS = 15
DTC_GROUPS = (
    ("stored", "300", "301", "310"),
    ("pending", "320", "321", "330"),
    ("permanent", "340", "341", "350"),
)
DTC_PREFIXES = "PCBU"
DTC_SYSTEMS = ("powertrain", "chassis", "body", "network")
INBOX_PATH_RE = re.compile(r"^([0-9A-Fa-f]{16})-([0-9]+)\.fqi$")
INBOX_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
UINT32_MODULUS = 1 << 32
_REQUIRED_COLUMNS = {
    "trip": {
        "device_id", "trip_id", "archive_path", "collector_login_ms",
        "start_capture_ms", "end_capture_ms", "timeline_start_ms", "timeline_end_ms",
        "time_basis", "timestamp_quality", "sample_count", "data_bytes", "gap_count",
        "over_target_interval_count",
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


@dataclass(frozen=True)
class InboxRecord:
    device_id: str
    session_id: str
    capture_sequence: int
    frame: Frame
    payload_sha256: str
    source_path: str
    collector_received_ms: int


def parse_inbox_record(path: Path) -> InboxRecord | None:
    """Validate an immutable FQI1 record; invalid files remain on disk."""
    match = INBOX_PATH_RE.fullmatch(path.name)
    device_id = path.parent.name
    if (not match or len(device_id) > 128 or device_id.startswith(".")
            or not INBOX_DEVICE_RE.fullmatch(device_id)):
        return None
    session_id, raw_sequence = match.groups()
    try:
        sequence = int(raw_sequence)
    except ValueError:
        return None
    if sequence >= UINT32_MODULUS:
        return None
    try:
        if path.stat().st_size > MAX_INBOX_RECORD_SIZE:
            return None
        raw = path.read_bytes()
        header, payload = raw.split(b"\n", 1)
        fields = header.decode("ascii").split(",")
        if len(fields) != 5 or fields[0] != "FQI1":
            return None
        header_session = fields[1]
        header_sequence, payload_length = int(fields[2]), int(fields[3])
        checksum = fields[4]
        if (not re.fullmatch(r"[0-9A-Fa-f]{16}", header_session)
                or header_session.lower() != session_id.lower()
                or header_sequence != sequence
                or str(header_sequence) != raw_sequence
                or payload_length != len(payload)
                or not re.fullmatch(r"[0-9a-f]{8}", checksum)
                or int(checksum, 16) != zlib.crc32(payload)):
            return None
        text = payload.decode("utf-8")
        if not re.match(r"\A0[:=][0-9]{1,10}(?:,|$)", text):
            return None
        # The FQI payload is one PID-0-delimited telemetry sample. A synthetic
        # next delimiter lets the archive parser apply its established rules.
        frames = parse_frames(text.rstrip(",") + ",0:0", include_final=False)
        if len(frames) != 1:
            return None
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    stat = path.stat()
    return InboxRecord(
        device_id, session_id.lower(), sequence, frames[0],
        hashlib.sha256(payload).hexdigest(), str(path), int(stat.st_mtime * 1_000),
    )


def order_wrapped_sequences(records: list[InboxRecord]) -> list[InboxRecord]:
    """Order uint32 sequence values from the largest missing arc's successor."""
    ordered = sorted(records, key=lambda record: record.capture_sequence)
    if len(ordered) < 2:
        return ordered
    gaps = [
        (ordered[(index + 1) % len(ordered)].capture_sequence - record.capture_sequence - 1)
        % UINT32_MODULUS
        for index, record in enumerate(ordered)
    ]
    # Starting after the largest absent arc yields the unique compact run
    # ordering even when the device counter rolls from UINT32_MAX to zero.
    start = (gaps.index(max(gaps)) + 1) % len(ordered)
    return ordered[start:] + ordered[:start]


def missing_sequence_count(records: list[InboxRecord]) -> int:
    ordered = order_wrapped_sequences(records)
    if len(ordered) < 2:
        return 0
    gaps = [
        (ordered[(index + 1) % len(ordered)].capture_sequence - record.capture_sequence - 1)
        % UINT32_MODULUS
        for index, record in enumerate(ordered)
    ]
    # The largest absent arc is outside the observed session span; count only
    # holes between its endpoints, including actual uint32 rollover holes.
    return sum(gaps) - max(gaps)


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
        if len(segment) > MAX_ARCHIVE_RECORD_SIZE:
            continue
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
        timestamp = int(match.group(1))
        if timestamp > 0xFFFFFFFF:
            continue
        frames.append(Frame(timestamp, fields, tuple(ordered_fields)))
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
        # Firmware stores DDMMYY as an integer, so dates on days 01-09 lose
        # their leading zero (for example, 03-10-26 is emitted as 31026).
        if len(date_text) == 5:
            date_text = date_text.zfill(6)
        time_text = str(int(raw_time)).zfill(8)
        if len(date_text) == 6:
            # Freematics PID 0x11 is UTC date in DDMMYY order.
            day = int(date_text[:2])
            month = int(date_text[2:4])
            year = 2000 + int(date_text[4:6])
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
    # PID 0x93 is elapsed time since the held GNSS fix. Repeated UTC fields
    # identify that fix, while each sample still has its own capture time.
    age = numeric(fields.get("93")) if "93" in fields else 0
    if age is None or age < 0 or age > 0xFFFFFFFF or age != int(age):
        return None
    return int(parsed.timestamp() * 1_000) + int(age)


def device_clock_capture_ms(fields: dict[str, str]) -> int | None:
    """Decode the journal's exact UTC epoch fields, rejecting non-integers."""
    raw_seconds = fields.get("90")
    raw_milliseconds = fields.get("91")
    if raw_seconds is None or raw_milliseconds is None:
        return None
    if not re.fullmatch(r"[0-9]+", raw_seconds) or not re.fullmatch(r"[0-9]+", raw_milliseconds):
        return None
    seconds = int(raw_seconds)
    milliseconds = int(raw_milliseconds)
    if not 1_704_067_200 <= seconds <= 0xFFFFFFFF or not 0 <= milliseconds <= 999:
        return None
    return seconds * 1_000 + milliseconds


def monotonic_delta(current: int, previous: int) -> int:
    """Signed elapsed milliseconds across a 32-bit clock rollover."""
    return (current - previous + 0x80000000) % 0x100000000 - 0x80000000


def frame_timestamps(frames: list[Frame]) -> tuple[list[int | None], list[str]]:
    """Return capture timestamps and evidence quality for each frame.

    GNSS-bearing frames are authoritative.  Frames without GNSS can be
    interpolated/extrapolated from a neighbouring GNSS anchor using the device
    monotonic clock and are marked ``anchored``.  With no anchor, timestamps
    stay NULL and are marked ``unknown`` rather than being fabricated from the
    collector-created trip filename.
    """
    captures: list[int | None] = []
    qualities: list[str] = []
    for frame in frames:
        device_clock = device_clock_capture_ms(frame.fields)
        if device_clock is not None:
            captures.append(device_clock)
            qualities.append("device_clock")
            continue
        gnss = gnss_capture_ms(frame.fields)
        captures.append(gnss)
        qualities.append(
            ("anchored" if numeric(frame.fields.get("93")) else "gnss")
            if gnss is not None else "unknown"
        )
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
        delta = monotonic_delta(frame.device_monotonic_ms, frames[anchor].device_monotonic_ms)
        # A reboot/reset invalidates monotonic interpolation across the reset.
        if previous is not None and monotonic_delta(frame.device_monotonic_ms, frames[previous].device_monotonic_ms) < 0:
            continue
        if following is not None and monotonic_delta(frame.device_monotonic_ms, frames[following].device_monotonic_ms) > 0:
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
    timeline: list[int] = []
    basis: list[str] = []
    offset = login_ms - first
    previous_tick = first
    previous_timeline = login_ms
    for index, (frame, capture, quality) in enumerate(zip(frames, captures, qualities)):
        tick = frame.device_monotonic_ms
        if index == 0:
            candidate = capture if capture is not None else login_ms
            offset = candidate - tick
        else:
            if monotonic_delta(tick, previous_tick) < 0:
                offset = previous_timeline - tick
            elif tick < previous_tick:
                offset += 0x100000000
            candidate = capture if capture is not None else tick + offset
            if candidate < previous_timeline:
                candidate = previous_timeline
                offset = candidate - tick
            elif capture is not None:
                offset = candidate - tick
        timeline.append(candidate)
        basis.append(quality if quality != "unknown" else "collector_session")
        previous_tick = tick
        previous_timeline = candidate
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
    for status, count_pid, base_pid, _status_pid in DTC_GROUPS:
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
        self._initialized = False
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
            connection.execute("PRAGMA busy_timeout = 30000")
            self._ensure_sample_columns(connection)
            self._ensure_trip_columns(connection)
            connection.executescript((Path(__file__).with_name("history_schema.sql")).read_text())
            self._refresh_interval_count_projections(connection)
            self._populate_catalogue(connection)
            connection.commit()
        self._initialized = True

    @staticmethod
    def _refresh_interval_count_projections(connection: sqlite3.Connection) -> None:
        """Recompute cached interval counts when either threshold changes."""
        projections = (
            ("gap_threshold_ms", GAP_THRESHOLD_MS, "gap_count", "sample_gaps"),
            ("over_target_interval_threshold_ms", OVER_TARGET_INTERVAL_THRESHOLD_MS,
             "over_target_interval_count", "sample_over_target_intervals"),
        )
        for key, threshold, column, view in projections:
            current = connection.execute(
                "SELECT value FROM history_projection_meta WHERE key=?", (key,)
            ).fetchone()
            if current and current[0] == str(threshold):
                continue
            connection.execute(
                f"""UPDATE trip SET {column}=(
                       SELECT COUNT(*) FROM {view} AS intervals
                       WHERE intervals.device_id=trip.device_id AND intervals.trip_id=trip.trip_id
                   )"""
            )
            connection.execute(
                """INSERT INTO history_projection_meta(key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, str(threshold)),
            )

    @staticmethod
    def _ensure_sample_columns(connection: sqlite3.Connection) -> None:
        """Apply additive columns so an existing projection can be rebuilt safely."""
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sample)")}
        if not columns:
            return
        for name in ("acceleration_x_g", "acceleration_y_g", "acceleration_z_g"):
            if name not in columns:
                connection.execute(f"ALTER TABLE sample ADD COLUMN {name} REAL")
        for name, declaration in (
            ("capture_session_id", "TEXT"),
            ("capture_sequence", "INTEGER"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE sample ADD COLUMN {name} {declaration}")

    @staticmethod
    def _ensure_trip_columns(connection: sqlite3.Connection) -> None:
        """Apply additive trip quality columns to an existing current schema."""

        columns = {row[1] for row in connection.execute("PRAGMA table_info(trip)")}
        if not columns:
            return
        if "missing_capture_sequence_count" not in columns:
            connection.execute(
                "ALTER TABLE trip ADD COLUMN missing_capture_sequence_count INTEGER NOT NULL DEFAULT 0"
            )
        for name in (
            "gps_fix_count", "gps_poor_quality_count", "speed_disagreement_count",
            "over_target_interval_count",
        ):
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
            if pid in priorities:
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
        if not self._initialized:
            self.initialise()
        files = sorted(self.archive_root.glob("*/????/??/??/*.txt"))
        indexed = 0
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            for archive in files:
                if self._index_file(connection, archive):
                    # Each archive is an independent projection. Commit it
                    # before hashing/scanning the next file so read-only UI
                    # and MCP clients are blocked for the shortest interval.
                    connection.commit()
                    indexed += 1
            indexed += self._index_capture_inbox(connection)
        return indexed

    def _index_capture_inbox(self, connection: sqlite3.Connection) -> int:
        inbox_root = self.archive_root / "capture-inbox"
        if not inbox_root.is_dir():
            return 0
        sessions: dict[tuple[str, str], list[InboxRecord]] = {}
        candidates: dict[tuple[str, str], list[Path]] = {}
        for path in sorted(inbox_root.glob("*/*.fqi")):
            path_match = INBOX_PATH_RE.fullmatch(path.name)
            if (path_match and len(path.parent.name) <= 128
                    and not path.parent.name.startswith(".")
                    and INBOX_DEVICE_RE.fullmatch(path.parent.name)):
                candidates.setdefault((path.parent.name, path_match.group(1).lower()), []).append(path)
            record = parse_inbox_record(path)
            if record is not None:
                sessions.setdefault((record.device_id, record.session_id), []).append(record)

        indexed = 0
        for device_id, session_id in candidates:
            records = sessions.get((device_id, session_id), [])
            by_sequence: dict[int, InboxRecord] = {}
            conflicts: set[int] = set()
            for record in records:
                if record.capture_sequence in conflicts:
                    continue
                incumbent = by_sequence.get(record.capture_sequence)
                if incumbent is None:
                    by_sequence[record.capture_sequence] = record
                elif incumbent.payload_sha256 != record.payload_sha256:
                    # Conflicting bytes for a durable identity invalidate that
                    # identity; neither version is projected.
                    by_sequence.pop(record.capture_sequence)
                    conflicts.add(record.capture_sequence)
            records = order_wrapped_sequences(list(by_sequence.values()))
            digest = hashlib.sha256("\n".join(
                f"{record.capture_sequence}:{record.payload_sha256}:{record.collector_received_ms}"
                for record in records
            ).encode()).hexdigest()
            marker = f"capture-inbox://{device_id}/{session_id}"
            previous = connection.execute(
                "SELECT content_sha256 FROM ingest_file WHERE archive_path=?", (marker,)
            ).fetchone()
            if previous and previous[0] == digest:
                continue
            self._project_inbox_session(connection, device_id, session_id, marker, records)
            total_bytes = sum(path.stat().st_size for path in candidates[(device_id, session_id)])
            connection.execute(
                """INSERT INTO ingest_file(archive_path,content_sha256,byte_size,processed_bytes,
                   sealed,mutation_detected,indexed_at_ms) VALUES(?,?,?,?,1,0,?)
                   ON CONFLICT(archive_path) DO UPDATE SET content_sha256=excluded.content_sha256,
                   byte_size=excluded.byte_size,processed_bytes=excluded.processed_bytes,
                   sealed=1,indexed_at_ms=excluded.indexed_at_ms""",
                (marker, digest, total_bytes, total_bytes, self.now_ms()),
            )
            connection.commit()
            indexed += 1
        return indexed

    def _project_inbox_session(
        self, connection: sqlite3.Connection, device_id: str, session_id: str,
        marker: str, records: list[InboxRecord],
    ) -> None:
        trip_id = f"fqi-{session_id}"
        connection.execute(
            "DELETE FROM capture_inbox_record WHERE device_id=? AND session_id=?",
            (device_id, session_id),
        )
        connection.execute("DELETE FROM trip WHERE device_id=? AND trip_id=?", (device_id, trip_id))
        if not records:
            return
        capture_times = [device_clock_capture_ms(record.frame.fields) for record in records]
        known = [value for value in capture_times if value is not None]
        timestamp_quality = "unknown" if not known else "device_clock" if len(known) == len(records) else "partial"
        timeline: list[int] = []
        elapsed = 0
        for index, record in enumerate(records):
            if index:
                elapsed += max(0, monotonic_delta(
                    record.frame.device_monotonic_ms, records[index - 1].frame.device_monotonic_ms
                ))
            timeline.append(elapsed)
        deltas = [
            monotonic_delta(records[index].frame.device_monotonic_ms,
                            records[index - 1].frame.device_monotonic_ms)
            for index in range(1, len(records))
        ]
        frames = [record.frame for record in records]
        trip_mtime = max(record.collector_received_ms for record in records)
        connection.execute(
            """INSERT INTO trip(device_id,trip_id,archive_path,collector_login_ms,
               start_capture_ms,end_capture_ms,timeline_start_ms,timeline_end_ms,time_basis,
               timestamp_quality,sample_count,data_bytes,gap_count,missing_capture_sequence_count,
               over_target_interval_count,gps_fix_count,gps_poor_quality_count,
               speed_disagreement_count,archive_mtime_ms,updated_at_ms)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (device_id, trip_id, marker, 0, known[0] if known else None,
             known[-1] if known else None, timeline[0], timeline[-1],
             "device_clock" if timestamp_quality == "device_clock" else "device_monotonic",
             timestamp_quality, len(records), sum(Path(r.source_path).stat().st_size for r in records),
             sum(delta > GAP_THRESHOLD_MS for delta in deltas), missing_sequence_count(records),
             sum(delta > OVER_TARGET_INTERVAL_THRESHOLD_MS for delta in deltas),
             *tracking_quality(frames), 0, self.now_ms()),
        )
        for ordinal, (record, capture_ms) in enumerate(zip(records, capture_times)):
            frame, fields = record.frame, record.frame.fields
            hdop = gps_value(fields, "12")
            connection.execute(
                """INSERT INTO sample(device_id,trip_id,sequence,device_monotonic_ms,
                   capture_utc_ms,timeline_ms,time_basis,collector_received_ms,archive_mtime_ms,
                   timestamp_quality,latitude,longitude,gps_speed_kph,gps_heading_degrees,
                   gps_hdop,gps_satellites,acceleration_x_g,acceleration_y_g,acceleration_z_g,
                   capture_session_id,capture_sequence)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (device_id, trip_id, ordinal, frame.device_monotonic_ms, capture_ms, timeline[ordinal],
                 "device_clock" if capture_ms is not None else "device_monotonic",
                 record.collector_received_ms, 0,
                 "device_clock" if capture_ms is not None else "unknown", gps_value(fields, "A"),
                 gps_value(fields, "B"), gps_value(fields, "D"), gps_value(fields, "E"),
                 hdop * 0.1 if hdop is not None else None,
                 int(gps_value(fields, "F")) if gps_value(fields, "F") is not None else None,
                 *acceleration_values(fields), session_id, record.capture_sequence),
            )
            for raw_pid, value in fields.items():
                parsed = numeric(value)
                connection.execute(
                    "INSERT INTO sample_metric(device_id,trip_id,sequence,pid,numeric_value,text_value) VALUES(?,?,?,?,?,?)",
                    (device_id, trip_id, ordinal, normalise_pid(raw_pid), parsed, value if parsed is None else None),
                )
            for field_ordinal, (raw_pid, value) in enumerate(frame.ordered_fields):
                parsed = numeric(value)
                connection.execute(
                    "INSERT INTO sample_field(device_id,trip_id,sequence,ordinal,pid,numeric_value,text_value) VALUES(?,?,?,?,?,?,?)",
                    (device_id, trip_id, ordinal, field_ordinal, normalise_pid(raw_pid), parsed,
                     value if parsed is None else None),
                )
            for status, slot, raw_code, code, system in diagnostic_rows(fields):
                connection.execute(
                    "INSERT INTO diagnostic_code(device_id,trip_id,sequence,status,slot,raw_code,code,system) VALUES(?,?,?,?,?,?,?,?)",
                    (device_id, trip_id, ordinal, status, slot, raw_code, code, system),
                )
            connection.execute(
                "INSERT INTO capture_inbox_record(device_id,session_id,capture_sequence,payload_sha256,source_path) VALUES(?,?,?,?,?)",
                (device_id, session_id, record.capture_sequence, record.payload_sha256, record.source_path),
            )

    def _index_file(self, connection: sqlite3.Connection, archive: Path) -> bool:
        trip_id = archive.stem
        device_id = archive.parent.parent.parent.parent.name
        if not TRIP_ID_RE.fullmatch(trip_id) or not device_id:
            return False
        stat = archive.stat()
        if stat.st_size > MAX_ARCHIVE_FILE_SIZE:
            return False
        raw_bytes = archive.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
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
        deltas = (
            monotonic_delta(frame.device_monotonic_ms, previous_frame.device_monotonic_ms)
            for previous_frame, frame in zip(frames, frames[1:])
        )
        interval_deltas = list(deltas)
        gap_count = sum(delta > GAP_THRESHOLD_MS for delta in interval_deltas)
        over_target_interval_count = sum(
            delta > OVER_TARGET_INTERVAL_THRESHOLD_MS for delta in interval_deltas
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
                sample_count, data_bytes, gap_count, over_target_interval_count, gps_fix_count,
                gps_poor_quality_count, speed_disagreement_count, timeline_start_ms,
                timeline_end_ms, time_basis, archive_mtime_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id, trip_id) DO UPDATE SET
                archive_path=excluded.archive_path,
                collector_login_ms=excluded.collector_login_ms,
                start_capture_ms=excluded.start_capture_ms,
                end_capture_ms=excluded.end_capture_ms,
                timestamp_quality=excluded.timestamp_quality,
                sample_count=excluded.sample_count, data_bytes=excluded.data_bytes,
                gap_count=excluded.gap_count,
                over_target_interval_count=excluded.over_target_interval_count,
                gps_fix_count=excluded.gps_fix_count,
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
                over_target_interval_count,
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
        try:
            print(f"[HISTORY] indexed {indexer.index_once()} archive files", flush=True)
        except sqlite3.OperationalError as error:
            print(f"[HISTORY] SQLite busy; retrying: {error}", flush=True)
            if args.once:
                raise
        if args.once:
            return
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
