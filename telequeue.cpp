#include "telequeue.h"

#if STORAGE == STORAGE_SD
#include <SD.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include "sdaccess.h"

namespace {
constexpr const char* DATA_PATH = "/QUEUE.BIN";
constexpr const char* RECOVERY_PATH = "/QUEUE.REC";
constexpr const char* CURSOR_A = "/QUEUE.A";
constexpr const char* CURSOR_B = "/QUEUE.B";
constexpr const char* REJECTED_PATH = "/QUEUE.REJ";
constexpr uint32_t RECORD_MAGIC = 0x46514A31; // FQJ1
constexpr uint32_t CURSOR_MAGIC = 0x46514331; // FQC1
constexpr uint16_t MAX_FRAME = SAMPLE_FRAME_SIZE;
// FAT32's maximum file size is 4 GiB minus one byte. Leave room for a final
// complete record and keep the bound below the Arduino File 32-bit limit.
constexpr uint32_t MAX_JOURNAL = 0xF0000000UL;

struct RecordHeader {
    uint32_t magic;
    uint16_t length;
    uint16_t reserved;
    uint32_t crc;
};
static_assert(sizeof(RecordHeader) == 12, "journal header layout changed");

struct Cursor {
    uint32_t magic;
    uint32_t offset;
    uint32_t crc;
};
static_assert(sizeof(Cursor) == 12, "journal cursor layout changed");

uint32_t crc32(const uint8_t* data, size_t length)
{
    uint32_t crc = 0xFFFFFFFF;
    while (length--) {
        crc ^= *data++;
        for (uint8_t bit = 0; bit < 8; bit++) {
            crc = (crc >> 1) ^ (0xEDB88320UL & -(crc & 1));
        }
    }
    return ~crc;
}

// Return -1 for an I/O failure, 0 for damaged bytes, and 1 for a valid record.
// A read failure must not be treated as permission to skip a record.
int readRecord(File& file, uint32_t position, uint32_t size,
               RecordHeader* header, char* frame)
{
    if (size - position < sizeof(*header)) return 0;
    if (!file.seek(position) ||
        file.read((uint8_t*)header, sizeof(*header)) != sizeof(*header)) return -1;
    if (header->magic != RECORD_MAGIC || header->reserved != 0 ||
        header->length < 3 || header->length > MAX_FRAME ||
        header->length > size - position - sizeof(*header)) return 0;
    if (file.read((uint8_t*)frame, header->length) != header->length) return -1;
    return frame[header->length - 1] == ',' &&
        crc32((const uint8_t*)frame, header->length) == header->crc ? 1 : 0;
}

bool removeCursors()
{
    return (!SD.exists(CURSOR_A) || SD.remove(CURSOR_A)) &&
           (!SD.exists(CURSOR_B) || SD.remove(CURSOR_B));
}

bool ensureDataFile()
{
    if (SD.exists(DATA_PATH)) return true;
    // A failed stat must never turn into a truncating FILE_WRITE open. Create
    // exclusively through the same SD VFS, preserving an existing journal.
    char path[64];
    snprintf(path, sizeof(path), "/sd%s", DATA_PATH);
    const int descriptor = ::open(path, O_WRONLY | O_CREAT | O_EXCL, 0666);
    if (descriptor < 0) return errno == EEXIST;
    return ::close(descriptor) == 0;
}

bool archiveAcceptedJournal()
{
    // Preserve the complete journal after upload, even when the CSV logger
    // failed. Unknown time or an archive failure leaves the original intact.
    time_t now = time(nullptr);
    if (now < 1704067200 || now > 2145916799) return false;
    if (!SD.exists("/DATA") && !SD.mkdir("/DATA")) return false;
    uint32_t id = (uint32_t)now;
    char archive[32], metadata[32], csv[32];
    for (uint8_t attempt = 0; attempt < 10; attempt++, id++) {
        snprintf(archive, sizeof(archive), "/DATA/%lu.BIN", (unsigned long)id);
        snprintf(metadata, sizeof(metadata), "/DATA/%lu.UTC", (unsigned long)id);
        snprintf(csv, sizeof(csv), "/DATA/%lu.CSV", (unsigned long)id);
        if (SD.exists(archive) || SD.exists(metadata) || SD.exists(csv)) continue;
        // Remove checkpoints before the journal changes identity. A reset here
        // can resend accepted records, but cannot apply an old offset to a
        // new journal and skip unacknowledged records.
        if (!removeCursors() || !SD.rename(DATA_PATH, archive)) return false;
        File date = SD.open(metadata, FILE_WRITE);
        if (date) {
            date.printf("%lu\n", (unsigned long)now);
            date.flush();
            date.close();
        }
        Serial.print("[QUEUE] Accepted journal retained locally for 14 days: ");
        Serial.println(archive);
        return true;
    }
    return false;
}
}

bool DurableQueue::lock()
{
    return lockSD();
}

void DurableQueue::unlock()
{
    // Published under the storage lock. Sampling reads these native-width
    // cached scalars without touching FAT or waiting for an SD operation.
    m_cachedPending = m_size >= m_ack ? m_size - m_ack : 0;
    m_cachedHealthy = m_ready && !m_fault;
    unlockSD();
}

bool DurableQueue::readCursor(const char* path, uint32_t size, uint32_t* value)
{
    File file = SD.open(path, FILE_READ);
    if (!file || file.size() != sizeof(Cursor)) return false;
    Cursor cursor;
    if (file.read((uint8_t*)&cursor, sizeof(cursor)) != sizeof(cursor)) return false;
    if (cursor.magic != CURSOR_MAGIC || cursor.offset > size ||
        cursor.crc != crc32((const uint8_t*)&cursor, sizeof(cursor) - sizeof(cursor.crc))) return false;
    *value = cursor.offset;
    return true;
}

bool DurableQueue::writeCursor(const char* path, uint32_t value)
{
    Cursor cursor = {CURSOR_MAGIC, value, 0};
    cursor.crc = crc32((const uint8_t*)&cursor, sizeof(cursor) - sizeof(cursor.crc));
    File file = SD.open(path, FILE_WRITE);
    if (!file) return false;
    bool written = file.write((const uint8_t*)&cursor, sizeof(cursor)) == sizeof(cursor);
    file.flush();
    file.close();
    uint32_t saved = 0;
    return written && readCursor(path, value, &saved) && saved == value;
}

bool DurableQueue::begin()
{
    if (!lock()) return false;
    // Complete a recovery interrupted after the original was quarantined.
    if (!SD.exists(DATA_PATH) && SD.exists(RECOVERY_PATH) &&
        !SD.rename(RECOVERY_PATH, DATA_PATH)) {
        m_ready = false;
        m_fault = true;
        unlock();
        return false;
    }
    File data = ensureDataFile() ? SD.open(DATA_PATH, FILE_APPEND) : File();
    if (!data) {
        m_ready = false;
        m_fault = true;
        unlock();
        Serial.println("[QUEUE] SD journal unavailable");
        return false;
    }
    uint32_t size = data.size();
    m_size = size;
    data.close();
    uint32_t a = 0, b = 0;
    bool validA = readCursor(CURSOR_A, size, &a);
    bool validB = readCursor(CURSOR_B, size, &b);
    m_ack = validA && validB ? max(a, b) : validA ? a : validB ? b : 0;
    m_read = m_ack;
    m_nextCursorB = validA && (!validB || a >= b);
    m_ready = true;
    m_fault = m_corrupt;
    unlock();
    if (m_corrupt && !recover()) return false;
    Serial.print("[QUEUE] SD journal ready | pending bytes: ");
    Serial.println(m_cachedPending);
    return true;
}

bool DurableQueue::recover()
{
    if (!m_ready || !m_corrupt || !lock()) return false;
    // Keep the damaged source permanently outside the normal log retention
    // directory. Only CRC-verified records enter the replacement journal.
    char archive[48];
    bool available = SD.exists("/RECOVERY") || SD.mkdir("/RECOVERY");
    for (uint16_t attempt = 0; available && attempt < 1000; attempt++) {
        snprintf(archive, sizeof(archive), "/RECOVERY/%lu-%u.BIN",
                 (unsigned long)millis(), attempt);
        if (!SD.exists(archive)) break;
        if (attempt == 999) available = false;
    }
    char* frame = available ? (char*)malloc(MAX_FRAME) : nullptr;
    File source = frame ? SD.open(DATA_PATH, FILE_READ) : File();
    File target = source ? SD.open(RECOVERY_PATH, FILE_WRITE) : File();
    bool okay = source && target;
    const uint32_t size = source ? source.size() : 0;
    uint32_t position = m_ack, recovered = 0, damaged = 0, lastYield = m_ack;
    okay = okay && position <= size;
    while (okay && position < size) {
        RecordHeader header;
        int valid = readRecord(source, position, size, &header, frame);
        if (valid < 0) { okay = false; break; }
        if (valid) {
            okay = target.write((const uint8_t*)&header, sizeof(header)) == sizeof(header) &&
                target.write((const uint8_t*)frame, header.length) == header.length;
            position += sizeof(header) + header.length;
            recovered += sizeof(header) + header.length;
        } else {
            position++;
            damaged++;
        }
        if (position - lastYield >= 4096) { delay(1); lastYield = position; }
    }
    if (target) { target.flush(); target.close(); }
    if (source) source.close();
    // Check the complete replacement before changing the original pathname.
    File verify = okay ? SD.open(RECOVERY_PATH, FILE_READ) : File();
    okay = okay && verify && verify.size() == recovered;
    position = lastYield = 0;
    while (okay && position < recovered) {
        RecordHeader header;
        okay = readRecord(verify, position, recovered, &header, frame) == 1;
        if (okay) position += sizeof(header) + header.length;
        if (position - lastYield >= 4096) { delay(1); lastYield = position; }
    }
    if (verify) verify.close();
    free(frame);
    if (okay) okay = removeCursors() && SD.rename(DATA_PATH, archive);
    if (okay) okay = SD.rename(RECOVERY_PATH, DATA_PATH);
    if (okay) {
        m_ack = m_read = 0; // offsets refer to the verified replacement journal
        m_size = recovered;
        m_nextCursorB = false;
        m_fault = m_corrupt = false;
        Serial.print("[QUEUE] Damaged journal retained at ");
        Serial.println(archive);
        Serial.print("[QUEUE] Intact records recovered; damaged bytes retained: ");
        Serial.println(damaged);
    } else {
        m_fault = true;
        Serial.println("[QUEUE] Recovery incomplete; original bytes retained");
    }
    unlock();
    return okay;
}

void DurableQueue::suspend()
{
    if (lock()) {
        m_ready = false;
        m_fault = true;
        unlock();
    }
}

bool DurableQueue::append(const char* frame, uint16_t length)
{
    if (!m_ready || m_fault || m_corrupt || !frame || length < 3 || length > MAX_FRAME || frame[length - 1] != ',' || !lock()) return false;
    File file = ensureDataFile() ? SD.open(DATA_PATH, FILE_APPEND) : File();
    if (!file) {
        Serial.print("[QUEUE] Append open errno: ");
        Serial.println(errno);
    }
    bool okay = false;
    bool partial = false;
    uint32_t recordStart = 0;
    if (file && file.size() + sizeof(RecordHeader) + length <= MAX_JOURNAL) {
        recordStart = file.size();
        RecordHeader header = {RECORD_MAGIC, length, 0, crc32((const uint8_t*)frame, length)};
        const size_t headerBytes = file.write((const uint8_t*)&header, sizeof(header));
        const size_t frameBytes = headerBytes == sizeof(header)
            ? file.write((const uint8_t*)frame, length) : 0;
        okay = headerBytes == sizeof(header) && frameBytes == length;
        partial = !okay && (headerBytes != 0 || frameBytes != 0);
        file.flush();
    }
    if (file) file.close();
    if (okay) {
        // Verify the persisted bytes before releasing the only RAM copy.
        File verify = SD.open(DATA_PATH, FILE_READ);
        RecordHeader header;
        okay = verify && verify.size() == recordStart + sizeof(header) + length &&
            verify.seek(recordStart) &&
            verify.read((uint8_t*)&header, sizeof(header)) == sizeof(header) &&
            header.magic == RECORD_MAGIC && header.length == length &&
            header.crc == crc32((const uint8_t*)frame, length);
        char chunk[64];
        for (uint16_t offset = 0; okay && offset < length; offset += sizeof(chunk)) {
            const uint16_t count = min((uint16_t)sizeof(chunk), (uint16_t)(length - offset));
            okay = verify.read((uint8_t*)chunk, count) == count &&
                memcmp(chunk, frame + offset, count) == 0;
        }
        if (verify) verify.close();
        if (!okay) partial = true;
    }
    if (!okay && !m_fault) Serial.println("[QUEUE] SD append failed or journal full; retaining RAM reading");
    if (!okay) m_fault = true;
    if (partial) m_corrupt = true;
    if (okay) {
        m_fault = false;
        m_size = recordStart + sizeof(RecordHeader) + length;
    }
    unlock();
    return okay;
}

bool DurableQueue::peek(char* frame, uint16_t capacity, uint16_t* length)
{
    if (!m_ready || m_fault || !frame || !length || !lock()) return false;
    File file = SD.open(DATA_PATH, FILE_READ);
    if (!file) {
        m_fault = true;
        unlock();
        return false;
    }
    const uint32_t size = file.size();
    bool found = false;
    if (m_read < size) {
        const uint32_t candidate = m_read;
        RecordHeader header;
        if (candidate + sizeof(header) <= size && file.seek(candidate) &&
            file.read((uint8_t*)&header, sizeof(header)) == sizeof(header) &&
            header.magic == RECORD_MAGIC && header.length >= 3 &&
            header.length <= MAX_FRAME && header.length <= capacity &&
            candidate + sizeof(header) + header.length <= size &&
            file.read((uint8_t*)frame, header.length) == header.length &&
            crc32((const uint8_t*)frame, header.length) == header.crc &&
            frame[header.length - 1] == ',') {
            m_read = candidate + sizeof(header) + header.length;
            *length = header.length;
            found = true;
        } else {
            // A corrupt or partial record must never be searched past: doing
            // so could acknowledge later bytes while silently losing this one.
            if (!m_fault) Serial.println("[QUEUE] Journal record invalid; replay stopped without skipping bytes");
            m_fault = true;
            m_corrupt = true;
        }
    }
    file.close();
    unlock();
    return found;
}

bool DurableQueue::acknowledge()
{
    if (!m_ready || !lock()) return false;
    const char* path = m_nextCursorB ? CURSOR_B : CURSOR_A;
    bool saved = writeCursor(path, m_read);
    if (saved) {
        m_ack = m_read;
        m_nextCursorB = !m_nextCursorB;
        File file = SD.open(DATA_PATH, FILE_READ);
        uint32_t size = file ? file.size() : 0;
        if (file) file.close();
        if (size && size == m_ack && archiveAcceptedJournal()) {
            // Only rotate when every record is accepted and its local archive
            // exists. A failed rename never deletes the original journal.
            if (SD.exists(CURSOR_A)) SD.remove(CURSOR_A);
            if (SD.exists(CURSOR_B)) SD.remove(CURSOR_B);
            m_ack = m_read = 0;
            m_size = 0;
            m_nextCursorB = false;
            if (!ensureDataFile()) m_fault = true;
            else if (!m_corrupt) m_fault = false;
        }
    } else {
        m_fault = true;
        m_read = m_ack;
        Serial.println("[QUEUE] Acknowledgement checkpoint failed; batch will replay");
    }
    unlock();
    return saved;
}

bool DurableQueue::quarantine(const char* frame, uint16_t length)
{
    if (!m_ready || !frame || length < 3 || length > MAX_FRAME || !lock()) return false;
    // Same record layout as the journal, so the reject file can be replayed or
    // inspected with the normal tools. The bytes never leave the card.
    RecordHeader header = {RECORD_MAGIC, length, 0, crc32((const uint8_t*)frame, length)};
    File file = SD.open(REJECTED_PATH, FILE_APPEND);
    bool okay = file &&
        file.write((const uint8_t*)&header, sizeof(header)) == sizeof(header) &&
        file.write((const uint8_t*)frame, length) == length;
    if (file) { file.flush(); file.close(); }
    unlock();
    if (!okay) {
        Serial.println("[QUEUE] Reject file write failed; record stays in the journal");
        return false;
    }
    if (!acknowledge()) return false;
    m_rejected++;
    Serial.print("[QUEUE] Collector refused one record; kept in ");
    Serial.println(REJECTED_PATH);
    return true;
}

void DurableQueue::rewind(uint32_t position)
{
    if (lock()) {
        if (position >= m_ack && position <= m_read) m_read = position;
        unlock();
    }
}

void DurableQueue::retry()
{
    if (lock()) {
        m_read = m_ack;
        unlock();
    }
}

uint32_t DurableQueue::pendingBytes()
{
    if (!lock()) return m_size >= m_ack ? m_size - m_ack : 0;
    if (!m_ready || m_fault) {
        uint32_t bytes = m_size >= m_ack ? m_size - m_ack : 0;
        unlock();
        return bytes;
    }
    File file = SD.exists(DATA_PATH) ? SD.open(DATA_PATH, FILE_READ) : File();
    if (file) m_size = file.size();
    else m_fault = true;
    uint32_t bytes = m_size >= m_ack ? m_size - m_ack : 0;
    if (file) file.close();
    unlock();
    return bytes;
}
#endif
