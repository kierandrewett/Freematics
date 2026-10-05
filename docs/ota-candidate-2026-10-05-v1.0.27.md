# OTA candidate evidence — 2026-10-05, v1.0.27

This local-only OTA candidate includes the wrap-up journal checkpoint for
trailing capture losses. It was built from signed source commit
`4a9facf1db8761bc82bb0f06cc8bec6b4a5e5a3a` and has not been published or
flashed.

This candidate was superseded for publication by v1.0.28 after later
non-documentation changes advanced the repository source. The packager
correctly requires an image rebuilt against that newer source commit.

| Property | Value |
| --- | --- |
| Firmware version | `1.0.27` |
| Source commit | `4a9facf1db8761bc82bb0f06cc8bec6b4a5e5a3a` |
| Boot build ID | `4a9facf1db87` |
| Image | `freematics-model-b.bin` |
| SHA-256 | `4057c84f686395d5dec99b2fad699a2f613cd019ab4ab7b26c0f36c0a760af7c` |
| Local candidate directory | `.pio/ota-release-candidate-4a9facf-v1.0.27/` |

The OTA-enabled release target built successfully with an empty firmware token.
The packager and its verify-only path accepted the exact two-file asset
allowlist, source/version/build markers, token-absent marker, SHA-256 sidecar,
configured-private-value scans, and owner-only modes (directory `0700`, files
`0600`). These are artifact checks, not proof of GitHub publication, cellular
download, device acceptance, or rollback.

Host verification: all 67 tests from
`python3 -m unittest discover -s tools -p 'test_*.py'` passed, including the
wrap-up checkpoint policy tests. The ordinary production image and the OTA
release image both compiled. No Model B hardware was available for SD,
cellular, USB, update-acceptance, or rollback validation.
