"""Validate deployment settings before creating a production firmware image."""

from __future__ import annotations

import re

_DEFINE_RE = re.compile(r"^\s*#define\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+?)\s*$")
_ENDIF_RE = re.compile(r"^#endif(?:\s*//.*)?$")
_REQUIRED_DEFINES = (
    "SERVER_HOST",
    "SERVER_PORT",
    "SERVER_PROTOCOL",
    "SERVER_PATH",
    "CELL_APN",
)


def _parse_defines(text: str) -> dict[str, str]:
    defines: dict[str, str] = {}
    guard_seen = False
    guard_depth = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if guard_seen and guard_depth == 0:
            raise ValueError(f"local_config.h:{line_number}: content after include guard")
        if not guard_seen and stripped != "#ifndef LOCAL_CONFIG_H_INCLUDED":
            raise ValueError(f"local_config.h:{line_number}: include guard must be first")
        if "/*" in line or "*/" in line:
            raise ValueError(f"local_config.h:{line_number}: block comments are not supported")
        if "\\" in line:
            raise ValueError(f"local_config.h:{line_number}: line continuations are not supported")
        if stripped == "#ifndef LOCAL_CONFIG_H_INCLUDED":
            if guard_seen:
                raise ValueError(f"local_config.h:{line_number}: duplicate include guard")
            guard_seen = True
            guard_depth = 1
            continue
        if stripped == "#define LOCAL_CONFIG_H_INCLUDED":
            if guard_depth != 1:
                raise ValueError(f"local_config.h:{line_number}: invalid include guard")
            continue
        if _ENDIF_RE.fullmatch(stripped):
            if guard_depth != 1:
                raise ValueError(f"local_config.h:{line_number}: unexpected #endif")
            guard_depth = 0
            continue
        match = _DEFINE_RE.match(line)
        if match:
            name, value = match.groups()
            if name in defines:
                raise ValueError(f"local_config.h:{line_number}: duplicate {name}")
            defines[name] = value
            continue
        if stripped.startswith("#"):
            raise ValueError(f"local_config.h:{line_number}: unsupported preprocessor directive")
        raise ValueError(f"local_config.h:{line_number}: unsupported configuration text")
    if guard_depth:
        raise ValueError("local_config.h: include guard is not closed")
    if not guard_seen:
        raise ValueError("local_config.h: include guard is required")
    return defines


def _quoted(defines: dict[str, str], name: str) -> str:
    value = defines.get(name)
    if value is None:
        raise ValueError(f"local_config.h: required {name} is missing")
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ValueError(f"local_config.h: {name} must be a quoted string")
    result = value[1:-1]
    if not result or any(character.isspace() for character in result):
        raise ValueError(f"local_config.h: {name} must be a non-empty value without whitespace")
    return result


def validate_production_config(text: str) -> None:
    """Reject deployment settings that would select a built-in fallback."""

    defines = _parse_defines(text)
    missing = [name for name in _REQUIRED_DEFINES if name not in defines]
    if missing:
        raise ValueError(f"local_config.h: required definitions are missing: {', '.join(missing)}")

    host = _quoted(defines, "SERVER_HOST")
    if host in {"hub.freematics.com", "telemetry.example.com"}:
        raise ValueError("local_config.h: SERVER_HOST still uses a fallback or example value")

    try:
        port = int(defines["SERVER_PORT"], 0)
    except ValueError as exc:
        raise ValueError("local_config.h: SERVER_PORT must be an integer") from exc
    if port != 443:
        raise ValueError("local_config.h: SERVER_PORT must be 443 for production HTTPS")

    if defines["SERVER_PROTOCOL"] != "PROTOCOL_HTTPS_POST":
        raise ValueError("local_config.h: SERVER_PROTOCOL must be PROTOCOL_HTTPS_POST")

    path = _quoted(defines, "SERVER_PATH")
    if path == "/hub/api" or not path.startswith("/"):
        raise ValueError("local_config.h: SERVER_PATH must be a deployment HTTPS path")

    apn = _quoted(defines, "CELL_APN")
    if apn in {"your-apn", "example"}:
        raise ValueError("local_config.h: CELL_APN still uses an example value")


__all__ = ["validate_production_config"]
