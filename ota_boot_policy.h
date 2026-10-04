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
  kBootAcceptedIdentityNotSaved,
};

// This is called only for ESP_OTA_IMG_PENDING_VERIFY. Keep the acceptance
// order explicit: validate every required service and image identity first,
// confirm with the bootloader second, and persist optional release metadata
// only after the bootloader has accepted the image.
template <typename Operations>
BootResult validateAndAcceptPendingImage(bool storageReady,
                                         bool motionSensorReady,
                                         bool credentialReady,
                                         bool imageIdentityValid,
                                         Operations& operations) {
  if (!storageReady || !motionSensorReady || !credentialReady ||
      !imageIdentityValid) {
    operations.rollback(kBootCoreValidationFailed);
    return kBootRolledBack;
  }
  if (!operations.confirmBoot()) {
    operations.rollback(kBootConfirmationFailed);
    return kBootRolledBack;
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
