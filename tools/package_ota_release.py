#!/usr/bin/env python3
"""Package a locally built Model B firmware image and sha256sum sidecar."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote_from_bytes

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_config import _parse_defines, load_build_environment


ASSET_NAME = "freematics-model-b.bin"
SIDECAR_NAME = ASSET_NAME + ".sha256sum"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
IMAGE_SCAN_CHUNK_BYTES = 1024 * 1024
TOKEN_PRESENT_MARKER = "FREEMATICS_CREDENTIAL_TOKEN_EMBEDDED=1"
TOKEN_ABSENT_MARKER = "FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1"
OTA_RELEASE_MARKER = "FREEMATICS_OTA_RELEASE_BUILD=1"
RELEASE_VERSION_MARKER = b"FREEMATICS_RELEASE_VERSION="
SOURCE_COMMIT_MARKER = b"FREEMATICS_SOURCE_COMMIT="
_PRIVATE_VALUE_KEY_RE = re.compile(
    r"(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"USERNAME|USER|LOGIN|SIM[_-]?CARD[_-]?PIN|SIM[_-]?PIN|PIN|SSID|"
    r"SERVER[_-]?HOST|(?:CELL[_-]?)?APN)",
    re.IGNORECASE,
)
_PRIVATE_ENV_KEY_RE = re.compile(
    r"(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"PIN|SSID|SERVER[_-]?HOST|(?:CELL[_-]?)?APN)",
    re.IGNORECASE,
)
_PROJECT_USERNAME_KEY_RE = re.compile(
    r"^(?:FREEMATICS|APN|WIFI|SERVER)_(?:.*_)?(?:USERNAME|USER|LOGIN)$",
    re.IGNORECASE,
)


def _image_contains_value(image_path: Path, value: str | bytes) -> bool:
    """Check for a byte sequence without ever displaying it."""
    if not value:
        return False
    needle = value if isinstance(value, bytes) else value.encode("utf-8")
    overlap = b""
    with image_path.open("rb") as image:
        while chunk := image.read(IMAGE_SCAN_CHUNK_BYTES):
            searchable = overlap + chunk
            if needle in searchable:
                return True
            overlap_length = len(needle) - 1
            overlap = searchable[-overlap_length:] if overlap_length else b""
    return False


def _image_contains_c_string(image_path: Path, value: bytes) -> bool:
    """Match a complete NUL-delimited C string, not an incidental substring."""
    if not value:
        return False
    with image_path.open("rb") as image:
        if image.read(len(value) + 1) == value + b"\0":
            return True
    return _image_contains_value(image_path, b"\0" + value + b"\0")


def _credential_signatures(value: str | bytes) -> set[bytes]:
    """Return common exact encodings of a configured value for binary scans."""
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    if not raw:
        return set()
    signatures = {raw}
    if len(raw) >= 8:
        signatures.update({
            base64.b64encode(raw),
            base64.urlsafe_b64encode(raw).rstrip(b"="),
            raw.hex().encode("ascii"),
            raw.hex().upper().encode("ascii"),
            quote_from_bytes(raw, safe="").encode("ascii"),
            raw.decode("utf-8", errors="ignore").encode("utf-16le"),
            raw.decode("utf-8", errors="ignore").encode("utf-16be"),
        })
    return {signature for signature in signatures if signature}


def _decode_c_string_sequence(raw_value: str) -> bytes | None:
    """Join adjacent ordinary C string literals and ignore a trailing // comment."""
    value = raw_value.strip()
    if value.startswith(("L\"", "u\"", "U\"", 'R"', 'u8R"', 'uR"', 'UR"', 'LR"')):
        raise ValueError("prefixed credential strings are unsupported; refusing to package")
    if value.startswith('u8"'):
        value = value[2:]
    if not value.startswith('"'):
        return None

    output = bytearray()
    index = 0
    literal_count = 0
    while index < len(value):
        while index < len(value) and value[index].isspace():
            index += 1
        if index == len(value) or value.startswith("//", index):
            return bytes(output) if literal_count else None
        if value[index] != '"':
            raise ValueError("unsupported credential definition suffix; refusing to package")
        literal_count += 1
        index += 1
        end = value.find('"', index)
        if end < 0:
            raise ValueError("unterminated credential string; refusing to package")
        literal = value[index:end]
        # The production config parser rejects backslashes. Keep that invariant
        # here too rather than guessing how a compiler interprets an escape.
        if "\\" in literal:
            raise ValueError("escaped credential strings are unsupported; refusing to package")
        output.extend(literal.encode("utf-8"))
        index = end
        index += 1
    return bytes(output) if literal_count else None


def firmware_release_version(image_path: Path) -> str | None:
    """Return the strict major.minor.patch identity embedded in the image."""
    with image_path.open("rb") as image:
        overlap = b""
        while chunk := image.read(1024 * 1024):
            searchable = overlap + chunk
            search_from = 0
            while True:
                marker_at = searchable.find(RELEASE_VERSION_MARKER, search_from)
                if marker_at < 0:
                    break
                version_start = marker_at + len(RELEASE_VERSION_MARKER)
                version_end = searchable.find(b"\0", version_start)
                if version_end >= 0:
                    version = searchable[version_start:version_end]
                    if re.fullmatch(
                        rb"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                        version,
                    ):
                        return version.decode("ascii")
                search_from = marker_at + 1
            overlap = searchable[-(len(RELEASE_VERSION_MARKER) + 33):]
    return None


def firmware_source_commit(image_path: Path) -> str | None:
    """Return the unique full Git commit marker embedded in an OTA image."""
    with image_path.open("rb") as image:
        overlap = b""
        matches: set[bytes] = set()
        while chunk := image.read(1024 * 1024):
            searchable = overlap + chunk
            search_from = 0
            while True:
                marker_at = searchable.find(SOURCE_COMMIT_MARKER, search_from)
                if marker_at < 0:
                    break
                start = marker_at + len(SOURCE_COMMIT_MARKER)
                end = searchable.find(b"\0", start)
                if end >= 0:
                    matches.add(searchable[start:end])
                search_from = marker_at + 1
            overlap = searchable[-(len(SOURCE_COMMIT_MARKER) + 41):]
    if len(matches) != 1:
        return None
    commit = next(iter(matches))
    return commit.decode("ascii") if re.fullmatch(rb"[0-9a-f]{40}", commit) else None


def _current_source_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError:
        result = None
    if (
        result is None
        or result.returncode
        or not re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip())
    ):
        raise ValueError("cannot verify the current Git source commit; refusing OTA packaging")
    return result.stdout.strip()


def _source_commit_matches_checkout(image_commit: str) -> bool:
    """Require the image source to match, with only docs-only commits afterward."""
    try:
        current = _current_source_commit()
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", image_commit, current],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ancestor.returncode:
            return False
        paths = [".", ":!docs/**"]
        for args in (
            ["git", "diff", "--quiet", f"{image_commit}..{current}", "--", *paths],
            ["git", "diff", "--quiet", "--", *paths],
            ["git", "diff", "--cached", "--quiet", "--", *paths],
        ):
            result = subprocess.run(
                args,
                cwd=REPOSITORY_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode:
                return False
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if untracked.returncode:
            return False
        if any(
            path and not path.startswith(b"docs/")
            for path in untracked.stdout.split(b"\0")
        ):
            return False
    except (OSError, ValueError):
        return False
    return True


def _verify_tokenless_build(image_path: Path) -> str:
    embedded = _image_contains_value(image_path, TOKEN_PRESENT_MARKER)
    absent = _image_contains_value(image_path, TOKEN_ABSENT_MARKER)
    ota_release = _image_contains_value(image_path, OTA_RELEASE_MARKER)
    version = firmware_release_version(image_path)
    if embedded or not absent or not ota_release or version is None:
        raise ValueError("firmware is missing a required token-free OTA/version marker; refusing to package")
    image_commit = firmware_source_commit(image_path)
    if image_commit is None:
        raise ValueError("firmware is missing a valid source commit marker; refusing to package")
    if not _source_commit_matches_checkout(image_commit):
        raise ValueError("firmware source does not match the current checkout; refusing to package")
    return version


def _configured_credentials() -> set[bytes]:
    """Read configured credentials without displaying them to the caller."""
    env_path = REPOSITORY_ROOT / ".env"
    settings = load_build_environment(env_path, {})
    credentials: set[bytes] = set()
    for key, value in settings.items():
        if _PRIVATE_VALUE_KEY_RE.search(key) and value:
            credentials.update(_credential_signatures(value))
    for key, value in os.environ.items():
        if value and (_PRIVATE_ENV_KEY_RE.search(key) or _PROJECT_USERNAME_KEY_RE.fullmatch(key)):
            credentials.update(_credential_signatures(value))
    if env_path.exists():
        for number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", stripped)
            if not match or not _PRIVATE_VALUE_KEY_RE.search(match.group(1)):
                continue
            value = match.group(2)
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f".env:{number}: unmatched credential quotes")
                value = value[1:-1]
            if value:
                credentials.update(_credential_signatures(value))

    config_path = REPOSITORY_ROOT / "local_config.h"
    defines = _parse_defines(config_path.read_text(encoding="utf-8"))
    for key, raw_value in defines.items():
        if not _PRIVATE_VALUE_KEY_RE.search(key):
            continue
        normalized = raw_value.strip()
        if normalized == "NULL" or re.fullmatch(r"NULL\s*//.*", normalized):
            continue
        value = _decode_c_string_sequence(raw_value)
        if value is None:
            raise ValueError(
                "cannot verify a configured private value in local_config.h; refusing to package"
            )
        if value:
            credentials.update(_credential_signatures(value))
    return credentials


def _configured_server_paths() -> set[bytes]:
    """Read private routing paths for exact-string artifact checks."""
    paths: set[bytes] = set()
    for key, value in os.environ.items():
        if key == "SERVER_PATH" and value:
            paths.update(_credential_signatures(value))

    env_path = REPOSITORY_ROOT / ".env"
    if env_path.exists():
        for number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?SERVER_PATH\s*=\s*(.*)", stripped)
            if not match:
                continue
            value = match.group(1)
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f".env:{number}: unmatched private server-path quotes")
                value = value[1:-1]
            if value:
                paths.update(_credential_signatures(value))

    config_path = REPOSITORY_ROOT / "local_config.h"
    defines = _parse_defines(config_path.read_text(encoding="utf-8"))
    if "SERVER_PATH" in defines:
        value = _decode_c_string_sequence(defines["SERVER_PATH"])
        if value is None:
            raise ValueError("cannot verify configured private server path; refusing to package")
        if value:
            paths.update(_credential_signatures(value))
    return paths


def _image_contains_configured_private_value(image_path: Path) -> bool:
    if any(_image_contains_value(image_path, value)
           for value in _configured_credentials()):
        return True
    return any(_image_contains_c_string(image_path, value)
               for value in _configured_server_paths())


def package_release(firmware: Path, output_dir: Path) -> tuple[Path, Path]:
    """Copy firmware and create a sha256sum-compatible sidecar.

    Existing assets are never replaced. Temporary files are kept in the
    destination directory so publishing each finished file is atomic.
    """
    firmware = Path(firmware)
    output_dir = Path(output_dir)
    if not firmware.is_file():
        raise ValueError(f"firmware image is not a regular file: {firmware}")
    _verify_tokenless_build(firmware)
    if _image_contains_configured_private_value(firmware):
        raise ValueError(
            "firmware contains a configured private value; refusing to package"
        )
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and output_dir.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("release output directory must be owner-only (0700)")

    # Treat the directory as the exact upload set, not merely a place to copy
    # two files. This prevents a later "upload everything in this folder"
    # command from including logs, configs, or unrelated build products.
    allowed_names = {ASSET_NAME, SIDECAR_NAME}
    unexpected = [path.name for path in output_dir.iterdir() if path.name not in allowed_names]
    if unexpected:
        raise ValueError(
            "release output directory must be empty before packaging; "
            "refusing to mix release assets with unrelated files"
        )

    image_path = output_dir / ASSET_NAME
    sidecar_path = output_dir / SIDECAR_NAME
    if image_path.exists() or sidecar_path.exists():
        existing = [str(path) for path in (image_path, sidecar_path) if path.exists()]
        raise FileExistsError("refusing to overwrite existing asset(s): " + ", ".join(existing))

    staged: list[Path] = []
    published: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(prefix=".freematics-ota-", dir=output_dir, delete=False) as tmp_image:
            staged_image = Path(tmp_image.name)
            staged.append(staged_image)
            digest = hashlib.sha256()
            with firmware.open("rb") as source:
                shutil.copyfileobj(source, tmp_image, length=1024 * 1024)
            tmp_image.flush()
            # Hash the staged copy, not an upload timestamp or source metadata.
            with staged_image.open("rb") as copied:
                for chunk in iter(lambda: copied.read(1024 * 1024), b""):
                    digest.update(chunk)
            os.fsync(tmp_image.fileno())
            _verify_tokenless_build(staged_image)
            if _image_contains_configured_private_value(staged_image):
                raise ValueError(
                    "firmware contains a configured private value; refusing to package"
                )

        sidecar_bytes = f"{digest.hexdigest()}  {ASSET_NAME}\n".encode("ascii")
        with tempfile.NamedTemporaryFile(prefix=".freematics-ota-", dir=output_dir, delete=False) as tmp_sidecar:
            staged_sidecar = Path(tmp_sidecar.name)
            staged.append(staged_sidecar)
            tmp_sidecar.write(sidecar_bytes)
            tmp_sidecar.flush()
            os.fsync(tmp_sidecar.fileno())

        # Hard-link publication is atomic and fails if a target appeared after
        # the initial existence check (unlike os.replace).
        os.link(staged_image, image_path)
        published.append(image_path)
        os.link(staged_sidecar, sidecar_path)
        published.append(sidecar_path)

        return image_path, sidecar_path
    except Exception:
        # Roll back only files created by this invocation; never remove a
        # pre-existing target.
        for path in published:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for path in staged:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def verify_release_directory(output_dir: Path) -> tuple[Path, Path]:
    """Fail closed unless a directory contains only a current, safe asset pair."""
    output_dir = Path(output_dir)
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ValueError("release asset directory is not a real directory")
    if os.name == "posix" and output_dir.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("release output directory must be owner-only (0700)")
    image_path = output_dir / ASSET_NAME
    sidecar_path = output_dir / SIDECAR_NAME
    names = {path.name for path in output_dir.iterdir()}
    if names != {ASSET_NAME, SIDECAR_NAME}:
        raise ValueError("release directory must contain exactly the approved firmware/checksum pair")
    if any(path.is_symlink() or not path.is_file() for path in (image_path, sidecar_path)):
        raise ValueError("release assets must be regular files")
    if os.name == "posix" and any(
        stat.S_IMODE(path.stat().st_mode) != 0o600 for path in (image_path, sidecar_path)
    ):
        raise ValueError("release assets must be owner-only (0600)")

    _verify_tokenless_build(image_path)
    if _image_contains_configured_private_value(image_path):
        raise ValueError("firmware contains a configured private value; refusing to publish")

    sidecar = sidecar_path.read_bytes()
    expected = re.fullmatch(
        rb"([0-9a-f]{64})  " + re.escape(ASSET_NAME.encode("ascii")) + rb"\n",
        sidecar,
    )
    if not expected:
        raise ValueError("release checksum sidecar is malformed")
    digest = hashlib.sha256()
    with image_path.open("rb") as image:
        for chunk in iter(lambda: image.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().encode("ascii") != expected.group(1):
        raise ValueError("release checksum does not match the firmware image")
    return image_path, sidecar_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", nargs="?", type=Path, help="built PlatformIO firmware.bin")
    parser.add_argument("--output-dir", required=True, type=Path, help="caller-selected release asset directory")
    parser.add_argument("--verify-only", action="store_true", help="verify an existing release asset directory")
    args = parser.parse_args(argv)
    if not args.verify_only and args.firmware is None:
        parser.error("firmware is required unless --verify-only is used")
    try:
        if args.verify_only:
            image, sidecar = verify_release_directory(args.output_dir)
        else:
            image, sidecar = package_release(args.firmware, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"package-ota-release: {exc}", file=sys.stderr)
        return 1
    print(image)
    print(sidecar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
