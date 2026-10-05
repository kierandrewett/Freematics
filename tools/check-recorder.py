#!/usr/bin/env python3
"""Exercise direct SD journalling with no completed-sample RAM backlog.

The harness runs the production journalSample(), CBuffer/CBufferManager,
CStorage serialiser and DurableQueue against a fake card. Simulated journal
latency blocks the sampler; failed samples are counted and never replayed.
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
queue_source = (ROOT / "telequeue.cpp").read_text()
config = (ROOT / "config.h").read_text()
settings = "\n".join(line for line in config.splitlines() if re.match(
    r"#define (LOG_FLUSH_INTERVAL_MS|SAMPLE_FRAME_SIZE|SAMPLE_INTERVAL_MS) ", line))
slot_match = re.search(r"#if STORAGE == STORAGE_SD\s+#define BUFFER_SLOTS (\d+)\b", config)
assert slot_match, "could not find the production SD handoff capacity"
buffer_slots = int(slot_match.group(1))
assert buffer_slots > 0
length_match = re.search(r"^#define BUFFER_LENGTH (\d+)\b", config, re.MULTILINE)
assert length_match, "could not find the production sample buffer length"
buffer_length = int(length_match.group(1))
assert "batch is unjournaled and will be discarded" in queue_source

# A pending OTA image is not proven healthy by uploading older backlog. The
# qualifying marker must be applied only after appendRaw has succeeded, so an
# append failure cannot accidentally let an older batch confirm the image.
replay_batch_start = firmware.index("uint8_t buildReplayBatch(")
replay_batch_end = firmware.index("\nuint8_t replayBatchLimit", replay_batch_start)
replay_batch = firmware[replay_batch_start:replay_batch_end]
append_branch = replay_batch.index("appended = envelopeLength > 0")
append_failure = replay_batch.index("if (!appended)")
current_boot_marker = replay_batch.index("if (currentBootRecord) *includesCurrentBootRecord = true;")
assert append_branch < append_failure < current_boot_marker, (
    "current-boot OTA marker must follow the complete SD append and failure check"
)

recorder = extract(firmware, "void recordSamples(void*)")
sd_recorder = recorder.split("#else", 1)[0]
assert "bufman.getOldest" not in sd_recorder
assert "durableQueue.appendBatch" not in sd_recorder
assert "synchronously by collectSample()" in sd_recorder
# Guard the production upload path as well as the recorder harness below: all
# STORAGE_SD protocols must read the durable journal, and may not select a RAM
# CBuffer when the journal is empty or unhealthy.
telemetry = extract(firmware, "void telemetry(void* inst)")
sd_upload = telemetry.split("// SD is the sole upload source in this build.", 1)[1].split("#else", 1)[0]
assert "replaying = durableQueue.healthy() && durableQueue.pendingBytes() != 0;" in sd_upload
assert "buildReplayBatch(durableQueue" in sd_upload
assert "bufman.getOldest" not in sd_upload
assert "durableQueue.acknowledge()" in telemetry
assert "durableQueue.retry()" in telemetry
assert "[STORAGE] SD journal unavailable; unjournaled readings are being discarded" in recorder
collect_sample = extract(firmware, "void collectSample()")
assert "const bool durableAvailable = durableQueue.cachedHealthy();" in collect_sample
assert collect_sample.index("usbTelemetryQueue.publish(record") < collect_sample.index("if (durableAvailable)")
assert "durableAvailable && journalSample(buffer, usbBootId, sampleSequence)" in collect_sample
sd_commit_path = collect_sample.rsplit("#if STORAGE == STORAGE_SD", 1)[-1].split("#else", 1)[0]
assert "bufman.publish(buffer);" not in sd_commit_path
journal_sample = extract(firmware, "bool journalSample(CBuffer* buffer, uint64_t session, uint32_t sequence)")
assert "durableQueue.appendIdentified(frame.buffer(), (uint16_t)frame.length(), session," in journal_sample
assert "do {" in journal_sample and "while (lockTimedOut && durableQueue.cachedHealthy())" in journal_sample
assert "frame.timestamp(buffer->timestamp)" in journal_sample
assert "lastJournalCommitTime" in firmware
assert "const uint32_t progressAt = haveJournalCommit ? journaledAt : recordingMonitorSince;" in firmware
csv_sample = sd_commit_path.split("// The journal is authoritative.", 1)[1]
assert "SDGuard csvGuard;" in csv_sample
maintain = extract(storage, "void SDLogger::maintain()")
assert maintain.index("SDGuard guard;") < maintain.index("time_t clock")

# OTA eligibility is based on scratch-file write/fsync/readback/remove under
# the shared SD mutex, only after normal journal owners are quiescent.
probe = extract(queue_source, "bool DurableQueue::probeStorage()")
assert "if (!lock()) return false;" in probe
assert probe.index("::write(") < probe.index("::fsync(") < probe.index("::pread(") < probe.index("::unlink(")
assert "m_probeFault = !okay;" in probe
parked_check = extract(firmware, "OTAParkedPolicy::Denial finalOtaParkedCheck()")
assert "durableQueue.probeStorage()" in parked_check
standby = extract(firmware, "void standby()")
assert standby.index("logger.end();") < standby.index("telemetryParked")
assert "otaStorageReadyAtPark = telemetryParked && durableQueue.probeStorage();" in standby
assert "const bool probeStorageNow" in standby and "!durableQueue.probeStorage()" in standby
setup = extract(firmware, "void setup()")
assert "otaStorageReady = durableQueue.probeStorage();" in setup
# The SD retry path powers the bus down and back up. The fake card has no bus.
journal_sample = journal_sample.replace(
    "if (durableQueue.appendIdentified(frame.buffer(), (uint16_t)frame.length(), session,\n                                      sequence, &lockTimedOut)) return true;",
    "if (simulatedJournalAppend(frame.buffer(), (uint16_t)frame.length(), session, sequence, &lockTimedOut)) return true;")

code = r'''
#include <cassert>
#include <cerrno>
#include <ctime>
#include <iostream>
#include <stdexcept>
#include <vector>
#include "Arduino.h"
#include "SD.h"
#include "config.h"
#include "sdaccess.h"
#include "telequeue.h"
uint32_t simulationTime = 0;
int simulatedLockTimeouts = 0;
#define portMUX_TYPE int
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)0)
#define portEXIT_CRITICAL(x) ((void)0)
#define heap_caps_malloc(size, caps) malloc(size)
#define heap_caps_free free
#define MALLOC_CAP_SPIRAM 0
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
for signature in ("void CBufferManager::recordMissedReading(uint32_t count)",
                  "uint32_t CBufferManager::missedReadings() const"):
    code += extract(client, signature) + "\n"
code = code.replace("int bytes, uint8_t count = 1)\n{", "int bytes, uint8_t count)\n{")
code += r'''
void CBufferManager::printStats() {}

// CSV trip log: counts lines so a reading logged twice is visible.
struct LoggerStub : CStorage {
    unsigned lines = 0;
    uint32_t bytes = 0;
    std::vector<uint32_t> timestamps;
    void dispatch(const char*, byte len) override { bytes += len + 1; }
    void timestamp(uint32_t ts) override { lines++; timestamps.push_back(ts); CStorage::timestamp(ts); }
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
static constexpr uint32_t SAMPLE_MS = SAMPLE_INTERVAL_MS;
static uint32_t nextSampleAt = SAMPLE_MS;
static uint32_t totalSamples = 0;
static uint32_t peakHeld = 0;
static uint32_t appendLatencyMs = 0;
bool simulatedJournalAppend(const char* frame, uint16_t length, uint64_t session, uint32_t sequence, bool* lockTimedOut);
''' + journal_sample + r'''

bool simulatedJournalAppend(const char* frame, uint16_t length, uint64_t session, uint32_t sequence, bool* lockTimedOut)
{
    simulationTime += appendLatencyMs;
    return durableQueue.appendIdentified(frame, length, session, sequence, lockTimedOut);
}

static void sampleOnce()
{
    CBuffer* buffer = bufman.getFree();
    totalSamples++;
    if (!buffer) { bufman.recordMissedReading(); return; }
    const float value = (float)totalSamples;
    buffer->add(0x100, ELEMENT_FLOAT_D2, (void*)&value, sizeof(value));
    buffer->timestamp = nextSampleAt;
    const bool journaled = durableQueue.cachedHealthy() && journalSample(buffer, 1, totalSamples);
    if (journaled) logger.timestamp(buffer->timestamp);
    else bufman.recordMissedReading();
    bufman.free(buffer);
    peakHeld = max(peakHeld, (uint32_t)bufman.unpersistedReadings());
}

static void advanceSimTime(uint32_t duration)
{
    const uint32_t finish = simulationTime + duration;
    while ((int32_t)(finish - nextSampleAt) >= 0) {
        if ((int32_t)(nextSampleAt - simulationTime) > 0) simulationTime = nextSampleAt;
        sampleOnce();
        nextSampleAt += SAMPLE_MS;
        if ((int32_t)(simulationTime - nextSampleAt) >= 0) {
            const uint32_t skipped = (simulationTime - nextSampleAt) / SAMPLE_MS + 1;
            totalSamples += skipped;
            bufman.recordMissedReading(skipped);
            nextSampleAt += skipped * SAMPLE_MS;
        }
    }
    if ((int32_t)(finish - simulationTime) > 0) simulationTime = finish;
}

static bool accountingBalanced()
{
    return totalSamples == bufman.missedReadings() + bufman.unpersistedReadings() + logger.lines;
}

static bool journalMatchesLogger()
{
    DurableQueue reader;
    if (!reader.begin()) return false;
    static char frame[8192];
    unsigned count = 0;
    while (count < logger.lines) {
        uint16_t length = 0;
        if (!reader.peek(frame, sizeof(frame), &length)) return false;
        const std::string prefix = "0:";
        if (length < prefix.size() || std::string(frame, prefix.size()) != prefix) return false;
        const size_t comma = std::string(frame, length).find(',');
        if (comma == std::string::npos) return false;
        const uint32_t timestamp = (uint32_t)std::stoul(std::string(frame + prefix.size(), comma - prefix.size()));
        if (timestamp != logger.timestamps.at(count)) return false;
        count++;
    }
    uint16_t length = 0;
    return !reader.peek(frame, sizeof(frame), &length);
}

int main()
{
    bufman.init();
    state.set(STATE_WORKING | STATE_STORAGE_READY);
    cardFiles.clear();
    assert(durableQueue.begin());
    // A lock timeout is transient contention, not a media error. The exact
    // same in-flight frame must retry and become durable rather than vanish.
    simulatedLockTimeouts = 1;
    appendLatencyMs = 650;
    cardOpens = 0;
    advanceSimTime(30000);
    const unsigned heldAtEnd = bufman.unpersistedReadings();
    bool okay = BUFFER_SLOTS == 1 && peakHeld <= 1 && heldAtEnd == 0 &&
      totalSamples == logger.lines + bufman.missedReadings() + heldAtEnd && accountingBalanced();
    okay = okay && bufman.missedReadings() > 0 && journalMatchesLogger();
    okay = okay && simulatedLockTimeouts == 0;
    appendLatencyMs = 0;
    const unsigned opens = cardOpens;
    std::cout << (okay ? "PASS" : "FAIL") << ": SD lock contention retried; slow SD blocks sampling; one in-flight frame, skipped intervals counted, committed bytes verified; missed="
              << bufman.missedReadings()
              << ", journaled=" << logger.lines << ", opens=" << opens << "\n";
    if (!okay) return 1;

    // Unavailable media never creates a RAM queue or replay candidate.
    cardOnline = false;
    const unsigned linesBeforeFailure = logger.lines;
    advanceSimTime(5000);
    okay = !durableQueue.healthy() && bufman.unpersistedReadings() == 0 &&
      totalSamples == logger.lines + bufman.missedReadings() && logger.lines == linesBeforeFailure;
    std::cout << (okay ? "PASS" : "FAIL") << ": unavailable SD produces counted misses, no retained samples or upload candidates\n";
    if (!okay) return 1;

    // Once SD recovers, only newly sampled records enter its journal.
    cardOnline = true;
    assert(durableQueue.begin());
    const unsigned linesBeforeRecovery = logger.lines;
    advanceSimTime(5000);
    okay = logger.lines > linesBeforeRecovery && bufman.unpersistedReadings() == 0 &&
      journalMatchesLogger() && totalSamples == logger.lines + bufman.missedReadings();
    std::cout << (okay ? "PASS" : "FAIL") << ": SD recovery records new samples only; failed samples remain counted, never replayed\n";
    if (!okay) return 1;
    return 0;
}
'''

with tempfile.TemporaryDirectory(prefix="freematics-recorder-") as directory:
    build = Path(directory)
    (build / "config.h").write_text("#pragma once\n#define STORAGE_NONE 0\n#define STORAGE_SPIFFS 1\n"
                                     "#define STORAGE_SD 2\n#define STORAGE 2\n#define BUFFER_SLOTS " +
                                     str(buffer_slots) + "\n#define BUFFER_LENGTH " +
                                     str(buffer_length) + "\n#define RECORD_BATCH_MAX 1\n"
                                     + settings + "\n")
    (build / "sdaccess.h").write_text("#pragma once\nextern int simulatedLockTimeouts;\n"
                                      "inline bool lockSD() { if (simulatedLockTimeouts) { --simulatedLockTimeouts; return false; } return true; }\n"
                                      "inline void unlockSD() {}\n"
                                      "struct SDGuard { explicit operator bool() const { return true; } };\n")
    (build / "freertos").mkdir()
    for name in ("FreeRTOS.h", "semphr.h"):
        (build / "freertos" / name).write_text("#pragma once\n")
    for name in ("telequeue.cpp", "telequeue.h", "ota_first_upload_policy.h"):
        shutil.copyfile(ROOT / name, build / name)
    (build / "recorder.cpp").write_text(code)
    binary = build / "recorder"
    subprocess.run(["c++", "-std=c++17", "-O1", "-I", str(build), "-I", str(EMULATOR),
                    str(build / "recorder.cpp"), str(build / "telequeue.cpp"),
                    "-Wl,--wrap=open,--wrap=close,--wrap=time", "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
