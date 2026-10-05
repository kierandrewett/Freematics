# OTA candidate evidence — 2026-10-05, v1.0.24

This is a local tokenless OTA candidate built from the signed and pushed source
commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.24` |
| Source commit | `da117375d94fe54dbc98b66f0c314441842edf09` |
| Boot build ID | `da117375d94f` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `9e12a89bd2af1f8eaba4f68d4881910a056084860121791eb712c957573987d7` |
| Local candidate directory | `.pio/ota-release-candidate-da11737-v1.0.24/` |

The ESP32 OTA-release PlatformIO target built successfully from the pushed
source. The package verifier accepted the embedded version/source markers,
token-absent marker, exact matching SHA-256 sidecar, two-file asset allowlist,
owner-only file modes, and scans against configured private values without
displaying any value. This candidate remains local; no GitHub release was
created.

Host verification for the changes in this source revision includes 21 Grafana
generator/SQLite query tests, 105 collector tests (4 skipped), strict firmware
and collector emulation, and the standalone measurement-age and OTA parked
policy C tests. The collector and OTA-release firmware targets compile.

Physical validation remains outstanding. The connected USB adapter is a
generic CH340 reader, not a confirmed Freematics Model B. SD-card hardware
health, car-off inference, cellular uploads, live USB telemetry, and OTA
acceptance/rollback have not been exercised on-device. Do not publish or deploy
this candidate before those applicable checks pass.
