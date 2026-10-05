# OTA candidate evidence — 2026-10-05, v1.0.33

Local-only tokenless OTA candidate built from the signed and pushed Freematics
source commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.33` |
| Source commit | `f895cb284f3a71ad77d9d8161f9667d34ff61caa` |
| Build ID | `f895cb284f3a` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `025477b4f2a401aa743689d3d6dacc0825774ce527a444c900ad61073668ac26` |
| Local candidate directory | `.pio/ota-release-candidate-f895cb2-v1.0.33/` |

Built with the `esp32dev-ota-test` PlatformIO environment from a clean
worktree. The candidate packager and its `--verify-only` pass accepted the
embedded source/version markers, token-absent marker, configured-private-value
scan, exact two-file allowlist, owner-only permissions, and SHA-256 sidecar.
The candidate directory is mode `0700`; both assets are mode `0600`. No GitHub
release is published. The separate credential-bearing USB bootstrap is a
private local build product and is not an OTA asset.

On this source, all 68 firmware Python tool tests and 26 Grafana dashboard
tests passed. The strict sanitized firmware emulator passed, including journal
power-cut boundaries, torn-tail recovery, acknowledgement replay, and
collector acceptance. OBD workspace tests, the optimized release build, and
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
