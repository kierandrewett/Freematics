#ifndef USB_TELEMETRY_H_INCLUDED
#define USB_TELEMETRY_H_INCLUDED

#include <Arduino.h>
#include "config.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"

#define USB_TELEMETRY_QUEUE_DEPTH 4
#define USB_TELEMETRY_LINE_SIZE SAMPLE_FRAME_SIZE
#define USB_TELEMETRY_SUPPORT_SIZE 256

struct UsbTelemetryRecord {
  uint16_t length;
  uint32_t captureMs;
  uint64_t captureUtcMs;
  uint64_t bootId;
  uint32_t dropped;
  uint8_t utcValid;
  char supported[USB_TELEMETRY_SUPPORT_SIZE];
  char payload[USB_TELEMETRY_LINE_SIZE];
};

class UsbTelemetryQueue {
public:
  UsbTelemetryRecord* reserve()
  {
    portENTER_CRITICAL(&m_mux);
    if (m_write - m_read >= USB_TELEMETRY_QUEUE_DEPTH) {
      if (m_dropped != UINT32_MAX) ++m_dropped;
      portEXIT_CRITICAL(&m_mux);
      return nullptr;
    }
    UsbTelemetryRecord* record = &m_records[m_write % USB_TELEMETRY_QUEUE_DEPTH];
    portEXIT_CRITICAL(&m_mux);
    return record;
  }

  bool publish(UsbTelemetryRecord* record, uint16_t length)
  {
    if (!record || length == 0 || length >= USB_TELEMETRY_LINE_SIZE) {
      noteDrop();
      return false;
    }
    record->length = length;
    portENTER_CRITICAL(&m_mux);
    ++m_write;
    portEXIT_CRITICAL(&m_mux);
    return true;
  }

  UsbTelemetryRecord* peek()
  {
    portENTER_CRITICAL(&m_mux);
    UsbTelemetryRecord* record = m_read == m_write ? nullptr :
      &m_records[m_read % USB_TELEMETRY_QUEUE_DEPTH];
    portEXIT_CRITICAL(&m_mux);
    return record;
  }

  void release()
  {
    portENTER_CRITICAL(&m_mux);
    if (m_read != m_write) ++m_read;
    portEXIT_CRITICAL(&m_mux);
  }

  uint32_t dropped() const
  {
    portENTER_CRITICAL(&m_mux);
    const uint32_t result = m_dropped;
    portEXIT_CRITICAL(&m_mux);
    return result;
  }

private:
  void noteDrop()
  {
    portENTER_CRITICAL(&m_mux);
    if (m_dropped != UINT32_MAX) ++m_dropped;
    portEXIT_CRITICAL(&m_mux);
  }

  UsbTelemetryRecord m_records[USB_TELEMETRY_QUEUE_DEPTH] = {};
  uint32_t m_read = 0;
  uint32_t m_write = 0;
  uint32_t m_dropped = 0;
  mutable portMUX_TYPE m_mux = portMUX_INITIALIZER_UNLOCKED;
};

#endif
