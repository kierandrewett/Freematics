#ifndef USB_TELEMETRY_CHECKSUM_H_INCLUDED
#define USB_TELEMETRY_CHECKSUM_H_INCLUDED

#include <stddef.h>
#include <stdint.h>

// Reflected CRC-32/ISO-HDLC used by the versioned USB telemetry envelope.
// Covers every byte before the '*' checksum separator, including metadata.
static inline uint32_t usbTelemetryCrc32(const uint8_t* data, size_t length)
{
  uint32_t crc = 0xFFFFFFFFUL;
  while (length--) {
    crc ^= *data++;
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320UL & (uint32_t)-(int32_t)(crc & 1));
    }
  }
  return ~crc;
}

#endif
