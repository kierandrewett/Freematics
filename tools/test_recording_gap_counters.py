#!/usr/bin/env python3
"""Host checks for bounded capture-gap diagnostics and firmware wiring."""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecordingGapCountersTests(unittest.TestCase):
    def test_counter_math_is_bounded_and_saturating(self) -> None:
        with tempfile.TemporaryDirectory(prefix="freematics-gap-counters-") as directory:
            binary = Path(directory) / "test_recording_gap_counters"
            subprocess.run(
                ["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 str(ROOT / "tools/test_recording_gap_counters.cpp"), "-o", str(binary)],
                check=True,
            )
            subprocess.run([str(binary)], check=True)

    def test_firmware_classifies_each_gap_at_its_origin(self) -> None:
        source = (ROOT / "telelogger.ino").read_text(encoding="utf-8")
        self.assertIn("recordingGapCounters.add(RecordingGapCounters::kBufferExhaustion);", source)
        self.assertIn("durableAvailable ? RecordingGapCounters::kJournalCommitFailure :", source)
        self.assertIn("RecordingGapCounters::kSdUnavailable);", source)
        self.assertIn("recordingGapCounters.add(RecordingGapCounters::kDeadlineOverrun, missedSlots);", source)
        for pid in (
            "PID_BUFFER_EXHAUSTION_READINGS", "PID_SD_UNAVAILABLE_READINGS",
            "PID_JOURNAL_COMMIT_FAILURES", "PID_SAMPLE_DEADLINE_OVERRUNS",
        ):
            self.assertIn(f"buffer->add({pid}, ELEMENT_UINT32", source)


if __name__ == "__main__":
    unittest.main()
