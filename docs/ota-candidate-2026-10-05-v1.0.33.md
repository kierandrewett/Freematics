# OTA candidate evidence — 2026-10-05, v1.0.33

Local-only tokenless OTA candidate rebuilt from the signed and pushed Freematics
source commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.33` |
| Source commit | `c3276c499039509b361bb451b968fe461a8ff023` |
| Build ID | `c3276c499039` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `2439286777f08852f5fa60545d06d212d8360c44317f40c0c47317c4bad524a6` |
| Local candidate directory | `.pio/ota-release-candidate-c3276c4-v1.0.33/` |

Built with the `esp32dev-ota-test` PlatformIO environment from a clean
worktree using the documented explicit-empty-token OTA release command. The
candidate packager and its `--verify-only` pass accepted the
embedded source/version markers, token-absent marker, configured-private-value
scan, exact two-file allowlist, owner-only permissions, and SHA-256 sidecar.
The candidate directory is mode `0700`; both assets are mode `0600`. No GitHub
release is published. The separate credential-bearing USB bootstrap is a
private local build product and is not an OTA asset. The OTA publisher also
requires an owner-only hardware-acceptance attestation matching this exact
image, source, and boot build ID; no such attestation exists because the board
is disconnected and the hardware tests have not passed. Earlier local v1.0.33
packages from `f895cb284f3a71ad77d9d8161f9667d34ff61caa`,
`8094e743f58bcabcae01ddde405b4c490b8170f4`, `878cc95bbf67d027beccbeb0dae44000b8b4ce7f`,
`3c120f376eb0003cf4abfe99c5361ec864d52e1b`, and
`9be2200f4730b7b347b407b599cc7c6f45dcc929` are retained unchanged; use only
the candidate directory recorded above for any later review.

On this source, all 75 firmware Python tool tests, 105 collector tests (four
skipped), and 141 OBD workspace tests passed. The strict sanitized firmware emulator passed, including journal
power-cut boundaries, torn-tail recovery, acknowledgement replay, and
collector acceptance. It now also cuts power at all 88 byte positions across
a three-record append batch and verifies exact replay/acknowledgement order.
The recorder harness verifies that an SD commit failure immediately before
orderly wrap-up is reflected in the durable checkpoint's missed-reading count
and capture sequence. OBD workspace tests, the optimized release build, and
Clippy also passed on the current `main` checkout.

The 16 MB upload-size/partition configuration is explicit in
`platformio.ini`; `test_ota_partition_table.py` now guards the upload override,
partition file, and OTA environment inheritance. PlatformIO's board template
still prints its default 4 MB descriptor, while the actual production image
command uses `--flash_size 16MB`; the connected Model B was previously probed
with 16 MB flash. Verify actual flash geometry again before any write.

The Model B is not currently connected. Car-off timing and ADC thresholds,
cellular transfer cancellation, SD journaling on the installed card, upload
continuity, live USB readings, first-boot acceptance, and automatic rollback
remain unverified on hardware. Keep this candidate unpublished and unflashed
until the hardware acceptance checks pass.
