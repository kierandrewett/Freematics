# OTA candidate evidence — 2026-10-05, v1.0.26

This is a local tokenless OTA candidate built from the signed and pushed source
commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.26` |
| Source commit | `002222c43db6b7de25f93e5e99f70080705408a8` |
| Boot build ID | `002222c43db6` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `6cc191d92f485133bc738060ca1f750475e8c268c219b378f268dab6cd632aa4` |
| Local candidate directory | `.pio/ota-release-candidate-002222c-v1.0.26/` |

The `esp32dev-ota-test` target built with production release flags and an empty
firmware token. The package verifier accepted the embedded version/source
markers, token-absent marker, matching SHA-256 sidecar, exact two-file asset
allowlist, owner-only file modes, and scans against configured private values
without displaying any value. The candidate remains local; no GitHub release
was created.

Host verification for this revision includes 21 Grafana generator/SQLite
tests, 105 collector tests (4 skipped), 56 OTA artifact/configuration tests,
the strict firmware/collector emulator, recorder/SD failure simulations, the
OTA parked-policy test, and the OBD dashboard tests/build. The OBD tests cover
passive Freematics telemetry, serial reconnect/corruption handling, Corsa
HS/MS profile selection, and the unsupported-PID display state.

The parked policy now resets its one-hour proof when a fresh nonzero speed or
RPM is observed during a transfer. The Grafana historical gap logic handles
uint32 capture-sequence rollover. These are host-verified software changes;
they do not prove ignition-state inference, Model B SD hardware health,
cellular continuity, USB behavior, or OTA acceptance/rollback on the car.

Physical validation remains outstanding. Do not publish or deploy this
candidate until the correct Model B and relevant hardware gates are verified.
