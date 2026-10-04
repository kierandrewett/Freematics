#ifndef USB_TELEMETRY_METADATA_H_INCLUDED
#define USB_TELEMETRY_METADATA_H_INCLUDED

#include <stddef.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#define USB_TELEMETRY_SUPPORT_SIZE 2048
#define OBD_PID(...) + 1
enum { USB_TELEMETRY_RAW_PID_COUNT = 0
#include "obd_pids.h"
};
#undef OBD_PID

struct UsbRawMode01Value {
  uint8_t pid;
  uint8_t length;
  uint8_t bytes[4];
  uint8_t valid;
};

inline bool appendSupportedMode01Pid(char* output, size_t capacity, size_t& offset,
                                     uint8_t pid)
{
  if (!output || offset >= capacity) return false;
  const int written = snprintf(output + offset, capacity - offset, "%s%02X",
                               offset ? "," : "", pid);
  if (written < 0 || (size_t)written >= capacity - offset) return false;
  offset += (size_t)written;
  return true;
}

inline bool appendRawMode01Metadata(char* output, size_t capacity, size_t& offset,
                                    const UsbRawMode01Value* values, size_t count)
{
  if (!output || offset >= capacity || (!values && count)) return false;
  size_t present = 0;
  for (size_t i = 0; i < count; ++i) if (values[i].valid) ++present;
  if (!present) return true;
  size_t required = 5; // ";raw="
  bool firstRequired = true;
  for (size_t i = 0; i < count; ++i) {
    if (!values[i].valid) continue;
    if (!values[i].length || values[i].length > sizeof(values[i].bytes)) return false;
    required += (firstRequired ? 0 : 1) + 3 + (size_t)values[i].length * 2;
    firstRequired = false;
  }
  if (required >= capacity - offset) return false;
  static const char hex[] = "0123456789ABCDEF";
  memcpy(output + offset, ";raw=", 5);
  offset += 5;
  bool first = true;
  for (size_t i = 0; i < count; ++i) {
    const UsbRawMode01Value& value = values[i];
    if (!value.valid) continue;
    if (!first) output[offset++] = ',';
    first = false;
    output[offset++] = hex[value.pid >> 4];
    output[offset++] = hex[value.pid & 0x0F];
    output[offset++] = ':';
    for (uint8_t byteIndex = 0; byteIndex < value.length; ++byteIndex) {
      output[offset++] = hex[value.bytes[byteIndex] >> 4];
      output[offset++] = hex[value.bytes[byteIndex] & 0x0F];
    }
  }
  output[offset] = 0;
  return true;
}

#endif
