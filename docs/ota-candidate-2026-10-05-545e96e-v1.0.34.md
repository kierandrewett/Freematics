# OTA candidate evidence — 2026-10-05, v1.0.34

Local-only, token-free Model B OTA candidate built from the signed and pushed
Freematics commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.34` |
| Source commit | `545e96e066a105baf244f6799aec41bc20114e89` |
| Build ID | `545e96e066a1` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `40b5b0ea42c319fd29af34984e9c084d0d93a6b80d7ffef1614e0a93d4d144d1` |
| Local candidate directory | `.pio/ota-release-candidate-545e96e-v1.0.34/` |

Built with the documented explicit-empty-token OTA release command in the
`esp32dev-ota-test` environment. The candidate package and `--verify-only`
check accepted the embedded source/version markers, token-absent and
OTA-release markers, configured-private-value scan, exact two-file allowlist,
owner-only permissions, and SHA-256 sidecar. The 16 MB image/partition layout
was used. The candidate directory is mode `0700`; both files are mode `0600`.

The private USB OTA bootstrap was also rebuilt locally from source commit
`545e96e066a105baf244f6799aec41bc20114e89`. Its build ID is `545e96e066a1`,
version is `1.0.0`, and image SHA-256 is
`e77928c7a73c581624fe377a709e9115eef3edca7ba7050e07ffcec0f0543327`. It
contains the telemetry credential, remains owner-only under `.pio`, and is not
an OTA asset. Local build evidence is stored in the ignored production build
directory; it is not committed.

The SD recovery task now retries immediately after detecting unhealthy storage,
then uses bounded 1, 2, 4, 8, 16, and 30 second backoff, resetting after
recovery. This reduces the delay before a transiently failed card can resume
journalling, but does not make a failed SD append recoverable: while storage is
unhealthy, samples remain counted as missed rather than queued in RAM. The
strict recorder harness and SD retry policy tests passed.

Host validation at this source included all 77 firmware-tool Python tests, all
27 monitoring tests, a clean collector build, recorder and sampling-boundary
checks, SD-retention and USB queue checks, and collector sampling checks. The
strict Clang ASan/UBSan emulator with the local collector completed 74/74
scenarios; its local report is
`/tmp/freematics-emulator-2026-10-05-545e96e-clang.json` (SHA-256
`23b6225d68019ef05238225039c83fc005d6dd5bdce991cef966ddde7909c9f7`). Its
88-byte append cut matrix uses fake SD and does not prove physical card
controller, filesystem-cache, or vehicle power-loss durability.

No GitHub release is published. The Model B is disconnected, so physical board
identity, car-off gating, installed-card journalling, upload continuity, live
USB/Grafana readings, OTA first-boot acceptance, and rollback remain
unverified. Keep this candidate unpublished and unflashed until those hardware
tests pass.
