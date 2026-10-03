#ifndef FREEMATICS_OTA_STAGE_POLICY_H
#define FREEMATICS_OTA_STAGE_POLICY_H

namespace freematics {
namespace ota {

enum StageResult {
  kStageInstalled,
  kStageCancelled,
  kStageImageFinishFailed,
  kStagePendingDigestFailed,
  kStageBootSelectionFailed,
};

inline bool stageCancelled(const volatile bool* requested) {
  return requested && *requested;
}

// Operations must keep abortImage valid only until finishImage succeeds.
// The helper keeps the irreversible boot-slot change last, and removes a
// possibly partial pending marker on every failure after writing it.
template <typename Operations>
StageResult stageVerifiedImage(Operations& operations,
                               const volatile bool* cancelRequested) {
  if (stageCancelled(cancelRequested)) {
    operations.abortImage();
    return kStageCancelled;
  }
  if (!operations.finishImage()) return kStageImageFinishFailed;
  if (stageCancelled(cancelRequested)) return kStageCancelled;
  if (!operations.writePendingDigest()) {
    operations.erasePendingDigest();
    return kStagePendingDigestFailed;
  }
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
