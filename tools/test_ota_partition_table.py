"""Guard the OTA app-slot layout and 16 MB address map."""

from __future__ import annotations

import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read_partitions() -> dict[str, tuple[str, str, int, int]]:
    result = {}
    with (ROOT / "default_16MB.csv").open(newline="", encoding="utf-8") as source:
        for row in csv.reader(line for line in source if not line.lstrip().startswith("#")):
            if not row or not row[0].strip():
                continue
            name, kind, subtype, offset, size, *_flags = (column.strip() for column in row)
            result[name] = (kind, subtype, int(offset, 0), int(size, 0))
    return result


class OtaPartitionTableTests(unittest.TestCase):
    def test_declares_two_aligned_non_overlapping_ota_slots(self):
        partitions = read_partitions()
        app0 = partitions["app0"]
        app1 = partitions["app1"]
        self.assertEqual(app0[:2], ("app", "ota_0"))
        self.assertEqual(app1[:2], ("app", "ota_1"))
        self.assertEqual(app0[2], 0x10000)
        self.assertEqual(app0[3], app1[3])
        self.assertEqual(app1[2], app0[2] + app0[3])
        self.assertEqual(app1[2] % 0x10000, 0)
        self.assertLessEqual(app1[2] + app1[3], 16 * 1024 * 1024)
        self.assertEqual(partitions["otadata"][:2], ("data", "ota"))


if __name__ == "__main__":
    unittest.main()
