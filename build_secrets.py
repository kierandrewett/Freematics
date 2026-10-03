"""Inject the per-device telemetry credential without storing it in Git."""

import os
import re
import subprocess
from pathlib import Path

from production_config import load_build_environment, validate_production_config

Import("env")


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
env.Append(CPPDEFINES=[("SERVER_TOKEN", env.StringifyMacro(token))])
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
