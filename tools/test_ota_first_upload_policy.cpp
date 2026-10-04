#include "../ota_first_upload_policy.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>

static void testRequiresCurrentBootDataAndAllCoreServices() {
  OTAFirstUploadPolicy policy;
  policy.begin(100, true);
  assert(policy.observe(200, false, true, true, true, true) ==
         OTAFirstUploadPolicy::kWait); // old backlog only
  assert(policy.observe(300, true, false, true, true, true) ==
         OTAFirstUploadPolicy::kWait);
  assert(policy.observe(400, true, true, false, true, true) ==
         OTAFirstUploadPolicy::kWait);
  assert(policy.observe(500, true, true, true, false, true) ==
         OTAFirstUploadPolicy::kWait);
  assert(policy.observe(600, true, true, true, true, false) ==
         OTAFirstUploadPolicy::kWait);
  assert(policy.observe(700, true, true, true, true, true) ==
         OTAFirstUploadPolicy::kConfirm);
  assert(!policy.pending());
  assert(policy.observe(800, true, true, true, true, true) ==
         OTAFirstUploadPolicy::kWait); // confirm only once
}

static void testFailedUploadNeverQualifiesAndTimeoutRollsBack() {
  OTAFirstUploadPolicy policy;
  policy.begin(1000, true);
  assert(policy.observe(1001, false, true, true, true, true) ==
         OTAFirstUploadPolicy::kWait);
  assert(policy.observe(1000 + OTAFirstUploadPolicy::kValidationTimeoutMs - 1,
                        true, true, true, true, true) ==
         OTAFirstUploadPolicy::kConfirm);

  policy.begin(2000, true);
  assert(policy.observe(2000 + OTAFirstUploadPolicy::kValidationTimeoutMs,
                        true, true, true, true, true) ==
         OTAFirstUploadPolicy::kRollback);
  assert(policy.pending());
}

static void testTimeoutArithmeticSurvivesMillisWrap() {
  const uint32_t start = UINT32_MAX - 300000UL;
  OTAFirstUploadPolicy policy;
  policy.begin(start, true);
  assert(policy.observe(start + OTAFirstUploadPolicy::kValidationTimeoutMs - 1,
                        false, true, true, true, true) ==
         OTAFirstUploadPolicy::kWait);
  assert(policy.observe(start + OTAFirstUploadPolicy::kValidationTimeoutMs,
                        false, true, true, true, true) ==
         OTAFirstUploadPolicy::kRollback);
}

static void testJournalPositionsExcludeOldBacklogAndTrackNewEpochs() {
  assert(!otaRecordWasJournaledThisBoot(120, 512)); // pre-boot backlog
  assert(otaRecordWasJournaledThisBoot(512, 512));  // first append this boot
  assert(otaRecordWasJournaledThisBoot(900, 512));
  // After full-journal archival, the queue resets both offsets to zero.
  assert(otaRecordWasJournaledThisBoot(0, 0));
  // Recovery rebases at the replacement journal end; only later appends count.
  assert(!otaRecordWasJournaledThisBoot(700, 1024));
  assert(otaRecordWasJournaledThisBoot(1024, 1024));
}

int main() {
  testRequiresCurrentBootDataAndAllCoreServices();
  testFailedUploadNeverQualifiesAndTimeoutRollsBack();
  testTimeoutArithmeticSurvivesMillisWrap();
  testJournalPositionsExcludeOldBacklogAndTrackNewEpochs();
  puts("OTA first-upload verification: all tests passed");
  return 0;
}
