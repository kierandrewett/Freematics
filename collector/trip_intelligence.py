#!/usr/bin/env python3
"""Evidence-led Freematics events and sealed-trip analysis.

The history indexer owns the source data. This worker keeps only delivery
cursors and reports in a separate SQLite database. It never edits telemetry.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MOVING_KPH = 5.0
STOPPED_KPH = 1.0
STOP_SECONDS = 45
LOCATION_SECONDS = 15 * 60
FRESH_MS = 5 * 60 * 1000
ANALYSIS_REVISION = 4
COLLECTION_LIMIT_TERMS = (
    "gps fix", "gps drop", "missing gps", "no gps", "data gap", "gap count",
    "timestamp", "missing obd", "obd absence", "incomplete logging", "sensor communication failure",
)
UNSUPPORTED_PATTERN_TERMS = (
    "electrical system", "route pattern",
)


def utc(ms: int | None) -> str:
    return datetime.fromtimestamp((ms or 0) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def open_history(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def open_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS device_cursor (
            device_id TEXT PRIMARY KEY, trip_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            moving INTEGER NOT NULL DEFAULT 0, moving_hits INTEGER NOT NULL DEFAULT 0,
            stopped_since_ms INTEGER, last_location_ms INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS trip_report (
            device_id TEXT NOT NULL, trip_id TEXT NOT NULL,
            signature TEXT NOT NULL, report_json TEXT,
            notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (device_id, trip_id)
        );
        CREATE TABLE IF NOT EXISTS worker_meta (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS health_cursor (
            device_id TEXT PRIMARY KEY, missed_readings INTEGER NOT NULL,
            queue_healthy INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS diagnostic_alert (
            device_id TEXT NOT NULL, trip_id TEXT NOT NULL, status TEXT NOT NULL,
            code TEXT NOT NULL, PRIMARY KEY(device_id,trip_id,status,code)
        );
        CREATE TABLE IF NOT EXISTS diagnostic_incident (
            device_id TEXT NOT NULL, trip_id TEXT NOT NULL, status TEXT NOT NULL,
            code TEXT NOT NULL, alert_notified INTEGER NOT NULL DEFAULT 0,
            analysis_report_json TEXT, analysis_notified INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_ms INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, requested_model TEXT, returned_model TEXT,
            PRIMARY KEY(device_id,trip_id,status,code)
        );
    """)
    existing = {row[1] for row in db.execute("PRAGMA table_info(trip_report)")}
    additions = {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_ms": "INTEGER NOT NULL DEFAULT 0",
        "last_error": "TEXT",
        "requested_model": "TEXT",
        "returned_model": "TEXT",
        "analyzed_at_ms": "INTEGER",
        "notified_at_ms": "INTEGER",
        "analysis_revision": "INTEGER NOT NULL DEFAULT 0",
        "screen_json": "TEXT",
    }
    for name, definition in additions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE trip_report ADD COLUMN {name} {definition}")
    db.commit()
    return db


def publish_ntfy(topic: str, token: str, title: str, message: str, priority: int = 3) -> None:
    body = json.dumps({
        "topic": topic, "title": title, "message": message,
        "priority": priority, "tags": ["car"],
    }).encode()
    request = urllib.request.Request(
        "https://ntfy.drewett.dev", body,
        {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f"ntfy returned HTTP {response.status}")


def location_link(row: sqlite3.Row) -> str:
    lat, lon, hdop = row["latitude"], row["longitude"], row["gps_hdop"]
    capture = row["capture_utc_ms"]
    now = int(time.time() * 1000)
    if lat is None or lon is None or capture is None or abs(now - capture) > FRESH_MS:
        return "Location unavailable (no fresh dated GPS fix)."
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (hdop is not None and hdop > 5):
        return "Location unavailable (GPS quality insufficient)."
    return f"Location: https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=16/{lat:.6f}/{lon:.6f}"


def latest_samples(history: sqlite3.Connection, device: str, trip: str, sequence: int):
    return history.execute("""
        SELECT s.trip_id, s.sequence, s.device_monotonic_ms, s.capture_utc_ms,
               s.archive_mtime_ms, s.latitude, s.longitude, s.gps_hdop,
               s.gps_speed_kph,
               (SELECT m.numeric_value FROM sample_metric AS m
                WHERE m.device_id=s.device_id AND m.trip_id=s.trip_id
                  AND m.sequence=s.sequence AND m.pid='0x08C') AS queue_bytes,
               (SELECT m.numeric_value FROM sample_metric AS m
                WHERE m.device_id=s.device_id AND m.trip_id=s.trip_id
                  AND m.sequence=s.sequence AND m.pid='0x08D') AS sd_queue_bytes,
               (SELECT m.numeric_value FROM sample_metric AS m
                WHERE m.device_id=s.device_id AND m.trip_id=s.trip_id
                  AND m.sequence=s.sequence AND m.pid='0x10D') AS obd_speed_kph
        FROM sample AS s
        WHERE s.device_id=? AND (s.trip_id>? OR (s.trip_id=? AND s.sequence>?))
        ORDER BY s.trip_id, s.sequence LIMIT 5000
    """, (device, trip, trip, sequence)).fetchall()


def process_motion(history: sqlite3.Connection, state: sqlite3.Connection,
                   device: str, topic: str, token: str) -> None:
    cursor = state.execute("SELECT * FROM device_cursor WHERE device_id=?", (device,)).fetchone()
    if cursor is None:
        # Starting the worker must not announce a historical journey.
        newest = history.execute("""
            SELECT trip_id, sequence FROM sample WHERE device_id=?
            ORDER BY trip_id DESC, sequence DESC LIMIT 1
        """, (device,)).fetchone()
        if newest:
            state.execute("INSERT INTO device_cursor(device_id,trip_id,sequence) VALUES(?,?,?)",
                          (device, newest["trip_id"], newest["sequence"]))
            state.commit()
        return

    moving = bool(cursor["moving"])
    hits = cursor["moving_hits"]
    stopped_since = cursor["stopped_since_ms"]
    last_location = cursor["last_location_ms"]
    for row in latest_samples(history, device, cursor["trip_id"], cursor["sequence"]):
        capture = row["capture_utc_ms"]
        now = int(time.time() * 1000)
        dated = capture is not None and abs(now - capture) <= FRESH_MS
        live_upload = (abs(now - row["archive_mtime_ms"]) <= FRESH_MS
                       and (row["queue_bytes"] or 0) < 5000
                       and (row["sd_queue_bytes"] or 0) < 5000)
        fresh = dated or live_upload
        observed_at = capture if dated else row["archive_mtime_ms"]
        speed = row["obd_speed_kph"]
        if speed is None:
            speed = row["gps_speed_kph"]
        if fresh and speed is not None:
            if speed >= MOVING_KPH:
                hits += 1
                stopped_since = None
                if not moving and hits >= 3:
                    publish_ntfy(topic, token, "Vehicle started moving",
                                 f"{utc(observed_at)} · {speed:.0f} km/h\n{location_link(row)}")
                    moving = True
                    last_location = observed_at
            elif speed <= STOPPED_KPH:
                hits = 0
                if moving:
                    stopped_since = stopped_since or observed_at
                    if observed_at - stopped_since >= STOP_SECONDS * 1000:
                        publish_ntfy(topic, token, "Vehicle stopped",
                                     f"{utc(observed_at)}\n{location_link(row)}")
                        moving = False
                        stopped_since = None
            else:
                hits = 0
                stopped_since = None
            if moving and observed_at - last_location >= LOCATION_SECONDS * 1000:
                link = location_link(row)
                if link.startswith("Location: "):
                    publish_ntfy(topic, token, "Vehicle location",
                                 f"{utc(observed_at)} · {speed:.0f} km/h\n{link}")
                    last_location = observed_at
        state.execute("""
            UPDATE device_cursor SET trip_id=?, sequence=?, moving=?, moving_hits=?,
                stopped_since_ms=?, last_location_ms=? WHERE device_id=?
        """, (row["trip_id"], row["sequence"], int(moving), hits,
              stopped_since, last_location, device))
        state.commit()


def metric_extreme(history: sqlite3.Connection, device: str, trip: str,
                   pid: str, mode: str = "MAX") -> float | None:
    if mode not in ("MIN", "MAX", "AVG"):
        raise ValueError(mode)
    value = history.execute(f"""
        SELECT {mode}(numeric_value) FROM sample_metric
        WHERE device_id=? AND trip_id=? AND pid=?
    """, (device, trip, pid)).fetchone()[0]
    return round(float(value), 2) if value is not None else None


def metric_summary(history: sqlite3.Connection, device: str, trip: str,
                   pid: str, scale: float = 1.0) -> dict | None:
    values = [float(row[0]) * scale for row in history.execute("""
        SELECT numeric_value FROM sample_metric
        WHERE device_id=? AND trip_id=? AND pid=? AND numeric_value IS NOT NULL
        ORDER BY numeric_value
    """, (device, trip, pid))]
    if not values:
        return None
    return {"readings": len(values), "min": round(values[0], 2),
            "mean": round(statistics.fmean(values), 2),
            "p95": round(values[int((len(values) - 1) * 0.95)], 2),
            "max": round(values[-1], 2)}


def motion_magnitude_summary(history: sqlite3.Connection, device: str, trip: str) -> dict | None:
    values = []
    for row in history.execute("""
        SELECT text_value FROM sample_metric
        WHERE device_id=? AND trip_id=? AND pid='0x020' AND text_value IS NOT NULL
    """, (device, trip)):
        try:
            components = [float(part) for part in row[0].split(";")]
        except ValueError:
            continue
        if len(components) == 3 and all(math.isfinite(part) for part in components):
            values.append(math.sqrt(sum(part * part for part in components)))
    if not values:
        return None
    values.sort()
    return {"readings": len(values), "p95": round(values[int((len(values) - 1) * 0.95)], 3),
            "max": round(values[-1], 3)}


def driving_dynamics(history: sqlite3.Connection, device: str, trip: str) -> dict | None:
    """Measure speed changes from consecutive OBD speed readings, without filling gaps."""
    rows = history.execute("""
        SELECT s.device_monotonic_ms, m.numeric_value
        FROM sample_metric AS m JOIN sample AS s
          ON s.device_id=m.device_id AND s.trip_id=m.trip_id AND s.sequence=m.sequence
        WHERE m.device_id=? AND m.trip_id=? AND m.pid='0x10D'
          AND m.numeric_value IS NOT NULL
        ORDER BY s.device_monotonic_ms, s.sequence
    """, (device, trip))
    previous = None
    speeds = []
    changes = []
    seconds_observed = 0.0
    moving_seconds = 0.0
    speed_rises = speed_falls = 0
    last_accel = last_decel = -100000
    for tick, raw_speed in rows:
        speed = float(raw_speed)
        if not math.isfinite(speed) or not 0 <= speed <= 250:
            previous = None
            continue
        speeds.append(speed)
        if previous is not None:
            dt = (tick - previous[0]) / 1000
            if 0.5 <= dt <= 5:
                seconds_observed += dt
                if (speed + previous[1]) / 2 >= MOVING_KPH:
                    moving_seconds += dt
                    acceleration = (speed - previous[1]) / 3.6 / dt
                    changes.append(acceleration)
                    # Count episodes, not each sample in a sustained manoeuvre.
                    if acceleration >= 1.5 and tick - last_accel > 5000:
                        speed_rises += 1
                        last_accel = tick
                    if acceleration <= -1.5 and tick - last_decel > 5000:
                        speed_falls += 1
                        last_decel = tick
        previous = (tick, speed)
    if len(speeds) < 20 or moving_seconds < 30:
        return None
    ordered = sorted(speeds)
    result = {
        "source": "consecutive OBD speed samples; no GPS or IMU inference",
        "speed_readings": len(speeds),
        "covered_minutes": round(seconds_observed / 60, 1),
        "moving_minutes": round(moving_seconds / 60, 1),
        "speed_kph_p10": round(ordered[int((len(ordered) - 1) * .10)], 1),
        "speed_kph_median": round(statistics.median(ordered), 1),
        "speed_kph_p90": round(ordered[int((len(ordered) - 1) * .90)], 1),
        "speed_rise_episodes": speed_rises,
        "speed_fall_episodes": speed_falls,
        "episode_threshold_mps2": 1.5,
    }
    if changes:
        ordered_changes = sorted(changes)
        result["speed_change_mps2_p05"] = round(ordered_changes[int((len(changes) - 1) * .05)], 2)
        result["speed_change_mps2_p95"] = round(ordered_changes[int((len(changes) - 1) * .95)], 2)
    return result


def moving_readings(history: sqlite3.Connection, device: str, trip: str) -> int:
    return history.execute("""
        SELECT COUNT(*) FROM sample_metric WHERE device_id=? AND trip_id=?
          AND pid IN ('0x10D','0x00D') AND numeric_value>=5
    """, (device, trip)).fetchone()[0]


def obd_metric_inventory(history: sqlite3.Connection, device: str, trip: str) -> list[dict]:
    """All observed standard OBD PIDs, labelled with the indexed catalogue."""
    result = []
    for row in history.execute("""
        SELECT m.pid, c.name, c.unit, COUNT(*) AS readings,
               MIN(m.numeric_value) AS minimum, AVG(m.numeric_value) AS mean,
               MAX(m.numeric_value) AS maximum
        FROM sample_metric AS m LEFT JOIN metric_catalogue AS c ON c.pid=m.pid
        WHERE m.device_id=? AND m.trip_id=? AND m.pid>='0x100' AND m.pid<'0x200'
          AND m.numeric_value IS NOT NULL
        GROUP BY m.pid ORDER BY m.pid
    """, (device, trip)):
        item = {"pid": row["pid"], "name": row["name"] or row["pid"],
                "unit": row["unit"] or "raw", "readings": row["readings"],
                "min": round(row["minimum"], 2), "max": round(row["maximum"], 2)}
        if item["unit"] not in ("code", "boolean", "enum"):
            item["mean"] = round(row["mean"], 2)
        result.append(item)
    return result


TREND_PIDS = {
    "0x10D": "speed_kph", "0x10C": "rpm", "0x104": "engine_load_percent",
    "0x110": "maf_gps", "0x111": "throttle_percent", "0x149": "pedal_percent",
    "0x10B": "map_kpa", "0x10F": "intake_celsius", "0x105": "coolant_celsius",
    "0x106": "short_fuel_trim_percent", "0x107": "long_fuel_trim_percent",
    "0x024": "supply_volts",
}


def obd_trend_windows(history: sqlite3.Connection, device: str, trip: str,
                      first_ms: int, last_ms: int) -> dict:
    """Bounded chronology retains co-movement that whole-trip averages erase."""
    width_ms = max(30000, math.ceil(max(1, last_ms-first_ms) / 60 / 1000) * 1000)
    values: dict[tuple[int, str], list[float]] = {}
    for row in history.execute("""
        SELECT s.device_monotonic_ms, m.pid, m.numeric_value
        FROM sample_metric AS m JOIN sample AS s
          ON s.device_id=m.device_id AND s.trip_id=m.trip_id AND s.sequence=m.sequence
        WHERE m.device_id=? AND m.trip_id=? AND m.pid IN
          ('0x10D','0x10C','0x104','0x110','0x111','0x149','0x10B','0x10F',
           '0x105','0x106','0x107','0x024') AND m.numeric_value IS NOT NULL
        ORDER BY s.device_monotonic_ms
    """, (device, trip)):
        bucket = max(0, (row[0] - first_ms) // width_ms)
        if bucket >= 60:
            continue
        scale = 0.01 if row[1] == "0x024" else 1.0
        values.setdefault((bucket, TREND_PIDS[row[1]]), []).append(row[2] * scale)
    windows: dict[int, dict] = {}
    for (bucket, name), series in values.items():
        windows.setdefault(bucket, {"from_minute": round(bucket * width_ms / 60000, 1)})[name] = {
            "n": len(series), "mean": round(statistics.fmean(series), 2),
            "min": round(min(series), 2), "max": round(max(series), 2),
        }
    return {"window_seconds": width_ms // 1000,
            "windows": [windows[index] for index in sorted(windows)]}


def obd_pid_timeline(history: sqlite3.Connection, device: str, trip: str,
                     first_ms: int, last_ms: int) -> dict:
    """Summarise every observed continuous OBD PID over equal time windows."""
    catalogue = history.execute("""
        SELECT m.pid, COALESCE(c.name,m.pid) AS name, COALESCE(c.unit,'raw') AS unit,
               COUNT(m.numeric_value) AS readings
        FROM sample_metric m LEFT JOIN metric_catalogue c ON c.pid=m.pid
        WHERE m.device_id=? AND m.trip_id=? AND m.pid>='0x100' AND m.pid<'0x200'
          AND m.numeric_value IS NOT NULL
        GROUP BY m.pid
        ORDER BY COALESCE(c.priority,3), m.pid
    """, (device, trip)).fetchall()
    pids = [row for row in catalogue if row["unit"] not in ("code", "boolean", "enum")]
    if not pids or first_ms is None or last_ms is None or last_ms < first_ms:
        return {"window_seconds": None, "windows": [], "pids": []}
    pid_keys = {row["pid"] for row in pids}
    duration_ms = max(1, last_ms - first_ms)
    width_ms = max(30000, math.ceil(duration_ms / 8 / 30000) * 30000)
    buckets: dict[tuple[int, str], list[float]] = {}
    query_rows = history.execute("""
        SELECT s.device_monotonic_ms,m.pid,m.numeric_value
        FROM sample_metric m JOIN sample s ON s.device_id=m.device_id
          AND s.trip_id=m.trip_id AND s.sequence=m.sequence
        WHERE m.device_id=? AND m.trip_id=? AND m.pid>='0x100' AND m.pid<'0x200'
          AND m.numeric_value IS NOT NULL
        ORDER BY s.device_monotonic_ms,s.sequence
    """, (device, trip))
    for row in query_rows:
        if row["pid"] not in pid_keys:
            continue
        value = float(row["numeric_value"])
        if not math.isfinite(value):
            continue
        bucket = min(7, max(0, (row["device_monotonic_ms"] - first_ms) // width_ms))
        buckets.setdefault((int(bucket), row["pid"]), []).append(value)
    windows: dict[int, dict] = {}
    for (bucket, pid), values in buckets.items():
        values.sort()
        windows.setdefault(bucket, {"minute": round(bucket * width_ms / 60000, 1), "values": {}})
        windows[bucket]["values"][pid] = [
            len(values), round(values[0], 2), round(statistics.median(values), 2), round(values[-1], 2)
        ]
    return {"window_seconds": width_ms // 1000,
            "windows": [windows[index] for index in sorted(windows)],
            "value_order": ["n", "min", "median", "max"],
            "pids": [[row["pid"], row["name"], row["unit"], row["readings"]]
                     for row in pids]}


def maf_operating_bins(history: sqlite3.Connection, device: str, trip: str) -> dict[tuple[int, int], list[float]]:
    """Compare MAF only at roughly matched RPM/load and warm operation."""
    last: dict[str, tuple[int, float]] = {}
    bins: dict[tuple[int, int], list[float]] = {}
    for row in history.execute("""
        SELECT s.device_monotonic_ms, m.pid, m.numeric_value
        FROM sample_metric AS m JOIN sample AS s
          ON s.device_id=m.device_id AND s.trip_id=m.trip_id AND s.sequence=m.sequence
        WHERE m.device_id=? AND m.trip_id=? AND m.pid IN
          ('0x10C','0x104','0x10D','0x110','0x105') AND m.numeric_value IS NOT NULL
        ORDER BY s.device_monotonic_ms, m.pid
    """, (device, trip)):
        tick, pid, value = int(row[0]), row[1], float(row[2])
        last[pid] = (tick, value)
        if pid != "0x110" or not all(key in last for key in ("0x10C", "0x104", "0x10D", "0x105")):
            continue
        if any(tick-last[key][0] > (30000 if key == "0x105" else 2000)
               for key in ("0x10C", "0x104", "0x10D", "0x105")):
            continue
        rpm, load, speed, coolant = (last[key][1] for key in ("0x10C", "0x104", "0x10D", "0x105"))
        if coolant < 70 or speed < 10 or not (750 <= rpm <= 4500 and 20 <= load <= 90):
            continue
        bins.setdefault((int(rpm // 250), int(load // 10)), []).append(value)
    return bins


def matched_maf_baseline(history: sqlite3.Connection, device: str, trip: str) -> list[dict]:
    current = maf_operating_bins(history, device, trip)
    if not current:
        return []
    prior_ids = [row[0] for row in history.execute("""
        SELECT t.trip_id FROM trip AS t JOIN ingest_file AS i ON i.archive_path=t.archive_path
        WHERE t.device_id=? AND t.trip_id<? AND i.sealed=1 AND t.sample_count>=60
        ORDER BY t.trip_id DESC LIMIT 20
    """, (device, trip))]
    baseline: dict[tuple[int, int], list[float]] = {}
    trip_counts: dict[tuple[int, int], int] = {}
    for prior in prior_ids:
        if moving_readings(history, device, prior) < 20:
            continue
        for key, values in maf_operating_bins(history, device, prior).items():
            if len(values) >= 3:
                baseline.setdefault(key, []).extend(values)
                trip_counts[key] = trip_counts.get(key, 0) + 1
    comparisons = []
    for key, values in current.items():
        reference = baseline.get(key, [])
        if len(values) < 8 or len(reference) < 30 or trip_counts.get(key, 0) < 5:
            continue
        recent, typical = statistics.median(values), statistics.median(reference)
        if typical <= 0:
            continue
        comparisons.append({"rpm_band": [key[0]*250, (key[0]+1)*250],
                            "load_band_percent": [key[1]*10, (key[1]+1)*10],
                            "current_maf_median_gps": round(recent, 2),
                            "prior_maf_median_gps": round(typical, 2),
                            "relative_change_percent": round((recent/typical-1)*100, 1),
                            "current_readings": len(values),
                            "prior_readings": len(reference),
                            "prior_trips": trip_counts[key]})
    return sorted(comparisons, key=lambda row: abs(row["relative_change_percent"]), reverse=True)[:20]


def jev_interest_screen(evidence: dict, key: str) -> dict:
    """Ask Jev whether this trip warrants a full mechanic review."""
    if not key:
        raise RuntimeError("Jev trip screening needs OPENROUTER_API_KEY")
    threshold = float(os.environ.get("FREEMATICS_JEV_REVIEW_THRESHOLD", "0.5"))
    if not 0.0 < threshold < 1.0:
        raise ValueError("FREEMATICS_JEV_REVIEW_THRESHOLD must be between 0 and 1")
    state = {name: evidence.get(name) for name in (
        "samples", "duration_minutes", "distance_km", "recent_trip_baseline",
        "driving_dynamics", "all_observed_obd_pids", "obd_pid_timeline",
        "diagnostics", "diagnostic_capabilities",
    )}
    request_body = {
        "model": "typesafe/jev-1.13",
        "state": {"trip_evidence": state},
        "questions": {
            "mechanical_concern": {
                "type": "noul",
                "instructions": (
                    "Does `trip_evidence` contain a plausible mechanical concern or signal "
                    "relationship that merits a mechanic-grade review? Consider every observed "
                    "signal and its time windows, not just named examples."
                ),
                "criteria": {
                    "true": "Measured behaviour materially departs from normal context, is persistent or recurring, or multiple signals support a concern worth checking.",
                    "false": "Signals fit ordinary operation, variation is brief and unsupported, or the evidence is too weak to justify a mechanic review.",
                },
            },
            "driving_pattern": {
                "type": "noul",
                "instructions": (
                    "Does this trip show a distinct, useful driving behaviour worth telling the owner "
                    "about, using measured speed changes and the owner's prior-trip baseline?"
                ),
                "criteria": {
                    "true": "A measured pattern is notably different or recurrent and useful to the owner; avoid moral or safety judgments unsupported by the data.",
                    "false": "Speed changes are ordinary for the available evidence, explained by route or traffic, or not meaningfully different from prior trips.",
                },
            },
            "cross_trip_pattern": {
                "type": "noul",
                "instructions": (
                    "Do these measurements and prior-trip comparisons reveal a meaningful repeated "
                    "or changing pattern across vehicle signals that merits owner attention?"
                ),
                "criteria": {
                    "true": "A signal or relationship changes materially or repeats across trips in a way that deserves investigation or a concise owner observation.",
                    "false": "No meaningful cross-trip change is supported, or differences are adequately explained by operating conditions and limited data.",
                },
            },
        },
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/alpha/decisions", json.dumps(request_body).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
         "HTTP-Referer": "https://freematics.drewett.dev", "X-OpenRouter-Title": "Freematics Trip Screen"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", errors="replace")
        raise RuntimeError(f"Jev HTTP {exc.code}: {detail}") from exc
    model = result.get("model", "")
    if not model.startswith("typesafe/jev-1.13"):
        raise ValueError(f"Jev returned unexpected model {model!r}")
    answers = result.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Jev response did not include typed answers")
    probabilities = {}
    for question in request_body["questions"]:
        answer = answers.get(question)
        if not isinstance(answer, dict) or answer.get("type") != "noul":
            raise ValueError(f"Jev omitted the typed {question} answer")
        probability = answer.get("noul")
        if not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError(f"Jev returned an invalid {question} probability")
        probabilities[question] = round(float(probability), 4)
    reasons = [name for name, probability in probabilities.items() if probability >= threshold]
    usage = result.get("usage") or {}
    cost = usage.get("cost")
    if cost is not None and (not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0):
        raise ValueError("Jev returned invalid usage accounting")
    return {
        "decision": "review" if reasons else "no_report",
        "screen_revision": 2, "screen_model": model,
        "threshold": threshold, "answers": probabilities,
        "reasons": reasons, "request_id": result.get("id"),
        "usage": {"input_tokens": usage.get("input_tokens"),
                  "output_tokens": usage.get("output_tokens"), "cost": cost},
    }


def screen_trip_interest(evidence: dict, key: str) -> dict:
    """Route confirmed codes directly; let Jev assess every other trip broadly."""
    codes = evidence.get("diagnostics", {}).get("confirmed_codes", [])
    if codes:
        return {"decision": "review", "screen_revision": 2,
                "screen_model": "deterministic-dtc", "threshold": 1.0,
                "answers": {}, "reasons": ["confirmed_diagnostic_code"],
                "codes": sorted({item["code"] for item in codes})}
    return jev_interest_screen(evidence, key)


def save_screened_trip(state: sqlite3.Connection, device: str, trip_id: str,
                       signature: str, screen: dict, now: int | None = None) -> None:
    """Persist the deterministic screen so quiet trips never get reprocessed."""
    analyzed_at = now or int(time.time() * 1000)
    state.execute("""
        INSERT INTO trip_report(device_id,trip_id,signature,report_json,notified,
                                analyzed_at_ms,analysis_revision,screen_json)
        VALUES(?,?,?,NULL,1,?,?,?) ON CONFLICT(device_id,trip_id) DO UPDATE SET
            signature=excluded.signature, report_json=NULL, notified=1,
            requested_model=NULL, returned_model=NULL, analyzed_at_ms=excluded.analyzed_at_ms,
            notified_at_ms=NULL, analysis_revision=excluded.analysis_revision,
            screen_json=excluded.screen_json, attempts=0, next_attempt_ms=0, last_error=NULL
    """, (device, trip_id, signature, analyzed_at, ANALYSIS_REVISION,
          json.dumps(screen, separators=(",", ":"))))
    state.commit()


def save_screen_verdict(state: sqlite3.Connection, device: str, trip_id: str,
                        signature: str, screen: dict) -> None:
    """Keep a positive Jev result across full-review retries."""
    saved = state.execute("SELECT 1 FROM trip_report WHERE device_id=? AND trip_id=?",
                          (device, trip_id)).fetchone()
    if saved:
        state.execute("""
            UPDATE trip_report SET signature=?,screen_json=?
            WHERE device_id=? AND trip_id=?
        """, (signature, json.dumps(screen, separators=(",", ":")), device, trip_id))
    else:
        state.execute("""
            INSERT INTO trip_report(device_id,trip_id,signature,notified,screen_json)
            VALUES(?,?,?,0,?)
        """, (device, trip_id, signature, json.dumps(screen, separators=(",", ":"))))
    state.commit()


def diagnostic_evidence(history: sqlite3.Connection, device: str, trip: str) -> dict:
    """Decode standard DTC slots directly, including on the older live schema."""
    groups = (("stored", 0x300, 0x301, 0x310),
              ("pending", 0x320, 0x321, 0x330),
              ("permanent", 0x340, 0x341, 0x350))
    output = {"scan_status": {}, "confirmed_codes": []}
    for name, _count_pid, first_pid, status_pid in groups:
        latest = history.execute("""
            SELECT sequence, numeric_value FROM sample_metric
            WHERE device_id=? AND trip_id=? AND pid=?
            ORDER BY sequence DESC LIMIT 1
        """, (device, trip, f"0x{status_pid:03X}")).fetchone()
        if latest and latest["numeric_value"] is not None:
            output["scan_status"][name] = {0: "no_response", 1: "response_no_codes",
                                            2: "codes_reported"}.get(int(latest["numeric_value"]), "unknown")
        rows = history.execute("""
            SELECT m.sequence, m.pid, m.numeric_value, s.device_monotonic_ms
            FROM sample_metric AS m JOIN sample AS s
              ON s.device_id=m.device_id AND s.trip_id=m.trip_id AND s.sequence=m.sequence
            WHERE m.device_id=? AND m.trip_id=? AND m.pid>=? AND m.pid<=?
              AND m.numeric_value>0 ORDER BY m.sequence
        """, (device, trip, f"0x{first_pid:03X}", f"0x{first_pid+14:03X}"))
        found = {}
        for row in rows:
            raw = int(row["numeric_value"])
            if raw < 1 or raw > 0xFFFF:
                continue
            prefix = "PCBU"[raw >> 14]
            code = f"{prefix}{(raw >> 12) & 3:X}{raw & 0xFFF:03X}"
            key = (name, code)
            if key not in found:
                found[key] = {"status": name, "code": code, "raw": raw,
                              "last_sequence": row["sequence"],
                              "first_monotonic_ms": row["device_monotonic_ms"],
                              "last_monotonic_ms": row["device_monotonic_ms"],
                              "observations": 0}
            found[key]["last_monotonic_ms"] = row["device_monotonic_ms"]
            found[key]["last_sequence"] = row["sequence"]
            found[key]["observations"] += 1
        for code in found.values():
            code["active_at_last_scan"] = bool(latest and code["last_sequence"] == latest["sequence"])
            code["prior_trip_sightings"] = history.execute("""
                SELECT COUNT(DISTINCT trip_id) FROM sample_metric
                WHERE device_id=? AND trip_id<? AND pid>=? AND pid<=? AND numeric_value=?
            """, (device, trip, f"0x{first_pid:03X}", f"0x{first_pid+14:03X}",
                  code["raw"])).fetchone()[0]
            code["near_first_scan_observation"] = [dict(row) for row in history.execute("""
                SELECT m.pid, c.name, c.unit, COUNT(*) AS readings,
                       ROUND(MIN(m.numeric_value),2) AS min,
                       ROUND(AVG(m.numeric_value),2) AS mean,
                       ROUND(MAX(m.numeric_value),2) AS max
                FROM sample_metric AS m JOIN sample AS s
                  ON s.device_id=m.device_id AND s.trip_id=m.trip_id AND s.sequence=m.sequence
                LEFT JOIN metric_catalogue AS c ON c.pid=m.pid
                WHERE m.device_id=? AND m.trip_id=?
                  AND s.device_monotonic_ms BETWEEN ? AND ?
                  AND ((m.pid>='0x100' AND m.pid<'0x200') OR m.pid='0x024')
                  AND m.numeric_value IS NOT NULL
                GROUP BY m.pid ORDER BY m.pid
            """, (device, trip, max(0, code["first_monotonic_ms"]-60000),
                  code["first_monotonic_ms"]+60000))]
            output["confirmed_codes"].append(code)
    return output


def previous_trip_baseline(history: sqlite3.Connection, device: str, trip_id: str) -> dict | None:
    distances, durations, mean_speeds = [], [], []
    acceleration_rates, deceleration_rates = [], []
    candidates = history.execute("""
        SELECT t.trip_id FROM trip AS t JOIN ingest_file AS i ON i.archive_path=t.archive_path
        WHERE t.device_id=? AND t.trip_id<? AND i.sealed=1 AND t.sample_count>=60
        ORDER BY t.trip_id DESC LIMIT 30
    """, (device, trip_id)).fetchall()
    for row in candidates:
        prior = row[0]
        if moving_readings(history, device, prior) < 20:
            continue
        low = metric_extreme(history, device, prior, "0x030", "MIN")
        high = metric_extreme(history, device, prior, "0x030")
        if low is not None and high is not None:
            distances.append(max(0, high - low))
        start, end = history.execute("""
            SELECT MIN(device_monotonic_ms), MAX(device_monotonic_ms) FROM sample
            WHERE device_id=? AND trip_id=?
        """, (device, prior)).fetchone()
        if start is not None and end >= start:
            durations.append((end - start) / 60000)
        speed = metric_extreme(history, device, prior, "0x10D", "AVG")
        if speed is not None:
            mean_speeds.append(speed)
        dynamics = driving_dynamics(history, device, prior)
        if dynamics and dynamics["moving_minutes"] >= 5:
            hours = dynamics["moving_minutes"] / 60
            acceleration_rates.append(dynamics["speed_rise_episodes"] / hours)
            deceleration_rates.append(dynamics["speed_fall_episodes"] / hours)
        if len(durations) >= 10:
            break
    if len(durations) < 3:
        return None
    return {"prior_driving_trips": len(durations),
            "median_duration_minutes": round(statistics.median(durations), 1),
            "median_distance_km": round(statistics.median(distances), 2) if distances else None,
            "median_observed_obd_speed_kph": round(statistics.median(mean_speeds), 1)
                if mean_speeds else None,
            "speed_change_baseline": {
                "comparable_trips": len(acceleration_rates),
                "speed_rise_episodes_per_moving_hour_median":
                    round(statistics.median(acceleration_rates), 1),
                "speed_fall_episodes_per_moving_hour_median":
                    round(statistics.median(deceleration_rates), 1),
                "threshold_mps2": 1.5,
            } if len(acceleration_rates) >= 3 else None}


def process_health(history: sqlite3.Connection, state: sqlite3.Connection,
                   device: str, topic: str, token: str) -> None:
    latest = history.execute("""
        SELECT trip_id, sequence, archive_mtime_ms FROM sample
        WHERE device_id=? ORDER BY trip_id DESC, sequence DESC LIMIT 1
    """, (device,)).fetchone()
    if not latest or abs(int(time.time() * 1000) - latest["archive_mtime_ms"]) > FRESH_MS:
        return
    values = {row["pid"]: row["numeric_value"] for row in history.execute("""
        SELECT pid, numeric_value FROM sample_metric
        WHERE device_id=? AND trip_id=? AND sequence=? AND pid IN ('0x08E','0x08F')
    """, (device, latest["trip_id"], latest["sequence"]))}
    if "0x08E" not in values or "0x08F" not in values:
        return
    missed, healthy = int(values["0x08E"]), int(values["0x08F"])
    previous = state.execute("SELECT * FROM health_cursor WHERE device_id=?", (device,)).fetchone()
    if previous:
        if missed > previous["missed_readings"]:
            publish_ntfy(topic, token, "Vehicle readings missed",
                         f"{missed - previous['missed_readings']} collection cycles missed since the last report. "
                         f"Total since boot: {missed}. Check the SD card and serial log.", 5)
        if healthy != previous["queue_healthy"]:
            publish_ntfy(topic, token, "Vehicle storage fault" if not healthy else "Vehicle storage recovered",
                         "SD journal unhealthy; readings are held in finite RAM." if not healthy else
                         "SD journal reports healthy again.", 5 if not healthy else 3)
    state.execute("""
        INSERT INTO health_cursor(device_id,missed_readings,queue_healthy) VALUES(?,?,?)
        ON CONFLICT(device_id) DO UPDATE SET
            missed_readings=excluded.missed_readings, queue_healthy=excluded.queue_healthy
    """, (device, missed, healthy))
    state.commit()


def process_diagnostic_alerts(history: sqlite3.Connection, state: sqlite3.Connection,
                              device: str, topic: str, token: str) -> None:
    recent_trips = history.execute("""
        SELECT trip_id, MAX(device_monotonic_ms) AS last_monotonic_ms
        FROM sample WHERE device_id=? GROUP BY trip_id
        ORDER BY trip_id DESC LIMIT 3
    """, (device,)).fetchall()
    for latest in recent_trips:
      trip_id = latest["trip_id"]
      diagnostics = diagnostic_evidence(history, device, trip_id)
      for code in diagnostics["confirmed_codes"]:
        key = (device, trip_id, code["status"], code["code"])
        incident = state.execute("""
            SELECT * FROM diagnostic_incident
            WHERE device_id=? AND trip_id=? AND status=? AND code=?
        """, key).fetchone()
        if incident and incident["alert_notified"]:
            continue
        if not incident and latest["last_monotonic_ms"] - code["last_monotonic_ms"] > FRESH_MS:
            continue
        fresh = history.execute("""
            SELECT archive_mtime_ms FROM sample WHERE device_id=? AND trip_id=?
              AND device_monotonic_ms=? LIMIT 1
        """, (device, trip_id, code["last_monotonic_ms"])).fetchone()
        if not incident and (not fresh or abs(int(time.time() * 1000) - fresh[0]) > FRESH_MS):
            # Imported history must never generate a live fault alert.
            continue
        state.execute("""
            INSERT OR IGNORE INTO diagnostic_incident(device_id,trip_id,status,code)
            VALUES(?,?,?,?)
        """, key)
        state.commit()
        publish_ntfy(topic, token, "Vehicle fault code detected",
                     f"{code['status'].title()} DTC {code['code']} observed on trip {trip_id}; "
                     f"{'present' if code['active_at_last_scan'] else 'not present'} at the last scan. "
                     "A deeper evidence report will follow after the trip closes.", 5)
        state.execute("""
            UPDATE diagnostic_incident SET alert_notified=1
            WHERE device_id=? AND trip_id=? AND status=? AND code=?
        """, key)
        state.commit()


def evidence_for_trip(history: sqlite3.Connection, trip: sqlite3.Row) -> dict:
    device, trip_id = trip["device_id"], trip["trip_id"]
    first, last = history.execute("""
        SELECT MIN(device_monotonic_ms), MAX(device_monotonic_ms) FROM sample
        WHERE device_id=? AND trip_id=?
    """, (device, trip_id)).fetchone()
    duration = round((last - first) / 60000, 1) if first is not None and last >= first else None
    diagnostics = diagnostic_evidence(history, device, trip_id)
    distance_min = metric_extreme(history, device, trip_id, "0x030", "MIN")
    distance_max = metric_extreme(history, device, trip_id, "0x030")
    signals = {
        "obd_speed_kph": metric_summary(history, device, trip_id, "0x10D"),
        "engine_rpm": metric_summary(history, device, trip_id, "0x10C"),
        "engine_load_percent": metric_summary(history, device, trip_id, "0x104"),
        "throttle_percent": metric_summary(history, device, trip_id, "0x111"),
        "accelerator_pedal_percent": metric_summary(history, device, trip_id, "0x149"),
        "mass_air_flow_gps": metric_summary(history, device, trip_id, "0x110"),
        "engine_fuel_rate_lph": metric_summary(history, device, trip_id, "0x15E"),
        "coolant_celsius": metric_summary(history, device, trip_id, "0x105"),
        "vehicle_supply_volts": metric_summary(history, device, trip_id, "0x024", 0.01),
        "device_motion_magnitude_g": motion_magnitude_summary(history, device, trip_id),
    }
    signals = {name: stats for name, stats in signals.items() if stats is not None}
    return {
        "device_id": device, "trip_id": trip_id,
        "samples": trip["sample_count"],
        "duration_minutes": duration,
        "distance_km": round(max(0, distance_max - distance_min), 2)
            if distance_min is not None and distance_max is not None else None,
        "recent_trip_baseline": previous_trip_baseline(history, device, trip_id),
        "observed_vehicle_signals": signals,
        "driving_dynamics": driving_dynamics(history, device, trip_id),
        "all_observed_obd_pids": obd_metric_inventory(history, device, trip_id),
        "obd_trends": obd_trend_windows(history, device, trip_id, first, last)
            if first is not None and last is not None else None,
        "obd_pid_timeline": obd_pid_timeline(history, device, trip_id, first, last),
        "maf_vs_matched_prior_operation": matched_maf_baseline(history, device, trip_id),
        "maf_baseline_limits": "Same device, warm coolant and roughly matched RPM/load; engine identity, EGR, boost, air temperature and route may differ. A MAF change alone does not identify a filter or sensor fault.",
        "diagnostic_capabilities": {
            "mode06_per_cylinder_misfire_counters_captured": False,
            "engine_variant_verified_for_spec_limits": False,
            "vehicle_specific_tolerances_available": False,
        },
        "reference_knowledge": [
            {"topic": "OBD mode scope", "url": "https://saemobilus.sae.org/standards/j1979_199709-e-e-diagnostic-test-modes",
             "applies": "Generic standard mode semantics; not vehicle-specific tolerances."},
            {"topic": "MAF function and checks", "url": "https://www.hella.com/techworld/uk/technical/sensors-and-actuators/check-air-mass-sensor/",
             "applies": "MAF is between air filter and intake; wiring, sensor and airflow must be checked together."},
            {"topic": "Air mass and lambda diagnosis", "url": "https://www.boschaftermarket.com/xrm/media/images/services/news_3/2023_08_bosch_sensors_campaign/magazine_article_03_lambda_and_air_mass_sensor.pdf",
             "applies": "Compensating fuel control can mask air or exhaust leaks; a single sensor value is not a part diagnosis."},
        ],
        "diagnostics": diagnostics,
    }


def openrouter_report(evidence: dict, key: str, model: str) -> dict:
    has_codes = bool(evidence.get("diagnostics", {}).get("confirmed_codes"))
    fault_item = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "code": {"type": "string"},
            "interpretation": {"type": "string"},
            "possible_causes": {"type": "array", "items": {"type": "string"}},
            "evidence_for": {"type": "array", "items": {"type": "string"}},
            "evidence_against": {"type": "array", "items": {"type": "string"}},
            "next_checks": {"type": "array", "items": {"type": "string"}},
            "source_urls": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["code", "interpretation", "possible_causes", "evidence_for",
                     "evidence_against", "next_checks", "source_urls"],
    }
    schema = {
        "name": "trip_report", "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "summary": {"type": "string"},
                "patterns": {"type": "array", "items": {"type": "string"}},
                "possible_issues": {"type": "array", "items": {"type": "string"}},
                "data_limits": {"type": "array", "items": {"type": "string"}},
                "fault_analysis": {"type": "array", "items": fault_item},
            },
            "required": ["summary", "patterns", "possible_issues", "data_limits", "fault_analysis"],
        },
    }
    payload = {
        "model": model, "temperature": 0.2,
        "max_tokens": 5000 if has_codes else 3000,
        "reasoning": {"effort": "medium"} if model.startswith("openai/gpt-6")
            else {"enabled": False},
        "response_format": {"type": "json_schema", "json_schema": schema},
        "messages": [
            {"role": "system", "content": (
                "You summarise observed vehicle behaviour from telemetry. Use only supplied positive measurements. "
                "Missing GPS fixes, data gaps, standby periods, absent OBD values and uncertain wall-clock "
                "timestamps are expected collection limits, not vehicle problems or interesting patterns. "
                "Do not mention them in the report. Never infer a fault from missing measurements. "
                "Only list an issue when a measured value or confirmed diagnostic code directly supports it; "
                "otherwise return an empty possible_issues list. Aggregate min/mean/max values do not show "
                "route or system health over time; do not claim those patterns from averages. "
                "Driving dynamics are measured from consecutive OBD speed samples; they show speed "
                "changes but not brake-pedal use or driver intent. Compare episode rates with the "
                "prior-trip baseline only when at least three comparable trips exist. "
                "Comparisons with prior trips describe differences only; routes and conditions may differ. "
                "For confirmed DTCs, examine scan status, earlier sightings, all OBD PIDs and the "
                "measurements near the first scan observation. Distinguish codes still present at the last "
                "scan from codes seen earlier and now absent. A scan observation is not the fault onset. "
                "Compare the time windows and matched-operation MAF evidence before proposing a trend. "
                "Low MAF alone cannot identify an air filter, MAF sensor, EGR or boost fault. "
                "Do not claim individual-cylinder firing or misfire from this Mode 01 data; Mode 06 "
                "misfire counters are not captured. Do not apply vehicle-specific min/max tolerances "
                "without a verified engine variant and an applicable cited specification. "
                "Give competing causes, direct supporting and contradicting evidence, and safe next checks. "
                "Distinguish generic code meanings from vehicle-specific causes; never invent a VIN or engine. "
                "Use web search only when a code needs external interpretation, and cite URLs actually returned. "
                "With no confirmed code, return an empty fault_analysis array. "
                "Avoid safety or moral driving judgements. Write like a mechanic speaking to the owner: "
                "one plain conclusion of at most 25 words; at most three measured patterns of at "
                "most 28 words each; at most two supported possible issues of at most 35 words "
                "each; and at most two material data limits of at most 25 words each. Investigate "
                "deeply, then write only the useful conclusion, not the investigation transcript. Empty arrays "
                "are better than filler. Do not repeat facts or catalogue normal readings. Explain "
                "jargon briefly and never repeat the device or trip ID in the summary."
            )},
            {"role": "user", "content": json.dumps(evidence, separators=(",", ":"))},
        ],
    }
    if has_codes:
        payload["tools"] = [{"type": "openrouter:web_search",
                             "parameters": {"max_results": 5, "max_total_results": 8}}]
    request = urllib.request.Request(
        OPENROUTER_URL, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
         "HTTP-Referer": "https://freematics.drewett.dev", "X-OpenRouter-Title": "Freematics Trip Intelligence"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    returned_model = result.get("model", "")
    if not returned_model or not returned_model.startswith(model):
        raise ValueError(f"OpenRouter returned unexpected model {returned_model!r} for {model!r}")
    content = result["choices"][0]["message"].get("content")
    if not content:
        raise ValueError(f"OpenRouter returned no report text ({result['choices'][0].get('finish_reason')})")
    report = json.loads(content)
    if not isinstance(report.get("summary"), str) or any(
        not isinstance(report.get(name), list) for name in ("patterns", "possible_issues", "data_limits", "fault_analysis")
    ):
        raise ValueError("OpenRouter report did not match the required schema")
    for name in ("patterns", "possible_issues", "data_limits"):
        report[name] = [item for item in report[name] if isinstance(item, str)
                        and not any(term in item.lower() for term in COLLECTION_LIMIT_TERMS)]
    report["patterns"] = [item for item in report["patterns"]
                          if not any(term in item.lower() for term in UNSUPPORTED_PATTERN_TERMS)]
    observed_codes = {item["code"] for item in evidence.get("diagnostics", {}).get("confirmed_codes", [])}
    report["fault_analysis"] = [item for item in report["fault_analysis"]
                                if isinstance(item, dict) and item.get("code") in observed_codes]
    if any(term in report["summary"].lower() for term in COLLECTION_LIMIT_TERMS):
        duration = evidence.get("duration_minutes")
        distance = evidence.get("distance_km")
        report["summary"] = (f"Observed trip: {duration} minutes, {distance} km."
                             if duration is not None and distance is not None
                             else "Vehicle trip recorded; see measured signals for details.")
    report["_inference"] = {
        "requested_model": model, "returned_model": returned_model,
        "request_id": result.get("id"), "usage": result.get("usage"),
    }
    return report


def codex_report(evidence: dict, model: str) -> dict:
    """Use an authenticated Codex CLI session and the read-only MCP server."""
    schema = Path(__file__).with_name("mechanic_report.schema.json")
    if not schema.is_file():
        raise RuntimeError("Codex report schema missing")
    device, trip = evidence["device_id"], evidence["trip_id"]
    prompt = (
        f"You are this vehicle's evidence-led mechanic. Analyse device {device}, trip {trip}. "
        "First call freematics vehicle_context, trip_summary, data_quality, dtc_history and "
        "compare_baseline. Inspect the full PID inventory, not only the summary's chosen metrics. "
        "Use metric_series, sample_window and raw_frames to investigate correlated "
        "OBD changes rather than relying on trip-wide averages. Read every observed OBD PID "
        "that bears on a candidate finding. For longitudinal context, read prior stored "
        "analysis_report entries and verify any prior hypothesis against those trips' samples; "
        "a previous model conclusion is never evidence on its own. Use live web search for standards, code meanings "
        "or manufacturer tolerances only when needed; cite the actual primary source URL. "
        "The trip_summary driving_dynamics field measures OBD speed changes only where consecutive "
        "samples are 0.5 to 5 seconds apart. Describe acceleration, deceleration and speed "
        "variation when that field is present. Compare episode rates with the prior-trip baseline "
        "only when at least three comparable trips exist; route and traffic can explain differences. "
        "A deceleration is not necessarily brake use, and speed variation alone is not poor driving. "
        "Do not infer driving style from trip-wide mean or max values. "
        "Never invent vehicle identity, engine-specific limits, per-cylinder firing data or "
        "part diagnoses. Low MAF alone is insufficient to condemn the air filter, sensor, "
        "EGR or boost system. Treat intentional GPS loss, standby and collection gaps as "
        "data limits, not mechanical patterns. Compare warm operating conditions and give "
        "alternative explanations and next checks. If evidence does not support an issue, "
        "leave possible_issues empty. Write the owner-facing result like a good mechanic after "
        "checking the vehicle: direct, specific and calm. Investigate deeply, then write only the "
        "useful conclusion, not the investigation transcript. The summary is one sentence of at most "
        "25 words, stating the actual conclusion. Do not start with 'No mechanical issue is "
        "supported' or repeat the device ID and trip ID. Give at most three patterns, each at "
        "most 28 words. Each pattern should say what changed, cite the measured value or comparison, "
        "and explain why it matters in plain English. Avoid raw sequence numbers unless paired "
        "with a useful time or event. Give at most two possible issues, each at most 35 words, "
        "only when evidence supports them. Empty arrays are preferable to filler or routine normal "
        "readings. Give at most two data limits, each at most 25 words, only if they change the "
        "conclusion. Use technical terms where helpful and explain them briefly. In a fault-code "
        "review, separate the code's meaning, direct evidence, competing causes and one or two "
        "practical next checks. Return the requested JSON report only."
    )
    with tempfile.TemporaryDirectory(prefix="freematics-codex-") as work:
        output = Path(work) / "report.json"
        command = [
            "codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "--skip-git-repo-check", "-m", model,
            "-c", 'mcp_servers.freematics.url="https://freematics.drewett.dev/mcp"',
            "-c", 'mcp_servers.freematics.bearer_token_env_var="FREEMATICS_MCP_TOKEN"',
            "-c", 'mcp_servers.freematics.default_tools_approval_mode="writes"',
            "-c", "mcp_servers.freematics.required=true",
            "-c", "features.shell_tool=false", "-c", 'web_search="live"',
            "-c", 'model_reasoning_effort="high"',
            "--output-schema", str(schema), "-o", str(output), prompt,
        ]
        workdir = "/app" if Path("/app").is_dir() else str(Path(__file__).parent)
        result = subprocess.run(command, cwd=workdir, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0 or not output.is_file():
            raise RuntimeError(f"Codex CLI failed with exit {result.returncode}; check provider login")
        if not re.search(r"mcp: freematics/trip_summary \(completed\)", result.stderr):
            raise RuntimeError("Codex did not successfully inspect the trip through MCP")
        if f"model: {model}" not in result.stderr or "provider: openai" not in result.stderr:
            raise RuntimeError("Codex CLI did not confirm the requested OpenAI model")
        report = json.loads(output.read_text())
    if not isinstance(report.get("summary"), str) or any(
        not isinstance(report.get(name), list)
        for name in ("patterns", "possible_issues", "data_limits", "fault_analysis")
    ):
        raise ValueError("Codex report did not match the required schema")
    observed = {item["code"] for item in evidence["diagnostics"]["confirmed_codes"]}
    report["fault_analysis"] = [item for item in report["fault_analysis"]
                                if isinstance(item, dict) and item.get("code") in observed]
    report["_inference"] = {"provider": "codex", "requested_model": model,
                            "returned_model": model, "mcp_trip_inspected": True}
    return report


def validate_owner_report(report: dict) -> dict:
    """Reject long-form prose before it becomes the visible trip assessment."""
    limits = {"summary": (1, 30), "patterns": (3, 32),
              "possible_issues": (2, 40), "data_limits": (2, 30)}
    for field, (maximum_items, maximum_words) in limits.items():
        value = report.get(field)
        entries = [value] if field == "summary" else value
        if not isinstance(entries, list) or len(entries) > maximum_items:
            raise ValueError(f"Owner report {field} exceeds the concise format")
        if any(not isinstance(entry, str) or not entry.strip() or
               len(entry.split()) > maximum_words for entry in entries):
            raise ValueError(f"Owner report {field} is too long or empty")
    return report


def generate_report(evidence: dict, key: str, model: str) -> dict:
    provider = os.environ.get("FREEMATICS_ANALYSIS_PROVIDER", "openrouter")
    if provider == "codex":
        return validate_owner_report(codex_report(evidence, model))
    if provider == "openrouter":
        return validate_owner_report(openrouter_report(evidence, key, model))
    raise ValueError(f"Unknown analysis provider: {provider}")


def report_message(evidence: dict, report: dict) -> str:
    issue_text = "; ".join(report.get("possible_issues", [])[:3]) or "No specific issue identified."
    message = (f"{report['summary']}\nPossible issues: {issue_text}\n"
               f"Trip: {evidence['trip_id']} · {evidence['samples']} samples")
    if report.get("fault_analysis"):
        fault = report["fault_analysis"][0]
        cause = "; ".join(fault.get("possible_causes", [])[:2]) or "Cause not established"
        check = "; ".join(fault.get("next_checks", [])[:2]) or "Review OBD data"
        message += f"\nDTC {fault['code']}: {cause}\nNext checks: {check}"
    return message


def process_incidents(history: sqlite3.Connection, state: sqlite3.Connection,
                      topic: str, token: str, key: str, model: str) -> None:
    now = int(time.time() * 1000)
    pending = state.execute("""
        SELECT * FROM diagnostic_incident WHERE analysis_notified=0
          AND next_attempt_ms<=? ORDER BY trip_id, code
    """, (now,)).fetchall()
    visited = set()
    for incident in pending:
        group = (incident["device_id"], incident["trip_id"])
        if group in visited:
            continue
        visited.add(group)
        device, trip_id = group
        try:
            saved = state.execute("""
                SELECT analysis_report_json FROM diagnostic_incident
                WHERE device_id=? AND trip_id=? AND analysis_notified=0
                  AND analysis_report_json IS NOT NULL LIMIT 1
            """, group).fetchone()
            if saved:
                result = json.loads(saved[0])
                evidence, report = result["evidence"], result["report"]
            else:
                trip = history.execute("""
                    SELECT * FROM trip WHERE device_id=? AND trip_id=?
                """, group).fetchone()
                if trip is None:
                    raise ValueError("trip no longer indexed")
                evidence = evidence_for_trip(history, trip)
                report = generate_report(evidence, key, model)
                state.execute("""
                    UPDATE diagnostic_incident SET analysis_report_json=?, requested_model=?,
                        returned_model=?, attempts=0, next_attempt_ms=0, last_error=NULL
                    WHERE device_id=? AND trip_id=? AND analysis_notified=0
                """, (json.dumps({"evidence": evidence, "report": report}), model,
                      report.get("_inference", {}).get("returned_model"), device, trip_id))
                state.commit()
            codes = ", ".join(sorted({item["code"] for item in evidence["diagnostics"]["confirmed_codes"]}))
            faults = report.get("fault_analysis", [])
            detail = "; ".join(f"{item['code']}: {item['interpretation']}" for item in faults[:3])
            checks = "; ".join(check for item in faults[:3] for check in item.get("next_checks", [])[:1])
            message = (f"Codes: {codes}\n{detail or report['summary']}\n"
                       f"Next checks: {checks or 'Review the full report and verify the code.'}\n"
                       f"Trip: {trip_id}")
            publish_ntfy(topic, token, "Vehicle diagnostic review", message[:3500], 5)
            state.execute("""
                UPDATE diagnostic_incident SET analysis_notified=1, attempts=0,
                    next_attempt_ms=0, last_error=NULL
                WHERE device_id=? AND trip_id=? AND analysis_notified=0
            """, group)
            state.commit()
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, RuntimeError,
                subprocess.TimeoutExpired, urllib.error.URLError) as exc:
            attempts = max(row[0] for row in state.execute("""
                SELECT attempts FROM diagnostic_incident
                WHERE device_id=? AND trip_id=? AND analysis_notified=0
            """, group)) + 1
            delay_ms = min(60 * (2 ** min(attempts - 1, 6)), 3600) * 1000
            state.execute("""
                UPDATE diagnostic_incident SET attempts=?, next_attempt_ms=?, last_error=?
                WHERE device_id=? AND trip_id=? AND analysis_notified=0
            """, (attempts, now + delay_ms, f"{type(exc).__name__}: {str(exc)[:300]}",
                  device, trip_id))
            state.commit()
            print(f"[trip-intelligence] diagnostic {trip_id} attempt {attempts}: {exc}", flush=True)
            if attempts == 3:
                try:
                    publish_ntfy(topic, token, "Vehicle diagnosis delayed",
                                 f"GPT-6 review for trip {trip_id} failed three times; retrying automatically.", 4)
                except (OSError, urllib.error.URLError):
                    pass


def record_analysis_failure(state: sqlite3.Connection, device: str, trip_id: str,
                            signature: str, prior: sqlite3.Row | None,
                            error: Exception, topic: str, token: str) -> None:
    attempts = ((prior["attempts"] if prior and prior["signature"] == signature else 0) or 0) + 1
    delay_ms = min(60 * (2 ** min(attempts - 1, 6)), 3600) * 1000
    next_attempt = int(time.time() * 1000) + delay_ms
    state.execute("""
        INSERT INTO trip_report(device_id,trip_id,signature,notified,attempts,next_attempt_ms,last_error)
        VALUES(?,?,?,0,?,?,?) ON CONFLICT(device_id,trip_id) DO UPDATE SET
            signature=excluded.signature, notified=0, attempts=excluded.attempts,
            next_attempt_ms=excluded.next_attempt_ms, last_error=excluded.last_error
    """, (device, trip_id, signature, attempts, next_attempt,
          f"{type(error).__name__}: {str(error)[:300]}"))
    state.commit()
    print(f"[trip-intelligence] trip {trip_id} attempt {attempts} failed: {type(error).__name__}: {error}",
          flush=True)
    if attempts in (3, 10):
        try:
            publish_ntfy(topic, token, "Vehicle analysis delayed",
                         f"Trip {trip_id} has failed {attempts} GPT-6/report attempts. "
                         f"Retrying automatically; last error: {type(error).__name__}.", 4)
        except (OSError, urllib.error.URLError) as exc:
            print(f"[trip-intelligence] delay alert failed: {exc}", flush=True)


def process_trips(history: sqlite3.Connection, state: sqlite3.Connection,
                  topic: str, ntfy_token: str, openrouter_key: str,
                  model: str, bootstrap: bool) -> None:
    # Finish the read statement before a report invokes Codex or ntfy. An
    # active SQLite cursor keeps a read lock for the entire model call and
    # prevents the history indexer from committing new device samples.
    trips = history.execute("""
        SELECT t.*, i.content_sha256, i.mutation_detected
        FROM trip AS t JOIN ingest_file AS i ON i.archive_path=t.archive_path
        WHERE i.sealed=1 AND t.sample_count>=3
        ORDER BY t.trip_id
    """).fetchall()
    for trip in trips:
        device, trip_id = trip["device_id"], trip["trip_id"]
        signature = f"{trip['sample_count']}:{trip['data_bytes']}:{trip['content_sha256']}"
        saved = state.execute("SELECT * FROM trip_report WHERE device_id=? AND trip_id=?",
                              (device, trip_id)).fetchone()
        # Older workers used the projection's updated_at_ms in this signature.
        # A rebuild changes it for unchanged archives. An already-notified
        # trip belongs in silent historical review, even if its raw file later
        # changes; keep the prior report until the replacement succeeds.
        if saved and saved["notified"] and saved["signature"] != signature:
            state.execute("""
                UPDATE trip_report SET signature=?, analysis_revision=0
                WHERE device_id=? AND trip_id=?
            """, (signature, device, trip_id))
            state.commit()
            if trip["mutation_detected"]:
                print(f"[trip-intelligence] archive mutation queued for review: {trip_id}", flush=True)
            saved = state.execute("SELECT * FROM trip_report WHERE device_id=? AND trip_id=?",
                                  (device, trip_id)).fetchone()
        if saved and saved["signature"] == signature and saved["notified"]:
            continue
        if saved and saved["signature"] == signature and saved["next_attempt_ms"] > int(time.time() * 1000):
            continue
        if bootstrap and saved is None:
            state.execute("INSERT INTO trip_report(device_id,trip_id,signature,notified) VALUES(?,?,?,1)",
                          (device, trip_id, signature))
            state.commit()
            continue
        try:
            has_codes = bool(diagnostic_evidence(history, device, trip_id)["confirmed_codes"])
            if not has_codes and (trip["sample_count"] < 60 or moving_readings(history, device, trip_id) < 20):
                save_screened_trip(state, device, trip_id, signature,
                                   {"decision": "no_report", "reasons": [],
                                    "screen_revision": 1, "basis": "insufficient driving data"})
                continue
            if saved and saved["signature"] == signature and saved["report_json"] and \
                    saved["analysis_revision"] >= ANALYSIS_REVISION:
                result = json.loads(saved["report_json"])
                evidence, report = result["evidence"], result["report"]
                screen = json.loads(saved["screen_json"] or "{}")
            else:
                evidence = evidence_for_trip(history, trip)
                screen = (json.loads(saved["screen_json"])
                          if saved and saved["signature"] == signature and saved["screen_json"]
                          else screen_trip_interest(evidence, openrouter_key))
                save_screen_verdict(state, device, trip_id, signature, screen)
                if screen["decision"] != "review":
                    save_screened_trip(state, device, trip_id, signature, screen)
                    continue
                report = generate_report(evidence, openrouter_key, model)
                is_dtc_case = bool(evidence["diagnostics"]["confirmed_codes"])
                if not (is_dtc_case or report.get("patterns") or report.get("possible_issues")
                        or report.get("fault_analysis")):
                    screen["decision"] = "no_report"
                    screen["reason_after_review"] = "No useful owner-facing finding survived review."
                    save_screened_trip(state, device, trip_id, signature, screen)
                    continue
                state.execute("""
                    INSERT INTO trip_report(device_id,trip_id,signature,report_json,notified,
                                            requested_model,returned_model,analyzed_at_ms,analysis_revision,
                                            screen_json)
                    VALUES(?,?,?,?,0,?,?,?,?,?) ON CONFLICT(device_id,trip_id) DO UPDATE SET
                        signature=excluded.signature, report_json=excluded.report_json,
                        notified=0, requested_model=excluded.requested_model,
                        returned_model=excluded.returned_model, analyzed_at_ms=excluded.analyzed_at_ms,
                        analysis_revision=excluded.analysis_revision,
                        screen_json=excluded.screen_json,
                        attempts=0, next_attempt_ms=0, last_error=NULL
                """, (device, trip_id, signature, json.dumps({"evidence": evidence, "report": report}),
                      model, report.get("_inference", {}).get("returned_model"), int(time.time() * 1000),
                      ANALYSIS_REVISION, json.dumps(screen, separators=(",", ":"))))
                state.commit()
            publish_ntfy(topic, ntfy_token, "Vehicle trip report", report_message(evidence, report),
                         4 if report.get("possible_issues") or report.get("fault_analysis") else 3)
            state.execute("""
                UPDATE trip_report SET notified=1, notified_at_ms=?, attempts=0,
                    next_attempt_ms=0, last_error=NULL WHERE device_id=? AND trip_id=?
            """, (int(time.time() * 1000), device, trip_id))
            state.commit()
            return
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, RuntimeError,
                subprocess.TimeoutExpired, urllib.error.URLError) as exc:
            record_analysis_failure(state, device, trip_id, signature,
                                    state.execute("SELECT * FROM trip_report WHERE device_id=? AND trip_id=?",
                                                  (device, trip_id)).fetchone(),
                                    exc, topic, ntfy_token)


def process_history_backfill(history: sqlite3.Connection, state: sqlite3.Connection,
                             topic: str, token: str, key: str, model: str) -> None:
    """Review one older driving trip per pass without old-trip push noise."""
    now = int(time.time() * 1000)
    candidates = history.execute("""
        SELECT t.* FROM trip AS t JOIN ingest_file AS i ON i.archive_path=t.archive_path
        WHERE i.sealed=1 AND t.sample_count>=60
        ORDER BY t.trip_id DESC
    """).fetchall()
    for trip in candidates:
        device, trip_id = trip["device_id"], trip["trip_id"]
        saved = state.execute("""
            SELECT * FROM trip_report WHERE device_id=? AND trip_id=?
        """, (device, trip_id)).fetchone()
        if not saved or not saved["notified"] or saved["analysis_revision"] >= ANALYSIS_REVISION:
            continue
        if saved["next_attempt_ms"] > now or moving_readings(history, device, trip_id) < 20:
            continue
        try:
            signature = saved["signature"]
            evidence = evidence_for_trip(history, trip)
            screen = (json.loads(saved["screen_json"])
                      if saved["screen_json"] else screen_trip_interest(evidence, key))
            save_screen_verdict(state, device, trip_id, signature, screen)
            if screen["decision"] != "review":
                save_screened_trip(state, device, trip_id, signature, screen, now)
                print(f"[trip-intelligence] historical {trip_id} screened; no report trigger", flush=True)
                return
            report = generate_report(evidence, key, model)
            is_dtc_case = bool(evidence["diagnostics"]["confirmed_codes"])
            if not (is_dtc_case or report.get("patterns") or report.get("possible_issues")
                    or report.get("fault_analysis")):
                screen["decision"] = "no_report"
                screen["reason_after_review"] = "No useful owner-facing finding survived review."
                save_screened_trip(state, device, trip_id, signature, screen, now)
                print(f"[trip-intelligence] historical {trip_id} review produced no owner report", flush=True)
                return
            state.execute("""
                UPDATE trip_report SET report_json=?, requested_model=?, returned_model=?,
                    analyzed_at_ms=?, analysis_revision=?, attempts=0, next_attempt_ms=0,
                    last_error=NULL, screen_json=?
                WHERE device_id=? AND trip_id=? AND notified=1
            """, (json.dumps({"evidence": evidence, "report": report}), model,
                  report.get("_inference", {}).get("returned_model"), now, ANALYSIS_REVISION,
                  json.dumps(screen, separators=(",", ":")),
                  device, trip_id))
            state.commit()
            print(f"[trip-intelligence] historical review stored for {trip_id}", flush=True)
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, RuntimeError,
                subprocess.TimeoutExpired, urllib.error.URLError) as exc:
            attempts = (saved["attempts"] or 0) + 1
            delay_ms = min(60 * (2 ** min(attempts - 1, 6)), 3600) * 1000
            state.execute("""
                UPDATE trip_report SET attempts=?, next_attempt_ms=?, last_error=?
                WHERE device_id=? AND trip_id=?
            """, (attempts, now + delay_ms, f"{type(exc).__name__}: {str(exc)[:300]}", device, trip_id))
            state.commit()
            print(f"[trip-intelligence] historical {trip_id} attempt {attempts}: {exc}", flush=True)
            if attempts == 3:
                try:
                    publish_ntfy(topic, token, "Vehicle historical review delayed",
                                 f"Trip {trip_id} GPT-6 review failed three times; retrying automatically.", 4)
                except (OSError, urllib.error.URLError):
                    pass
        return


def codex_login_ready(state: sqlite3.Connection, topic: str, token: str) -> bool:
    status = subprocess.run(["codex", "login", "status"], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=15, check=False)
    ready = status.returncode == 0 and "ChatGPT" in (status.stdout + status.stderr)
    alerted = state.execute("""
        SELECT 1 FROM worker_meta WHERE key='codex_login_needed'
    """).fetchone() is not None
    if not ready and not alerted:
        publish_ntfy(topic, token, "Vehicle AI login needed",
                     "Trip analysis is queued. Sign in at https://freematics.drewett.dev/; event alerts continue.", 4)
        state.execute("INSERT INTO worker_meta(key,value) VALUES('codex_login_needed','1')")
        state.commit()
    elif ready and alerted:
        state.execute("DELETE FROM worker_meta WHERE key='codex_login_needed'")
        state.commit()
        publish_ntfy(topic, token, "Vehicle AI analysis resumed",
                     "The Codex worker is signed into ChatGPT and will review queued trips.", 3)
    return ready


def run_once(history_path: Path, state_path: Path, topic: str,
             ntfy_token: str, openrouter_key: str, model: str,
             mode: str = "both") -> None:
    with closing(open_history(history_path)) as history, closing(open_state(state_path)) as state:
        if mode in ("both", "events"):
            devices = [row[0] for row in history.execute("SELECT DISTINCT device_id FROM trip")]
            for device in devices:
                for handler in (process_motion, process_health, process_diagnostic_alerts):
                    try:
                        handler(history, state, device, topic, ntfy_token)
                    except (sqlite3.Error, OSError, ValueError, KeyError, urllib.error.URLError) as exc:
                        print(f"[trip-intelligence] {handler.__name__} {device}: {exc}", flush=True)
        if mode in ("both", "analysis"):
            if os.environ.get("FREEMATICS_ANALYSIS_PROVIDER") == "codex" and not codex_login_ready(state, topic, ntfy_token):
                return
            bootstrap = state.execute("SELECT 1 FROM worker_meta WHERE key='bootstrapped'").fetchone() is None
            process_incidents(history, state, topic, ntfy_token, openrouter_key, model)
            process_trips(history, state, topic, ntfy_token, openrouter_key, model, bootstrap)
            if bootstrap:
                state.execute("INSERT INTO worker_meta(key,value) VALUES('bootstrapped','1')")
                state.commit()
            if os.environ.get("FREEMATICS_ANALYSIS_PROVIDER") == "codex":
                process_history_backfill(history, state, topic, ntfy_token, openrouter_key, model)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--mode", choices=("events", "analysis", "both"), default="both")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    topic = os.environ.get("FREEMATICS_NTFY_TOPIC", "freematics-device")
    ntfy_token = os.environ.get("NTFY_API_TOKEN", "")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    provider = os.environ.get("FREEMATICS_ANALYSIS_PROVIDER", "openrouter")
    model = (os.environ.get("CODEX_MODEL", "gpt-6-sol") if provider == "codex"
             else os.environ.get("OPENROUTER_MODEL", "openai/gpt-6-sol"))
    if not ntfy_token or (provider == "openrouter" and args.mode != "events" and not openrouter_key):
        parser.error("NTFY_API_TOKEN and the selected provider credential are required")
    while True:
        try:
            run_once(args.history, args.state, topic, ntfy_token, openrouter_key, model, args.mode)
        except (sqlite3.Error, OSError, ValueError, KeyError, RuntimeError,
                subprocess.TimeoutExpired, urllib.error.URLError) as exc:
            print(f"[trip-intelligence] {type(exc).__name__}: {exc}", flush=True)
        if args.once:
            break
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    main()
