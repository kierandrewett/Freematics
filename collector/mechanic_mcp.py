#!/usr/bin/env python3
"""Read-only, bounded MCP access to the Freematics history index."""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from trip_intelligence import (diagnostic_evidence, evidence_for_trip,
                               matched_maf_baseline, previous_trip_baseline)
from waveforms import waveform_window, waveform_trip_summary
from condition_baselines import acquisition_quality, contextual_baselines

HISTORY = Path(os.environ.get("FREEMATICS_HISTORY_DB", "/history/history.sqlite"))
REPORTS = Path(os.environ.get("FREEMATICS_INTELLIGENCE_DB", "/state/intelligence.sqlite"))
TOKEN = os.environ.get("FREEMATICS_MCP_TOKEN", "")
PID_RE = re.compile(r"^0x[0-9A-Fa-f]{3}$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                            idempotentHint=True, openWorldHint=False)

mcp = FastMCP(
    "freematics-mechanic",
    instructions=("Read-only vehicle telemetry. Start with vehicle_context and list_trips, "
                  "then inspect named signals, aligned samples, sensor_waveforms, diagnostic codes and baselines. "
                  "Report measured evidence, alternative causes and limitations. GPS/collection "
                  "loss is not a vehicle fault. Never guess engine-specific tolerances."),
    host="0.0.0.0", port=8018, streamable_http_path="/mcp",
    stateless_http=True, json_response=True,
)


def db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ident(value: str) -> str:
    if not IDENT_RE.fullmatch(value):
        raise ValueError("invalid device or trip identifier")
    return value


def pid(value: str) -> str:
    if not PID_RE.fullmatch(value):
        raise ValueError("PID must be three hexadecimal digits, e.g. 0x110")
    return value.upper().replace("0X", "0x")


def rows(cursor: sqlite3.Cursor) -> list[dict]:
    return [dict(row) for row in cursor]


@mcp.tool(annotations=READ_ONLY)
def vehicle_context(device_id: str) -> dict:
    """Device coverage, indexed signals and verified identity limits."""
    device_id = ident(device_id)
    with db(HISTORY) as conn:
        trips = conn.execute("""
            SELECT COUNT(*) AS trips, MIN(trip_id) AS first_trip,
                   MAX(trip_id) AS latest_trip, SUM(sample_count) AS indexed_samples
            FROM trip WHERE device_id=?
        """, (device_id,)).fetchone()
        signals = rows(conn.execute("""
            SELECT m.pid, c.name, c.unit, COUNT(*) AS observations
            FROM sample_metric m LEFT JOIN metric_catalogue c ON c.pid=m.pid
            WHERE m.device_id=? AND m.pid>='0x100' AND m.pid<'0x200'
            GROUP BY m.pid ORDER BY m.pid
        """, (device_id,)))
    return {"device_id": device_id, "coverage": dict(trips),
            "observed_standard_obd_signals": signals,
            "engine_variant": "unverified", "vehicle_specific_limits": "unavailable",
            "mode06_misfire_counters": "not captured",
            "notes": "An observed PID is not proof that all sessions contain it; inspect timestamps and quality."}


@mcp.tool(annotations=READ_ONLY)
def list_trips(device_id: str, limit: int = 30, before_trip: str = "") -> list[dict]:
    """Newest indexed trips with coverage; limit 1 to 100."""
    device_id = ident(device_id)
    if before_trip:
        ident(before_trip)
    limit = max(1, min(int(limit), 100))
    with db(HISTORY) as conn:
        return rows(conn.execute("""
            SELECT trip_id, sample_count, data_bytes, start_capture_ms, end_capture_ms,
                   timestamp_quality, time_basis, archive_mtime_ms, gap_count
            FROM trip WHERE device_id=? AND (?='' OR trip_id<?)
            ORDER BY trip_id DESC LIMIT ?
        """, (device_id, before_trip, before_trip, limit)))


@mcp.tool(annotations=READ_ONLY)
def metric_catalogue(search: str = "", limit: int = 100) -> list[dict]:
    """PID definitions and units. Search by name, description or PID."""
    limit = max(1, min(int(limit), 200))
    search = search[:80]
    with db(HISTORY) as conn:
        return rows(conn.execute("""
            SELECT pid,name,description,unit,category,priority FROM metric_catalogue
            WHERE ?='' OR pid LIKE ? OR name LIKE ? OR description LIKE ?
            ORDER BY pid LIMIT ?
        """, (search, f"%{search}%", f"%{search}%", f"%{search}%", limit)))


@mcp.tool(annotations=READ_ONLY)
def trip_summary(device_id: str, trip_id: str) -> dict:
    """Whole-trip evidence, chronological windows, DTCs and baseline comparisons."""
    device_id, trip_id = ident(device_id), ident(trip_id)
    with db(HISTORY) as conn:
        trip = conn.execute("SELECT * FROM trip WHERE device_id=? AND trip_id=?",
                            (device_id, trip_id)).fetchone()
        if trip is None:
            raise ValueError("trip not found")
        return evidence_for_trip(conn, trip)


@mcp.tool(annotations=READ_ONLY)
def metric_series(device_id: str, trip_id: str, metric_pid: str,
                  start_sequence: int = 0, limit: int = 300) -> dict:
    """Timestamped numeric/text readings for one PID; paginate by sequence."""
    device_id, trip_id, metric_pid = ident(device_id), ident(trip_id), pid(metric_pid)
    limit = max(1, min(int(limit), 500))
    start_sequence = max(0, int(start_sequence))
    with db(HISTORY) as conn:
        meta = conn.execute("SELECT pid,name,description,unit FROM metric_catalogue WHERE pid=?",
                            (metric_pid,)).fetchone()
        values = rows(conn.execute("""
            SELECT s.sequence, s.device_monotonic_ms, s.capture_utc_ms,
                   s.collector_received_ms, s.timestamp_quality, s.time_basis,
                   m.numeric_value, m.text_value
            FROM sample_metric m JOIN sample s
              ON s.device_id=m.device_id AND s.trip_id=m.trip_id AND s.sequence=m.sequence
            WHERE m.device_id=? AND m.trip_id=? AND m.pid=? AND m.sequence>=?
            ORDER BY m.sequence LIMIT ?
        """, (device_id, trip_id, metric_pid, start_sequence, limit)))
    return {"pid": metric_pid, "metadata": dict(meta) if meta else None,
            "values": values, "next_sequence": values[-1]["sequence"] + 1 if values else None,
            "provenance": "sample_metric projection of raw archive; source time is device monotonic or capture UTC when valid"}


@mcp.tool(annotations=READ_ONLY)
def sample_window(device_id: str, trip_id: str, start_sequence: int,
                  limit: int = 40) -> list[dict]:
    """Aligned raw PID values, location and quality for up to 100 samples."""
    device_id, trip_id = ident(device_id), ident(trip_id)
    limit = max(1, min(int(limit), 100))
    with db(HISTORY) as conn:
        samples = rows(conn.execute("""
            SELECT sequence,device_monotonic_ms,capture_utc_ms,collector_received_ms,
                   timestamp_quality,time_basis,latitude,longitude,gps_speed_kph,gps_hdop
            FROM sample WHERE device_id=? AND trip_id=? AND sequence>=?
            ORDER BY sequence LIMIT ?
        """, (device_id, trip_id, max(0, int(start_sequence)), limit)))
        if not samples:
            return []
        metrics = conn.execute("""
            SELECT sequence,pid,numeric_value,text_value FROM sample_metric
            WHERE device_id=? AND trip_id=? AND sequence BETWEEN ? AND ?
            ORDER BY sequence,pid
        """, (device_id, trip_id, samples[0]["sequence"], samples[-1]["sequence"]))
        by_seq = {sample["sequence"]: sample for sample in samples}
        for sample in samples:
            sample["metrics"] = {}
        for metric in metrics:
            by_seq[metric["sequence"]]["metrics"][metric["pid"]] = (
                metric["numeric_value"] if metric["numeric_value"] is not None else metric["text_value"])
    return samples


@mcp.tool(annotations=READ_ONLY)
def raw_frames(device_id: str, trip_id: str, start_sequence: int = 0,
               limit: int = 30) -> dict:
    """Original PID field order and repeated values for up to 50 complete frames."""
    device_id, trip_id = ident(device_id), ident(trip_id)
    limit = max(1, min(int(limit), 50))
    start_sequence = max(0, int(start_sequence))
    with db(HISTORY) as conn:
        frames = rows(conn.execute("""
            SELECT sequence,device_monotonic_ms,capture_utc_ms,collector_received_ms,
                   timestamp_quality,time_basis,latitude,longitude,
                   gps_speed_kph,gps_hdop,acceleration_x_g,acceleration_y_g,acceleration_z_g
            FROM sample WHERE device_id=? AND trip_id=? AND sequence>=?
            ORDER BY sequence LIMIT ?
        """, (device_id, trip_id, start_sequence, limit)))
        if not frames:
            return {"frames": [], "next_sequence": None}
        by_sequence = {frame["sequence"]: frame for frame in frames}
        for frame in frames:
            frame["fields"] = []
        for field in conn.execute("""
            SELECT sequence,ordinal,pid,numeric_value,text_value
            FROM sample_field WHERE device_id=? AND trip_id=?
              AND sequence BETWEEN ? AND ? ORDER BY sequence,ordinal
        """, (device_id, trip_id, frames[0]["sequence"], frames[-1]["sequence"])):
            by_sequence[field["sequence"]]["fields"].append(dict(field))
    return {"frames": frames, "next_sequence": frames[-1]["sequence"] + 1,
            "provenance": "source-order fields from the raw collector archive; duplicate PIDs retained"}


@mcp.tool(annotations=READ_ONLY)
def sensor_waveforms(device_id: str, trip_id: str, start_sequence: int = 0,
                     limit: int = 40) -> dict:
    """Raw voltage and motion acquisitions with timing and loss evidence; up to 100 frames.

    Start before an event sequence to inspect its lead-in. Paginate with
    next_sequence. Motion includes gravity; voltage is an uncalibrated device
    input measurement. Missing waveform coverage is not absence of a fault.
    """
    device_id, trip_id = ident(device_id), ident(trip_id)
    with db(HISTORY) as conn:
        return waveform_window(conn, device_id, trip_id, max(0, int(start_sequence)),
                               max(1, min(int(limit), 100)))


@mcp.tool(annotations=READ_ONLY)
def dtc_history(device_id: str, limit: int = 30) -> list[dict]:
    """Confirmed stored, pending and permanent code sightings with scan context."""
    device_id = ident(device_id)
    limit = max(1, min(int(limit), 100))
    with db(HISTORY) as conn:
        trip_ids = [r[0] for r in conn.execute("""
            SELECT DISTINCT trip_id FROM sample_metric WHERE device_id=?
              AND pid IN ('0x310','0x330','0x350')
            ORDER BY trip_id DESC LIMIT ?
        """, (device_id, min(100, limit * 3)))]
        results = []
        for trip_id in trip_ids:
            info = diagnostic_evidence(conn, device_id, trip_id)
            if info["confirmed_codes"]:
                results.append({"trip_id": trip_id, **info})
            if len(results) >= limit:
                break
    return results


@mcp.tool(annotations=READ_ONLY)
def compare_baseline(device_id: str, trip_id: str) -> dict:
    """Compare current readings with earlier sealed same-device operation contexts."""
    device_id, trip_id = ident(device_id), ident(trip_id)
    with db(HISTORY) as conn:
        return {"trip_id": trip_id,
                "prior_driving_trips": previous_trip_baseline(conn, device_id, trip_id),
                "matched_maf": matched_maf_baseline(conn, device_id, trip_id),
                "contextual_condition": contextual_baselines(conn, device_id, trip_id),
                "limitations": "Same device only; route, engine identity, EGR, boost and temperature may differ. MAF alone does not diagnose a filter."}


@mcp.tool(annotations=READ_ONLY)
def data_quality(device_id: str, trip_id: str) -> dict:
    """Collection provenance and missingness, kept separate from vehicle faults."""
    device_id, trip_id = ident(device_id), ident(trip_id)
    with db(HISTORY) as conn:
        trip = conn.execute("SELECT * FROM trip WHERE device_id=? AND trip_id=?",
                            (device_id, trip_id)).fetchone()
        if trip is None:
            raise ValueError("trip not found")
        coverage = rows(conn.execute("""
            SELECT m.pid,c.name,c.unit,COUNT(*) AS readings
            FROM sample_metric m LEFT JOIN metric_catalogue c ON c.pid=m.pid
            WHERE m.device_id=? AND m.trip_id=? GROUP BY m.pid ORDER BY m.pid
        """, (device_id, trip_id)))
        return {"trip_id": trip_id, "timestamp_quality": trip["timestamp_quality"],
                "time_basis": trip["time_basis"], "sample_count": trip["sample_count"],
                "gap_count": trip["gap_count"], "metric_coverage": coverage,
                "acquisition": acquisition_quality(conn, device_id, trip_id),
                "waveform": waveform_trip_summary(conn, device_id, trip_id),
                "interpretation": "Capture gaps, GPS drops and absent PIDs describe collection, not mechanical faults."}


@mcp.tool(annotations=READ_ONLY)
def analysis_report(device_id: str, trip_id: str) -> dict:
    """Previously stored agent report and exact model receipt, if available."""
    device_id, trip_id = ident(device_id), ident(trip_id)
    if not REPORTS.exists():
        return {"status": "unavailable"}
    with db(REPORTS) as conn:
        row = conn.execute("""
            SELECT report_json,requested_model,returned_model,analyzed_at_ms,
                   notified_at_ms,attempts,last_error FROM trip_report
            WHERE device_id=? AND trip_id=?
        """, (device_id, trip_id)).fetchone()
    if row is None:
        return {"status": "not_analyzed"}
    result = dict(row)
    result["report"] = json.loads(result.pop("report_json")) if result["report_json"] else None
    return result


class BearerGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            supplied = headers.get(b"authorization", b"")
            expected = b"Bearer " + TOKEN.encode()
            if not TOKEN or not hmac.compare_digest(supplied, expected):
                body = b"Unauthorized"
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


if __name__ == "__main__":
    if len(TOKEN) < 32:
        raise SystemExit("FREEMATICS_MCP_TOKEN must be set to a random 32+ character secret")
    import uvicorn
    uvicorn.run(BearerGuard(mcp.streamable_http_app()), host="0.0.0.0", port=8018,
                log_level="info", proxy_headers=False)
