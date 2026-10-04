#ifndef FREEMATICS_OTA_BOOT_IDENTITY_H
#define FREEMATICS_OTA_BOOT_IDENTITY_H

#include "ota_sha256_sidecar.h"

#include <stddef.h>
#include <stdint.h>

namespace freematics {
namespace ota {

inline bool matchesTargetPartition(size_t imageSize, size_t partitionSize,
                                   uint32_t expectedAddress,
                                   uint8_t expectedType,
                                   uint8_t expectedSubtype,
                                   uint32_t actualAddress,
                                   uint8_t actualType,
                                   uint8_t actualSubtype)
{
  return imageSize > 0 && imageSize <= partitionSize &&
         expectedAddress == actualAddress && expectedType == actualType &&
         expectedSubtype == actualSubtype;
}

inline bool matchesRunningImage(size_t imageSize, size_t partitionSize,
                                const uint8_t expectedDigest[32],
                                const uint8_t actualDigest[32])
{
  return imageSize > 0 && imageSize <= partitionSize &&
         constantTimeDigestEqual(expectedDigest, actualDigest);
}

} // namespace ota
} // namespace freematics

#endif
