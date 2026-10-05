#include "../sd_retry_policy.h"

#include <assert.h>
#include <stdint.h>
#include <limits.h>

int main() {
  SDRecoveryBackoff retry;
  assert(retry.due(0));
  assert(retry.delayMs() == 1000);

  retry.recordAttempt(100, false);
  assert(!retry.due(1099));
  assert(retry.due(1100));
  assert(retry.delayMs() == 2000);

  retry.recordAttempt(1100, false);
  assert(!retry.due(3099));
  assert(retry.due(3100));
  assert(retry.delayMs() == 4000);

  retry.recordAttempt(3100, false);
  assert(retry.delayMs() == 8000);
  retry.recordAttempt(7100, false);
  assert(retry.delayMs() == 16000);
  retry.recordAttempt(15100, false);
  assert(retry.delayMs() == SDRecoveryBackoff::kMaximumDelayMs);
  assert(!retry.due(31099));
  assert(retry.due(31100));
  retry.recordAttempt(31100, false);
  assert(!retry.due(61099));
  assert(retry.due(61100));

  retry.recordAttempt(61100, true);
  assert(retry.due(61100));
  assert(retry.delayMs() == SDRecoveryBackoff::kInitialDelayMs);

  SDRecoveryBackoff rollover;
  const uint32_t start = UINT32_MAX - 500;
  rollover.recordAttempt(start, false);
  assert(!rollover.due(UINT32_MAX - 1));
  assert(!rollover.due(498));
  assert(rollover.due(499));
  return 0;
}
