#pragma once

#include "ota_parked_policy.h"

#include <float.h>

// Records high-rate sensor evidence that the standby owner could miss while
// blocked in storage, OBD, hashing, or modem operations. Callers must serialize
// access (the firmware uses otaActivityMux; host tests are single-threaded).
class OTASensorActivityLatch {
 public:
  // Keep the MEMS worker alive while parked OTA checks block on SD, OBD, or
  // hashing. Working acquisition remains high-rate; idle non-OTA standby sleeps.
  static uint32_t samplingIntervalMs(bool working, bool parkedOtaWatch) {
    if (working) return 20;
    return parkedOtaWatch ? 250 : 50;
  }

  enum Event : uint8_t {
    kNoEvent = 0,
    kMotion = 1,
    kSupply = 2,
    kMotionSensorUnavailable = 4,
  };

  static bool finiteValue(float value) {
    return value == value && value <= FLT_MAX && value >= -FLT_MAX;
  }

  static bool finiteMotionVector(const float sample[3]) {
    return sample && finiteValue(sample[0]) && finiteValue(sample[1]) &&
        finiteValue(sample[2]);
  }

  void observeMotion(float accelerationMagnitudeG, float thresholdG) {
    if (!finiteValue(accelerationMagnitudeG) || !finiteValue(thresholdG) ||
        thresholdG < 0.0f) {
      m_events |= kMotionSensorUnavailable;
      return;
    }
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
