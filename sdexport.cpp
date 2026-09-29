#include <FreematicsPlus.h>
#include <SD.h>
#include <sys/time.h>
#include "sdaccess.h"
#include "sdarchive.h"
#include "sdexport.h"
#include "telequeue.h"
#include "telestore.h"

#if STORAGE == STORAGE_SD
extern DurableQueue durableQueue;
extern SDLogger logger;
extern volatile uint32_t lastCollectionTime;
extern char devid[];
extern nvs_handle_t nvs;
extern void beepTone(unsigned int frequency, int duration);

namespace {
bool readablePath(const char* path)
{
    return localLogPath(path) || !strcmp(path, "/QUEUE.BIN") || !strcmp(path, "/QUEUE.A") ||
        !strcmp(path, "/QUEUE.B");
}

void respond(uint32_t id, const char* json)
{
    char line[1200];
    int length = snprintf(line, sizeof(line), "\n[SD %lu] %s\n", (unsigned long)id, json);
    if (length > 0 && length < (int)sizeof(line)) Serial.write((const uint8_t*)line, length);
}

void request(char* command)
{
    unsigned long id = 0, offset = 0, count = 256;
    char operation[12] = {0}, path[64] = {0};
    const int fields = sscanf(command, "FSD %lu %11s %63s %lu %lu", &id, operation, path, &offset, &count);
    if (fields < 2) return;
    if (!strcmp(operation, "TIME") && fields == 3) {
        char* end = nullptr;
        unsigned long seconds = strtoul(path, &end, 10);
        if (!end || *end || seconds < 1704067200UL || seconds > 2145916799UL) {
            respond(id, "{\"error\":\"invalid time\"}");
            return;
        }
        struct timeval tv = {(time_t)seconds, 0};
        if (settimeofday(&tv, nullptr) != 0) {
            respond(id, "{\"error\":\"time update failed\"}");
            return;
        }
        nvs_set_u32(nvs, "last_utc", seconds);
        nvs_commit(nvs);
        respond(id, "{\"ok\":true}");
        Serial.println("[TIME] Clock set by USB host");
        return;
    }
    if (!strcmp(operation, "BEEP")) {
        beepTone(1600, 120);
        delay(100);
        beepTone(1600, 120);
        delay(100);
        beepTone(1600, 120);
        respond(id, "{\"ok\":true}");
        return;
    }
    if (!strcmp(operation, "STATUS")) {
        char json[512];
        snprintf(json, sizeof(json),
            "{\"device\":\"%s\",\"build\":\"%s\",\"journal_healthy\":%s,"
            "\"local_healthy\":%s,\"pending_bytes\":%lu,\"last_collection_ms\":%lu,\"uptime_ms\":%lu,"
            "\"utc\":%lu,\"retention_days\":14,\"wifi_enabled\":%s}",
            devid, FREEMATICS_BUILD_ID, durableQueue.healthy() ? "true" : "false",
            logger.healthy() ? "true" : "false", (unsigned long)durableQueue.pendingBytes(),
            (unsigned long)lastCollectionTime, (unsigned long)millis(), (unsigned long)time(nullptr),
            ENABLE_WIFI ? "true" : "false");
        respond(id, json);
        return;
    }
    SDGuard guard;
    if (!guard) {
        respond(id, "{\"error\":\"storage busy\"}");
        return;
    }
    if (!strcmp(operation, "LIST") && fields >= 3 && (!strcmp(path, "/") || !strcmp(path, "/DATA"))) {
        File directory = SD.open(path);
        if (!directory || !directory.isDirectory()) {
            respond(id, "{\"error\":\"directory unavailable\"}");
            return;
        }
        char json[1024];
        size_t length = snprintf(json, sizeof(json), "{\"files\":[");
        unsigned long seen = 0, added = 0;
        bool more = false;
        for (File file = directory.openNextFile(); file; file = directory.openNextFile()) {
            const char* name = file.path();
            if ((!file.isDirectory() || strcmp(name, "/DATA")) && !readablePath(name)) continue;
            if (seen++ < offset) continue;
            if (added == 10) { more = true; break; }
            length += snprintf(json + length, sizeof(json) - length,
                "%s{\"path\":\"%s\",\"size\":%lu,\"directory\":%s}", added ? "," : "",
                name, (unsigned long)file.size(), file.isDirectory() ? "true" : "false");
            added++;
        }
        snprintf(json + length, sizeof(json) - length, "],\"next\":%lu,\"more\":%s}", offset + added,
            more ? "true" : "false");
        respond(id, json);
        return;
    }
    if (fields < 3 || !readablePath(path) || (strcmp(operation, "READ") && strcmp(operation, "STAT"))) {
        respond(id, "{\"error\":\"invalid read-only request\"}");
        return;
    }
    File file = SD.open(path, FILE_READ);
    if (!file || file.isDirectory()) {
        respond(id, "{\"error\":\"file unavailable\"}");
        return;
    }
    char json[768];
    if (!strcmp(operation, "STAT")) {
        snprintf(json, sizeof(json), "{\"size\":%lu}", (unsigned long)file.size());
    } else if (fields >= 4 && count > 0 && count <= 256 && offset <= file.size() && file.seek(offset)) {
        uint8_t bytes[256];
        const size_t expected = min((size_t)count, (size_t)(file.size() - offset));
        const size_t read = expected ? file.read(bytes, expected) : 0;
        if (read != expected) {
            respond(id, "{\"error\":\"short SD read\"}");
            return;
        }
        size_t length = snprintf(json, sizeof(json), "{\"offset\":%lu,\"hex\":\"", offset);
        for (size_t index = 0; index < read; index++) {
            length += snprintf(json + length, sizeof(json) - length, "%02x", bytes[index]);
        }
        snprintf(json + length, sizeof(json) - length, "\"}");
    } else {
        respond(id, "{\"error\":\"invalid read range\"}");
        return;
    }
    file.close();
    respond(id, json);
}
}

void processSDExport()
{
    static char line[160];
    static size_t length = 0;
    static bool dropping = false;
    while (Serial.available()) {
        char value = Serial.read();
        if (value == '\n' || value == '\r') {
            if (length && !dropping) {
                line[length] = 0;
                if (!strncmp(line, "FSD ", 4)) request(line);
            }
            length = 0;
            dropping = false;
        } else if (length < sizeof(line) - 1) {
            line[length++] = value;
        } else {
            dropping = true;
        }
    }
}
#endif
