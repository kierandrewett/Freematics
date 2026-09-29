#ifndef SDARCHIVE_H_INCLUDED
#define SDARCHIVE_H_INCLUDED

#include <stdint.h>
#include <string.h>

// Unknown dates stay on the card until a valid retention anchor is recorded.
inline bool localLogExpired(uint32_t recorded, uint32_t now)
{
    return recorded >= 1704067200UL && now >= recorded && now - recorded >= 14UL * 86400UL;
}

inline bool localLogPath(const char* path)
{
    if (!path || strncmp(path, "/DATA/", 6)) return false;
    const char* number = path + 6;
    if (*number < '1' || *number > '9') return false;
    while (*number >= '0' && *number <= '9') number++;
    return !strcmp(number, ".CSV") || !strcmp(number, ".BIN") || !strcmp(number, ".UTC");
}

#endif
