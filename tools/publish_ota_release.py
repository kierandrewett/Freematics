#!/usr/bin/env python3
"""Upload only a freshly verified OTA asset pair to an existing GitHub release."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import sys
import tempfile

from package_ota_release import (
    firmware_release_version,
    firmware_source_commit,
    verify_release_directory,
)


REPOSITORY = "kierandrewett/Freematics"
EVIDENCE_SCHEMA_VERSION = 1
REQUIRED_HARDWARE_TESTS = frozenset({
    "boot_identity",
    "sd_journal_readback_replay",
    "upload_continuity",
    "live_usb_telemetry_capture_ages",
    "dashboard_disconnect_recording_continues",
    "car_off_60_minute_gate",
    "motion_cancellation",
    "first_boot_acceptance",
    "rollback",
})


def _validate_hardware_evidence(path: Path, image: Path, source_commit: str) -> None:
    """Fail closed on private, owner-only, exact-schema hardware acceptance evidence."""
    try:
        path_info = path.lstat()
    except OSError:
        raise ValueError("hardware evidence file is unavailable") from None
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise ValueError("hardware evidence must be a regular, non-symlink file")
    try:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("platform cannot safely open hardware evidence without following symlinks")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
            os.close(fd)
            raise ValueError("hardware evidence changed while being opened")
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise ValueError("hardware evidence must be a regular, non-symlink file")
        if (os.name != "posix" or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            os.close(fd)
            raise ValueError("hardware evidence must be owned by the current user with mode 0600")
        with os.fdopen(fd, "rb") as evidence_file:
            raw = evidence_file.read(65537)
        if len(raw) > 65536:
            raise ValueError("hardware evidence exceeds the 64 KiB limit")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("hardware evidence contains duplicate JSON keys")
                result[key] = value
            return result

        evidence = json.loads(raw, object_pairs_hook=unique_object)
    except json.JSONDecodeError:
        raise ValueError("hardware evidence is not valid JSON") from None
    except ValueError:
        raise
    except (OSError, UnicodeDecodeError):
        raise ValueError("hardware evidence is not valid JSON") from None

    fields = {"schema_version", "tested_at", "firmware_sha256", "source_commit",
              "build_id", "device", "tests"}
    if not isinstance(evidence, dict) or set(evidence) != fields:
        raise ValueError("hardware evidence has missing or unknown fields")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("hardware evidence schema version is unsupported")
    tested_at = evidence["tested_at"]
    if not isinstance(tested_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", tested_at
    ):
        raise ValueError("hardware evidence tested_at must be a UTC RFC3339 timestamp")
    try:
        datetime.fromisoformat(tested_at[:-1] + "+00:00")
    except ValueError:
        raise ValueError("hardware evidence tested_at is malformed") from None

    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    if evidence["firmware_sha256"] != digest:
        raise ValueError("hardware evidence firmware SHA256 does not match the release image")
    if evidence["source_commit"] != source_commit:
        raise ValueError("hardware evidence source commit does not match the release image")
    build_id = evidence["build_id"]
    image_bytes = image.read_bytes()
    if (not isinstance(build_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", build_id)
            or b"[BOOT] Build: " not in image_bytes
            or build_id.encode("ascii") not in image_bytes):
        raise ValueError("hardware evidence build ID does not match the release image")
    device = evidence["device"]
    if (not isinstance(device, dict) or set(device) != {"model", "flash_bytes"}
            or device["model"] != "Model B" or type(device["flash_bytes"]) is not int
            or device["flash_bytes"] != 16 * 1024 * 1024):
        raise ValueError("hardware evidence does not verify a Model B with 16 MB flash")
    tests = evidence["tests"]
    if not isinstance(tests, dict) or set(tests) != REQUIRED_HARDWARE_TESTS:
        raise ValueError("hardware evidence has missing or unknown hardware test fields")
    if any(value is not True for value in tests.values()):
        raise ValueError("hardware evidence reports a failed hardware acceptance test")


def _tag_commit(tag: str) -> str:
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/commits/{tag}", "--jq", ".sha"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("could not verify the source commit for the target Git tag")
    return commit


def publish(tag: str, asset_dir: Path, hardware_evidence: Path) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tag):
        raise ValueError("release tag has an invalid format")
    image, sidecar = verify_release_directory(asset_dir)
    version = firmware_release_version(image)
    if version is None or tag not in {version, f"v{version}"}:
        raise ValueError("release tag does not match the firmware's embedded version")
    source_commit = firmware_source_commit(image)
    if source_commit is None:
        raise ValueError("firmware has no valid source commit; refusing publication")
    _validate_hardware_evidence(hardware_evidence, image, source_commit)
    if _tag_commit(tag) != source_commit:
        raise ValueError("Git tag does not point to the firmware's embedded source commit")
    inspection = subprocess.run(
        ["gh", "release", "view", tag, "--json", "assets", "--repo", REPOSITORY],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    if inspection.returncode:
        raise RuntimeError("could not verify the target GitHub release assets")
    try:
        release = json.loads(inspection.stdout)
        existing_assets = release["assets"]
        if not isinstance(existing_assets, list):
            raise ValueError
        asset_names = [asset["name"] for asset in existing_assets]
        if any(not isinstance(name, str) for name in asset_names):
            raise ValueError
    except (TypeError, KeyError, json.JSONDecodeError, ValueError):
        raise RuntimeError("GitHub returned an invalid release asset list") from None
    if asset_names:
        raise ValueError(
            "target GitHub release is not empty; refusing to append unverified assets"
        )
    # Upload a private snapshot, not paths in the caller-owned directory. This
    # closes the verify-then-open race where gh could read replaced bytes.
    with tempfile.TemporaryDirectory(prefix="freematics-ota-publish-") as temporary:
        snapshot_dir = Path(temporary)
        if os.name == "posix":
            snapshot_dir.chmod(0o700)
        snapshot_paths = []
        for source in (image, sidecar):
            snapshot = snapshot_dir / source.name
            with source.open("rb") as input_file, snapshot.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            if os.name == "posix":
                snapshot.chmod(0o600)
            snapshot_paths.append(snapshot)
        verify_release_directory(snapshot_dir)

        # The tag may have moved while GitHub release metadata was queried or
        # the validated snapshot was copied. Resolve it again just before upload.
        if _tag_commit(tag) != source_commit:
            raise ValueError("Git tag changed during publication; refusing upload")

        result = subprocess.run(
            ["gh", "release", "upload", tag,
             *(str(path) for path in snapshot_paths), "--repo", REPOSITORY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"GitHub release upload failed (exit {result.returncode})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="tag for an already-created GitHub release")
    parser.add_argument("asset_dir", type=Path, help="directory made by package_ota_release.py")
    parser.add_argument("--hardware-evidence", required=True, type=Path,
                        help="private mode-0600 JSON record of completed Model B acceptance tests")
    args = parser.parse_args(argv)
    try:
        publish(args.tag, args.asset_dir, args.hardware_evidence)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"publish-ota-release: {exc}", file=sys.stderr)
        return 1
    print("Published the verified Freematics firmware and checksum pair.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
