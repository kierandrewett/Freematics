#!/usr/bin/env python3
"""Upload only a freshly verified OTA asset pair to an existing GitHub release."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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


def publish(tag: str, asset_dir: Path) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tag):
        raise ValueError("release tag has an invalid format")
    image, sidecar = verify_release_directory(asset_dir)
    version = firmware_release_version(image)
    if version is None or tag not in {version, f"v{version}"}:
        raise ValueError("release tag does not match the firmware's embedded version")
    source_commit = firmware_source_commit(image)
    if source_commit is None:
        raise ValueError("firmware has no valid source commit; refusing publication")
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
    args = parser.parse_args(argv)
    try:
        publish(args.tag, args.asset_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"publish-ota-release: {exc}", file=sys.stderr)
        return 1
    print("Published the verified Freematics firmware and checksum pair.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
