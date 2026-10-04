#ifndef TELEMETRY_ENDPOINT_POLICY_H
#define TELEMETRY_ENDPOINT_POLICY_H

#include <stddef.h>
#include <stdint.h>

namespace freematics {
namespace endpoint {

inline bool isAlphaNumeric(char value)
{
  return (value >= 'a' && value <= 'z') ||
         (value >= 'A' && value <= 'Z') ||
         (value >= '0' && value <= '9');
}

inline bool isHexDigit(char value)
{
  return (value >= '0' && value <= '9') ||
         (value >= 'a' && value <= 'f') ||
         (value >= 'A' && value <= 'F');
}

inline bool validHost(const char* host)
{
  if (!host || !host[0]) return false;
  size_t labelLength = 0;
  char previous = 0;
  for (size_t i = 0; host[i]; ++i) {
    const char value = host[i];
    if (i >= 253) return false;
    if (value == '.') {
      if (!labelLength || labelLength > 63 || previous == '-') return false;
      labelLength = 0;
    } else if (isAlphaNumeric(value) || value == '-') {
      if (!labelLength && value == '-') return false;
      ++labelLength;
      if (labelLength > 63) return false;
    } else {
      return false;
    }
    previous = value;
  }
  return labelLength && labelLength <= 63 && previous != '-';
}

inline bool validPath(const char* path)
{
  if (!path || path[0] != '/') return false;
  for (size_t i = 0; path[i]; ++i) {
    const unsigned char value = static_cast<unsigned char>(path[i]);
    if (i >= 255 || value <= 0x20 || value >= 0x7f || value == '\\' || value == '#') {
      return false;
    }
    if (value == '%') {
      if (!path[i + 1] || !path[i + 2] ||
          !isHexDigit(path[i + 1]) || !isHexDigit(path[i + 2])) {
        return false;
      }
      i += 2;
    }
  }
  return true;
}

inline bool copyString(char* destination, size_t capacity, const char* source)
{
  if (!destination || !capacity || !source) return false;
  size_t length = 0;
  while (source[length] && length < capacity) ++length;
  if (length == capacity) return false;
  for (size_t i = 0; i <= length; ++i) destination[i] = source[i];
  return true;
}

enum ValueSource {
  kInvalidValue,
  kStoredValue,
  kBuildSeedValue,
};

enum ConfigMarkerAction {
  kRequirePrivateMigration,
  kSeedPrivateMigrationMarker,
  kUseMigratedPrivateConfig,
};

inline ConfigMarkerAction resolveConfigMarker(bool markerFound,
                                              uint8_t markerVersion,
                                              bool allowMarkerSeed,
                                              bool dependentConfigReady)
{
  if (markerFound) {
    return markerVersion == 1 && dependentConfigReady
        ? kUseMigratedPrivateConfig : kRequirePrivateMigration;
  }
  return allowMarkerSeed && dependentConfigReady
      ? kSeedPrivateMigrationMarker : kRequirePrivateMigration;
}

inline ValueSource resolveStoredOrSeed(const char* storedValue,
                                       bool storedValueFound,
                                       const char* buildValue,
                                       bool allowBuildSeed,
                                       char* destination,
                                       size_t capacity)
{
  if (!destination || !capacity) return kInvalidValue;
  destination[0] = 0;
  if (storedValueFound && !storedValue) return kInvalidValue;
  if (!storedValueFound && !allowBuildSeed) return kInvalidValue;
  const char* candidate = storedValueFound ? storedValue : buildValue;
  if (!copyString(destination, capacity, candidate)) return kInvalidValue;
  return storedValueFound ? kStoredValue : kBuildSeedValue;
}

inline ValueSource resolveValue(const char* storedValue, bool storedValueFound,
                                const char* buildValue, bool allowBuildSeed,
                                bool isHost, char* destination, size_t capacity)
{
  const ValueSource source = resolveStoredOrSeed(storedValue, storedValueFound,
      buildValue, allowBuildSeed, destination, capacity);
  if (source == kInvalidValue) return source;
  const bool valid = isHost ? validHost(destination) : validPath(destination);
  if (!valid) {
    destination[0] = 0;
    return kInvalidValue;
  }
  return source;
}

} // namespace endpoint
} // namespace freematics

#endif
