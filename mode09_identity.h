#ifndef MODE09_IDENTITY_H_INCLUDED
#define MODE09_IDENTITY_H_INCLUDED

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace freematics {
namespace mode09 {

inline int hexNibble(char c)
{
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

// Parse a raw ELM response and skip command/searching text before the positive
// response. Header-on CAN responses are intentionally rejected; the firmware
// initializes the bridge with ATH0.
inline bool parseHexResponse(const char* response, uint8_t pid,
                             uint8_t* bytes, size_t capacity, size_t& length)
{
  length = 0;
  if (!response || !bytes || capacity < 3) return false;
  const char* start = nullptr;
  for (const char* p = response; p[0] && p[1]; ++p) {
    if (hexNibble(p[0]) != 4 || hexNibble(p[1]) != 9) continue;
    const char* q = p + 2;
    while (*q == ' ' || *q == '\t') ++q;
    if (!q[0] || !q[1]) continue;
    const int hi = hexNibble(q[0]);
    const int lo = hexNibble(q[1]);
    if (hi >= 0 && lo >= 0 && ((hi << 4) | lo) == pid) {
      start = p;
      break;
    }
  }
  if (!start) return false;
  uint8_t high = 0;
  bool haveHigh = false;
  for (const char* p = start; *p; ++p) {
    if (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') continue;
    if (*p == '>') break;
    const int nibble = hexNibble(*p);
    if (nibble < 0) break;
    if (!haveHigh) {
      high = (uint8_t)nibble;
      haveHigh = true;
    } else {
      if (length == capacity) return false;
      bytes[length++] = (high << 4) | (uint8_t)nibble;
      haveHigh = false;
    }
  }
  if (haveHigh || length < 3 || bytes[0] != 0x49 || bytes[1] != pid) {
    length = 0;
    return false;
  }
  return true;
}

inline bool supports(const uint8_t* response, size_t length, uint8_t pid)
{
  if (!response || length < 6 || response[0] != 0x49 || response[1] != 0x00 ||
      pid == 0 || pid > 0x20) return false;
  const uint32_t bitmap = ((uint32_t)response[2] << 24) |
      ((uint32_t)response[3] << 16) | ((uint32_t)response[4] << 8) | response[5];
  return (bitmap & ((uint32_t)1 << (32 - pid))) != 0;
}

// CALID (PID 04) records are 16 bytes; ECU name (PID 0A) records are 20.
// Decode the first advertised record only, and reject truncation or unsafe
// bytes instead of manufacturing a partial identifier.
inline bool parseTextRecord(const uint8_t* response, size_t length, uint8_t pid,
                            size_t recordLength, char* output, size_t capacity)
{
  if (!response || !output || !capacity || length < 3 ||
      response[0] != 0x49 || response[1] != pid || response[2] == 0 ||
      response[2] > 16 || (pid != 0x04 && pid != 0x0A) ||
      recordLength != (pid == 0x04 ? 16U : 20U) ||
      length < 3 + recordLength || capacity <= recordLength) return false;
  size_t end = recordLength;
  while (end && (response[3 + end - 1] == 0 || response[3 + end - 1] == 0xAA ||
                 response[3 + end - 1] == ' ')) --end;
  if (!end) return false;
  for (size_t i = 0; i < end; ++i) {
    const uint8_t c = response[3 + i];
    if (c < 0x20 || c > 0x7E) return false;
    output[i] = (char)c;
  }
  output[end] = 0;
  return true;
}

inline bool appendHexMetadata(char* output, size_t capacity, size_t& offset,
                              const char* key, const char* value)
{
  if (!output || !key || !value || !value[0]) return true;
  const size_t keyLength = strlen(key);
  const size_t valueLength = strlen(value);
  const size_t required = 1 + keyLength + 1 + valueLength * 2;
  if (offset >= capacity || required >= capacity - offset) return false;
  static const char hex[] = "0123456789ABCDEF";
  output[offset++] = ';';
  memcpy(output + offset, key, keyLength);
  offset += keyLength;
  output[offset++] = '=';
  for (size_t i = 0; i < valueLength; ++i) {
    const uint8_t byte = (uint8_t)value[i];
    output[offset++] = hex[byte >> 4];
    output[offset++] = hex[byte & 0x0F];
  }
  output[offset] = 0;
  return true;
}

} // namespace mode09
} // namespace freematics

#endif
