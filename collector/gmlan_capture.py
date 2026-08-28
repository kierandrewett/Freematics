#!/usr/bin/env python3
"""Analyse passive CAN captures with Scapy without transmitting traffic.

The adapter accepts either a Freematics archive or JSON Lines. A JSON Lines
record has this shape::

    {"timestamp_ms": 100, "can_id": "0x5E8", "data_hex": "03 5A 90 00"}

``can_id`` is an 11-bit or 29-bit CAN identifier. ``data_hex`` contains one
to eight CAN data bytes. ``extended`` is optional and defaults to false. A
Freematics archive uses PID ``0x92`` records. The firmware stores each raw
monitor line as hexadecimal ASCII, so the adapter decodes that field before
parsing ELM-style lines such as ``5E8 03 5A 90 ...``.

Scapy is an offline analysis dependency. This module never opens a transport,
uses a socket, sends a CAN frame, or changes a vehicle profile. Unknown raw
line formats remain counted as unparsed evidence instead of being guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from history_indexer import parse_frames
except ImportError:  # pragma: no cover - supports package imports
    from .history_indexer import parse_frames

MAX_CAPTURE_FILE_SIZE = 64 * 1024 * 1024
MAX_CAPTURE_LINE_SIZE = 64 * 1024
MAX_CAPTURE_FRAMES = 100_000
MAX_INFLIGHT_ISOTP_MESSAGES = 1_024
MAX_TIMESTAMP_MS = (1 << 63) - 1
MAX_CAN_DATA_BYTES = 8
_INTERESTING_SERVICES = frozenset({0x1A, 0x22, 0x5A, 0x62, 0x7F})

_CAN_ID_RE = re.compile(r"^(?:0[xX])?([0-9A-Fa-f]{1,8})$")
_BYTE_RE = re.compile(r"^(?:0[xX])?[0-9A-Fa-f]{2}$")
_MONITOR_LINE_RE = re.compile(r"^\s*(?:0[xX])?([0-9A-Fa-f]{1,8})[\s,]+(.+?)\s*$")
_RAW_CAPTURE_FIELD_RE = re.compile(r"(?:^|,)92[:=][^,\r\n]*")


class CaptureInputError(ValueError):
    """Raised when a capture does not satisfy the declared input contract."""


class ScapyUnavailable(RuntimeError):
    """Raised when the optional offline Scapy dependency is not installed."""


@dataclass(frozen=True)
class CanFrame:
    """One validated CAN frame from a passive capture."""

    timestamp_ms: int
    can_id: int
    data: bytes
    extended: bool = False


@dataclass(frozen=True)
class Capture:
    """Validated frames and source metadata for one capture file."""

    source_format: str
    source_sha256: str
    frames: tuple[CanFrame, ...]
    unparsed_records: int


@dataclass(frozen=True)
class ProfileCandidate:
    """A read-only candidate loaded from a vehicle profile."""

    name: str
    service: int
    identifier: int | None
    enabled: bool


def _read_source(path: Path) -> tuple[bytes, str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CaptureInputError(f"cannot stat capture: {path}") from exc
    if size > MAX_CAPTURE_FILE_SIZE:
        raise CaptureInputError("capture exceeds the 64 MiB input limit")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CaptureInputError(f"cannot read capture: {path}") from exc
    return content, hashlib.sha256(content).hexdigest()


def _parse_non_negative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TIMESTAMP_MS:
        raise CaptureInputError(f"{field} must be an integer from zero to {MAX_TIMESTAMP_MS}")
    return value


def _parse_can_id(value: Any) -> int:
    if isinstance(value, bool):
        raise CaptureInputError("can_id must be an integer or hexadecimal string")
    if isinstance(value, int):
        can_id = value
    elif isinstance(value, str):
        match = _CAN_ID_RE.fullmatch(value.strip())
        if match is None:
            raise CaptureInputError("can_id must contain one to eight hexadecimal digits")
        can_id = int(match.group(1), 16)
    else:
        raise CaptureInputError("can_id must be an integer or hexadecimal string")
    if not 0 <= can_id <= 0x1FFFFFFF:
        raise CaptureInputError("can_id is outside the 11-bit or 29-bit CAN range")
    return can_id


def _parse_data_hex(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise CaptureInputError("data_hex must contain one to eight bytes")
    try:
        data = bytes.fromhex(value)
    except ValueError as exc:
        raise CaptureInputError("data_hex must contain hexadecimal bytes") from exc
    if not 1 <= len(data) <= MAX_CAN_DATA_BYTES:
        raise CaptureInputError("data_hex must contain one to eight bytes")
    return data


def _parse_json_record(record: Any, line_number: int) -> CanFrame:
    if not isinstance(record, dict):
        raise CaptureInputError(f"JSON Lines record {line_number} must be an object")
    allowed = {"timestamp_ms", "can_id", "data_hex", "extended"}
    unknown = set(record) - allowed
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise CaptureInputError(f"JSON Lines record {line_number} has unknown field(s): {names}")
    required = {"timestamp_ms", "can_id", "data_hex"}
    missing = required - set(record)
    if missing:
        names = ", ".join(sorted(missing))
        raise CaptureInputError(f"JSON Lines record {line_number} is missing field(s): {names}")
    extended = record.get("extended", False)
    if not isinstance(extended, bool):
        raise CaptureInputError(f"JSON Lines record {line_number} field extended must be boolean")
    can_id = _parse_can_id(record["can_id"])
    if can_id > 0x7FF and not extended:
        extended = True
    return CanFrame(
        timestamp_ms=_parse_non_negative_integer(record["timestamp_ms"], "timestamp_ms"),
        can_id=can_id,
        data=_parse_data_hex(record["data_hex"]),
        extended=extended,
    )

def _append_frame(frames: list[CanFrame], frame: CanFrame) -> None:
    if len(frames) >= MAX_CAPTURE_FRAMES:
        raise CaptureInputError(f"capture exceeds the {MAX_CAPTURE_FRAMES} frame limit")
    frames.append(frame)


def parse_jsonl(path: Path) -> Capture:
    """Parse strict JSON Lines CAN records from ``path``."""
    content, source_sha256 = _read_source(path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptureInputError("JSON Lines capture must be UTF-8") from exc

    frames: list[CanFrame] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(line.encode("utf-8")) > MAX_CAPTURE_LINE_SIZE:
            raise CaptureInputError(f"JSON Lines record {line_number} exceeds the line limit")
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (RecursionError, json.JSONDecodeError) as exc:
            raise CaptureInputError(f"JSON Lines record {line_number} is not valid JSON") from exc
        _append_frame(frames, _parse_json_record(record, line_number))
    return Capture("jsonl", source_sha256, tuple(frames), 0)


def _parse_monitor_line(raw_line: str, timestamp_ms: int) -> CanFrame | None:
    match = _MONITOR_LINE_RE.fullmatch(raw_line)
    if match is None:
        return None
    can_id = int(match.group(1), 16)
    if can_id > 0x1FFFFFFF:
        return None
    tokens = [token for token in re.split(r"[\s,]+", match.group(2).strip()) if token]
    if not 1 <= len(tokens) <= MAX_CAN_DATA_BYTES or not all(_BYTE_RE.fullmatch(token) for token in tokens):
        return None
    data = bytes(int(token[-2:], 16) for token in tokens)
    return CanFrame(timestamp_ms, can_id, data, can_id > 0x7FF)


def parse_archive(path: Path) -> Capture:
    """Parse PID ``0x92`` raw monitor records from a Freematics archive."""
    content, source_sha256 = _read_source(path)
    try:
        raw = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptureInputError("Freematics archive must be UTF-8") from exc

    frames: list[CanFrame] = []
    unparsed_records = 0
    for archive_frame in parse_frames(raw, include_final=True):
        for pid, value in archive_frame.ordered_fields:
            if pid != "92":
                continue
            try:
                monitor_bytes = bytes.fromhex(value)
                monitor_line = monitor_bytes.decode("ascii")
            except (ValueError, UnicodeDecodeError):
                unparsed_records += 1
                continue
            frame = _parse_monitor_line(monitor_line, archive_frame.device_monotonic_ms)
            if frame is None:
                unparsed_records += 1
            else:
                _append_frame(frames, frame)
    raw_capture_records = len(_RAW_CAPTURE_FIELD_RE.findall(raw))
    dropped_records = raw_capture_records - len(frames) - unparsed_records
    unparsed_records += max(0, dropped_records)
    return Capture("freematics-archive", source_sha256, tuple(frames), unparsed_records)


def _profile_integer(value: Any, field: str, maximum: int) -> int:
    if not isinstance(value, str):
        raise CaptureInputError(f"profile {field} must be a hexadecimal string")
    match = _CAN_ID_RE.fullmatch(value.strip())
    if match is None:
        raise CaptureInputError(f"profile {field} must contain hexadecimal digits")
    parsed = int(match.group(1), 16)
    if not 0 <= parsed <= maximum:
        raise CaptureInputError(f"profile {field} is outside its permitted range")
    return parsed


def load_profile_candidates(path: Path) -> tuple[ProfileCandidate, ...]:
    """Load candidate service and identifier pairs without enabling them."""
    content, _ = _read_source(path)
    try:
        profile = json.loads(content)
    except (RecursionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureInputError("vehicle profile must be UTF-8 JSON") from exc
    if not isinstance(profile, dict):
        raise CaptureInputError("vehicle profile must be a JSON object")
    raw_candidates = profile.get("gmlan_candidates")
    if not isinstance(raw_candidates, list):
        raise CaptureInputError("vehicle profile gmlan_candidates must be a list")

    candidates: list[ProfileCandidate] = []
    for index, candidate in enumerate(raw_candidates, start=1):
        if not isinstance(candidate, dict):
            raise CaptureInputError(f"profile candidate {index} must be an object")
        try:
            name = candidate["name"]
            enabled = candidate.get("enabled", False)
            service = _profile_integer(candidate["service"], "service", 0xFF)
        except KeyError as exc:
            raise CaptureInputError(f"profile candidate {index} is missing {exc.args[0]}") from exc
        if not isinstance(name, str) or not name:
            raise CaptureInputError(f"profile candidate {index} name must be a non-empty string")
        if not isinstance(enabled, bool):
            raise CaptureInputError(f"profile candidate {index} enabled must be boolean")
        identifier = (
            None
            if "identifier" not in candidate
            else _profile_integer(candidate["identifier"], "identifier", 0xFFFF)
        )
        candidates.append(ProfileCandidate(name, service, identifier, enabled))
    return tuple(candidates)


def _scapy_components() -> tuple[Any, Any, Any]:
    try:
        from scapy.contrib.automotive.gm.gmlan import GMLAN
        from scapy.contrib.isotp.isotp_utils import ISOTPMessageBuilder
        from scapy.layers.can import CAN
    except ImportError as exc:  # pragma: no cover - exercised by CLI environment checks
        raise ScapyUnavailable("install tools/requirements-gmlan.txt before decoding a capture") from exc
    return CAN, GMLAN, ISOTPMessageBuilder


def _candidate_for(
    candidates: tuple[ProfileCandidate, ...], service: int, identifier: int | None
) -> ProfileCandidate | None:
    family_candidate: ProfileCandidate | None = None
    for candidate in candidates:
        if candidate.service != service:
            continue
        if candidate.identifier == identifier:
            return candidate
        if candidate.identifier is None:
            family_candidate = candidate
    return family_candidate


def _candidate_fields(candidate: ProfileCandidate | None) -> dict[str, Any]:
    if candidate is None:
        return {"candidate": None, "candidate_enabled": None}
    return {"candidate": candidate.name, "candidate_enabled": candidate.enabled}


def _message_timestamp_ms(message: Any) -> int | None:
    timestamp = getattr(message, "time", None)
    if timestamp is None:
        return None
    try:
        return int(round(float(timestamp) * 1_000))
    except (OverflowError, TypeError, ValueError):
        return None


def _decode_message(message: Any, candidates: tuple[ProfileCandidate, ...], gmlan: Any) -> dict[str, Any] | None:
    payload = bytes(message.data)
    if not payload:
        return None
    service = payload[0]
    if service not in _INTERESTING_SERVICES:
        return None

    observation: dict[str, Any] = {
        "timestamp_ms": _message_timestamp_ms(message),
        "can_id": f"0x{int(message.rx_id):03X}",
        "iso_tp_payload_hex": payload.hex().upper(),
        "service": f"0x{service:02X}",
        "service_name": gmlan.services.get(service, "unknown"),
    }
    minimum_length = 2 if service in (0x1A, 0x5A) else 3
    if len(payload) < minimum_length:
        observation.update({"status": "malformed", "reason": "message is shorter than its service envelope"})
        return observation
    try:
        packet = gmlan(payload)
    except Exception:
        observation.update({"status": "decode_error", "reason": "Scapy could not dissect the service envelope"})
        return observation
    observation["decoded_layer"] = packet.payload.__class__.__name__

    if service == 0x5A:
        identifier = payload[1]
        candidate = _candidate_for(candidates, 0x1A, identifier)
        observation.update(
            {
                "status": "positive_response",
                "capability": "positive_response_observed",
                "request_service": "0x1A",
                "identifier": f"0x{identifier:02X}",
                "data_hex": payload[2:].hex().upper(),
                "raw_response_hex": payload.hex().upper(),
                **_candidate_fields(candidate),
            }
        )
        return observation

    if service == 0x62:
        identifier = int.from_bytes(payload[1:3], "big")
        candidate = _candidate_for(candidates, 0x22, identifier)
        observation.update(
            {
                "status": "positive_response",
                "capability": "positive_response_observed",
                "request_service": "0x22",
                "identifier": f"0x{identifier:04X}",
                "data_hex": payload[3:].hex().upper(),
                "raw_response_hex": payload.hex().upper(),
                **_candidate_fields(candidate),
            }
        )
        return observation

    if service == 0x7F:
        request_service = payload[1]
        observation.update(
            {
                "status": "negative_response",
                "capability": "negative_response_observed",
                "request_service": f"0x{request_service:02X}",
                "negative_code": f"0x{payload[2]:02X}",
                "raw_response_hex": payload.hex().upper(),
                **_candidate_fields(_candidate_for(candidates, request_service, None)),
            }
        )
        return observation

    identifier = payload[1] if service == 0x1A else int.from_bytes(payload[1:3], "big")
    candidate = _candidate_for(candidates, service, identifier)
    observation.update(
        {
            "status": "request_observed",
            "capability": "request_observed",
            "identifier": f"0x{identifier:02X}" if service == 0x1A else f"0x{identifier:04X}",
            "raw_request_hex": payload.hex().upper(),
            **_candidate_fields(candidate),
        }
    )
    return observation


def analyse_capture(
    capture: Capture,
    candidates: tuple[ProfileCandidate, ...],
    *,
    extended_addressing: bool = False,
) -> dict[str, Any]:
    if len(capture.frames) > MAX_CAPTURE_FRAMES:
        raise CaptureInputError(f"capture exceeds the {MAX_CAPTURE_FRAMES} frame limit")
    can, gmlan, builder_type = _scapy_components()
    builder = builder_type(use_ext_address=extended_addressing)
    observations: list[dict[str, Any]] = []
    for frame in capture.frames:
        timestamp_ms = _parse_non_negative_integer(frame.timestamp_ms, "frame timestamp_ms")
        kwargs: dict[str, Any] = {
            "identifier": frame.can_id,
            "length": len(frame.data),
            "data": frame.data,
        }
        if frame.extended:
            kwargs["flags"] = "extended"
        packet = can(**kwargs)
        packet.time = timestamp_ms / 1_000
        builder.feed(packet)
        if len(builder.buckets) > MAX_INFLIGHT_ISOTP_MESSAGES:
            raise CaptureInputError(
                f"capture exceeds the {MAX_INFLIGHT_ISOTP_MESSAGES} incomplete ISO-TP message limit"
            )
        for message in builder:
            observation = _decode_message(message, candidates, gmlan)
            if observation is not None:
                observations.append(observation)

    return {
        "capture": {
            "source_format": capture.source_format,
            "source_sha256": capture.source_sha256,
            "frame_count": len(capture.frames),
            "unparsed_records": capture.unparsed_records,
        },
        "decoder": {
            "name": "scapy",
            "transport": "ISO-TP over CAN",
            "transmit": False,
            "extended_addressing": extended_addressing,
        },
        "observations": observations,
    }


def _default_profile_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "protocols/manufacturers/opel-vauxhall/corsa-d/2006-2014/profile.json"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Freematics archive or JSON Lines capture")
    parser.add_argument("--format", choices=("archive", "jsonl"), default="archive")
    parser.add_argument("--profile", type=Path, default=_default_profile_path())
    parser.add_argument("--extended-addressing", action="store_true")
    parser.add_argument("--output", type=Path, help="write JSON report to this path instead of stdout")
    args = parser.parse_args(argv)

    try:
        capture = parse_archive(args.input) if args.format == "archive" else parse_jsonl(args.input)
        report = analyse_capture(
            capture,
            load_profile_candidates(args.profile),
            extended_addressing=args.extended_addressing,
        )
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.write_text(rendered, encoding="utf-8")
    except (CaptureInputError, ScapyUnavailable, OSError) as exc:
        parser.error(str(exc))
    return 0


__all__ = [
    "CanFrame",
    "Capture",
    "CaptureInputError",
    "ProfileCandidate",
    "ScapyUnavailable",
    "analyse_capture",
    "load_profile_candidates",
    "parse_archive",
    "parse_jsonl",
]


if __name__ == "__main__":
    raise SystemExit(main())
