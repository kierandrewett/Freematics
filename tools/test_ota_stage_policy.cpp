#include "../ota_stage_policy.h"

#include <assert.h>
#include <stdio.h>
#include <string>
#include <vector>

namespace {

struct FakeOperations {
  std::vector<std::string> calls;
  bool finishOkay;
  bool bootOkay;
  volatile bool* cancelOnFinish;

  FakeOperations() : finishOkay(true), bootOkay(true), cancelOnFinish(0) {}

  void abortImage() { calls.push_back("abort"); }
  bool finishImage() {
    calls.push_back("finish");
    if (cancelOnFinish) *cancelOnFinish = true;
    return finishOkay;
  }
  bool selectBootPartition() {
    calls.push_back("select");
    return bootOkay;
  }
  void erasePendingDigest() { calls.push_back("erase"); }
};

void testVerificationFinishesImageWithoutPersistingIntentOrSelectingBoot() {
  volatile bool cancelled = false;
  FakeOperations operations;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageReadyForActivation);
  assert((operations.calls == std::vector<std::string>{"finish"}));
}

void testActivationSelectsBootOnlyAfterFinalGate() {
  volatile bool cancelled = false;
  FakeOperations operations;
  assert(freematics::ota::activateVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageInstalled);
  assert((operations.calls == std::vector<std::string>{"select"}));
}

void testBootSelectionFailureClearsPreparedIntent() {
  volatile bool cancelled = false;
  FakeOperations operations;
  operations.bootOkay = false;
  assert(freematics::ota::activateVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageBootSelectionFailed);
  assert((operations.calls == std::vector<std::string>{"select", "erase"}));
}

void testCancellationImmediatelyBeforeActivationClearsPreparedIntent() {
  volatile bool cancelled = true;
  FakeOperations operations;
  assert(freematics::ota::activateVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageCancelled);
  assert((operations.calls == std::vector<std::string>{"erase"}));
}

void testCancellationBeforeFinishAbortsImage() {
  volatile bool cancelled = true;
  FakeOperations operations;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageCancelled);
  assert((operations.calls == std::vector<std::string>{"abort"}));
}

void testFinishFailureNeverWritesPendingOrSelectsBoot() {
  volatile bool cancelled = false;
  FakeOperations operations;
  operations.finishOkay = false;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageImageFinishFailed);
  assert((operations.calls == std::vector<std::string>{"finish"}));
}

void testCancellationAfterFinishLeavesRunningSlotSelected() {
  volatile bool cancelled = false;
  FakeOperations operations;
  operations.cancelOnFinish = &cancelled;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageCancelled);
  assert((operations.calls == std::vector<std::string>{"finish"}));
}

} // namespace

int main() {
  testVerificationFinishesImageWithoutPersistingIntentOrSelectingBoot();
  testActivationSelectsBootOnlyAfterFinalGate();
  testBootSelectionFailureClearsPreparedIntent();
  testCancellationImmediatelyBeforeActivationClearsPreparedIntent();
  testCancellationBeforeFinishAbortsImage();
  testFinishFailureNeverWritesPendingOrSelectsBoot();
  testCancellationAfterFinishLeavesRunningSlotSelected();
  puts("OTA staging transaction: all tests passed");
  return 0;
}
