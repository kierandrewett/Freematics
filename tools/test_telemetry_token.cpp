#include "../telemetry_token.h"

#include <assert.h>
#include <string.h>

int main()
{
  const char compiled[] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
  const char stored[] = "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789";
  char output[65] = {};
  bool persist = false;

  assert(freematics::security::isTelemetryToken(compiled));
  assert(!freematics::security::isTelemetryToken(""));
  assert(!freematics::security::isTelemetryToken("0123456789abcdef"));
  char invalid[66];
  memset(invalid, 'g', 64);
  invalid[64] = '\0';
  assert(!freematics::security::isTelemetryToken(invalid));

  assert(freematics::security::resolveTelemetryToken(
      nullptr, false, compiled, output, sizeof(output), &persist));
  assert(persist);
  assert(strcmp(output, compiled) == 0);

  persist = true;
  assert(freematics::security::resolveTelemetryToken(
      stored, true, compiled, output, sizeof(output), &persist));
  assert(!persist);
  assert(strcmp(output, stored) == 0);

  assert(!freematics::security::resolveTelemetryToken(
      "bad", true, compiled, output, sizeof(output), &persist));
  assert(!persist);
  assert(!freematics::security::resolveTelemetryToken(
      nullptr, false, "", output, sizeof(output), &persist));
  assert(!freematics::security::resolveTelemetryToken(
      compiled, true, compiled, output, 64, &persist));
  return 0;
}
