#!/usr/bin/env python3
"""Compile and run the wrap-up journal checkpoint host test."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/test_recording_checkpoint_policy.cpp"

with tempfile.TemporaryDirectory(prefix="freematics-recording-checkpoint-") as temporary:
    binary = Path(temporary) / "test_recording_checkpoint_policy"
    subprocess.run(
        ["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror", str(SOURCE), "-o", str(binary)],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
