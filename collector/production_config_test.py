from __future__ import annotations

import unittest

from production_config import validate_production_config


VALID_CONFIG = """
#ifndef LOCAL_CONFIG_H_INCLUDED
#define LOCAL_CONFIG_H_INCLUDED
#define SERVER_HOST "freematics.drewett.dev"
#define SERVER_PORT 443
#define SERVER_PROTOCOL PROTOCOL_HTTPS_POST
#define SERVER_PATH "/api"
#define CELL_APN "simbase"
#endif
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
        duplicate = VALID_CONFIG.replace(
            "#endif",
            '#define SERVER_HOST "other.example"\n#endif',
        )
        with self.assertRaisesRegex(ValueError, "duplicate SERVER_HOST"):
            validate_production_config(duplicate)

    def test_include_guard_is_required_and_contains_all_definitions(self) -> None:
        unguarded = VALID_CONFIG.replace("#ifndef LOCAL_CONFIG_H_INCLUDED\n#define LOCAL_CONFIG_H_INCLUDED\n", "")
        unguarded = unguarded.replace("#endif\n", "")
        with self.assertRaisesRegex(ValueError, "include guard"):
            validate_production_config(unguarded)

        trailing = VALID_CONFIG + '#define SERVER_HOST "other.example"\n'
        with self.assertRaisesRegex(ValueError, "after include guard"):
            validate_production_config(trailing)

    def test_unsupported_preprocessor_controls_are_rejected(self) -> None:
        for directive in (
            "#if 0\n",
            "#undef SERVER_HOST\n",
            "#include \"other_config.h\"\n",
        ):
            candidate = VALID_CONFIG.replace("#endif", f"{directive}#endif")
            with self.assertRaisesRegex(ValueError, "preprocessor directive"):
                validate_production_config(candidate)

    def test_line_continuations_are_rejected(self) -> None:
        escaped = VALID_CONFIG.replace('"/api"', r'"/\x61pi"')
        with self.assertRaisesRegex(ValueError, "line continuations"):
            validate_production_config(escaped)


if __name__ == "__main__":
    unittest.main()
