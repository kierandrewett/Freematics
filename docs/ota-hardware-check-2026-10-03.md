# OTA indoor hardware check — 2026-10-03

## Board and image

- The running firmware identified hardware type 14, ICM-42627 motion sensor,
  8 MB PSRAM, and 16 MB flash, matching the Model B profile used here.
- ESP32 probe: ESP32-D0WDQ6 revision 1.1. The probe and subsequent firmware
  upload reset the board; neither command erased the SD card.
- Source commit: `ff430f45764463d386024c0329b77e4203f11b92` plus uncommitted
  working-tree changes.
- First private migration image SHA-256:
  `0b974e5d316b504e190e8e0650561e12033c94135b8226c2055c0b65ae7bc32a`.
- Current OTA development image SHA-256:
  `1e8fd0754a6c09ac399901d061d93a8105e0c55fecbf4352420c0af4dae5561a`.
  Both local OTA images embed the configured telemetry credential and are not
  release artifacts. PlatformIO verified the current upload digest; runtime
  build ID is `ff430f457644-dirty`.
- The initial indoor boot mounted the physical card on attempt 1 at 1 MHz SPI
  in 23 ms, with 86,147 pending journal bytes. After a later warm reset during
  development, the current image failed all three mount attempts with FatFS
  `FR_NOT_READY`; the SD core then reported `sdSelectCard(): Select Failed`
  after its 500 ms ready wait. A further in-session remount failed the same
  way. This is direct evidence of an intermittent card-response/initialization
  fault, not a demonstrated journal write failure. It does not distinguish a
  latched card state after warm reset from card contact, supply, or hardware
  failure.
- Rebuilt both firmware profiles from committed source `c10c2bf78c9c`:
  production `esp32dev` image SHA-256
  `a45c072cd5fdcc106c17bff7e207f5cea1d873030d6e5f66a90f0ec7755d7aec`, and
  OTA-enabled local migration/test image SHA-256
  `ec1e88cd92441d2e8e59d2ec1f479a7a11599c8857e553978c1216df1f3eaa0d`.
  Both report build ID `c10c2bf78c9c`. The OTA test image contains the private
  telemetry credential and remains local; neither rebuilt image was flashed
  during this pass. The board's serial port is currently held by the dashboard,
  and its SD journal is unhealthy.
- Built a separate tokenless OTA release candidate from committed source
  `dac18da40ec3`, with the production server/APN configuration validated but no
  telemetry token embedded. Its Model B image SHA-256 is
  `e765a4729ce3eb0da4fdaebb249b17cef91916425e6fae80fbbed790b38d45a9`; the
  matching `freematics-model-b.bin.sha256sum` sidecar verifies successfully.
  The binary carries the `FREEMATICS_OTA_RELEASE_BUILD=1` marker. It is staged
  locally only and has not been published to GitHub or exercised on hardware.
- Added a fail-closed OTA downgrade gate in signed source commit
  `fa911573f5b8`: the candidate image must embed a valid, strictly newer
  `major.minor.patch` firmware version. The clean, tokenless `1.0.1` candidate
  built from this commit has SHA-256
  `699093718c933b46ab5233a23aea707e4e42bd54c279357db7f49e04d080bd21`; its
  `.sha256sum` sidecar verifies with `sha256sum -c`, and the packager confirms
  the OTA-release, token-absent, and version markers. It is staged locally
  under `.pio/ota-release-fa91157-v1.0.1/`; it has not been published or
  flashed. The production `esp32dev` build from the same source also succeeds
  with OTA still disabled by default; its image SHA-256 is
  `a9526bf6efd4b3463be5e84e57cc3dc16c9844e268dca0fbc8dfb1b06d29429b`.
- With the SD journal unavailable, an 8-second read-only USB measurement
  produced 31 valid @FT1 frames at a 250 ms median/p95 cadence, one corrupt
  record, and zero USB drops or device restarts. The durable-journal health
  field stayed 0; missed readings rose from 199 to 229; the eight-slot
  handoff stayed empty. The vehicle/ECU was disconnected, so this verifies
  live transport and fail-closed recording only—not vehicle PID values.

## Follow-up — 2026-10-04

- Built a fresh tokenless OTA `1.0.3` image from signed source commit
  `1a6bb6f59e1699edabddc9698a7a50cbc3945da7` after adding first-boot flash
  read-back verification. Its SHA-256 is
  `28fba7c884ed98d7d427d1f57056208424432b14c9c541f81ee027657fe87ac6`; the
  matching `.sha256sum` sidecar verifies, and the credential-aware packager
  accepts it. The artifact remains local under
  `.pio/ota-release-1a6bb6f-v1.0.3/`; it has not been published or flashed.
- Built a separate private OTA-enabled `1.0.0` USB bootstrap from the same
  commit using the ignored production configuration. It contains the telemetry
  credential needed to seed NVS on first boot and must never be published; it
  remains local at `.pio/build/esp32dev-ota-test/firmware.bin` and has not been
  flashed. The configured ESP32 bootloader enables application rollback, and
  the selected partition table provides two OTA application slots. This is
  build-configuration evidence only, not a current hardware readback.

## Scope and remaining validation

This confirms board identity, local image installation, application boot, and
live USB continuity while the SD path is down. The later mount failures mean
the SD journal is not currently verified healthy; do not rely on local
recording until a full power-cycle check restores it. The vehicle is
disconnected indoors, so fresh zero-speed/RPM and vehicle-supply checks cannot
be made; the 60-minute parked gate, cellular release download, motion
cancellation, pending-image acceptance, and automatic rollback remain
untested on hardware. Do not publish or distribute the flashed images: they
embed the private telemetry credential.
