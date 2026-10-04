#include "ota_update.h"

#include "FreematicsCellTLS.h"
#include "config.h"
#include "ota_boot_identity.h"
#include "ota_boot_policy.h"
#include "ota_release_policy.h"
#include "ota_sha256_sidecar.h"
#include "ota_stage_policy.h"
#include "ota_version_policy.h"

#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <sdkconfig.h>
#include <mbedtls/sha256.h>
#include <nvs.h>
#include <freertos/FreeRTOS.h>
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
const uint32_t kPendingIdentityMagic = 0x46544F41; // "FTOA"
const uint8_t kMaxRedirects = 3;
const uint32_t kSidecarMaximum = 255;
const size_t kPartitionHashChunkBytes = 1024;
const unsigned kHttpTimeoutMs = 15UL * 60UL * 1000UL;
// The release version is parsed from the downloaded image before it can be
// staged. Keep a matching marker in every image so OTA can reject downgrades.
const char kFirmwareReleaseMarker[] =
    "FREEMATICS_RELEASE_VERSION=" FREEMATICS_RELEASE;

struct PendingOtaIdentity {
  uint32_t magic;
  uint32_t imageSize;
  uint32_t partitionAddress;
  uint8_t partitionType;
  uint8_t partitionSubtype;
  uint8_t reserved[2];
  uint8_t digest[kSha256Bytes];
};

static_assert(sizeof(PendingOtaIdentity) == 48,
              "pending OTA identity must remain a fixed 48-byte NVS blob");

PendingOtaIdentity stagedCandidate = {};
bool stagedCandidateAvailable = false;
portMUX_TYPE stagedCandidateMux = portMUX_INITIALIZER_UNLOCKED;

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

bool readBlob(const char* key, void* value, size_t valueLength)
{
  if (!key || !value || !valueLength) return false;
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READONLY, &handle) != ESP_OK) return false;
  size_t length = valueLength;
  const esp_err_t result = nvs_get_blob(handle, key, value, &length);
  nvs_close(handle);
  return result == ESP_OK && length == valueLength;
}

bool writeBlob(const char* key, const void* value, size_t valueLength)
{
  if (!key || !value || !valueLength) return false;
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READWRITE, &handle) != ESP_OK) return false;
  const esp_err_t result = nvs_set_blob(handle, key, value, valueLength);
  const esp_err_t committed = result == ESP_OK ? nvs_commit(handle) : result;
  nvs_close(handle);
  return committed == ESP_OK;
}

bool readDigest(const char* key, uint8_t digest[kSha256Bytes])
{
  return readBlob(key, digest, kSha256Bytes);
}

bool writeDigest(const char* key, const uint8_t digest[kSha256Bytes])
{
  return writeBlob(key, digest, kSha256Bytes);
}

bool eraseDigest(const char* key)
{
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READWRITE, &handle) != ESP_OK) return false;
  const esp_err_t erased = nvs_erase_key(handle, key);
  const esp_err_t committed = erased == ESP_ERR_NVS_NOT_FOUND
      ? ESP_OK : (erased == ESP_OK ? nvs_commit(handle) : erased);
  nvs_close(handle);
  return committed == ESP_OK;
}

PendingOtaIdentity makeIdentity(const esp_partition_t* partition,
                                uint32_t imageSize,
                                const uint8_t digest[kSha256Bytes])
{
  PendingOtaIdentity identity = {};
  if (!partition || !imageSize || !digest) return identity;
  identity.magic = kPendingIdentityMagic;
  identity.imageSize = imageSize;
  identity.partitionAddress = partition ? partition->address : 0;
  identity.partitionType = partition ? partition->type : 0;
  identity.partitionSubtype = partition ? partition->subtype : 0;
  memcpy(identity.digest, digest, sizeof(identity.digest));
  return identity;
}

bool sameIdentity(const PendingOtaIdentity& left,
                  const PendingOtaIdentity& right)
{
  return left.magic == kPendingIdentityMagic &&
      right.magic == kPendingIdentityMagic &&
      left.imageSize == right.imageSize &&
      left.partitionAddress == right.partitionAddress &&
      left.partitionType == right.partitionType &&
      left.partitionSubtype == right.partitionSubtype &&
      freematics::ota::constantTimeDigestEqual(left.digest, right.digest);
}

bool identityMatchesPartition(const PendingOtaIdentity& identity,
                              const esp_partition_t* partition)
{
  return partition && identity.magic == kPendingIdentityMagic &&
      freematics::ota::matchesTargetPartition(identity.imageSize,
          partition->size, identity.partitionAddress, identity.partitionType,
          identity.partitionSubtype, partition->address, partition->type,
          partition->subtype);
}

bool writePendingIdentity(const PendingOtaIdentity& identity)
{
  if (!identity.imageSize || identity.magic != kPendingIdentityMagic ||
      !writeBlob(kPendingDigestKey, &identity, sizeof(identity))) return false;
  PendingOtaIdentity readback = {};
  return readBlob(kPendingDigestKey, &readback, sizeof(readback)) &&
         sameIdentity(identity, readback);
}

bool readPendingIdentity(PendingOtaIdentity* identity)
{
  if (!identity) return false;
  return readBlob(kPendingDigestKey, identity, sizeof(*identity)) &&
         identity->magic == kPendingIdentityMagic && identity->imageSize;
}

bool pendingIdentityKeyAbsent()
{
  nvs_handle_t handle;
  if (nvs_open(kNvsNamespace, NVS_READONLY, &handle) != ESP_OK) return false;
  size_t length = 0;
  const esp_err_t result = nvs_get_blob(handle, kPendingDigestKey, nullptr, &length);
  nvs_close(handle);
  return result == ESP_ERR_NVS_NOT_FOUND;
}

void clearStagedCandidate()
{
  portENTER_CRITICAL(&stagedCandidateMux);
  memset(&stagedCandidate, 0, sizeof(stagedCandidate));
  stagedCandidateAvailable = false;
  portEXIT_CRITICAL(&stagedCandidateMux);
}

bool copyStagedCandidate(PendingOtaIdentity* identity)
{
  if (!identity) return false;
  portENTER_CRITICAL(&stagedCandidateMux);
  const bool available = stagedCandidateAvailable;
  if (available) *identity = stagedCandidate;
  portEXIT_CRITICAL(&stagedCandidateMux);
  return available;
}

void saveStagedCandidate(const PendingOtaIdentity& identity)
{
  portENTER_CRITICAL(&stagedCandidateMux);
  stagedCandidate = identity;
  stagedCandidateAvailable = true;
  portEXIT_CRITICAL(&stagedCandidateMux);
}

bool hashPartitionImage(const esp_partition_t* partition, uint32_t imageSize,
                        uint8_t digest[kSha256Bytes])
{
  if (!partition || !imageSize || imageSize > partition->size || !digest) {
    return false;
  }

  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  if (mbedtls_sha256_starts_ret(&sha, 0) != 0) {
    mbedtls_sha256_free(&sha);
    return false;
  }

  uint8_t buffer[kPartitionHashChunkBytes];
  uint32_t offset = 0;
  bool okay = true;
  while (offset < imageSize) {
    const size_t chunk = imageSize - offset < sizeof(buffer)
        ? imageSize - offset : sizeof(buffer);
    if (esp_partition_read(partition, offset, buffer, chunk) != ESP_OK ||
        mbedtls_sha256_update_ret(&sha, buffer, chunk) != 0) {
      okay = false;
      break;
    }
    offset += (uint32_t)chunk;
    delay(0);
  }
  if (okay && mbedtls_sha256_finish_ret(&sha, digest) != 0) okay = false;
  mbedtls_sha256_free(&sha);
  return okay;
}

struct StageOperations {
  FirmwareWriter& firmware;

  void abortImage() { esp_ota_abort(firmware.handle); }
  bool finishImage() { return esp_ota_end(firmware.handle) == ESP_OK; }
};

struct BootSelectionOperations {
  const esp_partition_t* partition;

  bool selectBootPartition() {
    return esp_ota_set_boot_partition(partition) == ESP_OK;
  }
  void erasePendingDigest() { (void)eraseDigest(kPendingDigestKey); }
};

struct PendingBootOperations {
  const PendingOtaIdentity& identity;

  bool confirmBoot() {
    return esp_ota_mark_app_valid_cancel_rollback() == ESP_OK;
  }
  bool saveInstalledIdentity() {
    return writeDigest(kInstalledDigestKey, identity.digest);
  }
  void erasePendingIdentity() { (void)eraseDigest(kPendingDigestKey); }
  bool rollback(freematics::ota::BootFailureReason reason) {
    if (reason == freematics::ota::kBootCoreValidationFailed) {
      Serial.println("[OTA] New image failed identity or core-service validation; rolling back");
    } else {
      Serial.println("[OTA] Could not confirm new image; rolling back");
    }
    const esp_err_t result = esp_ota_mark_app_invalid_rollback_and_reboot();
    if (result != ESP_OK) {
      Serial.printf("[OTA] CRITICAL: rollback request failed (%d); refusing normal startup\n",
                    (int)result);
    }
    return result == ESP_OK;
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
  clearStagedCandidate();
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
  StageOperations stage = {firmware};
  const freematics::ota::StageResult staged =
      freematics::ota::stageVerifiedImage(stage, cancelRequested);
  if (staged == freematics::ota::kStageCancelled) return OTA_ATTEMPT_CANCELLED;
  if (staged == freematics::ota::kStageImageFinishFailed) {
    Serial.println("[OTA] Image validation failed");
    return OTA_ATTEMPT_FAILED;
  }
  if (staged != freematics::ota::kStageReadyForActivation) {
    Serial.println("[OTA] Verified image was not ready for final activation");
    return OTA_ATTEMPT_FAILED;
  }
  saveStagedCandidate(makeIdentity(updatePartition, firmware.bytesWritten,
                                   calculatedDigest));
  Serial.printf("[OTA] Verified %lu bytes in inactive slot %s; boot slot unchanged\n",
                (unsigned long)firmware.bytesWritten, updatePartition->label);
  return OTA_ATTEMPT_READY;
}

bool prepareVerifiedOtaUpdate()
{
  PendingOtaIdentity candidate = {};
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* update = esp_ota_get_next_update_partition(nullptr);
  uint8_t actualDigest[kSha256Bytes];
  if (!copyStagedCandidate(&candidate) || !running || !update ||
      update == running || !identityMatchesPartition(candidate, update) ||
      !hashPartitionImage(update, candidate.imageSize, actualDigest) ||
      !freematics::ota::constantTimeDigestEqual(candidate.digest, actualDigest)) {
    Serial.println("[OTA] Cannot prepare activation: inactive image identity or contents do not match");
    return false;
  }
  if (!writePendingIdentity(candidate)) {
    const bool erased = eraseDigest(kPendingDigestKey);
    Serial.printf("[OTA] Could not persist/read back pending image identity%s\n",
                  erased ? "" : "; stale marker cleanup also failed");
    return false;
  }
  Serial.printf("[OTA] Exact inactive image verified and activation identity journaled (%s)\n",
                update->label);
  return true;
}

bool activateVerifiedOtaUpdate()
{
  PendingOtaIdentity candidate = {};
  PendingOtaIdentity pending = {};
  const esp_partition_t* running = esp_ota_get_running_partition();
  const esp_partition_t* update = esp_ota_get_next_update_partition(nullptr);
  if (!copyStagedCandidate(&candidate) || !readPendingIdentity(&pending) ||
      !running || !update || update == running ||
      !identityMatchesPartition(candidate, update) ||
      !identityMatchesPartition(pending, update) ||
      !sameIdentity(candidate, pending)) {
    if (!eraseDigest(kPendingDigestKey)) {
      Serial.println("[OTA] Could not clear invalid pending image identity");
    }
    Serial.println("[OTA] Cannot activate: verified inactive image identity is unavailable");
    return false;
  }

  // prepareVerifiedOtaUpdate() already hashed this exact partition. Keep the
  // final vehicle gate directly adjacent to this short identity check and
  // boot-slot write; first-boot validation hashes the running image again.
  BootSelectionOperations operations = {update};
  const freematics::ota::StageResult result =
      freematics::ota::activateVerifiedImage(operations, nullptr);
  if (result != freematics::ota::kStageInstalled) {
    Serial.println("[OTA] Could not select the verified inactive image");
    clearStagedCandidate();
    return false;
  }
  clearStagedCandidate();
  Serial.printf("[OTA] Selected verified inactive slot %s\n", update->label);
  return true;
}

void discardVerifiedOtaUpdate()
{
  clearStagedCandidate();
  if (!eraseDigest(kPendingDigestKey)) {
    Serial.println("[OTA] Could not clear discarded pending image identity");
  }
}

bool validatePendingOtaImage(bool storageReady, bool motionSensorReady,
                             bool telemetryEndpointReady,
                             bool telemetryCredentialReady)
{
#if ENABLE_OTA && CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
  const esp_partition_t* running = esp_ota_get_running_partition();
  esp_ota_img_states_t imageState;
  if (!running) {
    Serial.println("[OTA] CRITICAL: running partition unavailable; refusing normal startup");
    return false;
  }
  const esp_err_t stateResult = esp_ota_get_state_partition(running, &imageState);
  if (stateResult != ESP_OK) {
    PendingOtaIdentity orphaned = {};
    if (readPendingIdentity(&orphaned)) {
      if (orphaned.partitionAddress == running->address) {
        Serial.println("[OTA] CRITICAL: running image has a pending OTA identity but no boot state");
        return false;
      }
      if (!eraseDigest(kPendingDigestKey)) {
        Serial.println("[OTA] CRITICAL: cannot clear orphaned OTA identity after boot-state error");
        return false;
      }
    } else if (!pendingIdentityKeyAbsent()) {
      Serial.println("[OTA] CRITICAL: pending OTA identity state is unreadable; refusing normal startup");
      return false;
    }
    return true;
  }

  if (imageState == ESP_OTA_IMG_PENDING_VERIFY) {
    PendingOtaIdentity pending = {};
    uint8_t actualDigest[kSha256Bytes];
    bool identityValid = false;
    if (storageReady && motionSensorReady && telemetryEndpointReady &&
        telemetryCredentialReady) {
      identityValid = readPendingIdentity(&pending) &&
          identityMatchesPartition(pending, running) &&
          hashPartitionImage(running, pending.imageSize, actualDigest) &&
          freematics::ota::matchesRunningImage(pending.imageSize, running->size,
                                                pending.digest, actualDigest);
    }

    PendingBootOperations operations = {pending};
    const freematics::ota::BootResult result =
        freematics::ota::validateAndAcceptPendingImage(
            storageReady, motionSensorReady, telemetryEndpointReady,
            telemetryCredentialReady,
            identityValid, operations);
    if (result == freematics::ota::kBootRolledBack) return false;
    if (result == freematics::ota::kBootRollbackFailed) {
      Serial.println("[OTA] CRITICAL: rollback failed; refusing normal startup");
      return false;
    }
    if (result == freematics::ota::kBootAcceptedIdentityNotSaved) {
      Serial.println("[OTA] Image confirmed, but release identity was not saved");
      return true;
    }
    Serial.println("[OTA] New image passed core-service validation");
  } else if (imageState == ESP_OTA_IMG_ABORTED) {
    if (!eraseDigest(kPendingDigestKey)) {
      Serial.println("[OTA] Could not clear aborted pending image identity");
    }
  } else if (!eraseDigest(kPendingDigestKey)) {
    Serial.println("[OTA] Could not clear pending identity outside pending verification");
  }
#elif ENABLE_OTA
  (void)storageReady;
  (void)motionSensorReady;
  (void)telemetryEndpointReady;
  (void)telemetryCredentialReady;
  Serial.println("[OTA] Bootloader rollback support is not enabled");
#else
  (void)storageReady;
  (void)motionSensorReady;
  (void)telemetryEndpointReady;
  (void)telemetryCredentialReady;
#endif
  return true;
}
