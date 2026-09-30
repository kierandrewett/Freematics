#!/usr/bin/env python3
"""Report sample and per-PID coverage, including held sensor value ages."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
from history_indexer import monotonic_delta


def report(connection, device, trip, required=(), max_gap_ms=375):
    samples = connection.execute(
        'SELECT sequence, device_monotonic_ms FROM sample WHERE device_id=? AND trip_id=? ORDER BY sequence',
        (device, trip),
    ).fetchall()
    if not samples:
        raise ValueError('Trip has no indexed samples')
    fields = connection.execute(
        'SELECT sequence, pid, numeric_value FROM sample_metric WHERE device_id=? AND trip_id=?',
        (device, trip),
    ).fetchall()
    by_pid = {}
    for sequence, pid, value in fields:
        by_pid.setdefault(pid, {})[sequence] = value
    times = dict(samples)
    intervals = [monotonic_delta(b[1], a[1]) for a, b in zip(samples, samples[1:])]
    resets = sum(delta < 0 for delta in intervals)
    max_sample_gap = max([0, *intervals])
    metric_rows = []
    for pid in sorted(set(by_pid) | set(required)):
        values = by_pid.get(pid, {})
        present = [sequence for sequence, _ in samples if sequence in values]
        gaps = [monotonic_delta(times[b], times[a]) for a, b in zip(present, present[1:])]
        if present:
            gaps += [monotonic_delta(times[present[0]], samples[0][1]), monotonic_delta(samples[-1][1], times[present[-1]])]
        else:
            gaps = [monotonic_delta(samples[-1][1], samples[0][1])]
        number = int(pid, 16)
        age_pid = f'0x{0x400 | (number & 0xFF):03X}' if 0x100 <= number <= 0x1FF else {
            0x0A: '0x093', 0x0B: '0x093', 0x0C: '0x093', 0x0D: '0x093', 0x0E: '0x093',
            0x0F: '0x093', 0x10: '0x093', 0x11: '0x093', 0x12: '0x093',
            0x20: '0x095', 0x21: '0x095', 0x22: '0x095', 0x25: '0x095', 0x24: '0x094',
            0x81: '0x096',
        }.get(number)
        ages = [v for v in by_pid.get(age_pid, {}).values() if v is not None] if age_pid else []
        metric_rows.append({
            'pid': pid, 'present_samples': len(present), 'missing_samples': len(samples) - len(present),
            'max_gap_ms': max([0, *gaps]), 'age_pid': age_pid,
            'max_value_age_ms': max(ages) if ages else None,
            'held_samples': sum(age > 0 for age in ages),
        })
    return {
        'device': device, 'trip': trip, 'samples': len(samples),
        'max_sample_gap_ms': max_sample_gap, 'clock_resets': resets,
        'coverage_ok': not resets and max_sample_gap <= max_gap_ms and all(
            row['missing_samples'] == 0 and row['max_gap_ms'] <= max_gap_ms for row in metric_rows),
        'metrics': metric_rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--device', default='ZKUCALJ0')
    parser.add_argument('--trip', required=True)
    parser.add_argument('--max-gap-ms', default=375, type=int, help='250 ms cadence plus 125 ms tolerance')
    parser.add_argument('--require-pid', action='append', default=[], help='Also check a PID that may be completely absent, e.g. 0x10C')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    required = [f'0x{int(pid, 0):03X}' for pid in args.require_pid]
    with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        result = report(connection, args.device, args.trip, required, args.max_gap_ms)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['samples']} samples; largest sample gap {result['max_sample_gap_ms']} ms; clock resets {result['clock_resets']}")
        print('PID       missing   max gap ms   max value age ms')
        for row in result['metrics']:
            age = row['max_value_age_ms']
            print(f"{row['pid']:8} {row['missing_samples']:7} {row['max_gap_ms']:12} {str(age) if age is not None else '-':>18}")
        print('Coverage: ' + ('PASS' if result['coverage_ok'] else 'FAIL'))
        print('Value age distinguishes held values from new measurements; coverage alone does not prove freshness.')
    return 0 if result['coverage_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
