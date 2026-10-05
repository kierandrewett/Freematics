#include "../ota_cancel_policy.h"

#include <assert.h>
#include <stdio.h>

struct ContinuationState {
  bool allow;
  unsigned calls;
};

static bool continuationCheck(void* context) {
  ContinuationState* state = static_cast<ContinuationState*>(context);
  ++state->calls;
  return state->allow;
}

int main()
{
  using freematics::ota::cancellationWaitExpired;
  using freematics::ota::shouldContinueTransfer;
  const uint32_t started = 0xFFFFFFF0UL;
  assert(!cancellationWaitExpired(started,
      started + freematics::ota::kCancellationCompletionTimeoutMs - 1));
  assert(cancellationWaitExpired(started,
      started + freematics::ota::kCancellationCompletionTimeoutMs));
  assert(!cancellationWaitExpired(100, 100 + 29999));
  assert(cancellationWaitExpired(100, 100 + 30000));
  volatile bool cancel = false;
  ContinuationState state = {true, 0};
  assert(shouldContinueTransfer(&cancel, continuationCheck, &state));
  assert(state.calls == 1);
  state.allow = false;
  assert(!shouldContinueTransfer(&cancel, continuationCheck, &state));
  assert(state.calls == 2);
  cancel = true;
  assert(!shouldContinueTransfer(&cancel, continuationCheck, &state));
  assert(state.calls == 2);
  puts("OTA cancellation and transfer-continuation composition passed");
  return 0;
}
