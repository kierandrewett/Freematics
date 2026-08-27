#!/usr/bin/env python3
import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telemetry_git_mirror import TelemetryGitMirror, parse_frames


def with_checksum(payload: str) -> bytes:
    return f"{payload}*{sum(payload.encode()) & 0xFF:X}".encode()


class TelemetryGitMirrorTests(unittest.TestCase):
    def test_parser_preserves_multiple_frames_and_checksum(self) -> None:
        first = with_checksum("0:100,10:12345600,100:1")
        second = with_checksum("0:200,100:2,100:3")
        frames = parse_frames(first + b"," + second)
        self.assertEqual([frame.device_monotonic_ms for frame in frames], [100, 200])
        self.assertEqual([field["value"] for field in frames[1].fields], ["2", "3"])
        self.assertTrue(frames[0].checksum_valid)
        self.assertEqual(base64.b64decode(base64.b64encode(frames[0].raw)), frames[0].raw)

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
            with patch.object(mirror, "_push", side_effect=RuntimeError("temporary GitHub outage")):
                with self.assertRaisesRegex(RuntimeError, "temporary GitHub outage"):
                    mirror.flush(force=True)
            self.assertTrue((state / "pending.jsonl").read_text().strip())
            with patch.object(mirror, "_push") as push:
                self.assertFalse(mirror.flush(force=True))
                push.assert_called_once()
            self.assertEqual((state / "pending.jsonl").read_text(), "")


if __name__ == "__main__":
    unittest.main()
