#ifndef FREEMATICS_OTA_BOOT_POLICY_H
#define FREEMATICS_OTA_BOOT_POLICY_H

namespace freematics {
namespace ota {

enum BootFailureReason {
  kBootCoreValidationFailed,
  kBootConfirmationFailed,
};

enum BootResult {
  kBootAccepted,
  kBootRolledBack,
  kBootRollbackFailed,
  kBootAcceptedIdentityNotSaved,
};

// This is called only for ESP_OTA_IMG_PENDING_VERIFY. Keep the acceptance
// order explicit: validate every required service and image identity first,
// confirm with the bootloader second, and persist optional release metadata
// only after the bootloader has accepted the image.
template <typename Operations>
BootResult validateAndAcceptPendingImage(bool storageReady,
                                         bool motionSensorReady,
                                         bool endpointReady,
                                         bool credentialReady,
                                         bool imageIdentityValid,
                                         Operations& operations) {
  if (!storageReady || !motionSensorReady || !endpointReady || !credentialReady ||
      !imageIdentityValid) {
    return operations.rollback(kBootCoreValidationFailed)
        ? kBootRolledBack : kBootRollbackFailed;
  }
  if (!operations.confirmBoot()) {
    return operations.rollback(kBootConfirmationFailed)
        ? kBootRolledBack : kBootRollbackFailed;
  }
  if (!operations.saveInstalledIdentity()) {
    operations.erasePendingIdentity();
    return kBootAcceptedIdentityNotSaved;
  }
  operations.erasePendingIdentity();
  return kBootAccepted;
}

} // namespace ota
} // namespace freematics

#endif
