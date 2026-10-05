#pragma once

#include "ota_parked_policy.h"

// Records high-rate sensor evidence that the standby owner could miss while
// blocked in storage, OBD, hashing, or modem operations. Callers must serialize
// access (the firmware uses otaActivityMux; host tests are single-threaded).
class OTASensorActivityLatch {
 public:
  enum Event : uint8_t {
    kNoEvent = 0,
    kMotion = 1,
    kSupply = 2,
    kMotionSensorUnavailable = 4,
  };

  void observeMotion(float accelerationMagnitudeG, float thresholdG) {
    if (accelerationMagnitudeG >= thresholdG) m_events |= kMotion;
  }

  void observeSupply(float volts) {
    if (!OTAParkedPolicy::vehicleSupplyPlausible(volts)) m_events |= kSupply;
  }

  void observeMotionRead(bool valid) {
    if (!valid) m_events |= kMotionSensorUnavailable;
  }

  uint8_t consume() {
    const uint8_t events = m_events;
    m_events = kNoEvent;
    return events;
  }

 private:
  uint8_t m_events = kNoEvent;
};
