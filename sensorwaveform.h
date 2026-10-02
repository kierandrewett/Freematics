/*
 * Bounded passive sensor waveform retention.
 *
 * Format 1 emits repeated groups in acquisition order:
 *   0xA0: milliseconds;centivolts (two uint32 values)
 *   0xA1: milliseconds (uint32), 0xA2: raw acceleration (three floats),
 *         0xA3: gyroscope (three floats)
 *   0xA4: cumulative voltage-overflow;motion-overflow;invalid-voltage;
 *         invalid-motion (four uint32 values)
 *   0xA5: format version (one uint8 value)
 *
 * The caller protects record and emit boundaries with sensorMux. The class
 * does no sensor I/O. Voltage is the passive, uncalibrated device ADC input.
 */
#ifndef SENSOR_WAVEFORM_H
#define SENSOR_WAVEFORM_H

#include <stdint.h>
#include <math.h>
#include <string.h>

class SensorWaveforms {
public:
  static const uint8_t kCapacity = 128;
  static const uint8_t kMaximumPerFrame = 16;
  static const uint8_t kFormatVersion = 1;

  SensorWaveforms() = default;

  // The caller holds sensorMux. This reports queued acquisitions only; the
  // cumulative loss counters do not keep the sampler in wrap-up.
  bool hasPending() const { return m_voltageCount || m_motionCount; }

  bool recordVoltage(uint32_t acquiredMs, float voltage)
  {
    // uint32 centivolts is the wire type. The physical envelope is much
    // smaller so an ADC or conversion fault cannot become a plausible point.
    if (!isfinite(voltage) || voltage < 0.0f || voltage > 655.35f) {
      saturatingIncrement(m_invalidVoltage);
      return false;
    }
    if (m_voltageCount == kCapacity) {
      saturatingIncrement(m_voltageOverflow);
      return false;
    }
    VoltageReading& reading = m_voltage[m_voltageTail];
    reading.timestamp = acquiredMs;
    reading.centivolts = (uint32_t)lroundf(voltage * 100.0f);
    m_voltageTail = next(m_voltageTail);
    ++m_voltageCount;
    return true;
  }

  bool recordMotion(uint32_t acquiredMs, const float acceleration[3], const float gyro[3])
  {
    if (!acceleration || !gyro || !withinMotionEnvelope(acceleration, gyro)) {
      saturatingIncrement(m_invalidMotion);
      return false;
    }
    if (m_motionCount == kCapacity) {
      saturatingIncrement(m_motionOverflow);
      return false;
    }
    MotionReading& reading = m_motion[m_motionTail];
    reading.timestamp = acquiredMs;
    memcpy(reading.acceleration, acceleration, sizeof(reading.acceleration));
    memcpy(reading.gyro, gyro, sizeof(reading.gyro));
    m_motionTail = next(m_motionTail);
    ++m_motionCount;
    return true;
  }

  void emit(CBuffer* destination)
  {
    if (!destination) return;

    // The two contract markers must be present before any waveform group.
    // This prevents a full frame from containing unlabelled waveform data.
    const uint16_t markerBytes = elementBytes(sizeof(uint32_t) * 4) + elementBytes(sizeof(uint8_t));
    if (remaining(destination) < markerBytes) return;
    uint32_t losses[4] = {m_voltageOverflow, m_motionOverflow, m_invalidVoltage, m_invalidMotion};
    uint8_t version = kFormatVersion;
    if (!destination->add(PID_WAVEFORM_LOSSES, ELEMENT_UINT32, losses, sizeof(losses), 4) ||
        !destination->add(PID_WAVEFORM_FORMAT, ELEMENT_UINT8, &version, sizeof(version))) return;

    for (uint8_t emitted = 0; emitted < kMaximumPerFrame && m_voltageCount; ++emitted) {
      if (remaining(destination) < elementBytes(sizeof(uint32_t) * 2)) break;
      const VoltageReading& reading = m_voltage[m_voltageHead];
      uint32_t values[2] = {reading.timestamp, reading.centivolts};
      if (!destination->add(PID_WAVEFORM_VOLTAGE, ELEMENT_UINT32, values, sizeof(values), 2)) break;
      m_voltageHead = next(m_voltageHead);
      --m_voltageCount;
    }

    const uint16_t motionBytes = elementBytes(sizeof(uint32_t)) +
      elementBytes(sizeof(float) * 3) + elementBytes(sizeof(float) * 3);
    for (uint8_t emitted = 0; emitted < kMaximumPerFrame && m_motionCount; ++emitted) {
      if (remaining(destination) < motionBytes) break;
      MotionReading& reading = m_motion[m_motionHead];
      uint32_t timestamp = reading.timestamp;
      // The capacity test above covers all three additions. A group is popped
      // only when all of its fields have entered the same destination frame.
      if (!destination->add(PID_WAVEFORM_MOTION_TIMESTAMP, ELEMENT_UINT32, &timestamp, sizeof(timestamp)) ||
          !destination->add(PID_WAVEFORM_RAW_ACCELERATION, ELEMENT_FLOAT, reading.acceleration,
                            sizeof(reading.acceleration), 3) ||
          !destination->add(PID_WAVEFORM_GYRO, ELEMENT_FLOAT, reading.gyro, sizeof(reading.gyro), 3)) break;
      m_motionHead = next(m_motionHead);
      --m_motionCount;
    }
  }

private:
  struct VoltageReading {
    uint32_t timestamp;
    uint32_t centivolts;
  };
  struct MotionReading {
    uint32_t timestamp;
    float acceleration[3];
    float gyro[3];
  };

  static uint8_t next(uint8_t index) { return index + 1 == kCapacity ? 0 : index + 1; }
  static uint16_t elementBytes(uint16_t valueBytes) { return sizeof(ELEMENT_HEAD) + valueBytes; }
  static uint16_t remaining(const CBuffer* destination) { return BUFFER_LENGTH - destination->offset; }
  static void saturatingIncrement(uint32_t& value) { if (value != UINT32_MAX) ++value; }
  static bool withinMotionEnvelope(const float acceleration[3], const float gyro[3])
  {
    for (uint8_t i = 0; i < 3; ++i) {
      if (!isfinite(acceleration[i]) || !isfinite(gyro[i]) || fabsf(acceleration[i]) > 64.0f ||
          fabsf(gyro[i]) > 4000.0f) return false;
    }
    return true;
  }

  VoltageReading m_voltage[kCapacity] = {};
  MotionReading m_motion[kCapacity] = {};
  uint8_t m_voltageHead = 0;
  uint8_t m_voltageTail = 0;
  uint8_t m_voltageCount = 0;
  uint8_t m_motionHead = 0;
  uint8_t m_motionTail = 0;
  uint8_t m_motionCount = 0;
  uint32_t m_voltageOverflow = 0;
  uint32_t m_motionOverflow = 0;
  uint32_t m_invalidVoltage = 0;
  uint32_t m_invalidMotion = 0;
};

#endif
