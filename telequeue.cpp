#include "telequeue.h"

#if STORAGE == STORAGE_SD
#include <SD.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
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
constexpr uint32_t RECORD_MAGIC_IDENTIFIED = 0x46514A32; // FQJ2
constexpr uint32_t CURSOR_MAGIC = 0x46514331; // FQC1
constexpr uint16_t MAX_FRAME = SAMPLE_FRAME_SIZE;
// FAT32's maximum file size is 4 GiB minus one byte. Leave room for a final
// complete record and keep the bound below the Arduino File 32-bit limit.
constexpr uint32_t MAX_JOURNAL = 0xF0000000UL;
constexpr uint32_t READ_CACHE_SIZE = 32768;
uint32_t storageProbeSequence = 0;
constexpr uint8_t STORAGE_PROBE_NAME_ATTEMPTS = 32;
const uint8_t STORAGE_PROBE_PATTERN[] = {
    'F', 'T', 'S', 'D', 'H', 'E', 'A', 'L', 'T', 'H', 0xA5, 0x5A,
    0x31, 0xC7, 0x04, 0xE2, 0x9B, 0x60, 0xD3, 0x18, 0x77, 0x2A,
    0xF0, 0x4D, 0x83, 0x11, 0xCE, 0x62, 0x39, 0xB4, 0x05, 0xFA
};

struct RecordHeader {
    uint32_t magic;
    uint16_t length;
    uint16_t reserved;
    uint32_t crc;
};
static_assert(sizeof(RecordHeader) == 12, "journal header layout changed");

struct RecordIdentityDisk {
    uint32_t sessionLow;
    uint32_t sessionHigh;
    uint32_t sequence;
};
static_assert(sizeof(RecordIdentityDisk) == 12, "journal identity layout changed");

uint16_t recordHeaderSize(uint32_t magic)
{
    return magic == RECORD_MAGIC_IDENTIFIED
        ? (uint16_t)(sizeof(RecordHeader) + sizeof(RecordIdentityDisk))
        : (uint16_t)sizeof(RecordHeader);
}

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

uint32_t crc32Identified(const RecordIdentityDisk& identity, const uint8_t* frame, size_t length)
{
    uint32_t crc = 0xFFFFFFFF;
    const uint8_t* metadata = (const uint8_t*)&identity;
    for (size_t index = 0; index < sizeof(identity) + length; ++index) {
        const uint8_t value = index < sizeof(identity) ? metadata[index] : frame[index - sizeof(identity)];
        crc ^= value;
        for (uint8_t bit = 0; bit < 8; bit++) crc = (crc >> 1) ^ (0xEDB88320UL & -(crc & 1));
    }
    return ~crc;
}

// Return -1 for an I/O failure, 0 for damaged bytes, and 1 for a valid record.
// A read failure must not be treated as permission to skip a record.
int readRecord(File& file, uint32_t position, uint32_t size,
               RecordHeader* header, RecordIdentityDisk* identity, char* frame)
{
    if (size - position < sizeof(*header)) return 0;
    if (!file.seek(position) ||
        file.read((uint8_t*)header, sizeof(*header)) != sizeof(*header)) return -1;
    if ((header->magic != RECORD_MAGIC && header->magic != RECORD_MAGIC_IDENTIFIED) || header->reserved != 0 ||
        header->length < 3 || header->length > MAX_FRAME ||
        recordHeaderSize(header->magic) > size - position ||
        header->length > size - position - recordHeaderSize(header->magic)) return 0;
    if (header->magic == RECORD_MAGIC_IDENTIFIED) {
        if (!identity) return 0;
        if (file.read((uint8_t*)identity, sizeof(*identity)) != sizeof(*identity)) return -1;
    } else if (identity) {
        identity->sessionLow = 0;
        identity->sessionHigh = 0;
        identity->sequence = 0;
    }
    if (file.read((uint8_t*)frame, header->length) != header->length) return -1;
    const uint32_t actualCrc = header->magic == RECORD_MAGIC_IDENTIFIED
        ? crc32Identified(*identity, (const uint8_t*)frame, header->length)
        : crc32((const uint8_t*)frame, header->length);
    return frame[header->length - 1] == ',' && actualCrc == header->crc ? 1 : 0;
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
    m_cachedHealthy = m_ready && !m_fault && !m_probeFault;
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
        Serial.print("[SD-DIAG] journal recovery promote failed errno=");
        Serial.println(errno);
        return false;
    }
    const bool dataFileReady = ensureDataFile();
    File data = dataFileReady ? SD.open(DATA_PATH, FILE_APPEND) : File();
    if (!data) {
        m_ready = false;
        m_fault = true;
        unlock();
        Serial.print("[SD-DIAG] journal ");
        Serial.print(dataFileReady ? "open" : "create");
        Serial.print(" failed errno=");
        Serial.println(errno);
        Serial.println("[QUEUE] SD journal unavailable");
        return false;
    }
    uint32_t size = data.size();
    m_size = size;
    m_bootStart = size;
    data.close();
    dropReadCache();
    uint32_t a = 0, b = 0;
    bool validA = readCursor(CURSOR_A, size, &a);
    bool validB = readCursor(CURSOR_B, size, &b);
    m_ack = validA && validB ? max(a, b) : validA ? a : validB ? b : 0;
    m_read = m_ack;
    m_nextCursorB = validA && (!validB || a >= b);
    m_ready = true;
    m_fault = m_corrupt;
    m_probeFault = false;
    unlock();
    if (m_corrupt && !recover()) return false;
    Serial.print("[QUEUE] SD journal ready | pending bytes: ");
    Serial.println(m_cachedPending);
    return true;
}

bool DurableQueue::probeStorage()
{
    if (!lock()) return false;
    // A previous probe failure is retryable; journal corruption/fault is not.
    bool okay = m_ready && !m_fault;
    int descriptor = -1;
    char path[40] = {};
    bool created = false;

    for (uint8_t attempt = 0; okay && attempt < STORAGE_PROBE_NAME_ATTEMPTS; ++attempt) {
        const uint32_t sequence = storageProbeSequence++;
        snprintf(path, sizeof(path), "/sd/OTA%08lx%02x.TMP",
                 (unsigned long)millis(), (unsigned)(sequence & 0xFF));
        descriptor = ::open(path, O_RDWR | O_CREAT | O_EXCL, 0600);
        if (descriptor >= 0) {
            created = true;
            break;
        }
        if (errno != EEXIST) {
            okay = false;
            break;
        }
    }
    if (descriptor < 0) okay = false;

    if (okay && ::write(descriptor, STORAGE_PROBE_PATTERN,
                        sizeof(STORAGE_PROBE_PATTERN)) != sizeof(STORAGE_PROBE_PATTERN)) {
        okay = false;
    }
    if (okay && ::fsync(descriptor) != 0) okay = false;
    if (descriptor >= 0) {
        if (::close(descriptor) != 0) okay = false;
        descriptor = -1;
    }

    // Reopen by name after the durable flush so this exercises a fresh file
    // open/read path instead of merely reading the writer's descriptor.
    int readDescriptor = -1;
    if (okay) {
        readDescriptor = ::open(path, O_RDONLY);
        if (readDescriptor < 0) okay = false;
    }
    uint8_t readback[sizeof(STORAGE_PROBE_PATTERN)] = {};
    if (okay && (::pread(readDescriptor, readback, sizeof(readback), 0) != sizeof(readback) ||
                 memcmp(readback, STORAGE_PROBE_PATTERN, sizeof(readback)))) {
        okay = false;
    }
    if (readDescriptor >= 0 && ::close(readDescriptor) != 0) okay = false;
    if (created && ::unlink(path) != 0) okay = false;

    // A successful test must leave no sidecar behind, and any I/O or cleanup
    // failure invalidates the cached journal health until a normal recovery.
    m_probeFault = !okay;
    unlock();
    return okay;
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
        RecordIdentityDisk identity = {};
        int valid = readRecord(source, position, size, &header, &identity, frame);
        if (valid < 0) { okay = false; break; }
        if (valid) {
            const uint16_t headerBytes = recordHeaderSize(header.magic);
            okay = target.write((const uint8_t*)&header, sizeof(header)) == sizeof(header) &&
                (header.magic != RECORD_MAGIC_IDENTIFIED ||
                 target.write((const uint8_t*)&identity, sizeof(identity)) == sizeof(identity)) &&
                target.write((const uint8_t*)frame, header.length) == header.length;
            position += headerBytes + header.length;
            recovered += headerBytes + header.length;
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
        RecordIdentityDisk identity = {};
        okay = readRecord(verify, position, recovered, &header, &identity, frame) == 1;
        if (okay) position += recordHeaderSize(header.magic) + header.length;
        if (position - lastYield >= 4096) { delay(1); lastYield = position; }
    }
    if (verify) verify.close();
    free(frame);
    if (okay) okay = removeCursors() && SD.rename(DATA_PATH, archive);
    if (okay) okay = SD.rename(RECOVERY_PATH, DATA_PATH);
    if (okay) {
        m_ack = m_read = 0; // offsets refer to the verified replacement journal
        m_size = recovered;
        m_bootStart = recovered; // recovered bytes predate the current recording session
        dropReadCache();
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

bool DurableQueue::append(const char* frame, uint16_t length, bool* lockTimedOut)
{
    return appendBatch(&frame, &length, 1, lockTimedOut);
}

bool DurableQueue::appendIdentified(const char* frame, uint16_t length, uint64_t session,
                                   uint32_t sequence, bool* lockTimedOut)
{
    const JournalCaptureIdentity identity = {session, sequence};
    return appendRecords(&frame, &length, 1, &identity, lockTimedOut);
}

bool DurableQueue::appendBatch(const char* const* frames, const uint16_t* lengths, uint8_t count,
                               bool* lockTimedOut)
{
    return appendRecords(frames, lengths, count, nullptr, lockTimedOut);
}

bool DurableQueue::appendRecords(const char* const* frames, const uint16_t* lengths, uint8_t count,
                                 const JournalCaptureIdentity* identities, bool* lockTimedOut)
{
    if (lockTimedOut) *lockTimedOut = false;
    if (!m_ready || m_fault || m_corrupt || !frames || !lengths || !count) return false;
    uint32_t total = 0;
    for (uint8_t i = 0; i < count; i++) {
        const uint16_t length = lengths[i];
        if (!frames[i] || length < 3 || length > MAX_FRAME || frames[i][length - 1] != ',') return false;
        total += sizeof(RecordHeader) + (identities ? sizeof(RecordIdentityDisk) : 0) + length;
    }
    if (!lock()) {
        if (lockTimedOut) *lockTimedOut = true;
        return false;
    }
    File file = ensureDataFile() ? SD.open(DATA_PATH, FILE_APPEND) : File();
    if (!file) {
        Serial.print("[QUEUE] Append open errno: ");
        Serial.println(errno);
    }
    bool okay = false;
    bool partial = false;
    const char* failureStage = nullptr;
    uint32_t recordStart = 0;
    uint32_t observedSize = 0;
    if (!file) {
        failureStage = "append-open";
    } else {
        recordStart = observedSize = file.size();
        if ((uint64_t)recordStart + total > MAX_JOURNAL) {
            failureStage = "journal-capacity";
        } else {
            okay = true;
            size_t written = 0;
            for (uint8_t i = 0; okay && i < count; i++) {
                const uint64_t session = identities ? identities[i].session : 0;
                const RecordIdentityDisk identity = {(uint32_t)session, (uint32_t)(session >> 32),
                    identities ? identities[i].sequence : 0};
                RecordHeader header = {identities ? RECORD_MAGIC_IDENTIFIED : RECORD_MAGIC,
                    lengths[i], 0, identities
                        ? crc32Identified(identity, (const uint8_t*)frames[i], lengths[i])
                        : crc32((const uint8_t*)frames[i], lengths[i])};
                const size_t headerBytes = file.write((const uint8_t*)&header, sizeof(header));
                size_t identityBytes = 0;
                if (headerBytes == sizeof(header) && identities)
                    identityBytes = file.write((const uint8_t*)&identity, sizeof(identity));
                const size_t frameBytes = headerBytes == sizeof(header) &&
                    (!identities || identityBytes == sizeof(RecordIdentityDisk))
                    ? file.write((const uint8_t*)frames[i], lengths[i]) : 0;
                written += headerBytes + identityBytes + frameBytes;
                okay = headerBytes == sizeof(header) &&
                    (!identities || identityBytes == sizeof(RecordIdentityDisk)) && frameBytes == lengths[i];
                if (!okay) failureStage = headerBytes != sizeof(header) ? "append-header-write" :
                    (identities && identityBytes != sizeof(RecordIdentityDisk) ? "append-identity-write" : "append-frame-write");
            }
            partial = !okay && written != 0;
            file.flush();
        }
    }
    if (file) file.close();
    if (okay) {
        // Verify the persisted bytes before releasing the only RAM copies.
        File verify = SD.open(DATA_PATH, FILE_READ);
        if (!verify) {
            okay = false;
            failureStage = "readback-open";
        } else if (verify.size() != recordStart + total || !verify.seek(recordStart)) {
            okay = false;
            failureStage = "readback-position";
        }
        char chunk[256];
        for (uint8_t i = 0; okay && i < count; i++) {
            RecordHeader header;
            RecordIdentityDisk identity = {};
            const size_t headerBytes = verify.read((uint8_t*)&header, sizeof(header));
            okay = headerBytes == sizeof(header) &&
                header.magic == (identities ? RECORD_MAGIC_IDENTIFIED : RECORD_MAGIC) &&
                header.length == lengths[i];
            if (!okay) failureStage = headerBytes != sizeof(header) ? "readback-header" : "readback-header-mismatch";
            if (okay && identities) {
                const uint64_t expectedSession = identities[i].session;
                okay = verify.read((uint8_t*)&identity, sizeof(identity)) == sizeof(identity) &&
                    (((uint64_t)identity.sessionHigh << 32) | identity.sessionLow) == expectedSession &&
                    identity.sequence == identities[i].sequence;
                if (!okay) failureStage = "readback-identity";
            }
            if (okay) {
                const uint32_t expectedCrc = identities
                    ? crc32Identified(identity, (const uint8_t*)frames[i], lengths[i])
                    : crc32((const uint8_t*)frames[i], lengths[i]);
                okay = header.crc == expectedCrc;
                if (!okay) failureStage = "readback-crc";
            }
            for (uint16_t offset = 0; okay && offset < lengths[i]; offset += sizeof(chunk)) {
                const uint16_t part = min((uint16_t)sizeof(chunk), (uint16_t)(lengths[i] - offset));
                const size_t readBytes = verify.read((uint8_t*)chunk, part);
                okay = readBytes == part;
                if (!okay) failureStage = "readback-short-frame";
                else if (memcmp(chunk, frames[i] + offset, part) != 0) {
                    okay = false;
                    failureStage = "readback-frame-mismatch";
                }
            }
        }
        if (verify) verify.close();
        if (!okay) partial = true;
    }
    if (!okay) {
        Serial.print("[SD-DIAG] append failed stage=");
        Serial.print(failureStage ? failureStage : "unknown");
        Serial.print(" size=");
        Serial.print(observedSize);
        Serial.print(" add_bytes=");
        Serial.print(total);
        Serial.print(" errno=");
        Serial.println(errno);
    }
    if (!okay && !m_fault) Serial.println("[QUEUE] SD append failed or journal full; batch is unjournaled and will be discarded");
    if (!okay) m_fault = true;
    if (partial) m_corrupt = true;
    if (okay) {
        m_fault = false;
        m_size = recordStart + total;
    }
    unlock();
    return okay;
}

DurableQueue::~DurableQueue()
{
    free(m_cache);
}

bool DurableQueue::fillReadCache()
{
    if (!m_cache) m_cache = (char*)malloc(READ_CACHE_SIZE);
    dropReadCache();
    File file = m_cache ? SD.open(DATA_PATH, FILE_READ) : File();
    if (!file) return false;
    const uint32_t size = file.size();
    const uint32_t wanted = m_read < size ? min((uint32_t)READ_CACHE_SIZE, size - m_read) : 0;
    const bool okay = wanted && file.seek(m_read) &&
        (uint32_t)file.read((uint8_t*)m_cache, wanted) == wanted;
    file.close();
    if (!okay) return false;
    m_cacheStart = m_read;
    m_cacheEnd = m_read + wanted;
    return true;
}

bool DurableQueue::peek(char* frame, uint16_t capacity, uint16_t* length)
{
    return peekIdentified(frame, capacity, length, nullptr);
}

bool DurableQueue::peekIdentified(char* frame, uint16_t capacity, uint16_t* length,
                                  JournalCaptureIdentity* identity)
{
    if (!m_ready || m_fault || !frame || !length || !lock()) return false;
    const uint32_t candidate = m_read;
    // m_size is the journal length after the last verified append or
    // recovery. Nothing else writes the file, so no stat is needed here.
    if (candidate >= m_size) {
        unlock();
        return false;
    }
    RecordHeader header;
    bool cached = candidate >= m_cacheStart && candidate + sizeof(header) <= m_cacheEnd;
    if (cached) {
        memcpy(&header, m_cache + (candidate - m_cacheStart), sizeof(header));
        cached = (header.magic == RECORD_MAGIC || header.magic == RECORD_MAGIC_IDENTIFIED) &&
            header.length <= MAX_FRAME && candidate + recordHeaderSize(header.magic) + header.length <= m_cacheEnd;
    }
    if (!cached) {
        if (!fillReadCache()) {
            // An I/O failure must not look like a damaged record.
            m_fault = true;
            unlock();
            return false;
        }
    }
    bool found = false;
    const uint32_t available = m_cacheEnd - candidate;
    if (available >= sizeof(header)) {
        memcpy(&header, m_cache + (candidate - m_cacheStart), sizeof(header));
        const uint16_t headerBytes = recordHeaderSize(header.magic);
        const char* metadata = m_cache + (candidate - m_cacheStart) + sizeof(header);
        const char* body = m_cache + (candidate - m_cacheStart) + headerBytes;
        RecordIdentityDisk storedIdentity = {};
        if (header.magic == RECORD_MAGIC_IDENTIFIED &&
            sizeof(header) + sizeof(storedIdentity) <= available) memcpy(&storedIdentity, metadata, sizeof(storedIdentity));
        if ((header.magic == RECORD_MAGIC || header.magic == RECORD_MAGIC_IDENTIFIED) &&
            header.reserved == 0 && header.length >= 3 &&
            header.length <= MAX_FRAME && header.length <= capacity &&
            headerBytes + header.length <= available &&
            (header.magic == RECORD_MAGIC_IDENTIFIED
                ? crc32Identified(storedIdentity, (const uint8_t*)body, header.length)
                : crc32((const uint8_t*)body, header.length)) == header.crc &&
            body[header.length - 1] == ',') {
            if (identity) {
                identity->session = 0;
                identity->sequence = 0;
                if (header.magic == RECORD_MAGIC_IDENTIFIED) {
                    identity->session = ((uint64_t)storedIdentity.sessionHigh << 32) | storedIdentity.sessionLow;
                    identity->sequence = storedIdentity.sequence;
                }
            }
            memcpy(frame, body, header.length);
            m_read = candidate + headerBytes + header.length;
            *length = header.length;
            found = true;
        }
    }
    if (!found) {
        // A corrupt or partial record must never be searched past: doing
        // so could acknowledge later bytes while silently losing this one.
        if (!m_fault) Serial.println("[QUEUE] Journal record invalid; replay stopped without skipping bytes");
        m_fault = true;
        m_corrupt = true;
    }
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
        // The queue is the journal's only writer, so m_size is exact and the
        // file need not be opened on every acknowledgement.
        if (m_size && m_size == m_ack && archiveAcceptedJournal()) {
            // Only rotate when every record is accepted and its local archive
            // exists. A failed rename never deletes the original journal.
            if (SD.exists(CURSOR_A)) SD.remove(CURSOR_A);
            if (SD.exists(CURSOR_B)) SD.remove(CURSOR_B);
            m_ack = m_read = 0;
            m_size = 0;
            m_bootStart = 0;
            m_nextCursorB = false;
            dropReadCache();
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
    // The queue is the only writer of its journal, so m_size is exact. The
    // upload loop calls this often; it must not touch the card each time.
    if (!lock()) return m_size >= m_ack ? m_size - m_ack : 0;
    uint32_t bytes = m_size >= m_ack ? m_size - m_ack : 0;
    unlock();
    return bytes;
}
#endif
