#!/usr/bin/env python3
"""Run the firmware recorder task against the real SD journal on a fake card.

The 30 September drive showed the recorder falling behind 4 Hz: the RAM queue
reached 1,011 of 1,024 readings and 1,154 readings were missed. This check runs
the production recordSamples() loop, CBuffer/CBufferManager, CStorage
serialiser and DurableQueue. Only the CSV logger, task state and the ESP32
heap/lock calls are stubbed; the card is tools/emulator/SD.h.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EMULATOR = ROOT / "tools/emulator"


def extract(text: str, signature: str) -> str:
    start = text.index(signature + "\n{")
    opening = text.index("{", start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


firmware = (ROOT / "telelogger.ino").read_text()
client = (ROOT / "teleclient.cpp").read_text()
client_header = (ROOT / "teleclient.h").read_text()
storage = (ROOT / "telestore.cpp").read_text()
storage_header = (ROOT / "telestore.h").read_text()
config = (ROOT / "config.h").read_text()
settings = "\n".join(line for line in config.splitlines() if re.match(
    r"#define (RECORD_BATCH_MAX|RECORD_BLOCK_SIZE|LOG_FLUSH_INTERVAL_MS|SAMPLE_FRAME_SIZE) ", line))

recorder = extract(firmware, "void recordSamples(void*)")
# The SD retry path powers the bus down and back up. The fake card has no bus.
recorder = recorder.replace("SD.end();", "/* SD.end() */").replace("SPI.end();", "/* SPI.end() */")

code = r'''
#include <cassert>
#include <cerrno>
#include <ctime>
#include <iostream>
#include <stdexcept>
#include "Arduino.h"
#include "SD.h"
#include "config.h"
#include "sdaccess.h"
#include "telequeue.h"
uint32_t simulationTime = 0;
#define portMUX_TYPE int
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
#define heap_caps_malloc(size, caps) malloc(size)
#define heap_caps_free free
#define MALLOC_CAP_SPIRAM 0
#define BUFFER_SLOTS 1024
#define BUFFER_LENGTH 2048
#define PID_TIMESTAMP 0

extern "C" int __wrap_open(const char* path, int, ...)
{
    if (!cardOnline || strncmp(path, "/sd/", 4)) { errno = EIO; return -1; }
    const char* name = path + 3;
    if (SD.exists(name)) { errno = EEXIST; return -1; }
    cardFiles[name] = std::make_shared<std::vector<uint8_t>>();
    return 500;
}
extern "C" int __wrap_close(int) { return 0; }
extern "C" time_t __wrap_time(time_t* value) { if (value) *value = 0; return 0; }
'''
code += storage_header[storage_header.index("class CStorage {"):storage_header.index("class FileLogger")] + "\n"
code += client_header[client_header.index("#define BUFFER_STATE_EMPTY"):client_header.index("class TeleClient")] + "\n"
for signature in ("void CStorage::log(uint16_t pid, uint8_t values[], uint8_t count)",
                  "void CStorage::log(uint16_t pid, uint16_t values[], uint8_t count)",
                  "void CStorage::log(uint16_t pid, uint32_t values[], uint8_t count)",
                  "void CStorage::log(uint16_t pid, int32_t values[], uint8_t count)",
                  "void CStorage::log(uint16_t pid, float values[], uint8_t count, const char* fmt)",
                  "void CStorage::logHex(uint16_t pid, const uint8_t values[], uint8_t count)",
                  "void CStorage::timestamp(uint32_t ts)", "byte CStorage::checksum(const char* data, int len)",
                  "void CStorageRAM::dispatch(const char* buf, byte len)",
                  "bool CStorageRAM::appendRaw(const char* data, unsigned int length)",
                  "void CStorageRAM::checkpoint()", "void CStorageRAM::rollback()",
                  "void CStorageRAM::header(const char* devid)", "void CStorageRAM::tailer()",
                  "void CStorageRAM::untailer()"):
    code += extract(storage, signature) + "\n"
code += "void CStorage::dispatch(const char*, byte) {}\n"
for signature in ("CBuffer::CBuffer(uint8_t* mem)",
                  "bool CBuffer::add(uint16_t pid, uint8_t type, void* values, int bytes, uint8_t count)",
                  "void CBuffer::purge()", "void CBuffer::serialize(CStorage& store)",
                  "void CBufferManager::init()", "CBuffer* CBufferManager::getFree()",
                  "CBuffer* CBufferManager::getOldest(bool recorded)", "void CBufferManager::free(CBuffer* slot)",
                  "void CBufferManager::publish(CBuffer* slot)", "void CBufferManager::restore(CBuffer* slot)",
                  "uint16_t CBufferManager::pendingReadings() const",
                  "uint16_t CBufferManager::unpersistedReadings() const",
                  "uint16_t CBufferManager::recordedReadings() const"):
    code += extract(client, signature) + "\n"
code = code.replace("int bytes, uint8_t count = 1)\n{", "int bytes, uint8_t count)\n{")
code += r'''
void CBufferManager::printStats() {}

// CSV trip log: counts lines so a reading logged twice is visible.
struct LoggerStub : CStorage {
    unsigned lines = 0;
    uint32_t bytes = 0;
    void dispatch(const char*, byte len) override { bytes += len + 1; }
    void timestamp(uint32_t ts) override { lines++; CStorage::timestamp(ts); }
    uint32_t size() { return bytes; }
    void flush() {}
    void maintain() {}
    bool healthy() { return true; }
    bool init() { return true; }
    unsigned begins = 0;
    uint32_t begin() { begins++; return 1; }
    void end() {}
} logger;

#define STATE_STORAGE_READY 0x10
#define STATE_WORKING 0x100
struct {
    unsigned flags = 0;
    bool check(unsigned f) { return (flags & f) == f; }
    void set(unsigned f) { flags |= f; }
    void clear(unsigned f) { flags &= ~f; }
} state;
#define PHASE_CONFIRMING 0
#define PHASE_TRIP 1
uint8_t powerPhase = PHASE_TRIP;
CBufferManager bufman;
DurableQueue durableQueue;
uint32_t lastStatsTime = 0, lastLogFlush = 0, fileid = 0;
uint16_t lastSizeKB = 0;

struct Finished {};
uint32_t stopAt = 0;
void harnessDelay(unsigned long ms)
{
    simulationTime += ms;
    if (simulationTime >= stopAt) throw Finished{};
}
#define delay harnessDelay
''' + recorder + r'''
#undef delay

static void runRecorder(uint32_t forMs)
{
    stopAt = simulationTime + forMs;
    try { recordSamples(nullptr); } catch (Finished&) {}
}

static void publish(unsigned count, uint32_t firstTick)
{
    for (unsigned i = 0; i < count; i++) {
        CBuffer* buffer = bufman.getFree();
        assert(buffer);
        for (uint16_t pid = 0x100; pid < 0x128; pid++) {
            float value = pid + i;
            buffer->add(pid, ELEMENT_FLOAT_D2, &value, sizeof(value));
        }
        buffer->timestamp = firstTick + i * 250;
        bufman.publish(buffer);
    }
}

static bool journalHolds(unsigned count, uint32_t firstTick)
{
    DurableQueue reader;
    if (!reader.begin()) return false;
    static char frame[8192];
    for (unsigned i = 0; i < count; i++) {
        uint16_t length = 0;
        if (!reader.peek(frame, sizeof(frame), &length)) return false;
        const std::string expected = "0:" + std::to_string(firstTick + i * 250) + ",";
        if (std::string(frame, expected.size()) != expected) return false;
    }
    uint16_t length = 0;
    return !reader.peek(frame, sizeof(frame), &length);
}

int main()
{
    bufman.init();
    state.set(STATE_WORKING | STATE_STORAGE_READY);

    // A stall leaves 200 readings in RAM. They must reach the journal in
    // order, each logged to CSV once, with few SD transactions.
    cardFiles.clear();
    assert(durableQueue.begin());
    publish(200, 1000);
    cardOpens = 0;
    runRecorder(1000);
    const unsigned opens = cardOpens;
    bool okay = bufman.unpersistedReadings() == 0 && logger.lines == 200 && journalHolds(200, 1000) && opens <= 16;
    std::cout << (okay ? "PASS" : "FAIL") << ": 200-reading backlog journaled in order with " << opens
              << " SD opens (one transaction per reading needs 400)\n";
    if (!okay) return 1;

    // A failed card write keeps every reading in RAM and logs CSV once. When
    // the card recovers, the SD retry path journals the RAM copies.
    cardFiles.clear();
    durableQueue = DurableQueue();
    logger.lines = 0;
    assert(durableQueue.begin());
    publish(10, 500000);
    cardWriteBudget = 0;
    runRecorder(2000);
    okay = bufman.unpersistedReadings() == 10 && bufman.recordedReadings() == 10 && logger.lines == 10;
    runRecorder(2000);
    okay = okay && bufman.unpersistedReadings() == 10 && logger.lines == 10;
    std::cout << (okay ? "PASS" : "FAIL") << ": failed card write keeps 10 readings in RAM and logs CSV once\n";
    if (!okay) return 1;
    cardWriteBudget = -1;
    runRecorder(40000);
    okay = bufman.unpersistedReadings() == 0 && logger.lines == 10 && journalHolds(10, 500000);
    std::cout << (okay ? "PASS" : "FAIL") << ": recovered card journals the RAM copies without duplicating CSV lines\n";
    if (!okay) return 1;

    // A wake that is still being confirmed journals its readings but creates
    // no CSV file in /DATA; the CSV opens once the trip is confirmed.
    cardFiles.clear();
    durableQueue = DurableQueue();
    assert(durableQueue.begin());
    fileid = 0;
    logger.begins = 0;
    logger.lines = 0;
    powerPhase = PHASE_CONFIRMING;
    publish(8, 900000);
    runRecorder(2000);
    okay = logger.begins == 0 && fileid == 0 && journalHolds(8, 900000) && bufman.unpersistedReadings() == 0;
    powerPhase = PHASE_TRIP;
    publish(4, 902000);
    runRecorder(2000);
    okay = okay && logger.begins == 1 && fileid == 1 && logger.lines == 4;
    std::cout << (okay ? "PASS" : "FAIL") << ": confirming wake journals readings without creating a CSV; the CSV opens on the trip\n";
    return okay ? 0 : 1;
}
'''

with tempfile.TemporaryDirectory(prefix="freematics-recorder-") as directory:
    build = Path(directory)
    (build / "config.h").write_text("#pragma once\n#define STORAGE_NONE 0\n#define STORAGE_SPIFFS 1\n"
                                     "#define STORAGE_SD 2\n#define STORAGE 2\n" + settings + "\n")
    (build / "sdaccess.h").write_text("#pragma once\ninline bool lockSD() { return true; }\n"
                                      "inline void unlockSD() {}\n"
                                      "struct SDGuard { explicit operator bool() const { return true; } };\n")
    (build / "freertos").mkdir()
    for name in ("FreeRTOS.h", "semphr.h"):
        (build / "freertos" / name).write_text("#pragma once\n")
    for name in ("telequeue.cpp", "telequeue.h"):
        shutil.copyfile(ROOT / name, build / name)
    (build / "recorder.cpp").write_text(code)
    binary = build / "recorder"
    subprocess.run(["c++", "-std=c++17", "-O1", "-I", str(build), "-I", str(EMULATOR),
                    str(build / "recorder.cpp"), str(build / "telequeue.cpp"),
                    "-Wl,--wrap=open,--wrap=close,--wrap=time", "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
