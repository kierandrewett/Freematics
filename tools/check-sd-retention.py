#!/usr/bin/env python3
"""Compile the firmware retention rules and check deletion boundaries."""

from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
text = (ROOT / "sdarchive.h").read_text() if (ROOT / "sdarchive.h").exists() else ""
if "localLogExpired" not in text:
    raise SystemExit("FAIL: firmware has no 14-day local retention rule")
code = r'''
#include "sdarchive.h"
#include <cassert>
#include <iostream>
int main() {
 constexpr uint32_t now=1790640000, days=14*86400;
 assert(!localLogExpired(0,now));
 assert(!localLogExpired(now-1,0));
 assert(!localLogExpired(now+1000,now));
 assert(!localLogExpired(now-days+1,now));
 assert(localLogExpired(now-days,now));
 assert(localLogExpired(now-days-1,now));
 assert(localLogPath("/DATA/1.CSV"));
 assert(localLogPath("/DATA/234.UTC"));
 assert(localLogPath("/DATA/1790640000.BIN"));
 assert(!localLogPath("/DATA/../QUEUE.BIN"));
 assert(!localLogPath("/DATA/1.CSV/other"));
 assert(!localLogPath("/DATA/1.CSVextra"));
 assert(!localLogPath("/QUEUE.BIN"));
 assert(!localLogPath("/DATA/-1.CSV"));
 assert(!localLogPath("/DATA/0.CSV"));
 std::cout<<"PASS: 14-day boundary, unknown/future clock, and safe archive paths\n";
}
'''
with tempfile.TemporaryDirectory(prefix="freematics-retention-") as directory:
    cpp = Path(directory) / "retention.cpp"
    binary = Path(directory) / "retention"
    cpp.write_text(code)
    subprocess.run(["g++", "-std=c++17", "-I", str(ROOT), str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
