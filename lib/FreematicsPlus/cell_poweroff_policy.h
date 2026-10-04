#pragma once

namespace freematics {
namespace cell {

enum PowerDownResult {
  kPowerDownNoResponse,
  kPowerDownCommandAccepted,
  kPowerDownCommandLikelyEffective,
  kPowerDownKeyFallback,
  kPowerDownStillResponsive,
};

// A power-key pulse toggles state on SIMCom modules. Never pulse unless an
// AT probe just proved the radio is on; after a failed shutdown command, probe
// again because a lost OK response may mean the modem already powered down.
template <typename Probe, typename Request, typename Fallback>
PowerDownResult powerDownAfterCancellation(Probe responds,
                                          Request requestPowerDown,
                                          Fallback forcePowerKeyOff)
{
  if (!responds()) return kPowerDownNoResponse;
  if (requestPowerDown()) return kPowerDownCommandAccepted;
  if (!responds()) return kPowerDownCommandLikelyEffective;
  return forcePowerKeyOff()
      ? kPowerDownKeyFallback : kPowerDownStillResponsive;
}

} // namespace cell
} // namespace freematics
