#ifndef FREEMATICS_OTA_BOOT_IDENTITY_H
#define FREEMATICS_OTA_BOOT_IDENTITY_H

#include "ota_sha256_sidecar.h"

#include <stddef.h>
#include <stdint.h>

namespace freematics {
namespace ota {

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
