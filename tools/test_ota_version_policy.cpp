#include "../ota_version_policy.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

namespace {

void testVersionParsingAndOrdering() {
  freematics::ota::FirmwareVersion version = {};
  assert(freematics::ota::parseFirmwareVersion("1.2.3", &version));
  assert(version.major == 1 && version.minor == 2 && version.patch == 3);
  assert(freematics::ota::isStrictlyNewerFirmware("1.2.4", "1.2.3"));
  assert(freematics::ota::isStrictlyNewerFirmware("1.3.0", "1.2.99"));
  assert(freematics::ota::isStrictlyNewerFirmware("2.0.0", "1.99.99"));
  assert(!freematics::ota::isStrictlyNewerFirmware("1.2.3", "1.2.3"));
  assert(!freematics::ota::isStrictlyNewerFirmware("1.2.2", "1.2.3"));
  assert(!freematics::ota::parseFirmwareVersion("01.2.3", &version));
  assert(!freematics::ota::parseFirmwareVersion("1.2", &version));
  assert(!freematics::ota::parseFirmwareVersion("1.2.3-rc1", &version));
  assert(!freematics::ota::parseFirmwareVersion("4294967296.0.0", &version));
}

void testVersionMarkerAcrossChunks() {
  freematics::ota::FirmwareVersionScanner scanner;
  const unsigned char prefix[] =
      "FREEMATICS_RELEASE_VERSION=\0\xff" "FREEM";
  const unsigned char middle[] = "ATICS_RELEASE_VERSION=1.7.";
  const unsigned char suffix[] = {'4', '\0', 0xff};
  scanner.update(prefix, sizeof(prefix) - 1);
  scanner.update(middle, sizeof(middle) - 1);
  char version[freematics::ota::FirmwareVersionScanner::kVersionCapacity];
  assert(!scanner.read(version));
  scanner.update(suffix, sizeof(suffix));
  assert(scanner.read(version));
  assert(strcmp(version, "1.7.4") == 0);
  assert(freematics::ota::isStrictlyNewerFirmware(version, "1.7.3"));
}

void testMissingAndMalformedMarkersFailClosed() {
  const unsigned char noMarker[] = "random firmware bytes";
  freematics::ota::FirmwareVersionScanner missing;
  missing.update(noMarker, sizeof(noMarker));
  char version[freematics::ota::FirmwareVersionScanner::kVersionCapacity];
  assert(!missing.read(version));

  const unsigned char malformed[] = "FREEMATICS_RELEASE_VERSION=1.7.x\0";
  freematics::ota::FirmwareVersionScanner invalid;
  invalid.update(malformed, sizeof(malformed));
  assert(!invalid.read(version));

  const unsigned char malformedThenValid[] =
      "FREEMATICS_RELEASE_VERSION=\0"
      "FREEMATICS_RELEASE_VERSION=1.7.5\0";
  freematics::ota::FirmwareVersionScanner recovers;
  recovers.update(malformedThenValid, sizeof(malformedThenValid));
  assert(recovers.read(version));
  assert(strcmp(version, "1.7.5") == 0);
}

void testSourceCommitMarkerAcrossChunksAndUniqueness() {
  const char* commit = "0123456789abcdef0123456789abcdef01234567";
  freematics::ota::FirmwareSourceCommitScanner scanner;
  const unsigned char first[] = "prefix FREEMATICS_SOURCE_COMMIT=0123456789abcdef";
  const unsigned char second[] = "0123456789abcdef01234567\0 suffix";
  scanner.update(first, sizeof(first) - 1);
  char found[freematics::ota::FirmwareSourceCommitScanner::kCommitCapacity];
  assert(!scanner.read(found));
  scanner.update(second, sizeof(second) - 1);
  assert(scanner.read(found));
  assert(strcmp(found, commit) == 0);

  const unsigned char markerLiteralThenValid[] =
      "FREEMATICS_SOURCE_COMMIT=\0"
      "FREEMATICS_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567\0"
      "FREEMATICS_SOURCE_COMMIT=\0";
  freematics::ota::FirmwareSourceCommitScanner withCodeMarker;
  withCodeMarker.update(markerLiteralThenValid, sizeof(markerLiteralThenValid) - 1);
  assert(withCodeMarker.read(found));
  assert(strcmp(found, commit) == 0);

  const unsigned char duplicate[] =
      "FREEMATICS_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567\0"
      "FREEMATICS_SOURCE_COMMIT=0123456789abcdef0123456789abcdef01234567\0";
  freematics::ota::FirmwareSourceCommitScanner nonUnique;
  nonUnique.update(duplicate, sizeof(duplicate) - 1);
  assert(!nonUnique.read(found));

  const unsigned char malformed[] = "FREEMATICS_SOURCE_COMMIT=not-a-commit\0";
  freematics::ota::FirmwareSourceCommitScanner invalid;
  invalid.update(malformed, sizeof(malformed) - 1);
  assert(!invalid.read(found));
}

} // namespace

int main() {
  testVersionParsingAndOrdering();
  testVersionMarkerAcrossChunks();
  testMissingAndMalformedMarkersFailClosed();
  testSourceCommitMarkerAcrossChunksAndUniqueness();
  puts("OTA firmware version policy: all tests passed");
  return 0;
}
