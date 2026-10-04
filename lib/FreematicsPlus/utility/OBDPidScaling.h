#ifndef FREEMATICS_OBD_PID_SCALING_H
#define FREEMATICS_OBD_PID_SCALING_H

#include <stdint.h>

namespace freematics {
namespace obd {

// SAE Mode 01 PID 59 is encoded in 10 kPa per bit.
inline float absoluteFuelRailPressureKpa(uint16_t raw)
{
    return static_cast<float>(raw) * 10.0f;
}

} // namespace obd
} // namespace freematics

#endif // FREEMATICS_OBD_PID_SCALING_H
