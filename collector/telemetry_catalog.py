#!/usr/bin/env python3
"""Human-readable metadata and decoding for Freematics telemetry fields.

The wire format deliberately keeps PID numbers small for device fields and
uses ``0x100 + mode-01 PID`` for standard OBD values.  This module is the
single server-side naming boundary: raw fields remain untouched, while every
known field gets a stable key, unit, description, namespace, and decoded
value.  Unknown fields are retained under a deterministic ``pid_0x...`` key.

Manufacturer profiles live under ``protocols/`` and are intentionally not
mixed into this generic decoder.  A profile may add read-only, identity-gated
decoders later without changing the archive schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class MetricDefinition:
    pid: int
    key: str
    label: str
    description: str
    unit: str
    namespace: str
    scale: float = 1.0
    decoder: str = "numeric"


_OBD_LINE = re.compile(
    r'^OBD_PID\(0x([0-9A-Fa-f]+),\s*([a-zA-Z0-9_]+),\s*"([^"]+)",\s*"([^"]+)",\s*([0-9]+)\)'
)


_CUSTOM: tuple[MetricDefinition, ...] = (
    MetricDefinition(0x00, "device_timestamp", "Device timestamp", "Device monotonic timestamp in milliseconds.", "millisecond", "freematics/device", decoder="integer"),
    MetricDefinition(0x0A, "gps_latitude", "GPS latitude", "GNSS latitude reported by the device.", "degree", "freematics/gnss"),
    MetricDefinition(0x0B, "gps_longitude", "GPS longitude", "GNSS longitude reported by the device.", "degree", "freematics/gnss"),
    MetricDefinition(0x0C, "gps_altitude", "GPS altitude", "GNSS altitude above mean sea level.", "metre", "freematics/gnss"),
    MetricDefinition(0x0D, "gps_speed", "GPS speed", "GNSS speed; the firmware serialises this in kilometres per hour.", "kilometre_per_hour", "freematics/gnss"),
    MetricDefinition(0x0E, "gps_heading", "GPS heading", "GNSS course over ground.", "degree", "freematics/gnss"),
    MetricDefinition(0x0F, "gps_satellites", "GPS satellites", "Number of satellites used by the GNSS solution.", "count", "freematics/gnss", decoder="integer"),
    MetricDefinition(0x10, "gps_time", "GPS UTC time", "UTC time-of-day from the GNSS receiver; date may be carried in a separate field.", "utc_time", "freematics/gnss", decoder="string"),
    MetricDefinition(0x11, "gps_date", "GPS UTC date", "UTC calendar date from the GNSS receiver.", "utc_date", "freematics/gnss", decoder="string"),
    MetricDefinition(0x12, "gps_hdop", "GPS HDOP", "Horizontal dilution of precision.", "unitless", "freematics/gnss", scale=0.1),
    MetricDefinition(0x20, "acceleration", "Acceleration", "Three-axis accelerometer sample in g.", "g", "freematics/mems", decoder="vector3"),
    MetricDefinition(0x21, "angular_rate", "Angular rate", "Three-axis gyroscope sample.", "degree_per_second", "freematics/mems", decoder="vector3"),
    MetricDefinition(0x22, "magnetic_field", "Magnetic field", "Three-axis magnetometer sample.", "microtesla", "freematics/mems", decoder="vector3"),
    MetricDefinition(0x23, "mems_temperature", "MEMS temperature", "Temperature reported by the motion sensor.", "celsius", "freematics/mems"),
    MetricDefinition(0x24, "device_battery_voltage", "Device battery voltage", "Voltage measured at the device power input.", "volt", "freematics/device", scale=0.01),
    MetricDefinition(0x25, "orientation", "Device orientation", "Orientation sample when supported by the firmware.", "degree", "freematics/mems", decoder="vector3"),
    MetricDefinition(0x30, "trip_distance", "Trip distance", "Distance accumulated for the current device trip.", "kilometre", "freematics/trip"),
    MetricDefinition(0x80, "payload_size", "Payload size", "Encoded payload size reported by the device.", "byte", "freematics/device", decoder="integer"),
    MetricDefinition(0x81, "cellular_rssi", "Cellular signal strength", "Received signal strength indicator in dBm.", "decibel_milliwatt", "freematics/network", decoder="integer"),
    MetricDefinition(0x82, "device_temperature", "Device temperature", "Temperature reported by the telelogger enclosure.", "celsius", "freematics/device"),
    MetricDefinition(0x83, "hall_sensor", "Hall sensor", "Hall-effect sensor value when present.", "count", "freematics/device", decoder="numeric"),
    MetricDefinition(0x84, "network_transport", "Network transport", "Active uplink: 0 offline, 1 Wi-Fi, 2 cellular.", "enum", "freematics/network", decoder="transport"),
    MetricDefinition(0x85, "obd_protocol", "OBD protocol", "Protocol number selected by the OBD bridge.", "enum", "freematics/obd", decoder="obd_protocol"),
    MetricDefinition(0x86, "obd_supported_pids", "Supported OBD PIDs", "Count of standard Mode 01 PIDs advertised by the ECU.", "count", "freematics/obd", decoder="integer"),
    MetricDefinition(0x87, "obd_timeouts", "OBD timeouts", "Cumulative OBD read failures since the latest active session.", "count", "freematics/obd", decoder="integer"),
    MetricDefinition(0x88, "obd_last_latency", "OBD read latency", "Duration of the latest Mode 01 request. Older firmware reports the cycle maximum.", "millisecond", "freematics/obd", decoder="integer"),
    MetricDefinition(0x89, "obd_state", "OBD state", "OBD state: 0 disconnected, 1 ready, 2 degraded.", "enum", "freematics/obd", decoder="obd_state"),
    MetricDefinition(0x8A, "obd_fast_failures", "OBD core failures", "Maximum consecutive RPM or speed request failures. Older firmware counts failed core cycles.", "count", "freematics/obd", decoder="integer"),
    MetricDefinition(0x8B, "queue_readings", "Queued readings", "Number of filled telemetry readings waiting for upload.", "count", "freematics/transport", decoder="integer"),
    MetricDefinition(0x8C, "queue_bytes", "Queued bytes", "Encoded bytes waiting for upload.", "byte", "freematics/transport", decoder="integer"),
    MetricDefinition(0x8D, "durable_queue_bytes", "Durable queue bytes", "Unacknowledged journal bytes on microSD.", "byte", "freematics/transport", decoder="integer"),
    MetricDefinition(0x8E, "missed_readings", "Missed readings", "Collection cycles without a free RAM slot or skipped sampling deadlines; captured readings are never overwritten.", "count", "freematics/transport", decoder="integer"),
    MetricDefinition(0x8F, "durable_queue_healthy", "Durable queue healthy", "One when the microSD journal accepted its latest write.", "boolean", "freematics/transport", decoder="integer"),
    MetricDefinition(0x93, "gps_age", "GNSS value age", "Elapsed time since the held GNSS fix; UTC fields remain the fix time.", "millisecond", "freematics/gnss", decoder="integer"),
    MetricDefinition(0x94, "voltage_age", "Voltage value age", "Elapsed time since the last successful voltage measurement.", "millisecond", "freematics/power", decoder="integer"),
    MetricDefinition(0x95, "mems_age", "Motion sensor value age", "Elapsed time since the last successful motion sensor measurement.", "millisecond", "freematics/mems", decoder="integer"),
    MetricDefinition(0x97, "rejected_readings", "Rejected readings", "Journal records the collector refused with HTTP 400 since boot; each one is kept in /QUEUE.REJ on the microSD card.", "count", "freematics/transport", decoder="integer"),
    MetricDefinition(0x98, "power_phase", "Power phase", "Trip lifecycle: 0 confirming a wake (modem off), 1 trip, 2 wrap-up upload after the car turned off.", "enum", "freematics/power", decoder="integer"),
    MetricDefinition(0x99, "wake_reason", "Wake reason", "Why this boot started: 0 power on or reset, 1 motion sensor, 2 charging voltage after rest, 3 supervisor restart after a sampling stall.", "enum", "freematics/power", decoder="integer"),
    MetricDefinition(0x9A, "acceleration_peak", "Peak acceleration", "Largest acceleration magnitude since the previous sample, gravity removed; catches hard braking, potholes and swerves between 250 ms samples.", "g", "freematics/mems"),
    MetricDefinition(0x9B, "acceleration_peak_vector", "Peak acceleration vector", "Three-axis acceleration at the peak since the previous sample, gravity removed.", "g", "freematics/mems", decoder="vector3"),
    MetricDefinition(0x9C, "voltage_min", "Minimum voltage", "Lowest supply voltage since the previous sample, read at 50 Hz; shows the engine-cranking dip.", "volt", "freematics/power", scale=0.01),
    MetricDefinition(0x9D, "voltage_max", "Maximum voltage", "Highest supply voltage since the previous sample, read at 50 Hz.", "volt", "freematics/power", scale=0.01),
    MetricDefinition(0x96, "signal_age", "Network signal value age", "Elapsed time since the last signal-strength measurement; 4294967295 means not measured yet.", "millisecond", "freematics/transport", decoder="integer"),
    MetricDefinition(0x92, "can_frame", "Passive CAN frame", "Raw CAN monitor line encoded as hexadecimal bytes.", "hex", "freematics/can", decoder="string"),
    MetricDefinition(0x310, "stored_dtc_read_status", "Stored DTC read status", "Stored DTC read status: 0 no response, 1 response, 2 codes.", "enum", "freematics/diagnostics", decoder="dtc_status"),
    MetricDefinition(0x330, "pending_dtc_read_status", "Pending DTC read status", "Pending DTC read status: 0 no response, 1 response, 2 codes.", "enum", "freematics/diagnostics", decoder="dtc_status"),
    MetricDefinition(0x350, "permanent_dtc_read_status", "Permanent DTC read status", "Permanent DTC read status: 0 no response, 1 response, 2 codes.", "enum", "freematics/diagnostics", decoder="dtc_status"),
)


_TRANSPORT_NAMES = {0: "offline", 1: "wifi", 2: "cellular"}
_OBD_STATE_NAMES = {0: "disconnected", 1: "ready", 2: "degraded"}
_OBD_PROTOCOL_NAMES = {
    3: "iso_9141_2",
    4: "kwp2000_5kbps",
    5: "kwp2000_fast",
    6: "iso15765_11bit_500k",
    7: "iso15765_29bit_500k",
    8: "iso15765_29bit_250k",
    9: "iso15765_11bit_250k",
    11: "j1939",
    12: "iso11898_11bit_500k",
    13: "iso11898_29bit_500k",
    14: "iso11898_11bit_250k",
    15: "iso11898_29bit_250k",
}
_DTC_STATUS_NAMES = {0: "no_response", 1: "response", 2: "codes"}


def _standard_definitions() -> dict[int, MetricDefinition]:
    """Read the shared C X-macro so Python and firmware stay in sync."""

    header = Path(__file__).resolve().parents[1] / "obd_pids.h"
    definitions: dict[int, MetricDefinition] = {}
    try:
        lines = header.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = _OBD_LINE.match(line.strip())
        if not match:
            continue
        pid = int(match.group(1), 16)
        key, label, unit, _priority = match.group(2), match.group(3), match.group(4), match.group(5)
        definitions[0x100 + pid] = MetricDefinition(
            0x100 + pid,
            key,
            label,
            f"SAE J1979 Mode 01 PID 0x{pid:02X}: {label}.",
            unit,
            "standard/sae-j1979/mode-01",
        )
    return definitions


def metric_catalog() -> dict[int, MetricDefinition]:
    catalog = {definition.pid: definition for definition in _CUSTOM}
    standard = _standard_definitions()
    catalog.update(standard)
    for measured_pid, measured in standard.items():
        age_pid = 0x400 | (measured_pid & 0xFF)
        catalog[age_pid] = MetricDefinition(
            age_pid, f"obd_age_{measured_pid & 0xFF:02X}", f"{measured.label} age",
            "Elapsed time since this PID last responded; a held value is not a fresh measurement.",
            "millisecond", "freematics/obd", decoder="integer",
        )
    for index, group in enumerate(("stored", "pending", "permanent")):
        catalog[0x360 + index] = MetricDefinition(
            0x360 + index, f"dtc_age_{index}", f"{group.title()} diagnostic scan age",
            "Elapsed time since the last diagnostic scan attempt; read status indicates success.",
            "millisecond", "freematics/diagnostics", decoder="integer",
        )
    return catalog


CATALOG = metric_catalog()


def _number(raw: str) -> int | float | None:
    value = raw.strip()
    if not value:
        return None
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        return float(value)
    except ValueError:
        return None


def _scaled(value: int | float, scale: float) -> int | float:
    result = value * scale
    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def _decode(definition: MetricDefinition, raw: str) -> Any:
    if definition.decoder == "string":
        return raw
    if definition.decoder == "transport":
        numeric = _number(raw)
        if isinstance(numeric, (int, float)):
            code = int(numeric)
            return {"code": code, "name": _TRANSPORT_NAMES.get(code, "unknown")}
        return {"raw": raw, "name": "unknown"}
    if definition.decoder == "obd_protocol":
        numeric = _number(raw)
        if isinstance(numeric, (int, float)):
            code = int(numeric)
            return {"code": code, "name": _OBD_PROTOCOL_NAMES.get(code, "unknown")}
        return {"raw": raw, "name": "unknown"}
    if definition.decoder == "obd_state":
        numeric = _number(raw)
        if isinstance(numeric, (int, float)):
            code = int(numeric)
            return {"code": code, "name": _OBD_STATE_NAMES.get(code, "unknown")}
        return {"raw": raw, "name": "unknown"}
    if definition.decoder == "dtc_status":
        numeric = _number(raw)
        if isinstance(numeric, (int, float)):
            code = int(numeric)
            return {"code": code, "name": _DTC_STATUS_NAMES.get(code, "unknown")}
        return {"raw": raw, "name": "unknown"}
    if definition.decoder == "vector3":
        parts = [part.strip() for part in raw.split(";")]
        numbers = [_number(part) for part in parts]
        if len(numbers) == 3 and all(isinstance(value, (int, float)) for value in numbers):
            return {axis: value for axis, value in zip(("x", "y", "z"), numbers)}
        return {"raw": raw, "components": parts}
    numeric = _number(raw)
    if numeric is None:
        return raw
    return _scaled(numeric, definition.scale)


def _definition(pid_text: str) -> MetricDefinition:
    try:
        pid = int(pid_text, 0)
    except ValueError:
        pid = int(pid_text, 16)
    definition = CATALOG.get(pid)
    if definition is not None:
        return definition
    if 0x400 <= pid <= 0x4FF:
        measured_pid = 0x100 | (pid & 0xFF)
        measured = CATALOG.get(measured_pid)
        label = measured.label if measured else f"OBD PID 0x{measured_pid:03X}"
        return MetricDefinition(pid, f"obd_age_{pid & 0xFF:02X}", f"{label} age",
                                "Elapsed time since this PID last responded; a held value is not a fresh measurement.",
                                "millisecond", "freematics/obd", decoder="integer")
    if 0x360 <= pid <= 0x362:
        return MetricDefinition(pid, f"dtc_age_{pid - 0x360}", "Diagnostic scan age",
                                "Elapsed time since the last diagnostic scan attempt; read status indicates success.",
                                "millisecond", "freematics/diagnostics", decoder="integer")
    return MetricDefinition(
        pid,
        f"pid_{pid:03X}" if pid >= 0 else "pid_unknown",
        f"Unknown PID 0x{pid:03X}",
        "Field not present in the current Freematics or SAE catalogue; raw value retained.",
        "unknown",
        "unknown",
        decoder="string",
    )


def _put_metric(metrics: dict[str, Any], definition: MetricDefinition, raw: str) -> None:
    item = {
        "pid": f"0x{definition.pid:03X}",
        "key": definition.key,
        "label": definition.label,
        "description": definition.description,
        "unit": definition.unit,
        "namespace": definition.namespace,
        "raw": raw,
        "value": _decode(definition, raw),
    }
    existing = metrics.get(definition.key)
    if existing is None:
        metrics[definition.key] = item
    elif isinstance(existing, dict) and "occurrences" not in existing:
        metrics[definition.key] = {
            "key": definition.key,
            "label": definition.label,
            "description": definition.description,
            "unit": definition.unit,
            "namespace": definition.namespace,
            "occurrences": [existing, item],
        }
    else:
        existing.setdefault("occurrences", []).append(item)


def readable_metrics(fields: Iterable[dict[str, str]]) -> dict[str, Any]:
    """Return stable, named metric objects without discarding raw fields."""

    metrics: dict[str, Any] = {}
    for field in fields:
        pid = str(field.get("pid", ""))
        raw = str(field.get("value", ""))
        if not pid:
            continue
        _put_metric(metrics, _definition(pid), raw)
    return metrics


def metric_values(fields: Iterable[dict[str, str]]) -> dict[str, Any]:
    """Compact key/value view for scripts; detailed objects remain canonical."""

    details = readable_metrics(fields)
    values: dict[str, Any] = {}
    for key, item in details.items():
        if "occurrences" in item:
            values[key] = [occurrence["value"] for occurrence in item["occurrences"]]
        else:
            values[key] = item["value"]
    return values


__all__ = ["CATALOG", "MetricDefinition", "metric_catalog", "metric_values", "readable_metrics"]
