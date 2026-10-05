# OTA candidate evidence — 2026-10-05, v1.0.25

This is a local tokenless OTA candidate built from the signed and pushed source
commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.25` |
| Source commit | `4822c6f912a9012587dc6d45c3c052accbe4c751` |
| Boot build ID | `4822c6f912a9` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `27b6aae2e9a792bbcadbdb3f6349507adf1e2cfde9f04033b7c73fb7481aea19` |
| Local candidate directory | `.pio/ota-release-candidate-4822c6f-v1.0.25/` |

The ESP32 OTA-release PlatformIO target built successfully from the pushed
source. The package verifier accepted the embedded version/source markers,
token-absent marker, exact matching SHA-256 sidecar, two-file asset allowlist,
owner-only file modes, and scans against configured private values without
displaying any value. This candidate remains local; no GitHub release was
created.

Host verification for this revision includes 21 Grafana generator/SQLite query
tests, 105 collector tests (4 skipped), the standalone measurement-age and OTA
parked-policy C tests, and successful collector and tokenless OTA-target
builds. The strict firmware/collector emulator passed for the preceding
collector/Grafana source slice; it was not rerun for the final Grafana-only
fail-closed selector adjustment.

Physical validation remains outstanding. The connected USB adapter is a
generic CH340 reader, not a confirmed Freematics Model B. SD-card hardware
health, car-off inference, cellular uploads, live USB telemetry, and OTA
acceptance/rollback have not been exercised on-device. Do not publish or deploy
this candidate before those applicable checks pass.
