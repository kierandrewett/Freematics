#ifndef FREEMATICS_OTA_CANCEL_POLICY_H
#define FREEMATICS_OTA_CANCEL_POLICY_H

#include <stdint.h>

namespace freematics {
namespace ota {

// Cancellation must return ownership of the shared modem before standby can
// resume normal vehicle monitoring. Millis subtraction is rollover-safe.
static const uint32_t kCancellationCompletionTimeoutMs = 30000UL;

typedef bool (*TransferContinueCheck)(void* context);

// Compose the standby owner's periodic safety check with the atomic cancel
// flag. A set cancel flag short-circuits before touching shared vehicle state.
inline bool shouldContinueTransfer(const volatile bool* cancelRequested,
                                  TransferContinueCheck check,
                                  void* context) {
  if (cancelRequested && *cancelRequested) return false;
  return !check || check(context);
}

inline bool cancellationWaitExpired(uint32_t startedAtMs, uint32_t nowMs) {
  return static_cast<uint32_t>(nowMs - startedAtMs) >=
      kCancellationCompletionTimeoutMs;
}

} // namespace ota
} // namespace freematics

#endif
