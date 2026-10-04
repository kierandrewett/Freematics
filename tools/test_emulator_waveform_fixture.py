from __future__ import annotations

import unittest

from tools.emulator.run import waveform_journal_payload


class WaveformFixtureTests(unittest.TestCase):
    def test_validates_serializer_checksum_and_restores_journal_delimiter(self) -> None:
        body = "0:1000,A5:1,A0:990;1234,"
        checksum = sum(body[:-1].encode("ascii")) & 0xFF
        serialized = f"{body[:-1]}*{checksum:X}\n"

        self.assertEqual(waveform_journal_payload(serialized), body)

    def test_rejects_corrupt_checksum(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "checksum does not match"):
            waveform_journal_payload("0:1000,A5:1*00")

    def test_rejects_missing_checksum(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "one production checksum"):
            waveform_journal_payload("0:1000,A5:1,")


if __name__ == "__main__":
    unittest.main()
