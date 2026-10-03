#!/usr/bin/env python3
"""Measure live Freematics @FT1 timing without sending serial commands.

Opens the port read-only, configures 115200 8N1 through termios, and never
changes DTR/RTS. Output contains aggregate counters and ages only (no VIN,
coordinates, or sensor values).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import select
import statistics
import termios
import time
from collections import defaultdict
from pathlib import Path

BAUD = termios.B115200
MAX_LINE = 16 * 1024
PID_LABELS = {
    0x0C: "engine_rpm",
    0x0D: "vehicle_speed",
    0x05: "coolant_temperature",
    0x11: "throttle_position",
    0x04: "calculated_engine_load",
    0x24: "model_b_supply_voltage",
    0x42: "ecu_control_module_voltage",
}


def parse_frame(line: bytes) -> dict | None:
    """Return non-identifying timing/field metadata for a valid FT1 record."""
    try:
        text = line.decode("ascii").rstrip("\r")
        header, payload = text.split("|", 1)
        meta = header.removeprefix("@FT1,").split(",", 5)
        boot, capture, utc_valid, utc_ms, dropped, _supported = meta
        device_payload, checksum_text = payload.rsplit("*", 1)
        checksum = int(checksum_text, 16)
        if sum(device_payload.encode("ascii")) & 0xFF != checksum:
            return None
        if not device_payload.split("#", 1)[0]:
            return None
        fields: dict[int, list[float]] = {}
        _, data = device_payload.split("#", 1)
        for item in data.split(","):
            pid_text, values_text = item.split(":", 1)
            fields[int(pid_text, 16)] = [float(value) for value in values_text.split(";")]
        boot_id = int(boot)
        capture_ms = int(capture)
        dropped_count = int(dropped)
        if utc_valid not in ("0", "1") or (utc_valid == "0" and int(utc_ms) != 0):
            return None
        return {
            "boot": boot_id,
            "capture": capture_ms,
            "dropped": dropped_count,
            "fields": fields,
        }
    except (UnicodeDecodeError, ValueError, IndexError):
        return None


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(percent * len(ordered)) - 1)], 2)


def summarize(port: str, duration: float) -> dict:
    fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        # Set only the line discipline/baud; do not issue modem-control ioctls.
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
        attrs[3] = 0
        attrs[4] = BAUD
        attrs[5] = BAUD
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)

        started = time.monotonic()
        deadline = started + duration
        buffer = bytearray()
        totals = defaultdict(int)
        records: list[dict] = []
        capture_deltas: list[float] = []
        arrival_deltas: list[float] = []
        ages: dict[int, list[int]] = defaultdict(list)
        refresh_intervals: dict[int, list[int]] = defaultdict(list)
        observed_counters: dict[int, list[int]] = defaultdict(list)
        last_age: dict[int, int] = {}
        last_sample_tick: dict[int, int] = {}
        previous_boot: int | None = None
        previous_capture: int | None = None
        previous_arrival: float | None = None
        unwrapped_capture = 0
        last_device_metrics: dict[int, int] = {}
        first_device_metrics: dict[int, int] = {}
        log_tags: dict[str, int] = defaultdict(int)
        storage_error_lines = 0

        while time.monotonic() < deadline:
            readable, _, _ = select.select([fd], [], [], min(0.2, deadline - time.monotonic()))
            if not readable:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:
                continue
            totals["bytes_received"] += len(chunk)
            buffer.extend(chunk)
            if len(buffer) > MAX_LINE * 2:
                start = buffer.rfind(b"\n")
                del buffer[: start + 1 if start >= 0 else len(buffer) - MAX_LINE]
                totals["oversize_resyncs"] += 1
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer[:] = remainder
                line = line.rstrip(b"\r")
                if not line.startswith(b"@FT1,"):
                    if line:
                        totals["non_telemetry_lines"] += 1
                        lowered = line.lower()
                        for prefix, label in (
                            (b"[queue]", "queue"),
                            (b"[storage]", "storage"),
                            (b"[sd]", "sd"),
                            (b"[file]", "csv_file"),
                            (b"[critical]", "critical"),
                            (b"[obd]", "obd"),
                        ):
                            if lowered.startswith(prefix):
                                log_tags[label] += 1
                        if any(word in lowered for word in (b"sd journal unavailable", b"append failed", b"write failed", b"readback failed", b"storage fault", b"rejected")):
                            storage_error_lines += 1
                    continue
                frame = parse_frame(line)
                if frame is None:
                    totals["corrupt_ft1_records"] += 1
                    continue
                totals["valid_frames"] += 1
                totals["valid_frame_bytes"] += len(line) + 1
                totals["largest_frame_bytes"] = max(totals["largest_frame_bytes"], len(line) + 1)
                arrival = time.monotonic()
                if previous_arrival is not None:
                    arrival_deltas.append((arrival - previous_arrival) * 1000)
                previous_arrival = arrival

                if frame["boot"] != previous_boot:
                    if previous_boot is not None:
                        totals["device_restarts"] += 1
                    previous_boot = frame["boot"]
                    previous_capture = None
                    last_age.clear()
                    last_sample_tick.clear()
                if previous_capture is not None:
                    delta = (frame["capture"] - previous_capture) & 0xFFFFFFFF
                    if delta < 0x80000000:
                        capture_deltas.append(delta)
                        unwrapped_capture += delta
                    else:
                        totals["non_monotonic_capture_ticks"] += 1
                        unwrapped_capture = frame["capture"]
                else:
                    unwrapped_capture = frame["capture"]
                previous_capture = frame["capture"]

                dropped = frame["dropped"]
                if records:
                    previous_dropped = records[-1]["dropped"]
                    if dropped >= previous_dropped:
                        totals["device_usb_drops"] += dropped - previous_dropped
                records.append({"dropped": dropped})

                fields = frame["fields"]
                for metric_pid in (0x87, 0x88, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x97):
                    if metric_pid in fields and fields[metric_pid]:
                        value = int(fields[metric_pid][0])
                        first_device_metrics.setdefault(metric_pid, value)
                        last_device_metrics[metric_pid] = value
                        observed_counters[metric_pid].append(value)

                for pid in PID_LABELS:
                    value_field = fields.get(0x100 | pid)
                    age_field = fields.get(0x400 | pid)
                    if not value_field or not age_field:
                        continue
                    age = max(0, int(age_field[0]))
                    ages[pid].append(age)
                    previous = last_age.get(pid)
                    # A substantial age reset indicates a new successful ECU
                    # response; merely repeating a cached value does not.
                    if previous is not None and age + 40 < previous:
                        sample_tick = unwrapped_capture - age
                        prior_tick = last_sample_tick.get(pid)
                        if prior_tick is not None and sample_tick > prior_tick:
                            refresh_intervals[pid].append(sample_tick - prior_tick)
                        last_sample_tick[pid] = sample_tick
                    elif previous is None:
                        last_sample_tick[pid] = unwrapped_capture - age
                    last_age[pid] = age

        elapsed = max(0.001, time.monotonic() - started)
        whole_frame_gaps = [delta for delta in capture_deltas if delta > 300]
        link_busy_pct = totals["bytes_received"] * 10 * 100 / (115200 * elapsed)
        ft_link_busy_pct = totals["valid_frame_bytes"] * 10 * 100 / (115200 * elapsed)
        per_pid: dict[str, dict] = {}
        for pid, values in ages.items():
            limit = 250 if pid in (0x0C, 0x0D) else 1000
            refreshes = refresh_intervals[pid]
            per_pid[PID_LABELS[pid]] = {
                "sampled_frames": len(values),
                "age_ms_median": percentile(values, 0.50),
                "age_ms_p95": percentile(values, 0.95),
                "age_ms_max": max(values),
                "frames_over_freshness_target": sum(age > limit for age in values),
                "freshness_target_ms": limit,
                "inferred_successful_update_intervals_ms": len(refreshes),
                "update_interval_ms_median": percentile(refreshes, 0.50),
                "update_interval_ms_p95": percentile(refreshes, 0.95),
                "update_interval_ms_max": max(refreshes) if refreshes else None,
            }
        return {
            "port": port,
            "measurement_seconds": round(elapsed, 2),
            "baud": 115200,
            "valid_frames": totals["valid_frames"],
            "corrupt_ft1_records": totals["corrupt_ft1_records"],
            "non_telemetry_lines": totals["non_telemetry_lines"],
            "device_restarts": totals["device_restarts"],
            "received_bytes": totals["bytes_received"],
            "valid_ft1_frame_bytes": totals["valid_frame_bytes"],
            "largest_valid_ft1_frame_bytes": totals["largest_frame_bytes"],
            "estimated_uart_wire_utilization_pct": round(link_busy_pct, 2),
            "estimated_ft1_only_uart_wire_utilization_pct": round(ft_link_busy_pct, 2),
            "capture_interval_ms": {
                "median": percentile(capture_deltas, 0.50),
                "p95": percentile(capture_deltas, 0.95),
                "max": max(capture_deltas) if capture_deltas else None,
                "over_300ms_count": len(whole_frame_gaps),
            },
            "usb_frame_arrival_interval_ms": {
                "median": percentile(arrival_deltas, 0.50),
                "p95": percentile(arrival_deltas, 0.95),
                "max": max(arrival_deltas) if arrival_deltas else None,
            },
            "device_usb_drops_during_capture": totals["device_usb_drops"],
            "sanitized_device_log_tag_counts": dict(sorted(log_tags.items())),
            "recognized_storage_error_log_lines": storage_error_lines,
            "device_metrics_min_max": {
                f"0x{pid:02X}": [min(values), max(values)]
                for pid, values in sorted(observed_counters.items())
            },
            "device_metrics_start_end": {
                f"0x{pid:02X}": [first_device_metrics.get(pid), last_device_metrics.get(pid)]
                for pid in (0x8B, 0x8C, 0x8D, 0x8E, 0x8F, 0x87, 0x97)
                if pid in first_device_metrics
            },
            "pid_freshness_and_updates": per_pid,
            "partial_record_bytes_at_end": len(buffer),
            "privacy": "No VIN, coordinates, or sensor values are included.",
        }
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="verified Freematics USB serial port")
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--output", type=Path, help="optional path for aggregate JSON evidence")
    args = parser.parse_args()
    if args.seconds < 5 or args.seconds > 600:
        parser.error("--seconds must be between 5 and 600")
    report = summarize(args.port, args.seconds)
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
