#!/usr/bin/env python3
"""Compile production OBD code with a dummy ECU and save fault evidence."""
import argparse
import hashlib
import json
import platform
import re
import selectors
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def waveform_journal_payload(serialized_frame):
    """Validate a CStorageRAM frame and convert it to the SD journal payload."""
    frame = serialized_frame.rstrip("\r\n")
    if frame.count("*") != 1:
        raise RuntimeError("Waveform fixture must contain one production checksum")
    body, checksum = frame.rsplit("*", 1)
    if not body or not re.fullmatch(r"[0-9A-Fa-f]{1,2}", checksum):
        raise RuntimeError("Waveform fixture has a malformed production checksum")
    if sum(body.encode("ascii")) & 0xFF != int(checksum, 16):
        raise RuntimeError("Waveform fixture production checksum does not match")
    return body + ","


def request(base, path, packet=None):
    headers = {"Authorization": "Bearer " + "A" * 64, "Content-Type": "application/octet-stream"}
    with urllib.request.urlopen(urllib.request.Request(base + path, data=packet, headers=headers), timeout=3) as response:
        return response.read()


def drive(executable, collector):
    """Feed real HTTP responses back into the production journal replay loop."""
    with tempfile.TemporaryDirectory(prefix="freematics-drive-") as directory:
        root = Path(directory)
        server = None
        packets = []
        results = []
        child = None
        with (root / "collector.log").open("wb") as log:
            try:
                if collector:
                    (root / "data").mkdir()
                    (root / "log").mkdir()
                    with socket.socket() as port_socket:
                        port_socket.bind(("127.0.0.1", 0))
                        port = port_socket.getsockname()[1]
                    base = f"http://127.0.0.1:{port}"
                    command = [str(ROOT / "collector/teleserver"), "-g", "-p", str(port), "-u", "0",
                               "-w", "emulator-fixture-only", "-d", str(root / "data"), "-l", str(root / "log")]
                    server = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
                    for attempt in range(60):
                        try:
                            request(base, "/api/test")
                            break
                        except OSError:
                            if server.poll() is not None:
                                raise RuntimeError("Local collector exited")
                            time.sleep(0.05)
                    else:
                        raise RuntimeError("Local collector did not start")
                    request(base, "/api/notify/EMULATOR?EV=1&TS=1000")
                child = subprocess.Popen([str(executable), "--drive"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                events = selectors.DefaultSelector()
                events.register(child.stdout, selectors.EVENT_READ)
                while True:
                    if not events.select(timeout=5):
                        raise RuntimeError("Drive stopped producing events for five seconds")
                    line = child.stdout.readline()
                    if not line:
                        break
                    event = json.loads(line)
                    if event.get("event") != "upload":
                        results.append(event)
                        continue
                    packet = event["packet"]
                    packets.append(packet)
                    if len(packets) > 1000:
                        raise RuntimeError("Drive exceeded its replay limit")
                    response = request(base, "/api/post/EMULATOR", packet.encode()) if collector else b"OK 3"
                    if response.strip() != b"OK 3":
                        raise RuntimeError(f"Collector rejected sample: {response!r}")
                    child.stdin.write("ACK\n")
                    child.stdin.flush()
                child.wait(timeout=5)
                events.close()
                if child.returncode:
                    raise RuntimeError(f"Drive exited with {child.returncode}: {child.stderr.read()}")
                if collector:
                    live = json.loads(request(base, "/api/get/EMULATOR"))
                    values = {int(row[0]): row[1] for row in live["data"]}
                    if values.get(0x10C) != 3290:
                        raise RuntimeError(f"Incorrect final RPM: {values.get(0x10C)}")
                    archive = "".join(path.read_text() for path in (root / "data").rglob("*.txt"))
                    missing = [index for index in range(240) if f"0:{1000 + index * 250}," not in archive]
                    results.append({"scenario": "240 offline readings reach local collector archive",
                                    "status": "PASS" if not missing else "ERROR", "observed": 240 - len(missing)})
            finally:
                for process in (child, server):
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            return results, packets, (root / "collector.log").read_text()


def strict_status(packet):
    """The collector's pre-1 October rule: refuse a whole batch for one bad field or a clock reset."""
    body = packet.split("*")[0]
    ticks = [int(field[2:]) for field in body.split(",") if field.startswith("0:")]
    if any(field.endswith(":") for field in body.split(",")) or ticks != sorted(ticks):
        return 400, b"Invalid telemetry payload"
    return 200, b"OK %d" % sum(1 for field in body.split(",") if field and not field.startswith("0:"))


def drive_reboot(executable, strict=False):
    """Replay a journal spanning a reboot, plus one malformed frame, into a collector."""
    with tempfile.TemporaryDirectory(prefix="freematics-reboot-") as directory:
        root = Path(directory)
        (root / "data").mkdir()
        (root / "log").mkdir()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        command = [str(ROOT / "collector/teleserver"), "-g", "-p", str(port), "-u", "0",
                   "-w", "emulator-fixture-only", "-d", str(root / "data"), "-l", str(root / "log")]
        results, requests, statuses = [], 0, []
        with (root / "collector.log").open("wb") as log:
            server = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
            child = None
            try:
                for attempt in range(60):
                    try:
                        request(base, "/api/test")
                        break
                    except OSError:
                        time.sleep(0.05)
                request(base, "/api/notify/EMULATOR?EV=1&TS=1000")
                child = subprocess.Popen([str(executable), "--drive-reboot-strict" if strict else "--drive-reboot"],
                                         stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for line in child.stdout:
                    event = json.loads(line)
                    if event.get("event") != "upload":
                        results.append(event)
                        continue
                    requests += 1
                    if requests > 200:
                        raise RuntimeError("Reboot replay exceeded its request limit")
                    if strict:
                        status, body = strict_status(event["packet"])
                    else:
                        try:
                            body = request(base, "/api/post/EMULATOR", event["packet"].encode())
                            status = 200
                        except urllib.error.HTTPError as error:
                            status, body = error.code, error.read()
                    statuses.append(status)
                    child.stdin.write(f"{status} {body.decode().strip()}\n")
                    child.stdin.flush()
                child.wait(timeout=5)
                if not strict:
                    archive = "".join(path.read_text() for path in (root / "data").rglob("*.txt")
                                      if path.name != "rejected.txt")
                    expected = [f"0:{900000 + index * 250}," for index in range(40) if index != 20]
                    expected += [f"0:{1000 + index * 250}," for index in range(40)]
                    missing = [tick for tick in expected if tick not in archive]
                    results.append({"scenario": "journal spanning a reboot reaches the collector without a stuck batch",
                                    "status": "PASS" if not missing else "ERROR", "observed": len(expected) - len(missing)})
                    kept = "".join(path.read_text() for path in (root / "data").rglob("rejected.txt"))
                    results.append({"scenario": "collector keeps the refused payload with its reason",
                                    "status": "PASS" if " empty value 0:905000,10C:," in kept else "ERROR",
                                    "observed": kept.count("\n")})
                results.append({"scenario": ("strict server" if strict else "collector") + " reboot replay requests and HTTP statuses",
                                "status": "PASS", "observed": requests, "statuses": statuses})
            finally:
                for process in (child, server):
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        return results


def index_waveform_replay(archive_root, fixture):
    """Index a real collector archive and check ordered waveform fields.

    Failure cases checked here were declared in CONDITION_MONITORING_PLAN.md:
    an offline restart, a lost acknowledgement, repeated PID fields, and
    acquisition clocks that differ from the enclosing frame clock.
    """
    sys.path.insert(0, str(ROOT / "collector"))
    from history_indexer import HistoryIndexer, normalise_pid, numeric, parse_frames
    from waveforms import waveform_window
    import asyncio
    import mechanic_mcp

    archives = list(archive_root.rglob("*.txt"))
    if len(archives) != 1:
        raise RuntimeError(f"Expected one collector archive, found {len(archives)}")
    archive = archives[0]
    database = archive_root.parent / "history.sqlite"
    indexer = HistoryIndexer(archive_root, database,
                             now_ms=lambda: int(archive.stat().st_mtime * 1_000) + 61_000)
    if indexer.index_once() != 1:
        raise RuntimeError("History indexer did not index the waveform archive")
    import sqlite3
    with sqlite3.connect(database) as connection:
        samples = list(connection.execute(
            "SELECT sequence,device_monotonic_ms FROM sample WHERE device_id='EMULATOR' ORDER BY sequence"))
        fields = list(connection.execute(
            """SELECT sequence,ordinal,pid,numeric_value,text_value
               FROM sample_field WHERE device_id='EMULATOR' ORDER BY sequence,ordinal"""))
        trip_id = connection.execute("SELECT trip_id FROM trip WHERE device_id='EMULATOR'").fetchone()[0]
        decoded = waveform_window(connection, "EMULATOR", trip_id, 0, 10)
    expected = parse_frames(fixture, include_final=True)
    if len(expected) != 1:
        raise RuntimeError("Production waveform fixture must contain one complete frame")
    expected_timestamp = expected[0].device_monotonic_ms
    expected_group = [(normalise_pid(pid), numeric(value) if numeric(value) is not None else value)
                      for pid, value in expected[0].ordered_fields]
    observed_groups = [[(pid, numeric_value if text_value is None else text_value)
                        for row_sequence, _ordinal, pid, numeric_value, text_value in fields
                        if row_sequence == sequence]
                       for sequence, _timestamp in samples]
    if samples != [(0, expected_timestamp), (1, expected_timestamp)] or observed_groups != [expected_group, expected_group]:
        raise RuntimeError("Waveform replay projection changed field order or values")
    voltage = decoded["voltage"]
    motion = decoded["motion"]
    if len(voltage) != 32 or len(motion) != 32 or any(point["centivolts"] != 980 for point in voltage[7::16]):
        raise RuntimeError("Indexed production waveform lost a voltage dip or motion observations")
    if any(point["offset_ms"] != -299 + index * 20 for index, point in enumerate(motion[:16])):
        raise RuntimeError("Indexed motion acquisition timestamps no longer match the production fixture")
    # Use the registered MCP tool, not its implementation function. This is
    # the read-only access path used by diagnosis clients.
    mechanic_mcp.HISTORY = database
    mcp_result = asyncio.run(mechanic_mcp.mcp.call_tool("sensor_waveforms", {
        "device_id": "EMULATOR", "trip_id": trip_id, "start_sequence": 0, "limit": 10,
    }))
    if isinstance(mcp_result, dict):
        mcp_waveforms = mcp_result
    elif len(mcp_result) == 1 and hasattr(mcp_result[0], "text"):
        mcp_waveforms = json.loads(mcp_result[0].text)
    else:
        raise RuntimeError("Registered sensor_waveforms MCP tool returned an unexpected result")
    if len(mcp_waveforms.get("voltage", [])) != 32 or len(mcp_waveforms.get("motion", [])) != 32:
        raise RuntimeError("Registered sensor_waveforms MCP tool lost replayed waveform observations")
    quality = mechanic_mcp.data_quality("EMULATOR", trip_id)
    if quality["waveform"]["voltage"]["readings"] != 16 or quality["waveform"]["motion"]["readings"] != 16:
        raise RuntimeError("Summary counted a retried waveform as a new acquisition")
    if "acquisition_coverage" not in quality["acquisition"]:
        raise RuntimeError("Data-quality tool omitted acquisition coverage")
    return {"scenario": "production waveform replay retains repeated fields, order and acquisition clocks through collector indexing",
            "status": "PASS", "observed": len(samples)}


def drive_waveforms(executable, fixture):
    """Replay waveform frames after a simulated restart into the local collector."""
    with tempfile.TemporaryDirectory(prefix="freematics-waveform-replay-") as directory:
        root = Path(directory)
        (root / "data").mkdir()
        (root / "log").mkdir()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        command = [str(ROOT / "collector/teleserver"), "-g", "-p", str(port), "-u", "0",
                   "-w", "emulator-fixture-only", "-d", str(root / "data"), "-l", str(root / "log")]
        server = child = None
        results, packets = [], []
        with (root / "collector.log").open("wb") as log:
            try:
                server = subprocess.Popen(command, cwd=root, stdout=log, stderr=log)
                for _attempt in range(60):
                    try:
                        request(base, "/api/test")
                        break
                    except OSError:
                        if server.poll() is not None:
                            raise RuntimeError("Local collector exited")
                        time.sleep(0.05)
                else:
                    raise RuntimeError("Local collector did not start")
                request(base, "/api/notify/EMULATOR?EV=1&TS=1000")
                child = subprocess.Popen([str(executable), "--drive-waveforms"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for line in child.stdout:
                    event = json.loads(line)
                    if event.get("event") != "waveform-upload":
                        results.append(event)
                        continue
                    packet = event["packet"]
                    body = request(base, "/api/post/EMULATOR", packet.encode())
                    if not body.startswith(b"OK "):
                        raise RuntimeError(f"Collector rejected waveform replay: {body!r}")
                    packets.append(packet)
                    # The collector wrote the first packet, but its response is lost.
                    # The production replay logic must retain and resend the exact batch.
                    response = "503 simulated-lost-acknowledgement" if len(packets) == 1 else "200 " + body.decode().strip()
                    child.stdin.write(response + "\n")
                    child.stdin.flush()
                child.wait(timeout=5)
                if child.returncode:
                    raise RuntimeError(f"Waveform drive exited with {child.returncode}: {child.stderr.read()}")
                if len(packets) != 2 or packets[0] != packets[1]:
                    raise RuntimeError("Lost acknowledgement did not replay the identical waveform batch")
                live = json.loads(request(base, "/api/get/EMULATOR"))
                live_pids = {int(row[0]) for row in live["data"]}
                if any(pid in live_pids for pid in range(0xA0, 0xA5)) or 0xA5 not in live_pids:
                    raise RuntimeError("Legacy live data retained a partial waveform field or lost its format version")
                results.append(index_waveform_replay(root / "data", fixture))
            finally:
                for process in (child, server):
                    if process is not None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        return results, packets


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
    parser.add_argument("--collector", action="store_true", help="Replay the offline drive into a real local collector")
    parser.add_argument("--sanitize", action="store_true", help="Check native memory access and undefined behaviour")
    parser.add_argument("--waveform-fixture", type=Path,
                        help="CBuffer waveform frame from check-sampling-boundary.py")
    parser.add_argument("--compiler", default="c++", help="C++ compiler executable")
    args = parser.parse_args()
    if args.waveform_fixture and not args.collector:
        parser.error("--waveform-fixture requires --collector")
    waveform_fixture = (waveform_journal_payload(args.waveform_fixture.read_text())
                        if args.waveform_fixture else None)
    compiler = args.compiler
    if args.collector:
        subprocess.run(["make", "-C", str(ROOT / "collector")], check=True, capture_output=True, text=True, timeout=60)
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
    CBuffer* getNewest();
};
"""
        client_source = client.read_text()
        oldest = "CBuffer* CBufferManager::getOldest(bool recorded)"
        if oldest not in client_source:
            # The committed queue has no recorder filter; pending recorder work adds it.
            header = header.replace("CBuffer* getOldest(bool recorded);", "CBuffer* getOldest();\n"
                                    "    CBuffer* getOldest(bool) { return getOldest(); }")
            oldest = "CBuffer* CBufferManager::getOldest()"
        header += extract_function(client_source, oldest)
        header += "\n" + extract_function(client_source, "CBuffer* CBufferManager::getNewest()")
        (build / "queue_scenario.h").write_text(header + "\n")
        # Byte-identical production files; only hardware headers and POSIX calls are substituted.
        for name in ("telequeue.cpp", "telequeue.h", "ota_first_upload_policy.h"):
            shutil.copyfile(ROOT / name, build / name)
        (build / "config.h").write_text("#define STORAGE_SD 1\n#define STORAGE 1\n#define SAMPLE_FRAME_SIZE 8192\n")
        # Counts top-level acquisitions: the ones that can wait behind another task.
        (build / "sdaccess.h").write_text("#pragma once\ninline unsigned sdTopLocks = 0;\ninline int sdLockDepth = 0; inline int sdLockFailures = 0;\n"
                                          "inline bool lockSD() { if (sdLockFailures) { --sdLockFailures; return false; } if (sdLockDepth++ == 0) sdTopLocks++; return true; }\n"
                                          "inline void unlockSD() { sdLockDepth--; }\n")
        (build / "freertos").mkdir()
        for name in ("FreeRTOS.h", "semphr.h"):
            (build / "freertos" / name).write_text("#pragma once\n")
        wire = """#pragma once
#include "Arduino.h"
class CStorage { public: byte checksum(const char* data, int len); };
class CStorageRAM : public CStorage {
public:
    char m_cache[16384];
    unsigned int m_cacheSize = sizeof(m_cache);
    unsigned int m_cacheBytes = 0;
    unsigned int m_samples = 0, m_checkpointBytes = 0, m_checkpointSamples = 0;
    bool m_overflowed = false;
    void purge() { m_cacheBytes = m_samples = m_checkpointBytes = m_checkpointSamples = 0; m_overflowed = false; }
    unsigned int length() { return m_cacheBytes; }
    char* buffer() { return m_cache; }
    bool appendRaw(const char* data, unsigned int length);
    void checkpoint();
    void rollback();
    void tailer();
};
"""
        storage = (ROOT / "telestore.cpp").read_text()
        for signature in ("byte CStorage::checksum(const char* data, int len)", "void CStorageRAM::tailer()",
                          "bool CStorageRAM::appendRaw(const char* data, unsigned int length)",
                          "void CStorageRAM::checkpoint()", "void CStorageRAM::rollback()"):
            wire += extract_function(storage, signature) + "\n"
        # The production replay batch builder and acknowledgement policy.
        firmware = (ROOT / "telelogger.ino").read_text()
        wire += '#include "sdaccess.h"\n#include "telequeue.h"\n#define HTTP_BATCH_MAX_WAIT_MS 1000UL\n'
        wire += extract_function(firmware, "uint8_t buildReplayBatch(DurableQueue& queue, CStorageRAM& store, char* frame, uint16_t capacity,\n                         uint8_t limit, uint16_t* lastLength,\n                         bool* includesCurrentBootRecord)") + "\n"
        wire += "#define HTTP_BATCH_MAX_SAMPLES 24\n#define HTTP_BATCH_MIN_SAMPLES 4\n#define HTTP_BATCH_GROW_STEP 4\n"
        start = firmware.index("struct ReplayIsolation {")
        wire += firmware[start:firmware.index("};", start) + 2] + "\n"
        wire += extract_function(firmware, "uint8_t replayBatchLimit(const ReplayIsolation& isolation, uint8_t linkLimit)") + "\n"
        wire += extract_function(firmware, "void settleReplayBatch(DurableQueue& queue, ReplayIsolation& isolation, bool sent, uint16_t status,\n                       uint8_t count, const char* frame, uint16_t length)") + "\n"
        if waveform_fixture is not None:
            wire += "\n#define WAVEFORM_FIXTURE " + json.dumps(waveform_fixture) + "\n"
        (build / "wire_scenario.h").write_text(wire)
        mems_header = (ROOT / "lib/FreematicsPlus/FreematicsMEMS.h").read_text()
        class_start = mems_header.index("class ICM_42627 :")
        class_end = mems_header.index("};", class_start) + 2
        mems = """#pragma once
#include "FreematicsBase.h"
#include "utility/ICM_42627.h"
class MEMS_I2C {};
"""
        # Use the production class declaration and scale macros, without ESP32 drivers.
        scales = mems_header[mems_header.index("#define Ascale"):mems_header.index("#endif", mems_header.index("#if Gscale")) + 6]
        mems += scales + "\n"
        mems += mems_header[class_start:class_end] + "\n"
        mems_source = (ROOT / "lib/FreematicsPlus/FreematicsMEMS.cpp").read_text()
        for name in ("readAccelData", "readGyroData", "readTempData", "read"):
            signature = re.search(r"(?:void|bool|int16_t) ICM_42627::" + name + r"\([^\n]*\)", mems_source).group()
            mems += extract_function(mems_source, signature) + "\n"
        (build / "mems_scenario.h").write_text(mems)
        executable = build / "scenarios"
        command = [compiler, "-std=c++17", "-Wall", "-Wextra", "-Wno-unused-parameter", "-O1",
                   "-I", str(HERE), "-I", str(ROOT / "lib/FreematicsPlus"), "-I", str(build),
                   str(HERE / "scenarios.cpp"), str(ROOT / "lib/FreematicsPlus/FreematicsOBD.cpp"),
                   str(HERE / "journal_scenarios.cpp"), str(build / "telequeue.cpp"),
                   str(HERE / "mems_scenarios.cpp"),
                   "-Wl,--wrap=open,--wrap=write,--wrap=fsync,--wrap=pread,--wrap=unlink,--wrap=close,--wrap=time",
                   "-o", str(executable)]
        if args.sanitize:
            command[1:1] = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
        output = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=15).stdout
        drive_results, packets, collector_log = drive(executable, args.collector)
        if args.collector:
            drive_results += drive_reboot(executable)
            drive_results += drive_reboot(executable, strict=True)
            if waveform_fixture is not None:
                waveform_results, waveform_packets = drive_waveforms(executable, waveform_fixture)
                drive_results += waveform_results
                packets += waveform_packets
    results = [json.loads(line) for line in output.splitlines()]
    results.extend(drive_results)
    sources = [client, ROOT / "lib/FreematicsPlus/FreematicsOBD.cpp",
               ROOT / "lib/FreematicsPlus/FreematicsOBD.h", ROOT / "lib/FreematicsPlus/FreematicsBase.h",
               ROOT / "lib/FreematicsPlus/utility/OBD.h", HERE / "Arduino.h",
               ROOT / "telequeue.cpp", ROOT / "telequeue.h", ROOT / "ota_first_upload_policy.h",
               ROOT / "telestore.cpp", HERE / "SD.h",
               ROOT / "lib/FreematicsPlus/FreematicsMEMS.cpp", ROOT / "lib/FreematicsPlus/FreematicsMEMS.h",
               ROOT / "lib/FreematicsPlus/utility/ICM_42627.h", HERE / "mems_scenarios.cpp",
               HERE / "journal_scenarios.cpp", HERE / "scenarios.cpp", Path(__file__).resolve()]
    if args.waveform_fixture:
        sources.append(args.waveform_fixture)
    if args.collector:
        sources.append(ROOT / "collector/teleserver")
    report = {
        "command": shlex.join([sys.executable, *sys.argv]),
        "environment": platform.platform(),
        "compiler": subprocess.run([compiler, "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0],
        "sources": {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path):
                    hashlib.sha256(path.read_bytes()).hexdigest() for path in sources},
        "coverage": "Actual OBD decoder, queue methods, wire checksum, IMU methods and journal with fake SD; no ESP32 boot or concurrent tasks",
        "results": results,
        "drive_packets": packets,
        "collector_log": collector_log,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    for result in results:
        print(f"{result['status']}: {result['scenario']} (observed: {result['observed']})")
    return int(any(item["status"] == "ERROR" or (args.strict and item["status"] == "ISSUE") for item in results))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, RuntimeError) as error:
        print(getattr(error, "stderr", None) or str(error), file=sys.stderr)
        sys.exit(1)
