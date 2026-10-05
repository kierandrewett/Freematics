# OTA candidate evidence — 2026-10-05, v1.0.23

This is a local tokenless OTA candidate built from the signed and pushed source
commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.23` |
| Source commit | `d2ff0e89dfaca4a640531eefe917ce2868ae41db` |
| Boot build ID | `d2ff0e89dfac` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `bb9fdca666dcbe05da7cc650e84e199cf77887a4b085170c4e5649d86fbff942` |
| Local candidate directory | `.pio/ota-release-candidate-d2ff0e8-v1.0.23/` |

The ESP32 OTA-release PlatformIO target built successfully from a clean checkout.
The package verifier accepted the embedded release/source markers, token-absent
marker, matching SHA-256 sidecar, exact two-file allowlist, owner-only file
modes, and scans against configured private values without displaying any
value. The candidate remains local; no GitHub release was created.

The generator/SQL parity suite (19 tests) and collector waveform suite (11
tests) passed for the Grafana voltage-gap correction in this source revision.
This does not validate the production firmware on a physical Model B. The
currently connected USB adapter is a CH340 OBD reader, not a confirmed
Freematics device. Car-off timing, SD journaling, cellular upload/recovery,
live USB telemetry, and OTA acceptance/rollback remain hardware validation
gaps. Do not publish or deploy this candidate until the applicable checks pass.
