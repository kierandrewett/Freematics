#ifndef FREEMATICS_MEASUREMENT_AGE_H
#define FREEMATICS_MEASUREMENT_AGE_H

#include <limits.h>
#include <stdint.h>

#define TELEMETRY_CAPTURE_UTC_MAX_FUTURE_SKEW_MS 300000ULL

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

/* Convert a measurement tick to trusted UTC only when it can be anchored to a
 * valid capture UTC pair from the same device-tick timeline. Never substitute
 * collector receipt time for a missing or invalid device clock. */
static inline int telemetryCaptureUtcMsForMeasurement(uint32_t captureUtcSeconds,
	uint32_t captureUtcMilliseconds, uint32_t currentDeviceTick,
	uint32_t captureDeviceTick, uint32_t measurementDeviceTick,
	int captureUtcValid, uint64_t collectorNowUtcMs, uint64_t* measurementUtcMs)
{
	if (!captureUtcValid || !measurementUtcMs || captureUtcSeconds < 1704067200U ||
		captureUtcMilliseconds >= 1000U || !captureDeviceTick || !measurementDeviceTick ||
		(int32_t)(currentDeviceTick - captureDeviceTick) < 0 ||
		(int32_t)(captureDeviceTick - measurementDeviceTick) < 0) return 0;
	const uint32_t measurementAgeMs = captureDeviceTick - measurementDeviceTick;
	const uint64_t captureTimeMs = (uint64_t)captureUtcSeconds * 1000ULL + captureUtcMilliseconds;
	if (captureTimeMs < measurementAgeMs || !collectorNowUtcMs ||
		(captureTimeMs > collectorNowUtcMs &&
		 captureTimeMs - collectorNowUtcMs > TELEMETRY_CAPTURE_UTC_MAX_FUTURE_SKEW_MS)) return 0;
	*measurementUtcMs = captureTimeMs - measurementAgeMs;
	return 1;
}

#endif
