# OTA candidate evidence — 2026-10-05, v1.0.29

This local-only tokenless OTA candidate is built from the signed and pushed
source commit below. It has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.29` |
| Source commit | `c15548038b726ec5857a97d0e48d67f52c00d4bf` |
| Boot build ID | `c15548038b72` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `5e12f581ca89d4c4e4eed4d5ca249807f487828d13089444aea7e74feaf0c64b` |
| Local candidate directory | `.pio/ota-release-candidate-c155480-v1.0.29/` |

The OTA-enabled release build completed with the firmware token explicitly
empty. Packaging and verify-only accepted the exact two-file allowlist,
embedded version/source/build identities, token-absent marker, checksum
sidecar, configured private-value scans, and owner-only file modes. An initial
package attempt rejected the image because the source-commit scanner itself
left an ambiguous empty marker; the scanner was corrected, rebuilt cleanly,
and packaging then passed. No failed package was published.

Verification: 24 Grafana dashboard tests; 53 OTA packaging/publishing tests;
OTA release-version, release-identity, and parked-policy C++ tests; recorder
checks; normal production, OTA-production, and tokenless OTA-release firmware
builds. `similarity-py` was unavailable.

This proves host-side build and artifact checks only. The Model B is not
verified on the connected USB port; SD journaling, live upload, OTA download,
boot acceptance, and automatic rollback still require hardware validation.
