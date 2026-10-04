#include "../lib/FreematicsPlus/mode02_response.h"

#include <assert.h>
#include <string.h>

int main()
{
  char bytes[16] = {};
  assert(freematics::mode02::parseFrame0(
      "020C00\r7E8 04 42 0C 00 01 2C\r>", 0x0C, 2,
      bytes, sizeof(bytes)));
  assert(strcmp(bytes, "01 2C") == 0);

  assert(freematics::mode02::parseFrame0(
      "42 42 00 00 01 2C 00 00", 0x42, 4,
      bytes, sizeof(bytes)));
  assert(strcmp(bytes, "00 01 2C 00") == 0);

  assert(!freematics::mode02::parseFrame0(
      "42 0C 01 01 2C", 0x0C, 2, bytes, sizeof(bytes)));
  assert(!freematics::mode02::parseFrame0(
      "42 0C 00 01", 0x0C, 2, bytes, sizeof(bytes)));
  assert(!freematics::mode02::parseFrame0(
      "NO DATA", 0x0C, 2, bytes, sizeof(bytes)));
  assert(!freematics::mode02::parseFrame0(
      "42 0C 00 ZZ 2C", 0x0C, 2, bytes, sizeof(bytes)));
  assert(!freematics::mode02::parseFrame0(
      "TEXT42 0C 00 01 2C", 0x0C, 2, bytes, sizeof(bytes)));
  assert(!freematics::mode02::parseFrame0(
      "42 0C 00 01 2C", 0x0C, 2, bytes, 5));
  return 0;
}
