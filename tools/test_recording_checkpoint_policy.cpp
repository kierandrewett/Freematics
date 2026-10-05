#include "../recording_checkpoint_policy.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>

static void testWrapUpRetriesUntilDurableCommit() {
  freematics::recording::WrapUpCheckpointPolicy policy;
  assert(!policy.pending());
  policy.begin();
  assert(policy.pending());

  // A failed append or transient SD lock contention does not complete it.
  policy.observeJournalCommit(7, 7);
  assert(policy.pending());

  // A later successful journal append records the final missed-cycle count.
  policy.observeJournalCommit(7, 8);
  assert(!policy.pending());
}

static void testCommitCounterWrapStillCompletesCheckpoint() {
  freematics::recording::WrapUpCheckpointPolicy policy;
  policy.begin();
  policy.observeJournalCommit(UINT32_MAX, 0);
  assert(!policy.pending());
}

static void testRepeatedWrapUpCanStartAnotherCheckpoint() {
  freematics::recording::WrapUpCheckpointPolicy policy;
  policy.begin();
  policy.observeJournalCommit(10, 11);
  assert(!policy.pending());
  policy.begin();
  assert(policy.pending());
}

static void testPendingCheckpointKeepsWrapUpOpenOnlyForBoundedWindow() {
  freematics::recording::WrapUpCheckpointPolicy policy;
  policy.begin();
  const uint32_t since = UINT32_MAX - 1000;
  assert(policy.keepWrapUpOpen(since + 500, since, 2000));
  assert(!policy.keepWrapUpOpen(since + 2000, since, 2000));
  policy.observeJournalCommit(1, 2);
  assert(!policy.keepWrapUpOpen(since + 500, since, 2000));
}

int main() {
  testWrapUpRetriesUntilDurableCommit();
  testCommitCounterWrapStillCompletesCheckpoint();
  testRepeatedWrapUpCanStartAnotherCheckpoint();
  testPendingCheckpointKeepsWrapUpOpenOnlyForBoundedWindow();
  puts("Recording wrap-up checkpoint policy: all tests passed");
  return 0;
}
