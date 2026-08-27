"""Inject the per-device telemetry credential without storing it in Git."""

import os
import re
import subprocess
from pathlib import Path

from production_config import validate_production_config

Import("env")


token = os.environ.get("FREEMATICS_TOKEN", "")
production = os.environ.get("PRODUCTION_BUILD") == "1"

if production and not token:
    raise RuntimeError("FREEMATICS_TOKEN is required for a production firmware build")

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
    env.Append(CPPDEFINES=[("SERVER_TOKEN", env.StringifyMacro(token))])
# Stamp every image with the source revision visible in the boot log. This is
# deliberately metadata only; credentials remain injected through SERVER_TOKEN.
build_id = os.environ.get("FREEMATICS_BUILD_ID", "").strip()
if not build_id:
    try:
        build_id = subprocess.check_output(
            ["git", "rev-parse", "--short=12", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = subprocess.call(
            ["git", "diff", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) != 0
        dirty = dirty or subprocess.call(
            ["git", "diff", "--cached", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) != 0
        if dirty:
            build_id += "-dirty"
    except (OSError, subprocess.CalledProcessError):
        build_id = "unknown"

if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", build_id):
    raise RuntimeError("FREEMATICS_BUILD_ID must be 1-32 characters: A-Z, a-z, 0-9, dot, underscore, or hyphen")
env.Append(CPPDEFINES=[("FREEMATICS_BUILD_ID", env.StringifyMacro(build_id))])
