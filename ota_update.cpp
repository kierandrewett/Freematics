#include "ota_update.h"

#include "FreematicsCellTLS.h"
#include "config.h"
#include "ota_release_policy.h"
#include "ota_sha256_sidecar.h"
#include "ota_stage_policy.h"
#include "ota_version_policy.h"

#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <sdkconfig.h>
#include <mbedtls/sha256.h>
#include <nvs.h>
#include <stdio.h>
#include <string.h>

namespace {

const char kReleaseHost[] = "github.com";
const char kPackageName[] = "freematics-model-b.bin";
const char kSidecarName[] = "freematics-model-b.bin.sha256sum";
const char kNvsNamespace[] = "storage";
const char kInstalledDigestKey[] = "ota_sha";
const char kPendingDigestKey[] = "ota_pending";
const size_t kSidecarCapacity = 256;
const uint8_t kSha256Bytes = 32;
const uint8_t kMaxRedirects = 3;
const uint32_t kSidecarMaximum = 255;
const unsigned kHttpTimeoutMs = 15UL * 60UL * 1000UL;
// The release version is parsed from the downloaded image before it can be
// staged. Keep a matching marker in every image so OTA can reject downgrades.
const char kFirmwareReleaseMarker[] =
    "FREEMATICS_RELEASE_VERSION=" FREEMATICS_RELEASE;

bool cancelled(const volatile bool* requested)
{
  return requested && *requested;
}

bool continueRequested(void* context)
{
  return !cancelled(static_cast<const volatile bool*>(context));
}

struct AssetWriter {
  CellHTTP& cell;
  const volatile bool* cancelRequested;
  CellHTTPStreamResponse response;
  char host[128];
  char path[2048];
  uint8_t redirects;
};

bool getAsset(AssetWriter& asset, const char* filename, uint32_t maxLength,
              CellHTTPBodyWriter writer, void* writerContext)
{
  strcpy(asset.host, kReleaseHost);
  const int length = snprintf(asset.path, sizeof(asset.path),
      "/kierandrewett/Freematics/releases/latest/download/%s", filename);
  if (length <= 0 || (size_t)length >= sizeof(asset.path)) return false;
  asset.redirects = 0;

  for (;;) {
    if (cancelled(asset.cancelRequested)) return false;
    if (!asset.cell.getStream(asset.host, 443, asset.path, maxLength,
            writer, writerContext, &asset.response, kHttpTimeoutMs,
            continueRequested, (void*)asset.cancelRequested)) return false;
    if (asset.response.status == 200) {
      asset.cell.close();
      return true;
    }
    if (asset.response.status != 301 && asset.response.status != 302 &&
        asset.response.status != 303 && asset.response.status != 307 &&
        asset.response.status != 308) {
      Serial.printf("[OTA] Release asset HTTP status %u\n", asset.response.status);
      asset.cell.close();
      return false;
    }
    if (asset.redirects++ >= kMaxRedirects || !asset.response.location[0]) {
      asset.cell.close();
      return false;
    }
    char nextHost[sizeof(asset.host)];
    char nextPath[sizeof(asset.path)];
    if (!freematics::ota::parseHttpsReleaseLocation(asset.response.location,
            nextHost, sizeof(nextHost), nextPath, sizeof(nextPath))) {
      Serial.println("[OTA] Refusing untrusted release redirect");
      asset.cell.close();
      return false;
    }
    asset.cell.close();
    strcpy(asset.host, nextHost);
    strcpy(asset.path, nextPath);
  }
}

struct SidecarBuffer {
  char data[kSidecarCapacity];
  size_t length;
};

bool writeSidecar(void* context, const unsigned char* bytes, size_t length)
{
  SidecarBuffer* buffer = static_cast<SidecarBuffer*>(context);
  if (!buffer || length > sizeof(buffer->data) - buffer->length) return false;
  memcpy(buffer->data + buffer->length, bytes, length);
  buffer->length += length;
  return true;
}

struct FirmwareWriter {
  const esp_partition_t* partition;
  esp_ota_handle_t handle;
  bool started;
  bool failed;
  uint32_t bytesWritten;
  const volatile bool* cancelRequested;
  mbedtls_sha256_context sha;
  freematics::ota::FirmwareVersionScanner versionScanner;
};

bool writeFirmware(void* context, const unsigned char* bytes, size_t length)
{
  FirmwareWriter* writer = static_cast<FirmwareWriter*>(context);
  if (!writer || writer->failed || cancelled(writer->cancelRequested) ||
      length > writer->partition->size - writer->bytesWritten) return false;
  if (!writer->started) {
    if (esp_ota_begin(writer->partition, OTA_SIZE_UNKNOWN, &writer->handle) != ESP_OK) {
      writer->failed = true;
      return false;
    }
    writer->started = true;
  }
  if (esp_ota_write(writer->handle, bytes, length) != ESP_OK ||
      mbedtls_sha256_update_ret(&writer->sha, bytes, length) != 0) {
    writer->failed = true;
    return false;
  }
  writer->versionScanner.update(bytes, length);
  writer->bytesWritten += (uint32_t)length;
  return true;
}

bool readDigest(const char* key, uint8_t digest[kSha256Bytes])
{
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READONLY, &handle) != ESP_OK) return false;
  size_t length = kSha256Bytes;
  const esp_err_t result = nvs_get_blob(handle, key, digest, &length);
  nvs_close(handle);
  return result == ESP_OK && length == kSha256Bytes;
}

bool writeDigest(const char* key, const uint8_t digest[kSha256Bytes])
{
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READWRITE, &handle) != ESP_OK) return false;
  const esp_err_t result = nvs_set_blob(handle, key, digest, kSha256Bytes);
  const esp_err_t committed = result == ESP_OK ? nvs_commit(handle) : result;
  nvs_close(handle);
  return committed == ESP_OK;
}

void eraseDigest(const char* key)
{
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READWRITE, &handle) != ESP_OK) return;
  const esp_err_t erased = nvs_erase_key(handle, key);
  if (erased == ESP_OK) nvs_commit(handle);
  nvs_close(handle);
}

struct StageOperations {
  FirmwareWriter& firmware;
  const uint8_t* digest;
  const esp_partition_t* partition;

  void abortImage() { esp_ota_abort(firmware.handle); }
  bool finishImage() { return esp_ota_end(firmware.handle) == ESP_OK; }
  bool writePendingDigest() {
    return writeDigest(kPendingDigestKey, digest);
  }
  void erasePendingDigest() { eraseDigest(kPendingDigestKey); }
  bool selectBootPartition() {
    return esp_ota_set_boot_partition(partition) == ESP_OK;
  }
};

} // namespace

#if ENABLE_OTA
// Defer Arduino's default unconditional rollback confirmation until the logger
// and motion sensor have initialized and the application can run its own gate.
extern "C" bool verifyRollbackLater()
{
  return true;
}
#endif

OtaAttemptResult performOtaReleaseUpdate(CellHTTP& cell, const volatile bool* cancelRequested)
{
#if STORAGE != STORAGE_SD
  (void)cell;
  (void)cancelRequested;
  Serial.println("[OTA] Refusing update: healthy SD journal is required");
  return OTA_ATTEMPT_FAILED;
#endif
#if !CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  (void)cell;
  (void)cancelRequested;
  Serial.println("[OTA] Refusing update: bootloader rollback is not enabled");
  return OTA_ATTEMPT_FAILED;
#endif
  if (!cell.deviceName() || !strstr(cell.deviceName(), "7670")) {
    Serial.println("[OTA] Refusing update: strict SIM7670 TLS transport required");
    return OTA_ATTEMPT_FAILED;
  }
  Serial.printf("[OTA] Installed release identity: %s\n", kFirmwareReleaseMarker);
  if (cancelled(cancelRequested)) return OTA_ATTEMPT_CANCELLED;

  SidecarBuffer sidecar = {};
  AssetWriter asset = {cell, cancelRequested, {}, {}, {}, 0};
  if (!getAsset(asset, kSidecarName, kSidecarMaximum, writeSidecar, &sidecar)) {
    return cancelled(cancelRequested) ? OTA_ATTEMPT_CANCELLED : OTA_ATTEMPT_FAILED;
  }
  if (!sidecar.length || sidecar.length >= sizeof(sidecar.data)) return OTA_ATTEMPT_FAILED;

  uint8_t releaseDigest[kSha256Bytes];
  if (!freematics::ota::parseSha256Sidecar(kPackageName, kSidecarName,
          sidecar.data, sidecar.length, releaseDigest)) {
    Serial.println("[OTA] Release checksum sidecar is malformed");
    return OTA_ATTEMPT_FAILED;
  }
  uint8_t previousDigest[kSha256Bytes];
  if (readDigest(kInstalledDigestKey, previousDigest) &&
      freematics::ota::constantTimeDigestEqual(previousDigest, releaseDigest)) {
    Serial.println("[OTA] Latest release already installed");
    return OTA_ATTEMPT_NO_UPDATE;
  }

  const esp_partition_t* updatePartition = esp_ota_get_next_update_partition(nullptr);
  if (!updatePartition || updatePartition == esp_ota_get_running_partition()) {
    Serial.println("[OTA] No inactive application slot available");
    return OTA_ATTEMPT_FAILED;
  }
  FirmwareWriter firmware = {};
  firmware.partition = updatePartition;
  firmware.cancelRequested = cancelRequested;
  mbedtls_sha256_init(&firmware.sha);
  if (mbedtls_sha256_starts_ret(&firmware.sha, 0) != 0) {
    mbedtls_sha256_free(&firmware.sha);
    return OTA_ATTEMPT_FAILED;
  }

  const bool downloaded = getAsset(asset, kPackageName, updatePartition->size,
                                     writeFirmware, &firmware);
  CellHTTPStreamResponse binaryResponse = asset.response;
  uint8_t calculatedDigest[kSha256Bytes] = {};
  const bool hashed = firmware.started &&
      mbedtls_sha256_finish_ret(&firmware.sha, calculatedDigest) == 0;
  mbedtls_sha256_free(&firmware.sha);

  if (!downloaded || !hashed || firmware.failed || !firmware.bytesWritten ||
      firmware.bytesWritten != binaryResponse.contentLength || cancelled(cancelRequested)) {
    if (firmware.started) esp_ota_abort(firmware.handle);
    return cancelled(cancelRequested) ? OTA_ATTEMPT_CANCELLED : OTA_ATTEMPT_FAILED;
  }

  if (!freematics::ota::validateSha256Sidecar(kPackageName, kSidecarName,
          sidecar.data, sidecar.length, calculatedDigest)) {
    Serial.println("[OTA] Package SHA-256 does not match release sidecar");
    esp_ota_abort(firmware.handle);
    return OTA_ATTEMPT_FAILED;
  }
  char candidateVersion[freematics::ota::FirmwareVersionScanner::kVersionCapacity];
  if (!firmware.versionScanner.read(candidateVersion)) {
    Serial.println("[OTA] Package has no valid firmware release version");
    esp_ota_abort(firmware.handle);
    return OTA_ATTEMPT_FAILED;
  }
  if (!freematics::ota::isStrictlyNewerFirmware(candidateVersion,
                                                 FREEMATICS_RELEASE)) {
    Serial.println("[OTA] Latest release is not newer; refusing downgrade or reinstall");
    esp_ota_abort(firmware.handle);
    return OTA_ATTEMPT_NO_UPDATE;
  }
  if (cancelled(cancelRequested)) {
    esp_ota_abort(firmware.handle);
    return OTA_ATTEMPT_CANCELLED;
  }
  StageOperations stage = {firmware, calculatedDigest, updatePartition};
  const freematics::ota::StageResult staged =
      freematics::ota::stageVerifiedImage(stage, cancelRequested);
  if (staged == freematics::ota::kStageCancelled) return OTA_ATTEMPT_CANCELLED;
  if (staged == freematics::ota::kStageImageFinishFailed) {
    Serial.println("[OTA] Image validation failed");
    return OTA_ATTEMPT_FAILED;
  }
  if (staged == freematics::ota::kStagePendingDigestFailed) {
    Serial.println("[OTA] Could not journal pending release identity");
    return OTA_ATTEMPT_FAILED;
  }
  if (staged == freematics::ota::kStageBootSelectionFailed) {
    Serial.println("[OTA] Could not select the inactive image");
    return OTA_ATTEMPT_FAILED;
  }
  Serial.printf("[OTA] Verified %lu bytes; staged inactive slot %s\n",
                (unsigned long)firmware.bytesWritten, updatePartition->label);
  return OTA_ATTEMPT_INSTALLED;
}

bool cancelStagedOtaUpdate()
{
  const esp_partition_t* running = esp_ota_get_running_partition();
  if (!running || esp_ota_set_boot_partition(running) != ESP_OK) {
    Serial.println("[OTA] Could not restore the current boot slot after motion wake");
    return false;
  }
  eraseDigest(kPendingDigestKey);
  Serial.println("[OTA] Staged image cancelled after motion wake");
  return true;
}

bool validatePendingOtaImage(bool storageReady, bool motionSensorReady,
                             bool telemetryCredentialReady)
{
#if ENABLE_OTA && CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  const esp_partition_t* running = esp_ota_get_running_partition();
  esp_ota_img_states_t imageState;
  if (!running || esp_ota_get_state_partition(running, &imageState) != ESP_OK) return true;

  if (imageState == ESP_OTA_IMG_PENDING_VERIFY) {
    uint8_t digest[kSha256Bytes];
    if (!storageReady || !motionSensorReady || !telemetryCredentialReady ||
        !readDigest(kPendingDigestKey, digest)) {
      Serial.println("[OTA] New image failed core-service validation; rolling back");
      esp_ota_mark_app_invalid_rollback_and_reboot();
      return false;
    }
    if (esp_ota_mark_app_valid_cancel_rollback() != ESP_OK) {
      Serial.println("[OTA] Could not confirm new image; rolling back");
      esp_ota_mark_app_invalid_rollback_and_reboot();
      return false;
    }
    if (!writeDigest(kInstalledDigestKey, digest)) {
      Serial.println("[OTA] Image confirmed, but release identity was not saved");
      eraseDigest(kPendingDigestKey);
      return true;
    }
    eraseDigest(kPendingDigestKey);
    Serial.println("[OTA] New image passed core-service validation");
  } else if (imageState == ESP_OTA_IMG_ABORTED) {
    eraseDigest(kPendingDigestKey);
  }
#elif ENABLE_OTA
  (void)storageReady;
  (void)motionSensorReady;
  (void)telemetryCredentialReady;
  Serial.println("[OTA] Bootloader rollback support is not enabled");
#else
  (void)storageReady;
  (void)motionSensorReady;
  (void)telemetryCredentialReady;
#endif
  return true;
}
