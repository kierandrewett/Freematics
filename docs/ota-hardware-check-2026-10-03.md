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
- With the SD journal unavailable, an 8-second read-only USB measurement
  produced 31 valid @FT1 frames at a 250 ms median/p95 cadence, one corrupt
  record, and zero USB drops or device restarts. The durable-journal health
  field stayed 0; missed readings rose from 199 to 229; the eight-slot
  handoff stayed empty. The vehicle/ECU was disconnected, so this verifies
  live transport and fail-closed recording only—not vehicle PID values.

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
