#include <iostream>
#include <iomanip>
#include <string>
#include "FreematicsBase.h"
#include "FreematicsOBD.h"
#include "queue_scenario.h"

uint32_t simulationTime = 0;
void runJournalScenarios();
void runMEMSScenarios();
int runJournalDrive();
int runRebootDrive(bool strictServer);
int runWaveformDrive();

class DummyBridge : public CLink
{
public:
    std::string reply;
    std::string command;
    uint32_t latency = 12;
    bool disconnected = false;

    bool send(const char* text) override
    {
        command = text;
        return true;
    }

    int sendCommand(const char* text, char* buffer, int capacity, unsigned int) override
    {
        command = text;
        const int length = snprintf(buffer, capacity, "%s", reply.c_str());
        return length < capacity ? length : capacity - 1;
    }

    int receive(char* buffer, int capacity, unsigned int timeout) override
    {
        if (disconnected || latency > timeout) {
            delay(timeout);
            if (capacity) buffer[0] = 0;
            return 0;
        }
        delay(latency);
        const int length = snprintf(buffer, capacity, "%s", reply.c_str());
        return length < capacity ? length : capacity - 1;
    }
};

void report(const char* name, bool passed, bool fault, double observed)
{
    std::cout << "{\"scenario\":\"" << name << "\",\"status\":\""
              << (passed ? "PASS" : fault ? "ISSUE" : "ERROR")
              << "\",\"observed\":" << observed << "}\n";
}

int main(int argc, char** argv)
{
    std::cout << std::setprecision(17);
    if (argc == 2 && std::string(argv[1]) == "--drive") return runJournalDrive();
    if (argc == 2 && std::string(argv[1]) == "--drive-reboot") return runRebootDrive(false);
    if (argc == 2 && std::string(argv[1]) == "--drive-reboot-strict") return runRebootDrive(true);
    if (argc == 2 && std::string(argv[1]) == "--drive-waveforms") return runWaveformDrive();
    DummyBridge bridge;
    COBD obd;
    obd.begin(&bridge);
    float value = -123;

    bridge.reply = "41 0C 0E 10\r>";
    bool okay = obd.readPID(PID_RPM, value);
    report("normal RPM 900", okay && value == 900 && bridge.command == "010C\r", false, value);

    char vin[128] = {};
    bridge.reply = "014\r\n0:49020157304C3053444C36384434303530383431\r\n>";
    const bool vinOkay = obd.getVIN(vin, sizeof(vin));
    report("VIN from compact ISO-TP response", vinOkay && std::string(vin) == "W0L0SDL68D4050841" &&
           bridge.command == "0902\r", true, vinOkay ? 1 : 0);

    bridge.reply = "014\r\n0: 49 02 01 57 30 4C\r\n1: 30 53 44 4C 36 38 44\r\n"
                   "2: 34 30 35 30 38 34 31\r\n>";
    const bool splitVinOkay = obd.getVIN(vin, sizeof(vin));
    report("VIN from spaced numbered continuation frames",
           splitVinOkay && std::string(vin) == "W0L0SDL68D4050841", true, splitVinOkay ? 1 : 0);

    bridge.reply = "49 02 01 57 30 4C 30 53 44 4C 36 38 44 34 30 35 30 38 34 31\r>";
    const bool rawVinOkay = obd.getVIN(vin, sizeof(vin));
    report("VIN from a spaced single-frame response",
           rawVinOkay && std::string(vin) == "W0L0SDL68D4050841", true, rawVinOkay ? 1 : 0);

    bridge.reply = "014\r\n0: 49 02 01 49 30 4C 30 53 44 4C 36 38 44 34 30 35 30 38 34 31\r\n>";
    const bool invalidVinOkay = obd.getVIN(vin, sizeof(vin));
    report("reject invalid VIN alphabet", !invalidVinOkay, true, invalidVinOkay ? 1 : 0);

    bridge.reply = "41 0D 32\r>";
    okay = obd.readPID(PID_SPEED, value);
    report("normal speed 50", okay && value == 50, false, value);

    bridge.disconnected = true;
    const uint32_t start = simulationTime;
    value = -123;
    okay = obd.readPID(PID_RPM, value);
    report("ECU outage and unchanged output", !okay && value == -123 && obd.errors == 1 &&
           simulationTime - start == OBD_TIMEOUT_SHORT + 5, false, simulationTime - start);
    bridge.disconnected = false;
    bridge.reply = "41 0C 0E 10\r>";
    okay = obd.readPID(PID_RPM, value);
    report("recovery resets errors", okay && value == 900 && obd.errors == 0, false, value);

    const struct { const char* name; const char* reply; } rejectedReplies[] = {
        {"reject NO DATA", "NO DATA\r>"},
        {"reject wrong PID", "41 0D 32\r>"},
        {"reject empty payload", "41 0C "},
    };
    for (const auto& scenario : rejectedReplies) {
        bridge.reply = scenario.reply;
        value = -123;
        okay = obd.readPID(PID_RPM, value);
        report(scenario.name, !okay && value == -123, false, value);
    }
    bridge.reply = "41 0C 1A\r>";
    value = -123;
    okay = obd.readPID(PID_RPM, value);
    report("reject truncated RPM payload", !okay && value == -123, true, value);
    bridge.reply = "41 0D ZZ\r>";
    value = -123;
    okay = obd.readPID(PID_SPEED, value);
    report("reject invalid hexadecimal speed", !okay && value == -123, true, value);

    bridge.reply = "41 42 0C\r>";
    okay = obd.readPID(PID_CONTROL_MODULE_VOLTAGE, value);
    report("reject truncated two-byte voltage", !okay, true, value);
    bridge.reply = "41 24 80 00\r>";
    okay = obd.readPID(PID_O2_S1_WR_VOLTAGE, value);
    report("reject truncated four-byte oxygen value", !okay, true, value);
    bridge.reply = "41 0C  0e   10\r>";
    okay = obd.readPID(PID_RPM, value);
    report("RPM with lowercase bytes and extra spaces", okay && value == 900, true, value);

    unsigned failures = 0;
    unsigned successes = 0;
    unsigned mismatches = 0;
    for (unsigned i = 0; i < 200; ++i) {
        const unsigned rpm = 800 + i * 10;
        const unsigned raw = rpm * 4;
        char response[40];
        snprintf(response, sizeof(response), "41 0C %02X %02X\r>", raw >> 8, raw & 255);
        bridge.reply = response;
        bridge.disconnected = i >= 70 && i < 80;
        value = -123;
        okay = obd.readPID(PID_RPM, value);
        successes += okay;
        failures += !okay;
        mismatches += bridge.disconnected ? (okay || value != -123) : (!okay || value != rpm);
    }
    report("200 changing RPM replies with 10 timeouts", mismatches == 0 && successes == 190 && failures == 10,
           false, successes);

    bridge.disconnected = false;
    bridge.reply = "43 04 01 08 00 00\r>";
    uint16_t codes[4] = {};
    const int count = obd.readDTC(codes, 4);
    report("stored DTC P0108", count == 1 && codes[0] == 0x0108 && obd.getDTCStatus() == DTC_STATUS_CODES,
           false, codes[0]);

    CBuffer older = {BUFFER_STATE_FILLED, true, 0xffffff00};
    CBuffer newer = {BUFFER_STATE_FILLED, true, 10};
    CBuffer* entries[] = {&older, &newer};
    CBufferManager queue;
    queue.slots = entries;
    queue.total = 2;
    CBuffer* selected = queue.getOldest(true);
    report("FIFO across millisecond rollover", selected == &older, true, selected ? selected->timestamp : -1.0);

    older = {BUFFER_STATE_FILLED, true, 0xffffffff};
    queue.total = 1;
    selected = queue.getOldest(true);
    report("queue entry at maximum timestamp", selected == &older, true, selected ? selected->timestamp : -1.0);

    older = {BUFFER_STATE_FILLED, true, 0xffffff00};
    newer = {BUFFER_STATE_FILLED, true, 10};
    queue.total = 2;
    selected = queue.getNewest();
    report("newest across millisecond rollover", selected == &newer, true, selected ? selected->timestamp : -1.0);
    older = {BUFFER_STATE_FILLED, true, 0};
    queue.total = 1;
    selected = queue.getNewest();
    report("queue entry at zero timestamp", selected == &older, true, selected ? selected->timestamp : -1.0);
    runJournalScenarios();
    runMEMSScenarios();
}
