#!/usr/bin/env python3
"""Run the firmware's per-interval extremes code (acceleration peak, voltage
min/max) through its failure modes on the host.

Failure modes checked:
1. a peak counted in two intervals, or lost at the take/reset boundary
2. a stale peak emitted for an interval with no new reads
3. gravity counted as acceleration (the loop subtracts the calibrated bias)
4. voltage extremes depending on the motion sensor
5. a non-finite reading corrupting the extremes
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def extract(text: str, signature: str) -> str:
    start = text.index(signature + "\n{")
    opening = text.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


source = (ROOT / "telelogger.ino").read_text()
struct_start = source.index("struct IntervalExtremes {")
struct = source[struct_start:source.index("IntervalExtremes intervalExtremes = {};", struct_start)]
code = r'''
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>
using std::isfinite;
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
int sensorMux;
#define ELEMENT_UINT16 1
#define ELEMENT_FLOAT_D2 6
#define PID_ACC_PEAK 0x9A
#define PID_ACC_PEAK_VECTOR 0x9B
#define PID_VOLTAGE_MIN 0x9C
#define PID_VOLTAGE_MAX 0x9D
struct CBuffer {
    std::vector<uint16_t> pids;
    bool add(uint16_t pid, uint8_t, void*, int, uint8_t = 1) { pids.push_back(pid); return true; }
};
''' + struct + "IntervalExtremes intervalExtremes = {};\n"
for signature in ("void noteAcceleration(IntervalExtremes& extremes, const float acceleration[3])",
                  "void noteVoltage(IntervalExtremes& extremes, float voltage)",
                  "IntervalExtremes takeIntervalExtremes()",
                  "void emitIntervalExtremes(CBuffer* buffer)"):
    code += extract(source, signature) + "\n"
code += r'''
int main() {
    // 3: readings already have the calibrated gravity bias removed, so rest
    // is near zero and a 0.6 g brake stands out with its own vector.
    const float rest[3] = {0.01f, -0.01f, 0.02f};
    const float brake[3] = {-0.60f, 0.05f, 0.02f};
    for (int i = 0; i < 5; i++) noteAcceleration(intervalExtremes, rest);
    noteAcceleration(intervalExtremes, brake);
    for (int i = 0; i < 6; i++) noteAcceleration(intervalExtremes, rest);
    IntervalExtremes first = takeIntervalExtremes();
    assert(first.accReads == 12);
    assert(std::fabs(first.accPeak - std::sqrt(0.36f + 0.0025f + 0.0004f)) < 1e-5);
    assert(first.accAtPeak[0] == brake[0] && first.accAtPeak[1] == brake[1]);
    // 1: the next interval starts empty; the brake is not counted again.
    noteAcceleration(intervalExtremes, rest);
    IntervalExtremes second = takeIntervalExtremes();
    assert(second.accReads == 1 && second.accPeak < 0.05f);
    // 2: an interval with no reads emits nothing.
    CBuffer empty;
    emitIntervalExtremes(&empty);
    assert(empty.pids.empty());
    // 4: voltage alone (motion sensor failed) still reports min and max; the
    // cranking dip between two samples is kept.
    const float volts[] = {12.6f, 12.5f, 10.4f, 9.8f, 11.9f, 13.8f, 14.1f};
    for (float v : volts) noteVoltage(intervalExtremes, v);
    // 5: a non-finite reading is ignored.
    noteVoltage(intervalExtremes, NAN);
    const float broken[3] = {NAN, 0, 0};
    noteAcceleration(intervalExtremes, broken);
    CBuffer crank;
    emitIntervalExtremes(&crank);
    assert((crank.pids == std::vector<uint16_t>{PID_VOLTAGE_MIN, PID_VOLTAGE_MAX}));
    noteVoltage(intervalExtremes, 9.8f);
    noteVoltage(intervalExtremes, 14.1f);
    IntervalExtremes dip = takeIntervalExtremes();
    assert(dip.voltMin == 9.8f && dip.voltMax == 14.1f && dip.voltReads == 2 && dip.accReads == 0);
    std::cout << "PASS: peak kept once with its vector, empty interval emits nothing, "
                 "cranking dip kept without the motion sensor, non-finite reads ignored\n";
}
'''
with tempfile.TemporaryDirectory(prefix="freematics-extremes-") as directory:
    cpp, binary = Path(directory) / "extremes.cpp", Path(directory) / "extremes"
    cpp.write_text(code)
    subprocess.run(["c++", "-std=c++17", "-Wall", str(cpp), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
