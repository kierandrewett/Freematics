# OTA candidate evidence — 2026-10-05, v1.0.31

Local-only, tokenless OTA candidate built from the signed and pushed source
commit below. It has not been published or flashed. This supersedes v1.0.30
for current-source verification.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.31` |
| Source commit | `e2221d250345acbf594b32323c7c0af76ba50073` |
| Boot build ID | `e2221d250345` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `ccf736018c1df3a4631b45fa71e9593eae6df2f84d9765f47209a8b366c2f0f7` |
| Local candidate directory | `.pio/ota-release-candidate-e2221d2-v1.0.31/` |

The normal `esp32dev` production build and the tokenless `esp32dev-ota-test`
build both succeeded. Package creation, `--verify-only`, private-value scans,
exact two-file allowlisting, and `sha256sum -c` passed. The candidate directory
is mode `0700`; its image and checksum sidecar are mode `0600`. All 53 OTA
packaging/publishing tests and 25 Grafana dashboard tests passed.

The sanitized host journal replay report is
[`journal-powercut-20261005.json`](../tools/emulator/journal-powercut-20261005.json).
It injects a simulated process/power cut at all 82 byte boundaries in a
three-record journal append. Complete CRC-valid records replay in order, and
torn tails are preserved for repair. This fake-SD test does not model physical
card cache/sector behavior or replace a hardware power-loss test.

The laptop still detects a generic CH340 adapter, not the Freematics Model B.
SD recording on the physical card, live upload continuity, cellular OTA
download, first-boot acceptance, and rollback remain unverified. Keep the
candidate unpublished and unflashed until the correct device is available and
the hardware checks pass.
