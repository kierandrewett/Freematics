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
proof. The passive Model B input-voltage sample must also remain present and
within the 12.2–12.9 V resting range throughout the same hour; a missing or
weak sample, long gap, or elevated voltage restarts that proof. Loss of either
proof during a download cancels the transfer. Downloading and validating the
inactive image never changes the boot selection. In standby, the firmware verifies the exact
inactive partition and image digest, journals and reads back a pending identity
containing its partition address/type/subtype, size, and digest, repeats the
fresh motion, OBD speed/RPM, Model B supply, storage, and credential checks,
then selects it for boot immediately after those checks pass. First-boot
validation hashes the running image again.
If preparation or a final check fails, the candidate is discarded without
changing the boot selection. Model B supply must remain in
the conservative resting range (12.2 V through 12.9 V); a missing, weak, or
elevated reading cancels the transfer because it does not prove the car is
off. This is an inference from continuous motion/electrical evidence, not a
direct ignition-state sensor. The 12.2 V lower limit is a conservative firmware threshold; validate
Model B ADC accuracy and modem-load sag on hardware before enabling OTA.
After six hours the standby
loop can attempt a check, but only after the full 60-minute quiet period and
fresh supported readings show speed 0, RPM 0, and plausible Model B supply.
Any fresh nonzero speed or RPM observation also invalidates the accumulated
quiet proof; a new full hour of valid stillness evidence is required before a
later attempt.
OBD is refreshed after the final motion-observation interval immediately
before cellular setup and checked again before reboot. During download, the
telemetry owner's cancellation callback rechecks supported, fresh speed and
RPM every five seconds. Unknown, stale, or non-zero readings cancel the
transfer. The standby owner temporarily releases the coprocessor mutex for
these reads, while it independently continues the motion and supply checks;
other acquisition tasks remain asleep. It reacquires the mutex after the
telemetry owner finishes or acknowledges cancellation. This uses the Model B
OBD link separately from the SIM7670 BEE UART; verify this concurrency and
cancellation latency on hardware before enabling unattended OTA.
Standby motion reads share a mutex with the background sensor-acquisition task;
lock contention is bounded and treated as a failed motion sample, so it cannot
silently extend a quiet period or be mistaken for fresh motion evidence.
OTA also requires a successfully persisted-and-read-back telemetry credential
and a healthy SD journal; the journal remains durable across an update and
pending records continue to replay with their original capture timestamps
after normal telemetry resumes. Failed SD mount alone is not considered
storage ready. In addition, before parked eligibility, periodically during a long
cellular transfer, and at final pre-activation checks, the standby owner probes
the mounted card under the shared SD lock. The probe exclusively creates a
uniquely named scratch file outside the journal, writes a fixed pattern, calls
`fsync`, reads the bytes back and compares them, then removes the file. It does
not append to, acknowledge, seek, or rewrite the journal and creates no RAM
record backlog. SD I/O failures (including cleanup failure) deny OTA and mark
cached queue health false until a later successful probe or journal
reinitialization; lock contention denies OTA without changing cached health.
Probes are deferred until the trip logger has ended
and the uploader reports its parked state, so they do not race normal journal
users; lock acquisition remains bounded by the common SD-lock timeout. On first
boot, pending-image admission also performs a fresh probe before recorder and
uploader tasks are started, while upload confirmation continues to require an
actually journaled and accepted current-boot sample.
The modem task owns the shared coprocessor link during the request. A motion
wake cancels modem/TLS command waits between 50 ms UART reads and prevents a
staged image from rebooting. Forced modem power-down still uses the driver's
2.51-second power pulse. The standby owner waits at most 30 seconds for the
modem task to acknowledge cancellation; if ownership is still stuck, it resets
the ESP instead of releasing the shared link concurrently. Boot-slot selection
is not part of the transfer task, so that reset leaves the current image
selected. This is a bounded source-level path, but wake-to-sample recovery has
not yet been measured on hardware; verify it under stalled-modem conditions
before enabling production OTA.

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
firmware verifies that the recorded partition identity matches the running
partition and hashes the running image, but leaves it in `PENDING_VERIFY`. A
separate supervisor gives it 15 minutes to journal a current-boot sample to SD
and receive an exact successful HTTPS collector acknowledgement for a batch
containing that sample. Replaying older backlog, connecting to the server, or
receiving an unverified HTTP 200 is not enough. Only then—while SD,
motion-sensor, persistent endpoint, and credential checks still pass—is the
image confirmed. If no qualifying upload arrives before the deadline, firmware
requests rollback and reboots. A reset before boot selection leaves the old slot
active; an orphaned preparation record is cleared on the next normal boot.

Every OTA image embeds `FREEMATICS_RELEASE_VERSION=major.minor.patch`. Before
staging, the running firmware requires a valid candidate version strictly
newer than its own `FREEMATICS_RELEASE`; same-version rebuilds, malformed
versions, and downgrades are refused. Set `FREEMATICS_RELEASE` explicitly
when building a tokenless OTA release, for example `1.0.1` after a `1.0.0`
image. The device also resolves the captured release tag through GitHub's
commit API and requires the image's unique embedded
`FREEMATICS_SOURCE_COMMIT` to match that tag's commit, while requiring the
image version to exactly match the `vMAJOR.MINOR.PATCH` (or unprefixed)
release tag. Missing, malformed, duplicate, or mismatched identity metadata is
rejected before the image is staged. These checks bind asset identity to the
release ref; they do not authenticate the publisher if the repository or
release account is compromised.

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
to GitHub. For a public OTA image, explicitly clear the inherited secret:

Private build tokens are written to an owner-only generated header under the
per-environment `.pio/build` directory, not passed as compiler command-line
defines. This keeps verbose compiler output from disclosing the token. The
regular production ELF and firmware image still intentionally contain the
token and remain private build products; only the verified token-free OTA
package may be published. BLE command contents are also omitted from serial
logs because configuration commands can carry credentials.

Build command:

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
allowlisted compile header for the OTA image. Storage and transport mode remain
compatible; server host/path, APN, authentication values, and local HTTP access
are excluded from the public image. Private network identity is loaded from
NVS. HTTPD remains disabled in release images because this firmware's local AP
uses built-in default credentials; shipping them would make that service
publicly accessible. The release build is
restricted to the OTA-enabled PlatformIO environment, fails if a token is
present, and emits a non-secret release-mode marker. The local packager
requires both that marker and the token-absent marker, scans for configured
tokens, passwords, usernames, SIM PINs, Wi-Fi SSIDs, APNs, server hosts, and
exact server-path strings from the ignored `.env`, process environment, and
matching `local_config.h` settings, and repeats the checks on the staged copy.
OTA builds also require a clean Git worktree and embed the full source commit
in the firmware. Packaging and the final publisher require that commit to be
an ancestor of the checkout and reject any subsequent non-documentation source,
staged, or untracked changes. Documentation-only commits are permitted after
the image build. This prevents a stale or dirty-build image from being
published under changed firmware inputs. The same marker is printed at boot
alongside the shorter build ID for field verification.
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
file modes, matching sidecar, and that the Git tag resolves to the firmware's
embedded source commit and version before calling `gh`. Before upload it
also requires an empty draft release and fails closed if the asset inventory
cannot be read, then revalidates the files immediately before upload.
It snapshots the verified image and checksum into a private temporary directory,
verifies that snapshot, and resolves the tag to the embedded source commit again
immediately before upload. After upload, it checks the draft asset inventory,
downloads both assets, verifies their exact bytes and checksum, then checks the
inventory and source tag again. Only after those checks does it publish the
draft release. This prevents caller-directory changes after verification from
changing the bytes handed to `gh`, detects tag and asset changes during
preflight/readback, and keeps unverified assets out of public releases. It
never uploads logs, source archives, or build directories. It requires an
existing empty draft release tag and never clobbers assets. Use it instead of
uploading files manually.

Publication also requires a private hardware-acceptance evidence file passed
with `--hardware-evidence` and its detached GPG signature at the same path with
`.asc` appended. The signature must verify against the primary key selected by
Git's configured `user.signingkey`; this makes the all-passing hardware record
an explicit signer attestation instead of trusting editable JSON booleans.
This records the configured operator's attestation; it is not a machine-signed
measurement or protection against that signer deliberately attesting false
results.
After the Model B acceptance run, create the signature with
`gpg --detach-sign --armor --output hardware-evidence.json.asc hardware-evidence.json`.
Both files must be regular non-symlinks owned by the current user with mode
exactly `0600`; they are read locally and are never staged, committed, or
uploaded. The publisher rejects malformed JSON, missing or unknown keys,
unsupported schema versions, and timestamps other than valid UTC RFC3339 (`Z`)
timestamps. Its exact JSON schema is:

```json
{
  "schema_version": 1,
  "tested_at": "2026-10-05T12:30:00Z",
  "firmware_sha256": "<64 lowercase hex characters>",
  "source_commit": "<40 lowercase hex characters>",
  "build_id": "<1-32 safe characters>",
  "device": {"model": "Model B", "flash_bytes": 16777216},
  "tests": {
    "boot_identity": true,
    "sd_journal_readback_replay": true,
    "upload_continuity": true,
    "live_usb_telemetry_capture_ages": true,
    "dashboard_disconnect_recording_continues": true,
    "car_off_60_minute_gate": true,
    "motion_cancellation": true,
    "first_boot_acceptance": true,
    "rollback": true
  }
}
```

The angle-bracket values are placeholders, not literal JSON values. The SHA-256,
source commit, and boot build ID must match the exact packaged firmware image.
Every listed test must be the JSON boolean `true`, and the connected board must
be verified as a 16 MB Model B. This is an explicit owner attestation, not
cryptographic proof that tests occurred: retain underlying logs separately and
never mark a test true without its hardware evidence. Validation completes
before the publisher invokes `gh`, including tag lookup. Example:

```sh
python3 tools/publish_ota_release.py v1.0.1 .pio/ota-release-v1.0.1 \
  --hardware-evidence /private/path/model-b-acceptance.json
```

Create the evidence file outside the repository with restrictive permissions;
do not commit or upload it. The normal release preflight still runs after this
gate and can independently refuse publication.

For production releases, enable GitHub release immutability for this repository
and use a draft release: GitHub locks its tag and assets when the draft is
published. Before publishing the draft, confirm its tag still resolves to the
embedded source commit. The local publisher checks the tag immediately before
upload, but GitHub does not offer an atomic compare-and-upload operation through
`gh release upload`.

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
  tools/test_ota_first_upload_policy.cpp -o /tmp/test_ota_first_upload_policy
/tmp/test_ota_first_upload_policy
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_collector_ack.cpp -o /tmp/test_ota_collector_ack
/tmp/test_ota_collector_ack
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_sha256_sidecar.cpp -o /tmp/test_ota_sha256_sidecar
/tmp/test_ota_sha256_sidecar
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_version_policy.cpp -o /tmp/test_ota_version_policy
/tmp/test_ota_version_policy
g++ -std=c++11 -Wall -Wextra -Werror -pedantic \
  tools/test_ota_release_policy.cpp -o /tmp/test_ota_release_policy
/tmp/test_ota_release_policy
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
python3 -m unittest discover -s tools -p 'test_ota_partition_table.py' -v
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
