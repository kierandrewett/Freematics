from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest

from build_secret_header import HEADER_NAME, write_build_secret_header


class BuildSecretHeaderTests(unittest.TestCase):
    def test_writes_token_to_owner_only_header(self) -> None:
        token = "a1" * 32
        with tempfile.TemporaryDirectory() as temp:
            header = write_build_secret_header(Path(temp), token)
            self.assertEqual(header.name, HEADER_NAME)
            self.assertIn(f'#define SERVER_TOKEN "{token}"', header.read_text())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(header.stat().st_mode), 0o600)

    def test_empty_token_defines_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            header = write_build_secret_header(Path(temp), "")
            self.assertIn('#define SERVER_TOKEN ""', header.read_text())

    def test_rejects_unvalidated_token_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for invalid in ('x' * 63, 'g' * 64, '";#error leak'):
                with self.subTest(invalid_length=len(invalid)):
                    with self.assertRaises(ValueError):
                        write_build_secret_header(Path(temp), invalid)


if __name__ == "__main__":
    unittest.main()
