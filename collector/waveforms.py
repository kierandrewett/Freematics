"""Decode versioned high-rate voltage and motion fields from raw archive order.

The ``sample_field`` table is the authoritative waveform source.  The normal
metric projection keeps only the final occurrence of a PID in a frame.
"""

from __future__ import annotations

import math
import sqlite3
import heapq
import hashlib
from collections import deque
from collections.abc import Iterable
from typing import Any


WAVEFORM_FORMAT = 1
UINT32_MAX = 0xFFFFFFFF
_PIDS = {"0x0A0", "0x0A1", "0x0A2", "0x0A3", "0x0A4", "0x0A5"}


def _raw(field: sqlite3.Row) -> str:
    value = field["text_value"]
    if value is not None:
        return str(value)
    number = field["numeric_value"]
    if number is None:
        return ""
    return str(int(number)) if float(number).is_integer() else str(number)


def _uint32(raw: str) -> int | None:
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    return value if 0 <= value <= UINT32_MAX else None


def _vector(raw: str, maximum_absolute: float) -> dict[str, float] | None:
    parts = raw.split(";")
    if len(parts) != 3:
        return None
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None
    if not all(math.isfinite(value) and abs(value) <= maximum_absolute for value in values):
        return None
    return dict(zip(("x", "y", "z"), values))


def _offset_ms(acquisition_ms: int, parent_ms: int) -> int:
    """Return the signed nearby difference between two uint32 device ticks."""

    return ((acquisition_ms - parent_ms + (1 << 31)) & UINT32_MAX) - (1 << 31)


def _point_time(parent: sqlite3.Row, acquisition_ms: int) -> dict[str, int | None]:
    offset = _offset_ms(acquisition_ms, int(parent["device_monotonic_ms"]))
    capture = parent["capture_utc_ms"]
    timeline = parent["timeline_ms"]
    return {
        "device_monotonic_ms": acquisition_ms,
        "offset_ms": offset,
        "capture_utc_ms": int(capture) + offset if capture is not None else None,
        "timeline_ms": int(timeline) + offset if timeline is not None else None,
        "acquisition_offset_quality": (
            "delayed_backlog" if offset < -3000 else "future" if offset > 250 else "in_window"
        ),
    }


def _issue(issues: list[dict[str, Any]], sequence: int, ordinal: int, code: str, detail: str) -> None:
    issues.append({"sequence": sequence, "ordinal": ordinal, "code": code, "detail": detail})


def _interval_stats(points: Iterable[dict[str, Any]]) -> dict[str, int | float | None]:
    values = list(points)
    intervals: list[int] = []
    resets = 0
    for previous, current in zip(values, values[1:]):
        delta = _offset_ms(int(current["device_monotonic_ms"]), int(previous["device_monotonic_ms"]))
        if delta < 0:
            resets += 1
        else:
            intervals.append(delta)
    if not intervals:
        return {"count": 0, "minimum": None, "maximum": None, "average": None, "non_monotonic": resets}
    return {
        "count": len(intervals), "minimum": min(intervals), "maximum": max(intervals),
        "average": sum(intervals) / len(intervals), "non_monotonic": resets,
    }


def _offset_quality(points: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {"in_window": 0, "delayed_backlog": 0, "future": 0}
    for point in points:
        counts[point["acquisition_offset_quality"]] += 1
    return counts


def _sensor_coverage(frames: int, versioned: int, readings: int, frames_with_readings: int) -> dict[str, int | str]:
    without = frames - versioned
    if readings == 0:
        state = "missing"
    elif without or frames_with_readings < versioned:
        state = "partial"
    else:
        state = "observed"
    return {"state": state, "frames": frames, "frames_with_format": versioned,
            "frames_without_format": without, "readings": readings,
            "frames_with_readings": frames_with_readings,
            "empty_format_frames": max(0, versioned - frames_with_readings)}


def _coverage(frames: int, versioned: int, voltage: list[dict[str, Any]],
              motion: list[dict[str, Any]]) -> dict[str, Any]:
    voltage_frames = len({point["sequence"] for point in voltage})
    motion_frames = len({point["sequence"] for point in motion})
    sensors = {
        "voltage": _sensor_coverage(frames, versioned, len(voltage), voltage_frames),
        "motion": _sensor_coverage(frames, versioned, len(motion), motion_frames),
    }
    if not voltage and not motion:
        state = "missing"
    elif sensors["voltage"]["state"] == "observed" and sensors["motion"]["state"] == "observed":
        state = "observed"
    else:
        state = "partial"
    return {"state": state, "frames": frames, "frames_with_format": versioned,
            "frames_without_format": frames - versioned, "sensors": sensors}


def waveform_window(conn: sqlite3.Connection, device_id: str, trip_id: str,
                    start_sequence: int = 0, limit: int = 100) -> dict[str, Any]:
    """Return at most ``limit`` parent frames of decoded waveform observations.

    Each point keeps its uint32 device clock and timestamps aligned to its own
    acquisition, rather than to the enclosing frame start.  ``issues`` reports
    malformed source data.  It is evidence of data quality, not vehicle state.
    """

    start_sequence = max(0, int(start_sequence))
    limit = max(1, min(int(limit), 200))
    conn.row_factory = sqlite3.Row
    parents = list(conn.execute(
        """SELECT sequence,device_monotonic_ms,capture_utc_ms,timeline_ms,time_basis,timestamp_quality
           FROM sample WHERE device_id=? AND trip_id=? AND sequence>=?
           ORDER BY sequence LIMIT ?""", (device_id, trip_id, start_sequence, limit)))
    if not parents:
        return {"format_version": WAVEFORM_FORMAT, "frames": [], "voltage": [], "motion": [],
                "loss_reports": [], "issues": [], "quality": {"coverage": _coverage(0, 0, [], [])},
                "next_sequence": None}
    fields_by_sequence: dict[int, list[sqlite3.Row]] = {row["sequence"]: [] for row in parents}
    for field in conn.execute(
        """SELECT sequence,ordinal,pid,numeric_value,text_value FROM sample_field
           WHERE device_id=? AND trip_id=? AND sequence BETWEEN ? AND ?
           ORDER BY sequence,ordinal""",
        (device_id, trip_id, parents[0]["sequence"], parents[-1]["sequence"]),
    ):
        fields_by_sequence[field["sequence"]].append(field)

    voltage: list[dict[str, Any]] = []
    motion: list[dict[str, Any]] = []
    losses: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    versioned = 0
    frame_rows: list[dict[str, Any]] = []
    for parent in parents:
        sequence = int(parent["sequence"])
        all_fields = fields_by_sequence[sequence]
        fields = [field for field in all_fields if field["pid"] in _PIDS]
        format_values = [_uint32(_raw(field)) for field in fields if field["pid"] == "0x0A5"]
        format_ok = format_values == [WAVEFORM_FORMAT]
        if format_ok:
            versioned += 1
        elif format_values:
            _issue(issues, sequence, next(field["ordinal"] for field in fields if field["pid"] == "0x0A5"),
                   "unsupported_format", "Waveform fields require exactly one format value of 1.")
        contains_waveform = any(field["pid"] != "0x0A5" for field in fields)
        ordinal_index = {field["ordinal"]: index for index, field in enumerate(all_fields)}
        fingerprint = hashlib.sha256(repr([(field["pid"], _raw(field)) for field in all_fields]).encode()).hexdigest()
        frame_rows.append({"sequence": sequence, "device_monotonic_ms": parent["device_monotonic_ms"],
                           "format": WAVEFORM_FORMAT if format_ok else None,
                           "waveform_fields": contains_waveform, "frame_fingerprint": fingerprint})
        if not format_ok:
            if contains_waveform and not format_values:
                _issue(issues, sequence, fields[0]["ordinal"], "missing_format", "Waveform fields have no format declaration.")
            continue

        for index, field in enumerate(fields):
            pid = field["pid"]
            raw = _raw(field)
            if pid == "0x0A0":
                parts = raw.split(";")
                if len(parts) != 2:
                    _issue(issues, sequence, field["ordinal"], "invalid_voltage", "Voltage requires timestamp;centivolts.")
                    continue
                acquisition, centivolts = (_uint32(part) for part in parts)
                if acquisition is None or centivolts is None or centivolts > 65535:
                    _issue(issues, sequence, field["ordinal"], "invalid_voltage", "Voltage timestamp or centivolts is outside its unsigned range.")
                    continue
                voltage.append({"sequence": sequence, "ordinal": field["ordinal"], **_point_time(parent, acquisition),
                                "centivolts": centivolts, "volts": centivolts / 100,
                                "time_basis": parent["time_basis"], "timestamp_quality": parent["timestamp_quality"]})
            elif pid == "0x0A1":
                all_index = ordinal_index[field["ordinal"]]
                group = all_fields[all_index:all_index + 3]
                if len(group) != 3 or [item["pid"] for item in group] != ["0x0A1", "0x0A2", "0x0A3"]:
                    _issue(issues, sequence, field["ordinal"], "motion_group_not_adjacent",
                           "Motion requires adjacent 0x0A1, 0x0A2 and 0x0A3 fields.")
                    continue
                acquisition = _uint32(raw)
                acceleration, gyro = _vector(_raw(group[1]), 64), _vector(_raw(group[2]), 4000)
                if acquisition is None:
                    _issue(issues, sequence, field["ordinal"], "invalid_motion_timestamp", "Motion timestamp is outside the uint32 range.")
                if acceleration is None or gyro is None:
                    _issue(issues, sequence, field["ordinal"], "invalid_motion_vector", "Motion vectors require three finite values within firmware encoding limits.")
                if acquisition is not None and acceleration is not None and gyro is not None:
                    motion.append({"sequence": sequence, "ordinal": field["ordinal"], **_point_time(parent, acquisition),
                                   "acceleration_g": acceleration, "angular_rate_degrees_per_second": gyro,
                                   "time_basis": parent["time_basis"], "timestamp_quality": parent["timestamp_quality"]})
            elif pid == "0x0A2":
                all_index = ordinal_index[field["ordinal"]]
                if all_index == 0 or all_fields[all_index - 1]["pid"] != "0x0A1":
                    _issue(issues, sequence, field["ordinal"], "incomplete_motion_group",
                           "Acceleration has no adjacent waveform motion timestamp.")
            elif pid == "0x0A3":
                all_index = ordinal_index[field["ordinal"]]
                if all_index < 2 or [item["pid"] for item in all_fields[all_index - 2:all_index]] != ["0x0A1", "0x0A2"]:
                    _issue(issues, sequence, field["ordinal"], "incomplete_motion_group",
                           "Angular rate has no adjacent waveform timestamp and acceleration fields.")
            elif pid == "0x0A4":
                values = [_uint32(part) for part in raw.split(";")]
                if len(values) != 4 or any(value is None for value in values):
                    _issue(issues, sequence, field["ordinal"], "invalid_loss_counter", "Losses require four uint32 counters.")
                    continue
                losses.append({"sequence": sequence, "ordinal": field["ordinal"], "losses": dict(zip(
                    ("voltage_overflow", "motion_overflow", "invalid_voltage", "invalid_motion"), values))})

    quality = {
        "coverage": _coverage(len(parents), versioned, voltage, motion),
        "voltage_interval_ms": _interval_stats(voltage),
        "motion_interval_ms": _interval_stats(motion),
        "acquisition_offset_quality": {"voltage": _offset_quality(voltage), "motion": _offset_quality(motion)},
        "malformed_groups": len(issues),
        "loss_reports": len(losses),
        "loss_counter_resets": _loss_resets(losses),
        "loss_counter_increase": _loss_increase(losses),
        "loss_counter_provenance": _loss_provenance(losses),
    }
    return {"format_version": WAVEFORM_FORMAT, "frames": frame_rows, "voltage": voltage, "motion": motion,
            "loss_reports": losses, "issues": issues, "quality": quality,
            "next_sequence": parents[-1]["sequence"] + 1 if len(parents) == limit else None,
            "provenance": "ordered sample_field waveform format 1; timestamps align each acquisition to its parent frame"}


def _loss_resets(reports: list[dict[str, Any]]) -> int:
    previous: dict[str, int] | None = None
    resets = 0
    for report in reports:
        current = report["losses"]
        if previous is not None and any(current[key] < previous[key] for key in current):
            resets += 1
        previous = current
    return resets


def _loss_increase(reports: list[dict[str, Any]]) -> dict[str, int] | None:
    """Report observed counter increase; a reset makes the interval uncertain."""

    if len(reports) < 2:
        return None
    if _loss_resets(reports):
        return None
    first, last = reports[0]["losses"], reports[-1]["losses"]
    return {key: last[key] - first[key] for key in first}


def _loss_provenance(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        return {"state": "missing", "initial_counter_before_window": "unknown", "observed_delta": None}
    return {
        "state": "reset" if _loss_resets(reports) else "cumulative",
        "initial_counter_before_window": "unknown",
        "first_sequence": reports[0]["sequence"],
        "last_sequence": reports[-1]["sequence"],
        "observed_delta": _loss_increase(reports),
    }


def _cadence_add(state: dict[str, Any], point: dict[str, Any]) -> None:
    previous = state.get("previous")
    if previous is not None:
        interval = _offset_ms(int(point["device_monotonic_ms"]), int(previous))
        if interval < 0:
            state["non_monotonic"] += 1
        else:
            state["count"] += 1
            state["sum"] += interval
            state["minimum"] = interval if state["minimum"] is None else min(state["minimum"], interval)
            state["maximum"] = interval if state["maximum"] is None else max(state["maximum"], interval)
            if interval > 50:
                state["gaps_over_50ms"] += 1
    state["previous"] = point["device_monotonic_ms"]


def _cadence_result(state: dict[str, Any]) -> dict[str, Any]:
    count = state["count"]
    return {"interval_count": count, "minimum_ms": state["minimum"], "maximum_ms": state["maximum"],
            "average_ms": state["sum"] / count if count else None, "gaps_over_50ms": state["gaps_over_50ms"],
            "non_monotonic": state["non_monotonic"], "gap_threshold_ms": 50}


def _new_cadence() -> dict[str, Any]:
    return {"previous": None, "count": 0, "sum": 0, "minimum": None, "maximum": None,
            "gaps_over_50ms": 0, "non_monotonic": 0}


def _motion_frame_metric(sequence: int, points: list[dict[str, Any]]) -> dict[str, Any] | None:
    matched = [point for point in points if point["acquisition_offset_quality"] == "in_window"]
    if len(matched) < 2:
        return None
    vectors = [point["acceleration_g"] for point in matched]
    means = {axis: sum(vector[axis] for vector in vectors) / len(vectors) for axis in ("x", "y", "z")}
    deviations = [math.sqrt(sum((vector[axis] - means[axis]) ** 2 for axis in means)) for vector in vectors]
    spans = {axis: max(vector[axis] for vector in vectors) - min(vector[axis] for vector in vectors) for axis in means}
    return {"sequence": sequence, "motion_readings": len(points), "time_matched_motion_readings": len(matched),
            "detrended_rms_g": math.sqrt(sum(value * value for value in deviations) / len(deviations)),
            "acceleration_axis_span_g": math.sqrt(sum(value * value for value in spans.values()))}


def waveform_trip_summary(conn: sqlite3.Connection, device_id: str, trip_id: str) -> dict[str, Any]:
    """Summarise one trip in bounded pages; candidates are observations only."""

    conn.row_factory = sqlite3.Row
    frame_count_row = conn.execute(
        "SELECT COUNT(*) AS count FROM sample WHERE device_id=? AND trip_id=?", (device_id, trip_id)
    ).fetchone()
    frame_count = int(frame_count_row["count"])
    has_format = conn.execute(
        "SELECT 1 FROM sample_metric WHERE device_id=? AND trip_id=? AND pid='0x0A5' LIMIT 1",
        (device_id, trip_id),
    ).fetchone()
    if has_format is None:
        return _unavailable_trip_summary(frame_count)
    next_sequence: int | None = 0
    format_frames = voltage_frames = motion_frames = 0
    voltage_count = motion_count = issue_count = 0
    voltage_min: float | None = None
    voltage_max: float | None = None
    voltage_cadence, motion_cadence = _new_cadence(), _new_cadence()
    candidate_heap: list[tuple[float, int, dict[str, Any]]] = []
    recent_frames: deque[tuple[int, str]] = deque(maxlen=64)
    duplicate_frames = frame_tick_conflicts = epoch_resets = 0
    epoch_watermark: int | None = None
    loss_count = loss_resets = 0
    first_loss: dict[str, Any] | None = None
    last_loss: dict[str, Any] | None = None
    while next_sequence is not None:
        page = waveform_window(conn, device_id, trip_id, next_sequence, 200)
        if not page["frames"]:
            break
        next_sequence = page["next_sequence"]
        duplicate_sequences: set[int] = set()
        for frame in page["frames"]:
            frame_tick = int(frame["device_monotonic_ms"])
            same_frame = any(signature == frame["frame_fingerprint"] for _, signature in recent_frames)
            if same_frame:
                duplicate_frames += 1
                duplicate_sequences.add(frame["sequence"])
                continue
            if epoch_watermark is not None and _offset_ms(frame_tick, epoch_watermark) < 0:
                recent_frames.clear()
                epoch_watermark = None
                epoch_resets += 1
            same_tick = any(tick == frame_tick for tick, _ in recent_frames)
            if same_tick:
                frame_tick_conflicts += 1
            recent_frames.append((frame_tick, frame["frame_fingerprint"]))
            epoch_watermark = frame_tick
        frames = [frame for frame in page["frames"] if frame["sequence"] not in duplicate_sequences]
        voltage_points = [point for point in page["voltage"] if point["sequence"] not in duplicate_sequences]
        motion_points = [point for point in page["motion"] if point["sequence"] not in duplicate_sequences]
        loss_points = [point for point in page["loss_reports"] if point["sequence"] not in duplicate_sequences]
        format_frames += sum(frame["format"] == WAVEFORM_FORMAT for frame in frames)
        issue_count += sum(issue["sequence"] not in duplicate_sequences for issue in page["issues"])
        voltage_sequences = {point["sequence"] for point in voltage_points}
        motion_sequences: dict[int, list[dict[str, Any]]] = {}
        voltage_frames += len(voltage_sequences)
        motion_frames += len({point["sequence"] for point in motion_points})
        for point in voltage_points:
            voltage_count += 1
            voltage_min = point["volts"] if voltage_min is None else min(voltage_min, point["volts"])
            voltage_max = point["volts"] if voltage_max is None else max(voltage_max, point["volts"])
            _cadence_add(voltage_cadence, point)
        for point in motion_points:
            motion_count += 1
            motion_sequences.setdefault(point["sequence"], []).append(point)
            _cadence_add(motion_cadence, point)
        for sequence, points in motion_sequences.items():
            metric = _motion_frame_metric(sequence, points)
            if metric is None:
                continue
            score = max(metric["detrended_rms_g"], metric["acceleration_axis_span_g"])
            entry = (score, sequence, metric)
            if len(candidate_heap) < 20:
                heapq.heappush(candidate_heap, entry)
            elif entry > candidate_heap[0]:
                heapq.heapreplace(candidate_heap, entry)
        for report in loss_points:
            loss_count += 1
            if first_loss is None:
                first_loss = report
            elif last_loss is not None and any(
                report["losses"][key] < last_loss["losses"][key] for key in report["losses"]
            ):
                loss_resets += 1
            last_loss = report
    unique_frame_count = frame_count - duplicate_frames
    voltage_coverage = _sensor_coverage(unique_frame_count, format_frames, voltage_count, voltage_frames)
    motion_coverage = _sensor_coverage(unique_frame_count, format_frames, motion_count, motion_frames)
    available = voltage_count > 0 or motion_count > 0
    return {
        "availability": "available" if available else "unavailable",
        "limitations": "Candidates are waveform observations, not a diagnosis. Trips without format-1 acquisitions cannot support waveform comparison.",
        "frames": {"total": frame_count, "after_replay_deduplication": unique_frame_count,
                   "with_format": format_frames},
        "coverage": {"voltage": voltage_coverage, "motion": motion_coverage},
        "voltage": {"readings": voltage_count, "minimum_volts": voltage_min, "maximum_volts": voltage_max,
                    "cadence": _cadence_result(voltage_cadence)},
        "motion": {"readings": motion_count, "cadence": _cadence_result(motion_cadence),
                   "extreme_candidates": [entry[2] for entry in sorted(candidate_heap, reverse=True)]},
        "data_quality": {**_trip_loss_quality(issue_count, loss_count, loss_resets, first_loss, last_loss),
                         "duplicate_frames": duplicate_frames, "frame_tick_conflicts": frame_tick_conflicts,
                         "device_epoch_resets": epoch_resets,
                         "deduplication": "Exact repeated source frames within the latest 64 unique frames in one ordered device epoch are excluded from summary calculations only. An identical first frame after an unobserved reset is indistinguishable from a retry."},
    }


def _unavailable_trip_summary(frame_count: int) -> dict[str, Any]:
    coverage = _sensor_coverage(frame_count, 0, 0, 0)
    return {
        "availability": "unavailable",
        "limitations": "This trip has no waveform format field. It cannot support waveform comparison.",
        "frames": {"total": frame_count, "with_format": 0},
        "coverage": {"voltage": coverage, "motion": coverage.copy()},
        "voltage": {"readings": 0, "minimum_volts": None, "maximum_volts": None, "cadence": _cadence_result(_new_cadence())},
        "motion": {"readings": 0, "cadence": _cadence_result(_new_cadence()), "extreme_candidates": []},
        "data_quality": _trip_loss_quality(0, 0, 0, None, None),
    }


def _trip_loss_quality(issue_count: int, count: int, resets: int, first: dict[str, Any] | None,
                       last: dict[str, Any] | None) -> dict[str, Any]:
    reports = [] if first is None else [first] if last is first else [first, last]
    return {"malformed_fields": issue_count, "loss_reports": count,
            "loss_counter_resets": resets, "loss_counter_increase": None if resets else _loss_increase(reports),
            "loss_counter_provenance": {
                "state": "missing" if first is None else "reset" if resets else "cumulative",
                "initial_counter_before_trip": "unknown",
                "first_sequence": first["sequence"] if first else None,
                "last_sequence": last["sequence"] if last else None,
                "observed_delta": None if resets else _loss_increase(reports),
            }}


__all__ = ["WAVEFORM_FORMAT", "waveform_trip_summary", "waveform_window"]
