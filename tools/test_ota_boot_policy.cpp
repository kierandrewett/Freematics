#include "../ota_boot_policy.h"

#include <assert.h>
#include <stdio.h>
#include <string>
#include <vector>

namespace {

struct FakeOperations {
  std::vector<std::string> calls;
  bool confirmOkay;
  bool saveOkay;

  FakeOperations() : confirmOkay(true), saveOkay(true) {}

  void rollback(freematics::ota::BootFailureReason reason) {
    calls.push_back(reason == freematics::ota::kBootCoreValidationFailed
                        ? "rollback-validation" : "rollback-confirmation");
  }
  bool confirmBoot() {
    calls.push_back("confirm");
    return confirmOkay;
  }
  bool saveInstalledIdentity() {
    calls.push_back("save");
    return saveOkay;
  }
  void erasePendingIdentity() { calls.push_back("erase-pending"); }
};

freematics::ota::BootResult validate(FakeOperations& operations,
                                     bool storage = true,
                                     bool motion = true,
                                     bool credential = true,
                                     bool identity = true) {
  return freematics::ota::validateAndAcceptPendingImage(
      storage, motion, credential, identity, operations);
}

void testAcceptsOnlyAfterServicesAndBootloaderConfirmation() {
  FakeOperations operations;
  assert(validate(operations) == freematics::ota::kBootAccepted);
  assert((operations.calls == std::vector<std::string>{
      "confirm", "save", "erase-pending"}));
}

void testMissingCoreValidationRollsBackBeforeConfirmation() {
  FakeOperations storage;
  assert(validate(storage, false) == freematics::ota::kBootRolledBack);
  assert((storage.calls == std::vector<std::string>{"rollback-validation"}));

  FakeOperations motion;
  assert(validate(motion, true, false) == freematics::ota::kBootRolledBack);
  assert((motion.calls == std::vector<std::string>{"rollback-validation"}));

  FakeOperations credential;
  assert(validate(credential, true, true, false) == freematics::ota::kBootRolledBack);
  assert((credential.calls == std::vector<std::string>{"rollback-validation"}));

  FakeOperations identity;
  assert(validate(identity, true, true, true, false) == freematics::ota::kBootRolledBack);
  assert((identity.calls == std::vector<std::string>{"rollback-validation"}));
}

void testBootloaderConfirmationFailureRollsBack() {
  FakeOperations operations;
  operations.confirmOkay = false;
  assert(validate(operations) == freematics::ota::kBootRolledBack);
  assert((operations.calls == std::vector<std::string>{
      "confirm", "rollback-confirmation"}));
}

void testMetadataFailureCannotUndoBootloaderAcceptance() {
  FakeOperations operations;
  operations.saveOkay = false;
  assert(validate(operations) == freematics::ota::kBootAcceptedIdentityNotSaved);
  assert((operations.calls == std::vector<std::string>{
      "confirm", "save", "erase-pending"}));
}

} // namespace

int main() {
  testAcceptsOnlyAfterServicesAndBootloaderConfirmation();
  testMissingCoreValidationRollsBackBeforeConfirmation();
  testBootloaderConfirmationFailureRollsBack();
  testMetadataFailureCannotUndoBootloaderAcceptance();
  puts("OTA first-boot acceptance policy: all tests passed");
  return 0;
}
