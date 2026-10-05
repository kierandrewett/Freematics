#ifndef FREEMATICS_CAPTURE_INBOX_H
#define FREEMATICS_CAPTURE_INBOX_H

#include <stddef.h>
#include <stdint.h>

typedef enum CaptureInboxResult {
    CAPTURE_INBOX_STORED,
    CAPTURE_INBOX_DUPLICATE,
    CAPTURE_INBOX_CONFLICT,
    CAPTURE_INBOX_INVALID,
    CAPTURE_INBOX_ERROR
} CaptureInboxResult;

/* Store an immutable capture durably below root/capture-inbox/device_id. */
CaptureInboxResult captureInboxStore(const char* root, const char* device_id,
                                     uint64_t session, uint32_t sequence,
                                     const void* payload, size_t length);

#endif
