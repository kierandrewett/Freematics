#include <assert.h>
#include "measurement_age.h"

int main(void)
{
	/* Same cached voltage gets older from device time and collector receipt age. */
	assert(telemetryMeasurementAgeMs(1500, 1000, 200) == 700);
	assert(telemetryMeasurementAgeMs(1600, 1000, 300) == 900);

	/* A new voltage acquisition timestamp resets its device-acquisition age. */
	assert(telemetryMeasurementAgeMs(1600, 1600, 300) == 300);

	/* The helper retains the collector's rollover and saturation semantics. */
	assert(telemetryMeasurementAgeMs(0x20, 0xfffffff0U, 10) == 58);
	assert(telemetryMeasurementAgeMs(100, 200, 10) == 10);
	assert(telemetryMeasurementAgeMs(200, 100, UINT_MAX) == UINT_MAX);
	return 0;
}
