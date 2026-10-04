#!/usr/bin/env python3
"""Compile the production USB queue against deterministic host FreeRTOS stubs."""

from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/test_usb_telemetry_queue.cpp"

with tempfile.TemporaryDirectory(prefix="freematics-usb-queue-") as temporary:
    temp = Path(temporary)
    (temp / "freertos").mkdir()
    (temp / "Arduino.h").write_text("#pragma once\n", encoding="ascii")
    (temp / "freertos/FreeRTOS.h").write_text("#pragma once\n", encoding="ascii")
    (temp / "freertos/portmacro.h").write_text(
        "#pragma once\n"
        "typedef int portMUX_TYPE;\n"
        "#define portMUX_INITIALIZER_UNLOCKED 0\n"
        "#define portENTER_CRITICAL(mux) ((void)(mux))\n"
        "#define portEXIT_CRITICAL(mux) ((void)(mux))\n",
        encoding="ascii",
    )
    binary = temp / "test_usb_telemetry_queue"
    subprocess.run(
        [
            "g++",
            "-std=c++11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-DCONFIG_H_INCLUDED",
            "-DSAMPLE_FRAME_SIZE=8192",
            "-I",
            str(temp),
            str(SOURCE),
            "-o",
            str(binary),
        ],
        check=True,
    )
    subprocess.run([str(binary)], check=True)
