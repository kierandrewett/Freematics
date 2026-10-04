"""Fixture-only tests for the isolated OTA compile-config generator."""

from __future__ import annotations

import unittest

from ota_release_config import generate_header


_FIXTURE = """\
#ifndef LOCAL_CONFIG_H_INCLUDED
#define LOCAL_CONFIG_H_INCLUDED
#define ENABLE_OBD 1
#define ENABLE_MEMS 0
#define GNSS GNSS_STANDALONE
#define STORAGE STORAGE_SD
#define ENABLE_WIFI 0
#define PREFER_CELLULAR 1
#define SERVER_PORT 443
#define SERVER_PROTOCOL PROTOCOL_HTTPS_POST
#define ENABLE_BLE 0
#define ENABLE_HTTPD 0
#define SERVER_HOST "private.example.test"
#define SERVER_PATH "/private/fixture"
#define CELL_APN "fixture-apn"
#define APN_USERNAME "fixture-user"
#define APN_PASSWORD "fixture-password"
#define WIFI_SSID "fixture-ssid"
#define WIFI_PASSWORD "fixture-wifi-password"
#define SIM_CARD_PIN "1234"
#define SERVER_TOKEN "fixture-token"
#define FREEMATICS_TOKEN "fixture-token-env"
#endif
"""


class OtaReleaseConfigTests(unittest.TestCase):
    def test_preserves_storage_and_transport_compatibility_macros(self) -> None:
        header = generate_header(_FIXTURE)
        for expected in (
            "#define ENABLE_OBD 1",
            "#define ENABLE_MEMS 0",
            "#define GNSS GNSS_STANDALONE",
            "#define STORAGE STORAGE_SD",
            "#define ENABLE_WIFI 0",
            "#define ENABLE_BLE 0",
            "#define ENABLE_HTTPD 0",
            "#define PREFER_CELLULAR 1",
            "#define SERVER_PORT 443",
            "#define SERVER_PROTOCOL PROTOCOL_HTTPS_POST",
        ):
            with self.subTest(macro=expected.split()[1]):
                self.assertIn(expected, header)

    def test_excludes_all_network_identity_and_credential_keys_and_values(self) -> None:
        header = generate_header(_FIXTURE)
        forbidden_keys = (
            "SERVER_HOST", "SERVER_PATH", "CELL_APN", "APN_USERNAME",
            "APN_PASSWORD", "WIFI_SSID", "WIFI_PASSWORD", "SIM_CARD_PIN",
            "SERVER_TOKEN", "FREEMATICS_TOKEN",
        )
        forbidden_values = (
            "private.example.test", "/private/fixture", "fixture-apn",
            "fixture-user", "fixture-password", "fixture-ssid",
            "fixture-wifi-password", "1234", "fixture-token",
            "fixture-token-env",
        )
        for item in (*forbidden_keys, *forbidden_values):
            with self.subTest(excluded=item):
                self.assertNotIn(item, header)

    def test_rejects_injection_and_unapproved_identifiers(self) -> None:
        attacks = (
            ("ENABLE_WIFI", "1; #define SERVER_HOST injected"),
            ("SERVER_PORT", "443 /* injected */"),
            ("GNSS", "GNSS_STANDALONE + 1"),
            ("STORAGE", "OTHER_STORAGE"),
            ("SERVER_PROTOCOL", "PROTOCOL_HTTPS_POST; #define X 1"),
        )
        for key, value in attacks:
            with self.subTest(key=key):
                fixture = _FIXTURE.replace(
                    next(line for line in _FIXTURE.splitlines() if line.startswith(f"#define {key} ")),
                    f"#define {key} {value}",
                )
                with self.assertRaises(ValueError):
                    generate_header(fixture)

    def test_rejects_malformed_config_structure(self) -> None:
        with self.assertRaises(ValueError):
            generate_header(_FIXTURE.replace("#endif\n", "", 1))


if __name__ == "__main__":
    unittest.main()
