#!/usr/bin/env python3
"""Generate a credential-free compile configuration for OTA release builds."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_config import _parse_defines


# Values are constrained per key as well as lexically. A permissive identifier
# check alone could allow a different, valid C++ identifier to change meaning.
_IDENTIFIER_VALUES = {
    "GNSS": frozenset({"GNSS_NONE", "GNSS_STANDALONE", "GNSS_CELLULAR"}),
    "STORAGE": frozenset({"STORAGE_NONE", "STORAGE_SPIFFS", "STORAGE_SD"}),
    "SERVER_PROTOCOL": frozenset(
        {"PROTOCOL_UDP", "PROTOCOL_HTTPS_GET", "PROTOCOL_HTTPS_POST"}
    ),
}
_BOOLEAN_KEYS = frozenset(
    {
        "ENABLE_OBD",
        "ENABLE_MEMS",
        "ENABLE_WIFI",
        "ENABLE_BLE",
        "ENABLE_HTTPD",
        "PREFER_CELLULAR",
    }
)
_INTEGER_KEYS = frozenset({"SERVER_PORT"})
_ALLOWLIST = _BOOLEAN_KEYS | _INTEGER_KEYS | frozenset(_IDENTIFIER_VALUES)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_INTEGER_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_HEADER_GUARD = "FREEMATICS_OTA_RELEASE_CONFIG_H_INCLUDED"


def _safe_value(name: str, raw: str) -> str:
    """Validate and normalize a selected macro without evaluating C++ text."""
    value = raw.strip()
    if name in _BOOLEAN_KEYS:
        if value not in {"0", "1"}:
            raise ValueError(f"local_config.h: {name} must be 0 or 1")
        return value
    if name in _INTEGER_KEYS:
        if not _INTEGER_RE.fullmatch(value):
            raise ValueError(f"local_config.h: {name} must be a decimal integer")
        number = int(value, 10)
        if not 1 <= number <= 65535:
            raise ValueError(f"local_config.h: {name} is outside the valid port range")
        return str(number)
    allowed = _IDENTIFIER_VALUES[name]
    if not _IDENTIFIER_RE.fullmatch(value) or value not in allowed:
        raise ValueError(f"local_config.h: {name} is not an allowed identifier")
    return value


def generate_header(config_text: str) -> str:
    """Return a C++ header containing only validated non-private settings."""
    defines = _parse_defines(config_text)
    selected = [(name, _safe_value(name, defines[name]))
                for name in sorted(_ALLOWLIST) if name in defines]
    lines = [f"#ifndef {_HEADER_GUARD}", f"#define {_HEADER_GUARD}", ""]
    lines.extend(f"#define {name} {value}" for name, value in selected)
    lines.extend(["", "#endif", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="private local_config.h input")
    parser.add_argument("output", type=Path, help="generated header output path")
    args = parser.parse_args()
    try:
        header = generate_header(args.config.read_text(encoding="utf-8"))
        args.output.write_text(header, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"OTA release config generation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Generated credential-free OTA config header: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
