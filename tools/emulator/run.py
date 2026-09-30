#!/usr/bin/env python3
"""Compile production OBD code with a dummy ECU and save fault evidence."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def extract_function(source, signature):
    start = source.index(signature + "\n{")
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Fail when a firmware fault scenario reports an issue")
    parser.add_argument("--report", type=Path, help="Save a JSON evidence report")
    args = parser.parse_args()
    compiler = "c++"
    client = ROOT / "teleclient.cpp"
    with tempfile.TemporaryDirectory(prefix="freematics-ecu-") as temporary:
        build = Path(temporary)
        header = """#pragma once
#include <cstdint>
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
enum { BUFFER_STATE_EMPTY, BUFFER_STATE_FILLED, BUFFER_STATE_LOCKED };
struct CBuffer { int state; bool recorded; uint32_t timestamp; };
class CBufferManager {
public:
    CBuffer** slots;
    int total;
    CBuffer* getOldest(bool recorded);
};
"""
        header += extract_function(client.read_text(), "CBuffer* CBufferManager::getOldest(bool recorded)")
        (build / "queue_scenario.h").write_text(header + "\n")
        executable = build / "scenarios"
        command = [compiler, "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter", "-O1",
                   "-I", str(HERE), "-I", str(ROOT / "lib/FreematicsPlus"), "-I", str(build),
                   str(HERE / "scenarios.cpp"), str(ROOT / "lib/FreematicsPlus/FreematicsOBD.cpp"),
                   "-o", str(executable)]
        subprocess.run(command, check=True, capture_output=True, text=True)
        output = subprocess.run([str(executable)], check=True, capture_output=True, text=True).stdout
    results = [json.loads(line) for line in output.splitlines()]
    sources = [client, ROOT / "lib/FreematicsPlus/FreematicsOBD.cpp",
               ROOT / "lib/FreematicsPlus/FreematicsOBD.h", ROOT / "lib/FreematicsPlus/FreematicsBase.h",
               ROOT / "lib/FreematicsPlus/utility/OBD.h", HERE / "Arduino.h",
               HERE / "scenarios.cpp", Path(__file__).resolve()]
    report = {
        "command": shlex.join([sys.executable, *sys.argv]),
        "environment": platform.platform(),
        "compiler": subprocess.run([compiler, "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0],
        "sources": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "coverage": "Actual OBD decoder and extracted queue method; no ESP32 boot, task concurrency or physical I/O",
        "results": results,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    for result in results:
        print(f"{result['status']}: {result['scenario']} (observed: {result['observed']})")
    return int(any(item["status"] == "ERROR" or (args.strict and item["status"] == "ISSUE") for item in results))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        print(error.stderr or str(error), file=sys.stderr)
        sys.exit(1)
