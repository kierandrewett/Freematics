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

	/* Prometheus sample time is derived from capture UTC, not collector receipt.
	 * A held measurement is projected backward by its device-tick age. */
	uint64_t captureUtcMs = 0;
	assert(telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		5000U, 4250U, 1, 1791030012345ULL, &captureUtcMs));
	assert(captureUtcMs == 1791030011595ULL);
	assert(telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5500U,
		5000U, 5000U, 1, 1791030015345ULL, &captureUtcMs));
	assert(captureUtcMs == 1791030012345ULL);
	assert(!telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		5000U, 4250U, 0, 1791030012345ULL, &captureUtcMs));
	assert(!telemetryCaptureUtcMsForMeasurement(1791030012U, 1000U, 5000U,
		5000U, 4250U, 1, 1791030012345ULL, &captureUtcMs));
	assert(!telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		4000U, 4250U, 1, 1791030012345ULL, &captureUtcMs));
	assert(!telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		5000U, 5250U, 1, 1791030012345ULL, &captureUtcMs));
	/* A device clock far ahead of the collector must not poison Prometheus with
	 * samples that remain invisible until wall time catches up. */
	assert(telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		5000U, 5000U, 1, 1791029712345ULL, &captureUtcMs));
	assert(!telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		5000U, 5000U, 1, 1791029712344ULL, &captureUtcMs));
	assert(!telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 5000U,
		5000U, 5000U, 1, 0, &captureUtcMs));
	/* Device tick rollover is preserved on the same monotonic clock. */
	assert(telemetryCaptureUtcMsForMeasurement(1791030012U, 345U, 0x20U,
		0xfffffff0U, 0xffffffe0U, 1, 1791030012345ULL, &captureUtcMs));
	assert(captureUtcMs == 1791030012329ULL);
	return 0;
}
