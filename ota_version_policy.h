#ifndef FREEMATICS_OTA_VERSION_POLICY_H
#define FREEMATICS_OTA_VERSION_POLICY_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace freematics {
namespace ota {

struct FirmwareVersion {
  uint32_t major;
  uint32_t minor;
  uint32_t patch;
};

inline bool parseFirmwareVersion(const char* text, FirmwareVersion* version) {
  if (!text || !version) return false;
  uint32_t parts[3] = {};
  const char* cursor = text;
  for (size_t part = 0; part < 3; ++part) {
    if (*cursor < '0' || *cursor > '9') return false;
    if (*cursor == '0' && cursor[1] >= '0' && cursor[1] <= '9') return false;
    uint32_t value = 0;
    do {
      const uint32_t digit = static_cast<uint32_t>(*cursor - '0');
      if (value > (UINT32_MAX - digit) / 10) return false;
      value = value * 10 + digit;
      ++cursor;
    } while (*cursor >= '0' && *cursor <= '9');
    parts[part] = value;
    if (part < 2) {
      if (*cursor++ != '.') return false;
    } else if (*cursor != '\0') {
      return false;
    }
  }
  version->major = parts[0];
  version->minor = parts[1];
  version->patch = parts[2];
  return true;
}

inline bool isStrictlyNewerFirmware(const char* candidate, const char* current) {
  FirmwareVersion next = {};
  FirmwareVersion installed = {};
  if (!parseFirmwareVersion(candidate, &next) ||
      !parseFirmwareVersion(current, &installed)) return false;
  if (next.major != installed.major) return next.major > installed.major;
  if (next.minor != installed.minor) return next.minor > installed.minor;
  return next.patch > installed.patch;
}

class FirmwareVersionScanner {
 public:
  static const size_t kVersionCapacity = 32;

  FirmwareVersionScanner() : m_match(0), m_found(false), m_terminated(false),
                             m_valid(false), m_length(0) {
    m_version[0] = '\0';
  }

  void update(const unsigned char* bytes, size_t length) {
    if (!bytes || m_valid) return;
    for (size_t i = 0; i < length; ++i) consume(static_cast<char>(bytes[i]));
  }

  bool read(char version[kVersionCapacity]) const {
    FirmwareVersion parsed = {};
    if (!version || !m_valid || !m_terminated ||
        !parseFirmwareVersion(m_version, &parsed)) return false;
    memcpy(version, m_version, m_length + 1);
    return true;
  }

 private:
  void consume(char byte) {
    if (m_found) {
      if (m_terminated) return;
      if (byte == '\0') {
        m_terminated = true;
        FirmwareVersion parsed = {};
        if (parseFirmwareVersion(m_version, &parsed)) {
          m_valid = true;
        } else {
          // The scanner's own marker string can precede the version-bearing
          // marker in the image. Ignore malformed occurrences and keep
          // searching for a complete, valid release identity.
          m_found = false;
          m_terminated = false;
          m_match = 0;
          m_length = 0;
          m_version[0] = '\0';
        }
        return;
      }
      if (m_length + 1 >= sizeof(m_version)) {
        m_found = false;
        m_terminated = false;
        m_match = byte == releaseMarker()[0] ? 1 : 0;
        m_length = 0;
        m_version[0] = '\0';
        return;
      }
      m_version[m_length++] = byte;
      m_version[m_length] = '\0';
      return;
    }

    const char* marker = releaseMarker();
    if (byte == marker[m_match]) {
      ++m_match;
      if (marker[m_match] == '\0') m_found = true;
    } else {
      // The marker contains no repeated leading prefix, so only a new 'F'
      // can begin a valid overlapping match.
      m_match = byte == marker[0] ? 1 : 0;
    }
  }

  static const char* releaseMarker() {
    static const char marker[] = "FREEMATICS_RELEASE_VERSION=";
    return marker;
  }

  size_t m_match;
  bool m_found;
  bool m_terminated;
  bool m_valid;
  size_t m_length;
  char m_version[kVersionCapacity];
};

class FirmwareSourceCommitScanner {
 public:
  static const size_t kCommitCapacity = 41;

  FirmwareSourceCommitScanner() : m_match(0), m_found(false),
      m_invalid(false), m_length(0), m_count(0) {
    m_candidate[0] = '\0';
    m_commit[0] = '\0';
  }

  void update(const unsigned char* bytes, size_t length) {
    if (!bytes || m_invalid) return;
    for (size_t i = 0; i < length; ++i) consume(static_cast<char>(bytes[i]));
  }

  bool read(char commit[kCommitCapacity]) const {
    if (!commit || m_invalid || m_count != 1) return false;
    memcpy(commit, m_commit, kCommitCapacity);
    return true;
  }

 private:
  void consume(char byte) {
    // Keep the search pattern split so it does not add a bare empty
    // FREEMATICS_SOURCE_COMMIT= marker to the binary. The package verifier
    // requires exactly one full marker, containing the embedded commit.
    static const char markerPrefix[] = "FREEMATICS_SOURCE_";
    static const char markerSuffix[] = "COMMIT=";
    static const size_t markerPrefixLength = sizeof(markerPrefix) - 1;
    static const size_t markerLength = markerPrefixLength + sizeof(markerSuffix) - 1;
    if (m_found) {
      if (byte == '\0') {
        m_found = false;
        m_match = 0;
        if (m_length != 40) { m_length = 0; m_candidate[0] = '\0'; return; }
        for (size_t i = 0; i < 40; ++i) {
          if (!((m_candidate[i] >= '0' && m_candidate[i] <= '9') ||
                (m_candidate[i] >= 'a' && m_candidate[i] <= 'f'))) {
            m_length = 0;
            m_candidate[0] = '\0';
            return;
          }
        }
        if (++m_count != 1) m_invalid = true;
        else memcpy(m_commit, m_candidate, kCommitCapacity);
        return;
      }
      if (m_length >= 40) {
        m_found = false;
        m_match = 0;
        m_length = 0;
        m_candidate[0] = '\0';
        return;
      }
      m_candidate[m_length++] = byte;
      m_candidate[m_length] = '\0';
      return;
    }
    const char expected = m_match < markerPrefixLength
        ? markerPrefix[m_match]
        : markerSuffix[m_match - markerPrefixLength];
    if (byte == expected) {
      if (++m_match == markerLength) {
        m_found = true;
        m_length = 0;
        m_candidate[0] = '\0';
      }
    } else {
      m_match = byte == markerPrefix[0] ? 1 : 0;
    }
  }

  size_t m_match;
  bool m_found;
  bool m_invalid;
  size_t m_length;
  size_t m_count;
  char m_candidate[kCommitCapacity];
  char m_commit[kCommitCapacity];
};

} // namespace ota
} // namespace freematics

#endif
