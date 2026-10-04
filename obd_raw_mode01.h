#ifndef FREEMATICS_OBD_RAW_MODE01_H_INCLUDED
#define FREEMATICS_OBD_RAW_MODE01_H_INCLUDED

#include <stdint.h>

namespace freematics {
namespace obd_raw {

inline uint8_t expectedBytes(uint8_t pid)
{
  if (pid == 0x01) return 4;
  if (pid == 0x02 || pid == 0x03 || (pid >= 0x14 && pid <= 0x1B)) return 2;
  if ((pid >= 0x24 && pid <= 0x2B) || (pid >= 0x34 && pid <= 0x3B)) return 4;
  return 0;
}

inline int hexNibble(char value)
{
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
  return -1;
}

inline bool parseBytes(const char* text, uint8_t count, uint8_t* bytes)
{
  if (!text || !bytes || !count || count > 4) return false;
  const char* cursor = text;
  for (uint8_t i = 0; i < count; ++i) {
    while (*cursor == ' ' || *cursor == '\t') ++cursor;
    if (!cursor[0] || !cursor[1]) return false;
    const int high = hexNibble(cursor[0]);
    const int low = hexNibble(cursor[1]);
    if (high < 0 || low < 0) return false;
    const char separator = cursor[2];
    if (separator && separator != ' ' && separator != '\t' && separator != '\r' &&
        separator != '\n' && separator != '>') return false;
    bytes[i] = (uint8_t)((high << 4) | low);
    cursor += 2;
  }
  return true;
}

} // namespace obd_raw
} // namespace freematics

#endif
