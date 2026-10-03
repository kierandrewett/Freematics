#pragma once

#include <stddef.h>

namespace freematics {
namespace security {

static const size_t kTelemetryTokenLength = 64;

inline bool isTelemetryToken(const char* token)
{
  if (!token) return false;
  for (size_t i = 0; i < kTelemetryTokenLength; ++i) {
    const char c = token[i];
    if (c == '\0') return false;
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
          (c >= 'A' && c <= 'F'))) return false;
  }
  return token[kTelemetryTokenLength] == '\0';
}

// An existing NVS value always wins. A malformed existing value fails closed;
// only a genuinely absent key may be seeded from a private migration build.
inline bool resolveTelemetryToken(const char* storedToken, bool storedTokenExists,
                                  const char* compiledToken, char* output,
                                  size_t outputSize, bool* shouldPersist)
{
  if (!output || outputSize < kTelemetryTokenLength + 1 || !shouldPersist) return false;
  *shouldPersist = false;
  const char* selected = storedTokenExists ? storedToken : compiledToken;
  if (!isTelemetryToken(selected)) return false;
  for (size_t i = 0; i <= kTelemetryTokenLength; ++i) output[i] = selected[i];
  *shouldPersist = !storedTokenExists;
  return true;
}

} // namespace security
} // namespace freematics
