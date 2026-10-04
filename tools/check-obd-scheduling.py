#!/usr/bin/env python3
"""Compile the production OBD polling function against a deterministic ECU.

Failure cases defined before the scheduler change:

* Thirty-three supported PIDs can become fresh in 1000 ms when a request takes
  20 ms per slot (10 ms ECU response and 10 ms worker gap). This tests a
  defined capacity; it does not infer the car's response time.
* RPM and vehicle speed must become fresh within 250 ms when capacity permits.
* Variable response times must not make a published sample look fresh before
  its request completes.
* Unsupported PIDs must not consume a request slot.
* A failed read must not change its value or timestamp.
* A failing PID must not monopolise the scheduler.  A silent ECU must bound
  work per poll call.
* Unsigned clock rollover must preserve due-time ordering.
* The diagnostic scan path must remain separate and make no more than one
  scan call per poll invocation.
* A diagnostic no-response timeout must appear in the measured live-PID
  completion gap; cached reads must not be counted as acquisition completions.
* Reconnect must reset scheduling state and permit every supported PID to be
  selected again.

This is a host check of the extracted production ``pollOBD`` body.  It is not
a claim about physical vehicle throughput.  The fake ECU makes request cost
and failure behaviour explicit.  Run it before the scheduler change to retain
the baseline failures, then after the change to prove the selected policy.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "telelogger.ino"
CONFIG = ROOT / "config.h"
OBD_HEADER = ROOT / "lib/FreematicsPlus/FreematicsOBD.h"


def function(text: str, signature: str) -> str:
    """Return one complete C++ function without trying to parse C++."""
    start = text.index(signature + "\n{")
    opening = text.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


def source_hashes(source: str, config: str, obd_header: str) -> dict[str, str]:
    contents = {"telelogger.ino": source, "config.h": config,
                "lib/FreematicsPlus/FreematicsOBD.h": obd_header,
                "tools/check-obd-scheduling.py": Path(__file__).read_text()}
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()}


def harness(poll: str, reset: str | None, selector: str | None, defines: dict[str, int]) -> str:
    helpers = ""
    if reset is not None:
        helpers += reset + "\n"
    else:
        helpers += "void resetOBDSchedule() { fastOBDIndex = 0; lastOBDFastPoll = 0; nextOBDPollIndex = 0; obdScheduleStarted = millis(); memset(obdPollState, 0, sizeof(obdPollState)); }\n"
    if selector is not None:
        helpers += selector + "\n"
    reset_call = "resetOBDSchedule();"
    # The fake catalogue has RPM, speed, four fast values and 27 auxiliary
    # values.  The source decides what it polls; the harness only supplies a
    # stable ECU capability map and records the resulting requests.
    return (r'''
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

using byte = uint8_t;
static constexpr byte PID_RPM = 0x0c;
static constexpr byte PID_SPEED = 0x0d;
static constexpr unsigned PID_COUNT = 33;
static constexpr unsigned DTC_COUNT = 3;
static constexpr unsigned MAX_OBD_ERRORS = 3;
static constexpr uint32_t OBD_FAST_INTERVAL_MS = @OBD_FAST_INTERVAL_MS@;
static constexpr uint32_t OBD_AUX_INTERVAL_MS = 250;
static constexpr uint32_t OBD_PID_INTERVAL_MS = @OBD_PID_INTERVAL_MS@;
static constexpr uint32_t OBD_FAILED_RETRY_MS = @OBD_FAILED_RETRY_MS@;
static constexpr uint32_t OBD_DTC_TIMEOUT_MS = @OBD_DTC_TIMEOUT_MS@;
static constexpr byte OBD_FAST_PIDS_PER_CYCLE = 3;
static constexpr byte OBD_AUX_PIDS_PER_CYCLE = 1;
static constexpr uint32_t OBD_PID_READ_WARN_MS = 200;
static constexpr uint32_t DTC_SCAN_INTERVAL_MS = 120000;

struct PID_POLLING_INFO { byte pid; byte priority; float value; uint32_t ts; };
struct DTC_POLLING_INFO { uint32_t lastScan; };
struct State { void clear(unsigned) { clears++; } unsigned clears = 0; } state;
static constexpr unsigned STATE_OBD_READY = 2;
struct SerialStub {
  template<class T> void print(T) {}
  template<class T> void println(T) {}
  void println() {}
} Serial;
static constexpr int HEX = 16;

uint32_t tick = 0;
uint32_t started = 0;
uint32_t millis() { return tick; }
PID_POLLING_INFO obdData[PID_COUNT] = {};
DTC_POLLING_INFO dtcData[DTC_COUNT] = {};
byte dtcScanIndex = 0;
byte fastOBDFailureCycles = 0;
uint16_t supportedOBDPIDs = PID_COUNT;
uint32_t lastOBDReadLatency = 0;
uint32_t timeoutsOBD = 0;

// Baseline globals.  The new scheduler replaces these.  Keep both declarations
// available until the source transition is complete.
byte fastOBDIndex = 0;
uint32_t lastOBDFastPoll = 0;

struct OBDPollState { uint32_t lastAttempt; bool attempted; byte failures; };
OBDPollState obdPollState[PID_COUNT] = {};
byte nextOBDPollIndex = 0;
uint32_t obdScheduleStarted = 0;

struct FakeOBD {
  bool supported[256] = {};
  bool fail[256] = {};
  uint32_t responseMs[256] = {};
  unsigned reads[256] = {};
  std::vector<byte> order;
  std::vector<std::pair<byte, uint32_t>> completions;
  bool isValidPID(byte pid) const { return supported[pid]; }
  bool readPID(byte pid, float& value) {
    reads[pid]++; order.push_back(pid); tick += responseMs[pid];
    if (fail[pid]) return false;
    completions.push_back({pid, tick});
    value = float(reads[pid]); return true;
  }
} obd;

unsigned slowReports = 0;
unsigned failures = 0;
unsigned publishes = 0;
unsigned diagnosticCalls = 0;
unsigned diagnosticThisCall = 0;
unsigned diagnosticMaxPerCall = 0;
uint32_t diagnosticResponseMs = 0;
void reportSlowOBDRead(byte, const char*, uint32_t) { slowReports++; }
void reportOBDReadFailure(byte, const char*) { failures++; }
void publishOBDSnapshot() { publishes++; }
void scanDiagnostics() { diagnosticCalls++; diagnosticThisCall++; diagnosticMaxPerCall = std::max(diagnosticMaxPerCall, diagnosticThisCall); tick += diagnosticResponseMs; dtcData[dtcScanIndex].lastScan = millis(); dtcScanIndex = (dtcScanIndex + 1) % DTC_COUNT; }

''' + helpers + poll + r'''

static void seed(uint32_t start = 0) {
  tick = start; started = start; obd = FakeOBD{}; std::memset(obdData, 0, sizeof(obdData));
  std::memset(dtcData, 0, sizeof(dtcData)); std::memset(obdPollState, 0, sizeof(obdPollState));
  for (unsigned i = 0; i < PID_COUNT; i++) {
    const byte pid = i < 11 ? byte(i + 1) : byte(i + 3);
    obdData[i] = {pid, byte(i < 6 ? 1 : 2), 0, 0};
    obd.supported[pid] = true; obd.responseMs[pid] = 10;
  }
  obdData[0].pid = PID_RPM; obdData[1].pid = PID_SPEED;
  obd.supported[PID_RPM] = obd.supported[PID_SPEED] = true;
  obd.responseMs[PID_RPM] = obd.responseMs[PID_SPEED] = 10;
  dtcScanIndex = 0; fastOBDFailureCycles = 0; timeoutsOBD = 0; failures = 0;
  publishes = 0; diagnosticCalls = 0; diagnosticThisCall = 0; diagnosticMaxPerCall = 0; diagnosticResponseMs = 0; state.clears = 0;
  // Prevent DTC traffic during throughput cases.  The diagnostic case enables it.
  for (auto& item : dtcData) item.lastScan = 1;
  resetOBDSchedule();
}

static bool within(uint32_t now, uint32_t then, uint32_t limit) { return uint32_t(now - then) <= limit; }
static void callFor(uint32_t duration, uint32_t workerGap = 10) {
  const uint32_t end = tick + duration;
  while (uint32_t(tick - end) > 0x80000000u) { diagnosticThisCall = 0; pollOBD(); tick += workerGap; }
}
static bool allFresh(uint32_t now, uint32_t age) {
  for (auto& item : obdData) if (obd.isValidPID(item.pid) && (!item.ts || !within(now, item.ts, age))) return false;
  return true;
}
static bool readOnlySupported() {
  for (auto& item : obdData) if (!obd.isValidPID(item.pid) && obd.reads[item.pid]) return false;
  return true;
}
static uint32_t maxCompletionGap(byte pid) {
  uint32_t previous = 0, result = 0; bool seen = false;
  for (const auto& item : obd.completions) {
    if (item.first != pid) continue;
    if (seen) result = std::max(result, uint32_t(item.second - previous));
    previous = item.second; seen = true;
  }
  return seen ? std::max(result, uint32_t(tick - previous)) : UINT32_MAX;
}
static bool completionGapsAtMost(uint32_t limit) {
  for (const auto& item : obdData) if (obd.isValidPID(item.pid) && maxCompletionGap(item.pid) > limit) return false;
  return true;
}
static void resetScheduleUnderTest() {
  ''' + reset_call + r'''
}

int main() {
  bool pass = true;
  seed(); callFor(60000);
  bool fullFresh = completionGapsAtMost(1000);
  bool coreFresh = maxCompletionGap(PID_RPM) <= 250 && maxCompletionGap(PID_SPEED) <= 250;
  uint32_t maximumGap = 0, firstSweep = 0;
  bool firstSeen[256] = {};
  for (const auto& item : obd.completions) {
    if (!firstSeen[item.first]) firstSweep = std::max(firstSweep, uint32_t(item.second - started));
    firstSeen[item.first] = true;
  }
  std::cout << "first_sweep_ms=" << firstSweep << "\n";
  for (const auto& item : obdData) maximumGap = std::max(maximumGap, maxCompletionGap(item.pid));
  std::cout << "maximum_gap_ms=" << maximumGap << " rpm_gap_ms=" << maxCompletionGap(PID_RPM) << " speed_gap_ms=" << maxCompletionGap(PID_SPEED) << "\n";
  std::cout << "completion_gaps_33=" << fullFresh << " core_250=" << coreFresh << " reads=" << obd.order.size() << "\n";
  pass &= fullFresh && coreFresh;

  seed();
  for (unsigned i = 0; i < PID_COUNT; i++) obd.responseMs[obdData[i].pid] = 5 + (i % 13);
  callFor(60000);
  bool variableFresh = completionGapsAtMost(1000);
  std::cout << "variable_latency=" << variableFresh << "\n";
  pass &= variableFresh;

  seed(); obd.supported[obdData[8].pid] = false; callFor(1000);
  bool unsupported = readOnlySupported();
  std::cout << "unsupported_skipped=" << unsupported << "\n";
  pass &= unsupported;

  seed();
  const byte failedPid = obdData[0].pid; obdData[0].value = 42; obdData[0].ts = 7;
  obd.fail[failedPid] = true; callFor(500);
  bool noFakeTimestamp = obdData[0].value == 42 && obdData[0].ts == 7;
  bool fairAfterFailure = false;
  for (byte pid : obd.order) if (pid != failedPid && pid != PID_SPEED) fairAfterFailure = true;
  std::cout << "failed_read_unchanged=" << noFakeTimestamp << " fair_after_failure=" << fairAfterFailure << "\n";
  pass &= noFakeTimestamp && fairAfterFailure;

  seed();
  for (auto& item : obdData) { obd.fail[item.pid] = true; obd.responseMs[item.pid] = 50; }
  const uint32_t before = tick; pollOBD(); const uint32_t elapsed = tick - before;
  bool bounded = elapsed <= 60; // one timed-out request only
  std::cout << "silent_one_call_ms=" << elapsed << " bounded=" << bounded << "\n";
  pass &= bounded;

  seed(0xfffffff0u); callFor(2000);
  bool rollover = allFresh(tick, 1000);
  std::cout << "rollover=" << rollover << "\n";
  pass &= rollover;

  seed(); dtcData[0].lastScan = 0; callFor(1020);
  bool oneDtc = diagnosticCalls > 0 && diagnosticMaxPerCall <= 1;
  std::cout << "dtc_preserved=" << oneDtc << " calls=" << diagnosticCalls << "\n";
  pass &= oneDtc;

  seed(); diagnosticResponseMs = OBD_DTC_TIMEOUT_MS; dtcData[0].lastScan = 0; callFor(6000);
  const uint32_t dtcTimeoutRpmGap = maxCompletionGap(PID_RPM);
  bool dtcCostVisible = dtcTimeoutRpmGap >= diagnosticResponseMs;
  std::cout << "dtc_timeout_ms=" << diagnosticResponseMs << " dtc_timeout_rpm_gap_ms=" << dtcTimeoutRpmGap
            << " cost_visible=" << dtcCostVisible << "\n";
  pass &= dtcCostVisible;

  seed(); callFor(60000); resetScheduleUnderTest(); started = tick; for (auto& item : obdData) item.ts = 0; obd.completions.clear(); callFor(60000);
  bool reconnect = completionGapsAtMost(1000);
  std::cout << "reconnect=" << reconnect << "\n";
  pass &= reconnect;

  seed(); for (auto& item : obdData) obd.responseMs[item.pid] = 50; callFor(60000);
  bool saturationProgress = true;
  for (const auto& item : obdData) saturationProgress &= obd.reads[item.pid] > 1 && item.ts <= tick;
  std::cout << "saturation_progress=" << saturationProgress << "\n";
  pass &= saturationProgress;
  return pass ? 0 : 1;
}
''').replace("@OBD_FAST_INTERVAL_MS@", str(defines["OBD_FAST_INTERVAL_MS"])).replace(
        "@OBD_PID_INTERVAL_MS@", str(defines["OBD_PID_INTERVAL_MS"])
    ).replace("@OBD_FAILED_RETRY_MS@", str(defines["OBD_FAILED_RETRY_MS"])).replace(
        "@OBD_DTC_TIMEOUT_MS@", str(defines["OBD_DTC_TIMEOUT_MS"])
    )


def run(report: Path | None, source_ref: str | None = None) -> int:
    source = FIRMWARE.read_text() if source_ref is None else subprocess.check_output(
        ["git", "show", f"{source_ref}:telelogger.ino"], cwd=ROOT, text=True)
    config = CONFIG.read_text() if source_ref is None else subprocess.check_output(
        ["git", "show", f"{source_ref}:config.h"], cwd=ROOT, text=True)
    obd_header = OBD_HEADER.read_text() if source_ref is None else subprocess.check_output(
        ["git", "show", f"{source_ref}:lib/FreematicsPlus/FreematicsOBD.h"], cwd=ROOT, text=True)
    names = ("OBD_FAST_INTERVAL_MS", "OBD_PID_INTERVAL_MS", "OBD_FAILED_RETRY_MS")
    defines = {}
    for name in names:
        match = re.search(rf"^#define {name} (\d+)UL\b", config, re.MULTILINE)
        if not match and source_ref is not None and name != "OBD_FAST_INTERVAL_MS":
            defines[name] = 1000  # Verification target; the old scheduler does not use it.
            continue
        if not match:
            raise SystemExit(f"FAIL: config.h does not define {name} as an unsigned millisecond value")
        defines[name] = int(match.group(1))
    dtc_timeout = re.search(r"^#define OBD_DTC_TIMEOUT (\d+)\b", obd_header, re.MULTILINE)
    if not dtc_timeout:
        raise SystemExit("FAIL: FreematicsOBD.h does not define OBD_DTC_TIMEOUT")
    defines["OBD_DTC_TIMEOUT_MS"] = int(dtc_timeout.group(1))
    poll = function(source, "void pollOBD()")
    reset = function(source, "void resetOBDSchedule()") if "void resetOBDSchedule()\n{" in source else None
    selector = function(source, "int selectOBDPID(uint32_t now)") if "int selectOBDPID(uint32_t now)\n{" in source else None
    code = harness(poll, reset, selector, defines)
    with tempfile.TemporaryDirectory(prefix="freematics-obd-scheduling-") as temporary:
        temporary_path = Path(temporary)
        cpp = temporary_path / "obd_scheduling.cpp"
        binary = temporary_path / "obd_scheduling"
        cpp.write_text(code)
        compile_result = subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", str(cpp), "-o", str(binary)], text=True, capture_output=True)
        execution = None
        if compile_result.returncode == 0:
            execution = subprocess.run([str(binary)], text=True, capture_output=True)
        result = {
            "command": " ".join([sys.executable, *sys.argv]),
            "source_ref": source_ref,
            "sources": source_hashes(source, config, obd_header),
            "coverage": "Extracted production pollOBD with fake ECU. Initial acquisition is reported separately from successive reading gaps. DTC scheduler calls use an injected delay equal to production OBD_DTC_TIMEOUT; separate production-code emulator scenarios verify no-response timeout and partial-result handling. It does not measure Model B, bridge, FreeRTOS or Corsa throughput.",
            "scheduler_helpers_extracted": {"resetOBDSchedule": reset is not None, "selectOBDPID": selector is not None},
            "configured_timing_ms": defines,
            "compile": {"returncode": compile_result.returncode, "stderr": compile_result.stderr},
            "run": None if execution is None else {"returncode": execution.returncode, "stdout": execution.stdout, "stderr": execution.stderr},
        }
    encoded = json.dumps(result, indent=2) + "\n"
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(encoded)
    print(encoded)
    return 1 if compile_result.returncode else execution.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write the JSON evidence artefact here")
    parser.add_argument("--source-ref", help="compare a Git revision without changing the checkout")
    args = parser.parse_args()
    raise SystemExit(run(args.report, args.source_ref))
