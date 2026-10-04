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
proof. A failed motion read during an OTA transfer cancels the transfer, and a
staged image is not rebooted into without a fresh final motion check. After
six hours the standby loop can attempt a check, but only after the full
60-minute quiet period and fresh supported readings show speed 0, RPM 0, and
Model B supply below 13.2 V. Missing, stale, unsupported, or invalid readings
fail closed.
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
streams the image to the inactive slot, checks the digest, validates the ESP
image, records the pending digest, and changes the boot partition. The new
image is accepted only after storage and motion-sensor initialization; the
ESP32 bootloader rolls back a failed/power-cut first boot.

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

The first private/local firmware boot seeds the configured telemetry token to
the existing `storage` NVS namespace without overwriting a token already
there. OTA remains disabled unless the credential was committed and read back
successfully; it also requires a healthy SD journal both before staging and
before first-boot confirmation. Later tokenless OTA images use the stored
value. The NVS copy is not
hardware-encrypted by this project; physical flash extraction remains in the
threat model. The migration image embeds the token and must never be published
to GitHub. To build a public OTA image, explicitly clear the inherited secret:

```sh
FREEMATICS_TOKEN= FREEMATICS_OTA_RELEASE=1 PRODUCTION_BUILD=1 \
  FREEMATICS_RELEASE=1.0.1 pio run -e esp32dev-ota-test
```

This still validates the private production server/APN configuration. The
release build is restricted to the OTA-enabled PlatformIO environment, fails
if a token is present, and emits a non-secret release-mode marker. The local
packager requires both that marker and the token-absent marker, scans for
configured token/password/secret values from the ignored `.env`, process
environment, and credential-named `local_config.h` settings, and repeats the
checks on the staged copy. It never overwrites an existing asset pair and does
not upload anything. Server and APN routing settings remain in the firmware;
they are not treated as authentication credentials. These checks guard
against accidental publication, not deliberately falsified binaries or
markers.

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
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \\
  tools/test_ota_stage_policy.cpp -o /tmp/test_ota_stage_policy
/tmp/test_ota_stage_policy
python3 -m unittest discover -s tools -p 'test_package_ota_release.py' -v
```

The current code has been host-tested and firmware-built, but cellular release
downloads, cancellation under motion, first-boot validation, and rollback have
not yet been exercised on the device. `ENABLE_OTA` defaults to 0 in the normal
`esp32dev` production environment; `esp32dev-ota-test` is a compile-only target
that enables the path for validation. Do not flash or publish that test image
until the car-off gate, cancellation, and rollback have passed hardware tests.
