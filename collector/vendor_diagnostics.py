"""Parse identity-gated vendor diagnostic responses without sending commands.

The parser handles the response envelope for UDS ReadDataByIdentifier and the
legacy GMLAN equivalent. It does not know ECU addresses, open a transport, or
schedule retries. Profile validation and identity gating remain separate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

try:
    from vehicle_profiles import CandidateReadOperation
except ImportError:  # pragma: no cover - supports package imports
    from .vehicle_profiles import CandidateReadOperation

_HEX_TOKEN_RE = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
_SERVICE_RE = re.compile(r"^0[xX]([0-9A-Fa-f]{2})$")
_IDENTIFIER_RE = re.compile(r"^0[xX]([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})?$")

ObservationStatus = Literal[
    "positive",
    "negative",
    "no_positive_response",
    "unsupported_identifier",
    "policy_rejected",
]


@dataclass(frozen=True)
class DiagnosticObservation:
    """One response observation with its raw evidence and decoded envelope."""

    status: ObservationStatus
    candidate_name: str
    service: str
    identifier: str | None
    raw_response: str
    data_hex: str | None = None
    negative_code: int | None = None
    reason: str | None = None


def _service_byte(service: str) -> int | None:
    match = _SERVICE_RE.fullmatch(service)
    return int(match.group(1), 16) if match else None


def _identifier_bytes(identifier: str | None) -> tuple[int, ...] | None:
    if identifier is None:
        return None
    match = _IDENTIFIER_RE.fullmatch(identifier)
    if not match:
        return None
    high = int(match.group(1), 16)
    low = match.group(2)
    return (high,) if low is None else (high, int(low, 16))


def _response_bytes(response: bytes | str) -> list[int]:
    text = response.decode("ascii", errors="replace") if isinstance(response, bytes) else response
    return [int(token, 16) for token in _HEX_TOKEN_RE.findall(text)]


def parse_response(candidate: CandidateReadOperation, response: bytes | str) -> DiagnosticObservation:
    """Classify one captured response; never retries or transmits.

    A family candidate without an identifier is deliberately reported as
    ``unsupported_identifier``. Callers must record the evidence and stop
    instead of inventing a data identifier.
    """

    raw_response = response.decode("utf-8", errors="replace") if isinstance(response, bytes) else response
    service = _service_byte(candidate.service)
    identifier = _identifier_bytes(candidate.identifier)
    if (
        service is None
        or candidate.status != "experimental"
        or not candidate.enabled
        or not candidate.read_only
        or not candidate.identity_gated
        or not candidate.requires_positive_supported_response
        or not candidate.requires_capture_of_raw_response
    ):
        return DiagnosticObservation(
            "policy_rejected",
            candidate.name,
            candidate.service,
            candidate.identifier,
            raw_response,
            reason="candidate does not satisfy the read-only evidence policy",
        )
    if identifier is None:
        return DiagnosticObservation(
            "unsupported_identifier",
            candidate.name,
            candidate.service,
            candidate.identifier,
            raw_response,
            reason=candidate.unknown_identifier_action,
        )

    values = _response_bytes(response)
    negative_prefix = (0x7F, service)
    for index in range(len(values) - 2):
        if tuple(values[index : index + 2]) == negative_prefix:
            return DiagnosticObservation(
                "negative",
                candidate.name,
                candidate.service,
                candidate.identifier,
                raw_response,
                negative_code=values[index + 2],
                reason=candidate.negative_response_action,
            )

    positive_service = service + 0x40
    expected = (positive_service, *identifier)
    for index in range(len(values) - len(expected) + 1):
        if tuple(values[index : index + len(expected)]) != expected:
            continue
        data = values[index + len(expected) :]
        return DiagnosticObservation(
            "positive",
            candidate.name,
            candidate.service,
            candidate.identifier,
            raw_response,
            data_hex="".join(f"{value:02X}" for value in data),
        )

    return DiagnosticObservation(
        "no_positive_response",
        candidate.name,
        candidate.service,
        candidate.identifier,
        raw_response,
        reason="response did not contain the expected positive envelope",
    )


def decode_ascii(observation: DiagnosticObservation) -> str | None:
    """Decode printable ASCII payload bytes from a positive observation."""

    if observation.status != "positive" or observation.data_hex is None:
        return None
    try:
        value = bytes.fromhex(observation.data_hex).rstrip(b"\x00").decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    return value if value and all(32 <= ord(character) < 127 for character in value) else None


__all__ = ["DiagnosticObservation", "decode_ascii", "parse_response"]
