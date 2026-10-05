# OTA candidate evidence — 2026-10-05, v1.0.34

Local-only, token-free Model B OTA candidate built from the current signed and
pushed Freematics `master`. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.34` |
| Source commit | `2aeda410b350329296749521281b7eba816f6ee5` |
| Build ID | `2aeda410b350` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `c6416bf2e4873843ff99df2240d4d11399dcf4ecb9249c4822c7eddc9ac97560` |
| Local candidate directory | `.pio/ota-release-candidate-2aeda41-v1.0.34/` |

The candidate was built with the explicit empty-token OTA-test environment and
16 MB image/partition layout. The release package verifier accepted the image
markers, credential-absence scan against private local configuration, exact
two-file allowlist, SHA-256 sidecar, and owner-only permissions. The candidate
directory is mode `0700`; the image and sidecar are mode `0600`.

The private USB bootstrap was rebuilt from the same source and version. Its
SHA-256 is
`7e7f00ba45e41fb31f270c2a423d7d631ad90c398a35699db342b6217ecc3623`.
It contains production credentials and remains owner-only under the ignored
`.pio` directory. It is not a release asset.

The live Grafana view now flags fresh supply measurements whose trusted capture
UTC is unavailable without placing them at upload time. It also exposes the
latest reported SD journal-write status and the firmware's cumulative missed
sample counter. Dashboard generation tests passed, including freshness,
credential exclusion, and live-panel overlap checks.

Host validation: 28 monitoring tests, 77 firmware-tool tests, and 105 collector
tests (4 skipped) passed; the collector native build succeeded. Both
`esp32dev-ota-test` and `esp32dev-ota-production` builds succeeded. The source
contains bounded SD recovery retry backoff, but host emulation cannot verify
physical card initialization, durability, or power-loss behavior.

The Model B is disconnected. Board identity, car-off OTA gating, installed-card
journalling/replay, upload continuity, live USB/Grafana readings, first-boot
acceptance, and automatic rollback are unverified. Do not publish or flash this
candidate until the required hardware acceptance run passes.
