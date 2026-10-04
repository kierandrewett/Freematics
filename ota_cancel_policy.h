#ifndef FREEMATICS_OTA_CANCEL_POLICY_H
#define FREEMATICS_OTA_CANCEL_POLICY_H

#include <stdint.h>

namespace freematics {
namespace ota {

// Cancellation must return ownership of the shared modem before standby can
// resume normal vehicle monitoring. Millis subtraction is rollover-safe.
static const uint32_t kCancellationCompletionTimeoutMs = 30000UL;

inline bool cancellationWaitExpired(uint32_t startedAtMs, uint32_t nowMs) {
  return static_cast<uint32_t>(nowMs - startedAtMs) >=
      kCancellationCompletionTimeoutMs;
}

} // namespace ota
} // namespace freematics

#endif
