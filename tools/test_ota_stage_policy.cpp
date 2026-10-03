#include "../ota_stage_policy.h"

#include <assert.h>
#include <stdio.h>
#include <string>
#include <vector>

namespace {

struct FakeOperations {
  std::vector<std::string> calls;
  bool finishOkay;
  bool pendingOkay;
  bool bootOkay;
  volatile bool* cancelOnFinish;
  volatile bool* cancelOnPending;

  FakeOperations() : finishOkay(true), pendingOkay(true), bootOkay(true),
                     cancelOnFinish(0), cancelOnPending(0) {}

  void abortImage() { calls.push_back("abort"); }
  bool finishImage() {
    calls.push_back("finish");
    if (cancelOnFinish) *cancelOnFinish = true;
    return finishOkay;
  }
  bool writePendingDigest() {
    calls.push_back("pending");
    if (cancelOnPending) *cancelOnPending = true;
    return pendingOkay;
  }
  void erasePendingDigest() { calls.push_back("erase"); }
  bool selectBootPartition() {
    calls.push_back("select");
    return bootOkay;
  }
};

void testSuccessSelectsBootOnlyAfterPendingDigest() {
  volatile bool cancelled = false;
  FakeOperations operations;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageInstalled);
  assert((operations.calls == std::vector<std::string>{"finish", "pending", "select"}));
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

void testPendingFailureClearsMarkerAndNeverSelectsBoot() {
  volatile bool cancelled = false;
  FakeOperations operations;
  operations.pendingOkay = false;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStagePendingDigestFailed);
  assert((operations.calls == std::vector<std::string>{"finish", "pending", "erase"}));
}

void testCancellationAfterPendingClearsMarker() {
  volatile bool cancelled = false;
  FakeOperations operations;
  operations.cancelOnPending = &cancelled;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageCancelled);
  assert((operations.calls == std::vector<std::string>{"finish", "pending", "erase"}));
}

void testBootSelectionFailureClearsMarker() {
  volatile bool cancelled = false;
  FakeOperations operations;
  operations.bootOkay = false;
  assert(freematics::ota::stageVerifiedImage(operations, &cancelled) ==
         freematics::ota::kStageBootSelectionFailed);
  assert((operations.calls == std::vector<std::string>{"finish", "pending", "select", "erase"}));
}

} // namespace

int main() {
  testSuccessSelectsBootOnlyAfterPendingDigest();
  testCancellationBeforeFinishAbortsImage();
  testFinishFailureNeverWritesPendingOrSelectsBoot();
  testCancellationAfterFinishLeavesRunningSlotSelected();
  testPendingFailureClearsMarkerAndNeverSelectsBoot();
  testCancellationAfterPendingClearsMarker();
  testBootSelectionFailureClearsMarker();
  puts("OTA staging transaction: all tests passed");
  return 0;
}
