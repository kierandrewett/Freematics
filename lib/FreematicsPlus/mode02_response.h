#ifndef FREEMATICS_MODE02_RESPONSE_H
#define FREEMATICS_MODE02_RESPONSE_H

#include <stddef.h>
#include <stdint.h>
#include <ctype.h>
#include <stdio.h>
#include <string.h>

namespace freematics {
namespace mode02 {

inline bool readByte(const char*& cursor, uint8_t& value)
{
  while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' ||
         *cursor == '\n' || *cursor == ':') ++cursor;
  if (!cursor[0] || !cursor[1] || !isxdigit((unsigned char)cursor[0]) ||
      !isxdigit((unsigned char)cursor[1])) return false;
  unsigned int parsed = 0;
  if (sscanf(cursor, "%2x", &parsed) != 1) return false;
  value = (uint8_t)parsed;
  cursor += 2;
  return true;
}

// Extract the exact Mode 02 frame-0 data bytes from an ELM text response.
// ELM headers, command echo, spacing and line endings are tolerated, but a
// different service, PID, frame number, or truncated data is rejected.
inline bool parseFrame0(const char* response, uint8_t pid, size_t dataBytes,
                        char* normalized, size_t normalizedCapacity)
{
  if (!response || !normalized || dataBytes == 0 || dataBytes > 4 ||
      normalizedCapacity < dataBytes * 3) return false;
  normalized[0] = 0;
  for (const char* start = response; *start; ++start) {
    if (start[0] != '4' || start[1] != '2') continue;
    if (start != response && start[-1] != ' ' && start[-1] != '\t' &&
        start[-1] != '\r' && start[-1] != '\n' && start[-1] != ':') continue;
    const char* cursor = start + 2;
    uint8_t responsePid = 0;
    uint8_t frame = 0;
    if (!readByte(cursor, responsePid) || responsePid != pid ||
        !readByte(cursor, frame) || frame != 0) continue;

    char candidate[16] = {};
    size_t offset = 0;
    bool valid = true;
    for (size_t index = 0; index < dataBytes; ++index) {
      uint8_t value = 0;
      if (!readByte(cursor, value)) { valid = false; break; }
      const int written = snprintf(candidate + offset, sizeof(candidate) - offset,
                                   index ? " %02X" : "%02X", value);
      if (written <= 0 || (size_t)written >= sizeof(candidate) - offset) {
        valid = false;
        break;
      }
      offset += (size_t)written;
    }
    if (!valid || offset + 1 > normalizedCapacity) continue;
    memcpy(normalized, candidate, offset + 1);
    return true;
  }
  return false;
}

} // namespace mode02
} // namespace freematics

#endif
