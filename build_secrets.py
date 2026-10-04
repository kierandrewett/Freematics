"""Inject the per-device telemetry credential without storing it in Git."""

import os
import re
import subprocess
import sys
from pathlib import Path

Import("env")

project_dir = Path(env.subst("$PROJECT_DIR"))
sys.path.insert(0, str(project_dir / "tools"))
from production_config import load_build_environment, validate_production_config
from ota_release_config import generate_header
from build_secret_header import write_build_secret_header

build_dir = Path(env.subst("$BUILD_DIR"))
build_dir.mkdir(parents=True, exist_ok=True)
if os.name == "posix":
    # Production images may contain device credentials. Keep per-environment
    # build products inaccessible to other local users from compilation start.
    os.umask(0o077)
    for private_dir in (build_dir.parent.parent, build_dir.parent):
        private_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(private_dir, 0o700)
    os.chmod(build_dir, 0o700)


def protect_build_outputs(source, target, env):
    output_dir = Path(env.subst("$BUILD_DIR"))
    if os.name == "posix":
        os.chmod(output_dir, 0o700)
        for name in ("firmware.bin", "firmware.elf", "firmware.map"):
            output = output_dir / name
            if output.exists():
                os.chmod(output, 0o600)


env.AddPostAction("$BUILD_DIR/${PROGNAME}.elf", protect_build_outputs)
env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", protect_build_outputs)

settings = load_build_environment(Path(env.subst("$PROJECT_DIR")) / ".env", os.environ)
token = settings.get("FREEMATICS_TOKEN", "")
production = settings.get("PRODUCTION_BUILD") == "1"
ota_release = settings.get("FREEMATICS_OTA_RELEASE") == "1"
format_sd_once = os.environ.get("FREEMATICS_FORMAT_SD_ONCE") == "1"
if format_sd_once and os.environ.get("FREEMATICS_FORMAT_SD_CONFIRM") != "ERASE_UNFORMATTED_CARD":
    raise RuntimeError("SD provisioning requires FREEMATICS_FORMAT_SD_CONFIRM=ERASE_UNFORMATTED_CARD")

if production and not token and not ota_release:
    raise RuntimeError("FREEMATICS_TOKEN is required for a production firmware build")

if ota_release and token:
    raise RuntimeError("FREEMATICS_OTA_RELEASE must not embed FREEMATICS_TOKEN")
if ota_release and env.get("PIOENV") != "esp32dev-ota-test":
    raise RuntimeError("FREEMATICS_OTA_RELEASE is allowed only with the OTA-enabled esp32dev-ota-test environment")

release_version = settings.get("FREEMATICS_RELEASE", "").strip()
if ota_release and not release_version:
    raise RuntimeError("FREEMATICS_RELEASE must be set explicitly for an OTA release build")
if release_version:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", release_version):
        raise RuntimeError("FREEMATICS_RELEASE must be a numeric major.minor.patch version")
    env.Append(CPPDEFINES=[("FREEMATICS_RELEASE", env.StringifyMacro(release_version))])

if token:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
        raise RuntimeError("FREEMATICS_TOKEN must be a 64-character hexadecimal secret")
    if production:
        config_path = Path(env.subst("$PROJECT_DIR")) / "local_config.h"
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(f"local_config.h is required for a production build: {exc}") from exc
        try:
            validate_production_config(config_text)
        except ValueError as exc:
            raise RuntimeError(f"invalid production configuration: {exc}") from exc
elif ota_release:
    config_path = Path(env.subst("$PROJECT_DIR")) / "local_config.h"
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"local_config.h is required for an OTA release build: {exc}") from exc
    try:
        validate_production_config(config_text)
    except ValueError as exc:
        raise RuntimeError(f"invalid production configuration: {exc}") from exc
    public_config = build_dir / "ota_release_config.h"
    public_config.write_text(generate_header(config_text), encoding="utf-8")
    if os.name == "posix":
        os.chmod(public_config, 0o600)
    env.Append(CPPPATH=[str(build_dir.resolve())])
if not ota_release:
    write_build_secret_header(build_dir, token)
    env.Append(CPPPATH=[str(build_dir.resolve())])
env.Append(CPPDEFINES=[("FREEMATICS_TOKEN_EMBEDDED", "1" if token else "0")])
env.Append(CPPDEFINES=[("FREEMATICS_OTA_RELEASE_BUILD", "1" if ota_release else "0")])
if format_sd_once:
    # This opt-in image is used only while provisioning a new card. Never
    # format on an ordinary mount failure in the production image.
    env.Append(CPPDEFINES=["FREEMATICS_FORMAT_SD_ONCE"])
# Stamp every image with the source revision visible in the boot log. This is
# deliberately metadata only; credentials remain injected through SERVER_TOKEN.
build_id = settings.get("FREEMATICS_BUILD_ID", "").strip()
if not build_id:
    try:
        build_id = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            text=True,
            stderr=subprocess.DEVNULL,
        ))
        if dirty:
            build_id += "-dirty"
    except (OSError, subprocess.CalledProcessError):
        build_id = "unknown"

if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", build_id):
    raise RuntimeError("FREEMATICS_BUILD_ID must be 1-32 characters: A-Z, a-z, 0-9, dot, underscore, or hyphen")
env.Append(CPPDEFINES=[("FREEMATICS_BUILD_ID", env.StringifyMacro(build_id))])

try:
    source_commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=project_dir,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip().lower()
    dirty_source = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_dir,
        text=True,
        stderr=subprocess.DEVNULL,
    ))
except (OSError, subprocess.CalledProcessError):
    source_commit = "unknown"
    dirty_source = True

if ota_release and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
    raise RuntimeError("OTA release build requires a full Git source commit")
if ota_release and dirty_source:
    raise RuntimeError("OTA release build requires a clean source tree")
env.Append(CPPDEFINES=[("FREEMATICS_SOURCE_COMMIT", env.StringifyMacro(source_commit))])
