#!/usr/bin/env python3
"""Add the readable metric projection to an existing archive in place.

The migration is intentionally additive: event IDs, raw payload/frame bytes,
ordered fields, and every other existing key are preserved.  A temporary file
and atomic replace are used for each JSONL segment, so an interrupted run does
not leave a half-written segment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

try:
    from telemetry_catalog import metric_values, readable_metrics
except ImportError:  # pragma: no cover - supports package imports
    from .telemetry_catalog import metric_values, readable_metrics


SCHEMA_VERSION = 2


def migrate_file(path: Path) -> tuple[int, int]:
    changed = 0
    records = 0
    output: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record: dict[str, Any] = json.loads(line)
            records += 1
            if record.get("record_type") == "sample":
                fields = record.get("fields")
                if not isinstance(fields, list):
                    raise ValueError(f"sample without ordered fields: {path}")
                if record.get("metrics") != readable_metrics(fields) or record.get("metric_values") != metric_values(fields):
                    record["metrics"] = readable_metrics(fields)
                    record["metric_values"] = metric_values(fields)
                    changed += 1
            if record.get("schema") != SCHEMA_VERSION:
                record["schema"] = SCHEMA_VERSION
                changed += 1
            output.append(json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    if changed:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    return records, changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="archive repository root")
    args = parser.parse_args()
    total_records = 0
    changed_files = 0
    changed_records = 0
    for path in sorted(args.archive.glob("events/**/*.jsonl")):
        records, changed = migrate_file(path)
        total_records += records
        if changed:
            changed_files += 1
            changed_records += changed
    print(f"records={total_records} changed_files={changed_files} changed_records={changed_records}")


if __name__ == "__main__":
    main()
