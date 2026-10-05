import unittest
from pathlib import Path

from telemetry_catalog import metric_catalog


class RecordingGapMetricsTests(unittest.TestCase):
    def test_gap_cause_fields_have_stable_catalogue_entries(self) -> None:
        catalog = metric_catalog()
        expected = {
            0x9E: "buffer_exhaustion_readings",
            0x9F: "sd_unavailable_readings",
            0xA6: "journal_commit_failures",
            0xA7: "sample_deadline_overruns",
        }
        for pid, key in expected.items():
            with self.subTest(pid=hex(pid)):
                self.assertEqual(catalog[pid].key, key)
                self.assertEqual(catalog[pid].unit, "count")
                self.assertEqual(catalog[pid].decoder, "integer")

    def test_collector_protocol_ids_match_the_firmware(self) -> None:
        root = Path(__file__).resolve().parents[1]
        firmware = (root / "lib/FreematicsPlus/FreematicsBase.h").read_text(encoding="utf-8")
        collector = (root / "collector/logdata.h").read_text(encoding="utf-8")
        for name, value in (
            ("PID_BUFFER_EXHAUSTION_READINGS", "0x9E"),
            ("PID_SD_UNAVAILABLE_READINGS", "0x9F"),
            ("PID_JOURNAL_COMMIT_FAILURES", "0xA6"),
            ("PID_SAMPLE_DEADLINE_OVERRUNS", "0xA7"),
        ):
            with self.subTest(name=name):
                self.assertIn(f"#define {name} {value}", firmware)
                self.assertIn(f"#define {name} {value}", collector)

    def test_prometheus_exporter_exposes_each_cause(self) -> None:
        root = Path(__file__).resolve().parents[1]
        exporter = (root / "collector/teleserver.c").read_text(encoding="utf-8")
        for name, pid in (
            ("buffer_exhaustion_readings", "PID_BUFFER_EXHAUSTION_READINGS"),
            ("sd_unavailable_readings", "PID_SD_UNAVAILABLE_READINGS"),
            ("journal_commit_failures", "PID_JOURNAL_COMMIT_FAILURES"),
            ("sample_deadline_overruns", "PID_SAMPLE_DEADLINE_OVERRUNS"),
        ):
            with self.subTest(name=name):
                self.assertIn(f"freematics_device_{name}", exporter)
                self.assertIn(f"pld->data + {pid}", exporter)


if __name__ == "__main__":
    unittest.main()
