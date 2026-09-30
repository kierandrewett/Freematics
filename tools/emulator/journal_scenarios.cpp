#include <cerrno>
#include <ctime>
#include <iostream>
#include <string>
#include "FreematicsBase.h"
#include "FreematicsOBD.h"
#include "SD.h"
#include "telequeue.h"
#include "wire_scenario.h"

void report(const char* name, bool passed, bool fault, double observed);
inline time_t cardClock = 0;

// Wrap only the journal's POSIX creation and clock calls, not the production code.
extern "C" int __wrap_open(const char* path, int, ...)
{
    if (!cardOnline || strncmp(path, "/sd/", 4)) { errno = EIO; return -1; }
    const char* name = path + 3;
    if (SD.exists(name)) { errno = EEXIST; return -1; }
    cardFiles[name] = std::make_shared<std::vector<uint8_t>>();
    return 500;
}
extern "C" int __wrap_close(int) { return 0; }
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
    cardReadBudget = -1;
    cardRenameBudget = -1;
    cardResetAfterRename = false;
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
    report("torn tail repair accepts retained RAM reading", okay, true, tornRestart.pendingBytes());

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
        append(acknowledgedPrefix, second) && append(acknowledgedPrefix, first) &&
        peek(acknowledgedPrefix) == first && acknowledgedPrefix.acknowledge();
    (*cardFiles.at("/QUEUE.BIN"))[12 + first.size() + 12] ^= 1;
    okay = okay && peek(acknowledgedPrefix).empty() && acknowledgedPrefix.recover() &&
        peek(acknowledgedPrefix) == first && peek(acknowledgedPrefix).empty();
    report("recovery does not replay accepted prefix", okay, true, acknowledgedPrefix.pendingBytes());


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
