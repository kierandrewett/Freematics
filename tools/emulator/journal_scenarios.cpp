#include <cerrno>
#include <ctime>
#include <fcntl.h>
#include <iostream>
#include <map>
#include <string>
#include <type_traits>
#include <unistd.h>
#include "FreematicsBase.h"
#include "FreematicsOBD.h"
#include "SD.h"
#include "telequeue.h"
#include "wire_scenario.h"

static_assert(!std::is_copy_constructible<DurableQueue>::value,
              "DurableQueue owns its read-ahead cache and cannot be copied");
static_assert(!std::is_copy_assignable<DurableQueue>::value,
              "DurableQueue owns its read-ahead cache and cannot be copy-assigned");

void report(const char* name, bool passed, bool fault, double observed);
inline time_t cardClock = 0;
struct ProbeDescriptor { std::string path; size_t position; };
static std::map<int, ProbeDescriptor> probeDescriptors;
static int nextProbeDescriptor = 501;

// Wrap journal/probe POSIX calls and clock reads, not production queue logic.
extern "C" int __wrap_open(const char* path, int flags, ...)
{
    if (!strncmp(path, "/sd/OTA", 7)) {
        if (!cardOnline || cardProbeOpenFails) { errno = EIO; return -1; }
        const char* name = path + 3;
        if (flags & O_CREAT) {
            if (SD.exists(name)) { errno = EEXIST; return -1; }
            cardFiles[name] = std::make_shared<std::vector<uint8_t>>();
        } else if (!SD.exists(name)) {
            errno = ENOENT;
            return -1;
        }
        const int descriptor = nextProbeDescriptor++;
        probeDescriptors[descriptor] = {name, 0};
        return descriptor;
    }
    if (!cardOnline || strncmp(path, "/sd/", 4)) { errno = EIO; return -1; }
    const char* name = path + 3;
    if (SD.exists(name)) { errno = EEXIST; return -1; }
    cardFiles[name] = std::make_shared<std::vector<uint8_t>>();
    return 500;
}
extern "C" ssize_t __wrap_write(int descriptor, const void* data, size_t length)
{
    auto found = probeDescriptors.find(descriptor);
    if (found == probeDescriptors.end()) return -1;
    auto bytes = cardFiles.at(found->second.path);
    size_t count = length;
    if (cardWriteBudget >= 0) {
        count = std::min(count, static_cast<size_t>(cardWriteBudget));
        cardWriteBudget -= count;
    }
    bytes->resize(std::max(bytes->size(), found->second.position + count));
    if (count) memcpy(bytes->data() + found->second.position, data, count);
    found->second.position += count;
    return count;
}
extern "C" int __wrap_fsync(int descriptor)
{
    if (probeDescriptors.count(descriptor)) return cardProbeFlushFails ? -1 : 0;
    return -1;
}
extern "C" ssize_t __wrap_pread(int descriptor, void* data, size_t length, off_t offset)
{
    auto found = probeDescriptors.find(descriptor);
    if (found == probeDescriptors.end() || offset < 0) return -1;
    if (cardReadBudget == 0) return 0;
    auto bytes = cardFiles.at(found->second.path);
    if (static_cast<size_t>(offset) >= bytes->size()) return 0;
    size_t count = std::min(length, bytes->size() - static_cast<size_t>(offset));
    if (cardReadBudget >= 0) {
        count = std::min(count, static_cast<size_t>(cardReadBudget));
        cardReadBudget -= count;
    }
    if (count) memcpy(data, bytes->data() + offset, count);
    return count;
}
extern "C" int __wrap_unlink(const char* path)
{
    if (!strncmp(path, "/sd/OTA", 7)) {
        if (cardProbeRemoveFails || !cardOnline) return -1;
        return SD.remove(path + 3) ? 0 : -1;
    }
    return -1;
}
extern "C" int __wrap_close(int descriptor)
{
    probeDescriptors.erase(descriptor);
    return 0;
}
extern "C" time_t __wrap_time(time_t* value)
{
    if (value) *value = cardClock;
    return cardClock;
}

static void resetCard()
{
    cardFiles.clear();
    cardOnline = true;
    cardRenameFails = false;
    cardWriteBudget = -1;
    cardPowerCutBudget = -1;
    cardReadBudget = -1;
    cardRenameBudget = -1;
    cardResetAfterRename = false;
    cardProbeOpenFails = false;
    cardProbeFlushFails = false;
    cardProbeRemoveFails = false;
    probeDescriptors.clear();
    sdLockDepth = 0;
    sdLockFailures = 0;
    cardClock = 0;
}

static bool append(DurableQueue& queue, const std::string& frame)
{
    return queue.append(frame.c_str(), frame.size());
}

static std::string peek(DurableQueue& queue)
{
    char buffer[8192];
    uint16_t length = 0;
    return queue.peek(buffer, sizeof(buffer), &length) ? std::string(buffer, length) : "";
}

void runJournalScenarios()
{
    const std::string first = "0:1000,10C:900,";
    const std::string second = "0:1250,10C:910,";
    {
        resetCard();
        DurableQueue probe;
        bool okay = probe.begin() && append(probe, first);
        const auto journalBefore = *cardFiles.at("/QUEUE.BIN");
        const uint32_t pendingBefore = probe.pendingBytes();
        const unsigned topLocksBefore = sdTopLocks;
        const bool probePassed = probe.probeStorage();
        const unsigned topLocksAfterProbe = sdTopLocks;
        okay = okay && probePassed && probe.healthy() &&
            *cardFiles.at("/QUEUE.BIN") == journalBefore &&
            probe.pendingBytes() == pendingBefore && sdLockDepth == 0 &&
            topLocksAfterProbe == topLocksBefore + 1;
        bool scratchRemains = false;
        for (const auto& entry : cardFiles) scratchRemains |= entry.first.find("/OTA") == 0;
        report("OTA SD health probe verifies and removes scratch without changing journal", okay && !scratchRemains,
               false, probe.pendingBytes());
    }
    const auto probeFailure = [&](const char* name, bool* injection, int kind) {
        resetCard();
        DurableQueue probe;
        bool okay = probe.begin() && append(probe, first);
        const auto journalBefore = *cardFiles.at("/QUEUE.BIN");
        const uint32_t pendingBefore = probe.pendingBytes();
        if (kind == 1) cardWriteBudget = 0;
        else if (kind == 2) cardReadBudget = 0;
        else *injection = true;
        const bool rejected = !probe.probeStorage() && !probe.healthy();
        okay = okay && rejected && *cardFiles.at("/QUEUE.BIN") == journalBefore &&
            probe.pendingBytes() == pendingBefore;
        if (kind == 1) cardWriteBudget = -1;
        else if (kind == 2) cardReadBudget = -1;
        else *injection = false;
        const bool recoveredByProbe = probe.probeStorage() && probe.healthy();
        okay = okay && recoveredByProbe &&
            *cardFiles.at("/QUEUE.BIN") == journalBefore &&
            probe.pendingBytes() == pendingBefore;
        report(name, okay, true, probe.pendingBytes());
    };
    probeFailure("OTA SD probe fails closed on scratch open failure", &cardProbeOpenFails, 0);
    probeFailure("OTA SD probe fails closed on short write", nullptr, 1);
    probeFailure("OTA SD probe fails closed on flush failure", &cardProbeFlushFails, 0);
    probeFailure("OTA SD probe fails closed on readback failure", nullptr, 2);
    probeFailure("OTA SD probe fails closed on scratch removal failure", &cardProbeRemoveFails, 0);
    resetCard();
    DurableQueue contendedProbe;
    bool lockProbeOkay = contendedProbe.begin() && append(contendedProbe, first);
    const auto contendedJournal = *cardFiles.at("/QUEUE.BIN");
    sdLockFailures = 1;
    lockProbeOkay = lockProbeOkay && !contendedProbe.probeStorage() && contendedProbe.healthy() &&
        *cardFiles.at("/QUEUE.BIN") == contendedJournal && sdLockDepth == 0;
    report("OTA SD probe denies eligibility on shared-lock contention", lockProbeOkay, false,
           contendedProbe.pendingBytes());

    resetCard();
    DurableQueue queue;
    bool okay = queue.begin() && append(queue, first) && append(queue, second);
    DurableQueue restarted;
    okay = okay && restarted.begin() && peek(restarted) == first && peek(restarted) == second;
    report("unacknowledged journal survives restart", okay, false, restarted.pendingBytes());
    restarted.retry();
    okay = peek(restarted) == first;
    report("lost acknowledgement replays first record", okay, false, restarted.readPosition());
    okay = restarted.acknowledge();
    DurableQueue resumed;
    okay = okay && resumed.begin() && peek(resumed) == second;
    report("saved cursor resumes at second record", okay, false, resumed.readPosition());

    cardWriteBudget = 4;
    okay = !resumed.acknowledge();
    cardWriteBudget = -1;
    DurableQueue afterFailedCheckpoint;
    okay = okay && afterFailedCheckpoint.begin() && peek(afterFailedCheckpoint) == second;
    report("partial acknowledgement checkpoint cannot skip data", okay, false, afterFailedCheckpoint.pendingBytes());

    resetCard();
    DurableQueue partial;
    okay = partial.begin() && append(partial, first);
    cardWriteBudget = 15;
    okay = okay && !append(partial, second) && !partial.healthy();
    cardWriteBudget = -1;
    DurableQueue tornRestart;
    okay = okay && tornRestart.begin() && peek(tornRestart) == first && tornRestart.acknowledge();
    okay = okay && peek(tornRestart).empty() && !tornRestart.healthy() && !append(tornRestart, second);
    report("torn append preserves prefix and stops at damage", okay, false, tornRestart.pendingBytes());
    okay = tornRestart.recover() && append(tornRestart, second) && peek(tornRestart) == second;
    report("torn tail repair permits a new journal append", okay, true, tornRestart.pendingBytes());

    // Treat each byte boundary as a possible sudden power cut while a
    // multi-record batch is appended. Bytes that form complete CRC-valid
    // records must replay in order; an incomplete tail must be quarantined,
    // never acknowledged past, and never hide the complete prefix.
    const std::string batchFrames[] = {first, second, "0:1500,10C:920,"};
    const char* batchData[] = {
        batchFrames[0].c_str(), batchFrames[1].c_str(), batchFrames[2].c_str()
    };
    const uint16_t batchLengths[] = {
        (uint16_t)batchFrames[0].size(),
        (uint16_t)batchFrames[1].size(),
        (uint16_t)batchFrames[2].size()
    };
    const uint32_t recordBytes = 12 + batchLengths[0];
    const uint32_t totalBatchBytes = recordBytes * 3;
    bool powerCutOkay = batchLengths[0] == batchLengths[1] &&
                        batchLengths[1] == batchLengths[2];
    uint32_t cutCount = 0;
    for (uint32_t cut = 0; powerCutOkay && cut <= totalBatchBytes; cut++) {
        resetCard();
        bool began = false;
        bool powerWasLost = false;
        {
            DurableQueue interruptedBatch;
            began = interruptedBatch.begin();
            cardPowerCutBudget = cut;
            try {
                (void)interruptedBatch.appendBatch(batchData, batchLengths, 3);
            } catch (const std::runtime_error&) {
                powerWasLost = true;
            }
        }
        // A power loss restarts volatile lock/task state but preserves only
        // the bytes already written to the simulated card.
        cardPowerCutBudget = -1;
        sdLockDepth = 0;
        cutCount++;

        DurableQueue restartedBatch;
        powerCutOkay = began && powerWasLost && restartedBatch.begin();
        std::vector<std::string> replayed;
        const auto collectReplay = [&]() {
            for (;;) {
                std::string frame = peek(restartedBatch);
                if (frame.empty()) break;
                replayed.push_back(frame);
            }
        };
        if (powerCutOkay) collectReplay();
        const uint32_t expectedCount = std::min<uint32_t>(3, cut / recordBytes);
        if (powerCutOkay && restartedBatch.damaged()) {
            const auto original = *cardFiles.at("/QUEUE.BIN");
            powerCutOkay = restartedBatch.recover();
            bool originalPreserved = false;
            for (const auto& entry : cardFiles) {
                if (entry.first.find("/RECOVERY/") == 0 && *entry.second == original)
                    originalPreserved = true;
            }
            powerCutOkay = powerCutOkay && originalPreserved;
            replayed.clear();
            if (powerCutOkay) collectReplay();
        }
        powerCutOkay = powerCutOkay && replayed.size() == expectedCount;
        for (uint32_t index = 0; powerCutOkay && index < expectedCount; index++)
            powerCutOkay = replayed[index] == batchFrames[index];
        if (powerCutOkay && cut % recordBytes == 0)
            powerCutOkay = restartedBatch.healthy();
    }
    report("power cut at every multi-record journal byte preserves complete replay prefix",
           powerCutOkay && cutCount == totalBatchBytes + 1, true, cutCount);

    resetCard();
    DurableQueue corrupt;
    okay = corrupt.begin() && append(corrupt, first);
    (*cardFiles.at("/QUEUE.BIN"))[12] ^= 1;
    okay = okay && peek(corrupt).empty() && !corrupt.healthy() && corrupt.readPosition() == 0;
    report("CRC failure cannot advance replay", okay, false, corrupt.readPosition());
    // Recovery must preserve the original and resume acquisition/replay.
    resetCard();
    DurableQueue damaged;
    okay = damaged.begin() && append(damaged, first) && append(damaged, second) && append(damaged, first);
    (*cardFiles.at("/QUEUE.BIN"))[12 + first.size() + 12] ^= 1;
    const auto damagedBytes = *cardFiles.at("/QUEUE.BIN");
    okay = okay && peek(damaged) == first && peek(damaged).empty();
    damaged.begin();
    okay = okay && peek(damaged) == first && peek(damaged) == first && append(damaged, second);
    bool preserved = false;
    for (const auto& entry : cardFiles) {
        if (entry.first.find("/RECOVERY/") == 0 && *entry.second == damagedBytes) preserved = true;
    }
    report("damaged journal preserves evidence and resumes all intact records", okay && preserved, true, damaged.pendingBytes());

    resetCard();
    DurableQueue interrupted;
    okay = interrupted.begin() && append(interrupted, first) && append(interrupted, second);
    (*cardFiles.at("/QUEUE.BIN"))[12] ^= 1;
    okay = okay && peek(interrupted).empty();
    cardRenameBudget = 1; // source quarantined, replacement promotion fails
    okay = okay && !interrupted.recover() && !SD.exists("/QUEUE.BIN") && SD.exists("/QUEUE.REC");
    cardRenameBudget = -1;
    DurableQueue afterRecoveryReset;
    okay = okay && afterRecoveryReset.begin() && peek(afterRecoveryReset) == second &&
        afterRecoveryReset.acknowledge() && append(afterRecoveryReset, first);
    report("reset during recovery promotion resumes verified replacement", okay, true, afterRecoveryReset.pendingBytes());

    resetCard();
    DurableQueue repairFailure;
    okay = repairFailure.begin() && append(repairFailure, first) && append(repairFailure, second);
    (*cardFiles.at("/QUEUE.BIN"))[12] ^= 1;
    const auto repairBytes = *cardFiles.at("/QUEUE.BIN");
    okay = okay && peek(repairFailure).empty();
    cardWriteBudget = 5;
    okay = okay && !repairFailure.recover() && *cardFiles.at("/QUEUE.BIN") == repairBytes;
    cardWriteBudget = -1;
    cardReadBudget = 5;
    okay = okay && !repairFailure.recover() && *cardFiles.at("/QUEUE.BIN") == repairBytes;
    cardReadBudget = -1;
    cardRenameFails = true;
    okay = okay && !repairFailure.recover() && *cardFiles.at("/QUEUE.BIN") == repairBytes;
    cardRenameFails = false;
    okay = okay && repairFailure.recover() && peek(repairFailure) == second;
    report("recovery write read and rename failures preserve source until retry", okay, true, repairFailure.pendingBytes());

    resetCard();
    DurableQueue acknowledgedPrefix;
    okay = acknowledgedPrefix.begin() && append(acknowledgedPrefix, first) &&
        append(acknowledgedPrefix, second) && append(acknowledgedPrefix, first);
    // Damage the second record before replay reads it. Replay reads ahead, so
    // bytes changed after a read are not seen until the next journal read.
    (*cardFiles.at("/QUEUE.BIN"))[12 + first.size() + 12] ^= 1;
    okay = okay && peek(acknowledgedPrefix) == first && acknowledgedPrefix.acknowledge();
    okay = okay && peek(acknowledgedPrefix).empty() && acknowledgedPrefix.recover() &&
        peek(acknowledgedPrefix) == first && peek(acknowledgedPrefix).empty();
    report("recovery does not replay accepted prefix", okay, true, acknowledgedPrefix.pendingBytes());


    // At 4 Hz the recorder fell behind when every reading paid for its own
    // open, flush, close and read-back verify. Measure opens per reading.
    resetCard();
    DurableQueue single;
    okay = single.begin();
    unsigned before = cardOpens;
    for (unsigned index = 0; okay && index < 16; index++) okay = append(single, first);
    const unsigned singleOpens = cardOpens - before;
    resetCard();
    DurableQueue batched;
    okay = okay && batched.begin();
    const char* frames[16];
    uint16_t lengths[16];
    for (unsigned index = 0; index < 16; index++) { frames[index] = first.c_str(); lengths[index] = first.size(); }
    before = cardOpens;
    okay = okay && batched.appendBatch(frames, lengths, 16);
    const unsigned batchOpens = cardOpens - before;
    before = cardOpens;
    for (unsigned index = 0; okay && index < 16; index++) okay = peek(batched) == first;
    const unsigned replayOpens = cardOpens - before;
    std::cout << "{\"scenario\":\"SD opens for 16 readings\",\"status\":\"PASS\",\"observed\":" << batchOpens
              << ",\"single_append_opens\":" << singleOpens << ",\"replay_opens\":" << replayOpens << "}\n";
    report("batched append and read-ahead replay open the journal once each", okay && batchOpens == 2 &&
           replayOpens == 1 && batched.pendingBytes() == 16 * (12 + first.size()), false, batchOpens + replayOpens);

    // Capture identity is assigned once, persisted with the SD record, and
    // remains available after a simulated power cycle/recovery.
    {
        resetCard();
        constexpr uint64_t session = 0x123456789abcdef0ULL;
        constexpr uint32_t sequence = 0x10203040UL;
        DurableQueue writer;
        okay = writer.begin() && writer.appendIdentified(first.c_str(), first.size(), session, sequence);
        DurableQueue reader;
        okay = okay && reader.begin();
        char body[8192];
        uint16_t length = 0;
        JournalCaptureIdentity restored;
        okay = okay && reader.peekIdentified(body, sizeof(body), &length, &restored) &&
            length == first.size() && !memcmp(body, first.data(), length) &&
            restored.session == session && restored.sequence == sequence;
        report("SD journal preserves capture identity across reopen", okay, false, restored.sequence);
    }

    {
        resetCard();
        constexpr uint64_t session = 0x123456789abcdef0ULL;
        DurableQueue identified;
        okay = identified.begin() &&
            identified.appendIdentified(first.c_str(), first.size(), session, 101) &&
            identified.appendIdentified(second.c_str(), second.size(), session, 103);
        CStorageRAM wire;
        static char scratch[8192];
        uint16_t lastLength = 0;
        uint32_t sequences[2] = {};
        uint64_t builtSession = 0;
        bool currentBoot = false;
        const uint8_t built = okay ? buildReplayBatch(identified, wire, scratch, sizeof(scratch), 2,
            &lastLength, &currentBoot, sequences, &builtSession, true) : 0;
        const std::string expected =
            "FQI1,123456789abcdef0,101,14,5038f9a9\n0:1000,10C:900\n"
            "FQI1,123456789abcdef0,103,14,f766ba6d\n0:1250,10C:910\n";
        const bool wireOkay = built == 2 && builtSession == session && sequences[0] == 101 &&
            sequences[1] == 103 && std::string(wire.buffer(), wire.length()) == expected;
        report("identified SD replay envelope preserves exact IDs and tolerates sequence holes",
               okay && wireOkay, false, built);
    }

    {
        resetCard();
        constexpr uint64_t session = 0x123456789abcdef0ULL;
        DurableQueue wrapped;
        okay = wrapped.begin() &&
            wrapped.appendIdentified(first.c_str(), first.size(), session, UINT32_MAX) &&
            wrapped.appendIdentified(second.c_str(), second.size(), session, 0);
        CStorageRAM wire;
        static char scratch[8192];
        uint16_t lastLength = 0;
        uint32_t sequences[2] = {};
        uint64_t builtSession = 0;
        bool currentBoot = false;
        const uint8_t built = okay ? buildReplayBatch(wrapped, wire, scratch, sizeof(scratch), 2,
            &lastLength, &currentBoot, sequences, &builtSession, true) : 0;
        const std::string encoded(wire.buffer(), wire.length());
        const size_t secondEnvelopeAt = encoded.find('\n');
        const bool wireOkay = built == 2 && builtSession == session &&
            sequences[0] == UINT32_MAX && sequences[1] == 0 && secondEnvelopeAt != std::string::npos &&
            encoded.find("FQI1,123456789abcdef0,4294967295,", 0) == 0 &&
            encoded.find("FQI1,123456789abcdef0,0,", secondEnvelopeAt + 1) != std::string::npos;
        report("identified SD replay preserves capture-sequence uint32 rollover",
               okay && wireOkay, false, built);
    }

    {
        resetCard();
        DurableQueue journal;
        okay = journal.begin() &&
            journal.appendIdentified(first.c_str(), first.size(), 9, 101) &&
            append(journal, second) &&
            journal.appendIdentified(first.c_str(), first.size(), 9, 103);
        auto& bytes = *cardFiles.at("/QUEUE.BIN");
        const size_t secondBody = 24 + first.size() + 12;
        bytes[secondBody] ^= 1;
        char body[8192];
        uint16_t length = 0;
        JournalCaptureIdentity identity;
        okay = okay && journal.peekIdentified(body, sizeof(body), &length, &identity) && identity.sequence == 101;
        okay = okay && !journal.peekIdentified(body, sizeof(body), &length, &identity) && journal.damaged();
        okay = okay && journal.recover();
        journal.retry();
        okay = okay && journal.peekIdentified(body, sizeof(body), &length, &identity) && identity.sequence == 101;
        okay = okay && journal.peekIdentified(body, sizeof(body), &length, &identity) && identity.sequence == 103;
        report("SD recovery preserves IDs on intact records around corruption", okay, journal.damaged(), identity.sequence);
    }

    // Building a 24-frame upload batch takes the SD lock once, not once per
    // frame; each per-frame acquisition could wait behind the recorder.
    {
        resetCard();
        DurableQueue lockQueue;
        okay = lockQueue.begin();
        for (unsigned index = 0; okay && index < 30; index++) okay = append(lockQueue, first);
        CStorageRAM wire;
        static char scratch[8192];
        uint16_t lastLength = 0;
        const unsigned before = sdTopLocks;
        const uint8_t built = okay ? buildReplayBatch(lockQueue, wire, scratch, sizeof(scratch), 24,
                                                       &lastLength, nullptr) : 0;
        const unsigned locks = sdTopLocks - before;
        report("upload batch build takes the SD lock once", built == 24 && locks == 1 && sdLockDepth == 0, false, locks);
    }

    resetCard();
    cardOnline = false;
    DurableQueue absent;
    report("absent card cannot accept a reading", !absent.begin() && !append(absent, first) && !absent.healthy(),
           false, absent.pendingBytes());

    resetCard();
    DurableQueue full;
    okay = full.begin() && append(full, first);
    const auto bytes = *cardFiles.at("/QUEUE.BIN");
    cardWriteBudget = 0;
    okay = okay && !append(full, second) && !full.healthy() && *cardFiles.at("/QUEUE.BIN") == bytes;
    report("full card keeps existing journal bytes", okay, false, cardFiles.at("/QUEUE.BIN")->size());

    resetCard();
    cardClock = 1760000000;
    DurableQueue archive;
    okay = archive.begin() && append(archive, first) && peek(archive) == first && archive.acknowledge();
    okay = okay && SD.exists("/DATA/1760000000.BIN") && archive.pendingBytes() == 0 && append(archive, second);
    report("accepted journal rotates into retained archive", okay, false, archive.pendingBytes());

    resetCard();
    cardClock = 1760000000;
    DurableQueue rotation;
    okay = rotation.begin() && append(rotation, first) && peek(rotation) == first;
    cardResetAfterRename = true;
    bool reset = false;
    try { rotation.acknowledge(); } catch (const std::runtime_error&) { reset = true; }
    cardResetAfterRename = false;
    DurableQueue rotationRestart;
    okay = okay && reset && rotationRestart.begin() && append(rotationRestart, second);
    DurableQueue newJournalRestart;
    okay = okay && newJournalRestart.begin() && peek(newJournalRestart) == second;
    report("reset during accepted rotation cannot apply stale cursor to new journal", okay, true, newJournalRestart.readPosition());

    resetCard();
    cardClock = 1760000000;
    cardRenameFails = true;
    DurableQueue failedArchive;
    okay = failedArchive.begin() && append(failedArchive, first) && peek(failedArchive) == first;
    const auto original = *cardFiles.at("/QUEUE.BIN");
    okay = okay && failedArchive.acknowledge() && *cardFiles.at("/QUEUE.BIN") == original &&
           append(failedArchive, second);
    report("failed archive rename preserves journal", okay, false, failedArchive.pendingBytes());
}

class DriveBridge : public CLink
{
public:
    unsigned rpm = 900;
    byte pid = PID_RPM;
    bool send(const char* command) override { pid = hex2uint8(command + 2); return true; }
    int receive(char* buffer, int capacity, unsigned int) override
    {
        const unsigned encoded = rpm * 4;
        if (pid == PID_SPEED) return snprintf(buffer, capacity, "41 0D 32\r>");
        return snprintf(buffer, capacity, "41 0C %02X %02X\r>", encoded >> 8, encoded & 255);
    }
};

int runJournalDrive()
{
    resetCard();
    DurableQueue recording;
    DriveBridge bridge;
    COBD obd;
    obd.begin(&bridge);
    if (!recording.begin()) return 1;
    for (unsigned index = 0; index < 240; index++) {
        bridge.rpm = 900 + index * 10;
        float value = 0;
        float speed = 0;
        if (!obd.readPID(PID_RPM, value)) return 1;
        if (!obd.readPID(PID_SPEED, speed)) return 1;
        char frame[160];
        snprintf(frame, sizeof(frame), "0:%u,10C:%u,10D:%u,89:1,", 1000 + index * 250,
                 (unsigned)value, (unsigned)speed);
        if (!append(recording, frame)) return 1;
    }
    // Restart while all readings are offline. Only the fake SD bytes survive.
    DurableQueue upload;
    if (!upload.begin()) return 1;
    unsigned requests = 0;
    unsigned lostAcknowledgements = 0;
    unsigned accepted = 0;
    while (true) {
        const std::string frame = peek(upload);
        if (frame.empty()) break;
        CStorageRAM wire;
        memcpy(wire.m_cache, frame.data(), frame.size());
        wire.m_cacheBytes = frame.size();
        wire.tailer();
        std::cout << "{\"event\":\"upload\",\"packet\":\"" << wire.m_cache << "\"}" << std::endl;
        std::string response;
        if (!std::getline(std::cin, response)) return 1;
        requests++;
        if (response != "ACK") return 1;
        // Lose every twentieth response once, after the collector accepts it.
        if (accepted % 20 == 0 && lostAcknowledgements == accepted / 20) {
            lostAcknowledgements++;
            upload.retry();
            continue;
        }
        if (!upload.acknowledge()) return 1;
        accepted++;
    }
    report("offline drive restart and acknowledgement loss", accepted == 240 && requests == 252 &&
           lostAcknowledgements == 12 && upload.pendingBytes() == 0, false, accepted);
    return 0;
}

// Boot A leaves 40 unsent readings, one of them malformed (an empty value).
// Boot B restarts the device clock at 1000 ms and records 40 more. Replay uses
// the production batch builder and acknowledgement policy against a real
// collector, whose answers arrive on stdin as "<status> <body>".
int runRebootDrive(bool strictServer)
{
    resetCard();
    DurableQueue recording;
    if (!recording.begin()) return 1;
    for (unsigned index = 0; index < 40; index++) {
        char frame[96];
        if (index == 20) snprintf(frame, sizeof(frame), "0:%u,10C:,10D:40,89:1,", 900000 + index * 250);
        else snprintf(frame, sizeof(frame), "0:%u,10C:%u,10D:40,89:1,", 900000 + index * 250, 900 + index);
        if (!append(recording, frame)) return 1;
    }
    for (unsigned index = 0; index < 40; index++) {
        char frame[96];
        snprintf(frame, sizeof(frame), "0:%u,10C:%u,10D:0,89:1,", 1000 + index * 250, 800 + index);
        if (!append(recording, frame)) return 1;
    }
    DurableQueue upload;
    if (!upload.begin()) return 1;
    static char frame[8192];
    uint16_t lastLength = 0;
    ReplayIsolation isolation = {0, 0};
    unsigned frames = 0, batches = 0;
    for (unsigned attempt = 0; attempt < 200; attempt++) {
        CStorageRAM wire;
        const uint8_t count = buildReplayBatch(upload, wire, frame, sizeof(frame),
                                                replayBatchLimit(isolation, 24), &lastLength, nullptr);
        if (!count) break;
        wire.m_cache[wire.m_cacheBytes] = 0;
        std::cout << "{\"event\":\"upload\",\"packet\":\"" << wire.m_cache << "\"}" << std::endl;
        std::string response;
        if (!std::getline(std::cin, response)) return 1;
        const unsigned status = strtoul(response.c_str(), nullptr, 10);
        const bool sent = response == "200 OK " + std::to_string(count * 3);
        if (sent) frames += count;
        batches++;
        settleReplayBatch(upload, isolation, sent, status, count, frame, lastLength);
    }
    const bool rejectKept = SD.exists("/QUEUE.REJ") && cardFiles.at("/QUEUE.REJ")->size() > 12;
    if (strictServer) {
        // A server that refuses whole batches: the device must isolate the bad
        // frame, keep it on the card and deliver every other frame.
        report("strict server: every valid frame delivered, refused frame kept on the card",
               frames == 79 && upload.rejectedCount() == 1 && rejectKept && upload.pendingBytes() == 0, false, frames);
        report("strict server: requests to isolate the refused frame", batches < 20, false, batches);
    } else {
        // The collector sets malformed samples aside itself and acknowledges
        // the whole batch, so the device never needs its reject file.
        report("collector acknowledges every frame; device keeps nothing aside",
               frames == 80 && upload.rejectedCount() == 0 && upload.pendingBytes() == 0, false, frames);
        report("collector reboot replay requests", batches < 8, false, batches);
    }
    return 0;
}

// This uses the production durable queue and replay-batch code. The frame has
// repeated waveform fields so the collector and history indexer can prove that
// order and each acquisition clock survive an offline restart.
int runWaveformDrive()
{
    resetCard();
    DurableQueue recording;
    if (!recording.begin()) return 1;
#ifndef WAVEFORM_FIXTURE
#define WAVEFORM_FIXTURE "0:1000,A5:1,A0:990;1234,A1:980,A2:0.100000;0.200000;1.000000,A3:1.000000;2.000000;3.000000,A4:0;0;0;0,"
#endif
    // run.py supplies this from tools/check-sampling-boundary.py. It is a
    // complete CBuffer serialization, not a handwritten waveform payload.
    const char* waveform = WAVEFORM_FIXTURE;
    if (!append(recording, waveform)) return 1;

    // Only fake SD bytes survive this simulated device restart.
    DurableQueue upload;
    if (!upload.begin()) return 1;
    static char frame[8192];
    uint16_t lastLength = 0;
    ReplayIsolation isolation = {0, 0};
    unsigned attempts = 0;
    unsigned delivered = 0;
    for (; attempts < 4; attempts++) {
        CStorageRAM wire;
        const uint8_t count = buildReplayBatch(upload, wire, frame, sizeof(frame),
                                                replayBatchLimit(isolation, 24), &lastLength, nullptr);
        if (!count) break;
        wire.m_cache[wire.m_cacheBytes] = 0;
        std::cout << "{\"event\":\"waveform-upload\",\"packet\":\"" << wire.m_cache << "\"}" << std::endl;
        std::string response;
        if (!std::getline(std::cin, response)) return 1;
        const unsigned status = strtoul(response.c_str(), nullptr, 10);
        const bool sent = status == 200;
        if (sent) delivered += count;
        settleReplayBatch(upload, isolation, sent, status, count, frame, lastLength);
    }
    report("waveform journal replay retries a lost acknowledgement", attempts == 2 && delivered == 1 &&
           upload.pendingBytes() == 0, false, delivered);
    return 0;
}
