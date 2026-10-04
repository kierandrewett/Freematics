#ifndef TELEQUEUE_H_INCLUDED
#define TELEQUEUE_H_INCLUDED

#include "config.h"
#include "ota_first_upload_policy.h"

#if STORAGE == STORAGE_SD
#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// Append-only SD journal. HTTP acknowledgements advance a separately
// checksummed cursor; a reset can resend records but cannot skip unacked ones.
class DurableQueue {
public:
    DurableQueue() = default;
    ~DurableQueue();
    DurableQueue(const DurableQueue&) = delete;
    DurableQueue& operator=(const DurableQueue&) = delete;

    bool begin();
    void suspend();
    bool recover();
    bool damaged() const { return m_corrupt; }
    bool append(const char* frame, uint16_t length, bool* lockTimedOut = nullptr);
    // One open, flush and read-back verify for the whole batch. The recorder
    // falls behind 4 Hz when every sample pays for its own SD transaction.
    bool appendBatch(const char* const* frames, const uint16_t* lengths, uint8_t count,
                     bool* lockTimedOut = nullptr);
    bool peek(char* frame, uint16_t capacity, uint16_t* length);
    bool acknowledge();
    // Keep a record the collector refused permanently in a local reject file,
    // then acknowledge past it. Valid only when the batch held that one record.
    bool quarantine(const char* frame, uint16_t length);
    uint32_t rejectedCount() const { return m_rejected; }
    void rewind(uint32_t position);
    void retry();
    uint32_t readPosition() const { return m_read; }
    // Physical journal position separating records present at boot from
    // records durably appended during this boot.
    bool isCurrentBootPosition(uint32_t position) const {
        return otaRecordWasJournaledThisBoot(position, m_bootStart);
    }
    uint32_t pendingBytes();
    uint32_t cachedPendingBytes() const { return m_cachedPending; }
    bool cachedHealthy() const { return m_cachedHealthy; }
    bool ready() const { return m_ready; }
    bool healthy() const { return m_ready && !m_fault; }
private:
    bool lock();
    void unlock();
    bool readCursor(const char* path, uint32_t size, uint32_t* value);
    bool writeCursor(const char* path, uint32_t value);
    bool fillReadCache();
    void dropReadCache() { m_cacheStart = m_cacheEnd = 0; }
    // Read-ahead window over the append-only journal, so a replay batch costs
    // one SD read instead of one open and seek per frame.
    char* m_cache = nullptr;
    uint32_t m_cacheStart = 0;
    uint32_t m_cacheEnd = 0;
    volatile uint32_t m_cachedPending = 0;
    volatile uint32_t m_rejected = 0;
    volatile bool m_cachedHealthy = false;
    uint32_t m_ack = 0;
    uint32_t m_read = 0;
    uint32_t m_size = 0;
    uint32_t m_bootStart = 0;
    bool m_ready = false;
    bool m_fault = false;
    volatile bool m_corrupt = false;
    bool m_nextCursorB = false;
};
#endif

#endif
