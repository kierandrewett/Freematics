"""Bounded, read-only JSON views for the Freematics trip workspace."""

from __future__ import annotations

from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from trip_intelligence import diagnostic_evidence


DEVICE = os.environ.get("FREEMATICS_DEVICE_ID", "ZKUCALJ0")
HISTORY = Path(os.environ.get("FREEMATICS_HISTORY_DB", "/history/history.sqlite"))
REPORTS = Path(os.environ.get("FREEMATICS_INTELLIGENCE_DB", "/state/intelligence.sqlite"))
TRIP_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
PID_RE = re.compile(r"^0x[0-9A-Fa-f]{3,4}$")


def connection(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def bounded_int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw or default)))
    except ValueError:
        return default


def trip_id(raw: str | None) -> str:
    if not raw or not TRIP_RE.fullmatch(raw):
        raise ValueError("Invalid trip ID")
    return raw


def pid(raw: str | None) -> str:
    if not raw or not PID_RE.fullmatch(raw):
        raise ValueError("Invalid metric PID")
    return raw.upper().replace("0X", "0x")


def valid_fix(row: sqlite3.Row) -> bool:
    lat, lon, hdop = row["latitude"], row["longitude"], row["gps_hdop"]
    return (lat is not None and lon is not None and -90 <= lat <= 90 and
            -180 <= lon <= 180 and (hdop is None or 0 <= hdop <= 5))


def metres(a: sqlite3.Row, b: sqlite3.Row) -> float:
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(b["longitude"] - a["longitude"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 12742000 * math.asin(min(1.0, math.sqrt(h)))


def route_data(rows: list[sqlite3.Row], point_cap: int) -> tuple[list[list[list[float]]], float]:
    segments: list[list[list[float]]] = []
    current: list[list[float]] = []
    previous: sqlite3.Row | None = None
    distance = 0.0
    for row in rows:
        if not valid_fix(row):
            if len(current) > 1:
                segments.append(current)
            current, previous = [], None
            continue
        if previous is not None:
            delta_ms = row["device_monotonic_ms"] - previous["device_monotonic_ms"]
            leg = metres(previous, row)
            plausible = (0 < delta_ms <= 120000 and leg <= 250 * delta_ms / 3600)
            if not plausible:
                if len(current) > 1:
                    segments.append(current)
                current = []
            else:
                distance += leg
        current.append([round(row["longitude"], 6), round(row["latitude"], 6)])
        previous = row
    if len(current) > 1:
        segments.append(current)
    total_points = sum(len(segment) for segment in segments)
    if total_points > point_cap:
        stride = math.ceil(total_points / point_cap)
        segments = [segment[::stride] + ([segment[-1]] if segment[-1] != segment[::stride][-1] else [])
                    for segment in segments]
        segments = [segment for segment in segments if len(segment) > 1]
    return segments, round(distance / 1000, 2)


VISIBLE_REPORT_REVISION = 4


def overview(before: str | None, limit: int) -> dict:
    if before:
        before = trip_id(before)
    limit = bounded_int(str(limit), 80, 1, 150)
    reviewed: set[str] = set()
    if REPORTS.exists():
        with closing(connection(REPORTS)) as report_db:
            reviewed = {row[0] for row in report_db.execute(
                "SELECT trip_id FROM trip_report WHERE device_id=? AND report_json IS NOT NULL AND analysis_revision>=?",
                (DEVICE, VISIBLE_REPORT_REVISION)
            )}
    with closing(connection(HISTORY)) as db:
        trips = db.execute("""
            SELECT trip_id,start_capture_ms,end_capture_ms,timeline_start_ms,timeline_end_ms,
                   time_basis,timestamp_quality,sample_count,gap_count,archive_mtime_ms
            FROM trip WHERE device_id=? AND (? IS NULL OR trip_id<?)
            ORDER BY trip_id DESC LIMIT ?
        """, (DEVICE, before, before, limit + 1)).fetchall()
        more = len(trips) > limit
        result = []
        features = []
        for trip in trips[:limit]:
            item = dict(trip)
            item["has_report"] = trip["trip_id"] in reviewed
            item["moving_readings"] = db.execute("""
                SELECT COUNT(*) FROM sample_metric
                WHERE device_id=? AND trip_id=? AND pid IN ('0x10D','0x00D')
                  AND numeric_value>=5
            """, (DEVICE, trip["trip_id"])).fetchone()[0]
            points = db.execute("""
                SELECT sequence,device_monotonic_ms,latitude,longitude,gps_hdop
                FROM sample WHERE device_id=? AND trip_id=?
                ORDER BY sequence
            """, (DEVICE, trip["trip_id"])).fetchall()
            segments, distance = route_data(points, 180)
            item["distance_km"] = distance if segments else None
            item["has_route"] = bool(segments)
            result.append(item)
            if segments:
                features.append({"type": "Feature", "properties": {"trip_id": trip["trip_id"]},
                                 "geometry": {"type": "MultiLineString", "coordinates": segments}})
        return {"device_id": DEVICE, "trips": result,
                "routes": {"type": "FeatureCollection", "features": features},
                "next_before": trips[limit - 1]["trip_id"] if more else None}


def detail(raw_trip: str | None, after: int = -1, limit: int = 12000) -> dict:
    selected = trip_id(raw_trip)
    limit = bounded_int(str(limit), 12000, 1, 20000)
    with closing(connection(HISTORY)) as db:
        trip = db.execute("SELECT * FROM trip WHERE device_id=? AND trip_id=?",
                          (DEVICE, selected)).fetchone()
        if trip is None:
            raise LookupError("Trip not found")
        samples = db.execute("""
            SELECT s.sequence,s.device_monotonic_ms,s.timeline_ms,s.capture_utc_ms,
                   s.latitude,s.longitude,s.gps_hdop,s.gps_speed_kph,s.gps_heading_degrees,
                   (SELECT numeric_value FROM sample_metric m WHERE m.device_id=s.device_id
                    AND m.trip_id=s.trip_id AND m.sequence=s.sequence AND m.pid='0x10D') AS obd_speed_kph
            FROM sample s WHERE s.device_id=? AND s.trip_id=? AND s.sequence>?
            ORDER BY s.sequence LIMIT ?
        """, (DEVICE, selected, after, limit + 1)).fetchall()
        more = len(samples) > limit
        samples = samples[:limit]
        metrics = db.execute("""
            SELECT m.pid,COALESCE(c.name,m.pid) AS name,COALESCE(c.unit,'') AS unit,
                   COALESCE(c.category,'raw') AS category,COALESCE(c.priority,3) AS priority,
                   COUNT(*) AS readings,COUNT(m.numeric_value) AS numeric_readings
            FROM sample_metric m LEFT JOIN metric_catalogue c ON c.pid=m.pid
            WHERE m.device_id=? AND m.trip_id=? GROUP BY m.pid ORDER BY priority,name
        """, (DEVICE, selected)).fetchall()
        codes = diagnostic_evidence(db, DEVICE, selected)["confirmed_codes"]
        fix_count = db.execute("""
            SELECT COUNT(*) FROM sample WHERE device_id=? AND trip_id=?
              AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180
              AND (gps_hdop IS NULL OR gps_hdop BETWEEN 0 AND 5)
        """, (DEVICE, selected)).fetchone()[0]
        trip_data = dict(trip)
        trip_data["gps_fix_count"] = fix_count
    return {"trip": trip_data, "samples": [dict(row) for row in samples],
            "metrics": [dict(row) for row in metrics], "codes": codes,
            "report": report(selected), "next_after": samples[-1]["sequence"] if more else None}


def report(raw_trip: str | None) -> dict | None:
    selected = trip_id(raw_trip)
    result = None
    if REPORTS.exists():
        with closing(connection(REPORTS)) as db:
            saved = db.execute("""
                SELECT report_json,requested_model,returned_model,analyzed_at_ms,attempts,last_error,
                       analysis_revision,screen_json
                FROM trip_report WHERE device_id=? AND trip_id=?
            """, (DEVICE, selected)).fetchone()
            if saved and saved["analysis_revision"] >= VISIBLE_REPORT_REVISION:
                result = {key: saved[key] for key in saved.keys()
                          if key not in ("report_json", "screen_json")}
                if saved["report_json"]:
                    result["report"] = json.loads(saved["report_json"]).get("report")
                elif saved["screen_json"]:
                    result["screen"] = json.loads(saved["screen_json"])
    return result


def series(raw_trip: str | None, raw_pid: str | None, after: int = -1) -> dict:
    selected, metric = trip_id(raw_trip), pid(raw_pid)
    with closing(connection(HISTORY)) as db:
        rows = db.execute("""
            SELECT m.sequence,m.numeric_value,s.timeline_ms,s.device_monotonic_ms
            FROM sample_metric m JOIN sample s ON s.device_id=m.device_id
              AND s.trip_id=m.trip_id AND s.sequence=m.sequence
            WHERE m.device_id=? AND m.trip_id=? AND m.pid=? AND m.numeric_value IS NOT NULL
              AND m.sequence>? ORDER BY m.sequence LIMIT 10001
        """, (DEVICE, selected, metric, after)).fetchall()
    more = len(rows) > 10000
    rows = rows[:10000]
    return {"pid": metric, "values": [dict(row) for row in rows],
            "next_after": rows[-1]["sequence"] if more else None}


def sample(raw_trip: str | None, sequence: int) -> dict:
    selected = trip_id(raw_trip)
    with closing(connection(HISTORY)) as db:
        row = db.execute("SELECT * FROM sample WHERE device_id=? AND trip_id=? AND sequence=?",
                         (DEVICE, selected, sequence)).fetchone()
        if row is None:
            raise LookupError("Sample not found")
        values = db.execute("""
            SELECT m.pid,COALESCE(c.name,m.pid) AS name,COALESCE(c.unit,'') AS unit,
                   m.numeric_value,m.text_value
            FROM sample_metric m LEFT JOIN metric_catalogue c ON c.pid=m.pid
            WHERE m.device_id=? AND m.trip_id=? AND m.sequence=?
            ORDER BY COALESCE(c.priority,3),COALESCE(c.name,m.pid)
        """, (DEVICE, selected, sequence)).fetchall()
    return {"sample": dict(row), "values": [dict(value) for value in values]}


def status() -> dict:
    with closing(connection(HISTORY)) as db:
        latest = db.execute("""
            SELECT trip_id,sequence,archive_mtime_ms,capture_utc_ms,latitude,longitude
            FROM sample WHERE device_id=? ORDER BY archive_mtime_ms DESC,sequence DESC LIMIT 1
        """, (DEVICE,)).fetchone()
        trip_count = db.execute("SELECT COUNT(*) FROM trip WHERE device_id=?", (DEVICE,)).fetchone()[0]
    pending_reports = completed_reports = report_version = 0
    if REPORTS.exists():
        with closing(connection(REPORTS)) as db:
            pending_reports, completed_reports, report_version = db.execute("""
                SELECT SUM(CASE WHEN report_json IS NULL AND notified=0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN report_json IS NOT NULL AND analysis_revision>=? THEN 1 ELSE 0 END),
                       COALESCE(MAX(CASE WHEN analysis_revision>=? THEN analyzed_at_ms END),0)
                FROM trip_report WHERE device_id=?
            """, (VISIBLE_REPORT_REVISION, VISIBLE_REPORT_REVISION, DEVICE)).fetchone()
    return {"device_id": DEVICE, "latest": dict(latest) if latest else None,
            "trip_count": trip_count, "pending_reports": pending_reports,
            "completed_reports": completed_reports, "report_version": report_version}
