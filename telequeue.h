#ifndef TELEQUEUE_H_INCLUDED
#define TELEQUEUE_H_INCLUDED

#include "config.h"

#if STORAGE == STORAGE_SD
#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

// Append-only SD journal. HTTP acknowledgements advance a separately
// checksummed cursor; a reset can resend records but cannot skip unacked ones.
class DurableQueue {
public:
    bool begin();
    void suspend();
    bool append(const char* frame, uint16_t length);
    bool peek(char* frame, uint16_t capacity, uint16_t* length);
    bool acknowledge();
    void rewind(uint32_t position);
    void retry();
    uint32_t readPosition() const { return m_read; }
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
    volatile uint32_t m_cachedPending = 0;
    volatile bool m_cachedHealthy = false;
    uint32_t m_ack = 0;
    uint32_t m_read = 0;
    uint32_t m_size = 0;
    bool m_ready = false;
    bool m_fault = false;
    bool m_corrupt = false;
    bool m_nextCursorB = false;
};
#endif

#endif
