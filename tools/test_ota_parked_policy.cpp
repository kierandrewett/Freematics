#include "../ota_parked_policy.h"

#include <assert.h>
#include <stdio.h>

typedef OTAParkedPolicy Policy;

static Policy::Signal signal(float value, uint32_t sampledAt, uint32_t maxAge = 1000) {
  Policy::Signal result = {true, true, sampledAt, maxAge, value};
  return result;
}

static Policy::Observation parked(uint32_t sampledAt) {
  Policy::Observation result = {
      false, false, true, true, true,
      signal(0.0f, sampledAt), signal(0.0f, sampledAt), signal(12.5f, sampledAt),
  };
  return result;
}

static void quietSamples(Policy& policy, uint32_t start, uint32_t end) {
  for (uint32_t at = start; (uint32_t)(at - start) <= (uint32_t)(end - start); at += 250) {
    policy.observeMotionSample(at, true, false);
    policy.observeSupplySample(at, true, 12.5f);
  }
}

static void testRequiresAnHourOfContinuousSensorEvidence() {
  Policy policy;
  policy.beginBoot(100);
  Policy::Observation state = parked(100 + Policy::kRequiredQuietMs);
  assert(policy.observe(100 + Policy::kRequiredQuietMs, state) == Policy::kMotionUnavailable);

  quietSamples(policy, 100, 100 + Policy::kRequiredQuietMs - 1);
  state = parked(100 + Policy::kRequiredQuietMs - 1);
  assert(policy.observe(100 + Policy::kRequiredQuietMs - 1, state) == Policy::kQuietPeriod);
  policy.observeMotionSample(100 + Policy::kRequiredQuietMs, true, false);
  state = parked(100 + Policy::kRequiredQuietMs);
  assert(policy.observe(100 + Policy::kRequiredQuietMs, state) == Policy::kEligible);
}

static void testMotionInvalidSampleAndLongGapRestartTimer() {
  Policy policy;
  policy.beginBoot(0);
  quietSamples(policy, 0, Policy::kRequiredQuietMs);
  uint32_t at = Policy::kRequiredQuietMs + 250;
  policy.observeMotionSample(at, true, true);
  assert(policy.quietDurationMs(at) == 0);
  quietSamples(policy, at + 250, at + 250 + Policy::kRequiredQuietMs);
  assert(policy.observe(at + 250 + Policy::kRequiredQuietMs,
                        parked(at + 250 + Policy::kRequiredQuietMs)) == Policy::kEligible);

  at += Policy::kRequiredQuietMs + 500;
  policy.observeMotionSample(at, false, false);
  assert(!policy.motionProofCurrent(at));
  assert(policy.observe(at, parked(at)) == Policy::kMotionUnavailable);
  policy.observeMotionSample(at + 250, true, false);
  assert(policy.quietDurationMs(at + 250) == 0);
  at += 250 + Policy::kMotionSampleMaxGapMs + 1;
  policy.observeMotionSample(at, true, false);
  assert(policy.quietDurationMs(at) == 0);
  assert(policy.observe(at, parked(at)) == Policy::kQuietPeriod);
}

static void testActivityAndRebootResetTimer() {
  Policy policy;
  policy.beginBoot(1000);
  quietSamples(policy, 1000, 2000);
  Policy::Observation state = parked(2000);
  state.confirmedMotionWake = true;
  assert(policy.observe(2000, state) == Policy::kMotionWake);
  policy.observeMotionSample(2000, true, true);
  quietSamples(policy, 2250, 2000 + Policy::kRequiredQuietMs - 1);
  state = parked(2000 + Policy::kRequiredQuietMs - 1);
  assert(policy.observe(2000 + Policy::kRequiredQuietMs - 1, state) == Policy::kQuietPeriod);
  policy.observeMotionSample(2000 + Policy::kRequiredQuietMs, true, false);
  state = parked(2000 + Policy::kRequiredQuietMs);
  state.activity = true;
  assert(policy.observe(2000 + Policy::kRequiredQuietMs, state) == Policy::kActivity);

  policy.beginBoot(50);
  state = parked(50 + Policy::kRequiredQuietMs);
  assert(policy.observe(50 + Policy::kRequiredQuietMs, state) == Policy::kMotionUnavailable);
}

static void testVehicleSignalsAndReadinessRemainFailClosed() {
  assert(Policy::vehicleSupplyPlausible(12.2f));
  assert(!Policy::vehicleSupplyPlausible(12.19f));
  assert(Policy::vehicleSupplyPlausible(12.9f));
  assert(!Policy::vehicleSupplyPlausible(12.91f));
  assert(!Policy::vehicleSupplyPlausible(13.19f));
  assert(!Policy::vehicleSupplyPlausible(5.99f));
  assert(!Policy::vehicleSupplyPlausible(13.2f));
  const uint32_t start = 5000;
  const uint32_t checkAt = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  Policy::Observation state = parked(checkAt);
  state.speedKph.supported = false;
  assert(policy.observe(checkAt, state) == Policy::kSpeedUnavailable);
  state = parked(checkAt);
  state.rpm.valid = false;
  assert(policy.observe(checkAt, state) == Policy::kRpmUnavailable);
  state = parked(checkAt);
  state.modelBSupplyVolts.sampledAtMs = start;
  assert(policy.observe(checkAt, state) == Policy::kSupplyUnavailable);
  state = parked(checkAt);
  state.durableStorageHealthy = false;
  assert(policy.observe(checkAt, state) == Policy::kStorageUnavailable);
  state = parked(checkAt);
  state.telemetryCredentialPersisted = false;
  assert(policy.observe(checkAt, state) == Policy::kCredentialUnavailable);
  state = parked(checkAt);
  state.telemetryEndpointConfigured = false;
  assert(policy.observe(checkAt, state) == Policy::kEndpointUnavailable);
  state = parked(checkAt);
  state.speedKph.value = 1.0f;
  assert(policy.observe(checkAt, state) == Policy::kSpeedNotZero);
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  state = parked(checkAt);
  state.rpm.value = 1.0f;
  assert(policy.observe(checkAt, state) == Policy::kRpmNotZero);
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  state = parked(checkAt);
  state.modelBSupplyVolts.value = 13.2f;
  assert(policy.observe(checkAt, state) == Policy::kSupplyNotResting);
  state = parked(checkAt);
  state.modelBSupplyVolts.value = 12.91f;
  assert(policy.observe(checkAt, state) == Policy::kSupplyNotResting);
  state = parked(checkAt);
  state.modelBSupplyVolts.value = 0.0f;
  assert(policy.observe(checkAt, state) == Policy::kSupplyUnavailable);
  state = parked(checkAt);
  state.modelBSupplyVolts.value = 12.19f;
  assert(policy.observe(checkAt, state) == Policy::kSupplyTooLow);
  state = parked(checkAt);
  assert(policy.observe(checkAt, state) == Policy::kEligible);
}

static void testParkedSignalsAreRecheckedAfterDownload() {
  const uint32_t start = 9000;
  const uint32_t checkAt = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  Policy::Observation state = parked(checkAt);
  assert(policy.observe(checkAt, state) == Policy::kEligible);

  const uint32_t completedAt = checkAt + 250;
  policy.observeMotionSample(completedAt, true, false);
  state = parked(completedAt);
  state.rpm.value = 850.0f;
  assert(policy.observe(completedAt, state) == Policy::kRpmNotZero);

  const uint32_t supplyCheckAt = completedAt + Policy::kRequiredQuietMs;
  policy.beginBoot(completedAt);
  quietSamples(policy, completedAt, supplyCheckAt);
  policy.observeMotionSample(supplyCheckAt, true, false);
  state = parked(supplyCheckAt);
  state.modelBSupplyVolts.value = 14.1f;
  assert(policy.observe(supplyCheckAt, state) == Policy::kSupplyNotResting);
}

static void testObservedEngineOrVehicleActivityRestartsQuietPeriod() {
  const uint32_t start = 30000;
  const uint32_t checkAt = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);

  Policy::Observation state = parked(checkAt);
  state.speedKph.value = 1.0f;
  assert(policy.observe(checkAt, state) == Policy::kSpeedNotZero);
  assert(!policy.motionProofCurrent(checkAt));
  assert(policy.quietDurationMs(checkAt) == 0);

  const uint32_t restartedAt = checkAt + 1000;
  quietSamples(policy, restartedAt, restartedAt + Policy::kRequiredQuietMs - 1);
  state = parked(restartedAt + Policy::kRequiredQuietMs - 1);
  assert(policy.observe(restartedAt + Policy::kRequiredQuietMs - 1, state) ==
         Policy::kQuietPeriod);
  policy.observeMotionSample(restartedAt + Policy::kRequiredQuietMs, true, false);

  const uint32_t rpmAt = restartedAt + Policy::kRequiredQuietMs;
  state = parked(rpmAt);
  state.rpm.value = 750.0f;
  assert(policy.observe(rpmAt, state) == Policy::kRpmNotZero);
  assert(!policy.motionProofCurrent(rpmAt));
  assert(policy.quietDurationMs(rpmAt) == 0);
}

static void testMotionDuringDownloadInvalidatesTheQuietProof() {
  const uint32_t start = 12000;
  const uint32_t checkAt = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  assert(policy.quietPeriodComplete(checkAt));

  policy.observeMotionSample(checkAt + 250, true, true);
  assert(policy.motionProofCurrent(checkAt + 250));
  assert(!policy.quietPeriodComplete(checkAt + 250));
  assert(policy.quietDurationMs(checkAt + 250) == 0);

  policy.observeMotionSample(checkAt + 500, true, false);
  assert(!policy.quietPeriodComplete(checkAt + 500));
}

static void testSupplyEvidenceMustRemainContinuousAndResting() {
  const uint32_t start = 42000;
  const uint32_t checkAt = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  assert(policy.quietPeriodComplete(checkAt));

  // Charging/ignition voltage invalidates the full-hour proof even if the
  // supply returns to a resting level on the next poll.
  policy.observeMotionSample(checkAt + 250, true, false);
  policy.observeSupplySample(checkAt + 250, true, 13.8f);
  policy.observeMotionSample(checkAt + 500, true, false);
  policy.observeSupplySample(checkAt + 500, true, 12.5f);
  assert(!policy.supplyQuietPeriodComplete(checkAt + 500));
  assert(!policy.quietPeriodComplete(checkAt + 500));
  quietSamples(policy, checkAt + 750,
               checkAt + 750 + Policy::kRequiredQuietMs - 250);
  assert(policy.quietPeriodComplete(
      checkAt + 750 + Policy::kRequiredQuietMs - 250));

  const uint32_t invalidAt = checkAt + Policy::kRequiredQuietMs + 1000;
  policy.observeMotionSample(invalidAt, true, false);
  policy.observeSupplySample(invalidAt, false, 0.0f);
  policy.observeMotionSample(invalidAt + 250, true, false);
  policy.observeSupplySample(invalidAt + 250, true, 12.5f);
  assert(!policy.supplyQuietPeriodComplete(invalidAt + 250));

  // A weak battery is not evidence that the car remained off. It must break
  // the hour, even when voltage later returns to the healthy resting range.
  const uint32_t weakAt = invalidAt + 500;
  policy.observeMotionSample(weakAt, true, false);
  policy.observeSupplySample(weakAt, true, 12.1f);
  assert(!policy.supplyQuietPeriodComplete(weakAt));
  policy.observeMotionSample(weakAt + 250, true, false);
  policy.observeSupplySample(weakAt + 250, true, 12.5f);
  assert(!policy.supplyQuietPeriodComplete(weakAt + 250));
  assert(!policy.quietPeriodComplete(weakAt + 250));
}

static void testSupplyObservationGapInvalidatesParkedProof() {
  const uint32_t start = 70000;
  const uint32_t checkAt = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, checkAt);
  assert(policy.quietPeriodComplete(checkAt));
  const uint32_t afterGap = checkAt + 250 + Policy::kSupplySampleMaxGapMs + 1;
  for (uint32_t at = checkAt + 250; at < afterGap; at += 250)
    policy.observeMotionSample(at, true, false);
  policy.observeSupplySample(afterGap, true, 12.5f);
  assert(!policy.supplyQuietPeriodComplete(afterGap));
  assert(!policy.quietPeriodComplete(afterGap));
}

static void testWrapSafeContinuousQuietTimer() {
  const uint32_t start = UINT32_MAX - 120000UL;
  const uint32_t boundary = start + Policy::kRequiredQuietMs;
  Policy policy;
  policy.beginBoot(start);
  quietSamples(policy, start, boundary);
  assert(policy.observe(boundary, parked(boundary)) == Policy::kEligible);
  assert(policy.quietDurationMs(boundary) == Policy::kRequiredQuietMs);
}

int main() {
  testRequiresAnHourOfContinuousSensorEvidence();
  testMotionInvalidSampleAndLongGapRestartTimer();
  testActivityAndRebootResetTimer();
  testVehicleSignalsAndReadinessRemainFailClosed();
  testParkedSignalsAreRecheckedAfterDownload();
  testObservedEngineOrVehicleActivityRestartsQuietPeriod();
  testMotionDuringDownloadInvalidatesTheQuietProof();
  testSupplyEvidenceMustRemainContinuousAndResting();
  testSupplyObservationGapInvalidatesParkedProof();
  testWrapSafeContinuousQuietTimer();
  puts("OTA parked eligibility: all tests passed");
  return 0;
}
