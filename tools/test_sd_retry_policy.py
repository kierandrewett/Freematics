#!/usr/bin/env python3
"""Test the recorder's bounded SD recovery backoff and firmware integration."""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SdRetryPolicyTests(unittest.TestCase):
    def test_backoff_limits_retries_and_resets_after_recovery(self) -> None:
        source = ROOT / "tools/test_sd_retry_policy.cpp"
        with tempfile.TemporaryDirectory(prefix="freematics-sd-retry-") as directory:
            binary = Path(directory) / "test_sd_retry_policy"
            subprocess.run(
                ["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 str(source), "-o", str(binary)],
                check=True,
            )
            subprocess.run([str(binary)], check=True)

    def test_recorder_uses_backoff_only_while_storage_is_unhealthy(self) -> None:
        firmware = (ROOT / "telelogger.ino").read_text(encoding="utf-8")
        start = firmware.index("void recordSamples(void*)")
        end = firmware.index("\nvoid ", start + len("void recordSamples(void*)"))
        recorder = firmware[start:end]
        self.assertIn("static SDRecoveryBackoff sdRecoveryBackoff", recorder)
        self.assertIn("storageUnavailable && sdRecoveryBackoff.due", recorder)
        self.assertIn("sdRecoveryBackoff.recordAttempt", recorder)
        self.assertIn(
            "if (!storageUnavailable && durableQueue.healthy()) sdRecoveryBackoff.reset();",
            recorder,
        )

    def test_startup_mount_retry_restarts_spi_after_sd_cleanup(self) -> None:
        firmware = (ROOT / "telestore.cpp").read_text(encoding="utf-8")
        start = firmware.index("bool SDLogger::init()")
        end = firmware.index("\nuint32_t SDLogger::begin()", start)
        init = firmware[start:end]
        retry_start = init.index("if (attempt) {")
        retry_end = init.index("\n        }", retry_start)
        retry = init[retry_start:retry_end]
        self.assertLess(retry.index("SD.end();"), retry.index("SPI.end();"))
        self.assertLess(retry.index("SPI.end();"), retry.index("delay(250);"))


if __name__ == "__main__":
    unittest.main()
