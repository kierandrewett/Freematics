#include "../ota_boot_identity.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

namespace {

void testExactExpectedImageMatches()
{
  uint8_t expected[32];
  uint8_t actual[32];
  memset(expected, 0x5a, sizeof(expected));
  memcpy(actual, expected, sizeof(actual));
  assert(freematics::ota::matchesRunningImage(700000, 800000, expected, actual));
}

void testRejectsDigestMismatch()
{
  uint8_t expected[32];
  uint8_t actual[32];
  memset(expected, 0x5a, sizeof(expected));
  memcpy(actual, expected, sizeof(actual));
  actual[17] ^= 1;
  assert(!freematics::ota::matchesRunningImage(700000, 800000, expected, actual));
}

void testRejectsInvalidImageSizes()
{
  uint8_t digest[32] = {};
  assert(!freematics::ota::matchesRunningImage(0, 800000, digest, digest));
  assert(!freematics::ota::matchesRunningImage(800001, 800000, digest, digest));
}

} // namespace

int main()
{
  testExactExpectedImageMatches();
  testRejectsDigestMismatch();
  testRejectsInvalidImageSizes();
  puts("OTA first-boot image identity: all tests passed");
  return 0;
}
