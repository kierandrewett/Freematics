#!/usr/bin/env python3
"""Host tests for the local OTA release packager."""

import base64
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from package_ota_release import (
    ASSET_NAME,
    SIDECAR_NAME,
    _credential_signatures,
    _current_source_commit,
    _image_contains_c_string,
    _image_contains_value,
    firmware_source_commit,
    package_release,
    verify_release_directory,
)


class PackageOtaReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.firmware = self.root / ".pio" / "build" / "esp32dev" / "firmware.bin"
        self.firmware.parent.mkdir(parents=True)
        self.source_commit = _current_source_commit()
        self.payload = (
            b"FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1"
            b"FREEMATICS_OTA_RELEASE_BUILD=1"
            b"FREEMATICS_RELEASE_VERSION=\x00"
            b"FREEMATICS_RELEASE_VERSION=1.0.1\x00"
            b"FREEMATICS_SOURCE_COMMIT="
            + self.source_commit.encode("ascii")
            + b"\x00"
            + bytes(range(256)) * 17
            + b"firmware\x00payload"
        )
        self.firmware.write_bytes(self.payload)
        self.output = self.root / "release-assets"
        self.source_commit_check = patch(
            "package_ota_release._current_source_commit",
            return_value=self.source_commit,
        )
        self.source_commit_check.start()
        self.checkout_guard = patch(
            "package_ota_release._source_commit_matches_checkout", return_value=True
        )
        self.checkout_guard.start()

    def tearDown(self):
        self.checkout_guard.stop()
        self.source_commit_check.stop()
        self.temp.cleanup()

    def test_names_contents_and_sha256sum_format(self):
        image, sidecar = package_release(self.firmware, self.output)
        self.assertEqual(image.name, ASSET_NAME)
        self.assertEqual(sidecar.name, SIDECAR_NAME)
        self.assertEqual(image.read_bytes(), self.payload)
        expected = f"{hashlib.sha256(self.payload).hexdigest()}  {ASSET_NAME}\n"
        self.assertEqual(sidecar.read_text(encoding="ascii"), expected)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(image.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)

    def test_verifier_accepts_exact_unmodified_asset_pair(self):
        image, sidecar = package_release(self.firmware, self.output)
        self.assertEqual(verify_release_directory(self.output), (image, sidecar))

    def test_source_commit_marker_matches_current_checkout(self):
        self.assertEqual(firmware_source_commit(self.firmware), self.source_commit)

    def test_refuses_image_when_source_tree_has_non_documentation_changes(self):
        with patch("package_ota_release._source_commit_matches_checkout", return_value=False):
            with self.assertRaisesRegex(ValueError, "source does not match"):
                package_release(self.firmware, self.output)

    def test_source_commit_marker_can_cross_binary_scan_chunks(self):
        with patch("package_ota_release.IMAGE_SCAN_CHUNK_BYTES", 17):
            self.assertEqual(firmware_source_commit(self.firmware), self.source_commit)

    def test_refuses_image_from_a_different_source_commit(self):
        foreign_commit = (
            ("0" if self.source_commit[0] != "0" else "1") + self.source_commit[1:]
        )
        self.firmware.write_bytes(
            self.payload.replace(
                self.source_commit.encode("ascii"), foreign_commit.encode("ascii")
            )
        )
        with self.assertRaisesRegex(ValueError, "does not match the current checkout"):
            with patch("package_ota_release._source_commit_matches_checkout", return_value=False):
                package_release(self.firmware, self.output)

    def test_refuses_image_without_source_commit_marker(self):
        self.firmware.write_bytes(
            self.payload.replace(
                b"FREEMATICS_SOURCE_COMMIT=", b"FREEMATICS_OLD_COMMIT="
            )
        )
        with self.assertRaisesRegex(ValueError, "valid source commit marker"):
            package_release(self.firmware, self.output)

    def test_image_scanner_handles_chunk_boundaries_and_one_byte_values(self):
        self.firmware.write_bytes(b"abcz1234")
        with patch("package_ota_release.IMAGE_SCAN_CHUNK_BYTES", 4):
            self.assertTrue(_image_contains_value(self.firmware, b"z1"))
            self.assertTrue(_image_contains_value(self.firmware, b"a"))
            self.assertFalse(_image_contains_value(self.firmware, b"q"))

    def test_private_path_scanner_requires_a_complete_c_string(self):
        self.firmware.write_bytes(b"unrelated-prefix/private-path-suffix\x00")
        self.assertFalse(_image_contains_c_string(self.firmware, b"/private-path"))
        self.firmware.write_bytes(b"/private-path\x00")
        self.assertTrue(_image_contains_c_string(self.firmware, b"/private-path"))

    def test_verifier_rejects_modified_firmware(self):
        image, _ = package_release(self.firmware, self.output)
        image.write_bytes(self.payload + b"modified")
        with self.assertRaisesRegex(ValueError, "checksum does not match"):
            verify_release_directory(self.output)

    def test_verifier_rejects_extra_files(self):
        package_release(self.firmware, self.output)
        (self.output / "debug.log").write_text("private", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "exactly the approved"):
            verify_release_directory(self.output)

    def test_verifier_rejects_replaced_asset_containing_configured_secret(self):
        package_release(self.firmware, self.output)
        image = self.output / ASSET_NAME
        secret = b"private-fixture-credential"
        image.write_bytes(self.payload + secret)
        with patch("package_ota_release._configured_credentials", return_value={secret}):
            with self.assertRaisesRegex(ValueError, "contains a configured private value"):
                verify_release_directory(self.output)

    @unittest.skipUnless(os.name == "posix", "POSIX permission policy")
    def test_refuses_a_shared_output_directory(self):
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "must be owner-only"):
            package_release(self.firmware, self.output)
        self.assertFalse((self.output / ASSET_NAME).exists())

    def test_refuses_to_mix_unrelated_files_into_the_upload_directory(self):
        self.output.mkdir(mode=0o700)
        unrelated = self.output / "build.log"
        unrelated.write_text("private diagnostic data", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be empty before packaging"):
            package_release(self.firmware, self.output)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "private diagnostic data")
        self.assertFalse((self.output / ASSET_NAME).exists())
        self.assertFalse((self.output / SIDECAR_NAME).exists())

    def test_refuses_to_overwrite_existing_asset(self):
        self.output.mkdir(mode=0o700)
        image = self.output / ASSET_NAME
        image.write_bytes(b"keep-existing")
        with self.assertRaises(FileExistsError):
            package_release(self.firmware, self.output)
        self.assertEqual(image.read_bytes(), b"keep-existing")
        self.assertFalse((self.output / SIDECAR_NAME).exists())

    def test_refuses_existing_sidecar_without_creating_firmware(self):
        self.output.mkdir(mode=0o700)
        sidecar = self.output / SIDECAR_NAME
        sidecar.write_text("keep-existing\n", encoding="ascii")
        with self.assertRaises(FileExistsError):
            package_release(self.firmware, self.output)
        self.assertEqual(sidecar.read_text(encoding="ascii"), "keep-existing\n")
        self.assertFalse((self.output / ASSET_NAME).exists())

    def test_existing_assets_are_never_replaced(self):
        self.output.mkdir(mode=0o700)
        image = self.output / ASSET_NAME
        sidecar = self.output / SIDECAR_NAME
        image.write_bytes(b"old-image")
        sidecar.write_text("old-sidecar\n", encoding="ascii")
        with self.assertRaises(FileExistsError):
            package_release(self.firmware, self.output)
        self.assertEqual(image.read_bytes(), b"old-image")
        self.assertEqual(sidecar.read_text(encoding="ascii"), "old-sidecar\n")

    def test_refuses_to_package_an_image_containing_configured_token(self):
        token = "a1" * 32
        self.firmware.write_bytes(self.payload + token.encode("ascii"))
        with patch.dict("os.environ", {"FREEMATICS_TOKEN": token}):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(token, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_scans_private_env_token_when_release_build_clears_process_token(self):
        token = "b2" * 32
        self.firmware.write_bytes(self.payload + token.encode("ascii"))
        with patch.dict("os.environ", {"FREEMATICS_TOKEN": ""}), patch(
            "package_ota_release.load_build_environment",
            return_value={"FREEMATICS_TOKEN": token},
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_an_image_containing_a_configured_apn_password(self):
        secret = "fixture-apn-password-do-not-embed"
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch("package_ota_release._parse_defines",
                   return_value={"APN_PASSWORD": f'"{secret}"'}), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_refuses_an_image_containing_a_configured_login_username(self):
        secret = "fixture-apn-username-do-not-embed"
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch("package_ota_release._parse_defines",
                   return_value={"APN_USERNAME": f'"{secret}"'}), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_scans_project_username_environment_without_scanning_os_user(self):
        secret = "fixture-apn-user-from-environment"
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch.dict("os.environ", {"APN_USERNAME": secret}):
            with self.assertRaisesRegex(ValueError, "contains a configured private value"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_an_image_containing_a_configured_sim_pin(self):
        pin = "fixture-sim-pin"
        self.firmware.write_bytes(self.payload + pin.encode("ascii"))
        with patch("package_ota_release._parse_defines",
                   return_value={"SIM_CARD_PIN": f'"{pin}"'}), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(pin, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_refuses_an_image_containing_a_configured_wifi_ssid(self):
        ssid = "fixture-private-wifi-name"
        self.firmware.write_bytes(self.payload + ssid.encode("ascii"))
        with patch("package_ota_release._parse_defines",
                   return_value={"WIFI_SSID": f'"{ssid}"'}), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_common_encoded_forms_of_configured_credentials(self):
        secret = b"fixture-apn-password-value"
        encoded_forms = {
            base64.b64encode(secret),
            secret.hex().encode("ascii"),
            secret.decode("ascii").encode("utf-16le"),
        }
        for index, encoded in enumerate(encoded_forms):
            with self.subTest(encoding=index):
                self.firmware.write_bytes(self.payload + encoded)
                with patch(
                    "package_ota_release._configured_credentials",
                    return_value=_credential_signatures(secret),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "contains a configured private value"
                    ):
                        package_release(self.firmware, self.output)
                self.assertFalse(self.output.exists())

    def test_scans_apn_user_alias_in_private_env(self):
        username = "fixture-apn-user-alias"
        (self.root / ".env").write_text(f"APN_USER={username}\n", encoding="utf-8")
        (self.root / "local_config.h").write_text(
            "#ifndef LOCAL_CONFIG_H_INCLUDED\n"
            "#define LOCAL_CONFIG_H_INCLUDED\n"
            "#endif\n",
            encoding="utf-8",
        )
        self.firmware.write_bytes(self.payload + username.encode("ascii"))
        with patch("package_ota_release.REPOSITORY_ROOT", self.root), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_scans_credential_fields_even_when_build_loader_ignores_them(self):
        secret = "fixture-apn-password-from-private-config"
        (self.root / "local_config.h").write_text(
            "#ifndef LOCAL_CONFIG_H_INCLUDED\n"
            "#define LOCAL_CONFIG_H_INCLUDED\n"
            f'#define APN_PASSWORD "{secret}"\n'
            "#endif\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            f"APN_PASSWORD={secret}\n", encoding="utf-8"
        )
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch("package_ota_release.REPOSITORY_ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_refuses_private_server_and_apn_settings_from_local_config(self):
        settings = {
            "SERVER_HOST": "private-server-fixture.invalid",
            "SERVER_PATH": "/private-upload-fixture",
            "CELL_APN": "private-apn-fixture",
        }
        for key, value in settings.items():
            with self.subTest(key=key):
                config_lines = [
                    "#ifndef LOCAL_CONFIG_H_INCLUDED",
                    "#define LOCAL_CONFIG_H_INCLUDED",
                    f'#define {key} "{value}"',
                    "#endif",
                ]
                (self.root / "local_config.h").write_text(
                    "\n".join(config_lines) + "\n", encoding="utf-8"
                )
                separator = b"\x00" if key == "SERVER_PATH" else b""
                self.firmware.write_bytes(
                    self.payload + separator + value.encode("ascii")
                    + (b"\x00" if key == "SERVER_PATH" else b"")
                )
                with patch("package_ota_release.REPOSITORY_ROOT", self.root), patch(
                    "package_ota_release.load_build_environment", return_value={}
                ):
                    with self.assertRaisesRegex(
                        ValueError, "contains a configured private value"
                    ) as raised:
                        package_release(self.firmware, self.output)
                self.assertNotIn(value, str(raised.exception))
                self.assertFalse(self.output.exists())

    def test_refuses_a_credential_literal_followed_by_a_cpp_comment(self):
        secret = "fixture-apn-password-with-comment"
        (self.root / "local_config.h").write_text(
            "#ifndef LOCAL_CONFIG_H_INCLUDED\n"
            "#define LOCAL_CONFIG_H_INCLUDED\n"
            f'#define APN_PASSWORD "{secret}" // production APN password\n'
            "#endif\n",
            encoding="utf-8",
        )
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch("package_ota_release.REPOSITORY_ROOT", self.root), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_refuses_a_credential_split_across_adjacent_c_strings(self):
        secret = "fixture-apn-password-concatenated"
        (self.root / "local_config.h").write_text(
            "#ifndef LOCAL_CONFIG_H_INCLUDED\n"
            "#define LOCAL_CONFIG_H_INCLUDED\n"
            '#define APN_PASSWORD "fixture-apn-" "password-concatenated"\n'
            "#endif\n",
            encoding="utf-8",
        )
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch("package_ota_release.REPOSITORY_ROOT", self.root), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured private value") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_refuses_an_unresolved_credential_macro(self):
        (self.root / "local_config.h").write_text(
            "#ifndef LOCAL_CONFIG_H_INCLUDED\n"
            "#define LOCAL_CONFIG_H_INCLUDED\n"
            "#define APN_PASSWORD EXTERNAL_APN_PASSWORD\n"
            "#endif\n",
            encoding="utf-8",
        )
        with patch("package_ota_release.REPOSITORY_ROOT", self.root), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "cannot verify a configured private value"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_a_raw_c_string_credential_literal(self):
        (self.root / "local_config.h").write_text(
            "#ifndef LOCAL_CONFIG_H_INCLUDED\n"
            "#define LOCAL_CONFIG_H_INCLUDED\n"
            '#define APN_PASSWORD R"(fixture-apn-password-raw)"\n'
            "#endif\n",
            encoding="utf-8",
        )
        with patch("package_ota_release.REPOSITORY_ROOT", self.root), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "prefixed credential strings"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_an_image_without_a_tokenless_build_marker(self):
        self.firmware.write_bytes(b"firmware without credential status")
        with self.assertRaisesRegex(ValueError, "token-free OTA/version marker"):
            package_release(self.firmware, self.output)

    def test_refuses_a_build_marked_as_token_bearing(self):
        self.firmware.write_bytes(b"FREEMATICS_CREDENTIAL_TOKEN_EMBEDDED=1")
        with self.assertRaisesRegex(ValueError, "token-free OTA/version marker"):
            package_release(self.firmware, self.output)

    def test_refuses_an_image_without_release_version_marker(self):
        self.firmware.write_bytes(
            b"FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1FREEMATICS_OTA_RELEASE_BUILD=1"
        )
        with self.assertRaisesRegex(ValueError, "token-free OTA/version marker"):
            package_release(self.firmware, self.output)

    def test_refuses_a_malformed_release_version_marker(self):
        self.firmware.write_bytes(self.payload.replace(b"1.0.1", b"01.0.1"))
        with self.assertRaisesRegex(ValueError, "token-free OTA/version marker"):
            package_release(self.firmware, self.output)


if __name__ == "__main__":
    unittest.main()
