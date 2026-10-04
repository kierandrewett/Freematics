#include "../ota_collector_ack.h"

#include <assert.h>
#include <string.h>

int main()
{
  assert(otaCollectorAckMatches("OK 3", 4, 3));
  assert(otaCollectorAckMatches("OK 3\n", 5, 3));
  assert(otaCollectorAckMatches("OK 3\r\n", 6, 3));
  assert(!otaCollectorAckMatches("OK 3junk", 8, 3));
  assert(!otaCollectorAckMatches("OK 3 extra", 10, 3));
  assert(!otaCollectorAckMatches("OK 3\nextra", 10, 3));
  assert(!otaCollectorAckMatches("OK 2", 4, 3));
  assert(!otaCollectorAckMatches("OK 3\0junk", 9, 3));
  assert(!otaCollectorAckMatches("OK +3", 5, 3));
  assert(!otaCollectorAckMatches("OK 03", 5, 3));
  assert(!otaCollectorAckMatches("OK 3", 3, 3));
  assert(!otaCollectorAckMatches("OK 3", 4, 0));
  assert(!otaCollectorAckMatches(0, 4, 3));
  return 0;
}
