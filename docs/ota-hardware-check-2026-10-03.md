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
- A separate private OTA-enabled `1.0.0` USB bootstrap had been built from the
  same commit using ignored production configuration. It contained the
  telemetry credential needed to seed NVS and was never flashed; its build
  path has since been overwritten by the tokenless `1.0.4` candidate below.
  The configured ESP32 bootloader enables application rollback, and the
  selected partition table provides two OTA application slots. This is
  build-configuration evidence only, not a current hardware readback.
- From source commit `2edf1f6`, both the regular production environment and
  tokenless OTA-enabled environment build successfully. The production image
  is private at `.pio/build/esp32dev/firmware.bin` and unflashed; it embeds the
  telemetry credential. No boot ID or installed-image checksum is claimed.
- After first-boot acceptance was made host-testable in commit `2edf1f6`, built
  a fresh tokenless OTA `1.0.4` image from that commit. The Model B image SHA-256
  is `3c417545bee1f7e2cdeb67caffb90556aa74f02016afcdbd3fe8a590dc78ac1b`;
  the sidecar verifies, and the credential-aware packager accepted the binary
  against the current ignored `.env`, process environment, and
  `local_config.h`. It is staged locally under
  `.pio/ota-release-2edf1f6-v1.0.4/`; it has not been published or flashed.
- Current USB inventory contains a CH340 serial adapter, not the Model B's
  CP210x identity. No Freematics board was detected, so there was no serial
  open, reset, or flash. The public Freematics releases endpoint currently has
  no release assets.
- Audited the six local `.pio/ota-release*` binaries against configured
  credential values without displaying them: no byte matches, and all six
  checksum sidecars verify. The oldest unversioned `.pio/ota-release` image
  lacks the required release markers and is not eligible for publication.

## Follow-up — 2026-10-04 (PID compatibility build)

- Firmware commit `a6df7a330e78` corrects Mode 01 PID `0x59` absolute fuel-rail
  pressure to the standard 10 kPa/bit resolution. The regular production build
  succeeds; its private image SHA-256 is
  `a567225246499c7a856eca525690bac60f52af2822a54ad1f4b52e0b85159f60`, and
  its boot build ID is `a6df7a330e78`.
- Built an OTA-enabled, tokenless `1.0.5` candidate from the same source
  commit. Its local image SHA-256 is
  `29fa3eccffe81166721520e7154950cdc4890973362ed970c2e84bac22ed2c51`; the
  checksum sidecar verifies and the credential-aware packager accepts it.
  It is staged under `.pio/ota-release-a6df7a3-v1.0.5/`, not published or
  flashed.
- The connected USB inventory still shows the CH340 generic adapter, not a
  Model B. The production image remains unflashed; no boot-log build ID or
  live-car verification is claimed.
- Added a clearly separated `esp32dev-ota-production` bootstrap target because
  the ordinary production target keeps OTA disabled. Its private image built
  from source commit `27a83c3dad40`, has SHA-256
  `e454f3e13af63eb780693576ecf70dcb7855dfc87ac060667719def173564eb9`, and
  embeds boot build ID `27a83c3dad40`. The credential-presence check passed
  without displaying the configured value; the ignored build directory is
  owner-only. This bootstrap image has not been published or flashed.

## Scope and remaining validation

This confirms board identity, local image installation, application boot, and
live USB continuity while the SD path is down. The later mount failures mean
the SD journal is not currently verified healthy; do not rely on local
recording until a full power-cycle check restores it. The vehicle is
disconnected indoors, so fresh zero-speed/RPM and vehicle-supply checks cannot
be made; the 60-minute parked gate, cellular release download, motion
cancellation, pending-image acceptance, and automatic rollback remain
untested on hardware. Private migration/production images embed the telemetry
credential and must never be published. The separate tokenless OTA candidate
is package-checked but remains local until the hardware safety and rollback
checks pass.
