#!/usr/bin/env python3
"""Package a locally built Model B firmware image and sha256sum sidecar."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_config import _parse_defines, load_build_environment


ASSET_NAME = "freematics-model-b.bin"
SIDECAR_NAME = ASSET_NAME + ".sha256sum"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOKEN_PRESENT_MARKER = "FREEMATICS_CREDENTIAL_TOKEN_EMBEDDED=1"
TOKEN_ABSENT_MARKER = "FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1"
OTA_RELEASE_MARKER = "FREEMATICS_OTA_RELEASE_BUILD=1"
RELEASE_VERSION_MARKER = b"FREEMATICS_RELEASE_VERSION="
_CREDENTIAL_KEY_RE = re.compile(
    r"(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY|USERNAME|LOGIN)",
    re.IGNORECASE,
)
_SECRET_KEY_RE = re.compile(
    r"(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API[_-]?KEY|PRIVATE[_-]?KEY)",
    re.IGNORECASE,
)
_PROJECT_USERNAME_KEY_RE = re.compile(
    r"^(?:FREEMATICS|APN|WIFI|SERVER)_(?:.*_)?(?:USERNAME|LOGIN)$",
    re.IGNORECASE,
)


def _image_contains_value(image_path: Path, value: str | bytes) -> bool:
    """Check for an ASCII value without ever displaying it."""
    if not value:
        return False
    needle = value if isinstance(value, bytes) else value.encode("utf-8")
    overlap = b""
    with image_path.open("rb") as image:
        while chunk := image.read(1024 * 1024):
            searchable = overlap + chunk
            if needle in searchable:
                return True
            overlap = searchable[-(len(needle) - 1):]
    return False


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


def _verify_tokenless_build(image_path: Path) -> None:
    embedded = _image_contains_value(image_path, TOKEN_PRESENT_MARKER)
    absent = _image_contains_value(image_path, TOKEN_ABSENT_MARKER)
    ota_release = _image_contains_value(image_path, OTA_RELEASE_MARKER)
    version_found = False
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
                    version_found = re.fullmatch(
                        rb"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                        version,
                    ) is not None
                    if version_found:
                        break
                search_from = marker_at + 1
            if version_found:
                break
            overlap = searchable[-(len(RELEASE_VERSION_MARKER) + 33):]
    if embedded or not absent or not ota_release or not version_found:
        raise ValueError("firmware is missing a required token-free OTA/version marker; refusing to package")


def _configured_credentials() -> set[bytes]:
    """Read configured credentials without displaying them to the caller."""
    env_path = REPOSITORY_ROOT / ".env"
    settings = load_build_environment(env_path, {})
    credentials = {
        value.encode("utf-8") for key, value in settings.items()
        if _CREDENTIAL_KEY_RE.search(key) and value
    }
    for key, value in os.environ.items():
        if value and (_SECRET_KEY_RE.search(key) or _PROJECT_USERNAME_KEY_RE.fullmatch(key)):
            credentials.add(value.encode("utf-8"))
    if env_path.exists():
        for number, line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", stripped)
            if not match or not _CREDENTIAL_KEY_RE.search(match.group(1)):
                continue
            value = match.group(2)
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f".env:{number}: unmatched credential quotes")
                value = value[1:-1]
            if value:
                credentials.add(value.encode("utf-8"))

    config_path = REPOSITORY_ROOT / "local_config.h"
    defines = _parse_defines(config_path.read_text(encoding="utf-8"))
    for key, raw_value in defines.items():
        if not _CREDENTIAL_KEY_RE.search(key):
            continue
        normalized = raw_value.strip()
        if normalized == "NULL" or re.fullmatch(r"NULL\s*//.*", normalized):
            continue
        value = _decode_c_string_sequence(raw_value)
        if value is None:
            raise ValueError(
                "cannot verify a configured credential in local_config.h; refusing to package"
            )
        if value:
            credentials.add(value)
    return credentials


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
    for credential in _configured_credentials():
        if _image_contains_value(firmware, credential):
            raise ValueError(
                "firmware contains a configured credential; refusing to package"
            )
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and output_dir.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("release output directory must be owner-only (0700)")

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
            for credential in _configured_credentials():
                if _image_contains_value(staged_image, credential):
                    raise ValueError(
                        "firmware contains a configured credential; refusing to package"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path, help="built PlatformIO firmware.bin")
    parser.add_argument("--output-dir", required=True, type=Path, help="caller-selected release asset directory")
    args = parser.parse_args(argv)
    try:
        image, sidecar = package_release(args.firmware, args.output_dir)
    except (OSError, ValueError) as exc:
        print(f"package-ota-release: {exc}", file=sys.stderr)
        return 1
    print(image)
    print(sidecar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
