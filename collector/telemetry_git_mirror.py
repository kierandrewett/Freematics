#!/usr/bin/env python3
"""Mirror Freematics raw uploads to an append-only JSONL Git repository.

The collector's ``data/<device>/<year>/<month>/<day>/*.txt`` files remain the
canonical, lossless archive.  This process is deliberately a *secondary* view:
it tails complete newline-delimited uploads, writes a durable local spool, and
publishes immutable JSONL segments in bounded commits.  A GitHub outage can
therefore never block ingestion or make the device lose data.

Each upload produces one ``batch`` record containing the exact payload bytes
(base64) and one ``sample`` record per PID-0-delimited frame.  The sample keeps
the exact frame bytes as well as an ordered field list, so duplicate PIDs and
unknown fields are not silently discarded.  ``mirror_observed_at`` is the time
this sidecar saw the archive; it is *not* claimed to be collector receipt time.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    from telemetry_catalog import metric_values, readable_metrics
except ImportError:  # pragma: no cover - supports package imports
    from .telemetry_catalog import metric_values, readable_metrics


SCHEMA_VERSION = 2
MAX_SEGMENT_RECORDS = 5_000
TRIP_RE = re.compile(r"^\d{8}-\d{6}$")
FRAME_RE = re.compile(rb"(?:^|,)0[:=](\d+)(?=,|$)", re.MULTILINE)
FIELD_RE = re.compile(rb"(?:^|,)([0-9A-Fa-f]+)[:=]([^,\r\n]*)")


@dataclass(frozen=True)
class SourceFrame:
    raw: bytes
    device_monotonic_ms: int
    fields: list[dict[str, str]]
    checksum_hex: str | None
    checksum_valid: bool | None


def canonical_pid(raw_pid: bytes) -> str:
    return f"0x{int(raw_pid, 16):03X}"


def checksum_status(raw: bytes) -> tuple[str | None, bool | None]:
    star = raw.rfind(b"*")
    if star < 0:
        return None, None
    supplied = raw[star + 1 :].decode("ascii", errors="replace")
    try:
        expected = int(supplied, 16)
    except ValueError:
        return supplied, False
    actual = sum(raw[:star]) & 0xFF
    return supplied, actual == expected


def parse_frames(payload: bytes) -> list[SourceFrame]:
    """Split one complete upload into exact PID-0-delimited frames."""

    starts = list(FRAME_RE.finditer(payload))
    frames: list[SourceFrame] = []
    for index, match in enumerate(starts):
        start = match.start() + (1 if payload[match.start() : match.start() + 1] == b"," else 0)
        stop = starts[index + 1].start() if index + 1 < len(starts) else len(payload)
        raw = payload[start:stop]
        fields: list[dict[str, str]] = []
        for field in FIELD_RE.finditer(raw):
            pid = field.group(1)
            if pid.upper() == b"0":
                continue
            fields.append(
                {
                    "pid": canonical_pid(pid),
                    "value": field.group(2).decode("utf-8", errors="replace").split("*", 1)[0],
                }
            )
        checksum_hex, checksum_valid = checksum_status(raw)
        frames.append(
            SourceFrame(
                raw=raw,
                device_monotonic_ms=int(match.group(1)),
                fields=fields,
                checksum_hex=checksum_hex,
                checksum_valid=checksum_valid,
            )
        )
    return frames


def event_id(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def json_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _iso_now(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1_000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def source_identity(root: Path, archive: Path) -> tuple[str, str, str, str, str] | None:
    try:
        relative = archive.relative_to(root).as_posix()
    except ValueError:
        return None
    parts = relative.split("/")
    if len(parts) != 5 or not all(parts[1:4]):
        return None
    device, year, month, day, filename = parts
    trip_id = Path(filename).stem
    if not TRIP_RE.fullmatch(trip_id) or not (year.isdigit() and month.isdigit() and day.isdigit()):
        return None
    if not device.isascii() or not device.isalnum() or not 1 <= len(device) < 32:
        return None
    return relative, device, year, month, day + "/" + trip_id


class TelemetryGitMirror:
    """Tail raw archive files, spool records, then commit/push segments."""

    def __init__(
        self,
        archive_root: Path,
        repo: Path,
        state_dir: Path,
        *,
        flush_seconds: int = 120,
        now_ms: Callable[[], int] | None = None,
        git_push: bool = True,
        git_remote: str = "origin",
    ) -> None:
        self.archive_root = archive_root
        self.repo = repo
        self.state_dir = state_dir
        self.flush_seconds = max(1, int(flush_seconds))
        self.now_ms = now_ms or (lambda: int(time.time() * 1_000))
        self.git_push = git_push
        self.git_remote = git_remote
        self.state_path = state_dir / "cursor.json"
        self.spool_path = state_dir / "pending.jsonl"

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema": SCHEMA_VERSION, "files": {}, "last_flush_ms": 0}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": SCHEMA_VERSION, "files": {}, "last_flush_ms": 0}
        if not isinstance(value, dict) or value.get("schema") != SCHEMA_VERSION:
            return {"schema": SCHEMA_VERSION, "files": {}, "last_flush_ms": 0}
        value.setdefault("files", {})
        value.setdefault("last_flush_ms", 0)
        return value

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def _append_spool(self, records: Iterable[dict[str, Any]]) -> int:
        records = list(records)
        if not records:
            return 0
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.spool_path.open("ab") as handle:
            for record in records:
                handle.write(json_line(record))
            handle.flush()
            os.fsync(handle.fileno())
        return len(records)

    def _read_new_lines(self, archive: Path, cursor: dict[str, Any]) -> tuple[list[bytes], int, int, str]:
        raw = archive.read_bytes()
        offset = int(cursor.get("offset", 0))
        line_number = int(cursor.get("line_number", 0))
        prefix_hash = str(cursor.get("prefix_sha256", ""))
        reset = offset > len(raw)
        if not reset and offset and prefix_hash:
            reset = hashlib.sha256(raw[:offset]).hexdigest() != prefix_hash
        if not reset and offset and not prefix_hash:
            # Establish an integrity anchor for cursors written by schema 1.
            # The first replay is idempotent because event IDs include payload
            # hashes and flush de-duplicates records.
            reset = True
        if reset:
            # A collector rotation/truncation or an in-place mutation is a new
            # source revision. Replay it rather than skipping bytes.
            offset = 0
            line_number = 0
        suffix = raw[offset:]
        complete = suffix.splitlines(keepends=True)
        lines: list[bytes] = []
        consumed = 0
        for chunk in complete:
            if not chunk.endswith((b"\n", b"\r")):
                # Hold an unterminated final line until the collector closes it.
                break
            lines.append(chunk.rstrip(b"\r\n"))
            consumed += len(chunk)
            line_number += 1
        next_offset = offset + consumed
        return lines, next_offset, line_number, hashlib.sha256(raw[:next_offset]).hexdigest()

    def scan_once(self) -> int:
        """Append newly complete uploads to the durable spool."""

        state = self._read_state()
        records: list[dict[str, Any]] = []
        updates: dict[str, dict[str, Any]] = {}
        observed_at = _iso_now(self.now_ms())
        for archive in sorted(self.archive_root.glob("*/????/??/??/*.txt")):
            identity = source_identity(self.archive_root, archive)
            if identity is None:
                continue
            relative, device_id, year, month, day_trip = identity
            day, trip_id = day_trip.split("/", 1)
            cursor = dict(state.get("files", {}).get(relative, {}))
            lines, next_offset, next_line, prefix_sha256 = self._read_new_lines(archive, cursor)
            updates[relative] = {
                "offset": next_offset,
                "line_number": next_line,
                "size": archive.stat().st_size,
                "prefix_sha256": prefix_sha256,
            }
            if not lines:
                continue
            for line_index, payload in enumerate(lines, start=next_line - len(lines) + 1):
                batch_id = event_id(relative, str(line_index), hashlib.sha256(payload).hexdigest())
                frames = parse_frames(payload)
                records.append(
                    {
                        "schema": SCHEMA_VERSION,
                        "record_type": "batch",
                        "event_id": batch_id,
                        "device_id": device_id,
                        "trip_id": trip_id,
                        "archive_path": relative,
                        "line_number": line_index,
                        "mirror_observed_at": observed_at,
                        "raw_payload_sha256": hashlib.sha256(payload).hexdigest(),
                        "raw_payload_b64": base64.b64encode(payload).decode("ascii"),
                        "frame_count": len(frames),
                    }
                )
                for frame_index, frame in enumerate(frames):
                    records.append(
                        {
                            "schema": SCHEMA_VERSION,
                            "record_type": "sample",
                            "event_id": event_id(batch_id, str(frame_index), hashlib.sha256(frame.raw).hexdigest()),
                            "batch_id": batch_id,
                            "device_id": device_id,
                            "trip_id": trip_id,
                            "archive_path": relative,
                            "line_number": line_index,
                            "frame_index": frame_index,
                            "device_monotonic_ms": frame.device_monotonic_ms,
                            "fields": frame.fields,
                            "metrics": readable_metrics(frame.fields),
                            "metric_values": metric_values(frame.fields),
                            "raw_frame": frame.raw.decode("utf-8", errors="replace"),
                            "raw_frame_b64": base64.b64encode(frame.raw).decode("ascii"),
                            "checksum_hex": frame.checksum_hex,
                            "checksum_valid": frame.checksum_valid,
                            "mirror_observed_at": observed_at,
                        }
                    )
        if updates:
            state["files"].update(updates)
        if not records:
            if updates:
                self._write_state(state)
            return 0
        self._append_spool(records)
        self._write_state(state)
        return len(records)


    def _ensure_repo_metadata(self) -> None:
        self.repo.mkdir(parents=True, exist_ok=True)
        readme = self.repo / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Freematics telemetry archive\n\n"
                "Append-only, robot-authored JSONL mirror of raw Freematics uploads.\n\n"
                "The server raw archive and SQLite projection are canonical. This repository is a\n"
                "secondary, human-browseable copy. `batch` records preserve exact payload bytes;\n"
                "`sample` records preserve each PID frame and an ordered field list.\n"
                "`mirror_observed_at` is not collector receipt time. Never put credentials here.\n",
                encoding="utf-8",
            )
        schema = self.repo / "schema.json"
        if not schema.exists():
            schema.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA_VERSION,
                        "record_types": {
                            "batch": "one exact HTTP upload payload",
                            "sample": "one PID-0-delimited frame from a batch",
                        },
                        "canonical_store": "server raw archive + SQLite history projection",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def _ensure_git(self) -> None:
        if not (self.repo / ".git").exists():
            self._git("init", "-b", "main")
        name = os.environ.get("GIT_AUTHOR_NAME", "Freematics Telemetry Bot")
        email = os.environ.get("GIT_AUTHOR_EMAIL", "freematics-telemetry-bot@local.invalid")
        self._git("config", "user.name", name)
        self._git("config", "user.email", email)

    def _push(self) -> None:
        token = os.environ.get("GITHUB_TOKEN")
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        askpass_dir: tempfile.TemporaryDirectory[str] | None = None
        try:
            if token:
                askpass_dir = tempfile.TemporaryDirectory(prefix="freematics-askpass-")
                askpass = Path(askpass_dir.name) / "askpass.sh"
                askpass.write_text(
                    "#!/bin/sh\ncase \"$1\" in *Username*) echo x-access-token;; *) printf '%s' \"$GITHUB_TOKEN\";; esac\n",
                    encoding="utf-8",
                )
                askpass.chmod(0o700)
                environment["GIT_ASKPASS"] = str(askpass)
                environment["GITHUB_TOKEN"] = token
            subprocess.run(
                ["git", "push", self.git_remote, "HEAD:main"],
                cwd=str(self.repo),
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        finally:
            if askpass_dir is not None:
                askpass_dir.cleanup()

    def _maybe_push(self, state: dict[str, Any], now: int, *, force: bool) -> bool:
        if not self.git_push or not (self.repo / ".git").exists():
            return False
        last_attempt = int(state.get("last_push_attempt_ms", 0))
        if not force and now - last_attempt < self.flush_seconds * 1_000:
            return False
        state["last_push_attempt_ms"] = now
        self._write_state(state)
        try:
            self._push()
        except (OSError, subprocess.SubprocessError) as error:
            # A GitHub outage must never stop ingestion.  The local commit is
            # already durable; the next scheduled attempt will push it.
            # Avoid echoing command stderr because it may contain a remote URL.
            print(f"[TELEMETRY-GIT] push deferred ({type(error).__name__})", file=sys.stderr, flush=True)
            return False
        return True

    def flush(self, *, force: bool = False) -> bool:
        if not self.spool_path.exists() or self.spool_path.stat().st_size == 0:
            state = self._read_state()
            self._maybe_push(state, self.now_ms(), force=force)
            return False
        state = self._read_state()
        now = self.now_ms()
        if not force and now - int(state.get("last_flush_ms", 0)) < self.flush_seconds * 1_000:
            return False
        pending: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        # A crash can occur after the spool fsync but before the cursor write.
        # De-duplicate that replay by deterministic event_id before creating a
        # segment; the exact payload is still present in the first record.
        with self.spool_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                record_id = str(record.get("event_id", ""))
                if record_id and record_id in seen_ids:
                    continue
                if record_id:
                    seen_ids.add(record_id)
                pending.append(record)
        if not pending:
            return False
        self._ensure_repo_metadata()
        self._ensure_git()
        grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for record in pending:
            archive_path = str(record["archive_path"])
            parts = archive_path.split("/")
            if len(parts) != 5:
                continue
            key = (parts[0], parts[1], parts[2], parts[3], str(record["trip_id"]))
            grouped.setdefault(key, []).append(record)
        for (device, year, month, day, trip_id), records in grouped.items():
            for segment_number in range(0, len(records), MAX_SEGMENT_RECORDS):
                segment = records[segment_number : segment_number + MAX_SEGMENT_RECORDS]
                first_id = str(segment[0]["event_id"])[:16]
                last_id = str(segment[-1]["event_id"])[:16]
                target = self.repo / "events" / device / year / month / day / trip_id / f"part-{first_id}-{last_id}.jsonl"
                target.parent.mkdir(parents=True, exist_ok=True)
                content = b"".join(json_line(record) for record in segment)
                if target.exists():
                    if target.read_bytes() != content:
                        raise RuntimeError(f"immutable telemetry segment collision: {target}")
                else:
                    target.write_bytes(content)
        self._git("add", "README.md", "schema.json", "events")
        staged = self._git("diff", "--cached", "--name-only").stdout.strip()
        if not staged:
            # A crash after a local commit can leave the spool behind.  The
            # deterministic segment check above makes clearing it safe.  A
            # previous push may also have failed after its local commit; retry
            # that push before acknowledging the spool.
            self._maybe_push(state, now, force=True)
            self.spool_path.write_bytes(b"")
            state["last_flush_ms"] = now
            self._write_state(state)
            return False
        count = len(pending)
        self._git("commit", "-m", f"telemetry: publish {count} raw records")
        self.spool_path.write_bytes(b"")
        state["last_flush_ms"] = now
        self._write_state(state)
        # A local commit is the durable acknowledgement.  Push afterwards so
        # a transient GitHub outage cannot cause the same records to be
        # re-emitted alongside the next batch; the next cycle retries the
        # already-created local commit.
        self._maybe_push(state, now, force=True)
        return True

    def run_once(self, *, force_flush: bool = False) -> tuple[int, bool]:
        records = self.scan_once()
        published = self.flush(force=force_flush)
        return records, published


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--flush-seconds", type=int, default=120)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    mirror = TelemetryGitMirror(
        args.archive_root,
        args.repo,
        args.state_dir,
        flush_seconds=args.flush_seconds,
        git_push=not args.no_push,
        git_remote=args.remote,
    )
    while True:
        records, published = mirror.run_once(force_flush=args.once)
        print(f"[TELEMETRY-GIT] records={records} published={int(published)}", flush=True)
        if args.once:
            return
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
