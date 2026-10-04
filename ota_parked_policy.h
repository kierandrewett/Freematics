#pragma once

#include <float.h>
#include <stdint.h>

// Host-testable fail-closed gate for beginning a parked OTA attempt.
// The integration layer must feed every MEMS read through
// observeMotionSample() and call observe() with current acquisition timestamps
// and support/validity flags. Quiet time is earned only while MEMS samples
// continuously prove the device has remained still; OBD signals are checked
// separately as a final, fail-closed eligibility gate.
class OTAParkedPolicy {
 public:
  static const uint32_t kRequiredQuietMs = 60UL * 60UL * 1000UL;
  // Require a stable 12 V-class battery before modem activity or flash writes.
  // This conservative lower bound is intentionally above the board's
  // brownout region; a live hardware soak must still validate ADC accuracy.
  static constexpr float kMinimumVehicleSupplyVolts = 12.2f;
  // Match RESTING_VOLTAGE_MAX: an elevated reading is ambiguous (charging,
  // recently charged, or sensor error), so it must not authorize OTA.
  static constexpr float kMaximumRestingVoltage = 12.9f;
  // Standby polls the accelerometer every 250 ms. A missed or stalled poll
  // longer than this breaks the evidence of continuous quiet.
  static const uint32_t kMotionSampleMaxGapMs = 1500UL;

  static bool vehicleSupplyPlausible(float volts) {
    return volts == volts && volts <= FLT_MAX && volts >= -FLT_MAX &&
        volts >= kMinimumVehicleSupplyVolts && volts <= kMaximumRestingVoltage;
  }

  struct Signal {
    bool supported;
    bool valid;
    uint32_t sampledAtMs;
    uint32_t maxAgeMs;
    float value;
  };

  struct Observation {
    bool confirmedMotionWake;
    bool activity;
    bool durableStorageHealthy;
    bool telemetryEndpointConfigured;
    bool telemetryCredentialPersisted;
    Signal speedKph;
    Signal rpm;
    Signal modelBSupplyVolts;
  };

  enum Denial {
    kEligible,
    kQuietPeriod,
    kMotionWake,
    kActivity,
    kStorageUnavailable,
    kEndpointUnavailable,
    kCredentialUnavailable,
    kMotionUnavailable,
    kSpeedUnavailable,
    kSpeedNotZero,
    kRpmUnavailable,
    kRpmNotZero,
    kSupplyUnavailable,
    kSupplyTooLow,
    kSupplyNotResting,
  };

  OTAParkedPolicy()
      : m_quietSinceMs(0), m_lastMotionSampleMs(0), m_started(false),
        m_motionProofValid(false) {}

  // Call once on every boot. A prior off timer is never restored across boots.
  void beginBoot(uint32_t nowMs) {
    m_quietSinceMs = nowMs;
    m_lastMotionSampleMs = nowMs;
    m_started = true;
    m_motionProofValid = false;
  }

  // Quiet time is earned only from a continuous series of successful MEMS
  // samples. Any invalid read or detected movement restarts the proof window.
  void observeMotionSample(uint32_t nowMs, bool sampleValid, bool motionDetected) {
    if (!m_started) beginBoot(nowMs);
    if (!sampleValid) {
      m_quietSinceMs = nowMs;
      m_lastMotionSampleMs = nowMs;
      m_motionProofValid = false;
      return;
    }
    if (motionDetected || !m_motionProofValid ||
        static_cast<uint32_t>(nowMs - m_lastMotionSampleMs) > kMotionSampleMaxGapMs) {
      m_quietSinceMs = nowMs;
    }
    m_lastMotionSampleMs = nowMs;
    m_motionProofValid = true;
  }

  bool motionProofCurrent(uint32_t nowMs) const {
    return m_started && m_motionProofValid &&
        static_cast<uint32_t>(nowMs - m_lastMotionSampleMs) <= kMotionSampleMaxGapMs;
  }

  bool quietPeriodComplete(uint32_t nowMs) const {
    return motionProofCurrent(nowMs) && elapsed(nowMs) >= kRequiredQuietMs;
  }

  Denial observe(uint32_t nowMs, const Observation& observation) {
    if (!m_started) beginBoot(nowMs);

    if (observation.confirmedMotionWake) return reset(nowMs, kMotionWake);
    if (observation.activity) return reset(nowMs, kActivity);

    if (!motionProofCurrent(nowMs)) return kMotionUnavailable;
    if (elapsed(nowMs) < kRequiredQuietMs) return kQuietPeriod;

    if (!observation.durableStorageHealthy) return kStorageUnavailable;
    if (!observation.telemetryEndpointConfigured) return kEndpointUnavailable;
    if (!observation.telemetryCredentialPersisted) return kCredentialUnavailable;

    if (!fresh(observation.speedKph, nowMs)) return kSpeedUnavailable;
    if (observation.speedKph.value != 0.0f) return reset(nowMs, kSpeedNotZero);
    if (!fresh(observation.rpm, nowMs)) return kRpmUnavailable;
    if (observation.rpm.value != 0.0f) return reset(nowMs, kRpmNotZero);
    if (!fresh(observation.modelBSupplyVolts, nowMs)) return kSupplyUnavailable;
    if (observation.modelBSupplyVolts.value <= 0.0f) return kSupplyUnavailable;
    if (observation.modelBSupplyVolts.value > kMaximumRestingVoltage) return kSupplyNotResting;
    if (observation.modelBSupplyVolts.value < kMinimumVehicleSupplyVolts) return kSupplyTooLow;

    return kEligible;
  }

  uint32_t quietDurationMs(uint32_t nowMs) const {
    return m_started && m_motionProofValid
        ? static_cast<uint32_t>(nowMs - m_quietSinceMs) : 0;
  }

 private:
  static bool fresh(const Signal& signal, uint32_t nowMs) {
    if (!signal.supported || !signal.valid || signal.value != signal.value ||
        signal.value > FLT_MAX || signal.value < -FLT_MAX) return false;
    const uint32_t age = static_cast<uint32_t>(nowMs - signal.sampledAtMs);
    return age <= signal.maxAgeMs;
  }

  uint32_t elapsed(uint32_t nowMs) const {
    return static_cast<uint32_t>(nowMs - m_quietSinceMs);
  }

  Denial reset(uint32_t nowMs, Denial reason) {
    m_quietSinceMs = nowMs;
    m_lastMotionSampleMs = nowMs;
    m_motionProofValid = false;
    return reason;
  }

  uint32_t m_quietSinceMs;
  uint32_t m_lastMotionSampleMs;
  bool m_started;
  bool m_motionProofValid;
};
