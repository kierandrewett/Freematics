#include <FreematicsPlus.h>
#include "telestore.h"
#include "config.h"

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
    // reserve some space for checksum
    int remain = m_cacheSize - m_cacheBytes - len - 3;
    if (remain < 0) {
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
    m_file.write('\n');
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
    SPI.begin();
    if (SD.begin(PIN_SD_CS, SPI, SPI_FREQ)) {
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
    File root = SD.open("/DATA");
    m_id = getFileID(root);
    if (m_id == 0) {
        SD.mkdir("/DATA");
        m_id = 1;
    }
    char path[24];
    sprintf(path, "/DATA/%u.CSV", m_id);
    Serial.print("File: ");
    Serial.println(path);
    m_file = SD.open(path, FILE_WRITE);
    if (!m_file) {
        Serial.println("File error");
        m_id = 0;
    }
    m_dataCount = 0;
    return m_id;
}

void SDLogger::flush()
{
    char path[24];
    sprintf(path, "/DATA/%u.CSV", m_id);
    m_file.close();
    m_file = SD.open(path, FILE_APPEND);
    if (!m_file) {
        Serial.println("File error");
    }
}

bool SPIFFSLogger::init()
{
    bool mounted = SPIFFS.begin();
    if (!mounted) {
        Serial.println("Formatting SPIFFS...");
        mounted = SPIFFS.begin(true);
    }
    if (mounted) {
        Serial.print("[STORAGE] Internal flash: ");
        Serial.print(SPIFFS.totalBytes());
        Serial.print(" bytes total | used: ");
        Serial.print(SPIFFS.usedBytes());
        Serial.println(" bytes");
    } else {
        Serial.println("[STORAGE] Internal flash is not available");
    }
    return mounted;
}

uint32_t SPIFFSLogger::begin()
{
    while (SPIFFS.totalBytes() - SPIFFS.usedBytes() < SPIFFS_RESERVE_BYTES) {
        if (!purgeOldest()) break;
    }
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
    int idx = 0;
    while(file = root.openNextFile()) {
        if (!strncmp(file.name(), "/DATA/", 6)) {
            unsigned int n = atoi(file.name() + 6);
            if (n != 0 && (idx == 0 || n < idx)) idx = n;
        }
    }
    if (idx) {
        char path[32];
        sprintf(path, "/DATA/%u.CSV", idx);
        if (SPIFFS.remove(path)) {
            Serial.print(path);
            Serial.println(" removed");
            return true;
        }
    }
    return false;
}
