# OTA candidate evidence — 2026-10-05

This tokenless OTA package is built from the signed source commit below. It is
a local release candidate only; it has not been published or flashed.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.20` |
| Source commit | `a1c82c56254c7d1ec21473c43d21a6daafbbc5ae` |
| Boot build ID | `a1c82c56254c` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `b5da13e1c378e4b94be708479183625e80bf144be25d1c8bcde1234c22e65289` |
| Local candidate directory | `.pio/ota-release-candidate-a1c82c5-v1.0.20/` |

The package verifier accepted the source/version markers, token-absent marker,
checksum sidecar, exact asset allowlist, owner-only permissions, and scan
against configured private values. The earlier local v1.0.19 candidate is
superseded. No Freematics GitHub release is published.

The separate `esp32dev-ota-production` bootstrap also built successfully with
the local production config. Its ELF and image are mode `0600`, the image
contains configured private values as intended, and a direct release-package
attempt was rejected. This private bootstrap is not an OTA release asset.

Rebuild from this source commit with the private local `local_config.h`
available; do not print or commit it:

```sh
FREEMATICS_TOKEN= FREEMATICS_OTA_RELEASE=1 FREEMATICS_RELEASE=1.0.20 \
  pio run -e esp32dev-ota-test
python3 tools/package_ota_release.py \
  .pio/build/esp32dev-ota-test/firmware.bin \
  --output-dir .pio/ota-release-candidate-a1c82c5-v1.0.20
python3 tools/package_ota_release.py --verify-only \
  --output-dir .pio/ota-release-candidate-a1c82c5-v1.0.20
sha256sum .pio/ota-release-candidate-a1c82c5-v1.0.20/freematics-model-b.bin
```

All 66 host-side Python tests passed. The normal strict emulator and the
Clang ASan/UBSan waveform replay each passed 62 scenarios, including simulated
SD restart, lost acknowledgement, voltage/motion acquisition timestamps, and
the read-only waveform query. Repeatable report and scope notes are in
[`recording-sync-evidence-2026-10-04.md`](recording-sync-evidence-2026-10-04.md).

Hardware validation is still outstanding. The laptop sees a CH340 adapter,
not the Freematics Model B. Therefore car-off inference, cellular OTA,
first-boot upload acceptance, rollback, and live vehicle recording have not
been validated on hardware. Do not publish or deploy this candidate until
those checks pass.
