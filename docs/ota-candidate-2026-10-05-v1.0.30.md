# OTA candidate evidence — 2026-10-05, v1.0.30

This is a local-only tokenless OTA candidate built from pushed source. It has
not been published or flashed, and supersedes v1.0.29 for current-source
verification.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.30` |
| Source commit | `e1576de1b5506555e8294d47acf909ce90478e64` |
| Boot build ID | `e1576de1b550` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `8d564192e255b857a2f008b9f363b373dfeac3aed15b4e883fa815512f90ffb3` |
| Local candidate directory | `.pio/ota-release-candidate-e1576de-v1.0.30/` |

The `esp32dev-ota-test` build succeeded with the firmware token explicitly
empty. Package creation, `--verify-only`, the configured-private-value scan,
the exact two-file allowlist, and `sha256sum -c` all passed. The candidate
directory is mode `0700`; the image and checksum sidecar are mode `0600`. All
53 OTA packaging/publishing host tests passed. Grafana generation and its 25
dashboard tests also passed for the source commit.

This remains host-only evidence. The connected USB device is a generic CH340
adapter, not the Freematics Model B. SD recording under vehicle load, live
upload continuity, cellular OTA download, boot acceptance, and automatic
rollback have not been verified on hardware. Do not publish or flash this
candidate until the correct Model B and required hardware checks are available.
