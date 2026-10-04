# OTA candidate evidence — 2026-10-04

This is a credential-free, tokenless OTA package built from the signed
Freematics source commit below. It is a release candidate only; it has not been
published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.19` |
| Source commit | `99bd8898aa6ebf62f4764867bdf4588bc5c4124e` |
| Boot build ID | `99bd8898aa6e` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `ee123a441ad5de29338c088639d220fcb1040adc6efdbd951785e45ca5aef413` |
| Local candidate directory | `.pio/ota-release-candidate-99bd889-v1.0.19/` |

The package directory is owner-only (`0700`); both files are owner-only
(`0600`). The package verifier accepted the image, version/source markers,
checksum sidecar, exact allowlist, and scan against configured private values.
The tracked examples and runbook were also scrubbed of the configured carrier
APN in commit `99bd889`; the ignored production config was not modified.

Reproduction from the clean source commit (using the private local
`local_config.h`; do not print or commit it):

```sh
FREEMATICS_TOKEN= FREEMATICS_OTA_RELEASE=1 FREEMATICS_RELEASE=1.0.19 \
  pio run -e esp32dev-ota-test
python3 tools/package_ota_release.py \
  .pio/build/esp32dev-ota-test/firmware.bin \
  --output-dir .pio/ota-release-candidate-99bd889-v1.0.19
python3 tools/package_ota_release.py --verify-only \
  --output-dir .pio/ota-release-candidate-99bd889-v1.0.19
sha256sum .pio/ota-release-candidate-99bd889-v1.0.19/freematics-model-b.bin
```

The normal and OTA-production PlatformIO environments built successfully;
the OTA-test environment built successfully with an empty token. Host tests
passed for parked eligibility, first-boot identity and
rollback, first-upload acceptance, HTTP stream parsing, release packaging and
publishing guards, and local recording. The unattended OTA safety policy
requires a full hour of continuous motion and 12.2–12.9 V resting-supply
evidence, fresh supported zero speed/RPM readings, healthy SD journalling, and
persisted endpoint/credential state.

Hardware validation remains outstanding: the Model B is not connected, and
the laptop currently sees a CH340 adapter. Therefore the car-off inference,
cellular download/cancellation, first-boot upload confirmation, and automatic
rollback have not been validated on hardware. Do not publish or deploy this
candidate until those tests pass.
