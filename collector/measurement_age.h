#ifndef FREEMATICS_MEASUREMENT_AGE_H
#define FREEMATICS_MEASUREMENT_AGE_H

#include <limits.h>
#include <stdint.h>

static inline unsigned int telemetryAddAgeMs(unsigned int first, unsigned int second)
{
	return first > UINT_MAX - second ? UINT_MAX : first + second;
}

static inline unsigned int telemetryElapsedDeviceMs(uint32_t current, uint32_t then)
{
	if (!then) return 0;
	uint32_t elapsed = current - then;
	/* Accept normal uint32 rollover; reject timestamps from an older session. */
	return (int32_t)elapsed >= 0 ? elapsed : 0;
}

static inline unsigned int telemetryMeasurementAgeMs(uint32_t currentDeviceTick,
	uint32_t measurementDeviceTick, unsigned int collectorReceiptAgeMs)
{
	return telemetryAddAgeMs(telemetryElapsedDeviceMs(currentDeviceTick, measurementDeviceTick),
		collectorReceiptAgeMs);
}

#endif
