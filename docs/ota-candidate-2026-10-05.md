# OTA candidate evidence — 2026-10-05

This tokenless OTA package is built from the signed source commit below. It is
a local release candidate only; it has not been published or flashed. The
earlier v1.0.20 candidate is superseded by v1.0.21.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.21` |
| Source commit | `a466467a08f08949fce1d2ee9cbd568f49efa551` |
| Boot build ID | `a466467a08f0` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `5923757318d8ebc493d27885e4f5d77ba8e4ef89fc6c93b4cdf68500e231dfdd` |
| Local candidate directory | `.pio/ota-release-candidate-a466467-v1.0.21/` |

The package verifier accepted the source/version markers, token-absent marker,
checksum sidecar, exact asset allowlist, owner-only permissions, and scan
against configured private values without displaying any configured value.
No Freematics GitHub release is published.

The separate `esp32dev-ota-production` bootstrap also built successfully with
the local production config. Its ELF and image are mode `0600`, the image
contains configured private values as intended, and a direct release-package
attempt was rejected. This private bootstrap is not an OTA release asset.

Rebuild from this source commit with the private local `local_config.h`
available; do not print or commit it:

```sh
FREEMATICS_TOKEN= FREEMATICS_OTA_RELEASE=1 PRODUCTION_BUILD=1 \
  FREEMATICS_RELEASE=1.0.21 \
  pio run -e esp32dev-ota-test
python3 tools/package_ota_release.py \
  .pio/build/esp32dev-ota-test/firmware.bin \
  --output-dir .pio/ota-release-candidate-a466467-v1.0.21
python3 tools/package_ota_release.py --verify-only \
  --output-dir .pio/ota-release-candidate-a466467-v1.0.21
sha256sum .pio/ota-release-candidate-a466467-v1.0.21/freematics-model-b.bin
```

All 66 host-side Python tests passed. The normal strict emulator and the
Clang ASan/UBSan waveform replay each passed 62 scenarios, including simulated
SD restart, lost acknowledgement, voltage/motion acquisition timestamps, and
the read-only waveform query. Repeatable report and scope notes are in
[`recording-sync-evidence-2026-10-04.md`](recording-sync-evidence-2026-10-04.md).

The OTA-enabled firmware and credential-aware package checks pass on the
host, including the rebuilt v1.0.21 image. Hardware validation is still
outstanding. The laptop sees a CH340 adapter, not the Freematics Model B.
Therefore car-off inference, cellular OTA, first-boot upload acceptance,
rollback, and live vehicle recording have not been validated on hardware. Do
not publish or deploy this candidate until those checks pass.
