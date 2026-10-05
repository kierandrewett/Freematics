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

typedef struct CaptureInboxReceipt {
    uint64_t epoch_ms;
    int clock_plausible;
} CaptureInboxReceipt;

/* Store an immutable capture durably below root/capture-inbox/device_id. */
CaptureInboxResult captureInboxStore(const char* root, const char* device_id,
                                     uint64_t session, uint32_t sequence,
                                     const void* payload, size_t length);

/* Atomically create or validate the immutable sibling .receipt sidecar.
 * now_seconds is sampled by the caller only after captureInboxStore succeeds.
 * A duplicate returns the first durable receipt in receipt_out. */
CaptureInboxResult captureInboxReceiptEnsure(const char* root, const char* device_id,
                                             uint64_t session, uint32_t sequence,
                                             int64_t now_seconds,
                                             CaptureInboxReceipt* receipt_out);

#endif
