"""Validated, read-only vehicle diagnostic profile registry.

Profiles describe evidence-backed candidates; they are not command plans.  The
objects returned by this module intentionally contain metadata only.  In
particular, there is no transport, execution, or write operation in this API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from types import MappingProxyType
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias


_SERVICE_RE = re.compile(r"^0[xX][0-9A-Fa-f]{2}$")
_IDENTIFIER_RE = re.compile(r"^0[xX][0-9A-Fa-f]{2}(?:[0-9A-Fa-f]{2})?$")
_PID_RE = re.compile(r"^0[xX][0-9A-Fa-f]{2}$")
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_UNSAFE_SERVICE_CODES = {0x10, 0x11, 0x14, 0x27, 0x28, 0x2E, 0x2F, 0x31, 0x34, 0x36, 0x37, 0x3E, 0x85}
_SAFETY_FLAGS = (
    "read_only",
    "writes_allowed",
    "security_access_allowed",
    "clear_faults_allowed",
    "actuator_tests_allowed",
)

_UNSAFE_KEY_WORDS = ("write", "security", "actuator", "clear", "command", "execute", "session", "reset")


class ProfileValidationError(ValueError):
    """Raised when a profile is malformed or violates the read-only policy."""


# A short alias keeps callers from having to depend on the exception's longer name.
ValidationError = ProfileValidationError


@dataclass(frozen=True)
class VehicleIdentity:
    """Identity observed from a vehicle and used to gate a profile."""

    manufacturer: str
    model: str
    year: int
    vin: str | None = None
    engine_code: str | None = None
    ecu_address: str | None = None


@dataclass(frozen=True)
class ProfileIdentity:
    """Identity and required evidence declared by a profile."""

    profile_id: str
    manufacturer: str
    model: str
    model_years: tuple[int, int]
    vehicle_identity_required: tuple[str, ...]


@dataclass(frozen=True)
class ConnectorBus:
    """A connector bus description retained as evidence, not a command route."""

    name: str
    pins: Mapping[str, int]
    nominal_bitrate: int | None
    evidence: str


@dataclass(frozen=True)
class StandardService:
    """A standard diagnostic service exposed by a validated profile."""

    service: str
    name: str
    mode: str | None
    pid: str | None
    optional: bool
    enabled: bool
    evidence: str | None
    provenance: str | None


@dataclass(frozen=True)
class ActivationRequirements:
    """Evidence and stop-policy requirements for candidate probing."""

    requires_positive_supported_response: bool
    requires_capture_of_raw_response: bool
    unknown_identifier_action: str
    negative_response_action: str


@dataclass(frozen=True)
class CandidateReadOperation:
    """Read-only metadata for an identity-gated manufacturer candidate.

    This record deliberately has no callable or transport member.  A missing
    identifier denotes a family candidate; callers must apply the recorded
    unknown-identifier stop policy rather than guessing an identifier.
    """

    service: str
    name: str
    identifier: str | None
    status: str
    read_only: bool
    identity_gated: bool
    unknown_identifier_action: str
    requires_positive_supported_response: bool
    requires_capture_of_raw_response: bool
    negative_response_action: str
    enabled: bool
    evidence: str | None
    provenance: str | None


@dataclass(frozen=True)
class VehicleProfile:
    """Complete validated profile with standard and experimental records separate."""

    identity: ProfileIdentity
    status: str
    safety: Mapping[str, bool]
    connector_buses: tuple[ConnectorBus, ...]
    standard_services: tuple[StandardService, ...]
    manufacturer_candidates: tuple[CandidateReadOperation, ...]
    activation: ActivationRequirements
    evidence: str | None = None
    provenance: str | None = None

    @property
    def profile_id(self) -> str:
        return self.identity.profile_id

    @property
    def manufacturer(self) -> str:
        return self.identity.manufacturer

    @property
    def model(self) -> str:
        return self.identity.model

    @property
    def model_years(self) -> tuple[int, int]:
        return self.identity.model_years

    @property
    def gmlan_candidates(self) -> tuple[CandidateReadOperation, ...]:
        return self.manufacturer_candidates


IdentityInput: TypeAlias = VehicleIdentity | Mapping[str, object]


def _error(path: str, message: str) -> ProfileValidationError:
    return ProfileValidationError(f"{path}: {message}")


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(path, "expected an object")
    return value


def _text(value: object, path: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _error(path, "expected a non-empty string")
    return value.strip()


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(path, "expected a boolean")
    return value


def _integer(value: object, path: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, "expected an integer")
    if positive and value <= 0:
        raise _error(path, "expected a positive integer")
    return value


def _service(value: object, path: str) -> str:
    service = _text(value, path)
    assert service is not None
    if not _SERVICE_RE.fullmatch(service):
        raise _error(path, "expected a hexadecimal service such as 0x22")
    if int(service, 16) in _UNSAFE_SERVICE_CODES:
        raise _error(path, "service is not permitted by the read-only policy")
    return service


def _pid(value: object, path: str) -> str | None:
    pid = _text(value, path, required=False)
    if pid is not None and not _PID_RE.fullmatch(pid):
        raise _error(path, "expected a 2-digit hexadecimal PID")
    return pid


def _identifier(value: object, path: str) -> str | None:
    identifier = _text(value, path, required=False)
    if identifier is not None and not _IDENTIFIER_RE.fullmatch(identifier):
        raise _error(path, "expected a 2- or 4-digit hexadecimal identifier")
    return identifier


def _token(value: object, path: str) -> str:
    text = _text(value, path)
    assert text is not None
    if not _TOKEN_RE.fullmatch(text):
        raise _error(path, "expected an identifier-like name")
    return text


def _evidence(record: Mapping[str, Any], path: str) -> tuple[str | None, str | None]:
    return (
        _text(record.get("evidence"), f"{path}.evidence", required=False),
        _text(record.get("provenance"), f"{path}.provenance", required=False),
    )


def _load_data(path: str | Path) -> Mapping[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeError) as exc:
        raise _error("profile", f"cannot read profile: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _error("profile", f"invalid JSON: {exc.msg}") from exc
    return _mapping(data, "profile")




def _reject_unsafe_metadata(record: Mapping[str, Any], path: str) -> None:
    for key, value in record.items():
        if not isinstance(key, str) or not any(word in key.casefold() for word in _UNSAFE_KEY_WORDS):
            continue
        if value is True:
            raise _error(f"{path}.{key}", "unsafe capability must not be enabled")
        if value is not False and value is not None:
            raise _error(f"{path}.{key}", "unsafe capability metadata must be boolean false")

def _parse_safety(data: object) -> Mapping[str, bool]:
    safety = _mapping(data, "safety")
    parsed: dict[str, bool] = {}
    for flag in _SAFETY_FLAGS:
        if flag not in safety:
            raise _error(f"safety.{flag}", "required read-only safety flag is missing")
        parsed[flag] = _boolean(safety[flag], f"safety.{flag}")
    if not parsed["read_only"]:
        raise _error("safety.read_only", "must be true")
    for flag in _SAFETY_FLAGS[1:]:
        if parsed[flag]:
            raise _error(f"safety.{flag}", "must be false for a read-only profile")
    for key, value in safety.items():
        if key in parsed:
            continue
        if not isinstance(key, str) or not isinstance(value, bool):
            raise _error(f"safety.{key}", "unknown safety flags must be boolean")
        if value and any(word in key.lower() for word in ("write", "security", "clear", "actuator", "command", "session")):
            raise _error(f"safety.{key}", "unsafe capability must not be enabled")
    return parsed


def _parse_connector_buses(data: object) -> tuple[ConnectorBus, ...]:
    if not isinstance(data, list) or not data:
        raise _error("connector_buses", "expected a non-empty array")
    buses: list[ConnectorBus] = []
    for index, raw_bus in enumerate(data):
        path = f"connector_buses[{index}]"
        bus = _mapping(raw_bus, path)
        name = _token(bus.get("name"), f"{path}.name")
        raw_pins = _mapping(bus.get("pins"), f"{path}.pins")
        if not raw_pins:
            raise _error(f"{path}.pins", "expected at least one pin")
        pins: dict[str, int] = {}
        for pin_name, pin_number in raw_pins.items():
            if not isinstance(pin_name, str) or not _TOKEN_RE.fullmatch(pin_name):
                raise _error(f"{path}.pins", "pin names must be identifier-like")
            pins[pin_name] = _integer(pin_number, f"{path}.pins.{pin_name}", positive=True)
        bitrate = bus.get("nominal_bitrate")
        if bitrate is not None:
            bitrate = _integer(bitrate, f"{path}.nominal_bitrate", positive=True)
        evidence = _text(bus.get("evidence"), f"{path}.evidence")
        assert evidence is not None
        buses.append(ConnectorBus(name, MappingProxyType(dict(pins)), bitrate, evidence))
    return tuple(buses)


def _parse_standard_services(data: object) -> tuple[StandardService, ...]:
    if not isinstance(data, list) or not data:
        raise _error("standard_services", "expected a non-empty array")
    services: list[StandardService] = []
    for index, raw_service in enumerate(data):
        path = f"standard_services[{index}]"
        service_data = _mapping(raw_service, path)
        _reject_unsafe_metadata(service_data, path)
        service = _service(service_data.get("service"), f"{path}.service")
        name = _token(service_data.get("name"), f"{path}.name")
        mode = _text(service_data.get("mode"), f"{path}.mode", required=False)
        pid = _pid(service_data.get("pid"), f"{path}.pid")
        optional = _boolean(service_data.get("optional", False), f"{path}.optional")
        enabled = _boolean(service_data.get("enabled"), f"{path}.enabled")
        evidence, provenance = _evidence(service_data, path)
        services.append(StandardService(service, name, mode, pid, optional, enabled, evidence, provenance))
    return tuple(services)


def _parse_activation(data: object) -> ActivationRequirements:
    activation = _mapping(data, "activation")
    positive = _boolean(activation.get("requires_positive_supported_response"), "activation.requires_positive_supported_response")
    capture = _boolean(activation.get("requires_capture_of_raw_response"), "activation.requires_capture_of_raw_response")
    if not positive or not capture:
        raise _error("activation", "positive support and raw-response capture are required")
    unknown = _text(activation.get("unknown_identifier_action"), "activation.unknown_identifier_action")
    negative = _text(activation.get("negative_response_action"), "activation.negative_response_action")
    assert unknown is not None and negative is not None
    if unknown != "record_and_stop":
        raise _error("activation.unknown_identifier_action", "must be record_and_stop")
    if negative != "record_capability_and_backoff":
        raise _error("activation.negative_response_action", "must be record_capability_and_backoff")
    return ActivationRequirements(positive, capture, unknown, negative)


def _parse_candidates(data: object, activation: ActivationRequirements) -> tuple[CandidateReadOperation, ...]:
    if not isinstance(data, list):
        raise _error("gmlan_candidates", "expected an array")
    candidates: list[CandidateReadOperation] = []
    for index, raw_candidate in enumerate(data):
        path = f"gmlan_candidates[{index}]"
        candidate = _mapping(raw_candidate, path)
        _reject_unsafe_metadata(candidate, path)
        service = _service(candidate.get("service"), f"{path}.service")
        name = _token(candidate.get("name"), f"{path}.name")
        identifier = _identifier(candidate.get("identifier"), f"{path}.identifier")
        status = _text(candidate.get("status"), f"{path}.status")
        assert status is not None
        if status != "experimental":
            raise _error(f"{path}.status", "manufacturer candidates must be experimental")
        read_only = _boolean(candidate.get("read_only", True), f"{path}.read_only")
        if not read_only:
            raise _error(f"{path}.read_only", "must be true")
        identity_gated = _boolean(candidate.get("identity_gated", True), f"{path}.identity_gated")
        if not identity_gated:
            raise _error(f"{path}.identity_gated", "must be true")
        enabled = _boolean(candidate.get("enabled", True), f"{path}.enabled")
        candidate_unknown_action = _text(candidate.get("unknown_identifier_action"), f"{path}.unknown_identifier_action", required=False)
        if candidate_unknown_action is not None and candidate_unknown_action != activation.unknown_identifier_action:
            raise _error(f"{path}.unknown_identifier_action", "must match the profile stop policy")
        evidence, provenance = _evidence(candidate, path)
        candidates.append(
            CandidateReadOperation(
                service=service,
                name=name,
                identifier=identifier,
                status=status,
                read_only=read_only,
                identity_gated=identity_gated,
                unknown_identifier_action=activation.unknown_identifier_action,
                requires_positive_supported_response=activation.requires_positive_supported_response,
                requires_capture_of_raw_response=activation.requires_capture_of_raw_response,
                negative_response_action=activation.negative_response_action,
                enabled=enabled,
                evidence=evidence,
                provenance=provenance,
            )
        )
    return tuple(candidates)


def load_profile(path: str | Path) -> VehicleProfile:
    """Load and validate one JSON profile under the read-only policy."""

    data = _load_data(path)
    profile_id = _text(data.get("profile_id"), "profile_id")
    manufacturer = _text(data.get("manufacturer"), "manufacturer")
    model = _text(data.get("model"), "model")
    status = _text(data.get("status"), "status")
    assert profile_id is not None and manufacturer is not None and model is not None and status is not None
    if status != "identity-gated":
        raise _error("status", "must be identity-gated")

    years = data.get("model_years")
    if not isinstance(years, list) or len(years) != 2:
        raise _error("model_years", "expected [first_year, last_year]")
    first_year = _integer(years[0], "model_years[0]")
    last_year = _integer(years[1], "model_years[1]")
    if first_year <= 0 or last_year < first_year:
        raise _error("model_years", "must be a positive ascending range")

    required = data.get("vehicle_identity_required")
    if not isinstance(required, list) or not required:
        raise _error("vehicle_identity_required", "expected a non-empty array")
    required_fields: list[str] = []
    for index, field in enumerate(required):
        field_name = _token(field, f"vehicle_identity_required[{index}]")
        if field_name in required_fields:
            raise _error(f"vehicle_identity_required[{index}]", "duplicate field")
        required_fields.append(field_name)

    safety = _parse_safety(data.get("safety"))
    activation = _parse_activation(data.get("activation"))
    evidence = _text(data.get("evidence"), "evidence", required=False)
    provenance = _text(data.get("provenance"), "provenance", required=False)
    identity = ProfileIdentity(profile_id, manufacturer, model, (first_year, last_year), tuple(required_fields))
    return VehicleProfile(
        identity=identity,
        status=status,
        safety=MappingProxyType(dict(safety)),
        connector_buses=_parse_connector_buses(data.get("connector_buses")),
        standard_services=_parse_standard_services(data.get("standard_services")),
        manufacturer_candidates=_parse_candidates(data.get("gmlan_candidates"), activation),
        activation=activation,
        evidence=evidence,
        provenance=provenance,
    )


def _identity_values(identity: IdentityInput) -> Mapping[str, object] | None:
    if isinstance(identity, VehicleIdentity):
        return {
            "manufacturer": identity.manufacturer,
            "model": identity.model,
            "year": identity.year,
            "vin": identity.vin,
            "engine_code": identity.engine_code,
            "ecu_address": identity.ecu_address,
        }
    if isinstance(identity, Mapping):
        return identity
    return None


def profile_matches(profile: VehicleProfile, identity: IdentityInput) -> bool:
    """Return whether identity and all profile-required evidence are present."""

    values = _identity_values(identity)
    if values is None:
        return False
    manufacturer = values.get("manufacturer")
    model = values.get("model")
    year = values.get("year", values.get("model_year"))
    if not isinstance(manufacturer, str) or not isinstance(model, str):
        return False
    if manufacturer.strip().casefold() != profile.identity.manufacturer.casefold():
        return False
    if model.strip().casefold() != profile.identity.model.casefold():
        return False
    if isinstance(year, bool) or not isinstance(year, int) or not (profile.identity.model_years[0] <= year <= profile.identity.model_years[1]):
        return False
    for field in profile.identity.vehicle_identity_required:
        value = values.get(field)
        if field == "model_year" and value is None:
            value = year
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


def permitted_candidates(profile: VehicleProfile, identity: IdentityInput) -> tuple[CandidateReadOperation, ...]:
    """Return enabled, read-only candidates only after complete identity matching."""

    if not profile_matches(profile, identity):
        return ()
    return tuple(
        candidate
        for candidate in profile.manufacturer_candidates
        if candidate.enabled and candidate.read_only and candidate.identity_gated
    )


__all__ = [
    "ActivationRequirements",
    "CandidateReadOperation",
    "ConnectorBus",
    "IdentityInput",
    "ProfileIdentity",
    "ProfileValidationError",
    "StandardService",
    "ValidationError",
    "VehicleIdentity",
    "VehicleProfile",
    "load_profile",
    "permitted_candidates",
    "profile_matches",
]
