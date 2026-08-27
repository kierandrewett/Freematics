#!/usr/bin/env python3
import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telemetry_git_mirror import TelemetryGitMirror, parse_frames
from telemetry_catalog import metric_catalog, readable_metrics


def with_checksum(payload: str) -> bytes:
    return f"{payload}*{sum(payload.encode()) & 0xFF:X}".encode()


class TelemetryGitMirrorTests(unittest.TestCase):
    def test_readable_metrics_names_units_and_decodes_device_fields(self) -> None:
        metrics = readable_metrics(
            [
                {"pid": "0x10C", "value": "2400"},
                {"pid": "0x10D", "value": "48"},
                {"pid": "0x024", "value": "1364"},
                {"pid": "0x020", "value": "0.10;-0.02;1.01"},
                {"pid": "0x084", "value": "2"},
                {"pid": "0x085", "value": "6"},
                {"pid": "0x089", "value": "2"},
                {"pid": "0x08C", "value": "128"},
                {"pid": "0x310", "value": "2"},
            ]
        )
        self.assertEqual(metrics["engine_rpm"]["label"], "Engine speed")
        self.assertEqual(metrics["engine_rpm"]["unit"], "rpm")
        self.assertEqual(metrics["engine_rpm"]["value"], 2400)
        self.assertEqual(metrics["device_battery_voltage"]["value"], 13.64)
        self.assertEqual(metrics["acceleration"]["value"]["x"], 0.1)
        self.assertEqual(metrics["network_transport"]["value"], {"code": 2, "name": "cellular"})
        self.assertEqual(metrics["obd_state"]["value"], {"code": 2, "name": "degraded"})
        self.assertEqual(metrics["queue_bytes"]["value"], 128)
        self.assertEqual(metrics["stored_dtc_read_status"]["value"], {"code": 2, "name": "codes"})
        self.assertEqual(metrics["obd_protocol"]["value"], {"code": 6, "name": "iso15765_11bit_500k"})

    def test_catalog_covers_shared_standard_odometer_and_unknown_fields(self) -> None:
        catalog = metric_catalog()
        self.assertEqual(catalog[0x1A6].key, "odometer")
        metrics = readable_metrics([{"pid": "0x1A6", "value": "123456.7"}, {"pid": "0x7EE", "value": "abc"}])
        self.assertEqual(metrics["odometer"]["value"], 123456.7)
        self.assertEqual(metrics["pid_7EE"]["namespace"], "unknown")

    def test_duplicate_named_pid_keeps_each_occurrence(self) -> None:
        metrics = readable_metrics([{"pid": "0x10C", "value": "1000"}, {"pid": "0x10C", "value": "1100"}])
        self.assertEqual([item["value"] for item in metrics["engine_rpm"]["occurrences"]], [1000, 1100])

    def test_parser_accepts_equals_delimiter(self) -> None:
        payload = with_checksum("0=100,10=12345600,100=1")
        frames = parse_frames(payload)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].device_monotonic_ms, 100)
        self.assertEqual(frames[0].fields[0]["pid"], "0x010")
        self.assertTrue(frames[0].checksum_valid)

    def test_same_size_source_mutation_is_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27"
            archive.mkdir(parents=True)
            path = archive / "20260827-120000.txt"
            path.write_bytes(b"0:100,100:1\n")
            repo = Path(directory) / "repo"
            state = Path(directory) / "state"
            mirror = TelemetryGitMirror(root, repo, state, git_push=False, now_ms=lambda: 1_000)
            self.assertEqual(mirror.run_once(force_flush=True), (2, True))

            path.write_bytes(b"0:100,100:2\n")
            self.assertEqual(mirror.scan_once(), 2)
            pending = (state / "pending.jsonl").read_text()
            self.assertIn('"raw_frame":"0:100,100:2"', pending)


    def test_partial_line_is_held_until_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27"
            archive.mkdir(parents=True)
            path = archive / "20260827-120000.txt"
            path.write_bytes(b"0:100,100:1")
            repo = Path(directory) / "repo"
            mirror = TelemetryGitMirror(root, repo, Path(directory) / "state", git_push=False, now_ms=lambda: 1_000)
            self.assertEqual(mirror.scan_once(), 0)
            path.write_bytes(path.read_bytes() + b"\n")
            self.assertEqual(mirror.scan_once(), 2)  # batch + sample
            self.assertEqual(len((Path(directory) / "state" / "pending.jsonl").read_text().splitlines()), 2)

    def test_flush_is_idempotent_and_robot_authored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27"
            archive.mkdir(parents=True)
            (archive / "20260827-120000.txt").write_bytes(with_checksum("0:100,100:1") + b"\n")
            repo = Path(directory) / "repo"
            state = Path(directory) / "state"
            mirror = TelemetryGitMirror(root, repo, state, git_push=False, now_ms=lambda: 1_000)
            self.assertEqual(mirror.run_once(force_flush=True), (2, True))
            self.assertEqual(mirror.run_once(force_flush=True), (0, False))
            files = list((repo / "events").rglob("*.jsonl"))
            self.assertEqual(len(files), 1)
            records = [json.loads(line) for line in files[0].read_text().splitlines()]
            self.assertEqual({record["record_type"] for record in records}, {"batch", "sample"})
            sample = next(record for record in records if record["record_type"] == "sample")
            self.assertEqual(sample["metrics"]["pid_100"]["value"], "1")
            author = subprocess.check_output(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>"], text=True).strip()
            self.assertEqual(author, "Freematics Telemetry Bot <freematics-telemetry-bot@local.invalid>")

    def test_failed_push_keeps_spool_and_retries_after_local_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = root / "CAR" / "2026" / "08" / "27"
            archive.mkdir(parents=True)
            (archive / "20260827-120000.txt").write_bytes(with_checksum("0:100,100:1") + b"\n")
            repo = Path(directory) / "repo"
            state = Path(directory) / "state"
            mirror = TelemetryGitMirror(root, repo, state, git_push=True, now_ms=lambda: 1_000)
            mirror.scan_once()
            with patch.object(mirror, "_push", side_effect=subprocess.CalledProcessError(1, ["git", "push"])):
                self.assertTrue(mirror.flush(force=True))
            self.assertEqual((state / "pending.jsonl").read_text(), "")
            with patch.object(mirror, "_push") as push:
                self.assertFalse(mirror.flush(force=True))
                push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
