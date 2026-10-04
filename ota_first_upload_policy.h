#pragma once

#include <stdint.h>

inline bool otaRecordWasJournaledThisBoot(uint32_t recordPosition,
                                          uint32_t bootStartPosition) {
  // The append-only FAT32 journal stays below 4 GiB; its offsets do not wrap
  // within a mounted journal generation.
  return recordPosition >= bootStartPosition;
}

// Keep a newly selected image rollback-capable until one current-boot sample
// has made it through the SD journal and received an accepted collector POST.
class OTAFirstUploadPolicy {
 public:
  static const uint32_t kValidationTimeoutMs = 15UL * 60UL * 1000UL;

  enum Decision { kWait, kConfirm, kRollback };

  OTAFirstUploadPolicy() : m_startedAtMs(0), m_pending(false) {}

  void begin(uint32_t nowMs, bool pendingImage) {
    m_startedAtMs = nowMs;
    m_pending = pendingImage;
  }

  Decision observe(uint32_t nowMs, bool acceptedPostContainsCurrentBootRecord,
                   bool storageHealthy, bool motionSensorReady,
                   bool endpointReady, bool credentialReady) {
    if (!m_pending) return kWait;
    if (static_cast<uint32_t>(nowMs - m_startedAtMs) >= kValidationTimeoutMs)
      return kRollback;
    if (!acceptedPostContainsCurrentBootRecord || !storageHealthy ||
        !motionSensorReady || !endpointReady || !credentialReady)
      return kWait;
    m_pending = false;
    return kConfirm;
  }

  bool pending() const { return m_pending; }

 private:
  uint32_t m_startedAtMs;
  bool m_pending;
};
