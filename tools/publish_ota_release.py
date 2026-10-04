#!/usr/bin/env python3
"""Upload only a freshly verified OTA asset pair to an existing GitHub release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from package_ota_release import firmware_release_version, verify_release_directory


REPOSITORY = "kierandrewett/Freematics"


def publish(tag: str, asset_dir: Path) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tag):
        raise ValueError("release tag has an invalid format")
    image, sidecar = verify_release_directory(asset_dir)
    version = firmware_release_version(image)
    if version is None or tag not in {version, f"v{version}"}:
        raise ValueError("release tag does not match the firmware's embedded version")
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
    result = subprocess.run(
        ["gh", "release", "upload", tag, str(image), str(sidecar), "--repo", REPOSITORY],
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
