# OTA candidate evidence — 2026-10-05, v1.0.28

This local-only OTA candidate is built from the signed and pushed source
commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.28` |
| Source commit | `4fe143a421a9a10f10e08cf325a7489ae8eba7bc` |
| Boot build ID | `4fe143a421a9` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `75c93723f712435c90823e15235ecc29f4d0881cd8b77b814894dc8badf673a3` |
| Local candidate directory | `.pio/ota-release-candidate-4fe143a-v1.0.28/` |

The OTA-enabled release target built successfully with an empty firmware token.
The packager and verify-only command accepted the exact two-file allowlist,
source/version/build markers, token-absent marker, checksum sidecar, configured
private-value scans, and owner-only modes (directory `0700`, files `0600`).
This verifies artifact construction and secret scanning only; it does not prove
GitHub publication, cellular download, device acceptance, or rollback.

Host checks on the source include 23 dashboard/SQLite tests, 105 collector
tests (4 skipped), all 67 tools tests, and a successful collector server object
compile. The production firmware and OTA-enabled firmware builds both passed.
The similarity checker was unavailable. Physical Model B SD, upload, USB, OTA
acceptance, and rollback validation remain outstanding.
