from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from migrate_archive_metrics import migrate_file


class ArchiveMigrationTests(unittest.TestCase):
    def test_migration_adds_projection_without_changing_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.jsonl"
            original = {
                "schema": 1,
                "record_type": "batch",
                "event_id": "batch-1",
                "raw_payload_b64": "AA==",
            }
            sample = {
                "schema": 1,
                "record_type": "sample",
                "event_id": "sample-1",
                "fields": [{"pid": "0x10C", "value": "900"}],
                "raw_frame": "0:100,10C:900",
                "raw_frame_b64": "MDoxMDAsMTBDOjkwMA==",
            }
            path.write_text("\n".join(json.dumps(item) for item in (original, sample)) + "\n")

            self.assertEqual(migrate_file(path), (2, 3))
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["raw_payload_b64"], "AA==")
            self.assertEqual(records[1]["raw_frame"], "0:100,10C:900")
            self.assertEqual(records[1]["metrics"]["engine_rpm"]["value"], 900)
            self.assertEqual(records[1]["metric_values"], {"engine_rpm": 900})
            self.assertEqual(migrate_file(path), (2, 0))


if __name__ == "__main__":
    unittest.main()
