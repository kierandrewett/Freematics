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
File exportDirectory;
char exportDirectoryPath[8] = {0};
unsigned long exportNextOffset = 0;
unsigned long cachedID = 0;
char cachedCommand[160] = {0};
char cachedReply[1024] = {0};
bool replyCached = false;

bool readablePath(const char* path)
{
    return localLogPath(path) || !strcmp(path, "/QUEUE.BIN") || !strcmp(path, "/QUEUE.A") ||
        !strcmp(path, "/QUEUE.B");
}

void writeResponse(uint32_t id, const char* json)
{
    char line[1200];
    int length = snprintf(line, sizeof(line), "\n[SD %lu] %s\n", (unsigned long)id, json);
    if (length > 0 && length < (int)sizeof(line)) Serial.write((const uint8_t*)line, length);
}

void respond(uint32_t id, const char* json)
{
    snprintf(cachedReply, sizeof(cachedReply), "%s", json);
    replyCached = true;
    writeResponse(id, cachedReply);
}

void request(char* command)
{
    unsigned long id = 0, offset = 0, count = 256;
    char operation[12] = {0}, path[64] = {0};
    const int fields = sscanf(command, "FSD %lu %11s %63s %lu %lu", &id, operation, path, &offset, &count);
    if (fields < 2) return;
    if (replyCached && id == cachedID && !strcmp(command, cachedCommand)) {
        writeResponse(id, cachedReply);
        return;
    }
    cachedID = id;
    snprintf(cachedCommand, sizeof(cachedCommand), "%s", command);
    replyCached = false;
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
        if (!offset) {
            exportDirectory.close();
            exportDirectory = SD.open(path);
            snprintf(exportDirectoryPath, sizeof(exportDirectoryPath), "%s", path);
            exportNextOffset = 0;
        } else if (strcmp(path, exportDirectoryPath) || offset != exportNextOffset) {
            respond(id, "{\"error\":\"invalid directory continuation\"}");
            return;
        }
        if (!exportDirectory || !exportDirectory.isDirectory()) {
            respond(id, "{\"error\":\"directory unavailable\"}");
            return;
        }
        char json[1024];
        size_t length = snprintf(json, sizeof(json), "{\"files\":[");
        unsigned long added = 0;
        bool more = true;
        const uint32_t started = millis();
        while (added < 10 && millis() - started < 100) {
            File file = exportDirectory.openNextFile();
            if (!file) {
                more = false;
                exportDirectory.close();
                break;
            }
            exportNextOffset++;
            const char* name = file.path();
            if ((!file.isDirectory() || strcmp(name, "/DATA")) && !readablePath(name)) continue;
            length += snprintf(json + length, sizeof(json) - length,
                "%s{\"path\":\"%s\",\"size\":%lu,\"directory\":%s}", added ? "," : "",
                name, (unsigned long)file.size(), file.isDirectory() ? "true" : "false");
            added++;
        }
        snprintf(json + length, sizeof(json) - length, "],\"next\":%lu,\"more\":%s}", exportNextOffset,
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
    for (size_t consumed = 0; consumed < sizeof(line) && Serial.available(); consumed++) {
        char value = Serial.read();
        if (value == '\n' || value == '\r') {
            if (length && !dropping) {
                line[length] = 0;
                if (!strncmp(line, "FSD ", 4)) request(line);
            }
            length = 0;
            dropping = false;
            // Return to collection even if the host queued more commands.
            return;
        } else if (length < sizeof(line) - 1) {
            line[length++] = value;
        } else {
            dropping = true;
        }
    }
}

void resetSDExportDirectory()
{
    exportDirectory.close();
    exportDirectoryPath[0] = 0;
    exportNextOffset = 0;
    replyCached = false;
}
#endif
