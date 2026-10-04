#ifndef FREEMATICS_OTA_COLLECTOR_ACK_H
#define FREEMATICS_OTA_COLLECTOR_ACK_H

#include <stddef.h>
#include <stdint.h>

// Accept only the collector's exact "OK <count>" body, optionally followed by
// a single HTTP-style line ending. The caller supplies the received byte count
// so embedded NULs and bytes after a C-string terminator cannot be ignored.
inline bool otaCollectorAckMatches(const char* body, size_t length,
                                   unsigned int expected)
{
  if (!body || !length || !expected || length < 4 || body[0] != 'O' ||
      body[1] != 'K' || body[2] != ' ') return false;

  size_t at = 3;
  unsigned long reported = 0;
  size_t digits = 0;
  while (at < length && body[at] >= '0' && body[at] <= '9') {
    reported = reported * 10 + (unsigned int)(body[at] - '0');
    if (reported > 0xFFFFFFFFUL) return false;
    ++at;
    ++digits;
  }
  if (!digits || (digits > 1 && body[3] == '0') || reported != expected) return false;
  if (at == length) return true;
  if (body[at] == '\r') ++at;
  if (at < length && body[at] == '\n') ++at;
  return at == length;
}

#endif
