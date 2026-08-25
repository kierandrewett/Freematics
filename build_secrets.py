"""Inject the per-device telemetry credential without storing it in Git."""

import os
import re

Import("env")


token = os.environ.get("FREEMATICS_TOKEN", "")
production = os.environ.get("PRODUCTION_BUILD") == "1"

if production and not token:
    raise RuntimeError("FREEMATICS_TOKEN is required for a production firmware build")

if token:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
        raise RuntimeError("FREEMATICS_TOKEN must be a 64-character hexadecimal secret")
    env.Append(CPPDEFINES=[("SERVER_TOKEN", env.StringifyMacro(token))])
