from __future__ import annotations

import unittest

from production_config import validate_production_config


VALID_CONFIG = """
#define SERVER_HOST "freematics.drewett.dev"
#define SERVER_PORT 443
#define SERVER_PROTOCOL PROTOCOL_HTTPS_POST
#define SERVER_PATH "/api"
#define CELL_APN "simbase"
"""


class ProductionConfigTests(unittest.TestCase):
    def test_valid_deployment_config_is_accepted(self) -> None:
        validate_production_config(VALID_CONFIG)

    def test_missing_required_definition_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "SERVER_PROTOCOL"):
            validate_production_config(VALID_CONFIG.replace("#define SERVER_PROTOCOL PROTOCOL_HTTPS_POST\n", ""))

    def test_builtin_fallbacks_are_rejected(self) -> None:
        for fallback in (
            VALID_CONFIG.replace('"freematics.drewett.dev"', '"hub.freematics.com"'),
            VALID_CONFIG.replace('"simbase"', '""'),
            VALID_CONFIG.replace('"/api"', '"/hub/api"'),
        ):
            with self.assertRaises(ValueError):
                validate_production_config(fallback)

    def test_non_https_production_settings_are_rejected(self) -> None:
        for invalid in (
            VALID_CONFIG.replace("443", "8081"),
            VALID_CONFIG.replace("PROTOCOL_HTTPS_POST", "PROTOCOL_UDP"),
        ):
            with self.assertRaises(ValueError):
                validate_production_config(invalid)

    def test_duplicate_required_definition_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate SERVER_HOST"):
            validate_production_config(f'{VALID_CONFIG}\n#define SERVER_HOST "other.example"\n')


if __name__ == "__main__":
    unittest.main()
