#include "../lib/FreematicsPlus/utility/OBDPidScaling.h"

#include <assert.h>
#include <stdio.h>

int main()
{
    assert(freematics::obd::absoluteFuelRailPressureKpa(0x0000) == 0.0f);
    assert(freematics::obd::absoluteFuelRailPressureKpa(0x0123) == 2910.0f);
    assert(freematics::obd::absoluteFuelRailPressureKpa(0xFFFF) == 655350.0f);
    puts("OBD PID scaling: all tests passed");
    return 0;
}
