# OTA candidate evidence — 2026-10-05, v1.0.29

This local-only tokenless OTA candidate is built from the pushed source commit
below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.29` |
| Source commit | `f1a105f8ab5074425665010d0716da793d17d2fb` |
| Boot build ID | `f1a105f8ab50` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `3945ddbba1be37b0be4658570800e86c98435d6b087033679894deb15eb147f4` |
| Local candidate directory | `.pio/ota-release-candidate-f1a105f-v1.0.29/` |

The OTA-enabled release build completed from a clean pushed checkout with the
firmware token explicitly empty. Packaging and verify-only accepted the exact
two-file allowlist, embedded version/source/build identities, token-absent
marker, checksum sidecar, configured private-value scans, and owner-only file
modes. `sha256sum -c` passed. No package was published.

Verification for this candidate: `pio run -e esp32dev-ota-test`; package and
verify-only passed; `sha256sum -c` passed; 6 OTA policy Python tests and 24
Grafana dashboard tests passed. Earlier source revision checks also passed 53
OTA packaging/publishing tests; OTA release-version, release-identity, and
parked-policy C++ tests; recorder checks; and normal production and
OTA-production firmware builds. `similarity-py` was unavailable.

This proves host-side build and artifact checks only. The Model B is not
verified on the connected USB port; SD journaling, live upload, OTA download,
boot acceptance, and automatic rollback still require hardware validation.
