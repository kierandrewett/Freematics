#!/usr/bin/env python3
"""Write the private compile-time token without exposing it in compiler args."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


HEADER_NAME = "freematics_build_secrets.h"


def write_build_secret_header(build_dir: Path, token: str) -> Path:
    """Create an owner-only header containing only the validated token value."""
    if token and (len(token) != 64 or any(c not in "0123456789abcdefABCDEF" for c in token)):
        raise ValueError("FREEMATICS_TOKEN must be a 64-character hexadecimal secret")

    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = build_dir / HEADER_NAME
    contents = (
        "#ifndef FREEMATICS_BUILD_SECRETS_H_INCLUDED\n"
        "#define FREEMATICS_BUILD_SECRETS_H_INCLUDED\n"
        f'#define SERVER_TOKEN "{token}"\n'
        "#endif\n"
    ).encode("ascii")

    fd, temp_name = tempfile.mkstemp(prefix=".freematics-secrets-", dir=build_dir)
    temp_path = Path(temp_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, target)
        if os.name == "posix":
            os.chmod(target, 0o600)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return target
