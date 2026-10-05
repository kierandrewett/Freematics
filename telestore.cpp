#include <FreematicsPlus.h>
#include "telestore.h"
#include "config.h"
#include "sdaccess.h"
#include "sdarchive.h"
#include <errno.h>
#include <time.h>

void CStorage::log(uint16_t pid, uint8_t values[], uint8_t count)
{
    char buf[256];
    byte n = snprintf(buf, sizeof(buf), "%X%c%u", pid, m_delimiter, (unsigned int)values[0]);
    for (byte m = 1; m < count; m++) {
        n += snprintf(buf + n, sizeof(buf) - n, ";%u", (unsigned int)values[m]);
    }
    dispatch(buf, n);
}

void CStorage::log(uint16_t pid, uint16_t values[], uint8_t count)
{
    char buf[256];
    byte n = snprintf(buf, sizeof(buf), "%X%c%u", pid, m_delimiter, (unsigned int)values[0]);
    for (byte m = 1; m < count; m++) {
        n += snprintf(buf + n, sizeof(buf) - n, ";%u", (unsigned int)values[m]);
    }
    dispatch(buf, n);
}

void CStorage::log(uint16_t pid, uint32_t values[], uint8_t count)
{
    char buf[256];
    byte n = snprintf(buf, sizeof(buf), "%X%c%u", pid, m_delimiter, values[0]);
    for (byte m = 1; m < count; m++) {
        n += snprintf(buf + n, sizeof(buf) - n, ";%u", values[m]);
    }
    dispatch(buf, n);
}

void CStorage::log(uint16_t pid, int32_t values[], uint8_t count)
{
    char buf[256];
    byte n = snprintf(buf, sizeof(buf), "%X%c%d", pid, m_delimiter, values[0]);
    for (byte m = 1; m < count; m++) {
        n += snprintf(buf + n, sizeof(buf) - n, ";%d", values[m]);
    }
    dispatch(buf, n);
}

void CStorage::log(uint16_t pid, float values[], uint8_t count, const char* fmt)
{
    char buf[256];
    char *p = buf + snprintf(buf, sizeof(buf), "%X%c", pid, m_delimiter);
    for (byte m = 0; m < count && (p - buf) < sizeof(buf) - 3; m++) {
        if (m > 0) *(p++) = ';';
        int l = snprintf(p, sizeof(buf) - (p - buf), fmt, values[m]);
        char *q = strchr(p, '.');
        if (q && atoi(q + 1) == 0) {
            *q = 0;
            if (*p == '-' && *(p + 1) == '0') {
                *p = '0';
                *(++p) = 0;
            } else {
                p = q;
            }
        } else {
            p += l;
        }
    }
    dispatch(buf, (int)(p - buf));
}
void CStorage::logHex(uint16_t pid, const uint8_t values[], uint8_t count)
{
    if (!values || !count) return;
    char buf[256];
    int length = snprintf(buf, sizeof(buf), "%X%c", pid, m_delimiter);
    if (length < 0 || length >= (int)sizeof(buf)) return;
    for (uint8_t index = 0; index < count && length < (int)sizeof(buf) - 3; index++) {
        length += snprintf(buf + length, sizeof(buf) - (size_t)length, "%02X", values[index]);
    }
    dispatch(buf, (byte)length);
}

void CStorage::timestamp(uint32_t ts)
{
    log(PID_TIMESTAMP, &ts, 1);
}

void CStorage::dispatch(const char* buf, byte len)
{
    // output data via serial
    Serial.write((uint8_t*)buf, len);
    Serial.write(' ');
    m_samples++;
}

byte CStorage::checksum(const char* data, int len)
{
    byte sum = 0;
    for (int i = 0; i < len; i++) sum += data[i];
    return sum;
}

void CStorageRAM::dispatch(const char* buf, byte len)
{
    if (m_overflowed) return;
    // reserve space for the delimiter, checksum marker, checksum and NUL
    if (m_cacheBytes > m_cacheSize || (unsigned int)len + 4 > m_cacheSize - m_cacheBytes) {
        // Mark the transaction as failed so callers can roll back the whole
        // sample instead of sending a silently truncated record.
        m_overflowed = true;
        return;
    }
    // store data in m_cache
    memcpy(m_cache + m_cacheBytes, buf, len);
    m_cacheBytes += len;
    m_cache[m_cacheBytes++] = ',';
    m_samples++;
}

bool CStorageRAM::appendRaw(const char* data, unsigned int length)
{
    if (!m_cache || !data || !length || m_overflowed ||
        m_cacheBytes > m_cacheSize || length + 4 > m_cacheSize - m_cacheBytes) {
        m_overflowed = true;
        return false;
    }
    memcpy(m_cache + m_cacheBytes, data, length);
    m_cacheBytes += length;
    return true;
}

void CStorageRAM::checkpoint()
{
    m_checkpointBytes = m_cacheBytes;
    m_checkpointSamples = m_samples;
    m_overflowed = false;
}

void CStorageRAM::rollback()
{
    m_cacheBytes = m_checkpointBytes;
    m_samples = m_checkpointSamples;
    m_overflowed = false;
}

void CStorageRAM::header(const char* devid)
{
    m_cacheBytes = sprintf(m_cache, "%s#", devid);
}

void CStorageRAM::tailer()
{
    if (m_overflowed || !m_cacheBytes) return;
    if (m_cache[m_cacheBytes - 1] == ',') m_cacheBytes--;
    m_cacheBytes += sprintf(m_cache + m_cacheBytes, "*%X", (unsigned int)checksum(m_cache, m_cacheBytes));
}

void CStorageRAM::untailer()
{
    char *p = strrchr(m_cache, '*');
    if (p) {
        *p = ',';
        m_cacheBytes = p + 1 - m_cache;
    }
}

void FileLogger::dispatch(const char* buf, byte len)
{
    if (m_id == 0) return;

    if (m_file.write((uint8_t*)buf, len) != len) {
        // try again
        if (m_file.write((uint8_t*)buf, len) != len) {
            Serial.println("Error writing. End file logging.");
            end();
            return;
        }
    }
    if (m_file.write('\n') != 1) {
        Serial.println("Error writing newline. End file logging.");
        end();
        return;
    }
    m_size += (len + 1);
}

int FileLogger::getFileID(File& root)
{
    if (root) {
        File file;
        int id = 0;
        while(file = root.openNextFile()) {
            char *p = strrchr(file.name(), '/');
            unsigned int n = atoi(p ? p + 1 : file.name());
            if (n > id) id = n;
        }
        return id + 1;
    } else {
        return 0;
    }
}

bool SDLogger::init()
{
    SDGuard guard;
    if (!guard) return false;
    SPI.begin();
    bool mounted = false;
#ifdef FREEMATICS_FORMAT_SD_ONCE
    Serial.println("[STORAGE] SD provisioning image: unformatted card may be formatted now");
    mounted = SD.begin(PIN_SD_CS, SPI, SPI_FREQ, "/sd", 5, true);
#else
    // Power and SPI can settle after the ESP32 starts. Retry the mount, but
    // never format a production card or treat RAM as durable fallback storage.
    for (uint8_t attempt = 0; attempt < 3 && !mounted; attempt++) {
        if (attempt) {
            SD.end();
            // SD.end() only tears down a mounted filesystem. If card init
            // failed before _pdrv was assigned, it is a no-op; SPI.begin()
            // inside the next SD.begin() is also a no-op while the bus is
            // active. Force a real host/controller restart between attempts.
            SPI.end();
            delay(250);
        }
        const uint32_t started = millis();
        Serial.print("[SD-DIAG] mount begin attempt=");
        Serial.print(attempt + 1);
        Serial.print("/3 spi_hz=");
        Serial.println(SPI_FREQ);
        mounted = SD.begin(PIN_SD_CS, SPI, SPI_FREQ);
        Serial.print("[SD-DIAG] mount ");
        Serial.print(mounted ? "ok" : "failed");
        Serial.print(" attempt=");
        Serial.print(attempt + 1);
        Serial.print(" elapsed_ms=");
        Serial.println(millis() - started);
    }
#endif
    if (mounted) {
        unsigned int total = SD.totalBytes() >> 20;
        unsigned int used = SD.usedBytes() >> 20;
        Serial.print("SD:");
        Serial.print(total);
        Serial.print(" MB total, ");
        Serial.print(used);
        Serial.println(" MB used");
        return true;
    } else {
        Serial.println("NO SD CARD");
        return false;
    }
}

uint32_t SDLogger::begin()
{
    SDGuard guard;
    if (!guard) return 0;
    m_retentionRoot.close();
    m_file.close();
    char path[24];
    // Scanning /DATA for the highest ID opens every file. On 30 September that
    // took about 100 s per boot, and nothing was recorded until it finished.
    // A name only needs to be unique (retention reads the .UTC companion), so
    // use the clock, restored from NVS at boot, and step past any clash.
    const time_t clock = time(nullptr);
    if (clock >= 1704067200 && clock <= 2145916799) {
        if (!SD.exists("/DATA")) SD.mkdir("/DATA");
        m_id = (uint32_t)clock;
        for (uint8_t attempt = 0; attempt < 100; attempt++, m_id++) {
            sprintf(path, "/DATA/%u.CSV", m_id);
            if (!SD.exists(path)) break;
        }
    } else {
        File root = SD.open("/DATA");
        m_id = getFileID(root);
        if (m_id == 0) {
            SD.mkdir("/DATA");
            m_id = 1;
        }
    }
    sprintf(path, "/DATA/%u.CSV", m_id);
    Serial.print("File: ");
    Serial.println(path);
    m_file = SD.open(path, FILE_WRITE);
    if (!m_file) {
        Serial.print("[SD-DIAG] csv open failed errno=");
        Serial.println(errno);
        m_id = 0;
    }
    m_dataCount = 0;
    m_size = 0;
    m_retentionDay = 0;
    return m_id;
}

void SDLogger::flush()
{
    SDGuard guard;
    if (guard) m_file.flush();
}

void SDLogger::end()
{
    SDGuard guard;
    if (guard) {
        m_retentionRoot.close();
        FileLogger::end();
    }
}

void SDLogger::dispatch(const char* buf, byte len)
{
    SDGuard guard;
    if (guard) FileLogger::dispatch(buf, len);
}

void SDLogger::maintain()
{
    // Protect the whole lifecycle operation, including date rollover.
    // Logger methods re-enter this recursive SD lock safely.
    SDGuard guard;
    if (!guard) return;
    time_t clock = time(nullptr);
    if (clock < 1704067200 || clock > UINT32_MAX || !m_id) return;
    const uint32_t now = (uint32_t)clock;
    const uint32_t day = now / 86400UL;
    if (m_retentionDay && m_retentionDay != day) {
        end();
        if (!begin()) return;
    }
    if (!m_retentionRoot) {
        if (m_retentionDay == day && millis() - m_lastMaintenance < 3600000UL) return;
        m_retentionRoot = SD.open("/DATA");
        if (!m_retentionRoot) return;
        m_retentionDay = day;
        m_lastMaintenance = millis();
    }
    // A large archive must not stop fresh collection. Continue the directory
    // scan across collection cycles, with bounded work on each call.
    const uint32_t started = millis();
    for (uint8_t scanned = 0; scanned < 4 && millis() - started < 100; scanned++) {
        File entry = m_retentionRoot.openNextFile();
        if (!entry) {
            m_retentionRoot.close();
            break;
        }
        char path[48];
        snprintf(path, sizeof(path), "%s", entry.path());
        entry.close();
        if (!localLogPath(path) || !strcmp(strrchr(path, '.'), ".UTC")) continue;
        char metadata[48];
        snprintf(metadata, sizeof(metadata), "%s", path);
        strcpy(strrchr(metadata, '.'), ".UTC");
        uint32_t anchor = 0;
        File date = SD.open(metadata, FILE_READ);
        char value[24] = {0};
        if (date) {
            date.read((uint8_t*)value, sizeof(value) - 1);
            date.close();
            anchor = strtoul(value, nullptr, 10);
        }
        if (anchor < 1704067200UL || anchor > now) {
            // Old firmware did not record creation time. Start a full new
            // 14-day window, rather than guessing and deleting those files.
            date = SD.open(metadata, FILE_WRITE);
            if (date) {
                date.printf("%lu\n", (unsigned long)now);
                date.flush();
                date.close();
            }
            continue;
        }
        const unsigned long id = strtoul(path + 6, nullptr, 10);
        if (id != m_id && localLogExpired(anchor, now) && SD.remove(path)) {
            SD.remove(metadata);
            Serial.print("[STORAGE] Removed local archive older than 14 days: ");
            Serial.println(path);
        }
    }
}

bool SPIFFSLogger::init()
{
    bool mounted = SPIFFS.begin();
    if (mounted) {
        Serial.print("[STORAGE] Internal flash: ");
        Serial.print(SPIFFS.totalBytes());
        Serial.print(" bytes total | used: ");
        Serial.print(SPIFFS.usedBytes());
        Serial.println(" bytes");
    } else {
        // A transient mount failure must not erase the only local copy of a
        // trip. Recovery or formatting requires an explicit maintenance step.
        Serial.println("[STORAGE] Internal flash mount failed; existing logs preserved");
    }
    return mounted;
}

uint32_t SPIFFSLogger::begin()
{
    if (SPIFFS.totalBytes() - SPIFFS.usedBytes() < SPIFFS_RESERVE_BYTES) {
        // Old trip logs may be the only surviving copy after an outage.
        Serial.println("[STORAGE] Flash reserve reached; existing logs preserved");
        return 0;
    }
    // SPIFFS uses flat filenames, but its VFS accepts a /DATA prefix. New
    // partitions need that path initialised before the first trip file.
    if (!SPIFFS.exists("/DATA")) SPIFFS.mkdir("/DATA");
    File root = SPIFFS.open("/");
    m_id = getFileID(root);
    char path[24];
    sprintf(path, "/DATA/%u.CSV", m_id);
    Serial.print("[STORAGE] Local trip log: ");
    Serial.println(path);
    m_file = SPIFFS.open(path, FILE_WRITE);
    if (!m_file) {
        Serial.println("File error");
        m_id = 0;
    }
    m_dataCount = 0;
    return m_id;
}

bool SPIFFSLogger::purgeOldest()
{
    // Remove one oldest completed log chunk. The current file is always
    // closed before begin() calls this method.
    File root = SPIFFS.open("/");
    File file;
    unsigned int idx = 0;
    char oldestPath[32] = {0};
    while(file = root.openNextFile()) {
        const char* path = file.path();
        if (!path) continue;
        const char* name = path[0] == '/' ? path + 1 : path;
        if (!strncmp(name, "DATA/", 5)) {
            const char* number = name + 5;
            char* end = 0;
            unsigned long n = strtoul(number, &end, 10);
            if (n != 0 && end && !strcmp(end, ".CSV") && (idx == 0 || n < idx)) {
                idx = (unsigned int)n;
                snprintf(oldestPath, sizeof(oldestPath), "/DATA/%u.CSV", idx);
            }
        }
    }
    if (idx) {
        if (SPIFFS.remove(oldestPath)) {
            Serial.print(oldestPath);
            Serial.println(" removed");
            return true;
        }
    }
    return false;
}
