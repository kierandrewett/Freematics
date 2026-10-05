#ifndef USB_TELEMETRY_H_INCLUDED
#define USB_TELEMETRY_H_INCLUDED

#include <Arduino.h>
#include <stddef.h>
#include <string.h>
#include "config.h"
#include "freertos/FreeRTOS.h"
#include "freertos/portmacro.h"
#include "usbtelemetry_metadata.h"

#define USB_TELEMETRY_QUEUE_DEPTH 2
#define USB_TELEMETRY_LINE_SIZE SAMPLE_FRAME_SIZE
#define USB_TELEMETRY_TX_BUFFER_SIZE (USB_TELEMETRY_LINE_SIZE + 512)

struct UsbTelemetryRecord {
    uint16_t length;
    uint32_t sequence;
  uint32_t captureMs;
  uint32_t captureSequence;
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
    // This stream is a live view, so queued snapshots have no replay value.
    // Keep only the record already being written and replace any waiting
    // snapshot with the newest acquisition on every producer pass.
    for (int index = 0; index < USB_TELEMETRY_QUEUE_DEPTH; ++index) {
      if (m_states[index] == READY) {
        m_states[index] = FREE;
        if (m_dropped != UINT32_MAX) ++m_dropped;
      }
    }
    int slot = findFree();
    if (slot < 0) {
      if (m_dropped != UINT32_MAX) ++m_dropped;
      portEXIT_CRITICAL(&m_mux);
      return nullptr;
    }
    m_states[slot] = RESERVED;
    UsbTelemetryRecord* record = &m_records[slot];
    portEXIT_CRITICAL(&m_mux);
    return record;
  }

  bool publish(UsbTelemetryRecord* record, uint16_t length)
  {
    if (!record) return false;
    portENTER_CRITICAL(&m_mux);
    const int slot = slotFor(record);
    if (slot < 0 || m_states[slot] != RESERVED || length == 0 ||
        length >= USB_TELEMETRY_LINE_SIZE) {
      if (slot >= 0 && m_states[slot] == RESERVED) m_states[slot] = FREE;
      if (m_dropped != UINT32_MAX) ++m_dropped;
      portEXIT_CRITICAL(&m_mux);
      return false;
    }
    record->length = length;
    record->sequence = m_nextSequence++;
    m_states[slot] = READY;
    portEXIT_CRITICAL(&m_mux);
    return true;
  }

  UsbTelemetryRecord* peek()
  {
    portENTER_CRITICAL(&m_mux);
    if (m_sending >= 0) {
      portEXIT_CRITICAL(&m_mux);
      return nullptr;
    }
    int oldest = -1;
    for (int index = 0; index < USB_TELEMETRY_QUEUE_DEPTH; ++index) {
      if (m_states[index] != READY) continue;
      if (oldest < 0 || (int32_t)(m_records[index].sequence - m_records[oldest].sequence) < 0) {
        oldest = index;
      }
    }
    UsbTelemetryRecord* record = nullptr;
    if (oldest >= 0) {
      m_states[oldest] = SENDING;
      m_sending = oldest;
      record = &m_records[oldest];
    }
    portEXIT_CRITICAL(&m_mux);
    return record;
  }

  void release()
  {
    portENTER_CRITICAL(&m_mux);
    if (m_sending >= 0) {
      m_states[m_sending] = FREE;
      m_sending = -1;
    }
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
  enum SlotState : uint8_t { FREE, RESERVED, READY, SENDING };

  int findFree() const
  {
    for (int index = 0; index < USB_TELEMETRY_QUEUE_DEPTH; ++index) {
      if (m_states[index] == FREE) return index;
    }
    return -1;
  }

  int slotFor(const UsbTelemetryRecord* record) const
  {
    for (int index = 0; index < USB_TELEMETRY_QUEUE_DEPTH; ++index) {
      if (record == &m_records[index]) return index;
    }
    return -1;
  }

  UsbTelemetryRecord m_records[USB_TELEMETRY_QUEUE_DEPTH] = {};
  SlotState m_states[USB_TELEMETRY_QUEUE_DEPTH] = {};
  uint32_t m_nextSequence = 0;
  int8_t m_sending = -1;
  uint32_t m_dropped = 0;
  mutable portMUX_TYPE m_mux = portMUX_INITIALIZER_UNLOCKED;
};

#endif
