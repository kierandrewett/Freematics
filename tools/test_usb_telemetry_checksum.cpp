#include "../usbtelemetry_checksum.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

int main()
{
  static const uint8_t vector[] = "123456789";
  assert(usbTelemetryCrc32(vector, sizeof(vector) - 1) == 0xCBF43926UL);

  static const uint8_t envelope[] = "@FT2,42,1200,1,1790966400000,0,0C|ABC#0:1200";
  uint8_t altered[sizeof(envelope) - 1];
  memcpy(altered, envelope, sizeof(altered));
  const uint32_t original = usbTelemetryCrc32(envelope, sizeof(envelope) - 1);
  altered[12] ^= 1; // capture-time metadata is protected too
  assert(usbTelemetryCrc32(altered, sizeof(altered)) != original);

  puts("USB telemetry CRC-32: standard vector and envelope metadata passed");
  return 0;
}
