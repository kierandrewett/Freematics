"""Contextual, evidence-only comparison of sealed Freematics trips.

The module does not establish a healthy value or diagnose a component. It only
compares fresh observations from a trip with the same operating context in
earlier sealed trips from the same device.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict


MIN_REFERENCE_TRIPS = 5
MIN_CURRENT_SAMPLES = 3
MIN_METRIC_READINGS = 3
MAX_REFERENCE_TRIPS = 30
WARM_COOLANT_C = 80.0

# These are acceptance limits for the age recorded by the device. They are
# deliberately metric-specific. They do not claim that every auxiliary PID was
# acquired at one second in older archives.
METRICS = {
    "engine_rpm": ("0x10C", 1_500, 1.0),
    "speed_kph": ("0x10D", 1_500, 1.0),
    "engine_load_percent": ("0x104", 5_000, 1.0),
    "coolant_celsius": ("0x105", 30_000, 1.0),
    "mass_air_flow_gps": ("0x110", 5_000, 1.0),
    "short_fuel_trim_percent": ("0x106", 5_000, 1.0),
    "long_fuel_trim_percent": ("0x107", 5_000, 1.0),
    "supply_voltage_volts": ("0x024", 5_000, 0.01),
    "motion_magnitude_g": (None, 1_500, 1.0),
}


def _age_pid(pid: str) -> str:
    if pid == "0x024":
        # Supply voltage is a device sensor reading. It uses the dedicated
        # voltage-age PID, not the Mode 01 0x4xx age range.
        return "0x094"
    return f"0x4{int(pid[-2:], 16):02X}"


def _finite(value) -> float | None:
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _fresh(value, age, limit_ms: int, scale: float = 1.0) -> tuple[float | None, str]:
    value = _finite(value)
    age = _finite(age)
    if value is None:
        return None, "invalid"
    if age is None:
        return None, "age_unverified"
    if age < 0 or age > limit_ms:
        return None, "stale"
    return value * scale, "fresh"


def _motion(row: sqlite3.Row) -> tuple[float | None, str]:
    axes = [_finite(row[name]) for name in ("acceleration_x_g", "acceleration_y_g", "acceleration_z_g")]
    if any(value is None for value in axes):
        return None, "invalid"
    return _fresh(math.sqrt(sum(value * value for value in axes)), row["mems_age"], 1_500)


def _trip_samples(history: sqlite3.Connection, device: str, trip_id: str) -> list[dict]:
    """Load each source sample once; never interpolate or create synthetic rows."""
    pids = [pid for pid, _age, _scale in METRICS.values() if pid]
    age_pids = [_age_pid(pid) for pid in pids]
    requested_pids = pids + age_pids + ["0x095"]
    placeholders = ",".join("?" for _ in requested_pids)
    rows = history.execute(f"""
        SELECT s.sequence,s.device_monotonic_ms,s.acceleration_x_g,s.acceleration_y_g,
               s.acceleration_z_g,
               MAX(CASE WHEN m.pid='0x095' THEN m.numeric_value END) AS mems_age,
               {','.join(f"MAX(CASE WHEN m.pid='{pid}' THEN m.numeric_value END) AS m_{pid[-3:]}" for pid in pids)},
               {','.join(f"MAX(CASE WHEN m.pid='{_age_pid(pid)}' THEN m.numeric_value END) AS a_{pid[-3:]}" for pid in pids)}
        FROM sample AS s LEFT JOIN sample_metric AS m
          ON m.device_id=s.device_id AND m.trip_id=s.trip_id AND m.sequence=s.sequence
             AND m.pid IN ({placeholders})
        WHERE s.device_id=? AND s.trip_id=?
        GROUP BY s.sequence ORDER BY s.sequence
    """, (*requested_pids, device, trip_id))
    output = []
    source_ticks: dict[str, int] = {}
    previous_raw_tick = None
    logical_tick = 0
    epoch = 0
    for row in rows:
        raw_tick = int(row["device_monotonic_ms"])
        if previous_raw_tick is None:
            logical_tick = raw_tick
        elif raw_tick >= previous_raw_tick:
            logical_tick += raw_tick - previous_raw_tick
        elif previous_raw_tick - raw_tick > 0x80000000:
            # uint32 millis wrap. Keep the same epoch and an unwrapped time.
            logical_tick += (raw_tick - previous_raw_tick) & 0xFFFFFFFF
        else:
            # A small backwards jump is a device reset, not a clock wrap.
            epoch += 1
            source_ticks.clear()
            logical_tick = raw_tick
        previous_raw_tick = raw_tick
        values, quality, ages = {}, {}, {}
        acquisitions = {}
        for name, (pid, max_age, scale) in METRICS.items():
            if pid:
                age_value = row[f"a_{pid[-3:]}"]
                value, status = _fresh(row[f"m_{pid[-3:]}"], age_value, max_age, scale)
            else:
                age_value = row["mems_age"]
                value, status = _motion(row)
            # Age converts a repeated snapshot into the acquisition time. A
            # held value has one acquisition timestamp, so count it once.
            if value is not None:
                source_tick = int((raw_tick - float(age_value)) % 0x100000000)
                acquisition = (epoch, source_tick)
                if source_ticks.get(name) == acquisition:
                    status = "held_duplicate"
                else:
                    source_ticks[name] = acquisition
                acquisitions[name] = acquisition
            values[name], quality[name], ages[name] = value, status, _finite(age_value)
        output.append({"tick": logical_tick, "epoch": epoch, "values": values, "quality": quality,
                       "ages": ages, "acquisitions": acquisitions})
    return output


def _context(sample: dict) -> str | None:
    values = sample["values"]
    rpm, speed, coolant, load = (values[name] for name in
                                 ("engine_rpm", "speed_kph", "coolant_celsius", "engine_load_percent"))
    if None in (rpm, speed, coolant):
        return None
    # These are analysis boundaries, not manufacturer specifications.
    if speed <= 1 and 400 <= rpm <= 1500:
        return "cold_idle" if coolant < WARM_COOLANT_C else "warm_idle" if coolant >= WARM_COOLANT_C else None
    if load is not None and speed >= 5 and rpm >= 400:
        return "cold_driving" if coolant < WARM_COOLANT_C else "warm_driving" if coolant >= WARM_COOLANT_C else None
    return None


def _driving_bin(sample: dict) -> str | None:
    values = sample["values"]
    if any(values[name] is None for name in ("engine_rpm", "engine_load_percent", "speed_kph")):
        return None
    rpm = int(round(values["engine_rpm"] / 250) * 250)
    load = int(round(values["engine_load_percent"] / 10) * 10)
    speed = int(round(values["speed_kph"] / 10) * 10)
    return f"rpm_{rpm}_load_{load}_speed_{speed}"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * fraction)]


def _spread_windows(samples: list[dict]) -> list[float]:
    """Measure RPM variation in observed five-second windows without filling gaps."""
    buckets: dict[int, list[float]] = defaultdict(list)
    for sample in samples:
        value = sample["values"]["engine_rpm"]
        if value is not None and sample["quality"]["engine_rpm"] == "fresh":
            buckets[(sample["epoch"], int(sample["tick"]) // 5_000)].append(value)
    return [max(values) - min(values) for values in buckets.values() if len(values) >= 3]


def _summary(samples: list[dict], include_rpm_spread: bool = False) -> tuple[dict, dict]:
    metrics, coverage = {}, {}
    for name in METRICS:
        accepted = [sample["values"][name] for sample in samples if sample["quality"][name] == "fresh"]
        accepted_ages = [sample["ages"][name] for sample in samples if sample["quality"][name] == "fresh"]
        known_ages = [sample["ages"][name] for sample in samples if sample["ages"][name] is not None]
        acquired = [sample["acquisitions"].get(name) for sample in samples
                    if sample["quality"][name] == "fresh"]
        intervals = [(later[1] - earlier[1]) & 0xFFFFFFFF for earlier, later in zip(acquired, acquired[1:])
                     if earlier is not None and later is not None and earlier[0] == later[0]]
        frame_intervals = [later["tick"] - earlier["tick"] for earlier, later in zip(samples, samples[1:])
                           if earlier["epoch"] == later["epoch"]]
        epoch_changed = any(earlier["epoch"] != later["epoch"] for earlier, later in zip(samples, samples[1:]))
        rejected = defaultdict(int)
        for sample in samples:
            if sample["quality"][name] != "fresh":
                rejected[sample["quality"][name]] += 1
        quality_failure = rejected["stale"] + rejected["age_unverified"] + rejected["invalid"]
        distinct_acquisitions = len(acquired)
        coverage[name] = {"fresh": len(accepted), "stale_or_unverified": rejected["stale"] + rejected["age_unverified"],
                          "held_duplicate": rejected["held_duplicate"],
                          "invalid": rejected["invalid"], "max_accepted_age_ms": METRICS[name][1],
                          "max_observed_accepted_age_ms": round(max(accepted_ages), 1) if accepted_ages else None,
                          "max_observed_age_ms": round(max(known_ages), 1) if known_ages else None,
                          "max_successive_acquisition_gap_ms": max(intervals) if intervals else None,
                          "observed_successive_acquisition": "met" if intervals and max(intervals) <= 1_000
                          else "not_met" if intervals else "insufficient_observations",
                          "one_second_full_coverage": "met" if distinct_acquisitions >= 2 and intervals
                          and max(intervals) <= 1_000 and not quality_failure
                          and known_ages and max(known_ages) <= 1_000 and not epoch_changed
                          and (not frame_intervals or max(frame_intervals) <= 1_000)
                          else "not_met" if distinct_acquisitions >= 2 and (quality_failure or intervals)
                          else "insufficient_observations"}
        if len(accepted) >= MIN_METRIC_READINGS:
            metrics[name] = {"readings": len(accepted), "median": round(statistics.median(accepted), 3),
                             "p10": round(_percentile(accepted, .10), 3),
                             "p90": round(_percentile(accepted, .90), 3)}
    if include_rpm_spread:
        spreads = _spread_windows(samples)
        if len(spreads) >= MIN_METRIC_READINGS:
            metrics["engine_rpm_window_spread"] = {"windows": len(spreads), "median": round(statistics.median(spreads), 3),
                                                    "p90": round(_percentile(spreads, .90), 3), "unit": "rpm"}
    return metrics, coverage


def _reference_summary(per_trip: list[dict]) -> dict:
    output = {}
    names = {name for trip in per_trip for name in trip}
    for name in names:
        medians = [trip[name]["median"] for trip in per_trip if name in trip]
        if len(medians) >= MIN_REFERENCE_TRIPS:
            output[name] = {"trips": len(medians), "median_of_trip_medians": round(statistics.median(medians), 3),
                            "p10_of_trip_medians": round(_percentile(medians, .10), 3),
                            "p90_of_trip_medians": round(_percentile(medians, .90), 3)}
    return output


def _comparison(current: dict, reference: dict) -> dict:
    output = {}
    for name, current_stats in current.items():
        baseline = reference.get(name)
        if not baseline:
            continue
        delta = current_stats["median"] - baseline["median_of_trip_medians"]
        output[name] = {"current": current_stats, "reference": baseline,
                        "delta_from_reference_median": round(delta, 3),
                        "outside_reference_trip_median_band": bool(
                            current_stats["median"] < baseline["p10_of_trip_medians"]
                            or current_stats["median"] > baseline["p90_of_trip_medians"])}
    return output


def _eligible_trips(history: sqlite3.Connection, device: str, trip: sqlite3.Row) -> tuple[list[str], bool]:
    if trip["timestamp_quality"] != "gnss" or trip["timeline_start_ms"] is None:
        return [], False
    rows = history.execute("""
        SELECT t.trip_id FROM trip AS t JOIN ingest_file AS i ON i.archive_path=t.archive_path
        WHERE t.device_id=? AND i.sealed=1 AND i.mutation_detected=0
          AND t.gap_count=0 AND t.sample_count>0 AND t.timestamp_quality='gnss'
          AND t.timeline_start_ms IS NOT NULL AND t.timeline_start_ms < ?
        ORDER BY t.timeline_start_ms DESC LIMIT ?
    """, (device, trip["timeline_start_ms"], MAX_REFERENCE_TRIPS + 1)).fetchall()
    return [row["trip_id"] for row in rows[:MAX_REFERENCE_TRIPS]], len(rows) > MAX_REFERENCE_TRIPS


def _telemetry_health(history: sqlite3.Connection, device: str, trip_id: str) -> dict:
    """Collection facts. These fields are never used as vehicle evidence."""
    samples = history.execute("""
        SELECT sequence,device_monotonic_ms FROM sample WHERE device_id=? AND trip_id=? ORDER BY sequence
    """, (device, trip_id)).fetchall()
    gaps, resets, previous = [], 0, None
    for row in samples:
        tick = int(row["device_monotonic_ms"])
        if previous is not None:
            if tick < previous and previous - tick <= 0x80000000:
                resets += 1
            else:
                gap = (tick - previous) & 0xFFFFFFFF
                if gap > 1_000:
                    gaps.append(gap)
        previous = tick
    counters = {}
    for pid, name in (("0x087", "obd_timeouts"), ("0x08E", "missed_readings")):
        values = [row[0] for row in history.execute("""
            SELECT numeric_value FROM sample_metric WHERE device_id=? AND trip_id=? AND pid=?
              AND numeric_value IS NOT NULL ORDER BY sequence
        """, (device, trip_id, pid))]
        increases = [max(0, int(later - earlier)) for earlier, later in zip(values, values[1:])]
        counters[name] = None if not values else {"first": int(values[0]), "last": int(values[-1]),
                                                   "max": int(max(values)), "positive_increments": sum(increases),
                                                   "decreases_or_resets": sum(later < earlier for earlier, later in zip(values, values[1:]))}
    failures = [row[0] for row in history.execute("""
        SELECT numeric_value FROM sample_metric WHERE device_id=? AND trip_id=? AND pid='0x08A'
          AND numeric_value IS NOT NULL ORDER BY sequence
    """, (device, trip_id))]
    counters["obd_fast_failures"] = None if not failures else {
        "kind": "consecutive_failure_gauge", "first": int(failures[0]), "last": int(failures[-1]),
        "consecutive_failure_max": int(max(failures)),
        "observed_recoveries": sum(later < earlier for earlier, later in zip(failures, failures[1:])),
    }
    return {"kind": "collection_health_not_vehicle_diagnosis", "frame_count": len(samples),
            "frame_gaps_over_one_second": len(gaps), "max_frame_gap_ms": max(gaps) if gaps else None,
            "device_tick_resets": resets, "counters": counters}


def acquisition_quality(history: sqlite3.Connection, device: str, trip_id: str,
                        samples: list[dict] | None = None) -> dict:
    """Return data collection quality without comparing vehicle operation."""
    if samples is None:
        samples = _trip_samples(history, device, trip_id)
    _metrics, coverage = _summary(samples)
    return {"acquisition_coverage": coverage,
            "telemetry_health": _telemetry_health(history, device, trip_id)}


def contextual_baselines(history: sqlite3.Connection, device: str, trip_id: str) -> dict:
    """Return context-matched changes versus earlier sealed same-device trips."""
    history.row_factory = sqlite3.Row
    trip = history.execute("SELECT * FROM trip WHERE device_id=? AND trip_id=?", (device, trip_id)).fetchone()
    if trip is None:
        raise ValueError("trip not found")
    candidates, history_truncated = _eligible_trips(history, device, trip)
    current_samples = _trip_samples(history, device, trip_id)
    quality = acquisition_quality(history, device, trip_id, current_samples)
    prior_samples = {prior: _trip_samples(history, device, prior) for prior in candidates}
    contexts = {}
    for context in ("cold_idle", "cold_driving", "warm_idle", "warm_driving"):
        current_context = [sample for sample in current_samples if _context(sample) == context]
        if context.endswith("driving"):
            current_bins = {value for value in (_driving_bin(sample) for sample in current_context) if value}
            # A bin must have five historical trips. Do not mix different loads,
            # speeds or RPM to manufacture a larger reference set.
            usable_bins = []
            for bin_name in sorted(current_bins):
                count = sum(any(_driving_bin(sample) == bin_name for sample in samples
                                if _context(sample) == context)
                            for samples in prior_samples.values())
                if count >= MIN_REFERENCE_TRIPS:
                    usable_bins.append(bin_name)
            bins = {}
            for bin_name in usable_bins:
                current_bin = [sample for sample in current_context if _driving_bin(sample) == bin_name]
                prior_bin = []
                for samples in prior_samples.values():
                    summary, _unused = _summary([sample for sample in samples
                                                  if _context(sample) == context and _driving_bin(sample) == bin_name])
                    if summary:
                        prior_bin.append(summary)
                reference = _reference_summary(prior_bin)
                current_metrics, coverage = _summary(current_bin)
                comparison = _comparison(current_metrics, reference)
                bins[bin_name] = {"status": "compared" if len(current_bin) >= MIN_CURRENT_SAMPLES
                                    and len(prior_bin) >= MIN_REFERENCE_TRIPS and comparison
                                    else "insufficient_coverage",
                                  "current_samples": len(current_bin), "reference_trips": len(prior_bin),
                                  "coverage": coverage, "metrics": comparison}
            contexts[context] = {"status": "compared" if any(item["status"] == "compared" for item in bins.values())
                                 else "insufficient_coverage", "matched_bins": usable_bins, "bins": bins,
                                 "note": "Each RPM/load/speed bin is compared separately; bins are not pooled."}
            continue
        current_metrics, coverage = _summary(current_context, include_rpm_spread=context == "warm_idle")
        prior = []
        for samples in prior_samples.values():
            scoped = [sample for sample in samples if _context(sample) == context]
            summary, _unused = _summary(scoped, include_rpm_spread=context == "warm_idle")
            if summary:
                prior.append(summary)
        reference = _reference_summary(prior)
        if len(current_context) < MIN_CURRENT_SAMPLES:
            status = "insufficient_current_coverage"
        elif len(prior) < MIN_REFERENCE_TRIPS:
            status = "insufficient_reference_history"
        elif not _comparison(current_metrics, reference):
            status = "insufficient_metric_coverage"
        else:
            status = "compared"
        item = {"status": status, "current_samples": len(current_context), "reference_trips_with_context": len(prior),
                "coverage": coverage, "metrics": _comparison(current_metrics, reference)}
        contexts[context] = item
    status = "ok" if any(item["status"] == "compared" for item in contexts.values()) else "insufficient_history_or_coverage"
    return {
        "status": status, "device_id": device, "trip_id": trip_id,
        "eligible_prior_trips": len(candidates), "minimum_reference_trips": MIN_REFERENCE_TRIPS,
        "reference_history_truncated_to_latest": MAX_REFERENCE_TRIPS if history_truncated else None,
        "minimum_current_samples": MIN_CURRENT_SAMPLES,
        "current_acquisition_coverage": quality["acquisition_coverage"],
        "telemetry_health": quality["telemetry_health"],
        "contexts": contexts,
        "limits": [
            "Comparison uses only earlier sealed, unmutated, same-device trips with verified capture time and no indexed gap.",
            "A reading needs its paired acquisition age within that metric's stated limit. Missing ages are unverified and excluded.",
            "One-second coverage is measured from successive acquisition timestamps. A value age alone does not prove that coverage.",
            "An out-of-band value is a measured difference from this reference set. It is not a diagnosis or a healthy specification.",
        ],
    }
