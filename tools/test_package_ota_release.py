#!/usr/bin/env python3
"""Host tests for the local OTA release packager."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from package_ota_release import ASSET_NAME, SIDECAR_NAME, package_release


class PackageOtaReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.firmware = self.root / ".pio" / "build" / "esp32dev" / "firmware.bin"
        self.firmware.parent.mkdir(parents=True)
        self.payload = (b"FREEMATICS_CREDENTIAL_TOKEN_ABSENT=1"
                        b"FREEMATICS_OTA_RELEASE_BUILD=1" +
                        b"FREEMATICS_RELEASE_VERSION=\x00" +
                        b"FREEMATICS_RELEASE_VERSION=1.0.1\x00" +
                        bytes(range(256)) * 17 + b"firmware\x00payload")
        self.firmware.write_bytes(self.payload)
        self.output = self.root / "release-assets"

    def tearDown(self):
        self.temp.cleanup()

    def test_names_contents_and_sha256sum_format(self):
        image, sidecar = package_release(self.firmware, self.output)
        self.assertEqual(image.name, ASSET_NAME)
        self.assertEqual(sidecar.name, SIDECAR_NAME)
        self.assertEqual(image.read_bytes(), self.payload)
        expected = f"{hashlib.sha256(self.payload).hexdigest()}  {ASSET_NAME}\n"
        self.assertEqual(sidecar.read_text(encoding="ascii"), expected)

    def test_refuses_to_overwrite_existing_asset(self):
        self.output.mkdir()
        image = self.output / ASSET_NAME
        image.write_bytes(b"keep-existing")
        with self.assertRaises(FileExistsError):
            package_release(self.firmware, self.output)
        self.assertEqual(image.read_bytes(), b"keep-existing")
        self.assertFalse((self.output / SIDECAR_NAME).exists())

    def test_refuses_existing_sidecar_without_creating_firmware(self):
        self.output.mkdir()
        sidecar = self.output / SIDECAR_NAME
        sidecar.write_text("keep-existing\n", encoding="ascii")
        with self.assertRaises(FileExistsError):
            package_release(self.firmware, self.output)
        self.assertEqual(sidecar.read_text(encoding="ascii"), "keep-existing\n")
        self.assertFalse((self.output / ASSET_NAME).exists())

    def test_existing_assets_are_never_replaced(self):
        self.output.mkdir()
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
            with self.assertRaisesRegex(ValueError, "contains a configured credential") as raised:
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
            with self.assertRaisesRegex(ValueError, "contains a configured credential"):
                package_release(self.firmware, self.output)
        self.assertFalse(self.output.exists())

    def test_refuses_an_image_containing_a_configured_apn_password(self):
        secret = "fixture-apn-password-do-not-embed"
        self.firmware.write_bytes(self.payload + secret.encode("ascii"))
        with patch("package_ota_release._parse_defines",
                   return_value={"APN_PASSWORD": f'"{secret}"'}), patch(
            "package_ota_release.load_build_environment", return_value={}
        ):
            with self.assertRaisesRegex(ValueError, "contains a configured credential") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
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
            with self.assertRaisesRegex(ValueError, "contains a configured credential") as raised:
                package_release(self.firmware, self.output)
        self.assertNotIn(secret, str(raised.exception))
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
