#!/usr/bin/env python3
"""Exercise the production SDLogger::init body against deterministic SPI/SD stubs."""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def extract_function(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


HARNESS = r"""
#include <assert.h>
#include <stdint.h>

#define PIN_SD_CS 5
#define SPI_FREQ 1000000U
static uint32_t clockMs;
uint32_t millis() { return clockMs; }
void delay(uint32_t duration) { clockMs += duration; }

struct SerialStub {
  template <typename T> void print(const T&) {}
  template <typename T> void println(const T&) {}
} Serial;

struct SPIClass {
  bool active = false;
  unsigned starts = 0;
  unsigned stops = 0;
  void begin() { if (!active) { active = true; ++starts; } }
  void end() { if (active) { active = false; ++stops; } }
} SPI;

struct SDStub {
  bool mounted = false;
  unsigned attempts = 0;
  bool outcomes[3] = {false, false, false};
  void set(bool first, bool second, bool third) {
    mounted = false;
    attempts = 0;
    outcomes[0] = first; outcomes[1] = second; outcomes[2] = third;
  }
  bool begin(uint8_t, SPIClass& spi, uint32_t) {
    spi.begin();
    assert(attempts < 3);
    const bool success = outcomes[attempts++];
    mounted = success;
    return success;
  }
  void end() { if (mounted) mounted = false; }
  uint64_t totalBytes() { return 4ULL << 30; }
  uint64_t usedBytes() { return 1ULL << 30; }
} SD;

struct SDGuard { explicit operator bool() const { return true; } };
class SDLogger { public: bool init(); };

"""


class SDMountRetryTests(unittest.TestCase):
    def test_init_retries_with_real_spi_reinitialization_and_stops_on_success(self) -> None:
        source = (ROOT / "telestore.cpp").read_text(encoding="utf-8")
        init = extract_function(source, "bool SDLogger::init()")
        main = r"""
int main() {
  SDLogger logger;

  clockMs = 0; SPI = SPIClass{}; SD = SDStub{}; SD.set(true, false, false);
  assert(logger.init());
  assert(SD.attempts == 1 && SPI.starts == 1 && SPI.stops == 0 && clockMs == 0);

  clockMs = 0; SPI = SPIClass{}; SD = SDStub{}; SD.set(false, true, false);
  assert(logger.init());
  assert(SD.attempts == 2 && SPI.starts == 2 && SPI.stops == 1 && clockMs == 250);

  clockMs = 0; SPI = SPIClass{}; SD = SDStub{}; SD.set(false, false, false);
  assert(!logger.init());
  assert(SD.attempts == 3 && SPI.starts == 3 && SPI.stops == 2 && clockMs == 500);
  assert(!SD.mounted);
  return 0;
}
"""
        with tempfile.TemporaryDirectory(prefix="freematics-sd-mount-") as directory:
            harness = Path(directory) / "test_sd_mount_retry.cpp"
            binary = Path(directory) / "test_sd_mount_retry"
            harness.write_text(HARNESS + init + main, encoding="utf-8")
            subprocess.run(
                ["g++", "-std=c++11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 str(harness), "-o", str(binary)],
                check=True,
            )
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
