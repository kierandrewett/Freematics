#pragma once

#include <stdint.h>
#include "FreematicsNetwork.h"

enum OtaAttemptResult {
  OTA_ATTEMPT_FAILED,
  OTA_ATTEMPT_NO_UPDATE,
  OTA_ATTEMPT_READY,
  OTA_ATTEMPT_CANCELLED
};

// OTA uses only the strict ESP32-TLS-over-cellular transport on SIM7670.
// The caller owns the parked-state policy and modem lifecycle.
OtaAttemptResult performOtaReleaseUpdate(CellHTTP& cell, const volatile bool* cancelRequested);
bool prepareVerifiedOtaUpdate();
bool activateVerifiedOtaUpdate();
void discardVerifiedOtaUpdate();

// Call after core services have initialized. Arduino's default rollback hook
// validates too early for this firmware, so ota_update.cpp defers confirmation.
bool validatePendingOtaImage(bool storageReady, bool motionSensorReady,
                             bool telemetryCredentialReady);
