#include "../ota_cancel_policy.h"

#include <assert.h>
#include <stdio.h>

int main()
{
  using freematics::ota::cancellationWaitExpired;
  const uint32_t started = 0xFFFFFFF0UL;
  assert(!cancellationWaitExpired(started,
      started + freematics::ota::kCancellationCompletionTimeoutMs - 1));
  assert(cancellationWaitExpired(started,
      started + freematics::ota::kCancellationCompletionTimeoutMs));
  assert(!cancellationWaitExpired(100, 100 + 29999));
  assert(cancellationWaitExpired(100, 100 + 30000));
  puts("OTA cancellation completion deadline: rollover and boundary passed");
  return 0;
}
