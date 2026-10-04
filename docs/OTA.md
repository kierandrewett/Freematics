# Parked OTA updater

The updater targets Model B with the SIM7670 modem only. It uses cellular
transport, the ESP32 TLS stack, strict certificate/hostname/date checks, and
the two OTA application partitions in `default_16MB.csv`. It never sends the
telemetry bearer token to GitHub. Modems without the strict SIM7670 TLS path
are refused.

## Parked gate and cadence

No wall-clock window is used. The 60-minute timer starts only after a valid
MEMS sample and is earned by continuous successful samples; movement, an
invalid sample, or a sampling gap over 1.5 seconds restarts or invalidates the
proof. A failed motion read or a motion-threshold sample during an OTA transfer
cancels the transfer. Downloading and validating the inactive image never
changes the boot selection. In standby, the firmware verifies the exact
inactive partition and image digest, journals and reads back a pending identity
containing its partition address/type/subtype, size, and digest, repeats the
fresh motion, OBD speed/RPM, Model B supply, storage, and credential checks,
then selects it for boot immediately after those checks pass. First-boot
validation hashes the running image again.
If preparation or a final check fails, the candidate is discarded without
changing the boot selection. Model B supply must remain in
the conservative resting range (6.0 V through 12.9 V); a missing, weak, or
elevated reading cancels the transfer because it does not prove the car is
off. After six hours the standby
loop can attempt a check, but only after the full 60-minute quiet period and
fresh supported readings show speed 0, RPM 0, and plausible Model B supply.
Any fresh nonzero speed or RPM observation also invalidates the accumulated
quiet proof; a new full hour of valid stillness evidence is required before a
later attempt.
OBD is refreshed after the final motion-observation interval immediately
before cellular setup, then checked again before reboot. The modem owns the
shared coprocessor link during the download, so OBD is not polled concurrently;
motion proof, storage, credential presence, and supply voltage are monitored
during transfer. Missing, stale, unsupported, or invalid OBD readings fail
closed at each OBD check.
Standby motion reads share a mutex with the background sensor-acquisition task;
lock contention is bounded and treated as a failed motion sample, so it cannot
silently extend a quiet period or be mistaken for fresh motion evidence.
OTA also requires a successfully persisted-and-read-back telemetry credential
and a healthy SD journal; failed SD mount alone is not considered storage
ready, and the journal must still be healthy at first-boot validation.
The modem task owns the shared coprocessor link during the request. A motion
wake cancels modem/TLS command waits between 50 ms UART reads and prevents a
staged image from rebooting. Forced modem power-down still uses the driver's
2.51-second power pulse. This is a bounded source-level path, but wake-to-sample
recovery has not yet been measured on hardware; verify it under stalled-modem
conditions before enabling production OTA.

## Release assets

The latest GitHub release is expected to contain these exact asset names:

* `freematics-model-b.bin`
* `freematics-model-b.bin.sha256sum`

The sidecar uses standard `sha256sum` format. The updater downloads both over
HTTPS, follows only a short redirect chain to allowlisted GitHub release hosts,
captures the immutable release tag from GitHub's `latest` redirect, and then
downloads both assets from that same tag. It streams the image to the inactive
slot, checks the digest, and validates the ESP image. Boot selection is a
separate final standby operation, not part of the download task. On first boot,
firmware verifies that the recorded partition
identity matches the running partition and hashes the running image before it
can accept the image. Acceptance also requires storage, motion-sensor,
persistent endpoint configuration, and credential initialization; the ESP32 bootloader rolls back a failed or
power-cut first boot. A reset before boot selection leaves the old slot active;
an orphaned preparation record is cleared on the next normal boot.

Every OTA image embeds `FREEMATICS_RELEASE_VERSION=major.minor.patch`. Before
staging, the running firmware requires a valid candidate version strictly
newer than its own `FREEMATICS_RELEASE`; same-version rebuilds, malformed
versions, and downgrades are refused. Set `FREEMATICS_RELEASE` explicitly
when building a tokenless OTA release, for example `1.0.1` after a `1.0.0`
image. This is a downgrade guard, not release authentication: an attacker who
can replace the release assets can still publish a malicious image with a
higher version.

SHA-256 detects accidental corruption but does not authenticate a release if
the release asset and sidecar are both replaced by an attacker. HTTPS protects
the transport and validates GitHub's certificate, but does not protect a
compromised repository/release account.

The first private/local firmware boot seeds the configured telemetry token,
server host/path, APN, and configured Wi-Fi/SIM credentials into the existing
`storage` NVS namespace without overwriting values already there. A versioned
configuration marker is written only after these settings are loaded and the
endpoint is valid. OTA requires that marker and the persisted endpoint as well
as a committed-and-read-back token. The NVS copy is not
hardware-encrypted by this project; physical flash extraction remains in the
threat model. The migration image embeds the token and must never be published
to GitHub. To build a public OTA image, explicitly clear the inherited secret:

The private bootstrap also records a `private_cfg_flags` manifest describing
which APN, SIM, and Wi-Fi fields were configured. A tokenless OTA image requires
every flagged field to exist and be non-empty in NVS before it can accept an
update; it cannot mistake a blank build-time fallback for migrated settings.
If NVS initialization reports a format/full-partition error, firmware preserves
the partition rather than erasing credentials, disables network/OTA operation,
and continues local acquisition where possible. Recovering such an NVS
partition currently requires a separately planned service procedure.

```sh
FREEMATICS_TOKEN= FREEMATICS_OTA_RELEASE=1 PRODUCTION_BUILD=1 \
  FREEMATICS_RELEASE=1.0.1 pio run -e esp32dev-ota-test
```

This validates the private production configuration but generates a separate
allowlisted compile header for the OTA image. Feature, storage, and transport
mode remain compatible; server host/path, APN, and authentication values are
omitted from the public image and loaded from NVS. The release build is
restricted to the OTA-enabled PlatformIO environment, fails if a token is
present, and emits a non-secret release-mode marker. The local packager
requires both that marker and the token-absent marker, scans for configured
tokens, passwords, usernames, SIM PINs, Wi-Fi SSIDs, APNs, server hosts, and
exact server-path strings from the ignored `.env`, process environment, and
matching `local_config.h` settings, and repeats the checks on the staged copy.
For configured values at least eight bytes long, it also checks common Base64,
hex, URL-escaped, and UTF-16 encodings. Credential literals in
`local_config.h` are joined across adjacent C strings and comments are
excluded; unsupported escaped or prefixed private literals, or private macros
that cannot be resolved, fail packaging. Its output directory must be
empty before packaging, so an "upload everything in this folder" operation
cannot silently include logs, configs, or unrelated build outputs. It never
overwrites an existing asset pair and does not upload anything. The publisher
also rejects binaries containing exact configured server-path strings, without
mistaking an incidental substring in unrelated firmware text. These checks
guard against accidental publication, not deliberately falsified binaries or
markers.

Only publish the two files created by `tools/package_ota_release.py`. Never
attach the regular `esp32dev` production image, ELF/map files, build logs, or
the `.pio` build directory: the regular production image intentionally embeds
the telemetry token, and other build outputs may contain private build data.
The release-upload boundary is `tools/publish_ota_release.py`. It revalidates
the exact two-file allowlist, token-free build markers, configured credentials,
file modes, matching sidecar, and that the existing release tag matches the
firmware's embedded version immediately before calling `gh`. Before upload it
also queries the target release and fails closed if the asset inventory cannot
be read or is not empty. This prevents safe files being appended to a release
that already contains unknown or credential-bearing artifacts. It never uploads
logs, source archives, or build directories. It requires an existing release
tag and never clobbers assets. Use it instead of uploading files manually:

```sh
python3 tools/publish_ota_release.py v1.0.1 .pio/ota-release-v1.0.1
```

The device downloads the firmware and its SHA-256 sidecar from the same GitHub
release over HTTPS. The checksum detects transfer corruption or a mismatched
asset; it is not an independent publisher signature. A compromised GitHub
account or release could replace both files with a matching pair. The current
OTA trust model therefore relies on GitHub account/repository security and
HTTPS, and does not claim cryptographic publisher authentication.

The package directory must be owner-only (`0700`), and packaged files are
owner-only (`0600`). `.gitignore` alone does not protect manual uploads.

On POSIX build hosts, `.pio`, `.pio/build`, and each per-environment build
directory are set to mode `0700` before compilation. A restrictive umask and
post-build actions set the generated firmware image and ELF to `0600`. This
protects local build products, including token-bearing production images, from
other local users and reduces the risk of accidental attachment or copying.

## Local checks

```sh
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_telemetry_token.cpp -o /tmp/test_telemetry_token
/tmp/test_telemetry_token
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_parked_policy.cpp -o /tmp/test_ota_parked_policy
/tmp/test_ota_parked_policy
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_sha256_sidecar.cpp -o /tmp/test_ota_sha256_sidecar
/tmp/test_ota_sha256_sidecar
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_version_policy.cpp -o /tmp/test_ota_version_policy
/tmp/test_ota_version_policy
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_boot_identity.cpp -o /tmp/test_ota_boot_identity
/tmp/test_ota_boot_identity
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_boot_policy.cpp -o /tmp/test_ota_boot_policy
/tmp/test_ota_boot_policy
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_stage_policy.cpp -o /tmp/test_ota_stage_policy
/tmp/test_ota_stage_policy
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_cell_http_stream.cpp -o /tmp/test_cell_http_stream
/tmp/test_cell_http_stream
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_cell_poweroff_policy.cpp -o /tmp/test_cell_poweroff_policy
/tmp/test_cell_poweroff_policy
python3 -m unittest discover -s tools -p 'test_package_ota_release.py' -v
python3 -m unittest discover -s tools -p 'test_publish_ota_release.py' -v
```

If OTA is cancelled, modem shutdown first probes whether the modem responds,
then asks it to power down. The power-key fallback is used only if the modem
responds both before and after a failed shutdown command; an already-off or
newly unresponsive modem is never toggled, avoiding an accidental wake-up.

The current code has been host-tested and firmware-built, but cellular release
downloads, cancellation under motion, first-boot validation, and rollback have
not yet been exercised on the device. `ENABLE_OTA` defaults to 0 in the normal
`esp32dev` production environment. For an initial USB bootstrap, build the
private OTA-capable production image with:

```sh
PRODUCTION_BUILD=1 pio run -e esp32dev-ota-production
```

That image embeds the configured telemetry credential and must never be
published. The separate `esp32dev-ota-test` environment is compile-only; do not
flash or publish it. OTA updates themselves use only the tokenless package
created by `tools/package_ota_release.py`. Do not deploy the bootstrap or OTA
candidate until the car-off gate, cancellation, and rollback have passed
hardware tests.
