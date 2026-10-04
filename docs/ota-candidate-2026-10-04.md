# OTA candidate evidence — 2026-10-04

This is a credential-free, tokenless OTA package built from the signed
Freematics source commit below. It is a release candidate only; it has not been
published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.18` |
| Source commit | `97074a78cce43b7f81cb25a35bc9e8f62d48f264` |
| Boot build ID | `97074a78cce4` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `f5eb246edf3f48a07e308792ae3d3aaf0669a47801e69143ad26cc9ba7f527e6` |
| Local candidate directory | `.pio/ota-release-candidate-97074a7-v1.0.18/` |

The package directory is owner-only (`0700`); both files are owner-only
(`0600`). The package verifier accepted the image, version/source markers,
checksum sidecar, exact allowlist, and scan against configured private values.

Reproduction from the clean source commit (using the private local
`local_config.h`; do not print or commit it):

```sh
FREEMATICS_TOKEN= FREEMATICS_OTA_RELEASE=1 FREEMATICS_RELEASE=1.0.18 \
  pio run -e esp32dev-ota-test
python3 tools/package_ota_release.py \
  .pio/build/esp32dev-ota-test/firmware.bin \
  --output-dir .pio/ota-release-candidate-97074a7-v1.0.18
python3 tools/package_ota_release.py --verify-only \
  --output-dir .pio/ota-release-candidate-97074a7-v1.0.18
sha256sum .pio/ota-release-candidate-97074a7-v1.0.18/freematics-model-b.bin
```

The normal, OTA-production, and OTA-test PlatformIO environments built
successfully. Host tests passed for parked eligibility, first-boot identity and
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
