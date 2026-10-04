#include "../lib/FreematicsPlus/cell_poweroff_policy.h"

#include <assert.h>
#include <string.h>

namespace {

struct FakeModem {
  bool firstProbe;
  bool secondProbe;
  bool commandAccepted;
  bool fallbackAccepted;
  unsigned probes;
  unsigned commands;
  unsigned fallbacks;
  char calls[8];
  unsigned callCount;

  bool responds()
  {
    calls[callCount++] = 'P';
    return probes++ == 0 ? firstProbe : secondProbe;
  }

  bool requestPowerDown()
  {
    calls[callCount++] = 'C';
    ++commands;
    return commandAccepted;
  }

  bool forcePowerKeyOff()
  {
    calls[callCount++] = 'K';
    ++fallbacks;
    return fallbackAccepted;
  }
};

freematics::cell::PowerDownResult shutDown(FakeModem& modem)
{
  return freematics::cell::powerDownAfterCancellation(
      [&modem]() { return modem.responds(); },
      [&modem]() { return modem.requestPowerDown(); },
      [&modem]() { return modem.forcePowerKeyOff(); });
}

void testAlreadyOffModemNeverReceivesPowerKeyPulse()
{
  FakeModem modem = {false, false, false, true, 0, 0, 0, {}, 0};
  assert(shutDown(modem) ==
         freematics::cell::kPowerDownNoResponse);
  assert(modem.commands == 0);
  assert(modem.fallbacks == 0);
  assert(modem.callCount == 1 && modem.calls[0] == 'P');
}

void testResponsiveModemUsesCommandBeforeFallback()
{
  FakeModem modem = {true, true, false, true, 0, 0, 0, {}, 0};
  assert(shutDown(modem) ==
         freematics::cell::kPowerDownKeyFallback);
  assert(modem.commands == 1);
  assert(modem.fallbacks == 1);
  assert(modem.callCount == 4);
  assert(memcmp(modem.calls, "PCPK", 4) == 0);
}

void testAcceptedShutdownCommandDoesNotPulse()
{
  FakeModem modem = {true, false, true, true, 0, 0, 0, {}, 0};
  assert(shutDown(modem) ==
         freematics::cell::kPowerDownCommandAccepted);
  assert(modem.commands == 1);
  assert(modem.fallbacks == 0);
  assert(modem.callCount == 2);
  assert(memcmp(modem.calls, "PC", 2) == 0);
}

void testUnresponsiveAfterCommandIsNotPulsed()
{
  FakeModem modem = {true, false, false, true, 0, 0, 0, {}, 0};
  assert(shutDown(modem) ==
         freematics::cell::kPowerDownCommandLikelyEffective);
  assert(modem.commands == 1);
  assert(modem.fallbacks == 0);
  assert(modem.callCount == 3);
  assert(memcmp(modem.calls, "PCP", 3) == 0);
}

} // namespace

int main()
{
  testAlreadyOffModemNeverReceivesPowerKeyPulse();
  testResponsiveModemUsesCommandBeforeFallback();
  testAcceptedShutdownCommandDoesNotPulse();
  testUnresponsiveAfterCommandIsNotPulsed();
  return 0;
}
