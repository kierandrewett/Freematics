#ifndef FREEMATICS_OTA_STAGE_POLICY_H
#define FREEMATICS_OTA_STAGE_POLICY_H

namespace freematics {
namespace ota {

enum StageResult {
  kStageInstalled,
  kStageReadyForActivation,
  kStageCancelled,
  kStageImageFinishFailed,
  kStageBootSelectionFailed,
};

inline bool stageCancelled(const volatile bool* requested) {
  return requested && *requested;
}

// Operations must keep abortImage valid only until finishImage succeeds.
// Staging must not persist boot intent or select a boot slot. Both happen
// later, after the caller has completed its final vehicle safety checks.
template <typename Operations>
StageResult stageVerifiedImage(Operations& operations,
                               const volatile bool* cancelRequested) {
  if (stageCancelled(cancelRequested)) {
    operations.abortImage();
    return kStageCancelled;
  }
  if (!operations.finishImage()) return kStageImageFinishFailed;
  if (stageCancelled(cancelRequested)) return kStageCancelled;
  return kStageReadyForActivation;
}

// Boot-slot selection is separate so vehicle and storage safety can be
// rechecked after cellular transfer but before the device can boot the image.
template <typename Operations>
StageResult activateVerifiedImage(Operations& operations,
                                  const volatile bool* cancelRequested) {
  if (stageCancelled(cancelRequested)) {
    operations.erasePendingDigest();
    return kStageCancelled;
  }
  if (!operations.selectBootPartition()) {
    operations.erasePendingDigest();
    return kStageBootSelectionFailed;
  }
  return kStageInstalled;
}

} // namespace ota
} // namespace freematics

#endif
