#pragma once

#include <stdint.h>

// Bounded retry schedule for restoring the SD journal after a transient
// mount/write failure. Recovery starts promptly, then backs off if the card
// remains unavailable so failed media does not cause a hot reinitialization
// loop while the recorder continues to count unjournaled samples.
class SDRecoveryBackoff {
 public:
  static const uint32_t kInitialDelayMs = 1000UL;
  static const uint32_t kMaximumDelayMs = 30000UL;

  SDRecoveryBackoff() : m_nextAttemptMs(0), m_delayMs(kInitialDelayMs),
                        m_scheduled(false) {}

  bool due(uint32_t nowMs) const {
    return !m_scheduled || static_cast<int32_t>(nowMs - m_nextAttemptMs) >= 0;
  }

  uint32_t delayMs() const { return m_delayMs; }

  void recordAttempt(uint32_t nowMs, bool recovered) {
    if (recovered) {
      reset();
      return;
    }
    m_scheduled = true;
    m_nextAttemptMs = nowMs + m_delayMs;
    m_delayMs = m_delayMs >= kMaximumDelayMs / 2
        ? kMaximumDelayMs : m_delayMs * 2;
  }

  void reset() {
    m_scheduled = false;
    m_nextAttemptMs = 0;
    m_delayMs = kInitialDelayMs;
  }

 private:
  uint32_t m_nextAttemptMs;
  uint32_t m_delayMs;
  bool m_scheduled;
};
