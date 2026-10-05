#include "../recording_gap_counters.h"

#include <assert.h>
#include <stdint.h>

int main() {
  RecordingGapCounters counters;
  for (uint8_t cause = 0; cause < RecordingGapCounters::kCauseCount; ++cause) {
    assert(counters.get(static_cast<RecordingGapCounters::Cause>(cause)) == 0);
    counters.add(static_cast<RecordingGapCounters::Cause>(cause), cause + 1);
    assert(counters.get(static_cast<RecordingGapCounters::Cause>(cause)) ==
           static_cast<uint32_t>(cause) + 1U);
  }
  counters.add(RecordingGapCounters::kSdUnavailable, UINT32_MAX);
  assert(counters.get(RecordingGapCounters::kSdUnavailable) == UINT32_MAX);
  counters.add(RecordingGapCounters::kSdUnavailable);
  assert(counters.get(RecordingGapCounters::kSdUnavailable) == UINT32_MAX);
  counters.add(static_cast<RecordingGapCounters::Cause>(255));
  assert(counters.get(static_cast<RecordingGapCounters::Cause>(255)) == 0);
  return 0;
}
