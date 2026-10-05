#ifndef FREEMATICS_RECORDING_CHECKPOINT_POLICY_H
#define FREEMATICS_RECORDING_CHECKPOINT_POLICY_H

#include <stdint.h>

namespace freematics {
namespace recording {

class WrapUpCheckpointPolicy {
public:
  WrapUpCheckpointPolicy() : pending_(false) {}

  void begin() { pending_ = true; }
  bool pending() const { return pending_; }

  bool keepWrapUpOpen(uint32_t now, uint32_t wrapUpSince,
                      uint32_t maximumWaitMs) const {
    return pending_ && (uint32_t)(now - wrapUpSince) < maximumWaitMs;
  }

  // A checkpoint is complete only after a durable journal commit has been
  // observed. Failed writes and queue contention leave it pending for retry.
  void observeJournalCommit(uint32_t before, uint32_t after) {
    if (pending_ && before != after) pending_ = false;
  }

private:
  bool pending_;
};

} // namespace recording
} // namespace freematics

#endif
