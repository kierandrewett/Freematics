#!/usr/bin/env python3
"""Compile and run host tests for raw Mode 01 widths and USB metadata bounds."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/test_usb_raw_metadata.cpp"

with tempfile.TemporaryDirectory(prefix="freematics-usb-raw-") as temp:
    binary = Path(temp) / "test_usb_raw_metadata"
    subprocess.run(["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror",
                    str(SOURCE), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
