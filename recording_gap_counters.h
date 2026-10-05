#ifndef FREEMATICS_RECORDING_GAP_COUNTERS_H
#define FREEMATICS_RECORDING_GAP_COUNTERS_H

#include <stdint.h>

// Bounded diagnostic counters only; completed readings remain SD-only.
struct RecordingGapCounters {
  enum Cause : uint8_t {
    kBufferExhaustion = 0,
    kSdUnavailable = 1,
    kJournalCommitFailure = 2,
    kDeadlineOverrun = 3,
    kCauseCount = 4
  };

  void add(Cause cause, uint32_t count = 1) {
    if (cause >= kCauseCount) return;
    uint32_t& value = values[cause];
    value = UINT32_MAX - value < count ? UINT32_MAX : value + count;
  }

  uint32_t get(Cause cause) const {
    return cause < kCauseCount ? values[cause] : 0;
  }

  uint32_t values[kCauseCount] = {};
};

#endif
