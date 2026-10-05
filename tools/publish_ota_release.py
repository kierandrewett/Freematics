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
        signature_path = path.with_name(path.name + ".asc")
        try:
            signature_info = signature_path.lstat()
        except OSError:
            raise ValueError("hardware evidence detached signature is unavailable") from None
        if (stat.S_ISLNK(signature_info.st_mode) or not stat.S_ISREG(signature_info.st_mode)
                or signature_info.st_uid != os.getuid()
                or stat.S_IMODE(signature_info.st_mode) != 0o600):
            raise ValueError("hardware evidence signature must be an owner-only regular file (0600)")
        _verify_hardware_evidence_signature(signature_path, raw)

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


def _verify_hardware_evidence_signature(signature_path: Path, evidence: bytes) -> None:
    """Require a valid detached attestation by the configured Git signing identity."""
    signing_key = subprocess.run(
        ["git", "config", "--get", "user.signingkey"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    key_id = signing_key.stdout.strip()
    if signing_key.returncode or not key_id or key_id.startswith("-"):
        raise ValueError("configured Git signing identity is required to attest hardware evidence")

    key_listing = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--fingerprint", "--list-keys", key_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    primary_fingerprints = []
    trusted_fingerprints = set()
    expect_fingerprint = False
    current_key_is_primary = False
    for line in key_listing.stdout.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            current_key_is_primary = True
            expect_fingerprint = True
        elif fields[0] == "sub":
            current_key_is_primary = False
            expect_fingerprint = True
        elif fields[0] == "fpr" and expect_fingerprint and len(fields) > 9:
            fingerprint = fields[9].upper()
            trusted_fingerprints.add(fingerprint)
            if current_key_is_primary:
                primary_fingerprints.append(fingerprint)
            expect_fingerprint = False
    if key_listing.returncode or len(primary_fingerprints) != 1:
        raise ValueError("configured Git signing identity could not be resolved uniquely")

    verified = subprocess.run(
        ["gpg", "--batch", "--no-tty", "--status-fd=1", "--verify",
         str(signature_path.resolve()), "-"],
        input=evidence,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    valid_signatures = []
    for line in verified.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            signer = fields[2].upper()
            primary = fields[11].upper() if len(fields) > 11 else signer
            valid_signatures.append((signer, primary))
    if (verified.returncode or len(valid_signatures) != 1
            or valid_signatures[0][0] not in trusted_fingerprints
            or valid_signatures[0][1] != primary_fingerprints[0]):
        raise ValueError("hardware evidence signature is invalid or not from the configured Git signer")


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
        ["gh", "release", "view", tag, "--json", "assets,isDraft", "--repo", REPOSITORY],
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
        is_draft = release["isDraft"]
        if not isinstance(existing_assets, list):
            raise ValueError
        asset_names = [asset["name"] for asset in existing_assets]
        if any(not isinstance(name, str) for name in asset_names):
            raise ValueError
    except (TypeError, KeyError, json.JSONDecodeError, ValueError):
        raise RuntimeError("GitHub returned an invalid release asset list") from None
    if is_draft is not True:
        raise ValueError("target GitHub release must remain a draft until uploaded assets are verified")
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

        # Verify GitHub's stored bytes, not only the local snapshot passed to
        # `gh`. A successful upload command is not proof that both assets are
        # present and intact.
        inspection = subprocess.run(
            ["gh", "release", "view", tag, "--json", "assets,isDraft", "--repo", REPOSITORY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
        if inspection.returncode:
            raise RuntimeError("could not verify uploaded GitHub release assets")
        try:
            uploaded_release = json.loads(inspection.stdout)
            uploaded_assets = uploaded_release["assets"]
            uploaded_is_draft = uploaded_release["isDraft"]
            if (not isinstance(uploaded_assets, list)
                    or any(not isinstance(asset, dict)
                           or not isinstance(asset.get("name"), str)
                           for asset in uploaded_assets)):
                raise ValueError
            uploaded_names = [asset["name"] for asset in uploaded_assets]
        except (TypeError, KeyError, json.JSONDecodeError, ValueError):
            raise RuntimeError("GitHub returned an invalid uploaded asset list") from None
        if uploaded_is_draft is not True or sorted(uploaded_names) != sorted((image.name, sidecar.name)):
            raise RuntimeError("uploaded GitHub release asset pair is incomplete or unexpected")

        downloaded_dir = snapshot_dir / "downloaded"
        downloaded_dir.mkdir(mode=0o700)
        download = subprocess.run(
            ["gh", "release", "download", tag, "--pattern", image.name,
             "--pattern", sidecar.name, "--dir", str(downloaded_dir),
             "--repo", REPOSITORY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if download.returncode:
            raise RuntimeError(f"could not download uploaded GitHub assets (exit {download.returncode})")
        downloaded_names = {path.name for path in downloaded_dir.iterdir()}
        if downloaded_names != {image.name, sidecar.name}:
            raise RuntimeError("downloaded GitHub assets are incomplete or unexpected")
        downloaded_image = downloaded_dir / image.name
        downloaded_sidecar = downloaded_dir / sidecar.name
        for expected, actual in zip(snapshot_paths, (downloaded_image, downloaded_sidecar)):
            if hashlib.sha256(expected.read_bytes()).digest() != hashlib.sha256(actual.read_bytes()).digest():
                raise RuntimeError("downloaded GitHub asset bytes differ from the verified upload")

        final_inspection = subprocess.run(
            ["gh", "release", "view", tag, "--json", "assets,isDraft", "--repo", REPOSITORY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
        if final_inspection.returncode:
            raise RuntimeError("could not verify final GitHub release asset inventory")
        try:
            final_release = json.loads(final_inspection.stdout)
            final_assets = final_release["assets"]
            final_is_draft = final_release["isDraft"]
            if (not isinstance(final_assets, list)
                    or any(not isinstance(asset, dict)
                           or not isinstance(asset.get("name"), str)
                           for asset in final_assets)):
                raise ValueError
            final_names = [asset["name"] for asset in final_assets]
        except (TypeError, KeyError, json.JSONDecodeError, ValueError):
            raise RuntimeError("GitHub returned an invalid final asset list") from None
        if final_is_draft is not True or sorted(final_names) != sorted((image.name, sidecar.name)):
            raise RuntimeError("GitHub release asset inventory changed during verification")
        if _tag_commit(tag) != source_commit:
            raise ValueError("Git tag changed during asset verification; refusing publication")
        publication = subprocess.run(
            ["gh", "release", "edit", tag, "--draft=false", "--verify-tag",
             "--repo", REPOSITORY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if publication.returncode:
            raise RuntimeError(f"verified draft release could not be published (exit {publication.returncode})")


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
