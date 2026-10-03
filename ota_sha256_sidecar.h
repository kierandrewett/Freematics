#ifndef FREEMATICS_OTA_SHA256_SIDECAR_H
#define FREEMATICS_OTA_SHA256_SIDECAR_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace freematics {
namespace ota {

static const char SHA256_SIDECAR_SUFFIX[] = ".sha256sum";

inline const char* basename(const char* path)
{
    if (!path)
        return "";

    const char* name = path;
    for (const char* p = path; *p; ++p) {
        if (*p == '/' || *p == '\\')
            name = p + 1;
    }
    return name;
}

inline bool isHexDigit(char c)
{
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
           (c >= 'A' && c <= 'F');
}

inline uint8_t hexValue(char c)
{
    if (c >= '0' && c <= '9')
        return static_cast<uint8_t>(c - '0');
    if (c >= 'a' && c <= 'f')
        return static_cast<uint8_t>(c - 'a' + 10);
    return static_cast<uint8_t>(c - 'A' + 10);
}

// Compare exactly 32 bytes without data-dependent early exit.
inline bool constantTimeDigestEqual(const uint8_t left[32], const uint8_t right[32])
{
    if (!left || !right)
        return false;

    uint8_t difference = 0;
    for (size_t i = 0; i < 32; ++i)
        difference |= static_cast<uint8_t>(left[i] ^ right[i]);
    return difference == 0;
}

inline bool hasSha256SidecarName(const char* packageFilename,
                                 const char* sidecarFilename)
{
    if (!packageFilename || !sidecarFilename)
        return false;

    const char* package = basename(packageFilename);
    const char* sidecar = basename(sidecarFilename);
    const size_t packageLength = strlen(package);
    const size_t suffixLength = sizeof(SHA256_SIDECAR_SUFFIX) - 1;
    const size_t sidecarLength = strlen(sidecar);

    if (packageLength == 0 || sidecarLength != packageLength + suffixLength)
        return false;
    return memcmp(sidecar, package, packageLength) == 0 &&
           memcmp(sidecar + packageLength, SHA256_SIDECAR_SUFFIX,
                  suffixLength) == 0;
}

inline bool isAsciiWhitespace(char c)
{
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' ||
           c == '\v' || c == '\f';
}

// Parse sha256sum-compatible sidecar bytes. The payload is: 64 hex digits,
// horizontal whitespace, optional '*', exact package basename, then whitespace
// only (normally zero bytes or a final LF).
inline bool parseSha256Sidecar(const char* packageFilename,
                               const char* sidecarFilename,
                               const char* sidecarData,
                               size_t sidecarLength,
                               uint8_t expectedDigest[32])
{
    if (!sidecarData || !expectedDigest ||
        !hasSha256SidecarName(packageFilename, sidecarFilename) ||
        sidecarLength < 64)
        return false;

    for (size_t i = 0; i < 32; ++i) {
        const char high = sidecarData[i * 2];
        const char low = sidecarData[i * 2 + 1];
        if (!isHexDigit(high) || !isHexDigit(low))
            return false;
        expectedDigest[i] = static_cast<uint8_t>((hexValue(high) << 4) |
                                                  hexValue(low));
    }

    size_t position = 64;
    const size_t separatorStart = position;
    while (position < sidecarLength &&
           (sidecarData[position] == ' ' || sidecarData[position] == '\t'))
        ++position;
    if (position == separatorStart)
        return false;

    if (position < sidecarLength && sidecarData[position] == '*')
        ++position;

    const char* package = basename(packageFilename);
    const size_t packageLength = strlen(package);
    if (sidecarLength - position < packageLength ||
        memcmp(sidecarData + position, package, packageLength) != 0)
        return false;
    position += packageLength;

    while (position < sidecarLength && isAsciiWhitespace(sidecarData[position]))
        ++position;
    if (position != sidecarLength)
        return false;

    return true;
}

inline bool validateSha256Sidecar(const char* packageFilename,
                                  const char* sidecarFilename,
                                  const char* sidecarData,
                                  size_t sidecarLength,
                                  const uint8_t calculatedDigest[32])
{
    uint8_t expectedDigest[32];
    return calculatedDigest &&
           parseSha256Sidecar(packageFilename, sidecarFilename, sidecarData,
                              sidecarLength, expectedDigest) &&
           constantTimeDigestEqual(expectedDigest, calculatedDigest);
}

} // namespace ota
} // namespace freematics

#endif // FREEMATICS_OTA_SHA256_SIDECAR_H
