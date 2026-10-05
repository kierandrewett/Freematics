# OTA candidate evidence — 2026-10-05, v1.0.22

This is a local, tokenless release candidate built from the signed and pushed
source commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.22` |
| Source commit | `92b7f7afcca8bfa8663c99fc5e213adcbcb700f7` |
| Boot build ID | `v4.4.7` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `5f166a4307581405b9e8889073b25036c4b195b2d116cc912e81803c7547bb38` |
| Local candidate directory | `.pio/ota-release-candidate-92b7f7a-v1.0.22/` |

The package verifier accepted the source/version markers, token-absent marker,
matching SHA-256 sidecar, exact two-file asset allowlist, owner-only file modes,
and scans against configured private values without displaying any value. The
regular and OTA-bootstrap images remain credential-bearing local build outputs
and are not release assets. No GitHub release was created.

The strict emulator, collector/history tests, dashboard tests, OTA policy tests,
packager tests, and publisher tests passed for this source state. The ESP32
normal, bootstrap-OTA, and tokenless OTA targets built on the host. Hardware
validation remains outstanding: no Model B or vehicle connection was verified,
so parked-state timing under real sensor behavior, cellular OTA cancellation,
first-boot telemetry acceptance, and rollback have not been exercised on-device.
Do not publish or deploy this candidate until the applicable hardware checks
pass.
